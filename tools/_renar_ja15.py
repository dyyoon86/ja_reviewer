# -*- coding: utf-8 -*-
"""ja15 5편 내레이션 재배치 재생산 — 내레이션이 대사를 앞지르던 문제 수정 반영.

배경(2026-07-30) — regen_narration의 슬롯 배치가 keep 구간 머리만 보고 내레이션을
놓아서, 대사가 말하는 중이거나 대사가 시작되기 직전에 내레이션이 튀어나왔다.
5편 실측: 선행 19건 · 겹침 58건 · 촘촘(간격<0.5s) 55건.

수정 후에는 '대사 없는 틈'에만 슬롯을 놓으므로 대사가 빽빽한 작품은 내레이션 개수가
줄어든다(그게 정상 — 자리가 없는데 밀어넣던 것이 원인이었다).

각 편: regen_narration → TTS(화자 선별) → 번인(대사+배너만, 내레이션 자막은 숨김).
keep·대사·컷은 건드리지 않는다.

사용: .venv\\Scripts\\python.exe tools\\_renar_ja15.py [품번...]
"""
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa
from server import stages, pipeline as P
from server.core.regen import regen_narration
from batch_clean import CliEmitter
from batch_produce import ensure_voicebox, hide_narration

OUT_DIR = r"F:\ja_reviewer_out\ja15"
ORDER = ["SNOS-293", "SNOS-301", "SNOS-318", "SNOS-326", "SNOS-327"]


def main():
    want = [c for c in ORDER if c in set(sys.argv[1:])] or ORDER
    cfg = _common.load_cfg()
    cfg["out_dir"] = OUT_DIR
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT
    n = len(ORDER)
    print(f"대상 {len(want)}개 / out_dir={OUT_DIR} / seed={cfg.get('tts_seed')} "
          f"후보={cfg.get('tts_candidates', 1)}개")

    results = []
    for code in want:
        i = ORDER.index(code) + 1          # 서수 인트로는 모음집 순번 그대로
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        print(f"\n{'=' * 70}\n({i}/{n}) {code}", flush=True)
        t0 = time.time()
        try:
            nar = regen_narration(outdir, cfg["meta_api"], log=em.log, seq=(i, n))

            for attempt in (1, 2, 3):
                if not ensure_voicebox(cfg["tts_base"], em.log):
                    raise RuntimeError("voicebox 재기동 실패")
                try:
                    stages.stage_tts(cfg, code, cfg["tts_base"], cfg["tts_profile"],
                                     cfg.get("tts_language", "ko"),
                                     cfg.get("tts_seed"), False, em)
                    break
                except Exception as e:
                    if attempt == 3:
                        raise
                    em.log(f"⚠ TTS 재시도 {attempt}/2 ({e})")

            dsrt = outdir / f"{code}_대사.srt"
            has_dlg = dsrt.is_file() and bool(P.srt_parse(str(dsrt)))
            moved = hide_narration(outdir, code)
            try:
                stages.stage_burn(cfg, code, styles, em,
                                  parts=None if has_dlg else {"subs": False})
            finally:
                import os
                for hidden, orig in moved:
                    os.replace(hidden, orig)
            results.append((code, f"✔ 내레이션 {len(nar)}줄 ({(time.time()-t0)/60:.1f}분)"))
        except Exception as e:
            import traceback
            traceback.print_exc()
            results.append((code, f"✘ 실패: {e}"))

    print(f"\n{'=' * 70}\n요약")
    for c, note in results:
        print(f"  {c}: {note}")
    fails = sum(1 for _, x in results if x.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
