# -*- coding: utf-8 -*-
"""SNOS-327 눈검사 지적분 제거 → 재컷·재생산 (ja15, out_dir 명시).

눈검사(2026-07-30)에서 걸린 두 구간:
  ① final 176.77~201.27 (= keep seg6 클린 759.85~784.35, 24.5s)
     얼굴 클로즈업에 **모자이크**가 계속 보이는 검열된 행위 장면.
     NudeNet 전수검사는 통과했다(모자이크는 NN 최대 사각) — 눈검사에서만 잡힌다.
  ② final 83.5~98.4 (= seg4 내부 클린 490.63~505.50, 14.9s)
     침대에서 뛰는 짧은 원피스를 **아래에서 올려 찍은 치마밑 앵글**, 속옷이 보인다.
     노출부위가 아니라 NN·CLIP·STT 전부 통과. 유튜브 기준으로는 위험.
     ※ 1차 재컷에서 경계를 504.63으로 잡았더니 로우앵글 잔재 약 1초가 남았다
       (클린 504.0~505.0). 505.50이 첫 깨끗한 와이드샷이라 여기로 옮겼다.

내레이션은 **재생성**한다 — 영상의 20%(40s)가 빠져 기존 26문장의 시각이 크게 어긋나고,
슬롯 수도 새 길이(163s) 기준으로 다시 잡아야 한다(regen_narration이 final.mp4를 보므로
재컷을 먼저 한다). 배너 PNG는 기존 것을 재사용한다.

사용: .venv\\Scripts\\python.exe tools\\_recut_ja15_327.py
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

CODE = "SNOS-327"
OUT_DIR = r"F:\ja_reviewer_out\ja15"
SEQ = (5, 5)          # 모음집 5편 중 5번째 — batch_produce와 동일한 서수 인트로 유지

NEW_KEEP = [
    [160.35, 183.55],   # 그대로
    [204.35, 216.55],   # 그대로
    [407.85, 434.67],   # 그대로
    [469.35, 490.63],   # seg4 앞부분 (치마밑 앞까지)
    [505.50, 543.88],   # seg4 뒷부분 (치마밑 잔재까지 지나서부터)
    [558.33, 598.35],   # 기존 seg5
    # seg6 [759.85, 784.35] 삭제 — 모자이크 행위
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
    # 잘려나간 구간의 대사는 버린다(내레이션은 곧 regen이 통째로 다시 씀)
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

    # 대사 자막을 새 keep 기준으로 재타이밍 (내레이션 srt는 바로 다음 regen이 덮어씀)
    stages.stage_subs(cfg, CODE, em)

    em.log("내레이션 재생성(새 길이 기준 슬롯 재계산)...")
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
