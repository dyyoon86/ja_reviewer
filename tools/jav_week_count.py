# -*- coding: utf-8 -*-
r"""이번 회차가 훑은 신작이 몇 편인지 — 수집 DB에서 실측한다.

아웃트로에서 "매주 N편을 훑어서 열두 편만 골랐다"고 말하려면 그 N이 진짜여야 한다.
(초안에서 인트로 기본값 500을 그대로 쓸 뻔했다 — 그건 근거 없는 숫자다.)

**회차 범위** = 수집일 기준 **지난주 화요일 ~ 이번주 월요일** (7일).
  기준일이 금요일(2026-09-11)이면 → 이번주 월요일 09-07, 지난주 화요일 09-01.

**수집일 = `works.scraped_at`**. 이 컬럼이 갱신 시각이 아니라 **최초 삽입 시각**인 것은
id(autoincrement) 범위가 날짜별로 겹치지 않고 연속인 것으로 확인했다(2026-09-11 실측).
따라서 날짜별 건수 = 그날 새로 들어온 작품 수다.

DB는 우분투(172.30.1.40)의 `~/jav_scrap/jav_2026.db` 가 원본이다. 윈도우 로컬 사본은
낡아 있을 수 있어 **ssh 를 먼저** 보고, 안 되면 로컬로 떨어진다.

사용:
    python tools\jav_week_count.py                 # 오늘 기준
    python tools\jav_week_count.py --date 2026-09-11
    python tools\jav_week_count.py --json          # {"pool":333,"span":"9월 1일 ~ 9월 7일",...}
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SSH_HOST = "dyyoon@172.30.1.40"
REMOTE_DB = "/home/dyyoon/jav_scrap/jav_2026.db"
LOCAL_DB = ROOT / "jav_2026.db"

SQL = ("select count(*) from works "
       "where substr(scraped_at,1,10) between '{a}' and '{b}'")


def week_span(ref):
    """수집일 기준 회차 범위 — (지난주 화요일, 이번주 월요일)."""
    monday = ref - dt.timedelta(days=ref.weekday())     # 이번주 월요일
    return monday - dt.timedelta(days=6), monday        # 지난주 화요일 ~ 이번주 월요일


def count_remote(a, b):
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", SSH_HOST,
           "python3 -c \"import sqlite3;print(sqlite3.connect('%s')"
           ".execute(\\\"%s\\\").fetchone()[0])\"" % (REMOTE_DB, SQL.format(a=a, b=b))]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def count_local(a, b):
    import sqlite3
    if not LOCAL_DB.is_file():
        return None
    try:
        return sqlite3.connect(str(LOCAL_DB)).execute(SQL.format(a=a, b=b)).fetchone()[0]
    except Exception:
        return None


def measure(ref):
    a, b = week_span(ref)
    a_s, b_s = a.isoformat(), b.isoformat()
    n, src = count_remote(a_s, b_s), "우분투 서버"
    if n is None:
        n, src = count_local(a_s, b_s), "로컬 사본(낡았을 수 있음)"
    if n is None:
        raise RuntimeError("수집 DB를 읽을 수 없습니다 — ssh 와 로컬 사본 둘 다 실패")
    return {"pool": n, "from": a_s, "to": b_s, "source": src,
            "span": f"{a.month}월 {a.day}일 ~ {b.month}월 {b.day}일"}


def main():
    ap = argparse.ArgumentParser(description="회차 범위 수집량 실측")
    ap.add_argument("--date", help="기준일 YYYY-MM-DD (생략 시 오늘)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    ref = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    m = measure(ref)
    if args.json:
        print(json.dumps(m, ensure_ascii=False))
    else:
        print(f"회차 범위: {m['from']}(화) ~ {m['to']}(월)")
        print(f"수집 신작: {m['pool']}편   [{m['source']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
