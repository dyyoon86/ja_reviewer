# -*- coding: utf-8 -*-
"""배너(인포카드+워터마크) 재디자인 시안 렌더.

현재 문제(사용자 지적 2026-07-24 "중구난방·인덴트/장평/자간 안 맞음"):
  · 인포카드 타이틀(브러시체)의 좌측 시작점이 아래 배우명·알약 줄과 어긋남
    (.title은 margin-left 0, .actress/.meta는 6px + 브러시 폰트 좌측 side bearing)
  · 폰트 3종(Nanum Brush / Jua / 컬러 이모지)이 섞여 자간·글자높이가 제각각
  · 알약마다 이모지 유무로 높이·baseline이 흔들림
  · 품번이 타이틀·알약에, 배우명이 타이틀·본문에 중복 노출(3중 중복)
  · 워터마크 3줄이 각각 다른 폰트크기(38/29/24)·패딩·테두리 → 오른쪽 끝이 들쭉날쭉

시안:
  A안 = 현행 정돈. 브러시 타이틀 유지 + 좌측 광학정렬·8px 리듬·알약 규격 통일·중복 제거
  B안 = 모던 카드. 굵은 산세리프 타이틀 + 좌측 액센트 바, 워터마크는 단일 패널로 통합

사용: .venv\\Scripts\\python.exe tools\\_banner_redesign.py [품번]
출력: {out_dir}/_배너시안/{품번}_{A|B}_{info|wm}.jpg
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
DEST = OUT / "_배너시안"
JAVDB = Path(r"E:\vscode\workspace\jav_scrap\jav_2026.db")

FONTS = ("@import url('https://fonts.googleapis.com/css2?"
         "family=Nanum+Brush+Script&family=Jua&family=Black+Han+Sans&display=swap');")

# 딸기 레드 팔레트(gen_infocard.extract_theme 기본값과 동일 계열)
T = {"c1": "#ff2d55", "c2": "#c9184a", "accent": "#ffd23f",
     "frame": "#ff9ad1,#ff2d55,#ff9ad1"}


def meta_from_html(code: str) -> dict:
    """meta_api가 죽어 있어도 시안을 볼 수 있게, 이미 만든 배너 HTML에서 값 복원."""
    icdir = OUT / f"_infocard_{code}"
    hi = (icdir / "_L_info.html").read_text(encoding="utf-8")
    hw = (icdir / "_L_wm.html").read_text(encoding="utf-8")
    m = {"code": code}
    t = re.search(r'<div class=title>(.*?)</div>', hi, re.S)
    m["title"] = re.sub(r"<[^>]+>", "", t.group(1)).strip() if t else code
    a = re.search(r'<div class=actress>(.*?)</div>', hi, re.S)
    if a:
        inner = a.group(1)
        ja = re.search(r'<span class="?ja"?>([^<]+)</span>', inner)
        m["actress_ja"] = ja.group(1).strip() if ja else ""
        m["actress"] = re.sub(r"<[^>]+>", "", inner.replace(m["actress_ja"], "")).strip()
    else:
        m["actress"] = code
        m["actress_ja"] = ""
    pills = re.findall(r'<span class="pill[^"]*">(.*?)</span>', hi, re.S)
    flat = [re.sub(r"<[^>]+>", "", p).strip() for p in pills]
    m["label"] = ""
    for p in flat:
        if "·" in p and code in p:
            m["label"] = p.split("·")[0].strip()
    m["release"] = next((re.sub(r"[^0-9.]", "", p) for p in flat if re.search(r"\d{4}\.\d\d", p)), "")
    m["star"] = next((re.sub(r"[^0-9.]", "", p) for p in flat if "★" in p), "")
    m["views"] = next((p.split()[-1] for p in flat if "👁" in p), "")
    m["like_pct"] = next((re.sub(r"[^0-9]", "", p) for p in flat if "👍" in p), "")
    sz = re.search(r'<span class=sz>(.*?)</span>', hw, re.S)
    m["size"] = re.sub(r"<[^>]+>", "", sz.group(1)).strip() if sz else ""
    # 배우 사진(로컬 DB)
    m["photo"] = ""
    if m["actress_ja"]:
        db = sqlite3.connect(JAVDB); db.row_factory = sqlite3.Row
        r = db.execute("SELECT photo_path FROM actresses WHERE name_ja=?", (m["actress_ja"],)).fetchone()
        db.close()
        if r and r["photo_path"]:
            p = r["photo_path"]
            full = p if os.path.isabs(p) else os.path.join(JAVDB.parent, p)
            if os.path.exists(full):
                m["photo"] = full
    return m


def _face_css(m, size, radius):
    if m["photo"]:
        b64 = base64.b64encode(Path(m["photo"]).read_bytes()).decode()
        bg = f"url(data:image/jpeg;base64,{b64});background-size:cover"
    else:
        bg = f"linear-gradient(135deg,{T['c1']},{T['c2']})"
    return (f"width:{size}px;height:{size}px;border-radius:{radius}px;background-position:center top;"
            f"background-image:{bg};flex:0 0 auto")


def _mascot():
    p = Path(__file__).resolve().parent.parent / "server" / "assets" / "mascot.png"
    return base64.b64encode(p.read_bytes()).decode() if p.is_file() else ""


def _logo_css():
    """좌상단 채널 로고 — 인포카드 레이어에 원래 있던 요소(시안에서도 유지)."""
    return f""".logo{{position:absolute;top:48px;left:56px;display:flex;align-items:center;gap:12px;
 background:linear-gradient(135deg,{T['c1']},{T['c2']});padding:10px 22px 10px 12px;border-radius:18px;
 box-shadow:0 8px 26px rgba(0,0,0,.4);transform:rotate(-2deg);border:2px solid rgba(255,255,255,.25)}}
.logo img{{width:64px;height:64px;object-fit:contain}}
.logo .t{{color:#fff;font-size:30px;letter-spacing:-.01em}}"""


def _logo_html():
    mb = _mascot()
    img = f'<img src="data:image/png;base64,{mb}">' if mb else ""
    return f'<div class=logo>{img}<span class=t>딸딸기튜브</span></div>'


# ─────────────────────────── A안: 현행 정돈 ───────────────────────────
def info_A(m):
    """브러시 타이틀 유지. 좌측 광학정렬 + 8px 리듬 + 알약 규격 통일 + 중복 제거."""
    pills = []
    if m["label"]:    pills.append(("key", m["label"]))
    if m["size"]:     pills.append(("", m["size"]))
    if m["release"]:  pills.append(("", f'출시 {m["release"]}'))
    if m["star"]:     pills.append(("key", f'★ {m["star"]}'))
    if m["views"]:    pills.append(("", f'조회 {m["views"]}'))
    if m["like_pct"]: pills.append(("", f'좋아요 {m["like_pct"]}%'))
    ph = "".join(f'<span class="pill {k}">{v}</span>' for k, v in pills)
    aja = f'<span class=ja>{m["actress_ja"]}</span>' if m["actress_ja"] else ""
    return f"""<!doctype html><meta charset=utf-8><style>{FONTS}
*{{margin:0;box-sizing:border-box}}html,body{{background:transparent}}
.f{{width:1920px;height:1080px;position:relative;background:transparent;
 font-family:'Jua','Malgun Gothic',sans-serif}}
.lower{{position:absolute;left:76px;bottom:84px;background:rgba(12,7,11,.66);
 border-radius:24px;padding:32px 40px;border:1.5px solid rgba(255,255,255,.14);
 box-shadow:0 10px 40px rgba(0,0,0,.45)}}
/* 타이틀: 브러시체 좌측 side bearing(≈0.06em) 보정해 아래 줄과 광학 정렬 */
.title{{font-family:'Nanum Brush Script';font-size:128px;line-height:1;
 margin-left:-.06em;letter-spacing:.01em;
 background:linear-gradient(180deg,#fff 62%,{T['accent']});
 -webkit-background-clip:text;-webkit-text-fill-color:transparent;
 filter:drop-shadow(0 5px 18px rgba(0,0,0,.65))}}
.actress{{font-size:42px;color:{T['accent']};margin-top:16px;letter-spacing:-.01em;
 display:flex;align-items:baseline;gap:14px;text-shadow:0 3px 12px rgba(0,0,0,.7)}}
.actress .ja{{color:#d8d8e0;font-size:22px;letter-spacing:0}}
.meta{{display:flex;gap:10px;margin-top:24px;flex-wrap:wrap;max-width:1500px}}
/* 알약: 높이·반경·패딩·글자크기 전부 동일. 색만 다름 */
.pill{{height:42px;display:inline-flex;align-items:center;padding:0 18px;border-radius:21px;
 font-size:24px;line-height:1;letter-spacing:-.01em;color:#fff;
 background:rgba(255,255,255,.13);border:1.5px solid rgba(255,255,255,.4)}}
.pill.key{{background:linear-gradient(135deg,{T['c1']},{T['c2']});
 border-color:rgba(255,255,255,.35);box-shadow:0 4px 14px rgba(0,0,0,.35)}}
{_logo_css()}
</style><div class=f>{_logo_html()}<div class=lower>
 <div class=title>{m["code"]}</div>
 <div class=actress>{m["actress"]}{aja}</div>
 <div class=meta>{ph}</div>
</div></div>"""


def wm_A(m):
    """3줄 바 유지하되 글자크기·패딩·높이·반경 통일, 이모지 제거."""
    r3 = " · ".join(x for x in [m["release"], f'★ {m["star"]}' if m["star"] else ""] if x)
    return f"""<!doctype html><meta charset=utf-8><style>{FONTS}
*{{margin:0;box-sizing:border-box}}html,body{{background:transparent}}
.f{{width:1920px;height:1080px;position:relative;background:transparent;
 font-family:'Jua','Malgun Gothic',sans-serif}}
.wrap{{position:absolute;top:44px;left:48px;display:flex;align-items:center;gap:16px;
 filter:drop-shadow(0 6px 18px rgba(0,0,0,.5))}}
.face{{{_face_css(m,132,16)};border:3px solid #fff;box-shadow:0 4px 12px rgba(0,0,0,.45)}}
.rows{{display:flex;flex-direction:column;gap:8px;align-items:flex-start}}
/* 세 줄 모두 같은 높이·반경·좌우 패딩 — 색과 글자만 다름 */
.row{{height:42px;display:inline-flex;align-items:center;padding:0 18px;border-radius:12px;
 font-size:27px;line-height:1;letter-spacing:-.01em;white-space:nowrap;border:1.5px solid transparent}}
.r1{{background:linear-gradient(135deg,{T['c2']},{T['c1']});color:#fff;
 border-color:rgba(255,255,255,.4);letter-spacing:.02em}}
.r2{{background:rgba(255,255,255,.95);color:{T['c2']}}}
.r3{{background:rgba(20,10,14,.74);color:{T['accent']};border-color:{T['accent']}66}}
</style><div class=f><div class=wrap>
 <div class=face></div>
 <div class=rows>
  <div class="row r1">{m["code"]}</div>
  <div class="row r2">{m["actress"]}</div>
  <div class="row r3">{r3}</div>
 </div></div></div>"""


# ─────────────────────────── B안: 모던 카드 ───────────────────────────
def info_B(m):
    """굵은 산세리프 + 좌측 액센트 바. 정보는 한 줄 메타로 통합."""
    bits = [x for x in [m["label"], m["size"], m["release"],
                        f'★ {m["star"]}' if m["star"] else "",
                        f'좋아요 {m["like_pct"]}%' if m["like_pct"] else ""] if x]
    meta = '<span class=dot>·</span>'.join(f"<span>{b}</span>" for b in bits)
    aja = f'<span class=ja>{m["actress_ja"]}</span>' if m["actress_ja"] else ""
    return f"""<!doctype html><meta charset=utf-8><style>{FONTS}
*{{margin:0;box-sizing:border-box}}html,body{{background:transparent}}
.f{{width:1920px;height:1080px;position:relative;background:transparent;
 font-family:'Jua','Malgun Gothic',sans-serif}}
.card{{position:absolute;left:76px;bottom:84px;display:flex;align-items:stretch;
 background:rgba(10,6,10,.72);border-radius:20px;overflow:hidden;
 border:1.5px solid rgba(255,255,255,.13);box-shadow:0 12px 44px rgba(0,0,0,.5)}}
.bar{{width:10px;background:linear-gradient(180deg,{T['c1']},{T['c2']});flex:0 0 auto}}
.body{{padding:30px 40px 30px 34px}}
.code{{font-family:'Black Han Sans','Jua',sans-serif;font-size:96px;line-height:1;
 letter-spacing:.01em;color:#fff;text-shadow:0 4px 18px rgba(0,0,0,.6)}}
.name{{font-size:46px;color:{T['accent']};margin-top:14px;letter-spacing:-.01em;
 display:flex;align-items:baseline;gap:14px}}
.name .ja{{color:#cfcfd8;font-size:24px}}
.meta{{margin-top:22px;font-size:26px;color:#e6e6ee;letter-spacing:-.01em;
 display:flex;align-items:center;gap:12px}}
.meta .dot{{color:{T['c1']}}}
{_logo_css()}
</style><div class=f>{_logo_html()}<div class=card>
 <div class=bar></div>
 <div class=body>
  <div class=code>{m["code"]}</div>
  <div class=name>{m["actress"]}{aja}</div>
  <div class=meta>{meta}</div>
 </div></div></div>"""


def wm_B(m):
    """떠다니는 3개 바 → 얼굴+3줄을 담은 단일 패널."""
    r3 = " · ".join(x for x in [m["release"], f'★ {m["star"]}' if m["star"] else ""] if x)
    return f"""<!doctype html><meta charset=utf-8><style>{FONTS}
*{{margin:0;box-sizing:border-box}}html,body{{background:transparent}}
.f{{width:1920px;height:1080px;position:relative;background:transparent;
 font-family:'Jua','Malgun Gothic',sans-serif}}
.panel{{position:absolute;top:44px;left:48px;display:flex;align-items:center;gap:16px;
 background:rgba(10,6,10,.74);border:1.5px solid rgba(255,255,255,.16);
 border-radius:18px;padding:14px 22px 14px 14px;
 box-shadow:0 8px 26px rgba(0,0,0,.5)}}
.face{{{_face_css(m,116,12)};box-shadow:0 2px 8px rgba(0,0,0,.5)}}
.col{{display:flex;flex-direction:column;gap:7px;align-items:flex-start}}
.code{{font-family:'Black Han Sans','Jua',sans-serif;font-size:38px;line-height:1;color:#fff;letter-spacing:.01em}}
.name{{font-size:27px;line-height:1;color:{T['accent']};letter-spacing:-.01em}}
.sub{{font-size:21px;line-height:1;color:#b9b9c4;letter-spacing:-.01em}}
</style><div class=f><div class=panel>
 <div class=face></div>
 <div class=col>
  <div class=code>{m["code"]}</div>
  <div class=name>{m["actress"]}</div>
  <div class=sub>{r3}</div>
 </div></div></div>"""


def main():
    code = sys.argv[1] if len(sys.argv) > 1 else "PRED-879"
    m = meta_from_html(code)
    print(f"{code} 메타: {m['actress']}({m['actress_ja']}) / {m['label']} / "
          f"{m['release']} / ★{m['star']} / 사진 {'있음' if m['photo'] else '없음'}")
    DEST.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright
    from PIL import Image
    pages = {"A_info": info_A(m), "A_wm": wm_A(m), "B_info": info_B(m), "B_wm": wm_B(m)}
    with sync_playwright() as p:
        b = p.chromium.launch()
        for name, html in pages.items():
            f = DEST / f"_{code}_{name}.html"
            f.write_text(html, encoding="utf-8")
            pg = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
            pg.goto("file://" + str(f)); pg.wait_for_timeout(1500)
            png = DEST / f"{code}_{name}.png"
            pg.screenshot(path=str(png), omit_background=True); pg.close()
            im = Image.open(png).convert("RGBA")
            bg = Image.new("RGBA", im.size, (30, 30, 36, 255)); bg.alpha_composite(im)
            bb = im.getbbox()
            if bb:
                pad = 40
                box = (max(0, bb[0]-pad), max(0, bb[1]-pad),
                       min(im.width, bb[2]+pad), min(im.height, bb[3]+pad))
                bg.crop(box).convert("RGB").save(DEST / f"{code}_{name}.jpg", quality=92)
            print(f"  ✔ {name}")
        b.close()
    print("→", DEST)


if __name__ == "__main__":
    main()
