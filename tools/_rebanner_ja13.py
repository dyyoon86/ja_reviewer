# -*- coding: utf-8 -*-
"""ja13 11편 배너를 새 디자인(B안)으로 재생성.

우분투 meta_api가 죽어 있어도 되도록, 이미 만든 배너 HTML에서 메타를 복원해
gen_infocard.render_layers()에 그대로 넘긴다(사진은 로컬 jav_scrap DB에서 조회).

사용: .venv\\Scripts\\python.exe tools\\_rebanner_ja13.py [품번...]
"""
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gen_infocard as GIC  # noqa: E402

OUT = Path(r"F:\ja_reviewer_out\ja13")
JAVDB = Path(r"E:\vscode\workspace\jav_scrap\jav_2026.db")

CODES = ["MIZD-531", "EBWH-342", "DANDYA-043", "HMN-880", "START-614", "MFYD-165",
         "PRED-886", "PRWF-014", "EBWH-348", "START-600", "PRED-879"]


def meta_from_html(code: str) -> dict:
    icdir = OUT / f"_infocard_{code}"
    hi = (icdir / "_L_info.html").read_text(encoding="utf-8")
    hw = (icdir / "_L_wm.html").read_text(encoding="utf-8")
    m = {"code": code, "views": "", "like_pct": "", "bust": "", "waist": "", "hip": "",
         "cup": "", "height": "", "thumb": "", "photo": ""}

    t = re.search(r'<div class=title>(.*?)</div>', hi, re.S)
    m["title"] = re.sub(r"<[^>]+>", "", t.group(1)).strip() if t else code
    a = re.search(r'<div class=(?:actress|name)>(.*?)</div>', hi, re.S)
    if a:
        inner = a.group(1)
        ja = re.search(r'<span class="?ja"?>([^<]+)</span>', inner)
        m["actress_ja"] = ja.group(1).strip() if ja else ""
        m["actress"] = re.sub(r"<[^>]+>", "", inner.replace(m["actress_ja"], "")).strip()
    else:
        m["actress"], m["actress_ja"] = code, ""

    pills = re.findall(r'<span class="pill[^"]*">(.*?)</span>', hi, re.S)
    flat = [re.sub(r"<[^>]+>", "", p).strip() for p in pills]
    if not flat:      # 이미 새 디자인(메타 한 줄)으로 만들어진 경우
        mm = re.search(r'<div class=meta>(.*?)</div>', hi, re.S)
        if mm:
            flat = [x.strip() for x in re.sub(r"<[^>]+>", "|", mm.group(1)).split("|") if x.strip()]

    m["label"] = ""
    for p in flat:
        if code in p and "·" in p:
            m["label"] = p.split("·")[0].strip()
    if not m["label"] and flat:
        first = flat[0]
        if not re.search(r"\d", first):
            m["label"] = first
    m["release"] = next((re.sub(r"[^0-9.]", "", p) for p in flat
                         if re.search(r"\d{4}\.\d\d", p)), "")
    m["star"] = next((re.sub(r"[^0-9.]", "", p) for p in flat if "★" in p), "")
    m["views"] = next((p.split()[-1] for p in flat if "👁" in p or p.startswith("조회")), "")
    m["like_pct"] = next((re.sub(r"[^0-9]", "", p) for p in flat
                          if "👍" in p or p.startswith("좋아요")), "")
    sz = re.search(r'B(\d+)[^\d]+W(\d+)[^\d]+H(\d+)', hw + hi)
    if sz:
        m["bust"], m["waist"], m["hip"] = sz.groups()

    if m["actress_ja"]:
        db = sqlite3.connect(JAVDB); db.row_factory = sqlite3.Row
        r = db.execute("SELECT photo_path FROM actresses WHERE name_ja=?",
                       (m["actress_ja"],)).fetchone()
        db.close()
        if r and r["photo_path"]:
            p = r["photo_path"]
            full = p if os.path.isabs(p) else os.path.join(JAVDB.parent, p)
            if os.path.exists(full):
                m["photo"] = full
    return m


def main():
    want = sys.argv[1:] or CODES
    for code in want:
        icdir = OUT / f"_infocard_{code}"
        m = meta_from_html(code)
        print(f"\n{code}: {m['actress']}({m['actress_ja']}) / {m['label']} / "
              f"{m['release']} / ★{m['star']} / 사진 {'O' if m['photo'] else 'X'}")
        layers = GIC.render_layers(m, str(icdir))
        for k, fname in (("frame", f"{code}_프레임.png"),
                         ("info",  f"{code}_인포카드.png"),
                         ("wm",    f"{code}_워터마크.png")):
            shutil.copyfile(layers[k], icdir / fname)
        tr = icdir / f"{code}_워터마크_tr.png"      # 1080p 재배치본 무효화
        if tr.is_file():
            tr.unlink()
        print(f"  ✔ 배너 3종 갱신")


if __name__ == "__main__":
    main()
