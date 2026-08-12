# -*- coding: utf-8 -*-
"""검수 에이전트가 낸 대안 문장을 내레이션에 **그 줄만** 반영한다.

`batch_narreview.py --fix` 는 결함 편의 내레이션을 통째로 다시 쓴다. 그러면 멀쩡하던
줄까지 바뀌고 새 오류가 생길 수 있다(그래서 다시 검수해야 한다). 결함이 한두 줄뿐일
때는 **지적된 줄만 갈아끼우는 쪽**이 안전하고 빠르다 — 대안 문장은 이미 검수자가
같은 근거를 보고 써준 것이다.

바꾸는 것: plan.json 의 narration[i].text · {품번}_내레이션.json · {품번}_내레이션.srt
시각(start/end)은 건드리지 않는다 → **TTS만 다시 뽑으면 된다**(재컷·재타이밍 불필요).

n=0(개괄없음처럼 특정 줄이 아닌 지적)은 어느 줄을 고칠지 정해야 한다:
  · 대안이 'N 번째 작품은…' 으로 시작하면 → 1번 줄(소개 슬롯이 개괄을 겸하는 편)
  · 아니면 → 3번 줄(개괄 슬롯). 3번이 없으면 마지막 도입 줄.

사용: .venv\\Scripts\\python.exe tools\\_apply_narfix.py --out <out_dir> [품번...] [--dry]
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401

LEN_WARN = 1.4          # 대안이 원문보다 이만큼 길면 경고(음성이 슬롯을 넘길 수 있다)


def srt_ts(t):
    h = int(t // 3600); m = int(t % 3600 // 60); s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        s += 1; ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def target_index(issue, n_lines):
    """이슈가 가리키는 줄 인덱스(0-based). 못 정하면 None."""
    n = issue.get("n")
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if n > 0:
        return n - 1 if n <= n_lines else None
    fix = (issue.get("fix") or "").strip()
    if re.match(r"^\S*\s*번째 작품은", fix):       # 소개 슬롯이 개괄을 겸하는 형태
        return 0
    return 2 if n_lines >= 3 else 0                # 기본 개괄 슬롯


def apply_one(folder: Path, code: str, dry=False):
    rev = folder / f"{code}_내레이션검수.json"
    njson = folder / f"{code}_내레이션.json"
    plan_f = folder / f"{code}_plan.json"
    if not rev.is_file():
        return f"– 검수 결과 없음"
    issues = (json.loads(rev.read_text(encoding="utf-8")).get("issues") or [])
    issues = [i for i in issues if (i.get("fix") or "").strip()]
    if not issues:
        return "✔ 고칠 것 없음"

    nar = json.loads(njson.read_text(encoding="utf-8"))
    nar.sort(key=lambda d: float(d.get("start", 0)))
    done, skipped = [], []
    for it in issues:
        i = target_index(it, len(nar))
        if i is None:
            skipped.append(f"{it.get('type')}(줄 특정 실패)")
            continue
        old = nar[i].get("text", "")
        new = it["fix"].strip()
        if new == old:
            continue
        if len(new) > len(old) * LEN_WARN and len(new) > len(old) + 6:
            print(f"    ⚠ {i+1}번 대안이 원문보다 김({len(old)}→{len(new)}자) — "
                  f"음성이 슬롯을 넘길 수 있다")
        print(f"    {i+1}번: {old}")
        print(f"        → {new}   [{it.get('type')}]")
        nar[i]["text"] = new
        done.append(i + 1)
    if not done:
        return "✔ 반영할 것 없음" + (f" (건너뜀: {', '.join(skipped)})" if skipped else "")
    if dry:
        return f"(dry) {len(done)}줄 반영 예정 — {done}"

    njson.write_text(json.dumps(nar, ensure_ascii=False, indent=2), encoding="utf-8")
    # plan.narration 도 같이 맞춘다 — 여기가 원본이라 안 고치면 다음 stage_subs에서 되돌아간다
    if plan_f.is_file():
        plan = json.loads(plan_f.read_text(encoding="utf-8"))
        pn = plan.get("narration") or []
        pn.sort(key=lambda d: float(d.get("start", 0)))
        for i in done:
            if i - 1 < len(pn):
                pn[i - 1]["text"] = nar[i - 1]["text"]
        plan["narration"] = pn
        plan_f.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    # srt 는 시각을 그대로 두고 본문만 갈아끼운다(재타이밍 불필요)
    srt = folder / f"{code}_내레이션.srt"
    if srt.is_file():
        out = []
        for k, d in enumerate(nar, 1):
            out += [str(k), f'{srt_ts(float(d["start"]))} --> {srt_ts(float(d["end"]))}',
                    d.get("text", ""), ""]
        srt.write_text("\n".join(out), encoding="utf-8-sig")
    return f"✔ {len(done)}줄 반영 {done}" + (f" (건너뜀: {', '.join(skipped)})" if skipped else "")


def main():
    ap = argparse.ArgumentParser(description="검수 대안 문장 반영(줄 단위)")
    ap.add_argument("codes", nargs="*")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry", action="store_true", help="바꾸지 않고 무엇이 바뀔지만 출력")
    args = ap.parse_args()

    out = Path(args.out)
    codes = [c.upper() for c in args.codes] or sorted(
        p.name for p in out.iterdir()
        if p.is_dir() and (p / f"{p.name}_내레이션검수.json").is_file())
    rows = []
    for code in codes:
        print(f"\n[{code}]")
        try:
            rows.append((code, apply_one(out / code, code, args.dry)))
        except Exception as e:
            import traceback; traceback.print_exc()
            rows.append((code, f"✘ {e}"))
    print("\n요약")
    for c, n in rows:
        print(f"  {c}: {n}")
    print("\n※ 시각은 안 바뀌었다 — TTS만 다시 뽑고(batch_nar_tts) 재번인하면 된다.")


if __name__ == "__main__":
    main()
