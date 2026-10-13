# -*- coding: utf-8 -*-
"""claude -p 사용량 집계 — server/core/llm.run_claude 가 `_로그/llm_usage.jsonl` 에 남긴 기록을
날짜·태그별로 합산한다. 주간 한도(Max 플랜)에서 파이프라인이 얼마나 먹는지 보는 용도.

사용:
  .venv\\Scripts\\python.exe tools\\llm_usage.py            # 오늘
  .venv\\Scripts\\python.exe tools\\llm_usage.py --days 7   # 최근 7일
  .venv\\Scripts\\python.exe tools\\llm_usage.py --all

환산 $ 는 Opus급 공개 단가(입력 15 / 캐시쓰기 18.75 / 캐시읽기 1.5 / 출력 75 $/M) 기준 참고치.
"""
import argparse
import collections
import datetime
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
LOG = Path(__file__).resolve().parent.parent / "_로그" / "llm_usage.jsonl"
PRICE = {"in": 15, "cw": 18.75, "cr": 1.5, "out": 75}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    if not LOG.is_file():
        print(f"기록 없음: {LOG}")
        return
    since = None if a.all else (datetime.datetime.now() - datetime.timedelta(days=a.days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    by_tag = collections.defaultdict(collections.Counter)
    by_day = collections.defaultdict(collections.Counter)
    for line in LOG.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = datetime.datetime.fromisoformat(r["ts"])
        if since and ts < since:
            continue
        for g in (by_tag[r.get("tag") or "?"], by_day[ts.strftime("%m-%d")]):
            g["n"] += 1
            for k in PRICE:
                g[k] += int(r.get(k, 0))
            g["secs"] += float(r.get("secs", 0))

    def row(name, g):
        usd = sum(g[k] * PRICE[k] / 1e6 for k in PRICE)
        print(f"{name:16} {g['n']:4}회  출력 {g['out']:>9,}  캐시쓰기 {g['cw']:>10,}  캐시읽기 {g['cr']:>11,}"
              f"  {g['secs']/60:5.1f}분  ≈${usd:6.2f}")
        return usd

    print(f"=== 태그별 ({'전체' if a.all else '최근 %d일' % a.days}) ===")
    tot = sum(row(k, g) for k, g in sorted(by_tag.items(), key=lambda x: -x[1]["out"]))
    print(f"\n=== 날짜별 ===")
    for k, g in sorted(by_day.items()):
        row(k, g)
    print(f"\n합계 ≈ ${tot:,.2f} (API 환산 참고치)")


if __name__ == "__main__":
    main()
