# -*- coding: utf-8 -*-
r"""배치 진행현황판 — Claude 한테 물어보지 않고 로그만 읽어 표로 보여준다.

사용:
  .venv\Scripts\python.exe tools\_progress.py                 # 가장 최근 out 폴더(F:\ja_reviewer_out\ja*)
  .venv\Scripts\python.exe tools\_progress.py --out F:\ja_reviewer_out\ja26
  ... --watch            # 15초마다 화면 갱신(Ctrl+C 로 종료)
  ... --src "C:\...\26"  # 소스 폴더를 주면 아직 시작 안 한 편도 '대기'로 보인다

읽는 것: {out}\_로그\clean.log(섹션1) + 폴더 산출물(클린본/final/_완성/_검수필요).
토큰 0, GPU 0 — 그냥 텍스트 파일 파싱이다.
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

CODE_RE = re.compile(r"([A-Za-z]{2,6})-?(\d{2,5})")
STEP_NAMES = {1: "①풀전사", 2: "②줄거리", 3: "③컷·소리", 4: "④컷·의미", 5: "⑤컷·화면"}


def latest_out():
    base = Path(r"F:\ja_reviewer_out")
    cands = [p for p in base.glob("ja*") if p.is_dir()]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def guess_code(name):
    m = CODE_RE.search(Path(name).stem)
    return f"{m.group(1)}-{m.group(2)}".upper() if m else ""


def parse_clean_log(path):
    """clean.log → {code: {...}} + 전체 요약."""
    info, order = {}, []
    total = None
    cur = None
    done_line = None
    if not path.is_file():
        return info, order, total, done_line
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        m = re.match(r"대상 (\d+)개", line)
        if m:
            total = int(m.group(1)); continue
        m = re.match(r"\((\d+)/(\d+)\) (.+?) → 품번 (\S+)", line)
        if m:
            cur = m.group(4)
            total = total or int(m.group(2))
            info.setdefault(cur, {"idx": int(m.group(1)), "file": m.group(3), "step": 0,
                                  "last": "", "story": "", "result": "", "kept": "", "note": ""})
            if cur not in order:
                order.append(cur)
            continue
        m = re.match(r"^완료 (\d+)/(\d+)(.*)", line)
        if m:
            done_line = line; continue
        m = re.match(r"\[([A-Z0-9-]+)\]\s*(.*)", line)
        if not m:
            continue
        code, msg = m.group(1), m.group(2)
        d = info.setdefault(code, {"idx": 0, "file": "", "step": 0, "last": "", "story": "",
                                   "result": "", "kept": "", "note": ""})
        s = re.match(r"── \((\d)/5\)", msg)
        if s:
            d["step"] = int(s.group(1)); d["last"] = ""
            continue
        if "줄거리 브리핑 실패" in msg:
            d["story"] = "✘실패"
        elif "줄거리 요약(읽기용)" in msg:
            d["story"] = "✔"
        elif "줄거리 브리핑 재사용" in msg:
            d["story"] = "✔재사용"
        elif "줄거리 브리핑 생략" in msg:
            d["story"] = "—생략"
        if "✔ 클린본 확정" in msg:
            d["result"] = "완료"; d["step"] = 6
            k = re.search(r"확정: ([\d.]+분)", msg)
            d["kept"] = k.group(1) if k else ""
        elif "기존 클린본 재사용" in msg:
            d["result"] = "완료(재사용)"; d["step"] = 6
        elif "검출 0 — 원본 그대로" in msg:
            d["note"] = "검출0"
        elif msg.startswith("✘") or "실패" in msg and "스마트 컷 실패" not in msg and "브리핑 실패" not in msg:
            d["result"] = "✘실패"; d["note"] = msg[:60]
        # 진행률/최근 줄
        p = re.search(r"(\d+)%", msg)
        if p:
            d["last"] = msg.strip()[:40]
        elif msg and not msg.startswith("📄"):
            d["last"] = msg.strip()[:40]
    return info, order, total, done_line


def parse_review_log(path):
    """review.log(run_ja review→check→precheck) → 편별 상태 + 현재 단계 헤더."""
    info, order, stage, total = {}, [], "", None
    if not path.is_file():
        return info, order, stage, total
    cur = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        if line.startswith("▌"):
            stage = line[1:].strip(); continue
        m = re.match(r"\((\d+)/(\d+)\) \[(\d+)위\] (\S+)", line)
        if m:
            cur = m.group(4); total = int(m.group(2))
            info.setdefault(cur, {"rank": int(m.group(3)), "last": "", "result": ""})
            if cur not in order:
                order.append(cur)
            continue
        m = re.match(r"\[([A-Z0-9-]+)\]\s*(.*)", line)
        if not m:
            if "일시정지" in line:
                stage += "  ⏸ " + line.strip()[:50]
            continue
        code, msg = m.group(1), m.group(2)
        d = info.setdefault(code, {"rank": 0, "last": "", "result": ""})
        if "내레이션 자막:" in msg:
            d["result"] = "✔ 완료(final·대사·내레이션 srt)"
        elif "✔" in msg and ("keep" in msg or "이미 완료" in msg):
            d["result"] = msg.strip()[:48]
        elif msg.startswith("✘"):
            d["result"] = msg.strip()[:48]
        elif "브리핑 실패" in msg or "JSON 깨짐" in msg:
            d["last"] = msg.strip()[:48]
        elif msg and not msg.startswith("📄"):
            d["last"] = msg.strip()[:48]
    return info, order, stage, total


def folder_state(out, code):
    d = out / code
    flags = []
    if (d / f"{code}_클린.mp4").is_file():
        flags.append("클린")
    if (d / f"{code}_final.mp4").is_file():
        flags.append("final")
    if (out / "_완성" / f"{code}.mp4").is_file():
        flags.append("완성")
    if (out / "_검수필요" / f"{code}.mp4").is_file():
        flags.append("검수필요")
    return " ".join(flags)


def render(out, src=None):
    log = out / "_로그" / "clean.log"
    info, order, total, done_line = parse_clean_log(log)
    rows = []
    # 소스 폴더가 있으면 아직 시작 안 한 편도 대기로
    all_codes = list(order)
    if src and Path(src).is_dir():
        for v in sorted(Path(src).glob("*.mp4")):
            c = guess_code(v.name)
            if c and c not in all_codes:
                all_codes.append(c)
    n_done = sum(1 for c in order if info[c]["result"].startswith("완료"))
    n_fail = sum(1 for c in order if info[c]["result"].startswith("✘"))
    running = next((c for c in order if not info[c]["result"]), None)
    total = total or len(all_codes)
    mt = datetime.fromtimestamp(log.stat().st_mtime).strftime("%H:%M:%S") if log.is_file() else "-"
    stale = (time.time() - log.stat().st_mtime) > 600 if log.is_file() else False

    lines = []
    lines.append(f"▌섹션1 클린 진행현황  {out}   (로그 갱신 {mt}{'  ⚠10분 이상 멈춤?' if stale and not done_line else ''})")
    lines.append(f"  완료 {n_done}/{total}" + (f"  실패 {n_fail}" if n_fail else "")
                 + (f"  진행중 {running}" if running else "") + (f"   ★{done_line}" if done_line else ""))
    bar = int(30 * n_done / total) if total else 0
    lines.append("  [" + "█" * bar + "·" * (30 - bar) + "]")
    lines.append("")
    lines.append(f"  {'#':>2} {'품번':<11} {'상태':<9} {'단계':<8} {'클린본':<7} {'줄거리':<7} {'산출물':<14} 최근")
    lines.append("  " + "-" * 96)
    for i, c in enumerate(all_codes, 1):
        d = info.get(c)
        if not d:
            lines.append(f"  {i:>2} {c:<11} {'대기':<9}")
            continue
        if (out / c / f"{c}_줄거리.txt").is_file() and d["story"].startswith("✘"):
            d["story"] = "✔보충"
        if d["result"]:
            status = d["result"]; step = ""
        else:
            status = "진행중"; step = STEP_NAMES.get(d["step"], "시작")
        last = "" if d["result"] else d["last"]
        lines.append(f"  {d['idx'] or i:>2} {c:<11} {status:<9} {step:<8} {d['kept']:<7} {d['story']:<7} "
                     f"{folder_state(out, c):<14} {last}")
    bad = [c for c in order if info[c]["story"].startswith("✘")
           and not (out / c / f"{c}_줄거리.txt").is_file()]
    if bad:
        if bad:
            lines.append("")
            lines.append(f"  ※ 줄거리 보충 필요: {', '.join(bad)}  → tools/backfill_story.py --only {','.join(bad)}")

    # ── 섹션2 ──
    rlog = out / "_로그" / "review.log"
    if rlog.is_file():
        rinfo, rorder, rstage, rtotal = parse_review_log(rlog)
        rmt = datetime.fromtimestamp(rlog.stat().st_mtime).strftime("%H:%M:%S")
        rdone = sum(1 for c in rorder if rinfo[c]["result"].startswith("✔"))
        rfail = sum(1 for c in rorder if rinfo[c]["result"].startswith("✘"))
        rtotal = rtotal or len(rorder)
        lines.append("")
        lines.append(f"▌섹션2 리뷰 생성 진행현황   (로그 갱신 {rmt})   현재 단계: {rstage or '-'}")
        lines.append(f"  완료 {rdone}/{rtotal}" + (f"  실패 {rfail}" if rfail else ""))
        bar = int(30 * rdone / rtotal) if rtotal else 0
        lines.append("  [" + "█" * bar + "·" * (30 - bar) + "]")
        lines.append(f"  {'순위':>4} {'품번':<11} 결과 / 최근")
        lines.append("  " + "-" * 80)
        for c in rorder:
            d = rinfo[c]
            lines.append(f"  {d['rank']:>3}위 {c:<11} {d['result'] or ('… ' + d['last'])}")
        pre = out / "_사전눈검사"
        if pre.is_dir():
            n = len(list(pre.glob("*.jpg")))
            ok = (pre / "통과.txt").is_file()
            lines.append(f"  사전눈검사 몽타주 {n}장 — {'통과.txt 있음' if ok else '사람 확인 대기'}: {pre}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help=r"out 폴더 (생략 시 F:\ja_reviewer_out\ja* 중 최신)")
    ap.add_argument("--src", help="소스 영상 폴더 (대기 편 표시용)")
    ap.add_argument("--watch", action="store_true", help="15초마다 갱신")
    ap.add_argument("--interval", type=int, default=15)
    a = ap.parse_args()
    out = Path(a.out) if a.out else latest_out()
    if not out or not out.is_dir():
        print("out 폴더를 못 찾음"); sys.exit(1)
    while True:
        txt = render(out, a.src)
        if a.watch:
            os.system("cls" if os.name == "nt" else "clear")
        print(txt, flush=True)
        if not a.watch:
            break
        print(f"\n  ({a.interval}초마다 갱신 — Ctrl+C 종료)")
        try:
            time.sleep(a.interval)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
