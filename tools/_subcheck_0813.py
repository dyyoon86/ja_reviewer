# -*- coding: utf-8 -*-
"""완성본을 다시 전사해 **목소리는 나오는데 자막이 없는 구간**을 찾는다.

`{품번}_final.mp4`(최종컷, 내레이션 얹기 전 원음)를 large-v3 로 전사한 뒤
`{품번}_대사.srt`(같은 최종컷 좌표)와 겹쳐, 자막이 덮지 못한 발화를 뽑는다.

ja12 때 "일본어 나오는데 자막 없는 구간이 많다"의 재발 점검용. 그때 원인은
②AI 의 대사 선정 프롬프트가 실대사를 대량으로 버린 것이었고, ja18 은 keep 전량
번역(`_apply_dialogue.py`)으로 100% 를 맞춰 뒀다. 오늘 재컷 후에도 유지되는지 확인한다.

★ initial_prompt 는 절대 주지 않는다 — 배우 이름으로 끝나는 힌트를 주면 whisper 가
  그 이름을 받아적어 구간이 통째로 뭉갠다(2026-08-11 에 규명한 버그).

사용: .venv\\Scripts\\python.exe tools\\_subcheck_0813.py --out <out_dir> [품번...]
"""
import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import pipeline as P
from server.core.transcribe import transcribe

MIN_HOLE = 1.0     # 이보다 짧은 미커버 발화는 무시(감탄사·숨소리 수준)
COVER_OK = 0.4     # 발화 길이의 40% 이상 자막이 덮으면 커버된 것으로 본다


def overlap(a, b, c, d):
    return max(0.0, min(b, d) - max(a, c))


def ts(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def main():
    ap = argparse.ArgumentParser(description="목소리 있는데 자막 없는 구간 점검")
    ap.add_argument("codes", nargs="*")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="large-v3")
    args = ap.parse_args()

    out = Path(args.out)
    codes = [c.upper() for c in args.codes] or sorted(
        p.name for p in out.iterdir()
        if p.is_dir() and not p.name.startswith("_")
        and (p / f"{p.name}_final.mp4").is_file())

    rows = []
    for code in codes:
        d = out / code
        final = d / f"{code}_final.mp4"
        srt = d / f"{code}_대사.srt"
        subs = [(a, b) for a, b, *_ in P.srt_parse(str(srt))] if srt.is_file() else []
        print(f"\n{'=' * 70}\n{code}  (자막 {len(subs)}줄)", flush=True)
        segs = transcribe(final, model_name=args.model, log=lambda m: None)

        spoken = sum(b - a for a, b, *_ in segs)
        covered = 0.0
        holes = []
        for a, b, *rest in segs:
            txt = rest[0] if rest else ""
            ov = sum(overlap(a, b, c, dd) for c, dd in subs)
            covered += ov
            dur = b - a
            if dur >= MIN_HOLE and ov < dur * COVER_OK:
                holes.append((a, b, dur, txt))
        pct = (covered / spoken * 100) if spoken else 100.0
        print(f"  발화 {spoken:.0f}s / 자막이 덮은 {covered:.0f}s → 커버리지 {pct:.0f}%")
        for a, b, dur, txt in holes:
            print(f"   ✘ {ts(a)}~{ts(b)} ({dur:.1f}s)  {txt[:46]}")
        if not holes:
            print("   ✔ 자막 빠진 발화 없음")
        rows.append((code, spoken, pct, holes))

    print(f"\n{'=' * 70}\n요약")
    for code, spoken, pct, holes in rows:
        flag = "✔" if not holes else ("⚠" if pct >= 80 else "✘")
        gap = sum(h[2] for h in holes)
        print(f"  {flag} {code:<10} 커버리지 {pct:5.0f}%  발화 {spoken:5.0f}s  "
              f"자막없는 발화 {len(holes)}건 {gap:.0f}s")


if __name__ == "__main__":
    main()
