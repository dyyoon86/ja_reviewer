# -*- coding: utf-8 -*-
"""ja18 — 사람 눈검사로 확정한 위험 구간을 final 좌표로 지정해 잘라내고 다시 완성본까지 만든다.

NudeNet 전수검사는 '노출 부위'만 본다. 유튜브에서 문제가 되는 **착의 성적 접촉·구강 암시·
속옷 노출·취중 접근**은 검출 0으로 통과한다(ja18에서 SNOS-321/334/309가 그랬다).
반대로 착의 상태를 0.4대로 오탐해 멀쩡한 편을 격리하기도 한다. 그래서 최종 판단은 사람이
몽타주로 하고, 그 결과를 이 스크립트에 초 단위로 박아 재컷한다.

구간은 **완성본(final) 좌표**로 적는다 — 몽타주에서 읽은 시각을 그대로 쓸 수 있다.
final_to_src로 클린본 좌표로 환원해 plan.keep에서 빼고, 재컷 → 내레이션 재생성(짧아진
길이에 맞게) → 자막 → TTS → 번인 순서로 완성본을 다시 만든다.

사용: .venv\\Scripts\\python.exe tools\\_recut_ja18.py --out <out_dir> [품번...]
"""
import argparse
import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server import pipeline as P
from server.core.regen import regen_narration
from batch_clean import CliEmitter
from batch_produce import ensure_voicebox, hide_narration
from trim_final_flags import final_to_src, subtract

# ── 사람 눈검사 결과 (final 좌표, 초) ────────────────────────────────────────
#    None 끝값은 '영상 끝까지'를 뜻한다.
CUTS = {
    "ABF-375":  [(159.0, 163.5)],   # 속옷 차림 + 남성 상반신, 침대
    "IPZZ-932": [(64.0, 202.0)],    # 아이스캔디 구강 암시 반복 + 132s 가슴골 클로즈업
    "SNOS-309": [(0.0, 38.0)],      # 어두운 침대 리액션 컷 연속
    "SNOS-321": [(57.0, 148.0)],    # 셔츠 열린 속옷 노출 + 비키니 화보 전체화면
    "SNOS-334": [(63.0, None)],     # 취해 쓰러진 여성에게 접근 — 이후 전부
    "SNOS-361": [(93.0, None)],     # 엉덩이 클로즈업 → 키스 → 구강 암시
}

# 소스에 박힌 타 사이트 워터마크(우리 것이 아니다) — delogo로 지운다. 1920x1080 기준.
# ★SNOS-334 소스에는 두 개가 박혀 있다: 가운데 큰 `SEXTB.NET`과 우상단 구석 `98堂`(장미).
#   다른 편(SNOS-306 등)에는 IPPA·S1 원본 로고뿐이라 이 편만의 문제다.
DELOGO = {"SNOS-334": [(1250, 38, 610, 62), (1755, 6, 150, 52)]}


def apply_delogo(video: Path, boxes, log):
    chain = ",".join(f"delogo=x={x}:y={y}:w={w}:h={h}" for x, y, w, h in boxes)
    tmp = video.with_name(video.stem + "_dl.mp4")
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf", chain,
           "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-c:a", "copy", str(tmp)]
    subprocess.run(cmd, check=True)
    tmp.replace(video)
    log(f"소스 워터마크 {len(boxes)}곳 제거(delogo)")


def main():
    ap = argparse.ArgumentParser(description="눈검사 구간 재컷 + 완성본 재생성")
    ap.add_argument("codes", nargs="*", help="품번(생략 시 CUTS 전체)")
    ap.add_argument("--out", help="out_dir 오버라이드")
    # ★ 2차 재컷용. CUTS의 좌표는 '그때의 final' 기준이라 이미 자른 편에 다시 쓰면 엉뚱한
    #   데를 자른다. 새로 찾은 구간은 --cut 으로 **현재 final 좌표**를 넘긴다.
    ap.add_argument("--cut", action="append", metavar="S:E",
                    help="CUTS 대신 쓸 구간(현재 final 좌표, 초). 여러 번 지정 가능. "
                         "끝값을 비우면(예 93:) 영상 끝까지. 품번 1개일 때만.")
    # 내레이션 텍스트를 건드리지 않는 짧은 재컷용. regen(meta_api+LLM)과 TTS(voicebox)를
    #   건너뛰므로 두 서비스가 꺼져 있어도 돌아간다. 잘린 구간에 내레이션이 없을 때만 안전하다
    #   — 줄이 사라지면 기존 wav(n001..)와 순번이 어긋나므로 그때는 regen을 돌려야 한다.
    ap.add_argument("--no-regen", action="store_true",
                    help="내레이션 재생성·TTS를 건너뛴다(자막 재타이밍은 그대로 수행)")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    if args.out:
        cfg["out_dir"] = args.out
    codes = [c.upper() for c in args.codes] or sorted(CUTS)
    cuts = dict(CUTS)
    if args.cut:
        if len(codes) != 1:
            ap.error("--cut 은 품번을 하나만 지정했을 때만 쓸 수 있다")
        spec = []
        for s in args.cut:
            a, _, b = s.partition(":")
            spec.append((float(a), float(b) if b.strip() else None))
        cuts[codes[0]] = spec
        print(f"※ --cut 지정 — {codes[0]} 은 CUTS 대신 {spec} 를 현재 final 좌표로 잘라낸다")
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT
    # 서수 인트로는 모음집 전체 기준이어야 한다 — 재컷 대상만 세면 "1번째"가 여럿 생긴다.
    allc = sorted({p.name for p in Path(cfg["out_dir"]).iterdir()
                   if p.is_dir() and not p.name.startswith("_")})
    n_all = len(allc)

    results = []
    for code in codes:
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        plan_f = outdir / f"{code}_plan.json"
        final = outdir / f"{code}_final.mp4"
        src = stages.load_state(outdir, code).get("video")
        seq = (allc.index(code) + 1, n_all) if code in allc else None
        print(f"\n{'=' * 70}\n{code}  (모음집 {seq[0]}/{seq[1]})" if seq else f"\n{code}", flush=True)
        if not (plan_f.is_file() and src and Path(src).is_file()):
            results.append((code, "✘ plan/클린본 누락"))
            continue
        t0 = time.time()
        step = "재컷"
        try:
            dur = P.video_duration(final) or 0.0
            spans = [(a, dur if b is None else b) for a, b in cuts[code]]
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
            bak = plan_f.with_suffix(".json.bak_eyecut")
            if not bak.exists():
                bak.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
            plan["keep"] = [[round(s, 3), round(e, 3)] for s, e in new_keep]
            plan_f.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
            P.cut_video(str(src), new_keep, str(final), em.log, lambda fr: None)

            if code in DELOGO:
                step = "워터마크"
                apply_delogo(final, DELOGO[code], em.log)

            if args.no_regen:
                em.log("※ --no-regen — 내레이션 텍스트·TTS는 그대로 두고 자막만 재타이밍한다")
            else:
                step = "내레이션"  # 짧아진 길이에 맞춰 줄 수를 다시 잡는다
                regen_narration(outdir, cfg["meta_api"], log=em.log, seq=seq)

            step = "자막"
            n_before = len(P.srt_parse(str(outdir / f"{code}_내레이션.srt"))) \
                if (outdir / f"{code}_내레이션.srt").is_file() else 0
            stages.stage_subs(cfg, code, em)
            if args.no_regen:
                # 내레이션 줄이 사라졌으면 기존 wav(n001..)와 순번이 어긋난다 — 그대로 두면
                # 1080p 리프레임에서 다른 문장의 음성이 얹힌다. 조용히 넘기지 말 것.
                n_after = len(P.srt_parse(str(outdir / f"{code}_내레이션.srt")))
                if n_after != n_before:
                    raise RuntimeError(
                        f"내레이션 줄 수가 {n_before}→{n_after}로 바뀌었다 — 잘린 구간에 "
                        f"내레이션이 걸려 있다. --no-regen 없이 다시 돌릴 것.")
                em.log(f"내레이션 {n_after}줄 유지 — 기존 TTS 클립 그대로 쓸 수 있다")

            step = "TTS"
            for attempt in ([] if args.no_regen else (1, 2, 3)):
                if not ensure_voicebox(cfg["tts_base"], em.log):
                    raise RuntimeError("voicebox 재기동 실패")
                try:
                    stages.stage_tts(cfg, code, cfg["tts_base"], cfg["tts_profile"],
                                     cfg.get("tts_language", "ko"), cfg.get("tts_seed"), False, em)
                    break
                except Exception as e:
                    if attempt == 3:
                        raise
                    em.log(f"⚠ TTS 실패({e}) — 재시도 {attempt}/2")

            step = "번인"
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
            stages.worklog(outdir, code, f"눈검사 재컷 — {before - after:.0f}s 제거 ({after:.0f}s)")
            results.append((code, f"✔ {after:.0f}s ({(time.time() - t0) / 60:.1f}분)"))
        except Exception as e:
            results.append((code, f"✘ {step} 실패: {e}"))
            traceback.print_exc()

    print(f"\n{'=' * 70}\n요약")
    for code, note in results:
        print(f"  {code}: {note}")
    fails = sum(1 for _, x in results if x.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
