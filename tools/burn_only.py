# -*- coding: utf-8 -*-
"""지정 품번들만 번인(⑥) 재실행 — batch_produce와 동일 규칙.

내레이션 자막은 숨기고(대사+배너+워터마크만), 대사 0줄 작품은 자막 없이 배너만.
stage_burn이 전수검사 + _완성/_검수필요 수거까지 수행한다.

사용: .venv\\Scripts\\python.exe tools\\burn_only.py [--out DIR] ABF-366 SNOS-281
"""
import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server import pipeline as P
from batch_clean import CliEmitter
from batch_produce import hide_narration


def main():
    ap = argparse.ArgumentParser(description="지정 품번만 번인(⑥) 재실행")
    ap.add_argument("codes", nargs="+", help="품번 목록")
    ap.add_argument("--out", help="out_dir 오버라이드 (batch_clean/review/produce와 동일)")
    args = ap.parse_args()
    codes = [c for c in args.codes if c]
    cfg = _common.load_cfg()
    if args.out:
        cfg["out_dir"] = args.out
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT
    print(f"out_dir={cfg['out_dir']} / 대상 {len(codes)}개")
    fails = 0
    for code in codes:
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        print(f"\n{'=' * 70}\n{code}", flush=True)
        try:
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
            print(f"[{code}] ✔ 번인 완료")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[{code}] ✘ 실패: {e}")
            fails += 1
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
