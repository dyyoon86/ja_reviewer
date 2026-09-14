# -*- coding: utf-8 -*-
"""im-not-ai 룰북 스냅샷을 설치본으로 갱신한다.

내레이션 검수(server/core/narreview.py)의 'AI문체' 판정은 im-not-ai
(humanize-korean) quick-rules 를 근거로 쓴다. 설치본이 있으면 그걸 직접 읽지만,
설치 안 된 머신(렌더 서버 등)에서도 같은 판정이 나와야 하므로 저장소에
`server/core/refs/quick-rules.md` 스냅샷을 둔다. 룰북이 올라가면 이걸로 맞춘다.

  설치: /plugin marketplace add epoko77-ai/im-not-ai
        /plugin install humanize-korean@im-not-ai
  갱신: .venv\\Scripts\\python.exe tools\\sync_humanize_rules.py
        .venv\\Scripts\\python.exe tools\\sync_humanize_rules.py --check   (차이만 확인)
"""
import argparse
import difflib
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server.core import humanize_kr

SNAP = Path(humanize_kr.__file__).with_name("refs") / "quick-rules.md"


def main():
    ap = argparse.ArgumentParser(description="im-not-ai 룰북 스냅샷 갱신")
    ap.add_argument("--check", action="store_true", help="쓰지 않고 차이만 출력")
    args = ap.parse_args()

    # 스냅샷 자신이 잡히면 비교할 게 없다 — 설치본을 따로 찾는다.
    src = next((p for p in humanize_kr._candidates()
                if p.is_file() and p.resolve() != SNAP.resolve()), None)
    if src is None:
        print("설치본을 못 찾았다. 플러그인을 먼저 설치해라:")
        print("  /plugin marketplace add epoko77-ai/im-not-ai")
        print("  /plugin install humanize-korean@im-not-ai")
        sys.exit(2)

    new = src.read_text(encoding="utf-8")
    cur = SNAP.read_text(encoding="utf-8") if SNAP.is_file() else ""
    print(f"설치본 : {src}")
    print(f"스냅샷 : {SNAP}")
    if new == cur:
        print("이미 최신이다.")
        return

    diff = list(difflib.unified_diff(cur.splitlines(), new.splitlines(),
                                     "snapshot", "installed", lineterm=""))
    print(f"차이 {sum(1 for d in diff if d[:1] in '+-' and d[:3] not in ('+++', '---'))}줄")
    for ln in diff[:60]:
        print(" ", ln)
    if args.check:
        sys.exit(10)

    SNAP.parent.mkdir(parents=True, exist_ok=True)
    SNAP.write_text(new, encoding="utf-8")
    ids = sorted(humanize_kr.rule_ids())
    print(f"\n갱신 완료 — 검수에 실리는 규칙 {len(ids)}개")


if __name__ == "__main__":
    main()
