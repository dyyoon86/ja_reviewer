# -*- coding: utf-8 -*-
"""지정 품번들의 클린본을 정밀 NN 재스캔(기본 0.25s)해 검출 구간을 물리 컷으로 제거.

전수 검사(batch_final_check)에서 노출이 나온 품번용 — 1초짜리 노출은 ⓪ 클린의
1.0s 간격 스캔이 프레임 오프셋 때문에 놓친 것이라, 간격을 좁혀 다시 자른다.
컷이 발생하면 전사·plan을 무효화(삭제)해 이후 batch_review가 새로 만들게 한다.

사용: .venv\\Scripts\\python.exe tools\\reclean_targets.py SNOS-269 SNOS-295 START-608
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import pipeline as P
from server import stages
from server.core import nsfw

STEP, THRESHOLD, PAD, MERGE_GAP, MIN_CLIP = 0.25, 0.22, 2.0, 6.0, 3.0


def main():
    codes = [c for c in sys.argv[1:] if c]
    if not codes:
        print("사용: reclean_targets.py <품번>...")
        sys.exit(1)
    cfg = _common.load_cfg()

    for code in codes:
        outdir = Path(cfg["out_dir"]) / code
        clean = outdir / f"{code}_클린.mp4"
        print(f"\n{'=' * 70}\n{code}", flush=True)
        if not clean.is_file():
            print(f"  클린본 없음: {clean}")
            continue
        dur = P.video_duration(clean) or 0.0
        bad = nsfw.build_map(str(clean), step=STEP, threshold=THRESHOLD, pad=PAD,
                             merge_gap=MERGE_GAP, cache=None,
                             log=lambda m: print(f"[{code}] {m}", flush=True),
                             duration=dur)
        if not bad:
            print(f"[{code}] 정밀 스캔 검출 0 — 클린본 유지")
            continue
        keep = nsfw.complement(bad, dur, min_len=MIN_CLIP)
        if not keep:
            print(f"[{code}] ✘ 전부 제거되어 남는 게 없음 — 수동 검수 필요")
            continue
        cut = sum(b - a for a, b in bad)
        print(f"[{code}] {len(bad)}구간 {cut:.1f}s 제거 → 재컷")
        tmp = outdir / f"{code}_클린_재.mp4"
        P.cut_video(str(clean), keep, str(tmp),
                    lambda m: print(f"[{code}] {m}", flush=True), lambda fr: None)
        tmp.replace(clean)
        stages.save_state(outdir, code, video=str(clean), cleaned=True)
        # 시간축이 바뀌었으므로 전사·plan 무효화 → batch_review가 새로 생성
        for f in (outdir / f"{code}_전사.json", outdir / f"{code}_plan.json"):
            if f.is_file():
                f.unlink()
                print(f"[{code}] 무효화: {f.name}")
        new_dur = P.video_duration(clean) or 0.0
        stages.worklog(outdir, code, f"정밀 재클린(0.25s NN) — {len(bad)}구간 {cut:.1f}s 제거, "
                                     f"{dur:.0f}s → {new_dur:.0f}s")
        print(f"[{code}] ✔ 재클린 완료: {dur:.0f}s → {new_dur:.0f}s")


if __name__ == "__main__":
    main()
