# -*- coding: utf-8 -*-
"""지목 4편에서 '노출 세그먼트'를 plan.keep에서 빼고 안전 세그먼트만 남겨 재컷→재생산.
최대 안전 기준(대화·인터뷰 착의만 유지). 세그먼트 인덱스(1-based)로 유지 대상 지정.
사용: .venv\\Scripts\\python.exe tools\\_recut_safe.py
"""
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa
from server import stages, pipeline as P
from server.stages import NullLock
from server.core.regen import regen_narration
from batch_clean import CliEmitter
from batch_produce import ensure_voicebox, hide_narration

# 유지할 안전 세그먼트(1-based) + 카운트다운 위치(seq=(i, 11))
JOBS = [
    ("MIZD-531",  [5, 6, 7],    (1, 11)),
    ("EBWH-342",  [1, 2, 3, 4], (2, 11)),
    ("START-614", [1, 2, 3],    (5, 11)),
    ("EBWH-348",  [1, 2],       (9, 11)),
]


def in_keep(t, keep):
    return any(a <= t <= b for a, b in keep)


def main():
    cfg = _common.load_cfg()
    hold = float(cfg.get("banner_hold", 5.0))
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT
    results = []

    for code, seg_idx, seq in JOBS:
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        print(f"\n{'=' * 70}\n{code} — 안전 세그먼트 {seg_idx} 유지", flush=True)
        t0 = time.time()
        try:
            planf = outdir / f"{code}_plan.json"
            plan = json.loads(planf.read_text(encoding="utf-8"))
            keep_all = plan.get("keep", [])
            new_keep = [keep_all[i - 1] for i in seg_idx]
            # 백업(1회)
            bak = outdir / f"{code}_plan.json.precut"
            if not bak.is_file():
                bak.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
            # keep 교체 + keep 밖 대사/내레이션 제거(재타이밍은 stage_subs가 keep 매핑)
            plan["keep"] = new_keep
            plan["dialogue"] = [d for d in plan.get("dialogue", [])
                                if in_keep(d.get("start", 0), new_keep)]
            plan["narration"] = [n for n in plan.get("narration", [])
                                 if in_keep(n.get("start", 0), new_keep)]
            if not plan["narration"]:
                # 내레이션이 다 빠지면 regen이 못 도니 최소 seed 유지(summary 기반 1줄)
                plan["narration"] = [{"start": new_keep[0][0], "end": new_keep[0][0] + 3,
                                      "text": plan.get("summary", "")[:40] or code, "style": "기본"}]
            planf.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
            kept = sum(b - a for a, b in new_keep)
            em.log(f"keep {len(keep_all)}→{len(new_keep)}구간, {kept:.0f}s / 대사 {len(plan['dialogue'])}")

            # ① 재컷 (클린본 → final)
            st = stages.load_state(outdir, code)
            clean = st.get("video") or str(outdir / f"{code}_클린.mp4")
            final = str(outdir / f"{code}_final.mp4")
            em.log("재컷 중...")
            with NullLock():
                P.cut_video(clean, new_keep, final, em.log, lambda fr: None)
            P.invalidate_derived(outdir, code, em.log)

            # ② 재자막(대사/내레이션 keep 재매핑)
            stages.stage_subs(cfg, code, em)
            # ③ 내레이션 재생성(안전 keep + 시각브리핑, 카운트다운 seq)
            regen_narration(outdir, cfg["meta_api"], log=em.log, seq=seq)
            # ④ 배너(재사용)
            b = stages.stage_banner(cfg, code, em, hold=hold)
            # ⑤ TTS
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
                    em.log(f"⚠ TTS 재시도 {attempt}/2 ({e})")
            # ⑥ 번인(+BGM 제거는 stage_burn 내 config remove_bgm)
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
            results.append((code, f"✔ {kept:.0f}s ({(time.time()-t0)/60:.1f}분)"))
        except Exception as e:
            import traceback
            traceback.print_exc()
            results.append((code, f"✘ {e}"))

    print(f"\n{'=' * 70}\n요약")
    for c, r in results:
        print(f"  {c}: {r}")


if __name__ == "__main__":
    main()
