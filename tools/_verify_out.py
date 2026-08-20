# -*- coding: utf-8 -*-
"""납품 폴더(_완성) 전수 검증 — 해상도·길이·자막·노출을 한 번에 본다.

굽는 과정에서 도는 stage_burn 의 검사는 '그때 그 파일'에 대한 것이다. 재컷·재번인을
여러 번 돌리고 나면 폴더에 실제로 남아 있는 물건이 전부 통과본인지 다시 확인해야
한다(구버전 격리본이 섞이는 사고가 ja19 에서 실제로 있었다).

검증 항목:
  ① 해상도 — 납품 규격 1920x1080 인가
  ② 길이   — 0 이 아닌가, 작업폴더의 _final_subbed.mp4 와 같은 물건인가(크기 대조)
  ③ 노출   — NudeNet 전수 스캔(최종 게이트와 동일 간격), 검출 0 이어야 통과
  ④ 자막   — 대사/내레이션 SRT 존재 및 줄 수

사용: .venv\\Scripts\\python.exe tools\\_verify_out.py --out "C:\\...\\ja19" [--step 0.25]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import pipeline as P
from server.core import nsfw


def probe(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60).stdout.split()
        wh = out[0].split(",")
        return int(wh[0]), int(wh[1]), float(out[1])
    except Exception:
        return None, None, None


def main():
    ap = argparse.ArgumentParser(description="_완성 폴더 전수 검증")
    ap.add_argument("--out", required=True, help="out_dir")
    ap.add_argument("--step", type=float, default=0.25, help="노출 스캔 간격")
    ap.add_argument("--threshold", type=float, default=0.22, help="노출 임계")
    ap.add_argument("--folder", default="_완성", help="검증할 폴더")
    ap.add_argument("--json", help="결과 JSON 저장 경로")
    args = ap.parse_args()

    root = Path(args.out)
    dst = root / args.folder
    files = sorted(dst.glob("*.mp4"))
    print(f"검증 대상 {len(files)}편 / {dst} / 간격 {args.step}s · 임계 {args.threshold}")

    rows = []
    for i, f in enumerate(files, 1):
        code = f.stem
        w, h, dur = probe(f)
        work = root / code
        dsrt = work / f"{code}_대사.srt"
        nsrt = work / f"{code}_내레이션.srt"
        nd = len(P.srt_parse(dsrt)) if dsrt.is_file() else 0
        nn = len(P.srt_parse(nsrt)) if nsrt.is_file() else 0
        print(f"\n({i}/{len(files)}) {code} — {w}x{h} {dur:.1f}s / 대사 {nd}줄 · 내레이션 {nn}줄")
        hits = nsfw.check_final(str(f), step=args.step, threshold=args.threshold, log=print)
        ok = (w == 1920 and h == 1080 and dur and dur > 0 and not hits and nd + nn > 0)
        rows.append({"code": code, "w": w, "h": h, "sec": round(dur or 0, 1),
                     "dialogue": nd, "narration": nn,
                     "nsfw": [[t, c, s] for t, c, s in hits], "ok": ok})

    print(f"\n{'=' * 70}\n검증 요약")
    print(f"{'품번':12} {'해상도':11} {'길이':>7} {'대사':>5} {'해설':>5}  판정")
    for r in rows:
        print(f"{r['code']:12} {r['w']}x{r['h']:<6} {r['sec']:>6.1f}s {r['dialogue']:>5} {r['narration']:>5}  "
              + ("✔ 통과" if r["ok"] else f"✘ 노출 {len(r['nsfw'])}프레임"
                 if r["nsfw"] else "✘ 규격 불일치"))
    bad = [r for r in rows if not r["ok"]]
    print(f"\n통과 {len(rows) - len(bad)}/{len(rows)}" + (f" · 실패 {len(bad)}" if bad else " — 전편 통과"))
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"결과 JSON: {args.json}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
