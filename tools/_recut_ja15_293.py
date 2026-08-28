# -*- coding: utf-8 -*-
"""SNOS-293 눈검사 지적분 제거 → 재컷·재생산 (ja15, out_dir 명시).

1080p 리프레임(상단 크롭)에서 다리 노출이 커진다는 지적(2026-07-30). 크롭을 2.5배로
좁히고 하단 가림배너를 덮는 것으로 대부분 해결되지만, **크롭·배너로도 안 되는 두 구간**은
잘라낸다:
  ① final 4.0~5.2 (= seg1 내부 클린 31.24~32.44, 1.2s)
     허벅지+손 타이트 클로즈업 — 화면 전체가 허벅지라 가림배너 위까지 찬다.
     (배너 구간 0~5.6s 안이라 인포카드가 떠 있어 컷이 눈에 덜 띈다)
  ② final 62.5~89.8 (= seg3 내부 클린 181.53~208.83, 27.3s)
     바 스툴 아래 로우앵글 스타킹 다리 — 화면 전체가 다리라 크롭은 오히려 더 채우고
     블러/배너로 덮으면 27초가 통째로 가려진다. 잘라내는 게 유일한 방법.
  ※ 남는 마사지 구간(다리가 하단 35~45%)은 crop 2.5배 + 가림배너로 처리한다
    → _archive/scratch_2026/ja_reviewer_old/_reburn_1080_ja15.py 의 CROP/MASK 표 참조.

원본 keep [27.24,51.44] [88.39,125.27] [180.11,240.57] = 121.5s
새 keep 4구간 = 91.6s. seg3 앞머리 180.11~181.53(1.4s)은 너무 짧아 버린다.

사용: .venv\\Scripts\\python.exe tools\\_recut_ja15_293.py
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

CODE = "SNOS-293"
OUT_DIR = r"F:\ja_reviewer_out\ja15"
SEQ = (1, 5)          # 모음집 5편 중 1번째 — "첫 번째 작품" 서수 인트로 유지

NEW_KEEP = [
    [27.24,  31.24],    # seg1 앞 (허벅지 클로즈업 앞까지)
    [32.44,  51.44],    # seg1 뒤 (클로즈업 지나서부터)
    [88.39, 125.27],    # seg2 그대로
    [208.83, 240.57],   # seg3 뒤 (로우앵글 지나서부터 = 바 마감 장면)
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
