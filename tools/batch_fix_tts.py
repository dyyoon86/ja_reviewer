#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LONG keep 폴더 수정 + 전체 TTS 배치 생성.

※ 2026-07-10 배치(SNOS 3건 keep 수정) 전용 원오프 — KEEP_FIX가 하드코딩돼 있다.
   범용 배치 TTS가 필요하면 GUI 큐(stage_tts)를 쓰고, 이 파일은 패턴 참고용으로만.
"""
import json, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import _common
from server import pipeline as P

CFG      = _common.load_cfg()
OUT_DIR  = Path(CFG["out_dir"])
TTS_BASE = CFG["tts_base"]
TTS_PROF = CFG["tts_profile"] or "ebc09f54-89c2-4797-97a0-6b7a6056a7dd"  # 윤두영일반
TTS_LANG = CFG["tts_language"]
TTS_SEED = 42  # 동일 시드 → 일관된 음색

# keep 수정이 필요한 폴더: {code: new_keep}
KEEP_FIX = {
    "SNOS-213": [[26.0, 34.0], [39.0, 71.0], [194.0, 219.0]],   # 101s→65s
    "SNOS-256": [[11.0, 23.0], [254.0, 286.0], [480.0, 506.0]], # 120s→70s
    "SNOS-285": [[57.0, 90.0], [144.0, 173.0]],                  # 108s→62s
}


def retime_dialogue(plan, keep):
    dlg = plan.get("dialogue", [])
    if not dlg:
        return []
    entries = [(d["start"], d["end"], d.get("ko", ""), d.get("speaker", "여")) for d in dlg]
    return P.retime(entries, keep, snap=False)


def save_srt(entries, path, is_dialogue=True):
    srt = []
    for i, (s, e, text, *_) in enumerate(entries, 1):
        srt += [str(i), f"{P.s2srt(s)} --> {P.s2srt(e)}", text, ""]
    Path(path).write_text("\n".join(srt), encoding="utf-8-sig")


def fix_folder(code, new_keep):
    folder = OUT_DIR / code
    pf = folder / f"{code}_plan.json"
    vf = folder / f"{code}_trim.mp4"
    final = folder / f"{code}_final.mp4"

    plan = json.loads(pf.read_text(encoding="utf-8"))
    old_total = sum(e-s for s,e in plan["keep"])
    new_total = sum(e-s for s,e in new_keep)
    print(f"  keep: {len(plan['keep'])}구간 {old_total:.0f}s → {len(new_keep)}구간 {new_total:.0f}s")

    plan["keep"] = new_keep
    pf.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    keep = P.parse_keep(new_keep)
    print(f"  cut_video 실행 중...")
    P.cut_video(str(vf), keep, str(final))
    dur = P.video_duration(str(final))
    print(f"  final.mp4: {dur:.1f}s")

    # 대사 SRT 재타이밍
    dlg_rt = retime_dialogue(plan, keep)
    if dlg_rt:
        save_srt(dlg_rt, folder / f"{code}_대사.srt")
        json_out = [{"start": s, "end": e, "ko": ko, "speaker": sp} for s, e, ko, sp in dlg_rt]
        (folder / f"{code}_대사.json").write_text(
            json.dumps(json_out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  대사 SRT: {len(dlg_rt)}줄")

    return keep


def make_final(code):
    """START-597처럼 final.mp4만 없는 경우"""
    folder = OUT_DIR / code
    pf = folder / f"{code}_plan.json"
    vf = folder / f"{code}_trim.mp4"
    final = folder / f"{code}_final.mp4"
    plan = json.loads(pf.read_text(encoding="utf-8"))
    keep = P.parse_keep(plan["keep"])
    print(f"  cut_video ({len(keep)}구간)...")
    P.cut_video(str(vf), keep, str(final))
    dur = P.video_duration(str(final))
    print(f"  final.mp4: {dur:.1f}s")


def run_tts(code):
    folder = OUT_DIR / code
    srt_path = folder / f"{code}_내레이션.srt"
    wav_path  = folder / f"{code}_내레이션.wav"
    clipdir   = folder / f"{code}_tts"
    clipdir.mkdir(parents=True, exist_ok=True)

    entries = P.srt_parse(srt_path)
    if not entries:
        print(f"  [SKIP] 내레이션 SRT 비어있음")
        return

    clips = []
    for i, (st, en, text) in enumerate(entries, 1):
        out_wav = str(clipdir / f"n{i:03d}.wav")
        print(f"  음성 {i}/{len(entries)}: {text[:20]}")
        P.tts_generate(TTS_BASE, text, TTS_PROF, TTS_LANG, out_wav, seed=TTS_SEED, log=print)
        clips.append((st, out_wav))

    print(f"  내레이션 WAV 합성 중...")
    P.build_narration_wav(clips, str(wav_path), log=print)
    print(f"  완료: {wav_path.name}")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-fix", action="store_true")
    ap.add_argument("--skip-tts", action="store_true")
    ap.add_argument("--only", help="특정 코드만 처리 (쉼표 구분)")
    args = ap.parse_args()

    only = [x.strip() for x in args.only.split(",")] if args.only else None

    # ── 1단계: LONG 폴더 keep 수정 ─────────────────────────
    if not args.skip_fix:
        print("\n=== 1단계: keep 구간 수정 ===")
        for code, new_keep in KEEP_FIX.items():
            if only and code not in only:
                continue
            print(f"\n[{code}] 수정 중...")
            keep = fix_folder(code, new_keep)

        # START-597: final.mp4 없음
        if not only or "START-597" in only:
            if not (OUT_DIR / "START-597" / "START-597_final.mp4").exists():
                print(f"\n[START-597] final.mp4 생성...")
                make_final("START-597")

        # LONG 폴더 나레이션 SRT 재생성
        print("\n=== 나레이션 SRT 재생성 (수정된 폴더) ===")
        import subprocess, os
        regen = Path(__file__).parent / "regen_narration.py"
        for code in list(KEEP_FIX.keys()):
            if only and code not in only:
                continue
            folder = str(OUT_DIR / code)
            print(f"\n[{code}] regen_narration...")
            r = subprocess.run(
                [sys.executable, str(regen), folder],
                capture_output=True, text=True, encoding="utf-8")
            print(r.stdout.strip())
            if r.returncode != 0:
                print("STDERR:", r.stderr[:300])

    # ── 2단계: 전체 TTS ────────────────────────────────────
    if not args.skip_tts:
        print("\n=== 2단계: TTS 생성 ===")
        folders = sorted([f for f in OUT_DIR.iterdir() if f.is_dir()])
        for f in folders:
            code = f.name
            if only and code not in only:
                continue
            wav = f / f"{code}_내레이션.wav"
            if wav.exists():
                print(f"[{code}] 이미 존재 — SKIP")
                continue
            srt = f / f"{code}_내레이션.srt"
            if not srt.exists():
                print(f"[{code}] SRT 없음 — SKIP")
                continue
            print(f"\n[{code}] TTS 시작...")
            try:
                run_tts(code)
            except Exception as e:
                print(f"  ERROR: {e}")

    print("\n모든 작업 완료")


if __name__ == "__main__":
    main()
