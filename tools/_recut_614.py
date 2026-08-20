# -*- coding: utf-8 -*-
"""START-614 눈검사 지적분 제거: seg3 앞머리(원본 432.44~443.80 = final 78.6~90.0)
혀 접촉 클로즈업 약 11초를 keep에서 빼고 재컷→재생산.

내레이션은 재생성하지 않는다(우분투 meta_api 다운 + 다른 10편과 톤 일관성 유지).
잘려나가는 2줄만 새 seg3 시작으로 옮겨 그대로 살린다. 배너도 기존 PNG 재사용.
사용: .venv\\Scripts\\python.exe tools\\_recut_614.py
"""
import json
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa
from server import stages, pipeline as P
from server.stages import NullLock
from batch_clean import CliEmitter
from batch_produce import ensure_voicebox, hide_narration

CODE = "START-614"
NEW_KEEP = [[299.95, 331.78], [377.20, 424.01], [443.80, 486.43]]
# 잘려나가는 내레이션 2줄 → 새 seg3 시작으로 이동(원본 좌표)
MOVE = {432.44: 443.80, 435.44: 446.80}


def in_keep(t, keep):
    return any(a <= t <= b for a, b in keep)


def main():
    cfg = _common.load_cfg()
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT
    outdir = stages.work_dir(cfg, CODE)
    em = CliEmitter(CODE)
    t0 = time.time()

    planf = outdir / f"{CODE}_plan.json"
    plan = json.loads(planf.read_text(encoding="utf-8"))
    bak = outdir / f"{CODE}_plan.json.prekiss"
    if not bak.is_file():
        bak.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    nar = []
    for n in plan.get("narration", []):
        s = round(n.get("start", 0), 2)
        if s in MOVE:
            dur = n["end"] - n["start"]
            n = dict(n, start=MOVE[s], end=MOVE[s] + dur)
            nar.append(n)
        elif in_keep(s, NEW_KEEP):
            nar.append(n)
    plan["keep"] = NEW_KEEP
    plan["narration"] = nar
    plan["dialogue"] = [d for d in plan.get("dialogue", []) if in_keep(d.get("start", 0), NEW_KEEP)]
    planf.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    kept = sum(b - a for a, b in NEW_KEEP)
    em.log(f"keep {len(NEW_KEEP)}구간 {kept:.0f}s / 내레이션 {len(nar)} / 대사 {len(plan['dialogue'])}")

    st = stages.load_state(outdir, CODE)
    clean = st.get("video") or str(outdir / f"{CODE}_클린.mp4")
    final = str(outdir / f"{CODE}_final.mp4")
    em.log("재컷 중...")
    with NullLock():
        P.cut_video(clean, NEW_KEEP, final, em.log, lambda fr: None)
    P.invalidate_derived(outdir, CODE, em.log)

    stages.stage_subs(cfg, CODE, em)

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
            em.log(f"⚠ TTS 재시도 {attempt}/2 ({e})")

    dsrt = outdir / f"{CODE}_대사.srt"
    has_dlg = dsrt.is_file() and bool(P.srt_parse(str(dsrt)))
    moved = hide_narration(outdir, CODE)
    try:
        stages.stage_burn(cfg, CODE, styles, em, parts=None if has_dlg else {"subs": False})
    finally:
        import os
        for hidden, orig in moved:
            os.replace(hidden, orig)

    print(f"\n{CODE}: ✔ {kept:.0f}s ({(time.time()-t0)/60:.1f}분)")


if __name__ == "__main__":
    main()
