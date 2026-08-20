# -*- coding: utf-8 -*-
"""섹션③ 마무리 — 내레이션 mux(→_final_voiced.mp4) + ⑥굽기(→_final_subbed.mp4).

_regen_tts.py 는 batch_produce 와 같은 프리셋(mux=False)으로 돌아 `{code}_내레이션.wav`
까지만 만든다. 여기서 그 WAV 를 영상에 입히고(원음 덕킹) 자막·배너·워터마크를 굽는다.
★ TTS 를 다시 부르지 않는다 — 이미 뽑아 둔 `{code}_tts/n*.wav` 클립을 그대로 쓴다.

굽는 것: 대사 자막 + 내레이션 자막 + 프레임/인포카드/워터마크, 1080p 리프레임(config
reframe_1080). stage_burn 이 완성본 전수검사 + _완성/_검수필요 수거까지 한다.

★ n001.. ↔ 문장 매핑은 **TTS 를 뽑던 그때의 srt 순서**다. 그 뒤 재컷/재배치로 srt 를
  건드렸다면 클립 순서가 어긋난다(ja18 사고) — 이 스크립트는 srt 를 안 고치는 전제.

사용: .venv\\Scripts\\python.exe tools\\_finish_ja19.py --out "C:\\...\\ja19" --skip MIDA-703
"""
import argparse
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server import pipeline as P
from batch_clean import CliEmitter


def finish_one(cfg, code, em, duck=0.3, no_mux=False):
    """한 편의 섹션③ 마무리 — BGM 제거 → 내레이션 mux → 굽기(+리프레임·전수검사·수거).

    ★_safecut.py 도 이 함수를 그대로 쓴다. 재컷 편과 무재컷 편이 다른 경로로
      마무리되면 납품 규격이 갈리므로, 마무리는 반드시 여기 한 곳에서만 한다.
    """
    outdir = stages.work_dir(cfg, code)
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT
    final = outdir / f"{code}_final.mp4"
    wav = outdir / f"{code}_내레이션.wav"
    srt = outdir / f"{code}_내레이션.srt"
    clipdir = outdir / f"{code}_tts"
    if not final.is_file():
        raise RuntimeError(f"final.mp4 없음: {final}")

    # ★ BGM 제거는 내레이션을 입히기 **전에** 한다(ja13 교훈).
    #   stage_burn 의 remove_bgm 은 맨 마지막, 즉 내레이션이 이미 섞인 오디오에
    #   demucs 를 건다 — TTS 목소리까지 분리기를 통과해 음질이 상한다.
    if bool(cfg.get("remove_bgm", False)) and not no_mux:
        marker = outdir / f"{code}_final.nobgm"
        if marker.is_file():
            em.log("BGM 이미 제거됨 — 건너뜀")
        else:
            from server.core import bgm
            em.log("원본 BGM 제거 (demucs — 내레이션 얹기 전)")
            bgm.remove_bgm(str(final), str(final), log=em.log,
                           python=cfg.get("bgm_python"),
                           model=cfg.get("bgm_model") or "htdemucs")
            marker.write_text("demucs done", encoding="utf-8")

    if not no_mux:
        if not (wav.is_file() and srt.is_file()):
            raise RuntimeError("내레이션 wav/srt 없음 — TTS 먼저")
        # 덕킹 구간(실제 발화 spans)을 얻으려면 트랙을 한 번 다시 합성해야 한다.
        # TTS 호출은 없고 기존 클립을 이어붙이는 것뿐이라 수 초.
        entries = P.srt_parse(srt)
        clips = [(st, str(clipdir / f"n{i:03d}.wav"))
                 for i, (st, en, tx) in enumerate(entries, 1)]
        missing = [w for _, w in clips if not Path(w).is_file()]
        if missing:
            raise RuntimeError(f"TTS 클립 {len(missing)}개 없음(첫: {missing[0]})")
        em.log(f"내레이션 트랙 재합성 {len(clips)}문장(TTS 재생성 없음)")
        _, spans = P.build_narration_wav(clips, str(wav), em.log,
                                         video_sec=P.video_duration(str(final)))
        voiced = outdir / f"{code}_final_voiced.mp4"
        em.log("영상에 내레이션 입히는 중(원음 덕킹)…")
        P.mux_narration(str(final), str(wav), str(voiced), mode="duck",
                        duck_level=duck, duck_spans=spans, log=em.log)
        em.file("음성 입힌 영상", voiced)

    # voiced 가 있으면 stage_burn 이 알아서 그걸 소스로 쓴다.
    # remove_bgm=False — 위에서 내레이션 전에 이미 걸었다(두 번 걸면 TTS가 상한다).
    # 리프레임(1080p 상단 확대)은 config reframe_1080 을 따른다.
    stages.stage_burn(cfg, code, styles, em, remove_bgm=False)
    return outdir / f"{code}_final_subbed.mp4"


def main():
    ap = argparse.ArgumentParser(description="내레이션 mux + 굽기")
    ap.add_argument("--out", help="out_dir 오버라이드")
    ap.add_argument("--skip", default="", help="제외할 품번(쉼표 구분)")
    ap.add_argument("--only", default="", help="이 품번만(쉼표 구분)")
    ap.add_argument("--duck", type=float, default=0.3, help="해설 중 원음 볼륨(0~1)")
    ap.add_argument("--no-mux", action="store_true", help="mux 건너뛰고 굽기만")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    if args.out:
        cfg["out_dir"] = args.out
    skip = {c.strip().upper() for c in args.skip.split(",") if c.strip()}
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT

    root = Path(cfg["out_dir"])
    codes = sorted(d.name for d in root.iterdir()
                   if d.is_dir() and not d.name.startswith("_")
                   and (d / f"{d.name}_plan.json").is_file())
    codes = [c for c in codes if c not in skip and (not only or c in only)]
    print(f"대상 {len(codes)}개 / out_dir={root} / "
          f"리프레임1080={cfg.get('reframe_1080', False)} / 덕킹={args.duck}")

    results = []
    for i, code in enumerate(codes, 1):
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        print(f"\n{'=' * 70}\n({i}/{len(codes)}) {code}", flush=True)
        t0 = time.time()
        step = "마무리"
        try:
            sub = finish_one(cfg, code, em, duck=args.duck, no_mux=args.no_mux)
            sz = sub.stat().st_size / 1e6 if sub.is_file() else 0
            results.append((code, f"✔ 완료 ({sz:.0f}MB)", time.time() - t0))
        except Exception as e:
            results.append((code, f"✘ {step} 실패: {e}", time.time() - t0))
            traceback.print_exc()

    print(f"\n{'=' * 70}\n요약")
    for code, note, el in results:
        print(f"  {code}: {note} ({el / 60:.1f}분)")
    fails = sum(1 for _, x, _ in results if x.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
