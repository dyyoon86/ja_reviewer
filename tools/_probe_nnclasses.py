# -*- coding: utf-8 -*-
r"""납품본 프레임에서 NudeNet 이 **실제로 무엇을 보는지** 전 클래스 그대로 찍어본다.

지금 판정은 `*_EXPOSED` 5종 + 살노출(ARMPITS/BELLY) 비율만 쓴다. 그런데 ja20 01회의
모자이크 구간·크롭탑 구간은 그 둘 다 0 으로 통과했다. 어떤 클래스가 뜨고 있는지 봐야
"속옷 노출까지 배제" 기준을 어디에 걸지 정할 수 있다.

사용: .venv\Scripts\python.exe tools\_probe_nnclasses.py <영상> --times 34,100,116 [--min 0.2]
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--times", required=True, help="검사 시각(초) 쉼표 구분")
    ap.add_argument("--min", type=float, default=0.20, help="이 점수 이상만 출력")
    ap.add_argument("--crop", default="", help="ffmpeg crop 필터(납품 리프레임과 동일하게)")
    args = ap.parse_args()

    from nudenet import NudeDetector
    det = NudeDetector()
    tmp = Path(tempfile.mkdtemp(prefix="nnprobe_"))
    try:
        for t in [float(x) for x in args.times.split(",") if x.strip()]:
            p = tmp / f"f{t:.0f}.jpg"
            vf = ["-vf", args.crop] if args.crop else []
            subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", args.video,
                            "-frames:v", "1"] + vf + [str(p)], check=False)
            if not p.is_file():
                print(f"{t:7.1f}s  (프레임 없음)")
                continue
            res = det.detect(str(p)) or []
            got = sorted(((x.get("class"), float(x.get("score", 0))) for x in res
                          if float(x.get("score", 0)) >= args.min),
                         key=lambda z: -z[1])
            if got:
                print(f"{t:7.1f}s  " + " · ".join(f"{c} {s:.2f}" for c, s in got))
            else:
                print(f"{t:7.1f}s  (검출 없음 ≥{args.min})")
    finally:
        for f in tmp.glob("*"):
            f.unlink(missing_ok=True)
        tmp.rmdir()


if __name__ == "__main__":
    main()
