# -*- coding: utf-8 -*-
"""섹션 ②(리뷰생성) 완주 후 전수 점검 — 대사 유실 / keep 재료 / 내레이션 시각.

메모리에 남은 실제 사고 3종을 그대로 검사한다.
  ① 대사 0줄 납품 — 2-pass는 `prompt_dialogue_fix` 1회에 대사 전체가 걸려 있고,
    실패해도 경고만 찍고 진행한다(ja16 12편 중 3편이 0줄로 나갔다).
  ② keep 재료 부족 — keep 합계가 target×min_keep_ratio 근처면 본편형 의심.
  ③ 내레이션 srt 끝시각 > 영상 길이 — `_내레이션.json`(클린본 좌표)으로 srt를 쓰면
    최종컷 좌표가 통째로 밀려 내레이션이 화면에 하나도 안 나온 채 납품된다.
  ④ 내레이션 **json** 끝시각 > 영상 길이 — ③의 거울상(ja19 사고 2026-08-21).
    srt 는 최종컷 좌표로 멀쩡한데 json 만 클린본 좌표로 남은 경우다. 굽기(burn_subs)는
    유형(style) 때문에 srt 보다 json 을 우선하므로, ③만 보면 통과시켜 놓고
    자막이 영상 밖에 박힌 납품본이 나간다(ja19 3편이 이렇게 나갔다).

사용: .venv\\Scripts\\python.exe tools\\_sec2_check.py "C:\\Users\\yoon\\ja_reviewer_out\\ja19"
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def dur(p: Path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(p)], capture_output=True, text=True, timeout=60)
        return float((out.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def srt_end(p: Path):
    """srt 마지막 큐의 끝 시각(초). 없으면 0."""
    if not p.is_file():
        return 0.0
    times = re.findall(r"-->\s*(\d+):(\d+):(\d+)[,.](\d+)", p.read_text(encoding="utf-8", errors="ignore"))
    if not times:
        return 0.0
    h, m, s, ms = times[-1]
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def json_end(p: Path):
    """내레이션 json 마지막 항목의 끝 시각(초). 없으면 0. (④ 검사용)"""
    if not p.is_file():
        return 0.0
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        return max(float(d["end"]) for d in data) if data else 0.0
    except Exception:
        return 0.0


def srt_count(p: Path):
    if not p.is_file():
        return 0
    return len(re.findall(r"-->", p.read_text(encoding="utf-8", errors="ignore")))


def keep_ranges(plan):
    rs = []
    for k in plan.get("keep", []):
        try:
            a, b = (k[0], k[1]) if isinstance(k, (list, tuple)) else (k["start"], k["end"])
            rs.append((float(a), float(b)))
        except Exception:
            continue
    return rs


def in_keep(line, rs, pad=0.05):
    try:
        s = float(line["start"])
    except Exception:
        return False
    return any(a - pad <= s <= b + pad for a, b in rs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--target", type=int, default=120, help="목표 길이(초) — 재료부족 경고 기준")
    ap.add_argument("--ratio", type=float, default=0.5, help="min_keep_ratio")
    ap.add_argument("--override", action="append", default=[],
                    help="편별 target 오버라이드(예: --override PRWF-015=60). 클린본이 짧아 "
                         "--target 낮춰 따로 돌린 편은 그 값으로 재료부족을 판정해야 한다.")
    args = ap.parse_args()

    overrides = {}
    for o in args.override:
        c, _, t = o.partition("=")
        if t.strip().isdigit():
            overrides[c.strip().upper()] = int(t)

    root = Path(args.out_dir)
    rows, problems = [], []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        code = d.name
        planp = d / f"{code}_plan.json"
        if not planp.is_file():
            rows.append((code, "—", "plan 없음(②AI 미완/중단)", "", "", ""))
            problems.append(f"{code}: plan.json 없음 — ②AI가 중단됐다(재료부족이면 --target 낮춰 재실행)")
            continue
        plan = json.loads(planp.read_text(encoding="utf-8"))
        rs = keep_ranges(plan)
        nseg, ksum = len(rs), sum(b - a for a, b in rs)
        dlg = plan.get("dialogue", []) or []
        # ★ keep 밖 대사는 stage_subs 가 정상적으로 버린다 — srt 와 비교할 대상은 keep 안쪽뿐.
        dlg_in = [d for d in dlg if in_keep(d, rs)]
        dlg_out = len(dlg) - len(dlg_in)
        nar = plan.get("narration", []) or []
        tgt = overrides.get(code, args.target)
        dsrt, nsrt = srt_count(d / f"{code}_대사.srt"), srt_count(d / f"{code}_내레이션.srt")
        vid = d / f"{code}_final.mp4"
        vdur = dur(vid) if vid.is_file() else 0.0
        nend = srt_end(d / f"{code}_내레이션.srt")
        njend = json_end(d / f"{code}_내레이션.json")   # ④ 굽기가 실제로 읽는 쪽

        flags = []
        if not dlg_in:
            flags.append("★대사0")
            problems.append(f"{code}: keep 안쪽 대사 0줄 — `_apply_dialogue.py` 로 전량 재번역 필요")
        elif len(dlg_in) < 5:
            flags.append(f"대사적음({len(dlg_in)})")
            problems.append(f"{code}: 대사 {len(dlg_in)}줄뿐 — keep이 짧아서인지 번역 실패인지 확인")
        # stage_subs 는 긴 대사를 ≤25자 단위로 쪼개 여러 큐로 굽는다 → srt 큐 ≥ 줄 수 가 정상.
        # 반대로 srt 가 더 적으면 자막이 실제로 유실된 것.
        if dsrt < len(dlg_in):
            flags.append(f"★srt유실({dsrt}<{len(dlg_in)})")
            problems.append(f"{code}: _대사.srt {dsrt}큐 < keep 안쪽 대사 {len(dlg_in)}줄 — stage_subs 재실행")
        if not nar:
            flags.append("★내레이션0")
            problems.append(f"{code}: 내레이션 0줄")
        if ksum < tgt * args.ratio:
            flags.append(f"★keep부족({ksum:.0f}s)")
            problems.append(f"{code}: keep {ksum:.0f}s < 최소 {tgt*args.ratio:.0f}s(target {tgt}s) — 본편형 의심")
        if vdur and abs(vdur - ksum) > 5:
            flags.append(f"영상≠keep({vdur:.0f}s)")
            problems.append(f"{code}: final.mp4 {vdur:.0f}s vs keep 합계 {ksum:.0f}s — 컷 불일치")
        if vdur and nend > vdur + 0.5:
            flags.append(f"★내레이션밖({nend:.0f}s>{vdur:.0f}s)")
            problems.append(f"{code}: 내레이션 srt 끝 {nend:.1f}s > 영상 {vdur:.1f}s "
                            f"— 클린본 좌표로 srt를 쓴 사고. stage_subs 재실행")
        # ④ 굽기는 json 을 우선한다 — srt 가 멀쩡해도 json 이 밖이면 자막이 안 뜬다.
        if vdur and njend > vdur + 0.5:
            flags.append(f"★내레이션json밖({njend:.0f}s>{vdur:.0f}s)")
            problems.append(f"{code}: 내레이션 json 끝 {njend:.1f}s > 영상 {vdur:.1f}s "
                            f"— 굽기가 읽는 쪽이 클린본 좌표다(srt 는 {nend:.1f}s 로 정상). "
                            f"json 시각을 srt 로 맞춘 뒤 재굽기")
        rows.append((code, f"{nseg}구간 {ksum:.0f}s",
                     f"대사 {len(dlg_in)}/{dsrt}" + (f"(+{dlg_out}밖)" if dlg_out else ""),
                     f"내레이션 {len(nar)}/{nsrt}", f"{vdur:.0f}s", " ".join(flags) or "OK"))

    w = max((len(r[0]) for r in rows), default=10)
    print(f"{'품번'.ljust(w)}  {'keep':<14} {'대사(plan/srt)':<16} {'내레이션(plan/srt)':<20} "
          f"{'영상':<7} 판정")
    print("─" * (w + 70))
    for r in rows:
        print(f"{r[0].ljust(w)}  {r[1]:<14} {r[2]:<16} {r[3]:<20} {r[4]:<7} {r[5]}")

    ok = sum(1 for r in rows if r[5] == "OK")
    print(f"\n정상 {ok}/{len(rows)}")
    if problems:
        print(f"\n조치 필요 {len(problems)}건")
        for p in problems:
            print(f"  · {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
