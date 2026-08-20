# -*- coding: utf-8 -*-
"""워터마크의 배우 얼굴 슬롯이 딸기 마스코트로 대체된 것 복구.

원인: gen_infocard가 로컬 DB에 없는 신작이면 우분투 meta_api로 폴백하는데,
      그 경로는 배우 사진(우분투 로컬 파일)을 못 받아 마스코트로 떨어진다.
      로컬 jav_scrap DB에는 사진이 있는 배우가 많은데도 안 붙었다.

여기서는 우분투가 죽어 있어도 되도록, 이미 만들어진 `_L_wm.html`의 face 배경만
로컬 사진으로 갈아끼우고 playwright로 다시 렌더한다(메타 재조회 불필요).

사용: .venv\\Scripts\\python.exe tools\\_fix_wm_face.py
"""
import base64
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa

OUT = Path(r"C:\Users\yoon\ja_reviewer_out\ja13")
JAVDB = Path(r"E:\vscode\workspace\jav_scrap\jav_2026.db")
JAVROOT = JAVDB.parent

CODES = ["MIZD-531", "EBWH-342", "DANDYA-043", "HMN-880", "START-614", "MFYD-165",
         "PRED-886", "PRWF-014", "EBWH-348", "START-600", "PRED-879"]

FACE_RE = re.compile(r'(<div class=face style="background-image:url\()([^)]*)(\)[^"]*")')


def actress_ja(icdir: Path) -> str:
    """인포카드 HTML의 <div class=actress>한글 이름 <span class=ja>일본어</span></div>"""
    h = (icdir / "_L_info.html").read_text(encoding="utf-8")
    m = re.search(r'<div class=actress>(.{0,200}?)</div>', h, re.S)
    if not m:
        return ""
    m2 = re.search(r'<span class="?ja"?>([^<]+)</span>', m.group(1))
    return m2.group(1).strip() if m2 else ""


def photo_for(name_ja: str) -> str:
    if not name_ja:
        return ""
    db = sqlite3.connect(JAVDB)
    db.row_factory = sqlite3.Row
    r = db.execute("SELECT photo_path FROM actresses WHERE name_ja=?", (name_ja,)).fetchone()
    db.close()
    if not r or not r["photo_path"]:
        return ""
    p = r["photo_path"]
    full = p if os.path.isabs(p) else os.path.join(JAVROOT, p)
    return full if os.path.exists(full) else ""


def main():
    from playwright.sync_api import sync_playwright
    todo, skipped = [], []
    for code in CODES:
        icdir = OUT / f"_infocard_{code}"
        ja = actress_ja(icdir)
        ph = photo_for(ja)
        if ph:
            todo.append((code, icdir, ja, ph))
        else:
            skipped.append((code, ja or "(일본어 이름 없음)"))

    print(f"사진 적용 {len(todo)}편 / 건너뜀 {len(skipped)}편")
    for c, ja in skipped:
        print(f"  – {c}: {ja} — 로컬 DB에 사진 없음, 마스코트 유지")

    with sync_playwright() as p:
        b = p.chromium.launch()
        for code, icdir, ja, ph in todo:
            f = icdir / "_L_wm.html"
            h = f.read_text(encoding="utf-8")
            ext = "jpeg" if ph.lower().endswith((".jpg", ".jpeg")) else "png"
            b64 = base64.b64encode(Path(ph).read_bytes()).decode()
            # 마스코트 폴백은 background-size:70%/배경색이 붙어 있다 → 사진용으로 통째 교체
            new, n = FACE_RE.subn(
                lambda m: f'{m.group(1)}data:image/{ext};base64,{b64})"', h)
            if not n:
                print(f"  ✘ {code}: face 슬롯을 못 찾음")
                continue
            f.write_text(new, encoding="utf-8")

            pg = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
            pg.goto("file://" + str(f))
            pg.wait_for_timeout(1300)
            png = icdir / "L_wm.png"
            pg.screenshot(path=str(png), omit_background=True)
            pg.close()
            # gen_infocard.generate()가 쓰는 이름으로 복사 + 재배치본 무효화
            import shutil
            shutil.copyfile(png, icdir / f"{code}_워터마크.png")
            tr = icdir / f"{code}_워터마크_tr.png"
            if tr.is_file():
                tr.unlink()
            print(f"  ✔ {code}: {ja} 사진 적용 ({os.path.basename(ph)})")
        b.close()


if __name__ == "__main__":
    main()
