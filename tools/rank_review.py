# -*- coding: utf-8 -*-
r"""섹션 ②(리뷰생성: ①전사 → ②AI → ③자막)를 **랭킹 순서대로** 실행 — 배치 무관 범용판.

ja20까지는 배치마다 `_review_rank_jaNN.py` 를 복사해 ITEMS(랭킹)와 OUT 경로를 손으로
고쳤다. 이 도구는 소스 폴더의 랭킹 txt(`_ranklist`)를 읽으므로 복사가 필요 없다.

★랭킹 순서가 중요한 이유: 섹션③의 서수 인트로("n편 중 i번째")가 이 순서를 따른다.
  섹션②ㅡ자체는 순서에 무관하지만, 여기서 탈락한 편이 생기면 섹션③ 서수가 밀리므로
  같은 순서로 돌려 결과를 같은 축으로 읽을 수 있게 한다.

★클린본이 짧은 편 처리(2026-09-07 변경): 예전엔 `클린본 < target×min_keep_ratio` 면
  통째로 건너뛰었다(120s 목표 기준 클린본 60초 미만 탈락). 지금은 stages.fit_target 이
  target 을 클린본 길이로 낮춰 주므로 짧은 편도 그 길이에 맞는 한 편이 나온다.
  여기서는 fit_target 이 어차피 거부하는 것(클린본 < TARGET_FLOOR)만 미리 걸러
  전사 낭비를 막는다.

사용:
  .venv\Scripts\python.exe tools\rank_review.py --src "C:\...\ja21" --out "F:\ja_reviewer_out\ja21"
  옵션: [--reverse] [--redo] [--only A,B] [--skip C] [--target 120]
        [--target-override 품번=초] [--llm codex|claude] [--meta http://...]
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
from _ranklist import load_rank, find_rank_file, match_videos
from server import stages
from server import pipeline as P
from server.stages import NullLock, TARGET_FLOOR
from batch_clean import CliEmitter


def main():
    ap = argparse.ArgumentParser(description="섹션② 랭킹순 리뷰생성(범용)")
    ap.add_argument("--src", required=True, help="원본 영상 폴더(랭킹 txt가 같이 있는 곳)")
    ap.add_argument("--out", help="out_dir 오버라이드. 생략 시 config out_dir")
    ap.add_argument("--rank", help="랭킹 txt 경로. 생략 시 --src 안에서 자동 탐색")
    ap.add_argument("--reverse", action="store_true", help="랭킹 역순(꼴찌 → 1위)")
    ap.add_argument("--redo", action="store_true", help="plan.json 있어도 다시 실행")
    ap.add_argument("--only", default="", help="이 품번만(쉼표 구분)")
    ap.add_argument("--skip", default="", help="제외할 품번(쉼표 구분)")
    ap.add_argument("--target", type=int, help="목표 길이(초). 생략 시 config target_sec")
    ap.add_argument("--target-override", action="append", default=[], metavar="품번=초",
                    help="편별 목표 길이(여러 번 지정 가능)")
    ap.add_argument("--meta", help="meta_api 주소 오버라이드")
    ap.add_argument("--llm", help="LLM 오버라이드(codex|claude). 생략 시 config llm")
    ap.add_argument("--style", default="3min", help="문체(3min|cinema|gootabari)")
    args = ap.parse_args()

    skip = {c.strip().upper() for c in args.skip.split(",") if c.strip()}
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}
    tov = {}
    for kv in args.target_override:
        k, _, v = kv.partition("=")
        tov[k.strip().upper()] = int(v)

    cfg = _common.load_cfg()
    if args.out:
        cfg["out_dir"] = args.out
    if args.meta:
        cfg["meta_api"] = args.meta
    # 줄거리 교차 검증의 기준(사이트 소개문)을 랭킹 txt에서 읽도록 출처를 넘긴다
    cfg["rank_src"] = args.src
    mode = cfg.get("fullauto_mode", "summary")
    base_target = int(args.target or cfg.get("target_sec", 60))
    llm = args.llm or cfg.get("llm", "claude")

    rank_f = args.rank or find_rank_file(args.src)
    items = load_rank(rank_f)
    pairs, missing, extra = match_videos(items, args.src)
    order = [(r, c) for r, c, _v in pairs]
    if args.reverse:
        order = list(reversed(order))
    arrow = "꼴찌 → 1위" if args.reverse else "1위 → 꼴찌"
    print(f"랭킹 {rank_f} — {len(items)}건 중 영상 있는 {len(order)}건")
    if missing:
        print(f"  ⚠ 영상 없음: {', '.join(missing)}")
    if extra:
        print(f"  ⚠ 랭킹에 없는 영상(제외됨): {', '.join(extra)}")
    print(f"순서={arrow} / out_dir={cfg['out_dir']} / meta={cfg['meta_api']} / llm={llm} / "
          f"mode={mode} / target={base_target}s / pos=solo / style={args.style}")
    if skip:
        print(f"제외: {', '.join(sorted(skip))}")

    results = []
    for i, (rank, code) in enumerate(order, 1):
        print(f"\n{'=' * 70}\n({i}/{len(order)}) [{rank}위] {code}", flush=True)
        if code in skip or (only and code not in only):
            print(f"[{code}] 대상 아님 — 건너뜀")
            results.append((rank, code, "— 제외", None))
            continue

        em = CliEmitter(code)
        outdir = stages.work_dir(cfg, code)
        if not args.redo and (outdir / f"{code}_plan.json").is_file():
            print(f"[{code}] plan.json 이미 존재 — 건너뜀(--redo 로 재실행)")
            results.append((rank, code, "✔ 이미 완료 — 건너뜀", None))
            continue

        st = stages.load_state(outdir, code)
        video = st.get("video")
        if not video or not Path(video).is_file():
            clean = outdir / f"{code}_클린.mp4"
            video = str(clean) if clean.is_file() else None
        if not video:
            results.append((rank, code, "✘ 클린본 없음 — 섹션①을 먼저", None))
            print(f"[{code}] ✘ 클린본 없음")
            continue

        dur = P.video_duration(video) or 0.0
        if dur < TARGET_FLOOR:
            msg = (f"⚠ 클린본 {dur:.0f}초 — 한 편으로 못 씀(⓪ 클린이 과하게 잘라냄). "
                   f"본편형 의심 — 원본 재클린 또는 수동 구간 선택 필요")
            print(f"[{code}] {msg}")
            results.append((rank, code, msg, None))
            continue
        target = tov.get(code, base_target)
        eff = min(target, int(dur))
        print(f"[{code}] 클린본 {dur / 60:.1f}분 / target {target}s"
              + (f" → {eff}s(클린본에 맞춤)" if eff != target else ""), flush=True)

        t0 = time.time()
        try:
            if stages.transcribe_fresh(outdir, code, video):
                em.log("① 전사 이미 있음(같은 영상) — 재사용")
            else:
                init = None
                try:
                    m = P.fetch_meta(cfg["meta_api"], code, em.log)
                    init = P.build_initial_prompt(m) or None
                except Exception as e:
                    em.log(f"※ 메타 조회 실패({e}) → 힌트 없이 전사 진행")
                stages.stage_transcribe(cfg, code, video, cfg["whisper_model"], em,
                                        initial_prompt=init)
            # ② AI — target 은 stage_ai 안의 fit_target 이 클린본 길이로 다시 맞춘다
            stages.stage_ai(cfg, code, video, target, llm, mode, "", em,
                            gpu=NullLock(), pos="solo", style=args.style)
            # ③ 자막 — 내레이션이 컷에 비해 부족하면 stage_subs 가 자동으로 다시 짠다
            stages.stage_subs(cfg, code, em)

            el = time.time() - t0
            plan = json.loads((outdir / f"{code}_plan.json").read_text(encoding="utf-8"))
            keep = P.parse_keep(plan.get("keep", []))
            kept = sum(b - a for a, b in keep)
            nsrt = outdir / f"{code}_내레이션.srt"
            nar = len(P.srt_parse(nsrt)) if nsrt.is_file() else 0
            dia = len(plan.get("dialogue", []))
            flag = "  ★대사 0 — _apply_dialogue 필요" if dia == 0 else ""
            results.append((rank, code, f"✔ keep {len(keep)}구간 {kept:.0f}s / "
                                        f"내레이션 {nar} / 대사 {dia}{flag}", el))
        except Exception as e:
            el = time.time() - t0
            results.append((rank, code, f"✘ 실패: {e}", el))
            traceback.print_exc()

    print(f"\n{'=' * 70}\n요약 (랭킹순)")
    for rank, code, note, el in sorted(results):
        t = f" ({el / 60:.1f}분)" if el else ""
        print(f"  [{rank:2d}위] {code:11s} {note}{t}")
    fails = sum(1 for _, _, n, _ in results if n.startswith("✘"))
    done = sum(1 for _, _, n, _ in results if n.startswith("✔"))
    print(f"\n성공 {done} / 실패 {fails} / 전체 {len(results)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
