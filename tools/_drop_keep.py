# -*- coding: utf-8 -*-
r"""눈검사로 확정한 구간을 plan.keep 에서 **원본(클린본) 좌표로** 빼낸다.

★자동 검사가 원리적으로 못 잡는 것이 있다 — 대표가 **모자이크 행위 장면**이다.
  NudeNet 은 '부위 노출'을 보는데 모자이크가 덮으면 EXPOSED 가 안 뜬다(ja20 01회
  source 35~46s: NN 검출 0인데 화면 하단에 모자이크가 선명). 격자 주기성으로 잡아보려
  했지만 h264 매크로블록·2배 업스케일과 구분이 안 돼 실패했다(tools/_probe_mosaic.py).
  그래서 사람(또는 Claude)이 몽타주로 확정한 구간은 이 도구로 직접 빼낸다.

빼는 방식은 `_safecut` 과 같다 — keep 에서 그 구간을 잘라내고, 남은 조각이 --min-clip
미만이면 버린다. plan 은 `.presafe` 로 백업한다(_safecut 과 같은 이름이라 한쪽만 남는다).
자막·내레이션 정리와 재컷은 하지 않는다 — 이 도구로 keep 을 먼저 줄이고 나서
`_safecut --strict --carve-first` 를 돌리면 한 번의 재컷으로 끝난다.

사용:
  .venv\Scripts\python.exe tools\_drop_keep.py --out "F:\ja_reviewer_out\ja20" ^
      --code SNOS-373 --drop 35-46 [--dry]
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages


def parse_ranges(text):
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        out.append((float(a), float(b)))
    return sorted(out)


def subtract(keep, drops, min_clip):
    out = []
    for a, b in ((float(x), float(y)) for x, y in keep):
        pieces = [(a, b)]
        for da, db in drops:
            nxt = []
            for s, e in pieces:
                if db <= s or da >= e:          # 안 겹침
                    nxt.append((s, e)); continue
                if s < da:
                    nxt.append((s, min(da, e)))
                if e > db:
                    nxt.append((max(db, s), e))
            pieces = nxt
        out += [[round(s, 2), round(e, 2)] for s, e in pieces if e - s >= min_clip]
    return out


def main():
    ap = argparse.ArgumentParser(description="눈검사 확정 구간을 plan.keep 에서 제거")
    ap.add_argument("--out", required=True, help="배치 out_dir")
    ap.add_argument("--code", required=True)
    ap.add_argument("--drop", required=True, help="원본 좌표 구간, 예 35-46,120-131")
    ap.add_argument("--min-clip", type=float, default=4.0, help="남은 조각 최소 길이(초)")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    outdir = Path(args.out) / args.code
    plan_f = outdir / f"{args.code}_plan.json"
    if not plan_f.is_file():
        print(f"✗ plan 없음: {plan_f}")
        return 1
    plan = json.loads(plan_f.read_text(encoding="utf-8"))
    keep = plan.get("keep") or []
    drops = parse_ranges(args.drop)
    new = subtract(keep, drops, args.min_clip)

    old_t = sum(b - a for a, b in keep)
    new_t = sum(b - a for a, b in new)
    print(f"{args.code}: 제거 {drops}")
    print(f"  keep {len(keep)}→{len(new)}구간, {old_t:.0f}s→{new_t:.0f}s")
    for a, b in new:
        print(f"    {a:7.1f} ~ {b:7.1f}  ({b - a:.0f}s)")
    if not new:
        print("  ✗ 남는 구간이 없습니다 — 중단")
        return 1
    if args.dry:
        print("  [dry] 저장하지 않았습니다.")
        return 0

    bak = plan_f.with_suffix(".json.presafe")
    if not bak.is_file():
        bak.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    plan["keep"] = new
    plan_f.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    stages.worklog(outdir, args.code, f"눈검사 확정 구간 제거(모자이크 등): {args.drop}")
    print(f"  ✔ 저장. 이어서 _safecut --strict --carve-first 로 나머지를 도려내세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
