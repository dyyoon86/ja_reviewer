#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_infocard.py — 품번(code) 하나로 딸딸기튜브 인포배너를 자동 생성.

DB(jav_2026.db) 조회 → 인포카드 / 워터마크 / 액자프레임 오버레이 PNG 생성
→ (옵션) 입력 영상에 ffmpeg 오버레이:
    · 프레임: 상시
    · 인포카드: 앞 N초 등장 후 페이드아웃
    · 워터마크(좌상단 코너로고): 이후 상시

사용:
  # 오버레이 PNG + 데모 mp4(어두운 배경)
  python gen_infocard.py SNOS-152

  # 실제 영상에 오버레이
  python gen_infocard.py SNOS-152 --video clip.mp4 --out clip_banner.mp4

  # 인포카드 노출시간 조정
  python gen_infocard.py SNOS-152 --video clip.mp4 --hold 2.0
"""
import argparse, base64, colorsys, json, os, re, shutil, sqlite3, subprocess, sys, tempfile

HERE   = os.path.dirname(os.path.abspath(__file__))
DB     = os.environ.get("JAV_DB", "/home/dyyoon/jav_scrap/jav_2026.db")
MASCOT = os.path.join(HERE, "server", "assets", "mascot.png")
CHROME = os.environ.get(
    "PW_CHROME",
    "/home/dyyoon/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",
)

# ────────────────────────────── DB 조회 ──────────────────────────────
def fetch_meta(code: str) -> dict:
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    w = db.execute("SELECT * FROM works WHERE code=?", (code,)).fetchone()
    if not w:
        raise SystemExit(f"[gen_infocard] '{code}' 를 works 테이블에서 찾지 못했습니다.")
    w = dict(w)
    a = None
    if w.get("actress_ja"):
        a = db.execute("SELECT * FROM actresses WHERE name_ja=?",
                       (w["actress_ja"],)).fetchone()
    a = dict(a) if a else {}
    db.close()

    likes = w.get("likes") or 0
    dis   = w.get("dislikes") or 0
    ratio = likes / (likes + dis) if (likes + dis) else 0.0
    like_pct = round(ratio * 100)
    star = round(ratio * 5, 1) if (likes + dis) else 0.0

    def man(v):                       # 269991 → "27만"
        v = v or 0
        return f"{round(v/10000)}만" if v >= 10000 else str(v)

    rd = (w.get("release_date") or "")[:7].replace("-", ".")   # 2026.03

    # 배우 프로필 사진(SFW 헤드샷) 절대경로
    photo = a.get("photo_path") or w.get("actress_photo") or ""
    if photo and not os.path.isabs(photo):
        photo = os.path.join("/home/dyyoon/jav_scrap", photo)
    if not (photo and os.path.exists(photo)):
        photo = ""

    # 썸네일(색 추출 전용 — 화면 표시 안 함)
    thumb = w.get("thumb_path") or ""
    if thumb and not os.path.isabs(thumb):
        thumb = os.path.join("/home/dyyoon/jav_scrap", thumb)
    if not (thumb and os.path.exists(thumb)):
        thumb = ""

    return {
        "thumb":    thumb,
        "code":     w["code"],
        "title":    w.get("hook_title") or w.get("title") or w["code"],
        "actress":  w.get("actress") or "",
        "actress_ja": w.get("actress_ja") or "",
        "photo":    photo,
        "label":    w.get("label") or w.get("maker") or "",
        "release":  rd,
        "runtime":  w.get("runtime_mins") or "",
        "views":    man(w.get("views")),
        "like_pct": like_pct,
        "star":     star,
        "bust":     a.get("bust"), "waist": a.get("waist"), "hip": a.get("hip"),
        "cup":      a.get("cup"),  "height": a.get("height"),
    }

# ────────────────────────────── 테마 색 추출 ──────────────────────────────
def _hx(r, g, b):
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"

def extract_theme(img_path: str) -> dict:
    """썸네일에서 대표 색을 뽑아 배너 테마(그라데이션+강조색) 구성.
    이미지 없으면 딸기 레드 기본값."""
    default = {"c1": "#ff2d55", "c2": "#e50914", "accent": "#ffe14d",
               "frame": "#ff2d55,#ff6ec4 30%,#ffd23f 55%,#ff6ec4 75%,#ff2d55"}
    if not img_path:
        return default
    try:
        from PIL import Image
        im = Image.open(img_path).convert("RGB").resize((80, 80))
        px = list(im.getdata())
        # 살구/피부톤(hue 20~50도의 저채도) 억제 → 선명한 포인트 색 선택
        best = None; bestscore = -1
        buckets = {}
        for r, g, b in px:
            h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
            if v < 0.15 or s < 0.30:      # 어둡거나 흐린(피부/무채색) 색 제외
                continue
            key = round(h * 24)           # hue 15도 단위
            acc = buckets.setdefault(key, [0, 0, 0, 0])
            acc[0] += r; acc[1] += g; acc[2] += b; acc[3] += 1
        if not buckets:
            return default
        total = sum(v[3] for v in buckets.values())
        for key, (sr, sg, sb, n) in buckets.items():
            r, g, b = sr/n, sg/n, sb/n
            h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
            skin = 0.35 if (0.03 <= h <= 0.13 and s < 0.55) else 1.0  # 피부톤 감점
            # 채도 중심 점수(선명한 포인트 색 우선) + 최소한의 빈도
            score = (s ** 2) * (0.5 + v) * (0.3 + n/total) * skin
            if score > bestscore:
                bestscore = score; best = (h, s, v)
    except Exception:
        return default
    if not best:
        return default
    h, s, v = best
    s = min(1.0, max(0.70, s))            # 쨍하게
    v = min(1.0, max(0.55, v))
    # 패널 그라데이션(밝은 → 진한)
    r1, g1, b1 = [c*255 for c in colorsys.hsv_to_rgb(h, s, min(1.0, v*1.05+0.15))]
    r2, g2, b2 = [c*255 for c in colorsys.hsv_to_rgb(h, min(1, s+0.1), max(0.35, v*0.6))]
    # 강조색: 같은 색 컨셉의 밝은 톤(살짝 노랑 쪽으로 이웃 hue) — 튀지 않게 조화
    ah = (h + 0.06) % 1.0
    ar, ag, ab = [c*255 for c in colorsys.hsv_to_rgb(ah, min(0.85, s*0.7), 1.0)]
    # 프레임: 대표색 → 밝은 이웃톤 → 대표색 흐름(동일 계열)
    lh = (h + 0.05) % 1.0
    fr, fg, fb = [c*255 for c in colorsys.hsv_to_rgb(h, s, 1.0)]
    fr2, fg2, fb2 = [c*255 for c in colorsys.hsv_to_rgb(lh, max(0.4, s*0.7), 1.0)]
    frame = (f"{_hx(fr,fg,fb)},{_hx(fr2,fg2,fb2)} 35%,"
             f"{_hx(fr,fg,fb)} 65%,{_hx(fr2,fg2,fb2)}")
    return {"c1": _hx(r1, g1, b1), "c2": _hx(r2, g2, b2),
            "accent": _hx(ar, ag, ab), "frame": frame}

# ────────────────────────────── HTML 템플릿 ──────────────────────────────
def _mascot_b64() -> str:
    with open(MASCOT, "rb") as f:
        return base64.b64encode(f.read()).decode()

_FONTS = ("@import url('https://fonts.googleapis.com/css2?"
          "family=Nanum+Brush+Script&family=Jua&display=swap');")

def html_bg() -> str:
    return f"""<!doctype html><meta charset=utf-8><style>*{{margin:0}}
.f{{width:1920px;height:1080px;background:radial-gradient(1200px 700px at 60% 32%,#3a2530,#140d12 70%)}}
.g{{position:absolute;left:0;right:0;bottom:0;height:50%;background:linear-gradient(to top,rgba(0,0,0,.6),transparent)}}
</style><div class=f><div class=g></div></div>"""

def html_frame(t: dict) -> str:
    return f"""<!doctype html><meta charset=utf-8><style>*{{margin:0;box-sizing:border-box}}html,body{{background:transparent}}
.f{{width:1920px;height:1080px;position:relative;background:transparent}}
.b{{position:absolute;inset:0;border:14px solid transparent;
 background:linear-gradient(120deg,{t['frame']}) border-box;
 -webkit-mask:linear-gradient(#000 0 0) padding-box,linear-gradient(#000 0 0);
 -webkit-mask-composite:xor;mask-composite:exclude;border-radius:8px}}
.b2{{position:absolute;inset:26px;border:2px solid rgba(255,255,255,.35);border-radius:4px}}
</style><div class=f><div class=b></div><div class=b2></div></div>"""

def html_info(m: dict, mb: str, t: dict) -> str:
    # 정보 알약 동적 구성 (없는 값은 생략)
    pills = [f'<span class="pill key">{_h(m["label"])} · {_h(m["code"])}</span>']
    if m["bust"] and m["waist"] and m["hip"]:
        pills.append(f'<span class="pill">B<b>{m["bust"]}</b>·W<b>{m["waist"]}</b>·H<b>{m["hip"]}</b></span>')
    ck = []
    if m["cup"]:    ck.append(f'<b>{m["cup"]}</b>컵')
    if m["height"]: ck.append(f'{m["height"]}cm')
    if ck: pills.append(f'<span class="pill">{" · ".join(ck)}</span>')
    if m["release"]: pills.append(f'<span class="pill">📅 {m["release"]}</span>')
    if m["star"]:    pills.append(f'<span class="pill key">★ <b>{m["star"]}</b></span>')
    if m["views"]:   pills.append(f'<span class="pill">👁 {m["views"]}</span>')
    if m["like_pct"]:pills.append(f'<span class="pill">👍 <b>{m["like_pct"]}%</b></span>')
    aja = f'<span class="ja">{_h(m["actress_ja"])}</span>' if m["actress_ja"] else ""
    return f"""<!doctype html><meta charset=utf-8><style>{_FONTS}
*{{margin:0;box-sizing:border-box;font-family:'Pretendard','Malgun Gothic',sans-serif}}html,body{{background:transparent}}
.f{{width:1920px;height:1080px;position:relative;background:transparent}}
.logo{{position:absolute;top:48px;left:56px;display:flex;align-items:center;gap:12px;
 background:linear-gradient(135deg,{t['c1']},{t['c2']});padding:10px 22px 10px 12px;border-radius:18px;
 box-shadow:0 8px 26px rgba(0,0,0,.4);transform:rotate(-2deg);border:2px solid rgba(255,255,255,.25)}}
.logo img{{width:64px;height:64px;object-fit:contain;filter:drop-shadow(0 2px 4px rgba(0,0,0,.4))}}
.logo .t{{color:#fff;font-family:'Jua';font-size:30px}}
.lower{{position:absolute;left:76px;bottom:84px}}
.title{{font-family:'Nanum Brush Script';font-size:134px;line-height:.9;
 background:linear-gradient(180deg,#fff 60%,{t['accent']});-webkit-background-clip:text;-webkit-text-fill-color:transparent;
 filter:drop-shadow(0 5px 18px rgba(0,0,0,.65)) drop-shadow(0 0 30px {t['c1']}66)}}
.actress{{font-family:'Jua';font-size:44px;color:{t['accent']};margin-top:2px;margin-left:6px;text-shadow:0 3px 12px rgba(0,0,0,.7)}}
.actress .ja{{color:#e8e8ee;font-size:23px;margin-left:10px;font-weight:600}}
.meta{{display:flex;align-items:center;gap:9px;margin-top:18px;margin-left:6px;flex-wrap:wrap;max-width:1500px}}
.pill{{font-family:'Jua';font-size:24px;padding:7px 15px;border-radius:99px;
 background:rgba(255,255,255,.14);border:1.5px solid rgba(255,255,255,.45);color:#fff;backdrop-filter:blur(3px)}}
.pill b{{color:{t['accent']}}}
.pill.key{{background:linear-gradient(135deg,{t['c1']},{t['c2']});border:1.5px solid rgba(255,255,255,.35);color:#fff;box-shadow:0 4px 14px rgba(0,0,0,.35)}}
.pill.key b{{color:{t['accent']}}}
</style><div class=f>
 <div class=logo><img src="data:image/png;base64,{mb}"><span class=t>딸딸기튜브</span></div>
 <div class=lower>
  <div class=title>{_h(m["title"])}</div>
  <div class=actress>{_h(m["actress"])} {aja}</div>
  <div class=meta>{''.join(pills)}</div>
 </div></div>"""

def html_wm(m: dict, mb: str, t: dict) -> str:
    # 배우 얼굴(좌) + 3줄: ①품번  ②배우이름+3사이즈  ③출시일+평점
    if m.get("photo"):
        with open(m["photo"], "rb") as f:
            face = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
        face_html = f'<div class=face style="background-image:url({face})"></div>'
    else:
        face_html = f'<div class=face style="background-image:url(data:image/png;base64,{mb});background-size:70%;background-color:#e50914"></div>'
    size = ""
    if m["bust"] and m["waist"] and m["hip"]:
        size = f'<span class=sz>B{m["bust"]}·W{m["waist"]}·H{m["hip"]}</span>'
    r3 = []
    if m["release"]: r3.append(f'📅 {m["release"]}')
    if m["star"]:    r3.append(f'★ {m["star"]}')
    row3 = "&nbsp;&nbsp;".join(r3)
    # 3줄 각각 다른 바 스타일(같은 팔레트) — 방송 하단자막식
    return f"""<!doctype html><meta charset=utf-8><style>{_FONTS}
*{{margin:0;box-sizing:border-box}}html,body{{background:transparent}}
.f{{width:1920px;height:1080px;position:relative;background:transparent;font-family:'Jua','Malgun Gothic',sans-serif}}
.wrap{{position:absolute;top:44px;left:48px;display:flex;align-items:center;gap:16px;
 filter:drop-shadow(0 6px 18px rgba(0,0,0,.5))}}
.face{{width:138px;height:138px;border-radius:20px;background-size:cover;background-position:center top;
 border:4px solid #fff;box-shadow:0 4px 12px rgba(0,0,0,.45);z-index:2;flex:0 0 auto}}
.rows{{display:flex;flex-direction:column;gap:6px;align-items:flex-start}}
.row{{border-radius:11px;line-height:1;white-space:nowrap;display:inline-flex;align-items:center}}
/* 1줄: 품번 — 진한 테마색 solid, 굵게 */
.r1{{background:linear-gradient(135deg,{t['c2']},{t['c1']});color:#fff;font-size:38px;
 padding:11px 20px;border:2px solid rgba(255,255,255,.4);box-shadow:0 3px 8px rgba(0,0,0,.35)}}
/* 2줄: 배우+사이즈 — 밝은 흰바탕에 테마색 글씨 */
.r2{{background:rgba(255,255,255,.95);color:{t['c2']};font-size:29px;padding:9px 18px;gap:11px}}
.r2 .sz{{color:{t['c1']};font-size:22px;font-weight:400}}
/* 3줄: 출시일·평점 — 반투명 어두운 바 + 강조색 */
.r3{{background:rgba(20,10,14,.72);color:{t['accent']};font-size:24px;padding:9px 18px;
 border:1.5px solid {t['accent']}88;backdrop-filter:blur(3px)}}
</style><div class=f>
 <div class=wrap>
  {face_html}
  <div class=rows>
   <div class="row r1">{_h(m["code"])}</div>
   <div class="row r2">{_h(m["actress"])} <span class=sz>{size}</span></div>
   <div class="row r3">{row3}</div>
  </div>
 </div></div>"""

def _h(s):  # html escape
    return (str(s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"))

# ────────────────────────────── 렌더 ──────────────────────────────
def render_layers(m: dict, outdir: str, theme=None) -> dict:
    from playwright.sync_api import sync_playwright
    mb = _mascot_b64()
    # 색은 인포카드 브랜드 팔레트(딸기 레드)로 통일 — 인포카드·워터마크 동일 컨셉
    t = theme or extract_theme("")          # ""이면 딸기 레드 기본 팔레트
    print(f"[gen_infocard] theme: {t['c1']} / {t['c2']} / accent {t['accent']}")
    pages = {
        "bg":    (html_bg(),           False),
        "frame": (html_frame(t),       True),
        "info":  (html_info(m, mb, t), True),
        "wm":    (html_wm(m, mb, t),   True),
    }
    paths = {}
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        for name, (html, transp) in pages.items():
            f = os.path.join(outdir, f"_L_{name}.html")
            with open(f, "w") as fp: fp.write(html)
            pg = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
            pg.goto("file://" + f); pg.wait_for_timeout(1300)
            png = os.path.join(outdir, f"L_{name}.png")
            pg.screenshot(path=png, omit_background=transp); pg.close()
            paths[name] = png
        b.close()
    return paths

# ────────────────────────────── 인코더 자동 선택 ──────────────────────────────
_ENC_CACHE = None
def pick_encoder(prefer=None):
    """가능하면 GPU(NVENC) 사용 → 없으면 libx264 빠른 프리셋.
    반환: (['-c:v', ...] 인자 리스트, 이름)"""
    global _ENC_CACHE
    if prefer == "cpu":
        return (["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"], "libx264 veryfast")
    if _ENC_CACHE is not None and prefer is None:
        return _ENC_CACHE
    have = ""
    try:
        have = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                              capture_output=True, text=True).stdout
    except Exception:
        pass
    # NVENC는 인코더 목록에 있어도 실제 GPU/드라이버 없으면 실패 → GPU 존재 확인
    gpu = os.path.exists("/proc/driver/nvidia") or shutil.which("nvidia-smi") is not None \
          or os.name == "nt"   # 윈도우(RTX3060 렌더 머신)는 시도
    if prefer == "nvenc" or ("h264_nvenc" in have and gpu):
        enc = (["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20", "-b:v", "0"],
               "h264_nvenc(GPU)")
    else:
        enc = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"], "libx264 veryfast")
    if prefer is None:
        _ENC_CACHE = enc
    return enc

# ────────────────────────────── ffmpeg 합성 ──────────────────────────────
def compose(layers, out_mp4, video=None, hold=2.0, fade=0.5, demo_len=4.0,
            encoder=None, log=print):
    """video 지정 시 실제 영상에 오버레이, 없으면 어두운 배경 데모.
    encoder: None(자동/GPU우선) | 'nvenc' | 'cpu'"""
    enc_args, enc_name = pick_encoder(encoder)
    fc = (
        f"[2]fade=t=in:st=0:d=0.4:alpha=1,"
        f"fade=t=out:st={hold}:d={fade}:alpha=1[ic];"
        f"[3]fade=t=in:st={hold+fade*0.2}:d={fade}:alpha=1[wm];"
    )
    if video:
        # 입력영상 위에 frame/info/wm 오버레이 (오디오 유지)
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-i", video,
               "-loop", "1", "-i", layers["frame"],
               "-loop", "1", "-i", layers["info"],
               "-loop", "1", "-i", layers["wm"]]
        fc += ("[0:v][1]overlay=0:0[b1];"
               "[b1][ic]overlay=0:0[b2];"
               "[b2][wm]overlay=0:0,format=yuv420p[out]")
        cmd += ["-filter_complex", fc, "-map", "[out]",
                "-map", "0:a?", "-c:a", "copy"] + enc_args + [out_mp4]
    else:
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-loop", "1", "-t", str(demo_len), "-i", layers["bg"],
               "-loop", "1", "-t", str(demo_len), "-i", layers["frame"],
               "-loop", "1", "-t", str(demo_len), "-i", layers["info"],
               "-loop", "1", "-t", str(demo_len), "-i", layers["wm"]]
        fc += ("[0][1]overlay=0:0[b1];"
               "[b1][ic]overlay=0:0[b2];"
               "[b2][wm]overlay=0:0,format=yuv420p[out]")
        cmd += ["-filter_complex", fc, "-map", "[out]",
                "-r", "30", "-t", str(demo_len)] + enc_args + [out_mp4]
    log(f"[infocard] 인코더: {enc_name}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        # GPU 실패 시 CPU 폴백
        if enc_name.startswith("h264_nvenc"):
            log("[infocard] NVENC 실패 → libx264로 폴백")
            enc_args, enc_name = pick_encoder("cpu")
            cmd = cmd[:cmd.index(out_mp4)]
            # 인코더 인자 교체: 마지막 -c:v ... 부분을 다시 구성하는 대신 재빌드가 안전
            return compose(layers, out_mp4, video=video, hold=hold, fade=fade,
                           demo_len=demo_len, encoder="cpu", log=log)
        raise
    return out_mp4

# ────────────────────────────── 미리보기 스틸(PIL, 인코딩 없음) ──────────────────────────────
def _preview_still(layers, keys, out_png):
    """PNG 레이어들을 순서대로 알파합성해 스틸 1장 생성(ffmpeg 인코딩 없음)."""
    from PIL import Image
    base = Image.open(layers["bg"]).convert("RGBA")
    for k in keys:
        base = Image.alpha_composite(base, Image.open(layers[k]).convert("RGBA"))
    base.convert("RGB").save(out_png)
    return out_png

# ────────────────────────────── 재사용 API ──────────────────────────────
def generate(code, video=None, out=None, hold=2.0, outdir=None, log=print,
             assets_only=True, preview_anim=True):
    """품번 → 인포배너 오버레이 소스 생성.
    assets_only=True(기본): 인코딩 없이 오버레이 PNG(프레임/인포카드/워터마크) +
       미리보기 스틸 2장만 생성 → 편집 프로그램에 얹어 사용. (초 단위, 재인코딩 안 함)
    assets_only=False: (옵션) 실제 mp4까지 합성(느림/재인코딩)."""
    m = fetch_meta(code)
    log(f"[infocard] {m['code']} / {m['actress']} / {m['title']}")
    outdir = outdir or os.path.join(tempfile.gettempdir(), f"infocard_{code}")
    os.makedirs(outdir, exist_ok=True)
    log("[infocard] 오버레이 이미지 렌더 중…")
    layers = render_layers(m, outdir)

    # 편집용 오버레이 소스(투명 PNG) — 파일명 알기 쉽게 복사
    assets = {}
    for k, fname in (("frame", f"{code}_프레임.png"),
                     ("info",  f"{code}_인포카드.png"),
                     ("wm",    f"{code}_워터마크.png")):
        dst = os.path.join(outdir, fname)
        if layers[k] != dst:
            shutil.copyfile(layers[k], dst)
        assets[k] = dst

    # 미리보기 스틸(인코딩 없이 즉시)
    log("[infocard] 미리보기 스틸 합성 중(인코딩 없음)…")
    prev_info = _preview_still(layers, ["frame", "info"], os.path.join(outdir, f"{code}_미리보기_인포카드.png"))
    prev_wm   = _preview_still(layers, ["frame", "wm"],   os.path.join(outdir, f"{code}_미리보기_워터마크.png"))

    # 움직이는 미리보기(짧은 4초 애니 데모) — 짧아서 빠름
    prev_anim = None
    if preview_anim:
        try:
            log("[infocard] 움직이는 미리보기(4초 애니) 생성 중…")
            prev_anim = compose(layers, os.path.join(outdir, f"{code}_미리보기_애니.mp4"),
                                video=None, hold=hold, log=log)
        except Exception as e:
            log(f"[infocard] 애니 미리보기 실패(무시): {e}")

    result = {"assets": assets, "preview_info": prev_info, "preview_wm": prev_wm,
              "preview_anim": prev_anim, "layers": layers, "meta": m, "out": None}

    if not assets_only:
        out = out or (
            os.path.splitext(video)[0] + "_banner.mp4" if video
            else os.path.join(outdir, f"{code}_demo.mp4"))
        log("[infocard] ffmpeg 합성 중…" + (" (입력영상 오버레이)" if video else " (데모 배경)"))
        compose(layers, out, video=video, hold=hold, log=log)
        result["out"] = out
    log("[infocard] 완료")
    return result


# ────────────────────────────── main ──────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code", help="품번 예: SNOS-152")
    ap.add_argument("--video", help="오버레이할 입력 영상(mp4). 없으면 데모 배경 생성")
    ap.add_argument("--out", help="출력 mp4 경로")
    ap.add_argument("--hold", type=float, default=2.0, help="인포카드 노출 시간(초)")
    ap.add_argument("--outdir", default=None, help="레이어 PNG 저장 폴더")
    ap.add_argument("--layers-only", action="store_true", help="PNG만 생성하고 종료")
    args = ap.parse_args()

    m = fetch_meta(args.code)
    print(f"[gen_infocard] {m['code']} / {m['actress']} / {m['title']}")

    outdir = args.outdir or os.path.join(tempfile.gettempdir(), f"infocard_{args.code}")
    os.makedirs(outdir, exist_ok=True)
    layers = render_layers(m, outdir)
    print("[gen_infocard] layers:", {k: os.path.basename(v) for k, v in layers.items()})
    if args.layers_only:
        return

    out = args.out or (
        os.path.splitext(args.video)[0] + "_banner.mp4" if args.video
        else os.path.join(outdir, f"{args.code}_demo.mp4"))
    compose(layers, out, video=args.video, hold=args.hold)
    print(f"[gen_infocard] done → {out}")

if __name__ == "__main__":
    main()
