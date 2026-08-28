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

FFPROBE_TIMEOUT = 60      # 짧은 조회(인코더목록·프로브)
FFMPEG_TIMEOUT = 1800     # 오버레이 인코딩(배너는 보통 짧음)

HERE   = os.path.dirname(os.path.abspath(__file__))

def _first_existing(*cands):
    for c in cands:
        if c and os.path.exists(c):
            return c
    return cands[0] if cands else ""

# DB: 환경변수 → 리눅스(우분투 서버) → 이 레포 옆 jav_scrap (Windows)
DB = _first_existing(
    os.environ.get("JAV_DB"),
    "/home/dyyoon/jav_scrap/jav_2026.db",
    os.path.join(os.path.dirname(HERE), "jav_scrap", "jav_2026.db"),
)
JAV_ROOT = os.path.dirname(DB)   # photo/thumb 상대경로 기준
MASCOT = os.path.join(HERE, "server", "assets", "mascot.png")
# CHROME: 환경변수 → 리눅스 경로. 없으면 None → playwright 번들 chromium 사용
CHROME = _first_existing(
    os.environ.get("PW_CHROME"),
    "/home/dyyoon/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome",
)
if not (CHROME and os.path.exists(CHROME)):
    CHROME = None

# ────────────────────────────── DB 조회 ──────────────────────────────
class MetaNotFound(Exception):
    """품번이 DB(works)에 없음. 라이브러리로 쓰이므로 SystemExit(BaseException)을 던지면
    호출측의 except Exception 을 뚫고 나가 큐 워커까지 죽는다."""


class SpecMissing(Exception):
    """3사이즈(B/W/H)를 못 채웠다 — **배너를 만들지 않고 멈춘다**.

    ★MetaNotFound 와 일부러 별개 계층으로 둔다. stage_banner 는 MetaNotFound 를
      '아직 크롤링 안 된 신작'으로 보고 배너만 건너뛰고 계속 가는데, 스펙 누락까지
      거기 얹으면 배너가 통째로 빠진 채 조용히 납품된다(더 나쁘다).
    """


def _cfg(key, default=None):
    try:
        cfg = json.load(open(os.path.join(HERE, "studio_config.json"), encoding="utf-8"))
        v = cfg.get(key)
        return default if v is None else v
    except Exception:
        return default


def _meta_api_base():
    """studio_config.json의 meta_api(우분투 DB 서버). 없으면 기본값."""
    return (_cfg("meta_api", "http://172.30.1.40:8770") or "").rstrip("/")


def _alias_map():
    """`actress_alias.json` — 배우명(한국어 또는 변형 한자) → actresses.name_ja.

    DB 매칭이 끝내 안 될 때 **파일만 떨구면 이어지는** 탈출구다. 한자 표기가
    사이트마다 다르거나(七島舞/七嶋舞) 크롤러가 한국어명만 남긴 행에서 쓴다.
    '_' 로 시작하는 키는 설명용이라 무시한다.
    """
    try:
        m = json.load(open(os.path.join(HERE, "actress_alias.json"), encoding="utf-8"))
        return {k.strip(): v.strip() for k, v in m.items()
                if not k.startswith("_") and isinstance(v, str) and v.strip()}
    except Exception:
        return {}


def _fetch_remote(code: str):
    """로컬 DB(복사본)는 신작이 없다 — 크롤링은 우분투에서 매일 돌고 로컬은 뒤처진다.
    파이프라인의 다른 단계는 이미 meta_api를 쓰므로 배너도 같은 소스로 폴백한다.
    (배우 사진·썸네일은 우분투 로컬 파일이라 못 받는다 → 사진 없이 카드를 만든다)"""
    import urllib.request
    base = _meta_api_base()
    if not base:
        return None
    url = f"{base}/work/{urllib.parse.quote(code)}"
    with urllib.request.urlopen(url, timeout=15) as r:
        m = json.loads(r.read().decode("utf-8"))
    if not m or m.get("error") or not m.get("code"):
        return None
    return m


def _actress_row(db, w):
    """작품행 → actresses 행. 이름이 안 맞으면 **사진 경로**로 되짚는다.

    ★3사이즈·컵·키·일본어 배우명은 한 덩어리로 이 한 행에서 온다. 못 찾으면 넷이
      동시에 비는데 화면에는 '3사이즈가 없다'로만 보여 원인을 알기 어렵다.
      실제로 끊기는 경우가 셋 있었다(2026-08-28 ja20 조사):
        ① `works.actress_ja` 가 아예 NULL — 전체 works 의 84%가 이 상태다.
           크롤러(jav_scraper)는 이 컬럼을 안 채우고 fetch_actress 를 따로 돌려야 채워진다.
        ② 한자 표기가 사이트마다 다름(七島舞/七嶋舞, 개명 병기 河北彩花（河北彩伽）).
        ③ actresses 행은 있는데 수치가 빔 → 그건 여기서 못 고친다(fetch_measurements 몫).
      ①②는 `works.actress_photo` 로 복구된다 — 사진 파일 경로는 배우를 유일하게
      가리키기 때문이다(실측: 이 경로로 1,113개 작품이 3사이즈까지 즉시 복구된다.
      photo_path 가 두 명 이상에 겹치는 경우가 5건 있어 **정확히 1행일 때만** 채택한다).
    """
    ja = (w.get("actress_ja") or "").strip()
    if ja:
        r = db.execute("SELECT * FROM actresses WHERE name_ja=?", (ja,)).fetchone()
        if r:
            return r
        base = re.split(r"[（(]", ja)[0].strip()          # 개명 병기 벗겨서 한 번 더
        if base and base != ja:
            r = db.execute("SELECT * FROM actresses WHERE name_ja=? OR name_ja LIKE ?",
                           (base, base + "（%")).fetchone()
            if r:
                print(f"[gen_infocard] 배우 '{ja}' → '{r['name_ja']}' (병기 표기 정규화)")
                return r
    photo = (w.get("actress_photo") or "").strip()
    if photo:
        rows = db.execute("SELECT * FROM actresses WHERE photo_path=?", (photo,)).fetchall()
        if len(rows) == 1:
            print(f"[gen_infocard] actress_ja 로 못 찾음 → 사진 경로로 배우 확정: "
                  f"'{rows[0]['name_ja']}'")
            return rows[0]
    alias = _alias_map()                                  # 마지막 탈출구: 수동 별칭 파일
    for key in (ja, (w.get("actress") or "").strip()):
        tgt = alias.get(key)
        if tgt:
            r = db.execute("SELECT * FROM actresses WHERE name_ja=?", (tgt,)).fetchone()
            if r:
                print(f"[gen_infocard] actress_alias.json: '{key}' → '{tgt}'")
                return r
            print(f"[gen_infocard] ⚠ actress_alias.json 의 '{key}' → '{tgt}' 가 "
                  f"actresses 에 없습니다")
    return None


def fetch_meta(code: str) -> dict:
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    w = db.execute("SELECT * FROM works WHERE code=?", (code,)).fetchone()
    a = None
    if w:
        w = dict(w)
        a = _actress_row(db, w)
    db.close()
    if not w:
        # 로컬 DB에 없음 = 신작. 우분투 meta_api(매일 크롤링)로 폴백.
        try:
            w = _fetch_remote(code)
        except Exception as e:
            raise MetaNotFound(
                f"[gen_infocard] '{code}' 가 로컬 DB에 없고 meta_api 조회도 실패했습니다({e}). "
                f"우분투 meta_api(8770)가 떠 있는지 확인하세요.")
        if not w:
            raise MetaNotFound(
                f"[gen_infocard] '{code}' 를 로컬 DB·meta_api 어디서도 찾지 못했습니다. "
                f"아직 크롤링되지 않은 품번일 수 있습니다.")
        print(f"[gen_infocard] '{code}' 로컬 DB에 없음 → meta_api에서 가져옴(신작)")
        # ★ 원격 메타에는 배우 사진이 안 실려 온다(우분투 로컬 파일). 그렇다고 사진을
        #   포기하면 워터마크 얼굴 슬롯이 딸기 마스코트로 떨어진다(2026-07-24 ja13 사례:
        #   11편 전부 얼굴 없이 납품될 뻔). 작품만 신작이고 배우는 로컬 DB에 이미 있는
        #   경우가 대부분이므로, 이름으로 로컬 actresses를 한 번 더 조회해 사진을 붙인다.
        db2 = sqlite3.connect(DB); db2.row_factory = sqlite3.Row
        a = _actress_row(db2, w)
        db2.close()
        if a:
            print(f"[gen_infocard] 배우 '{a['name_ja']}' 로컬 DB에서 사진 확보")
    a = dict(a) if a else {}

    # ── 3사이즈는 **반드시** 들어간다 (2026-08-28 규칙) ─────────────────────
    # 예전에는 비어도 경고 한 줄 없이 그냥 그려서 ja20 12편 중 6편이 스펙 없는 배너로
    # 납품될 뻔했다. 이제 소스를 3단으로 두고, 그래도 못 채우면 **배너를 안 만든다**.
    SPEC = ("bust", "waist", "hip", "cup", "height")
    BWH = ("bust", "waist", "hip")
    spec = {k: a.get(k) for k in SPEC}
    if not all(spec[k] for k in BWH):
        # ② meta_api 의 work 응답은 배우 수치를 flatten 해서 같이 준다. 로컬 actresses 에
        #    행이 아예 없는 신작은 이 값이 유일한 출처인데 여태 통째로 버려지고 있었다.
        filled = [k for k in SPEC if not spec[k] and w.get(k)]
        for k in filled:
            spec[k] = w[k]
        if filled and all(spec[k] for k in BWH):
            print(f"[gen_infocard] 3사이즈를 meta_api 응답에서 채웠습니다({','.join(filled)})")

    if not all(spec[k] for k in BWH):
        who_ja = (w.get("actress_ja") or "").strip()
        who_ko = (w.get("actress") or "").strip()
        who = who_ja or who_ko or "?"
        # 배우가 특정되지 않는 작품(기획물·다인 출연)은 애초에 스펙이 존재하지 않는다.
        solo = bool(who_ja or who_ko) and "," not in who_ko
        if not solo:
            print(f"[gen_infocard] · '{code}' 배우 특정 불가(기획물/다인) — "
                  f"3사이즈 없이 진행합니다")
        elif not _cfg("require_spec", True) or os.environ.get("JA_ALLOW_NOSPEC"):
            print(f"[gen_infocard] ⚠ '{code}' 3사이즈 없음(배우 {who}) — "
                  f"require_spec 해제 상태라 그대로 만듭니다")
        else:
            raise SpecMissing(
                f"'{code}' 3사이즈(B/W/H)를 못 채워 배너를 만들지 않았습니다. 배우: {who}\n"
                f"  채우는 법 —\n"
                f"   1) actresses 행에 수치가 없다면:  jav_scrap/fetch_measurements.py "
                f"--retry-miss   (우분투 venv + Tor 필요)\n"
                f"   2) 배우 연결이 안 된 것이면:      works.actress_ja 를 잇거나 "
                f"ja_reviewer/actress_alias.json 에 '{who}' → 한자명 한 줄 추가\n"
                f"   3) 수동으로 넣을 값이 있으면:     tools/_fill_actress_ja20.py 를 본떠 "
                f"로컬·우분투 DB 양쪽에 반영\n"
                f"  일부러 스펙 없이 만들려면: studio_config.json 에 \"require_spec\": false "
                f"(또는 환경변수 JA_ALLOW_NOSPEC=1)")

    likes = w.get("likes") or 0
    dis   = w.get("dislikes") or 0
    ratio = likes / (likes + dis) if (likes + dis) else 0.0
    like_pct = round(ratio * 100)
    star = round(ratio * 5, 1) if (likes + dis) else 0.0

    def man(v):                       # 269991 → "27만"
        v = v or 0
        return f"{round(v/10000)}만" if v >= 10000 else str(v)

    rd = (w.get("release_date") or "")[:10].replace("-", ".")  # 2026.03.15 (일자까지)

    # 배우 프로필 사진(SFW 헤드샷) 절대경로
    photo = a.get("photo_path") or w.get("actress_photo") or ""
    if photo and not os.path.isabs(photo):
        photo = os.path.join(JAV_ROOT, photo)
    if not (photo and os.path.exists(photo)):
        photo = ""

    # 썸네일(색 추출 전용 — 화면 표시 안 함)
    thumb = w.get("thumb_path") or ""
    if thumb and not os.path.isabs(thumb):
        thumb = os.path.join(JAV_ROOT, thumb)
    if not (thumb and os.path.exists(thumb)):
        thumb = ""

    # 제목: hook_title(한글 번역) 우선. title이 배우이름과 같으면(크롤러가 제목 대신
    # 배우이름을 넣은 행) 이름이 두 번 찍히므로 영/일 원제 → 품번으로 폴백.
    # ★크롤러가 제목을 못 얻으면 "START-627 - 미야지마 메이" 같은 **자리표시자**를 넣는다.
    #   이건 배우이름과 '같지' 않아 기존 조건을 빠져나가고, 그대로 그리면 큰 품번 +
    #   제목줄 + 배우줄로 같은 정보가 3중으로 찍힌다(ja20 START-627·START-622·FNS-248).
    #   품번으로 폴백해두면 렌더 쪽이 '제목==품번'을 이미 숨기므로 줄이 깔끔하게 빠진다.
    title = w.get("hook_title") or w.get("title") or ""
    _t = title.strip()
    _ko = (w.get("actress") or "").strip()
    _placeholder = bool(re.match(rf"^{re.escape(w['code'])}\s*[-–—]\s*", _t))
    if not _t or _t == _ko or _placeholder:
        title = w.get("title_en") or w.get("title_ja") or w["code"]
        if _placeholder:
            print(f"[gen_infocard] 제목이 자리표시자('{_t}') — 제목 줄을 비웁니다")

    return {
        "thumb":    thumb,
        "code":     w["code"],
        "title":    title,
        "actress":  w.get("actress") or "",
        "actress_ja": w.get("actress_ja") or "",
        "photo":    photo,
        "label":    w.get("label") or w.get("maker") or "",
        "release":  rd,
        "runtime":  w.get("runtime_mins") or "",
        "views":    man(w.get("views")),
        "like_pct": like_pct,
        "star":     star,
        "bust":     spec["bust"], "waist": spec["waist"], "hip": spec["hip"],
        "cup":      spec["cup"],  "height": spec["height"],
    }

# ────────────────────────────── 테마 색 추출 ──────────────────────────────
def _hx(r, g, b):
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"

def extract_theme(img_path: str) -> dict:
    """썸네일에서 대표 색을 뽑아 배너 테마(그라데이션+강조색) 구성.
    이미지 없으면 딸기 레드 기본값."""
    default = {"c1": "#ff2d55", "c2": "#e50914", "accent": "#ffe14d",
               "frame": "#ff2d55,#ff6ec4 30%,#ff9ad1 55%,#ff6ec4 75%,#ff2d55"}
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
    # 프레임: 대표색 → 같은 hue의 밝은톤 → 대표색 흐름(색상 이동 없이 동일 계열 유지)
    # (이웃 hue를 섞으면 핑크가 주황/노랑으로 번져 테두리가 끊겨 보인다 — 2026-07-19)
    fr, fg, fb = [c*255 for c in colorsys.hsv_to_rgb(h, s, 1.0)]
    fr2, fg2, fb2 = [c*255 for c in colorsys.hsv_to_rgb(h, max(0.35, s*0.55), 1.0)]
    frame = (f"{_hx(fr,fg,fb)},{_hx(fr2,fg2,fb2)} 35%,"
             f"{_hx(fr,fg,fb)} 65%,{_hx(fr2,fg2,fb2)}")
    return {"c1": _hx(r1, g1, b1), "c2": _hx(r2, g2, b2),
            "accent": _hx(ar, ag, ab), "frame": frame}

# ────────────────────────────── HTML 템플릿 ──────────────────────────────
def _mascot_b64() -> str:
    with open(MASCOT, "rb") as f:
        return base64.b64encode(f.read()).decode()

_FONTS = ("@import url('https://fonts.googleapis.com/css2?"
          "family=Nanum+Brush+Script&family=Jua&family=Black+Han+Sans&display=swap');")

def html_bg() -> str:
    return f"""<!doctype html><meta charset=utf-8><style>*{{margin:0}}
.f{{width:1920px;height:1080px;background:radial-gradient(1200px 700px at 60% 32%,#3a2530,#140d12 70%)}}
.g{{position:absolute;left:0;right:0;bottom:0;height:50%;background:linear-gradient(to top,rgba(0,0,0,.6),transparent)}}
</style><div class=f><div class=g></div></div>"""

def html_frame(t: dict) -> str:
    return f"""<!doctype html><meta charset=utf-8><style>*{{margin:0;box-sizing:border-box}}html,body{{background:transparent}}
.f{{width:1920px;height:1080px;position:relative;background:transparent}}
.b{{position:absolute;inset:0;border:22px solid transparent;
 background:linear-gradient(120deg,{t['frame']}) border-box;
 -webkit-mask:linear-gradient(#000 0 0) padding-box,linear-gradient(#000 0 0);
 -webkit-mask-composite:xor;mask-composite:exclude;border-radius:8px}}
.b2{{position:absolute;inset:34px;border:2px solid rgba(255,255,255,.35);border-radius:4px}}
</style><div class=f><div class=b></div><div class=b2></div></div>"""

def html_info(m: dict, mb: str, t: dict) -> str:
    """인트로 인포카드 — 좌측 액센트 바 + 품번/배우/메타 3줄(2026-07-24 재디자인).

    옛 디자인은 브러시체 타이틀의 좌측 시작점이 아래 줄과 어긋나고(side bearing),
    폰트 3종(브러시·Jua·컬러이모지)이 섞여 자간·글자높이가 제각각인 데다
    품번이 타이틀과 알약에, 배우명이 타이틀과 본문에 중복 노출됐다.
    → 한 서체 계열 + 왼쪽 끝 완전 일치 + 중복 제거 + 이모지 대신 텍스트 라벨.
    """
    # ★2026-08-12 재설계 — 사용자 지적 3건을 한꺼번에 고친다.
    #   ① 제목이 화면에 없었다. meta()는 hook_title→title→원제 순으로 title을 계산하는데
    #      2026-07-24 재디자인이 '중복 제거'를 하면서 제목 줄 자체를 지웠고, 그 뒤로
    #      AI가 쓴 hook_title이 한 번도 안 나갔다(계산만 되는 죽은 값).
    #   ② 정보가 부족했다 — 신체 스펙이 레이블·발매일과 한 줄에 섞여 눈에 안 들어왔다.
    #      B·W·H / 컵 / 키를 **별도 강조 줄(pill)**로 빼서 반드시 보이게 한다.
    #   ③ 작았다 — 품번 96→104, 배우 46→52, 메타 26→30, 카드 여백도 키웠다.
    #   폰트도 Jua(둥근 캐주얼체) → Paperlogy(채널 자막과 같은 계열)로 통일.
    #   품번만 Black Han Sans 유지(임팩트).
    # ★2026-08-19 3사이즈 색상 복원 — 6057447이 .sz span을 지우면서 B·W·H가 회색
    #   한 덩어리가 됐다(원래는 c1 컬러). 글자(B/W/H)만 브랜드색, 숫자는 흰색으로
    #   되살린다. 이 항목만 raw HTML이라 _h() 이스케이프를 건너뛴다(값은 전부 숫자).
    spec = []
    if m["bust"] and m["waist"] and m["hip"]:
        spec.append(f'<span class=sz><i>B</i>{m["bust"]}<b>·</b>'
                    f'<i>W</i>{m["waist"]}<b>·</b><i>H</i>{m["hip"]}</span>')
    if m["cup"]:    spec.append(_h(f'{m["cup"]}컵'))
    if m["height"]: spec.append(_h(f'{m["height"]}cm'))
    spec_html = ("<div class=spec>"
                 + "".join(f"<span class=pill>{s}</span>" for s in spec)
                 + "</div>") if spec else ""

    bits = []
    if m["label"]:    bits.append(_h(m["label"]))
    if m["release"]:  bits.append(m["release"])
    if m["runtime"]:  bits.append(f'{m["runtime"]}분')
    if m["star"]:     bits.append(f'★ {m["star"]}')
    if m["like_pct"]: bits.append(f'좋아요 {m["like_pct"]}%')
    meta = '<span class=dot>·</span>'.join(f"<span>{b}</span>" for b in bits)
    aja = f'<span class="ja">{_h(m["actress_ja"])}</span>' if m["actress_ja"] else ""
    # 제목은 품번과 겹치면(폴백으로 품번이 들어온 경우) 굳이 두 번 쓰지 않는다.
    ttl = (m.get("title") or "").strip()
    title_html = (f'<div class=title>{_h(ttl)}</div>'
                  if ttl and ttl != m["code"] else "")
    return f"""<!doctype html><meta charset=utf-8><style>{_FONTS}
*{{margin:0;box-sizing:border-box}}html,body{{background:transparent}}
.f{{width:1920px;height:1080px;position:relative;background:transparent;
 font-family:'Paperlogy','Pretendard','Malgun Gothic',sans-serif;font-weight:700}}
.logo{{position:absolute;top:48px;left:56px;display:flex;align-items:center;gap:14px;
 background:linear-gradient(135deg,{t['c1']},{t['c2']});padding:12px 26px 12px 14px;border-radius:20px;
 box-shadow:0 8px 26px rgba(0,0,0,.4);transform:rotate(-2deg);border:2px solid rgba(255,255,255,.25)}}
.logo img{{width:72px;height:72px;object-fit:contain;filter:drop-shadow(0 2px 4px rgba(0,0,0,.4))}}
.logo .t{{color:#fff;font-size:34px;letter-spacing:-.01em}}
.card{{position:absolute;left:84px;bottom:88px;display:flex;align-items:stretch;
 background:rgba(10,6,10,.78);border-radius:24px;overflow:hidden;
 border:1.5px solid rgba(255,255,255,.14);box-shadow:0 14px 50px rgba(0,0,0,.55)}}
.bar{{width:12px;background:linear-gradient(180deg,{t['c1']},{t['c2']});flex:0 0 auto}}
.body{{padding:34px 52px 34px 40px}}
.code{{font-family:'Black Han Sans',sans-serif;font-size:104px;line-height:1;
 letter-spacing:.01em;color:#fff;text-shadow:0 4px 18px rgba(0,0,0,.6)}}
.title{{font-size:52px;line-height:1.25;color:#fff;margin-top:16px;letter-spacing:-.02em;
 max-width:1420px;text-shadow:0 2px 10px rgba(0,0,0,.55)}}
.name{{font-size:52px;color:{t['accent']};margin-top:16px;letter-spacing:-.02em;
 display:flex;align-items:baseline;gap:16px}}
.name .ja{{color:#cfcfd8;font-size:28px;font-weight:400}}
.spec{{margin-top:20px;display:flex;gap:12px;flex-wrap:wrap}}
.spec .pill{{font-size:34px;line-height:1;color:#fff;padding:12px 22px;border-radius:999px;
 background:rgba(255,255,255,.10);border:1.5px solid {t['accent']}66;letter-spacing:.01em}}
.spec .sz i{{color:{t['c1']};font-style:normal;font-weight:700}}
.spec .sz b{{color:{t['c1']};font-weight:700;margin:0 3px}}
.meta{{margin-top:20px;font-size:30px;color:#e6e6ee;letter-spacing:-.01em;font-weight:400;
 display:flex;align-items:center;gap:14px;flex-wrap:wrap;max-width:1500px}}
.meta .dot{{color:{t['c1']}}}
</style><div class=f>
 <div class=logo><img src="data:image/png;base64,{mb}"><span class=t>딸딸기튜브</span></div>
 <div class=card>
  <div class=bar></div>
  <div class=body>
   <div class=code>{_h(m["code"])}</div>
   {title_html}
   <div class=name>{_h(m["actress"])}{aja}</div>
   {spec_html}
   <div class=meta>{meta}</div>
  </div>
 </div></div>"""

def html_wm(m: dict, mb: str, t: dict) -> str:
    """상시 워터마크 — 얼굴 + 품번/배우/출시일을 담은 단일 패널(2026-07-24 재디자인).

    옛 디자인은 바 3개가 각각 다른 글자크기(38/29/24)·패딩·테두리로 떠 있어
    내용 길이에 따라 오른쪽 끝이 계단처럼 어긋났다("중구난방"). 하나의 패널로
    묶어 오른쪽 끝을 직선으로 떨어뜨리고, 이모지(📅) 대신 가운뎃점으로 통일.
    """
    if m.get("photo"):
        with open(m["photo"], "rb") as f:
            face = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
        face_bg = f"url({face});background-size:cover"
    else:
        face_bg = (f"url(data:image/png;base64,{mb});background-size:70%;"
                   f"background-color:{t['c1']}")
    # 상시 워터마크에도 3사이즈를 넣는다(2026-07-30 요청) — 인트로 인포카드가 사라진 뒤에는
    # 스펙을 볼 수 있는 곳이 없었다. 형식은 인포카드와 동일(B··W··H·).
    # ★2026-08-12 — 크기 상향(얼굴 116→148, 품번 38→50, 이름 27→34, 서브 21→26)과
    #   3사이즈 노출 보강. 1080p 리프레임 납품본에서 옛 크기는 화면 대비 너무 작았다.
    #   컵·키까지 넣어 인포카드가 사라진 뒤에도 스펙을 계속 볼 수 있게 한다.
    meas = ""
    if m["bust"] and m["waist"] and m["hip"]:   # ★B/W/H 글자만 브랜드색(위 인포카드와 동일 규칙)
        meas = (f'<span class=sz><i>B</i>{m["bust"]}<b>·</b>'
                f'<i>W</i>{m["waist"]}<b>·</b><i>H</i>{m["hip"]}</span>')
    ck = " ".join(x for x in [f'{m["cup"]}컵' if m["cup"] else "",
                              f'{m["height"]}cm' if m["height"] else ""] if x)
    sub = " · ".join(x for x in [m["release"], f'★ {m["star"]}' if m["star"] else "",
                                 meas, ck] if x)
    return f"""<!doctype html><meta charset=utf-8><style>{_FONTS}
*{{margin:0;box-sizing:border-box}}html,body{{background:transparent}}
.f{{width:1920px;height:1080px;position:relative;background:transparent;
 font-family:'Paperlogy','Pretendard','Malgun Gothic',sans-serif;font-weight:700}}
.panel{{position:absolute;top:44px;left:48px;display:flex;align-items:center;gap:20px;
 background:rgba(10,6,10,.78);border:1.5px solid rgba(255,255,255,.18);
 border-radius:22px;padding:16px 28px 16px 16px;box-shadow:0 8px 26px rgba(0,0,0,.5)}}
.face{{width:148px;height:148px;border-radius:14px;background-position:center top;
 background-image:{face_bg};flex:0 0 auto;box-shadow:0 2px 8px rgba(0,0,0,.5)}}
.col{{display:flex;flex-direction:column;gap:9px;align-items:flex-start}}
.code{{font-family:'Black Han Sans',sans-serif;font-size:50px;line-height:1;
 color:#fff;letter-spacing:.01em}}
.name{{font-size:34px;line-height:1;color:{t['accent']};letter-spacing:-.01em}}
.sub{{font-size:26px;line-height:1;color:#c9c9d4;letter-spacing:-.01em;font-weight:400}}
.sub .sz{{color:#fff;font-weight:700}}
.sub .sz i{{color:{t['c1']};font-style:normal;font-weight:700}}
.sub .sz b{{color:{t['c1']};font-weight:700;margin:0 2px}}
</style><div class=f>
 <div class=panel>
  <div class=face></div>
  <div class=col>
   <div class=code>{_h(m["code"])}</div>
   <div class=name>{_h(m["actress"])}</div>
   <div class=sub>{sub}</div>
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
        b = p.chromium.launch(executable_path=CHROME) if CHROME else p.chromium.launch()
        for name, (html, transp) in pages.items():
            f = os.path.join(outdir, f"_L_{name}.html")
            with open(f, "w", encoding="utf-8") as fp: fp.write(html)
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
                              capture_output=True, text=True, timeout=FFPROBE_TIMEOUT).stdout
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
        subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT)
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

# ────────────────────────────── 투명 오버레이 영상(알파 채널) ──────────────────────────────
CANVAS_W, CANVAS_H = 1920, 1080

# 알파 보존 코덱 — 프리미어/캡컷에 얹어 쓰는 용도.
# ※ libvpx-vp9(webm) 알파는 이 ffmpeg 빌드에서 조용히 알파가 빠지므로 쓰지 않는다.
ALPHA_ENC = {
    # ProRes 4444: 편집기 호환성 최고. 화질 무손실급이나 용량 큼(1080p 6초 ≈ 16MB)
    "mov":   (["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"],
              "ProRes 4444"),
    # QuickTime Animation(RLE): 무손실 + 알파. 단색·투명 위주 배너는 ProRes의 1/9 용량
    "qtrle": (["-c:v", "qtrle", "-pix_fmt", "argb"], "QuickTime Animation(RLE)"),
}


def probe_duration(path):
    """영상 길이(초). 실패 시 None."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True,
            timeout=FFPROBE_TIMEOUT)
        return float(out.stdout.strip())
    except Exception:
        return None


def compose_alpha(layers, out_path, duration=6.0, hold=2.0, fade=0.5, fps=30,
                  fmt="mov", log=print):
    """가운데가 투명한 오버레이 '영상' 생성 — 원본 영상 없이 레이어만 렌더.
    프리미어프로/캡컷 타임라인에 얹으면 아래 트랙 영상이 그대로 비친다(번인 불필요).

    레이어 합성은 compose()와 동일한 타이밍:
      frame(항상) + info(0초 등장 → hold 후 페이드아웃) + wm(info 사라질 즈음 등장 → 유지)
    """
    fmt = (fmt or "mov").lower()
    if fmt not in ALPHA_ENC:
        raise ValueError(f"지원하지 않는 알파 포맷: {fmt} (mov|webm)")
    enc_args, enc_name = ALPHA_ENC[fmt]
    dur = float(duration or 6.0)

    # ffmpeg의 overlay 필터는 main을 '배경'으로 취급해 출력 알파를 항상 불투명으로 만든다.
    # → 색상은 검은 배경 위 overlay로 합성하고, 알파는 각 레이어의 알파를 screen(=Porter-Duff
    #   over 알파 공식 a+b-ab)으로 합집합해 따로 만든 뒤 alphamerge로 다시 붙인다.
    #   검은 배경 위 합성색은 premultiplied 이므로 unpremultiply로 스트레이트 알파에 맞춘다.
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i",
           f"color=c=black:s={CANVAS_W}x{CANVAS_H}:r={fps}:d={dur}",
           "-loop", "1", "-i", layers["frame"],
           "-loop", "1", "-i", layers["info"],
           "-loop", "1", "-i", layers["wm"]]
    fc = (
        # 레이어별 페이드(알파 포함) → 색/알파 두 갈래로 분기
        "[1]split[c1][k1];"
        f"[2]fade=t=in:st=0:d=0.4:alpha=1,"
        f"fade=t=out:st={hold}:d={fade}:alpha=1,split[c2][k2];"
        f"[3]fade=t=in:st={hold + fade * 0.2}:d={fade}:alpha=1,split[c3][k3];"
        # 알파 합집합
        "[k1]alphaextract[a1];[k2]alphaextract[a2];[k3]alphaextract[a3];"
        "[a1][a2]blend=all_mode=screen[a12];[a12][a3]blend=all_mode=screen[av];"
        # 색상 합성
        "[0][c1]overlay=0:0[b1];[b1][c2]overlay=0:0[b2];"
        "[b2][c3]overlay=0:0,format=gbrp[rgb];"
        # 알파 재결합 + 스트레이트 알파 보정
        "[rgb][av]alphamerge,unpremultiply=inplace=1[out]"
    )
    cmd += ["-filter_complex", fc, "-map", "[out]",
            "-r", str(fps), "-t", str(dur)] + enc_args + [out_path]
    log(f"[infocard] 투명 오버레이 영상({enc_name}, {dur:.1f}s) 인코딩 중…")
    subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT)
    return out_path

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
             assets_only=True, preview_anim=True,
             alpha=False, alpha_format="qtrle", alpha_duration=None, fps=30):
    """품번 → 인포배너 오버레이 소스 생성.
    assets_only=True(기본): 인코딩 없이 오버레이 PNG(프레임/인포카드/워터마크) +
       미리보기 스틸 2장만 생성 → 편집 프로그램에 얹어 사용. (초 단위, 재인코딩 안 함)
    assets_only=False: (옵션) 실제 mp4까지 합성(느림/재인코딩).
    alpha=True: 가운데 투명한 오버레이 '영상'({code}_오버레이.mov) 생성 →
       프리미어프로/캡컷에 얹으면 아래 영상이 비침. 원본 재인코딩 없어 빠름.
       alpha_duration 미지정 시 video 길이(있으면) → 없으면 hold+4초."""
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
              "preview_anim": prev_anim, "layers": layers, "meta": m, "out": None,
              "overlay": None}

    # 투명 오버레이 영상(편집기에 얹는 용도) — 원본 영상 건드리지 않음
    if alpha:
        dur = alpha_duration or (probe_duration(video) if video else None) or (hold + 4.0)
        ov = os.path.join(outdir, f"{code}_오버레이.mov")   # 두 코덱 모두 .mov 컨테이너
        try:
            compose_alpha(layers, ov, duration=dur, hold=hold, fps=fps,
                          fmt=alpha_format, log=log)
            result["overlay"] = ov
        except Exception as e:
            log(f"[infocard] 투명 오버레이 영상 실패: {e}")

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
    ap.add_argument("--alpha", action="store_true",
                    help="가운데 투명한 오버레이 영상 생성(프리미어/캡컷에 얹는 용도)")
    ap.add_argument("--alpha-format", default="qtrle", choices=["mov", "qtrle"],
                    help="qtrle=QuickTime Animation(기본, 무손실+알파, 용량 1/9) | "
                         "mov=ProRes4444(호환성 최고, 용량 큼)")
    ap.add_argument("--duration", type=float, default=None,
                    help="오버레이 영상 길이(초). 미지정 시 --video 길이 또는 hold+4초")
    ap.add_argument("--fps", type=int, default=30, help="오버레이 영상 fps")
    args = ap.parse_args()

    try:
        m = fetch_meta(args.code)
    except MetaNotFound as e:
        raise SystemExit(str(e))     # CLI에서는 깔끔히 종료
    print(f"[gen_infocard] {m['code']} / {m['actress']} / {m['title']}")

    outdir = args.outdir or os.path.join(tempfile.gettempdir(), f"infocard_{args.code}")
    os.makedirs(outdir, exist_ok=True)
    layers = render_layers(m, outdir)
    print("[gen_infocard] layers:", {k: os.path.basename(v) for k, v in layers.items()})
    if args.layers_only:
        return

    # 투명 오버레이 영상만 뽑고 종료 — 번인(재인코딩) 안 함
    if args.alpha:
        dur = args.duration or (probe_duration(args.video) if args.video else None) \
              or (args.hold + 4.0)
        ov = args.out or os.path.join(outdir, f"{args.code}_오버레이.mov")
        compose_alpha(layers, ov, duration=dur, hold=args.hold, fps=args.fps,
                      fmt=args.alpha_format)
        print(f"[gen_infocard] done → {ov}  (투명 오버레이 — 편집기 상위 트랙에 얹으세요)")
        return

    out = args.out or (
        os.path.splitext(args.video)[0] + "_banner.mp4" if args.video
        else os.path.join(outdir, f"{args.code}_demo.mp4"))
    compose(layers, out, video=args.video, hold=args.hold)
    print(f"[gen_infocard] done → {out}")

if __name__ == "__main__":
    main()
