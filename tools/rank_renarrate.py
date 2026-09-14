# -*- coding: utf-8 -*-
r"""섹션2 → 섹션3 사이: 순위 호명 내레이션만 다시 쓴다(TTS·배너·번인 없음).

섹션2 초안은 pos=solo("이번 작품은~")라 순위를 모른다. rank_produce 의 내레이션 단계
(regen_narration seq/rank)만 떼어 돌려, 사람이 대본을 읽고 확정한 뒤
`rank_produce.py --reverse --keep-nar` 로 넘어가게 한다.

★영상이 빠진 순위가 있으면(ja22 KSBJ-446) 호명 순위를 1부터 다시 매긴다 — 카운트다운이
  중간에 건너뛰지 않게. 원래 순위는 로그에 같이 찍는다.

사용:
  .venv\Scripts\python.exe tools\rank_renarrate.py --src "C:\...\ja22" --out F:\ja_reviewer_out\ja22
  옵션: [--only A,B] [--keep-rank](원래 순위 번호 그대로 호명) [--redo]
  ★run_ja.py 섹션2(review) 단계의 마지막에 자동으로 붙는다. 첫 줄이 이미 "N위"면 건너뛴다.
"""
import argparse
import json
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from _ranklist import load_rank, find_rank_file, match_videos, load_details
from server import stages
from server import pipeline as P
from server.core.regen import regen_narration
from batch_clean import CliEmitter


def main():
    ap = argparse.ArgumentParser(description="순위 호명 내레이션 재작성(꼴찌 → 1위)")
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--keep-rank", action="store_true", help="빠진 순위가 있어도 원래 번호로 호명")
    ap.add_argument("--style", default="3min")
    ap.add_argument("--redo", action="store_true", help="이미 순위 호명 대본이어도 다시 쓴다")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    cfg["out_dir"] = args.out
    cfg["rank_src"] = args.src
    out = Path(args.out)
    rank_f = find_rank_file(args.src)
    pairs, missing, _extra = match_videos(load_rank(rank_f), args.src, renumber=False)
    details = load_details(rank_f)
    asc = [(r, c) for r, c, _v in pairs]                       # 1위 → 꼴찌
    shown = {c: (r if args.keep_rank else i) for i, (r, c) in enumerate(asc, 1)}
    order = list(reversed(asc))                                # 꼴찌 → 1위
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}
    n = len(order)
    if missing:
        print(f"⚠ 영상 없음: {', '.join(missing)}" +
              ("" if args.keep_rank else " — 호명 순위를 1부터 다시 매깁니다"))
    print("순서: " + " → ".join(f"{shown[c]}위 {c}" for _r, c in order))

    txt = out / "_로그" / "narration_ranked.txt"
    txt.parent.mkdir(parents=True, exist_ok=True)
    lines, results = [], []
    for i, (orig, code) in enumerate(order, 1):
        if only and code not in only:
            continue
        d = details.get(code.upper()) or {}
        rk = shown[code]
        print(f"\n{'=' * 70}\n({i}/{n}) {rk}위 {code}" + (f" (원래 {orig}위)" if rk != orig else ""),
              flush=True)
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        t0 = time.time()
        srt_f = outdir / f"{code}_내레이션.srt"
        if not args.redo and srt_f.is_file():
            first = P.srt_parse(srt_f)
            if first and first[0][2].startswith(f"{rk}위"):
                # 이미 순위 호명 대본 — 원커맨드 재실행 때 Claude를 다시 부르지 않는다
                print(f"[{code}] 이미 {rk}위 호명 대본 — 건너뜀(--redo 로 재작성)")
                lines += [f"\n{'=' * 60}\n▌{rk}위  {code}  {d.get('actress') or ''}  "
                          f"👍{d.get('likes')}/👎{d.get('dislikes')}\n{'=' * 60}"]
                lines += [f"  {k}. {t}" for k, (_a, _b, t) in enumerate(first, 1)]
                results.append(f"{rk}위 {code} — 이미 있음 {len(first)}줄")
                continue
        try:
            regen_narration(outdir, cfg["meta_api"], log=em.log, seq=(i, n), style=args.style,
                            rank={"rank": rk, "total": n, "likes": d.get("likes"),
                                  "dislikes": d.get("dislikes"), "views": d.get("views")})
            stages.save_state(outdir, code, seq=[i, n])
            srt = P.srt_parse(outdir / f"{code}_내레이션.srt")
            lines += [f"\n{'=' * 60}\n▌{rk}위  {code}  {d.get('actress') or ''}  "
                      f"👍{d.get('likes')}/👎{d.get('dislikes')}\n{'=' * 60}"]
            lines += [f"  {k}. {t}" for k, (_a, _b, t) in enumerate(srt, 1)]
            results.append(f"{rk}위 {code} — {len(srt)}줄 ({time.time() - t0:.0f}s)")
        except Exception as e:
            traceback.print_exc()
            results.append(f"{rk}위 {code} — ✘ {e}")
        txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n{'=' * 70}\n요약")
    for r in results:
        print("  " + r)
    print(f"\n대본: {txt}")
    return 1 if any("✘" in r for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
