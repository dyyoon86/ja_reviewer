# -*- coding: utf-8 -*-
r"""섹션 ③(최종 생산: ①내레이션 → ②배너 → ③TTS → ④번인)을 ja20 에 **랭킹 순서대로** 실행.

batch_produce.py 와 같은 스테이지·순서를 쓰되, `codes = sorted(glob)` 가나다순 대신
`ja20_1~16.txt` 게재 순서(= 좋아요 순위)로 돈다. `_clean_rank_ja20.py`·`_review_rank_ja20.py` 와 짝.

★섹션③은 순서가 결과물을 바꾸는 단계다: `regen_narration(seq=(i, n))` 이 "n편 중 i번째"를
  알기 때문에 1→n 연속 리뷰 흐름(연결 인트로)을 쓴다. 가나다순으로 돌리면 서수 인트로가
  랭킹과 어긋난다. 제외분(SNOS-360)은 codes 확정 전에 걸러 seq 가 헛번호를 세지 않게 한다.

각 단계는 batch_produce 와 동일:
  ① regen_narration — 섹션② 내레이션은 **초안**이다(target 120s 기준 21~24문장으로 과밀).
     여기서 실제 영상 길이 기준(15초당 1줄·5~10개)으로 통째로 다시 쓴다.
  ② stage_banner — 인포배너/프레임/워터마크. 메타 없으면 배너만 생략하고 진행.
  ③ stage_tts(mux=False) — {code}_내레이션.wav 만 생성(영상에 안 섞음, 사람이 조합).
  ④ stage_burn — **대사 자막 + 배너/워터마크만** 번인(내레이션 srt/json 은 잠시 숨김).
     전수검사 + 자체검사 + _완성/_검수필요 수거까지 stage_burn 이 해준다.

사용: .venv\Scripts\python.exe tools\_produce_rank_ja20.py [--reverse] [--keep-nar]
                                   [--only A,B] [--skip C] [--hold 5]
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
from server import stages
from server import pipeline as P
from server.core.regen import regen_narration
from batch_clean import CliEmitter
from batch_produce import ensure_voicebox, hide_narration

OUT = r"F:\ja_reviewer_out\ja20"
META = "http://172.30.1.40:8770"

# (원본랭킹, 품번) — ja20_1~16.txt 게재 순서. 영상 없는 4건 제외.
ITEMS = [
    (1, "SNOS-373"), (2, "SNOS-371"), (3, "START-627"), (4, "FNS-248"),
    (5, "SNOS-365"), (6, "START-622"), (7, "JUR-819"), (8, "JUR-823"),
    (13, "SNOS-360"), (14, "SNOS-409"), (15, "JUR-820"), (16, "SDMM-238"),
]

# 섹션①에서 클린본 7.4초(본편형)로 확정돼 섹션②를 못 넘긴 편.
DEFAULT_SKIP = {"SNOS-360"}


def main():
    ap = argparse.ArgumentParser(description="ja20 섹션③ 랭킹순 최종생산")
    ap.add_argument("--reverse", action="store_true", help="랭킹 역순(꼴찌 → 1위)")
    ap.add_argument("--only", default="", help="이 품번만(쉼표 구분)")
    ap.add_argument("--skip", default="", help="추가로 제외할 품번(쉼표 구분)")
    ap.add_argument("--no-default-skip", action="store_true", help="DEFAULT_SKIP 무시")
    ap.add_argument("--hold", type=float, default=None, help="배너 유지 초(기본 config banner_hold)")
    ap.add_argument("--meta", default=META, help="meta_api 주소")
    ap.add_argument("--seq", metavar="i/n",
                    help="서수 인트로를 강제 지정(예: 9/12). --only 로 한 편만 다시 돌릴 때 "
                         "todo 가 1개라 seq 가 (1,1) 로 잘못 잡히는 것을 막는다. "
                         "★내레이션 첫 줄에 서수가 텍스트로 박히므로, 전체 편수가 바뀌면 "
                         "뒤 편들의 첫 줄도 같이 손봐야 한다.")
    ap.add_argument("--keep-nar", action="store_true",
                    help="★확정한 내레이션을 그대로 쓰고 재생성을 건너뛴다. 기본은 regen 이라 "
                         "사람이 검수해 확정한 대본이 새 LLM 출력으로 덮어써진다(ja16 사고). "
                         "대본 확정 후 배너·TTS·번인만 돌릴 때 반드시 붙일 것.")
    args = ap.parse_args()

    skip = set(() if args.no_default_skip else DEFAULT_SKIP)
    skip |= {c.strip().upper() for c in args.skip.split(",") if c.strip()}
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}

    cfg = _common.load_cfg()
    cfg["out_dir"] = OUT
    cfg["meta_api"] = args.meta
    hold = args.hold if args.hold is not None else float(cfg.get("banner_hold", 5.0))
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT

    # ★seq 는 '실제로 굽는 편'만 세야 한다 — 제외분을 미리 걷어내고 번호를 매긴다.
    order = list(reversed(ITEMS)) if args.reverse else list(ITEMS)
    todo = [(rk, c) for rk, c in order if c not in skip and (not only or c in only)]
    n = len(todo)
    arrow = "꼴찌 → 1위" if args.reverse else "1위 → 꼴찌"

    print(f"대상 {n}개 / 순서={arrow} / out_dir={cfg['out_dir']} / meta={cfg['meta_api']} / "
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
        print(f"\n{'=' * 70}\n({i}/{n}) [{rank}위] {code}", flush=True)
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
                regen_narration(outdir, cfg["meta_api"], log=em.log, seq=seq)

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
        print(f"  [{rank:2d}위] {code:10s} {note}")
    fails = sum(1 for _, _, x in results if x.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
