# -*- coding: utf-8 -*-
r"""섹션 ③(최종 생산: ①내레이션 → ②배너 → ③TTS → ④번인)을 **랭킹 순서대로** 실행 — 범용판.

ja20까지는 배치마다 `_produce_rank_jaNN.py` 를 복사해 ITEMS·OUT 을 손으로 고쳤다.
이 도구는 소스 폴더의 랭킹 txt(`_ranklist`)를 읽으므로 복사가 필요 없다.

★섹션③은 순서가 결과물을 바꾼다: `regen_narration(seq=(i, n))` 이 "n편 중 i번째"를
  알기 때문에 연속 리뷰 인트로("세 번째 작품은 …")를 쓴다. 순서가 틀리면 서수가 어긋난다.
  제외분은 seq 를 세기 **전에** 걸러 헛번호가 생기지 않게 한다.

각 단계:
  ① regen_narration — 섹션② 내레이션을 최종 영상 길이 기준(15초당 1줄)으로 다시 쓴다.
     `--keep-nar` 면 사람이 확정한 대본을 그대로 두고 건너뛴다.
  ② stage_banner — 인포배너/프레임/워터마크. 메타 없으면 배너만 생략.
  ③ stage_tts(mux=False) — {code}_내레이션.wav 만 생성(영상엔 안 섞음, 사람이 조합).
  ④ stage_burn — 대사 자막 + 배너/워터마크만 번인(내레이션 srt/json 은 잠시 숨김).

사용:
  .venv\Scripts\python.exe tools\rank_produce.py --src "C:\...\ja21" --out "F:\ja_reviewer_out\ja21"
  옵션: [--reverse] [--only A,B] [--skip C] [--hold 5] [--seq i/n] [--keep-nar]
"""
import argparse
import os
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from _ranklist import load_rank, find_rank_file, match_videos
from server import stages
from server import pipeline as P
from server.core.regen import regen_narration
from batch_clean import CliEmitter
from batch_produce import ensure_voicebox, hide_narration


def main():
    ap = argparse.ArgumentParser(description="섹션③ 랭킹순 최종생산(범용)")
    ap.add_argument("--src", required=True, help="원본 영상 폴더(랭킹 txt가 같이 있는 곳)")
    ap.add_argument("--out", help="out_dir 오버라이드. 생략 시 config out_dir")
    ap.add_argument("--rank", help="랭킹 txt 경로. 생략 시 --src 안에서 자동 탐색")
    ap.add_argument("--reverse", action="store_true", help="랭킹 역순(꼴찌 → 1위)")
    ap.add_argument("--only", default="", help="이 품번만(쉼표 구분)")
    ap.add_argument("--skip", default="", help="제외할 품번(쉼표 구분)")
    ap.add_argument("--hold", type=float, default=None, help="배너 유지 초(기본 config banner_hold)")
    ap.add_argument("--meta", help="meta_api 주소 오버라이드")
    ap.add_argument("--seq", metavar="i/n",
                    help="서수 인트로를 강제 지정(예: 9/12). --only 로 한 편만 다시 돌릴 때 "
                         "todo 가 1개라 seq 가 (1,1) 로 잘못 잡히는 것을 막는다.")
    ap.add_argument("--keep-nar", action="store_true",
                    help="★확정한 내레이션을 그대로 쓰고 재생성을 건너뛴다. 기본은 regen 이라 "
                         "사람이 검수해 확정한 대본이 새 LLM 출력으로 덮어써진다(ja16 사고).")
    args = ap.parse_args()

    skip = {c.strip().upper() for c in args.skip.split(",") if c.strip()}
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}

    cfg = _common.load_cfg()
    if args.out:
        cfg["out_dir"] = args.out
    if args.meta:
        cfg["meta_api"] = args.meta
    hold = args.hold if args.hold is not None else float(cfg.get("banner_hold", 5.0))
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT

    rank_f = args.rank or find_rank_file(args.src)
    items = load_rank(rank_f)
    pairs, missing, extra = match_videos(items, args.src)
    order = [(r, c) for r, c, _v in pairs]
    if args.reverse:
        order = list(reversed(order))
    # ★seq 는 '실제로 굽는 편'만 세야 한다 — 제외분을 먼저 걷어내고 번호를 매긴다.
    todo = [(rk, c) for rk, c in order if c not in skip and (not only or c in only)]
    n = len(todo)
    arrow = "꼴찌 → 1위" if args.reverse else "1위 → 꼴찌"

    print(f"랭킹 {rank_f} — {len(items)}건 중 영상 있는 {len(order)}건 / 생산 대상 {n}건")
    if missing:
        print(f"  ⚠ 영상 없음: {', '.join(missing)}")
    print(f"순서={arrow} / out_dir={cfg['out_dir']} / meta={cfg['meta_api']} / "
          f"banner hold={hold}s / tts={cfg.get('tts_base')} "
          f"profile={str(cfg.get('tts_profile'))[:8]}… seed={cfg.get('tts_seed')} "
          f"후보={cfg.get('tts_candidates', 1)}개 / reframe_1080={cfg.get('reframe_1080')}")
    if skip:
        print(f"제외: {', '.join(sorted(skip))}")
    seq_force = None
    if args.seq:
        _a, _, _b = args.seq.partition("/")
        seq_force = (int(_a), int(_b))
        print(f"★서수 강제: {seq_force[0]}/{seq_force[1]}")
    else:
        print("서수 인트로 순서: " + " → ".join(f"{i}.{c}" for i, (_, c) in enumerate(todo, 1)))

    results = []
    for i, (rank, code) in enumerate(todo, 1):
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        seq = seq_force or (i, n)
        print(f"\n{'=' * 70}\n({i}/{n}) [{rank}위] {code}  서수 {seq[0]}/{seq[1]}", flush=True)
        if not (outdir / f"{code}_plan.json").is_file():
            results.append((rank, code, "✘ plan 없음 — 섹션②를 먼저"))
            print(f"[{code}] ✘ plan.json 없음")
            continue
        t0 = time.time()
        step = "내레이션"
        try:
            if args.keep_nar:
                srt = outdir / f"{code}_내레이션.srt"
                if not srt.is_file():
                    raise RuntimeError("--keep-nar인데 내레이션 srt가 없다 — 먼저 대본을 만들 것")
                em.log(f"--keep-nar: 확정 대본 그대로 사용 ({srt.name})")
            else:
                st = stages.load_state(outdir, code)
                regen_narration(outdir, cfg["meta_api"], log=em.log, seq=seq,
                                style=st.get("style") or "3min")
                stages.save_state(outdir, code, seq=list(seq))   # 나중 재실행이 서수를 안다

            step = "배너"
            b = stages.stage_banner(cfg, code, em, hold=hold)
            banner_note = "배너 생략" if b.get("skipped") else "배너 OK"

            step = "TTS"
            for attempt in (1, 2, 3):
                if not ensure_voicebox(cfg["tts_base"], em.log):
                    raise RuntimeError("voicebox 재기동 실패 — 수동 확인 필요")
                try:
                    stages.stage_tts(cfg, code, cfg["tts_base"], cfg["tts_profile"],
                                     cfg.get("tts_language", "ko"), cfg.get("tts_seed"), False, em)
                    break
                except Exception as e:
                    if attempt == 3:
                        raise
                    em.log(f"⚠ TTS 실패({e}) — voicebox 상태 점검 후 재시도 {attempt}/2")

            step = "번인"
            dsrt = outdir / f"{code}_대사.srt"
            has_dlg = dsrt.is_file() and bool(P.srt_parse(str(dsrt)))
            if not has_dlg:
                em.log("대사 자막 0줄 — 자막 없이 배너·워터마크만 번인")
            moved = hide_narration(outdir, code)
            try:
                stages.stage_burn(cfg, code, styles, em,
                                  parts=None if has_dlg else {"subs": False})
            finally:
                for hidden, orig in moved:
                    os.replace(hidden, orig)

            el = (time.time() - t0) / 60
            results.append((rank, code, f"✔ 완료 ({banner_note}) {el:.1f}분"))
        except Exception as e:
            results.append((rank, code, f"✘ {step} 실패: {e}"))
            traceback.print_exc()

    print(f"\n{'=' * 70}\n요약 (랭킹순)")
    for rank, code, note in sorted(results):
        print(f"  [{rank:2d}위] {code:11s} {note}")
    fails = sum(1 for _, _, x in results if x.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
