# -*- coding: utf-8 -*-
r"""ja20 SNOS-360 — 7초짜리 짧은 편의 내레이션을 슬롯 3개로 줄여 다시 쓴다.

기본 regen 은 영상 7.1s 에 5문장을 배치해 겹침 4건 + 0.9s 초과가 났다. 짧은 편은
'소개 → 한 마디 → 본편 유도'면 충분하다(사용자 방침: 짧아도 내레이션 넣고
"본편에서 보시죠"로 넘긴다).

사용: .venv\Scripts\python.exe tools\_renar_ja20_360.py [--slots 3]
"""
import argparse
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server import pipeline as P
from server.core.regen import regen_narration
from batch_clean import CliEmitter
from batch_produce import ensure_voicebox, hide_narration
import os

OUT = r"F:\ja_reviewer_out\ja20"
META = "http://172.30.1.40:8770"
CODE = "SNOS-360"
SEQ = (9, 12)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slots", type=int, default=3)
    args = ap.parse_args()

    cfg = _common.load_cfg()
    cfg["out_dir"] = OUT
    cfg["meta_api"] = META
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT
    hold = float(cfg.get("banner_hold", 5.0))
    outdir = stages.work_dir(cfg, CODE)
    em = CliEmitter(CODE)
    t0 = time.time()

    em.log(f"내레이션 재작성 — 슬롯 {args.slots}개, 서수 {SEQ[0]}/{SEQ[1]}")
    nar = regen_narration(outdir, cfg["meta_api"], log=em.log, seq=SEQ, slots=args.slots)
    for x in nar:
        em.log(f"  [{x['start']:5.1f}~{x['end']:5.1f}] {x['text']}")

    em.log("배너…")
    stages.stage_banner(cfg, CODE, em, hold=hold)

    em.log("TTS…")
    for attempt in (1, 2, 3):
        if not ensure_voicebox(cfg["tts_base"], em.log):
            raise RuntimeError("voicebox 재기동 실패")
        try:
            stages.stage_tts(cfg, CODE, cfg["tts_base"], cfg["tts_profile"],
                             cfg.get("tts_language", "ko"), cfg.get("tts_seed"), False, em)
            break
        except Exception as e:
            if attempt == 3:
                raise
            em.log(f"⚠ TTS 실패({e}) — 재시도 {attempt}/2")

    em.log("번인…")
    dsrt = outdir / f"{CODE}_대사.srt"
    has_dlg = dsrt.is_file() and bool(P.srt_parse(str(dsrt)))
    moved = hide_narration(outdir, CODE)
    try:
        stages.stage_burn(cfg, CODE, styles, em, parts=None if has_dlg else {"subs": False})
    finally:
        for hidden, orig in moved:
            os.replace(hidden, orig)

    print(f"\n✔ 완료 ({(time.time() - t0) / 60:.1f}분)")


if __name__ == "__main__":
    main()
