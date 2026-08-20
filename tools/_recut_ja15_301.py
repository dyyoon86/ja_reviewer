# -*- coding: utf-8 -*-
"""SNOS-301 눈검사 지적분 제거 → 재컷 (ja15).

지적(2026-07-30) — 1080p 납품본 final 50~56s. 메이드 의상이 **측면 몸통이 드러나는
끈 디자인**이고, 측면 프로필 6초 동안 언더버스트~허리 맨살과 가슴 옆선이 보인다.
NudeNet 전수검사는 0건이었다(정면 노출이 아니라 '노출 의상' 사각). 노출 위치가
화면 중앙 좌측·중간 높이여서 하단 가림배너(SNOS-293 방식)로는 덮을 수 없다 → 컷.

  final 49.8~56.0 = seg2 내부 클린 161.24~167.44 (6.2s)

남겨둔 것: final 38~40s는 남자 얼굴이 화면을 차지하고 그 옆에 맨팔·측면이 조금
보이는 정도라 유지한다(같은 의상의 정면 스탠딩 컷들은 코르셋 앞판이 가려 문제 없음).

내레이션은 재생성한다 — keep이 바뀌어 시각이 전부 밀린다. 배너 PNG는 재사용.

사용: .venv\\Scripts\\python.exe tools\\_recut_ja15_301.py
"""
import json
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa
from server import stages, pipeline as P
from server.stages import NullLock
from server.core.regen import regen_narration
from batch_clean import CliEmitter
from batch_produce import ensure_voicebox, hide_narration

CODE = "SNOS-301"
OUT_DIR = r"C:\Users\yoon\ja_reviewer_out\ja15"
SEQ = (2, 5)          # 모음집 5편 중 2번째 — "두 번째 작품"

NEW_KEEP = [
    [101.85, 128.26],   # 그대로
    [137.85, 161.24],   # seg2 앞 (측면 노출 앞까지)
    [167.44, 177.01],   # seg2 뒤 (측면 노출 지나서부터)
    [242.85, 258.27],   # 그대로
    [280.85, 295.56],   # 그대로
    [299.85, 334.47],   # 그대로
]


def in_keep(t, keep):
    return any(a <= t <= b for a, b in keep)


def main():
    cfg = _common.load_cfg()
    cfg["out_dir"] = OUT_DIR
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT
    outdir = stages.work_dir(cfg, CODE)
    em = CliEmitter(CODE)
    t0 = time.time()

    planf = outdir / f"{CODE}_plan.json"
    plan = json.loads(planf.read_text(encoding="utf-8"))
    bak = outdir / f"{CODE}_plan.json.pre_eyecheck"
    if not bak.is_file():
        bak.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        em.log(f"원본 plan 백업: {bak.name}")

    old_kept = sum(b - a for a, b in P.parse_keep(plan["keep"]))
    plan["keep"] = NEW_KEEP
    plan["dialogue"] = [d for d in plan.get("dialogue", [])
                        if in_keep(d.get("start", 0), NEW_KEEP)]
    plan["narration"] = [n for n in plan.get("narration", [])
                         if in_keep(n.get("start", 0), NEW_KEEP)]
    planf.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    kept = sum(b - a for a, b in NEW_KEEP)
    em.log(f"keep {len(NEW_KEEP)}구간 {kept:.1f}s (기존 {old_kept:.1f}s, -{old_kept - kept:.1f}s) "
           f"/ 대사 {len(plan['dialogue'])}줄")

    st = stages.load_state(outdir, CODE)
    clean = st.get("video") or str(outdir / f"{CODE}_클린.mp4")
    final = str(outdir / f"{CODE}_final.mp4")
    em.log("재컷 중...")
    with NullLock():
        P.cut_video(clean, NEW_KEEP, final, em.log, lambda fr: None)
    P.invalidate_derived(outdir, CODE, em.log)

    stages.stage_subs(cfg, CODE, em)

    em.log("내레이션 재생성(대사 회피 배치)...")
    regen_narration(outdir, cfg["meta_api"], log=em.log, seq=SEQ)

    for attempt in (1, 2, 3):
        if not ensure_voicebox(cfg["tts_base"], em.log):
            raise RuntimeError("voicebox 재기동 실패 — 수동 확인 필요")
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

    print(f"\n{CODE}: ✔ {kept:.1f}s ({(time.time() - t0) / 60:.1f}분)")


if __name__ == "__main__":
    main()
