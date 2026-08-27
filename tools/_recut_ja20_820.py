# -*- coding: utf-8 -*-
r"""ja20 JUR-820 — 눈검사 재컷(내레이션 문장 유지, TTS 재생성 없음).

`_recut_0813.py` 와 같은 방식: **현재 final 좌표**로 구간을 지정하고 `final_to_src` 로
원본(클린본) 좌표로 환원해 plan.keep 에서 빼낸다.

컷 근거(2026-08-27 눈검사):
  21.25~25.0  속옷(자주색 브라) 차림 상반신 클로즈업. NN 이 21.75~24.5s 에 11프레임
              연속 검출(최고 MALE_GENITALIA_EXPOSED 0.41)했고, 굽기 게이트 0.35 를
              넘긴 유일한 편이라 _검수필요 로 격리됐다. 실제로 성기 노출은 아니지만
              살색 클로즈업이 3초 연속이라 뺀다. 검출 경계(0.25s 샘플)에 ±0.5s 여유.

★TTS 를 다시 만들지 않는다. 이 파이프라인의 번인은 stage_tts(mux=False) 라 내레이션
  음성이 영상에 안 섞인다 — 번인과 TTS 클립은 무관하다. 다만 사람이 나중에 조합할 때
  줄 번호가 맞아야 하므로, stage_subs 의 retime 뒤 **내레이션 줄 수가 그대로인지**를
  확인하고 달라지면 멈춘다(기존 n001.. 클립과 순번이 어긋나기 때문).

사용: .venv\Scripts\python.exe tools\_recut_ja20_820.py
"""
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server import pipeline as P
from batch_clean import CliEmitter
from trim_final_flags import final_to_src, subtract

OUT = r"F:\ja_reviewer_out\ja20"
CODE = "JUR-820"
CUTS = [(21.25, 25.0)]      # final 좌표


def main():
    cfg = _common.load_cfg()
    cfg["out_dir"] = OUT
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT
    outdir = stages.work_dir(cfg, CODE)
    em = CliEmitter(CODE)
    plan_f = outdir / f"{CODE}_plan.json"
    final = outdir / f"{CODE}_final.mp4"
    src = stages.load_state(outdir, CODE).get("video")
    if not (plan_f.is_file() and src and Path(src).is_file()):
        print("✘ plan/클린본 누락")
        sys.exit(1)

    t0 = time.time()
    dur = P.video_duration(final) or 0.0
    spans = [(a, dur if b is None else b) for a, b in CUTS]
    em.log(f"영상 {dur:.1f}s — 잘라낼 구간 " + ", ".join(f"{a:.2f}~{b:.2f}" for a, b in spans))

    plan = json.loads(plan_f.read_text(encoding="utf-8"))
    keep = P.parse_keep(plan.get("keep", []))
    before = sum(e - s for s, e in keep)
    bad = final_to_src(spans, keep)
    em.log("원본 좌표 환산: " + ", ".join(f"{a:.2f}~{b:.2f}" for a, b in bad))
    new_keep = subtract(keep, bad)
    if not new_keep:
        print("✘ 자르고 나면 남는 구간이 없음")
        sys.exit(1)
    after = sum(e - s for s, e in new_keep)
    em.log(f"keep {before:.1f}s → {after:.1f}s ({before - after:.1f}s 제거, "
           f"{len(keep)}→{len(new_keep)}구간)")

    bak = plan_f.with_suffix(".json.bak_ja20cut")
    if not bak.exists():
        bak.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        em.log(f"plan 백업: {bak.name}")
    plan["keep"] = [[round(s, 3), round(e, 3)] for s, e in new_keep]
    plan_f.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    em.log("재컷 중…")
    P.cut_video(str(src), new_keep, str(final), em.log, lambda fr: None)

    n_before = len(P.srt_parse(str(outdir / f"{CODE}_내레이션.srt")))
    stages.stage_subs(cfg, CODE, em)
    n_after = len(P.srt_parse(str(outdir / f"{CODE}_내레이션.srt")))
    if n_after != n_before:
        raise RuntimeError(f"내레이션 줄 수 {n_before}→{n_after} 변동 — TTS 재생성 필요")
    em.log(f"내레이션 {n_after}줄 유지 — 기존 TTS 클립(n001..) 순서 그대로")

    em.log("재번인…")
    stages.stage_burn(cfg, CODE, styles, em)

    real = P.video_duration(outdir / f"{CODE}_final_subbed.mp4") or 0.0
    stages.worklog(outdir, CODE, f"ja20 눈검사 재컷 — {before - after:.1f}s 제거 ({real:.0f}s)")
    print(f"\n✔ {dur:.1f}s → {real:.1f}s ({(time.time() - t0) / 60:.1f}분)")


if __name__ == "__main__":
    main()
