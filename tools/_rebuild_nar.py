# -*- coding: utf-8 -*-
r"""재컷한 편의 `{code}_내레이션.wav` 를 현재 컷에 맞춰 다시 합성한다(TTS 없이).

★재컷 경로(_safecut / _drop_keep)는 `stage_subs` 로 내레이션 **SRT 시각**은 새 컷에
  맞춰 다시 쓰고, `reuse_tts` 로 **클립(n001..)** 도 다시 깔아준다. 그런데 그 둘을 합친
  **단일 트랙 `{code}_내레이션.wav` 는 아무도 다시 만들지 않는다**. 그래서 납품 폴더의
  내레이션이 재컷 전 길이 그대로 남아 영상보다 길어진다(ja20 실측: 영상 98s / wav 126s).
  프리미어에서 얹으면 끝이 넘친다.

이 도구는 voicebox 없이 처리한다 — 이미 있는 클립을 현재 SRT 시각으로 재배치할 뿐이다.

★슬롯이 좁아진 문장은 뺀다. 재컷으로 영상이 짧아지면 stage_subs 가 남은 빈틈에 문장을
  욱여넣어 0.8s 짜리 슬롯이 생기는데, 1.35배로 압축해도 영상보다 길어져 못 쓴다.

사용: .venv\Scripts\python.exe tools\_rebuild_nar.py --out "F:\ja_reviewer_out\ja20" ^
          --codes START-627,SNOS-365,JUR-820 [--min-slot 1.5] [--dry]
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import pipeline as P
from server.core import tts as T


def ts(x):
    x = max(0.0, x)
    h, m = int(x // 3600), int(x % 3600 // 60)
    s, ms = int(x % 60), int(round((x - int(x)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def rebuild(outdir, code, min_slot, dry):
    srt_f = outdir / f"{code}_내레이션.srt"
    final = outdir / f"{code}_final.mp4"
    clipdir = outdir / f"{code}_tts"
    if not (srt_f.is_file() and final.is_file() and clipdir.is_dir()):
        print(f"  {code}: srt/final/tts 누락 — 건너뜀")
        return False

    cur = P.srt_parse(str(srt_f))
    dur = float(P.video_duration(str(final)) or 0.0)
    keep = [(s, e, t) for s, e, t in cur if e - s >= min_slot]
    dropped = len(cur) - len(keep)
    print(f"  {code}: 영상 {dur:.1f}s · 내레이션 {len(cur)}줄"
          + (f" → {len(keep)}줄 (좁은 슬롯 {dropped}줄 제외)" if dropped else ""))
    if not keep:
        print("    ✗ 남는 문장이 없습니다 — 건너뜀")
        return False
    if dry:
        return True

    # 남길 문장의 클립을 n001.. 로 다시 깐다(원래 인덱스는 현재 SRT 순서 기준)
    idxs = [i for i, (s, e, _t) in enumerate(cur, 1) if e - s >= min_slot]
    tmp = outdir / "_narrebuild_tmp"
    if tmp.is_dir():
        shutil.rmtree(tmp)
    tmp.mkdir()
    for j, i in enumerate(idxs, 1):
        src = clipdir / f"n{i:03d}.wav"
        if not src.is_file():
            print(f"    ✗ 클립 없음: {src.name} — 건너뜀")
            shutil.rmtree(tmp)
            return False
        shutil.copy2(src, tmp / f"n{j:03d}.wav")
    for f in clipdir.glob("n*.wav"):
        f.unlink()
    for f in sorted(tmp.glob("n*.wav")):
        shutil.move(str(f), str(clipdir / f.name))
    tmp.rmdir()

    if dropped:
        srt_f.write_text("\n".join(f"{n}\n{ts(s)} --> {ts(e)}\n{t}\n"
                                   for n, (s, e, t) in enumerate(keep, 1)), encoding="utf-8")
        jf = outdir / f"{code}_내레이션.json"
        if jf.is_file():
            norm = lambda s: " ".join(str(s).split())
            txts = {norm(t) for _s, _e, t in keep}
            data = [d for d in json.loads(jf.read_text(encoding="utf-8"))
                    if norm(d.get("text", "")) in txts]
            for d, (s, e, _t) in zip(data, keep):
                d["start"], d["end"] = round(s, 2), round(e, 2)
            jf.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    clips = [(float(s), str(clipdir / f"n{j:03d}.wav"))
             for j, (s, _e, _t) in enumerate(keep, 1)]
    T.build_narration_wav(clips, str(outdir / f"{code}_내레이션.wav"),
                          log=lambda m: print(f"    {m}"), video_sec=dur)
    wav = float(P.video_duration(str(outdir / f"{code}_내레이션.wav")) or 0.0)
    mark = "✔" if wav <= dur + 0.5 else "★ 여전히 김"
    print(f"    {mark} wav {wav:.1f}s / 영상 {dur:.1f}s")
    return wav <= dur + 0.5


def main():
    ap = argparse.ArgumentParser(description="재컷 후 내레이션 wav 재합성(TTS 없이)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--codes", required=True, help="품번 쉼표 구분")
    ap.add_argument("--min-slot", type=float, default=1.5,
                    help="이보다 좁은 슬롯의 문장은 뺀다(초)")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    base = Path(args.out)
    bad = []
    for code in [c.strip() for c in args.codes.split(",") if c.strip()]:
        if not rebuild(base / code, code, args.min_slot, args.dry):
            bad.append(code)
    print(f"\n완료 — 문제 {bad if bad else '없음'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
