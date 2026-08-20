# -*- coding: utf-8 -*-
"""섹션 ③(최종 생산: ①내레이션 → ②배너 → ③TTS → ④번인)를 '13' 리스트에,
랭킹 역순(12위 → 1위)·plan.json 있는 편만 처리한다. batch_produce.py 와 동일 로직이되
폴더 글롭(알파벳·전체 17편) 대신 명시한 순서/개수로 seq(카운트다운 서수)를 정확히 매긴다.
사용: .venv\\Scripts\\python.exe tools\\_produce_sel.py [--meta URL] [--hold 5]
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
from server import pipeline as P
from server.core.regen import regen_narration
from batch_clean import CliEmitter
from batch_produce import ensure_voicebox, hide_narration

# '13' 리스트 1순위→12순위. 카운트다운(12위→1위) 순서로 나열, plan 없는 편은 런타임 스킵.
RANKED_REVERSE = [
    ("MIZD-531", "12위"),
    ("MFYD-161", "11위"),
    ("EBWH-342", "10위"),
    ("DANDYA-043", "9위"),
    ("HMN-880", "8위"),
    ("START-614", "7위"),
    ("MFYD-165", "6위"),
    ("PRED-886", "5위"),
    ("PRWF-014", "4위"),
    ("EBWH-348", "3위"),
    ("START-600", "2위"),
    ("PRED-879", "1위"),
]


def main():
    ap = argparse.ArgumentParser(description="섹션 ③ 최종 생산 (역순, plan 있는 편만)")
    ap.add_argument("--meta", help="meta_api 주소 오버라이드")
    ap.add_argument("--hold", type=float, default=None, help="배너 유지 초")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    if args.meta:
        cfg["meta_api"] = args.meta
    hold = args.hold if args.hold is not None else float(cfg.get("banner_hold", 5.0))
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT

    # plan.json 있는 편만, 원래 카운트다운 순서 유지
    todo = [(c, r) for c, r in RANKED_REVERSE
            if (stages.work_dir(cfg, c) / f"{c}_plan.json").is_file()]
    n = len(todo)
    skipped = [f"{c}({r})" for c, r in RANKED_REVERSE if (c, r) not in todo]
    print(f"대상 {n}개(카운트다운 순) / meta={cfg['meta_api']} / banner hold={hold}s / "
          f"tts={cfg.get('tts_base')} seed={cfg.get('tts_seed')}")
    if skipped:
        print(f"plan 없어 제외: {', '.join(skipped)}")

    results = []
    for i, (code, rank) in enumerate(todo, 1):
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        print(f"\n{'=' * 70}\n({i}/{n}) [{rank}] {code}", flush=True)
        t0 = time.time()
        step = "내레이션"
        try:
            regen_narration(outdir, cfg["meta_api"], log=em.log, seq=(i, n))

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
                    em.log(f"⚠ TTS 실패({e}) — voicebox 점검 후 재시도 {attempt}/2")

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
                import os
                for hidden, orig in moved:
                    os.replace(hidden, orig)

            el = (time.time() - t0) / 60
            results.append((f"[{rank}] {code}", f"✔ 완료 ({banner_note}) {el:.1f}분"))
        except Exception as e:
            results.append((f"[{rank}] {code}", f"✘ {step} 실패: {e}"))
            traceback.print_exc()

    print(f"\n{'=' * 70}\n요약")
    for code, note in results:
        print(f"  {code}: {note}")
    fails = sum(1 for _, x in results if x.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
