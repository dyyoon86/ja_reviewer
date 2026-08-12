# -*- coding: utf-8 -*-
"""경량 패스 — 품번 순서대로 내레이션 재생성(연속 리뷰+아웃트로 로테이션) + TTS만 다시.

배너·번인(final_subbed)은 내레이션과 무관하므로 건드리지 않는다.
batch_produce와 동일 프리셋(seq, tts_seed, voicebox 자가복구)을 그대로 쓴다.

사용: .venv\\Scripts\\python.exe tools\\batch_nar_tts.py "C:\\...\\영상폴더" [--meta URL]
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
    ap = argparse.ArgumentParser(description="내레이션+TTS 경량 재생성")
    ap.add_argument("folder")
    ap.add_argument("--out", help="out_dir 오버라이드. 생략 시 studio_config.json의 out_dir "
                                  "— 다른 모음집을 덮어쓰지 않으려면 반드시 지정할 것.")
    ap.add_argument("--meta", help="meta_api 주소 오버라이드")
    ap.add_argument("--skip", default="", help="제외할 품번(쉼표 구분). ★서수 인트로(seq)가 "
                                              "제외분을 순번으로 세지 않도록 codes 확정 전에 거른다.")
    ap.add_argument("--no-tts", action="store_true",
                    help="내레이션 대본만 다시 쓰고 TTS는 건너뛴다(문안 확정 단계용).")
    ap.add_argument("--only", default="", help="이 품번들만 처리(쉼표 구분). seq 번호는 "
                                              "--skip 적용 후 전체 목록 기준이라 그대로 유지된다.")
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
    print(f"대상 {n}개 / out_dir={cfg['out_dir']} / TTS={'생략' if args.no_tts else 'ON'}"
          + (f" / only={sorted(only)}" if only else ""))

    results = []
    for i, code in enumerate(codes, 1):
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        print(f"\n{'=' * 70}\n({i}/{n}) {code}", flush=True)
        if only and code not in only:
            print(f"[{code}] --only 대상 아님 — 건너뜀(seq {i} 자리는 유지)")
            continue
        if not (outdir / f"{code}_plan.json").is_file():
            results.append((code, "✘ plan 없음"))
            continue
        t0 = time.time()
        try:
            regen_narration(outdir, cfg["meta_api"], log=em.log, seq=(i, n))
            if args.no_tts:
                results.append((code, f"✔ 내레이션만 ({(time.time() - t0) / 60:.1f}분)"))
                continue
            for attempt in (1, 2, 3):
                if not ensure_voicebox(cfg["tts_base"], em.log):
                    raise RuntimeError("voicebox 재기동 실패")
                try:
                    stages.stage_tts(cfg, code, cfg["tts_base"], cfg["tts_profile"],
                                     cfg.get("tts_language", "ko"), cfg.get("tts_seed"), False, em)
                    break
                except Exception as e:
                    if attempt == 3:
                        raise
                    em.log(f"⚠ TTS 실패({e}) — 재시도 {attempt}/2")
            results.append((code, f"✔ 완료 ({(time.time() - t0) / 60:.1f}분)"))
        except Exception as e:
            results.append((code, f"✘ 실패: {e}"))
            traceback.print_exc()

    print(f"\n{'=' * 70}\n요약")
    for code, note in results:
        print(f"  {code}: {note}")
    fails = sum(1 for _, x in results if x.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
