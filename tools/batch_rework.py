# -*- coding: utf-8 -*-
"""ja12 일괄 재작업 — 무음구간 제거 + BGM 제거 + 자막/내레이션 재배치 + 새 배너 번인.

흐름(품번당):
  ① plan.keep으로 원본에서 재컷 → final
  ② demucs로 BGM 제거(보컬만) — 무음 감지는 보컬 트랙에서 해야 정확하다
  ③ silencedetect(보컬)로 무음 스팬 수집 → 내레이션 슬롯·컷 경계는 보호 → keep에서 제거
  ④ 원본에서 새 keep으로 재컷 → demucs 2차(새 컷도 보컬만)
  ⑤ stage_subs(대사 자막 재매핑) → stage_tts(내레이션 WAV 재배치)
  ⑥ stage_banner(새 메타: 3사이즈·얼굴·한글제목·일자) → 번인(+NudeNet 전수) → _완성 수거
  ※ 대사 0줄 작품(SNOS-281)은 무음 제거를 건너뛴다(전체가 무음이라 다 날아감).

사용: .venv\\Scripts\\python.exe tools\\batch_rework.py CODE...  (무인자면 ja12 11편 전체)
"""
import json
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import pipeline as P
from server import stages
from server.core import bgm, selfeval
from batch_clean import CliEmitter
from batch_produce import ensure_voicebox, hide_narration
from trim_final_flags import final_to_src, subtract

DEFAULT = ["ABF-366", "FNS-235", "MIDA-686", "SNOS-245", "SNOS-269", "SNOS-274",
           "SNOS-281", "SNOS-286", "SNOS-295", "START-599", "START-608"]

SIL_MIN = 1.2      # 이 길이 이상의 보컬 무음만 제거 대상
EDGE = 0.35        # 무음 양끝 숨쉴 틈(초) — 대사 직후가 뚝 끊기지 않게
BOUND = 0.4        # 컷 경계 보호(초) — 페이드 구간은 건드리지 않는다
MIN_RM = 0.6       # 이보다 짧아진 제거 조각은 무시
NAR_PAD = 0.3      # 내레이션 슬롯 보호 여유


def src_to_final(span, keep):
    """원본 좌표 구간 → final 좌표 구간들(keep에 걸친 부분만)."""
    a, b = span
    out, acc = [], 0.0
    for ks, ke in keep:
        s0, e0 = max(a, ks), min(b, ke)
        if e0 > s0:
            out.append((acc + s0 - ks, acc + e0 - ks))
        acc += ke - ks
    return out


def cut_spans(spans, protected):
    """spans에서 protected 구간을 뺀 조각들."""
    out = list(spans)
    for ps, pe in protected:
        nxt = []
        for s, e in out:
            if pe <= s or ps >= e:
                nxt.append((s, e))
                continue
            if s < ps:
                nxt.append((s, ps))
            if pe < e:
                nxt.append((pe, e))
        out = nxt
    return [(s, e) for s, e in out if e - s >= MIN_RM]


def rework(cfg, code, seq):
    outdir = Path(cfg["out_dir"]) / code
    em = CliEmitter(code)
    plan_f = outdir / f"{code}_plan.json"
    final = outdir / f"{code}_final.mp4"
    plan = json.loads(plan_f.read_text(encoding="utf-8"))
    keep = P.parse_keep(plan["keep"])
    st = stages.load_state(outdir, code)
    src = st.get("video")
    if not (src and Path(src).is_file()):
        raise RuntimeError("원본 영상 경로를 찾지 못함")

    dsrt = outdir / f"{code}_대사.srt"
    has_dlg = dsrt.is_file() and bool(P.srt_parse(str(dsrt)))

    # ① 현재 keep으로 재컷(결정적 기준점)
    em.log(f"① 재컷 — keep {len(keep)}개 {sum(e-s for s,e in keep):.1f}s")
    P.cut_video(str(src), keep, str(final), em.log, lambda fr: None)

    # ② BGM 제거 1차 — 보컬만
    bgm.remove_bgm(str(final), str(final), log=em.log, python=cfg.get("bgm_python"))

    # ③ 무음 감지(보컬 기준) → keep 축소
    if has_dlg:
        sil = selfeval.silences(str(final), min_sec=SIL_MIN, thresh_db=-45.0)
        dur = P.video_duration(str(final)) or 0.0
        # 보호 구간: 내레이션 슬롯 + 컷 경계
        nar_f = outdir / f"{code}_내레이션.json"
        protected = []
        if nar_f.is_file():
            for n in json.loads(nar_f.read_text(encoding="utf-8")):
                for fs, fe in src_to_final((n["start"] - NAR_PAD, n["end"] + NAR_PAD), keep):
                    protected.append((fs, fe))
        acc = 0.0
        for ks, ke in keep:
            d = ke - ks
            protected.append((max(0.0, acc - BOUND), acc + BOUND))
            acc += d
        protected.append((acc - BOUND, acc + BOUND))

        spans = []
        for s, d in sil:
            a, b = s + EDGE, min(s + d - EDGE, dur)
            if b > a:
                spans.append((a, b))
        removes = cut_spans(spans, protected)
        cut_total = sum(e - s for s, e in removes)
        em.log(f"③ 무음 {len(sil)}건 → 보호구간 제외 후 제거 {len(removes)}조각 {cut_total:.1f}s")
        if removes:
            bad_src = final_to_src(removes, keep)
            new_keep = subtract(keep, bad_src)
            if not new_keep:
                raise RuntimeError("keep이 전부 사라짐 — 무음 임계 조정 필요")
            plan["keep"] = [[round(s, 3), round(e, 3)] for s, e in new_keep]
            plan_f.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
            keep = new_keep
            # ④ 새 keep 재컷 + BGM 제거 2차
            P.cut_video(str(src), keep, str(final), em.log, lambda fr: None)
            bgm.remove_bgm(str(final), str(final), log=em.log, python=cfg.get("bgm_python"))
    else:
        em.log("③ 대사 0줄 — 무음 제거 건너뜀(BGM 제거만)")

    em.log(f"현재 final: {P.video_duration(str(final)):.1f}s")

    # ⑤ 자막 재매핑 + 내레이션 재배치
    stages.stage_subs(cfg, code, em)
    if not ensure_voicebox(cfg["tts_base"], em.log):
        raise RuntimeError("voicebox 기동 실패")
    stages.stage_tts(cfg, code, cfg["tts_base"], cfg["tts_profile"],
                     cfg.get("tts_language", "ko"), cfg.get("tts_seed"), False, em)

    # ⑥ 새 배너 + 번인(전수검사·수거 포함)
    stages.stage_banner(cfg, code, em, hold=float(cfg.get("banner_hold", 5)), preview=False)
    moved = hide_narration(outdir, code)
    try:
        stages.stage_burn(cfg, code, cfg.get("sub_styles") or P.STYLE_DEFAULT, em,
                          parts=None if has_dlg else {"subs": False})
    finally:
        import os
        for hidden, orig in moved:
            os.replace(hidden, orig)
    stages.worklog(outdir, code, "재작업: 무음 제거+BGM 제거+새 배너 번인")


def main():
    codes = [c for c in sys.argv[1:] if c] or DEFAULT
    cfg = _common.load_cfg()
    results = []
    for i, code in enumerate(codes, 1):
        print(f"\n{'=' * 70}\n({i}/{len(codes)}) {code}", flush=True)
        t0 = time.time()
        try:
            rework(cfg, code, (i, len(codes)))
            results.append((code, f"✔ 완료 ({(time.time() - t0) / 60:.1f}분)"))
        except Exception as e:
            traceback.print_exc()
            results.append((code, f"✘ 실패: {e}"))

    print(f"\n{'=' * 70}\n요약")
    for code, note in results:
        print(f"  {code}: {note}")
    fails = sum(1 for _, x in results if x.startswith("✘"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
