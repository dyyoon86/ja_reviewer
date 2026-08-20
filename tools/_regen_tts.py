# -*- coding: utf-8 -*-
"""①내레이션 재생성(regen) → ②TTS 만. 배너·번인은 건드리지 않는다.

batch_produce.py 는 regen→배너→TTS→번인을 한 번에 돌린다. 배너를 _banner_only.py 로
이미 구웠고 번인은 나중에 할 때, 그 가운데 두 단계만 떼어 쓰는 스크립트.

★ regen 을 건너뛰고 TTS 만 돌리면 안 된다: 섹션②가 만든 내레이션은 target_sec 기준
  예산(120s → 21~24문장)으로 뽑은 **초안**이라 실제 영상 길이에 비해 과밀하다(0.8초
  슬롯에 60자가 들어가 있는 줄이 있다). regen 은 narration_slots(실제 영상 길이) 로
  5~10줄로 다시 쓰고 갭에 배치한다 — TTS 는 그 결과를 읽어야 한다.
  이미 사람이 검수해 확정한 대본이 있으면 --keep-nar 로 regen 을 건너뛴다.

seq=(i, n) 은 모음집 연속 리뷰 흐름(먼저/다음은/마지막)을 만든다 — 제외한 품번이
순번에 끼지 않도록 codes 확정 후에 매긴다.

사용: .venv\\Scripts\\python.exe tools\\_regen_tts.py "C:\\...\\ja19소스폴더" \
        --out "C:\\Users\\yoon\\ja_reviewer_out\\ja19" --skip MIDA-703
"""
import argparse
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server.core.regen import regen_narration
from batch_clean import CliEmitter, guess_code
from batch_produce import ensure_voicebox


def main():
    ap = argparse.ArgumentParser(description="내레이션 재생성 + TTS (배너·번인 제외)")
    ap.add_argument("folder", help="원본 영상 폴더 (품번·순서 결정용)")
    ap.add_argument("--out", help="out_dir 오버라이드")
    ap.add_argument("--meta", help="meta_api 주소 오버라이드")
    ap.add_argument("--skip", default="", help="제외할 품번(쉼표 구분). 순번 계산 전에 걸러진다.")
    ap.add_argument("--only", default="", help="이 품번만(쉼표 구분). ★순번(seq)은 전체 기준 유지.")
    ap.add_argument("--keep-nar", action="store_true",
                    help="확정 대본을 그대로 쓰고 regen 을 건너뛴다(TTS만).")
    ap.add_argument("--style", default="3min", help="내레이션 문체: 3min | cinema | gootabari")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    if args.out:
        cfg["out_dir"] = args.out
    if args.meta:
        cfg["meta_api"] = args.meta
    skip = {c.strip().upper() for c in args.skip.split(",") if c.strip()}
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}

    codes = sorted({guess_code(v.name) for v in Path(args.folder).glob("*.mp4")} - {""} - skip)
    n = len(codes)
    print(f"대상 {n}개(순서 고정) / out_dir={cfg['out_dir']} / meta={cfg['meta_api']} / "
          f"문체={args.style} / tts={cfg.get('tts_base')} seed={cfg.get('tts_seed')} "
          f"후보={cfg.get('tts_candidates', 1)}개")
    if only:
        print(f"  ※ 실제 실행은 {sorted(only)} 만 (seq 는 전체 {n}개 기준)")

    results = []
    for i, code in enumerate(codes, 1):
        if only and code not in only:
            continue
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        print(f"\n{'=' * 70}\n({i}/{n}) {code}", flush=True)
        if not (outdir / f"{code}_plan.json").is_file():
            results.append((code, "✘ plan 없음 — 섹션② 먼저", 0))
            continue
        t0 = time.time()
        step = "내레이션"
        try:
            if args.keep_nar:
                srt = outdir / f"{code}_내레이션.srt"
                if not srt.is_file():
                    raise RuntimeError("--keep-nar인데 내레이션 srt가 없다")
                em.log(f"--keep-nar: 확정 대본 그대로 사용 ({srt.name})")
            else:
                regen_narration(outdir, cfg["meta_api"], log=em.log, seq=(i, n),
                                style=args.style)

            step = "TTS"
            # seed 고정 — 작품 간 목소리 톤 편차 제거. voicebox 가 산발적으로 죽으므로
            # 시도 전 생존 확인 + 실패 시 재기동 후 재시도(batch_produce 와 동일).
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
                    em.log(f"⚠ TTS 실패({e}) — voicebox 점검 후 재시도 {attempt}/2")

            wav = outdir / f"{code}_내레이션.wav"
            sz = wav.stat().st_size / 1e6 if wav.is_file() else 0
            results.append((code, f"✔ 완료 (wav {sz:.1f}MB)", time.time() - t0))
        except Exception as e:
            results.append((code, f"✘ {step} 실패: {e}", time.time() - t0))
            traceback.print_exc()

    print(f"\n{'=' * 70}\n요약")
    for code, note, el in results:
        print(f"  {code}: {note} ({el / 60:.1f}분)")
    fails = sum(1 for _, x, _ in results if x.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
