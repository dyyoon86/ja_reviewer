# -*- coding: utf-8 -*-
"""자막이 빠진 것으로 판정된 구간만 정밀 재전사해 원문을 뽑는다(번역 근거용).

`_subcheck_0813.py` 가 찾은 구멍 중, 앞뒤 자막을 대조해 '진짜 누락'으로 남은 것만
아래 GAPS 에 넣고 돌린다. 전체 전사본의 텍스트는 세그먼트가 길게 뭉쳐 있어 그대로
번역하기엔 부정확하다 — 구간만 잘라 large-v3 로 다시 들으면 문장이 또렷해진다.

사용: .venv\\Scripts\\python.exe tools\\_gapdump_0813.py --out <out_dir>
"""
import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server.core.transcribe import transcribe_ranges

GAPS = {
    "ABF-375":  [(146.8, 149.4)],
    "IPZZ-907": [(10.4, 15.6)],
    "IPZZ-932": [(58.4, 62.2)],
    "SNOS-321": [(48.6, 54.6)],
    "SNOS-334": [(32.9, 37.6)],
    "SNOS-353": [(24.0, 27.0), (53.2, 57.2), (64.9, 70.3)],
    "SNOS-361": [(53.8, 61.4)],
    "DSOD-001": [(37.0, 41.6)],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    for code, ranges in GAPS.items():
        v = out / code / f"{code}_final.mp4"
        print(f"\n===== {code}", flush=True)
        try:
            segs = transcribe_ranges(v, ranges, log=lambda m: None)
        except Exception as e:
            print(f"  ✘ {e}")
            continue
        if not segs:
            print("  (발화 없음 — 잡음이었을 가능성)")
        for a, b, t in segs:
            print(f"  {a:7.2f}~{b:7.2f}  {t}")


if __name__ == "__main__":
    main()
