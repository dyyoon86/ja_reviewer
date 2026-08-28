# -*- coding: utf-8 -*-
r"""ja20 — 인포카드/워터마크에서 3사이즈가 빠진 5편의 배우 데이터를 채운다.

**왜 빠졌나**(2026-08-28 규명, 원인이 셋 다 다르다):
  ① `works.actress_ja` 가 NULL — START-627·START-622·FNS-248.
     3사이즈·컵·키·일본어명은 `works.actress_ja` → `actresses.name_ja` **정확 일치** 한 줄에
     전부 걸려 있다. 이 고리가 없으면 넷이 동시에 빈다. 사진만 나오는 이유는 사진이
     `works.actress_photo` 에 따로 들어 있어서다. (전체 works 31,359건 중 26,432건이 NULL)
     ★FNS-248 은 `明里つむぎ` 행이 3사이즈까지 **이미 DB에 있었는데** 연결만 없어서 비었다.
  ② `fetch_measurements.py` 가 `meas_at IS NULL` 만 증분 처리한다 — 실패하면 meas_at 에
     `miss:name-mismatch(...)` 를 박고 **다시는 재시도하지 않는다**. 河北彩花（河北彩伽）가
     이 상태로 굳어 있었다. 부분 수집(星空ねる: W·컵·키만 있고 B·H 없음)도 meas_at 이
     찍혀 재시도 대상에서 빠진다. 같은 상태의 배우가 383명.
  ③ 배너 생성이 3사이즈가 비어도 **경고 없이 그냥 만든다** — 눈으로 볼 때까지 모른다.

수치 출처: minnano-av 프로필(우분투 Tor 경유, `fetch_measurements.parse_profile` 재사용).
  · 宮島めい / 明里つむぎ 는 works.actress_photo 의 `mnav_{id}` 로 프로필을 직접 지정해
    이름 검색 오매칭 여지를 없앴다.
  · 唯井まひろ 는 id 가 없어 이름으로 찾은 뒤 프로필 h2 로마자(`Tadai Mahiro`)가
    로컬 사진 파일명(`tadai_mahiro.jpg`)과 일치하는 것 + 얼굴 대조로 동일인 확인.

로컬 DB 와 우분투 DB **양쪽에 각각** 돌려야 한다(scp 로 덮으면 로컬 전용 배우행이 날아간다).
  로컬 : .venv\Scripts\python.exe tools\_fill_actress_ja20.py --db "E:\vscode\workspace\jav_scrap\jav_2026.db"
  우분투: python3 _fill_actress_ja20.py --db ~/jav_scrap/jav_2026.db
"""
import argparse
import sqlite3
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")

SRC = "minnano"
FETCHED = "2026-08-28"

# (품번, 한국어명, name_ja, B, W, H, 컵, 키, 생일, 사진경로)
RECORDS = [
    ("START-627", "미야지마 메이",  "宮島めい",             88, 59, 80, "F", 155, "2000-10-17",
     "images_actress/mnav_558772.jpg"),
    ("START-622", "타다이 마히로",  "唯井まひろ",           90, 58, 97, "F", 156, "2000-03-04",
     "images_actress/tadai_mahiro.jpg"),
    ("FNS-248",   "아카리 츠무기",  "明里つむぎ",           80, 58, 83, "B", 157, "1998-03-31",
     "images_actress/mnav_273627.jpg"),
    ("SNOS-371",  "카와키타 사이카", "河北彩花（河北彩伽）",  87, 57, 86, "E", 169, "1999-04-24",
     "images_actress/kawakita_saika.jpg"),
    ("SNOS-409",  "호시조라 네루",  "星空ねる",             90, 55, 85, "G", 170, "2004-03-10",
     "images_actress/hosizora_neru.jpg"),
]


def main():
    ap = argparse.ArgumentParser(description="ja20 배우 3사이즈 채우기 + works 연결")
    ap.add_argument("--db", required=True, help="jav_2026.db 경로")
    ap.add_argument("--dry", action="store_true", help="바뀔 내용만 출력")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    cols = {r[1] for r in con.execute("PRAGMA table_info(actresses)")}

    for code, ko, ja, b, w, h, cup, ht, bd, photo in RECORDS:
        cur = con.execute("SELECT * FROM actresses WHERE name_ja=?", (ja,)).fetchone()
        if cur:
            before = f"B{cur['bust']} W{cur['waist']} H{cur['hip']} {cur['cup']} {cur['height']}"
            action = "UPDATE"
        else:
            before, action = "(행 없음)", "INSERT"
        print(f"{code:<11} {ja:<12} {action}  {before}  →  B{b} W{w} H{h} {cup} {ht}")

        if not args.dry:
            if cur:
                con.execute(
                    "UPDATE actresses SET bust=?, waist=?, hip=?, cup=?, height=?, birthday=?,"
                    " photo_path=COALESCE(NULLIF(photo_path,''),?), meas_src=?, meas_at=?"
                    " WHERE name_ja=?",
                    (b, w, h, cup, ht, bd, photo, SRC, ts, ja))
            else:
                fields = ["name_ja", "bust", "waist", "hip", "cup", "height", "birthday",
                          "photo_path", "meas_src", "meas_at"]
                vals = [ja, b, w, h, cup, ht, bd, photo, SRC, ts]
                if "id" in cols:                       # 수동 보충 행임을 남긴다
                    fields.insert(0, "id"); vals.insert(0, f"manual:{ja}")
                con.execute(f"INSERT INTO actresses ({','.join(fields)}) "
                            f"VALUES ({','.join('?' * len(vals))})", vals)

        # works 연결 — 이게 없으면 위 데이터가 있어도 배너에서 못 쓴다
        wrow = con.execute("SELECT code, actress_ja FROM works WHERE code=?", (code,)).fetchone()
        if not wrow:
            print(f"            · works 행 없음(신작, meta_api 쪽에만 존재) — 연결 생략")
        elif wrow["actress_ja"] == ja:
            print(f"            · works.actress_ja 이미 연결됨")
        else:
            print(f"            · works.actress_ja {wrow['actress_ja']!r} → {ja!r}")
            if not args.dry:
                con.execute("UPDATE works SET actress_ja=? WHERE code=?", (ja, code))

    if args.dry:
        print("\n[dry] 아무것도 쓰지 않았습니다.")
    else:
        con.commit()
        print("\n커밋 완료. 확인:")
        for code, ko, ja, *_ in RECORDS:
            r = con.execute("SELECT bust,waist,hip,cup,height FROM actresses WHERE name_ja=?",
                            (ja,)).fetchone()
            print(f"  {code:<11} {ja:<12} B{r['bust']}·W{r['waist']}·H{r['hip']} "
                  f"{r['cup']}컵 {r['height']}cm")
    con.close()


if __name__ == "__main__":
    main()
