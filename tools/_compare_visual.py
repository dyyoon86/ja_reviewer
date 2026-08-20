# -*- coding: utf-8 -*-
"""시각정보 有/無 섹션2 결과 비교 — {code}_plan.json(신, 시각有) vs {code}_plan.json.novis(구, 시각無).
내레이션·대사·summary를 나란히 출력한다.
사용: .venv\\Scripts\\python.exe tools\\_compare_visual.py [CODE ...]  (생략 시 전체 11편)
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, "tools")
import _common  # noqa
from server import stages

BASE = Path(r"C:\Users\yoon\ja_reviewer_out\ja13")
CODES = "MIZD-531 EBWH-342 DANDYA-043 HMN-880 START-614 MFYD-165 PRED-886 PRWF-014 EBWH-348 START-600 PRED-879".split()


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8")) if Path(p).is_file() else None


def nar_lines(plan):
    return [n.get("text", "") for n in plan.get("narration", [])]


def main():
    codes = sys.argv[1:] or CODES
    for c in codes:
        d = BASE / c
        new = load(d / f"{c}_plan.json")
        old = load(d / f"{c}_plan.json.novis")
        print("\n" + "=" * 78)
        print(f"◆ {c}")
        print("=" * 78)
        if not new or not old:
            print(f"  (비교 불가 — new={bool(new)} old={bool(old)})")
            continue
        print(f"summary  [구] {old.get('summary','')[:70]}")
        print(f"         [신] {new.get('summary','')[:70]}")
        print(f"stars    구 {old.get('stars')} → 신 {new.get('stars')}  |  "
              f"내레이션 구 {len(old.get('narration',[]))} → 신 {len(new.get('narration',[]))}  |  "
              f"대사 구 {len(old.get('dialogue',[]))} → 신 {len(new.get('dialogue',[]))}")
        on, nn = nar_lines(old), nar_lines(new)
        print("\n  ── 내레이션 비교 (구 → 신) ──")
        for i in range(max(len(on), len(nn))):
            o = on[i] if i < len(on) else "—"
            n = nn[i] if i < len(nn) else "—"
            print(f"  [{i+1:2}] 구 {o}")
            print(f"       신 {n}")


if __name__ == "__main__":
    main()
