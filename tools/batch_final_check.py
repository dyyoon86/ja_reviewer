# -*- coding: utf-8 -*-
"""완성 직전 전수 검사 — 품번별 final.mp4 와 plan 대사를 한 번에 점검.

① 대사: plan.json dialogue에서 노골적 성적 표현 검색(키워드 리스트)
② 화면: {code}_final.mp4 를 NudeNet 정밀 스캔(기본 0.5s 간격) — 노출 구간 보고

⑥ 굽기의 완성본 전수검사(nsfw_final_check)와 달리 굽기 전에 미리 확인하는 용도.
문제가 나오면 파일을 옮기지 않고 보고만 한다(판단은 사람).

사용: .venv\\Scripts\\python.exe tools\\batch_final_check.py "C:\\...\\영상폴더" [--step 0.5]
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import pipeline as P
from server.core import nsfw
from batch_clean import guess_code

BAD_WORDS = ["자지", "보지", "섹스", "삽입", "사정", "발기", "정액", "질내", "박아",
             "빨아", "빨고", "핥", "꽂", "싸버", "싸도", "쌌", "후장", "유두",
             "가슴이다", "주물", "비벼", "흥분", "서 버렸", "섰어", "젖었", "젖어",
             "느껴져", "기모치", "이쿠", "싸는"]


def check_dialogue(plan):
    hits = []
    for d in plan.get("dialogue", []):
        t = d.get("ko") or d.get("text") or ""
        for w in BAD_WORDS:
            if w in t:
                hits.append((d.get("start", 0), t))
                break
    return hits


def main():
    ap = argparse.ArgumentParser(description="final 전수 검사(대사+노출)")
    ap.add_argument("folder", help="원본 영상 폴더 (품번 결정용)")
    ap.add_argument("--step", type=float, default=0.5, help="NN 스캔 간격(초)")
    ap.add_argument("--threshold", type=float, default=0.22, help="NN 노출 임계값")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    codes = sorted({guess_code(v.name) for v in Path(args.folder).glob("*.mp4")} - {""})
    print(f"대상 {len(codes)}개 / NN step={args.step}s threshold={args.threshold}")

    results = []
    for i, code in enumerate(codes, 1):
        outdir = Path(cfg["out_dir"]) / code
        final = outdir / f"{code}_final.mp4"
        plan_f = outdir / f"{code}_plan.json"
        print(f"\n{'=' * 70}\n({i}/{len(codes)}) {code}", flush=True)
        if not final.is_file() or not plan_f.is_file():
            results.append((code, "✘ final/plan 없음"))
            continue
        try:
            plan = json.loads(plan_f.read_text(encoding="utf-8"))
            d_hits = check_dialogue(plan)
            dur = P.video_duration(final) or 0.0
            bad = nsfw.build_map(str(final), step=args.step, threshold=args.threshold,
                                 pad=0.5, merge_gap=3.0, cache=None,
                                 log=lambda m: print(f"[{code}] {m}", flush=True),
                                 duration=dur)
            note = []
            if d_hits:
                note.append(f"대사 의심 {len(d_hits)}건: "
                            + "; ".join(f"{s:.0f}s「{t}」" for s, t in d_hits[:3]))
            if bad:
                note.append("노출 " + ", ".join(f"{a:.1f}~{b:.1f}s" for a, b in bad))
            results.append((code, ("⚠ " + " / ".join(note)) if note
                            else f"✔ 클린 ({dur:.0f}s)"))
        except Exception as e:
            results.append((code, f"✘ 검사 실패: {e}"))

    print(f"\n{'=' * 70}\n검사 요약")
    warns = 0
    for code, note in results:
        print(f"  {code}: {note}")
        if not note.startswith("✔"):
            warns += 1
    print(f"\n클린 {len(results) - warns}/{len(results)}" + (f", 확인 필요 {warns}" if warns else " — 전부 통과"))
    sys.exit(1 if warns else 0)


if __name__ == "__main__":
    main()
