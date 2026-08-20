# -*- coding: utf-8 -*-
"""ja18 — 내레이션/대사 자막을 plan 기준으로 재타이밍하고 최종컷 좌표를 검증한다.

배경: `_apply_narfix.py` 는 대안 문장을 반영하며 `{code}_내레이션.srt` 를
`{code}_내레이션.json`(**클린본 좌표**)의 start/end 로 다시 쓴다. srt 는 최종컷 좌표여야
하므로, narfix 를 태운 편은 내레이션 시각이 통째로 어긋난다(영상 길이를 넘겨 아예
안 나오는 편까지 생겼다). stage_subs 를 다시 돌리면 plan.keep 기준으로 재타이밍된다.

사용: .venv\\Scripts\\python.exe tools\\_resubs_0813.py --out <out_dir> [품번...]
"""
import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server import pipeline as P
from batch_clean import CliEmitter


def srt_span(path: Path):
    if not path.is_file():
        return 0, 0.0
    ev = P.srt_parse(str(path))
    return len(ev), (max(e[1] for e in ev) if ev else 0.0)


def main():
    ap = argparse.ArgumentParser(description="자막 재타이밍 + 최종컷 좌표 검증")
    ap.add_argument("codes", nargs="*")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cfg = _common.load_cfg()
    cfg["out_dir"] = args.out
    out = Path(args.out)
    codes = [c.upper() for c in args.codes] or sorted(
        p.name for p in out.iterdir()
        if p.is_dir() and not p.name.startswith("_")
        and (p / f"{p.name}_plan.json").is_file())

    rows = []
    for code in codes:
        outdir = out / code
        final = outdir / f"{code}_final.mp4"
        dur = P.video_duration(final) or 0.0
        n_before, end_before = srt_span(outdir / f"{code}_내레이션.srt")
        print(f"\n{'=' * 66}\n{code}  (영상 {dur:.1f}s)", flush=True)
        try:
            stages.stage_subs(cfg, code, CliEmitter(code))
        except Exception as e:
            rows.append((code, dur, f"✘ {e}", None, None))
            continue
        n_after, end_after = srt_span(outdir / f"{code}_내레이션.srt")
        n_dlg, end_dlg = srt_span(outdir / f"{code}_대사.srt")
        moved = abs(end_after - end_before) > 0.5 or n_after != n_before
        rows.append((code, dur, "바뀜" if moved else "동일",
                     (n_before, end_before), (n_after, end_after, n_dlg, end_dlg)))

    print(f"\n{'=' * 66}\n요약  (내레이션 끝 시각이 영상 길이를 넘으면 ✘)")
    for code, dur, note, before, after in rows:
        if after is None:
            print(f"  {code:<10} {note}")
            continue
        nb, eb = before
        na, ea, nd, ed = after
        flag = "✘ 영상 밖" if ea > dur + 0.5 else "✔"
        print(f"  {code:<10} 영상 {dur:6.1f}s | 내레이션 {nb}줄 끝 {eb:6.1f}s "
              f"→ {na}줄 끝 {ea:6.1f}s {flag} | 대사 {nd}줄 끝 {ed:6.1f}s  [{note}]")


if __name__ == "__main__":
    main()
