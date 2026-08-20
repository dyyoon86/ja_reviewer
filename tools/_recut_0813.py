# -*- coding: utf-8 -*-
"""ja18 — 업로드 전 사용자 판단 3건(+눈검사 추가분) 재컷.

`_recut_ja18.py` 와 하는 일은 같지만 두 가지가 다르다:
  · 구간을 스크립트 안 CUTS 가 아니라 **현재 final 좌표**로 아래에 새로 박았다
    (이미 한 번 자른 편이 있어 예전 좌표는 못 쓴다).
  · 내레이션 재생성(regen)·TTS 를 하지 않는다 — meta_api(우분투)와 voicebox 가 꺼져
    있고, 문장을 바꾸지 않으므로 필요도 없다. stage_subs 의 retime 이 남은 줄을
    새 타임라인으로 옮겨 주고(줄 수 보존 → 기존 n001.. 클립 순서 그대로), 1080p
    리번인은 `--allow-stale-tts` 로 돌린다.

컷 근거(2026-08-13 몽타주 눈검사):
  IPZZ-932  63.5~68.5   아이스캔디를 입에 무는 컷(구강 암시) — 남아 있던 잔재
            118.5~124.5 스타킹 다리 클로즈업
            150.3~끝    소파에 누운 채 다리·허벅지를 잡는 구간 전체
  SNOS-353  3.7~60.5    여교사 2명이 남학생에게 밀착해 속삭이는 씬 전체
                        (0~3.7 인트로 내레이션 자리는 남긴다)
  SNOS-321  11.4~14.0   폰 화면 수영복 화보 전체화면
            54.3~78.5   탈의실 — 셔츠 열린 가슴골 근접 투샷
            106.6~끝    체육복 지퍼 열린 가슴골 근접 투샷

사용: .venv\\Scripts\\python.exe tools\\_recut_0813.py --out <out_dir> [품번...]
"""
import argparse
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

CUTS = {
    # ── 1차(오전): 사용자 판단 보류였던 3건. **이미 적용됐다 — 다시 돌리지 말 것**
    #   IPZZ-932 (63.5,68.5)(118.5,124.5)(150.3,끝) / SNOS-353 (3.7,60.5)
    #   SNOS-321 (11.4,14.0)(54.3,78.5)(106.6,끝)
    # ── 2차: "웬만하면 노출 있으면 잘라라" 기준으로 전편 재검토한 결과 (현재 final 좌표)
    "SNOS-306": [(100.6, 106.6)],   # 수영장 — 수영복 차림 상반신
    "SNOS-309": [(28.6, 44.6)],     # 어두운 사무실 — 남자가 뒤에서 끌어안는 구간 전체
}


def main():
    ap = argparse.ArgumentParser(description="2026-08-13 눈검사 재컷(내레이션 문장 유지)")
    ap.add_argument("codes", nargs="*")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cfg = _common.load_cfg()
    cfg["out_dir"] = args.out
    codes = [c.upper() for c in args.codes] or sorted(CUTS)

    results = []
    for code in codes:
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        plan_f = outdir / f"{code}_plan.json"
        final = outdir / f"{code}_final.mp4"
        src = stages.load_state(outdir, code).get("video")
        print(f"\n{'=' * 70}\n{code}", flush=True)
        if not (plan_f.is_file() and src and Path(src).is_file()):
            results.append((code, "✘ plan/클린본 누락"))
            continue
        t0 = time.time()
        step = "재컷"
        try:
            dur = P.video_duration(final) or 0.0
            spans = [(a, dur if b is None else b) for a, b in CUTS[code]]
            em.log(f"영상 {dur:.1f}s — 잘라낼 구간 " +
                   ", ".join(f"{a:.1f}~{b:.1f}" for a, b in spans))
            plan = json.loads(plan_f.read_text(encoding="utf-8"))
            keep = P.parse_keep(plan.get("keep", []))
            before = sum(e - s for s, e in keep)
            new_keep = subtract(keep, final_to_src(spans, keep))
            if not new_keep:
                results.append((code, "✘ 자르고 나면 남는 구간이 없음"))
                continue
            after = sum(e - s for s, e in new_keep)
            em.log(f"keep {before:.0f}s → {after:.0f}s ({before - after:.0f}s 제거, "
                   f"{len(keep)}→{len(new_keep)}구간)")
            bak = plan_f.with_suffix(".json.bak_0813cut")
            if not bak.exists():
                bak.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
            plan["keep"] = [[round(s, 3), round(e, 3)] for s, e in new_keep]
            plan_f.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
            P.cut_video(str(src), new_keep, str(final), em.log, lambda fr: None)

            step = "자막"
            n_before = len(P.srt_parse(str(outdir / f"{code}_내레이션.srt")))
            stages.stage_subs(cfg, code, em)
            n_after = len(P.srt_parse(str(outdir / f"{code}_내레이션.srt")))
            if n_after != n_before:
                # 줄 수가 바뀌면 기존 TTS 클립(n001..)과 순번이 어긋난다 — 조용히 넘기지 말 것
                raise RuntimeError(f"내레이션 줄 수 {n_before}→{n_after} 변동 — TTS 재생성 필요")
            em.log(f"내레이션 {n_after}줄 유지 — 기존 TTS 클립 순서 그대로 쓸 수 있다")
            real = P.video_duration(final) or 0.0
            stages.worklog(outdir, code, f"눈검사 재컷(0813) — {before - after:.0f}s 제거 ({real:.0f}s)")
            results.append((code, f"✔ {dur:.0f}s → {real:.0f}s ({(time.time() - t0) / 60:.1f}분)"))
        except Exception as e:
            import traceback
            traceback.print_exc()
            results.append((code, f"✘ {step} 실패: {e}"))

    print(f"\n{'=' * 70}\n요약")
    for code, note in results:
        print(f"  {code}: {note}")
    fails = sum(1 for _, x in results if x.startswith("✘"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
