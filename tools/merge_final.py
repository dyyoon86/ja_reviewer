# -*- coding: utf-8 -*-
"""★최종_업로드용 전체를 통합본 한 개로 합친다 — 순위 브릿지 삽입.

  .venv\\Scripts\\python.exe tools\\merge_final.py --out F:\\ja_reviewer_out\\ja27

규칙
  · `NN_M위_품번.mp4` 앞에 `_브릿지/브릿지_MM.mp4`(M위)가 있으면 끼운다.
  · 브릿지 앞·뒤 이음매는 컷(브릿지 자체가 흰 번쩍 전환을 갖고 있다).
  · 브릿지가 없는 이음매(1위→아웃트로 등)는 0.6초 크로스페이드.
  · 인트로/아웃트로 48k·본편 44.1k 혼재 → concat **필터**로 전부 정규화(demuxer 금지).
결과: {out}/_통합_{배치명}.mp4
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
XF = 0.6


def dur(f):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(f)], capture_output=True, text=True)
    return float(r.stdout.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-bridge", action="store_true", help="브릿지 없이 전부 크로스페이드")
    a = ap.parse_args()
    out = Path(a.out)
    final_dir = out / "★최종_업로드용"
    if not final_dir.is_dir():
        final_dir = out / "_납품_자막"
    parts = sorted(final_dir.glob("*.mp4"))
    if not parts:
        print(f"[X] 합칠 파일이 없습니다: {final_dir}")
        return 1

    # (파일, 앞 이음매가 컷인가)
    seq = []
    for f in parts:
        m = re.match(r"\d+_(\d+)위_", f.name)
        br = out / "_브릿지" / f"브릿지_{int(m.group(1)):02d}.mp4" if m else None
        if br and br.is_file() and not a.no_bridge:
            seq.append((br, True))
            seq.append((f, True))
        else:
            seq.append((f, False))
    seq[0] = (seq[0][0], True)

    # 컷으로 이어지는 조각끼리 run 으로 묶고, run 사이는 크로스페이드
    runs = []
    for i, (f, cut) in enumerate(seq):
        if i == 0 or not cut:
            runs.append([i])
        else:
            runs[-1].append(i)

    durs = [dur(f) for f, _ in seq]
    ins, filt = [], []
    for i, (f, cut) in enumerate(seq):
        print(f"  {'│' if cut and i else '╳'} {f.name:30s} {durs[i]:7.1f}s")
        ins += ["-i", str(f)]
        filt.append(f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                    f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p,setsar=1[n{i}]")
        filt.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=N/SR/TB[m{i}]")

    rdur = []
    for k, run in enumerate(runs):
        if len(run) == 1:
            filt.append(f"[n{run[0]}]settb=AVTB[rv{k}]")
            filt.append(f"[m{run[0]}]anull[ra{k}]")
        else:
            pads = "".join(f"[n{i}][m{i}]" for i in run)
            filt.append(f"{pads}concat=n={len(run)}:v=1:a=1[rc{k}][ra{k}]")
            filt.append(f"[rc{k}]settb=AVTB[rv{k}]")
        rdur.append(sum(durs[i] for i in run))

    vprev, aprev, acc = "[rv0]", "[ra0]", rdur[0]
    for k in range(1, len(runs)):
        last = k == len(runs) - 1
        vout, aout = ("[vo]", "[ao]") if last else (f"[x{k}]", f"[y{k}]")
        off = acc - XF
        filt.append(f"{vprev}[rv{k}]xfade=transition=fade:duration={XF}:offset={off:.3f}{vout}")
        filt.append(f"{aprev}[ra{k}]acrossfade=d={XF}:c1=tri:c2=tri{aout}")
        vprev, aprev, acc = vout, aout, off + rdur[k]
    if len(runs) == 1:
        vprev, aprev = "[rv0]", "[ra0]"

    dst = out / f"_통합_{out.name}.mp4"
    cmd = ["ffmpeg", "-v", "error", "-stats", "-y", *ins, "-filter_complex", ";".join(filt),
           "-map", vprev, "-map", aprev,
           "-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "20", "-b:v", "0",
           "-c:a", "aac", "-b:a", "192k", "-r", "30", "-movflags", "+faststart", str(dst)]
    print(f"조각 {len(seq)}개 / 크로스페이드 {len(runs) - 1}곳 → {dst}  예상 {acc / 60:.1f}분", flush=True)
    r = subprocess.run(cmd)
    if r.returncode:
        print(f"[X] rc={r.returncode}")
        return r.returncode
    got = dur(dst)
    ok = abs(got - acc) < 1.5
    print(f"[{'OK' if ok else '!'}] {dst}  {got:.1f}s (예상 {acc:.1f}s)")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
