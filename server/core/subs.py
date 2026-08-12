#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""⑥ 자막 굽기(ASS 하드섭) + 인포배너 오버레이."""
import json
import subprocess
from pathlib import Path

from .common import FFMPEG_TIMEOUT, _part_path, _finalize, srt_parse, video_duration, video_wh
from .cutter import has_nvenc, _vcodec_args

def _ass_color(hexstr, alpha="00"):
    """#RRGGBB → ASS &HAABBGGRR. alpha는 "80" 같은 hex 문자열 또는 0~255 정수.
    ASS alpha는 00=불투명, FF=완전투명이다(반대로 알기 쉬우니 주의)."""
    if isinstance(alpha, (int, float)):
        alpha = f"{max(0, min(255, int(alpha))):02X}"
    alpha = str(alpha)[:2].zfill(2)
    h = str(hexstr or "").lstrip("#")
    if len(h) != 6:
        return f"&H{alpha}FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()


def _ass_time(t):
    t = max(0.0, t); h = int(t // 3600); m = int(t % 3600 // 60); s = int(t % 60); cs = int(round((t - int(t)) * 100))
    if cs >= 100:
        s += 1; cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


_ALIGN = {("bottom", "left"): 1, ("bottom", "center"): 2, ("bottom", "right"): 3,
          ("middle", "left"): 4, ("middle", "center"): 5, ("middle", "right"): 6,
          ("top", "left"): 7, ("top", "center"): 8, ("top", "right"): 9}

# 기본 스타일 — 대사(하단 흰), 내레이션=기본(대사 바로 위 노랑), 강조/정보(현재 미사용)
# anim: 등장 효과 — none | fade | pop | punch | slide  (_ass_anim 참고)
#
# ★내레이션 위치 (2026-07-13 사용자 피드백): 예전엔 화면 '상단'이라 대사(하단)와 멀리
#   떨어져 시선이 위아래로 튀었다 → **대사 바로 위**(하단 정렬, 대사 한 줄 높이만큼 띄움).
#   대사 margin 46 + 대사 2줄 높이(42*2.2≈92) = 138 → 대사가 2줄로 늘어도 안 겹친다.
STYLE_DEFAULT = {
    "dialogue":  {"font": "Malgun Gothic", "size": 42, "color": "#FFFFFF", "outline_color": "#000000",
                  "outline": 2.2, "shadow": 0.4, "bold": True, "v": "bottom", "h": "center", "margin": 46,
                  "anim": "none"},
    "dialogue_m": {"font": "Malgun Gothic", "size": 42, "color": "#7FD0FF", "outline_color": "#000000",
                   "outline": 2.2, "shadow": 0.4, "bold": True, "v": "bottom", "h": "center", "margin": 46,
                   "anim": "none"},
    # 내레이션 (2026-07-13 사용자 지정): 페이퍼로지 · 흰 글씨 · **노란 둥근 배경판** · 투명도 30%
    #   ★ASS는 박스(BorderStyle=3) 모서리를 둥글게 못 한다 → plate=True면 둥근 사각형을
    #     드로잉 이벤트로 직접 그려 자막 아래 레이어에 깐다(_plate_events).
    #   plate_alpha 0x4D ≈ 30% 투명(00=불투명, FF=완전투명).
    "narration": {"font": "Paperlogy 7 Bold", "size": 38, "color": "#FFFFFF",
                  "outline_color": "#000000", "outline": 1.4, "shadow": 0.0, "bold": True,
                  "v": "bottom", "h": "center", "margin": 138, "anim": "pop",
                  "plate": True, "plate_color": "#FFD400", "plate_alpha": 0x4D,
                  "plate_radius": 16, "plate_pad_x": 24, "plate_pad_y": 8},
    # ★ 강조·정보 = 무도식(예능) 자막 (2026-07-13 — 사용자 지정: '무한도전식')
    #   기존엔 '색만 다른 글씨'라 아무 효과가 없었다. 예능 자막의 3요소를 넣는다:
    #     ① 두꺼운 외곽선 + 그림자로 배경과 분리 (어떤 화면에서도 읽힌다)
    #     ② 임팩트 등장 애니메이션(쾅 박히고 부르르)
    #     ③ 정보자막은 **반투명 박스 배경**(border_style=3) — 괄호 상황설명 톤
    #   강조는 대사·내레이션(하단)과 겹치지 않게 화면 중상단에 크게 띄운다.
    # 강조 = 노랑 + 두꺼운 검정 외곽, 화면 중앙에 크게 '쾅' 박힌다(smash).
    #   대사(하단 흰)·내레이션(하단 노랑 작게)과 크기·위치로 확실히 구분된다.
    "emphasis":  {"font": "Malgun Gothic", "size": 66, "color": "#FFE500",
                  "outline_color": "#141414", "outline": 6.0, "shadow": 3.0, "bold": True,
                  "v": "middle", "h": "center", "margin": 40, "spacing": 1,
                  "anim": "flame", "sfx": "impact"},
    # 정보 = 상단 중앙 **반투명 검정 박스 + 흰 글씨**, 위에서 툭 떨어진다(drop).
    #   ★BorderStyle=3에서는 outline 값이 '박스 여백'이고 박스 색은 outline_color다
    #     (back_color는 그림자) — 0으로 두면 판이 안 생긴다. 9 정도가 적당.
    #   상황설명은 괄호로 감싸는 게 이 톤의 관습: ( 친구 커플의 술자리 )
    "info":      {"font": "Malgun Gothic", "size": 34, "color": "#FFFFFF",
                  "outline_color": "#0D0D0D", "outline": 9.0, "shadow": 0.0, "bold": True,
                  "v": "top", "h": "center", "margin": 34,
                  "border_style": 3, "back_color": "#000000", "back_alpha": 0x30,
                  "anim": "shimmer", "sfx": "blip"},
}

# LLM이 붙이는 내레이션 유형 → ASS 스타일명
STYLE_TAGNAME = {"기본": "Narration", "일반": "Narration", "강조": "Emphasis", "정보": "Info",
                 "normal": "Narration", "emphasis": "Emphasis", "info": "Info"}
# 대사 화자 → ASS 스타일명 (여=기본 Dialogue, 남=DialogueM)
SPEAKER_TAGNAME = {"여": "Dialogue", "여자": "Dialogue", "f": "Dialogue", "female": "Dialogue",
                   "남": "DialogueM", "남자": "DialogueM", "m": "DialogueM", "male": "DialogueM"}

# ────────────────────────── 1080p 리프레임용 스타일 ──────────────────────────
# STYLE_DEFAULT는 720p 캔버스 기준이다. 리프레임(1920x1080)에 그대로 쓰면 글자가
# 절반 크기로 보인다 → 기하 값만 1.5배(=1080/720)로 키운다.
_SCALE_KEYS = ("size", "margin", "outline", "shadow",
               "plate_radius", "plate_pad_x", "plate_pad_y", "spacing")

# ★내레이션만 단순 확대가 아니다. ja12 v3(2026-07-18)부터 사용자가 확정한 납품 규격 =
#   **69px 검정 글씨 + 불투명 노란판**(720p의 흰 글씨 + 반투명판과 다른 look).
#   1.5배만 하면 57px 흰 글씨/반투명이 되어 지금까지 낸 ja12~ja18과 달라진다.
#   config sub_styles_1080 으로 편별 덮어쓰기 가능.
STYLE_1080_OVERRIDE = {
    "narration": {"size": 69, "color": "#111111", "outline": 0, "plate_alpha": 0},
}


def wm_to_topright(png, margin=24, suffix="_tr"):
    """워터마크 PNG의 내용을 우상단으로 옮긴 사본 경로를 돌려준다(없으면 원본 경로).

    gen_infocard의 html_wm은 패널을 **좌상단**(top:44px;left:48px)에 그린다. 그런데
    ja12 v3부터 납품본은 우상단이 규격이다 — 인트로 인포카드가 왼쪽에서 뜨고 워터마크가
    그 자리에 남으면 시선이 한쪽에 몰린다. HTML을 고치는 대신 다 그려진 PNG의 내용을
    bbox로 떠서 반대쪽에 붙인다(배너 생성을 다시 안 해도 되고, 메타 조회도 불필요).
    margin은 1920 기준이고 실제 PNG 폭에 비례로 환산한다.
    """
    from pathlib import Path as _P
    from PIL import Image
    src = _P(png)
    dst = src.with_name(src.stem + suffix + src.suffix)
    if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        return str(dst)                      # 원본이 그대로면 사본 재사용
    im = Image.open(src).convert("RGBA")
    bb = im.getbbox()
    if not bb:
        return str(src)                      # 내용이 없다 — 손댈 것 없음
    content = im.crop(bb)
    m = round(margin * im.width / 1920)
    canvas = Image.new("RGBA", im.size, (0, 0, 0, 0))
    canvas.paste(content, (im.width - content.width - m, m))
    canvas.save(dst)
    return str(dst)


def scale_styles(styles=None, factor=1080 / 720, override=None):
    """자막 스타일을 캔버스 배율만큼 키운 완전한 dict로 돌려준다(원본 불변).

    글자·여백·외곽선처럼 픽셀로 재는 값만 곱하고 색·폰트·애니는 그대로 둔다.
    """
    out = {}
    for name, base in STYLE_DEFAULT.items():
        st = {**base, **((styles or {}).get(name) or {})}
        for k in _SCALE_KEYS:
            v = st.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                st[k] = round(v * factor, 2)
        st.update((override or {}).get(name) or {})
        out[name] = st
    # STYLE_DEFAULT에 없는 사용자 정의 스타일도 흘려보낸다
    for name, st in (styles or {}).items():
        out.setdefault(name, dict(st or {}))
    return out


def _style_line(name, st):
    """ASS Style 한 줄.
    border_style: 1=외곽선(기본) / 3=**불투명 박스 배경**(무도식 정보자막의 핵심 —
      텍스트 뒤에 반투명 판을 깔아 배경이 복잡해도 읽힌다. BackColour가 그 판 색).
    back_color/back_alpha: 박스(또는 그림자) 색·투명도. angle: 글자 기울기(도)."""
    st = {**STYLE_DEFAULT["dialogue"], **(st or {})}
    align = _ALIGN.get((st.get("v", "bottom"), st.get("h", "center")), 2)
    bs = int(st.get("border_style", 1))
    back = _ass_color(st.get("back_color", "#000000"), st.get("back_alpha", 0x64))
    # alpha: 글자(또는 배경판) 투명도. ASS는 00=불투명, FF=투명
    return (f"Style: {name},{st.get('font','Malgun Gothic')},{int(st.get('size',40))},"
            f"{_ass_color(st.get('color','#FFFFFF'), st.get('alpha', '00'))},&H000000FF,"
            f"{_ass_color(st.get('outline_color','#000000'))},{back},"
            f"{-1 if st.get('bold') else 0},0,0,0,100,100,"
            f"{st.get('spacing',0)},{st.get('angle',0)},{bs},"
            f"{st.get('outline',2)},{st.get('shadow',0)},{align},40,40,{int(st.get('margin',40))},1")


def _ass_anim(kind, dur_ms):
    """자막 등장 효과 → ASS 인라인 오버라이드 태그. \\t(t1,t2,...)의 시각은 이벤트 시작 기준(ms).
    none    : 없음(그냥 뜸)
    pop     : 작게 나타나 살짝 커졌다가(오버슈트) 제자리 — '휙' 튀어나오는 느낌
    punch   : pop보다 강하게. 강조 문구용
    fade    : 부드럽게 페이드
    slide   : 아래에서 살짝 밀려 올라옴
    ─ 무도식(예능) 자막용 (2026-07-13) ─
    smash   : 크게 들어와 쾅 박히고 미세하게 흔들린다 — 임팩트 강조의 정석
    shake   : 좌우로 부르르 떨림(짧게 3회) — 놀람·경악
    drop    : 위에서 툭 떨어져 살짝 튕김 — 상황 설명 등장
    stamp   : 도장처럼 기울어져 찍힌다(회전+축소) — 판정·낙인
    typein  : 좌→우로 쓸려 나타남(\\clip 애니) — 정보자막 타이핑 느낌
    """
    if kind == "pop":
        return r"{\fad(0,120)\fscx70\fscy70\t(0,110,\fscx108\fscy108)\t(110,190,\fscx100\fscy100)}"
    if kind == "punch":
        return (r"{\fad(0,140)\fscx40\fscy40\t(0,90,\fscx118\fscy118)"
                r"\t(90,170,\fscx94\fscy94)\t(170,240,\fscx100\fscy100)}")
    if kind == "fade":
        return r"{\fad(180,180)}"
    if kind == "slide":
        return r"{\fad(0,120)\fscy60\t(0,150,\fscy105)\t(150,230,\fscy100)}"
    if kind == "smash":
        # 180%에서 쾅 → 92% 반동 → 100%. 마지막에 미세 흔들림(회전 ±1.5도)
        return (r"{\fad(0,120)\fscx180\fscy180\blur3"
                r"\t(0,70,\fscx92\fscy92\blur0)\t(70,130,\fscx104\fscy104)"
                r"\t(130,180,\fscx100\fscy100)"
                r"\t(180,240,\frz1.5)\t(240,300,\frz-1.5)\t(300,350,\frz0)}")
    if kind == "shake":
        return (r"{\fad(0,100)\fscx105\fscy105\t(0,80,\fscx100\fscy100)"
                r"\t(80,140,\frz2)\t(140,200,\frz-2)\t(200,260,\frz1)\t(260,320,\frz0)}")
    if kind == "drop":
        # 위에서 떨어지는 느낌 — \move는 절대좌표가 필요해 세로 스케일+블러로 대체
        return (r"{\fad(0,110)\fscy40\blur2\t(0,110,\fscy112\blur0)"
                r"\t(110,180,\fscy94)\t(180,240,\fscy100)}")
    if kind == "stamp":
        return (r"{\fad(0,90)\fscx160\fscy160\frz-9\blur4"
                r"\t(0,90,\fscx100\fscy100\frz-3\blur0)\t(90,150,\frz0)}")
    if kind == "typein":
        # 좌→우 쓸어 나타남. \clip 사각형을 가로로 넓힌다(PlayRes 기준 넉넉히)
        return (r"{\fad(0,80)\clip(0,0,0,2000)\t(0,260,\clip(0,0,2000,2000))}")
    if kind == "soft":
        # 흐릿하게 번져 있다가 부드럽게 맺힌다 — 정보 자막용(사용자 지정: '부드럽게 나타나는')
        return r"{\fad(320,260)\blur8\fscy94\t(0,340,\blur0\fscy100)}"
    if kind == "shimmer":
        # soft + 은은한 일렁임(블러·크기가 파도처럼 미세하게 오르내린다)
        return (r"{\fad(320,260)\blur8\fscy94"
                r"\t(0,340,\blur0\fscy100)"
                r"\t(600,1100,\blur1.2\fscx101\fscy101)"
                r"\t(1100,1600,\blur0\fscx100\fscy100)"
                r"\t(1600,2100,\blur1.2\fscx101\fscy101)"
                r"\t(2100,2600,\blur0\fscx100\fscy100)}")
    if kind == "flame":
        # 불꽃이 일렁이듯 — 글자가 미세하게 흔들리며 외곽이 번졌다 선명해졌다 반복
        return (r"{\fad(0,120)\fscx170\fscy170\blur4"
                r"\t(0,70,\fscx96\fscy96\blur0)\t(70,130,\fscx103\fscy103)"
                r"\t(130,180,\fscx100\fscy100)"
                r"\t(200,340,\blur1.6\frz1.2)\t(340,480,\blur0\frz-1.2)"
                r"\t(480,620,\blur1.6\frz0.8)\t(620,760,\blur0\frz0)}")
    return ""


_FONT_CACHE = {}


def _font_file(name, bold=True):
    """ASS 폰트명 → 실제 TTF 경로(윈도우 Fonts 폴더). 글자 폭을 재려면 파일이 필요하다."""
    key = (name, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    import glob
    import os
    fd = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    base = str(name or "").replace(" ", "")
    cands = []
    for p in glob.glob(os.path.join(fd, "*.ttf")) + glob.glob(os.path.join(fd, "*.otf")):
        stem = os.path.basename(p).rsplit(".", 1)[0].replace(" ", "").replace("-", "")
        if stem.lower() == base.replace("-", "").lower():
            cands.insert(0, p)
        elif base.lower() and base.split("-")[0].lower() in stem.lower():
            cands.append(p)
    _FONT_CACHE[key] = cands[0] if cands else None
    return _FONT_CACHE[key]


def _text_width(text, font_name, size, bold=True):
    """자막 한 줄의 픽셀 폭(대략). PIL로 실제 폰트를 재고, 실패하면 글자수로 추정한다."""
    try:
        from PIL import ImageFont
        fp = _font_file(font_name, bold)
        if fp:
            f = ImageFont.truetype(fp, int(size))
            return f.getbbox(text)[2] - f.getbbox(text)[0]
    except Exception:
        pass
    # 폴백: 한글은 대략 size, 영문/숫자는 size*0.55
    w = 0.0
    for ch in text:
        w += size if ord(ch) > 0x1100 else size * 0.55
    return w


def _rounded_rect_drawing(w, h, r):
    """ASS 드로잉 명령(\\p1) — 모서리 둥근 사각형. 좌표는 **(0,0)~(w,h)** 절대좌표.
    ASS 박스(BorderStyle=3)는 **모서리가 각지고 둥글게 못 한다** → 직접 그린다.
    ★드로잉은 \\an7(좌상단 기준)+\\pos와 함께 써야 위치가 예측대로 잡힌다
      (중심 기준 좌표로 그렸더니 판이 왼쪽 위로 밀렸다 — 2026-07-13 실측).
    베지어(b)로 네 모서리를 굴린다."""
    x0, y0, x1, y1 = 0.0, 0.0, w, h
    r = max(0.0, min(r, w / 2, h / 2))
    k = r * 0.5523   # 원에 가까운 베지어 손잡이 길이
    return (
        f"m {x0 + r:.0f} {y0:.0f} "
        f"l {x1 - r:.0f} {y0:.0f} "
        f"b {x1 - r + k:.0f} {y0:.0f} {x1:.0f} {y0 + r - k:.0f} {x1:.0f} {y0 + r:.0f} "
        f"l {x1:.0f} {y1 - r:.0f} "
        f"b {x1:.0f} {y1 - r + k:.0f} {x1 - r + k:.0f} {y1:.0f} {x1 - r:.0f} {y1:.0f} "
        f"l {x0 + r:.0f} {y1:.0f} "
        f"b {x0 + r - k:.0f} {y1:.0f} {x0:.0f} {y1 - r + k:.0f} {x0:.0f} {y1 - r:.0f} "
        f"l {x0:.0f} {y0 + r:.0f} "
        f"b {x0:.0f} {y0 + r - k:.0f} {x0 + r - k:.0f} {y0:.0f} {x0 + r:.0f} {y0:.0f}")


def _plate_events(rows, st, w, h, style_name):
    """자막 뒤에 깔 '둥근 배경판' 이벤트들 — rows=[(start, end, text[, cy])].

    ASS는 박스 모서리를 둥글게 못 하므로(BorderStyle=3은 각진 사각형), 배경판을
    **드로잉 이벤트로 직접 그려** 자막보다 아래 레이어에 깐다.
    글자 폭은 실제 폰트로 재서(PIL) 텍스트에 딱 맞는 판을 만든다.
    row에 cy(세로 중심, px)가 있으면 스타일 위치 대신 그 지점 중앙에 판을 깐다
    (배너 구간 중앙 이동용 — 텍스트의 \\an5\\pos와 같은 좌표 기준).
    """
    if not st.get("plate"):
        return []
    pad_x = float(st.get("plate_pad_x", 22))
    pad_y = float(st.get("plate_pad_y", 8))
    radius = float(st.get("plate_radius", 14))
    size = int(st.get("size", 38))
    font = st.get("font", "Malgun Gothic")
    v = st.get("v", "bottom")
    margin = float(st.get("margin", 40))
    line_h = size * 1.25          # libass 줄높이 ≈ 폰트크기의 1.2~1.3배
    out = []
    for row in rows:
        a, b, text = row[0], row[1], row[2]
        cy = row[3] if len(row) > 3 else None
        lines = str(text).split("\\N")
        tw = max((_text_width(ln, font, size, st.get("bold", True)) for ln in lines), default=0)
        text_h = line_h * len(lines)
        bw = tw + pad_x * 2
        bh = text_h + pad_y * 2
        # 판의 좌상단 좌표(\an7 기준) — 텍스트 블록을 감싸도록 패딩만큼 바깥으로.
        #   libass는 MarginV를 '화면 끝 ~ 텍스트 끝' 거리로 쓴다.
        x = (w - bw) / 2
        if cy is not None:
            y = cy - text_h / 2 - pad_y
        elif v == "top":
            y = margin - pad_y
        elif v == "middle":
            y = (h - bh) / 2
        else:
            y = h - margin - text_h - pad_y
        draw = _rounded_rect_drawing(bw, bh, radius)
        out.append((a, b, style_name,
                    f"{{\\an7\\pos({x:.0f},{y:.0f})\\p1}}{draw}{{\\p0}}"))
    return out


def screen_fx(events, w, h, log=print, intensity=0.16):
    """자막 등장 순간 **화면 자체**에 거는 효과 — ffmpeg 필터 조각을 돌려준다.

    왜 — 자막만 커지고 색이 바뀌는 건 '색만 다른 글씨'와 다를 게 없다(사용자 피드백).
    강조가 박히는 순간 화면이 붉게 확 달아올랐다 식으면 타격감이 생긴다.

    events=[(time_sec, kind)] — kind='red'(붉은 마스킹 플래시)
    구현: 전체 화면 빨간 drawbox를 알파를 낮춰가며 짧게 여러 겹 깔아 감쇠를 만든다
      (drawbox는 시간에 따른 알파 보간이 안 되므로 계단식으로 흉내낸다).

    ★강도 주의(2026-07-13 실측): 알파 0.30·0.30초로 했더니 **화면이 통째로 새빨개져
      영상이 안 보였다**. 확 달아올랐다 바로 식어야 타격감이 나온다
      → 0.16에서 시작해 0.22초 만에 사라지도록 낮췄다. intensity로 조절 가능.
    """
    base = [(0.00, 0.05, 1.00), (0.05, 0.10, 0.62),
            (0.10, 0.16, 0.31), (0.16, 0.22, 0.12)]
    parts = []
    n = 0
    for t, kind in (events or []):
        if kind != "red":
            continue
        for s0, s1, k in base:
            a = round(intensity * k, 3)
            if a < 0.01:
                continue
            parts.append(
                f"drawbox=x=0:y=0:w={w}:h={h}:color=red@{a}:t=fill:"
                f"enable='between(t,{t + s0:.3f},{t + s1:.3f})'")
        n += 1
    if n:
        log(f"화면 효과: 붉은 플래시 {n}개 (강조 자막이 박히는 순간, 강도 {intensity})")
    return ",".join(parts)


# 배너(인포카드) 구간 중앙 표시 위치 — 화면 높이 비율.
#   인포카드는 하단(좌)을 크게 덮으므로 하단 자막이 가려진다. 워터마크(우상단)·프레임
#   테두리를 피한 안전지대 = 화면 중앙부. 내레이션을 대사보다 위에 둔다(평소 배치와 동일).
BANNER_NAR_CY = 0.40
BANNER_DLG_CY = 0.54


def _banner_reflow(evs, banner_end):
    """인포카드(0~banner_end)가 하단을 덮는 동안의 자막 처리 — **지우지 않고 중앙으로 옮긴다**.

    ja12까지는 겹치는 자막을 통째로 드롭해서(_reburn_1080.suppress_banner) 오프닝
    내레이션("이번 작품은~")이 음성만 나오고 자막이 사라졌다 (2026-07-19 사용자 지적).
    → 배너 구간과 겹치는 이벤트: 통째면 중앙 표시, 걸치면 분할(배너 중=중앙, 이후=제자리).
    반환: (s, e, style, text, in_banner, cont) — cont=True는 분할 후반부(등장 애니 생략)."""
    if not banner_end or banner_end <= 0:
        return [(s, e, st, t, False, False) for s, e, st, t in evs]
    out = []
    for s, e, st, t in evs:
        if s >= banner_end:                    # 배너 뒤 → 그대로
            out.append((s, e, st, t, False, False))
        elif e <= banner_end + 0.4:            # 통째로(거의) 배너 안 → 중앙 표시
            out.append((s, e, st, t, True, False))
        elif s >= banner_end - 0.3:            # 끝자락에 살짝 걸침 → 시작만 뒤로 밀기
            out.append((banner_end, e, st, t, False, False))
        else:                                  # 크게 걸침 → 배너 끝에서 분할
            out.append((s, banner_end, st, t, True, False))
            out.append((banner_end, e, st, t, False, True))
    return out


def build_ass(dialogue, narration, out_ass, width, height, styles=None, banner_end=None):
    styles = styles or {}
    S = {k: {**STYLE_DEFAULT[k], **(styles.get(k) or {})}
         for k in ("dialogue", "dialogue_m", "narration", "emphasis", "info")}
    # 스타일 태그명 → 애니 종류
    ANIM = {"Dialogue": S["dialogue"].get("anim", "none"),
            "DialogueM": S["dialogue_m"].get("anim", "none"),
            "Narration": S["narration"].get("anim", "none"),
            "Emphasis": S["emphasis"].get("anim", "none"),
            "Info": S["info"].get("anim", "none")}
    L = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
         "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
         "[V4+ Styles]",
         "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
         "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
         "Alignment, MarginL, MarginR, MarginV, Encoding",
         _style_line("Dialogue", S["dialogue"]),
         _style_line("DialogueM", S["dialogue_m"]),
         _style_line("Narration", S["narration"]),
         _style_line("Emphasis", S["emphasis"]),
         _style_line("Info", S["info"])]
    # 둥근 배경판 전용 스타일 — 판 색은 PrimaryColour로 칠한다(드로잉은 1차색으로 채워짐).
    #   plate_alpha: 0=불투명 … 255=완전투명. 사용자가 말하는 '투명도 30%' = alpha 약 0x4D.
    for key, sname in (("narration", "NarPlate"), ("emphasis", "EmpPlate"), ("info", "InfoPlate")):
        st = S[key]
        if st.get("plate"):
            L.append(_style_line(sname, {
                **st, "color": st.get("plate_color", "#FFE500"),
                "outline": 0, "shadow": 0, "border_style": 1,
                "alpha": st.get("plate_alpha", 0x4D)}))
    L += ["", "[Events]",
          "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    evs = []
    for it in dialogue:                        # (s,e,text) 또는 (s,e,text,speaker)
        spk = it[3] if len(it) > 3 else "여"
        evs.append((it[0], it[1], SPEAKER_TAGNAME.get(spk, "Dialogue"), it[2]))
    for it in narration:                       # (s,e,text) 또는 (s,e,text,style)
        tag = it[3] if len(it) > 3 else "기본"
        evs.append((it[0], it[1], STYLE_TAGNAME.get(tag, "Narration"), it[2]))
    evs.sort(key=lambda x: x[0])
    evs = _banner_reflow(evs, banner_end)      # → (s,e,style,text,in_banner,cont)

    # 배경판 이벤트 — Layer 0(자막보다 아래). 자막은 Layer 1로 올린다.
    PLATE_OF = {"Narration": ("narration", "NarPlate"),
                "Emphasis": ("emphasis", "EmpPlate"),
                "Info": ("info", "InfoPlate")}
    for style, (skey, sname) in PLATE_OF.items():
        if not S[skey].get("plate"):
            continue
        rows = [(s, e, str(t).replace("\n", "\\N"),
                 height * BANNER_NAR_CY if (inb and style == "Narration") else None)
                for s, e, stl, t, inb, _c in evs if stl == style]
        for a, b, _sn, body in _plate_events(rows, S[skey], width, height, sname):
            L.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},{sname},,0,0,0,,{body}")

    for s, e, style, t, inb, cont in evs:
        txt = str(t).replace("\n", "\\N")
        tag = _ass_anim("none" if cont else ANIM.get(style, "none"), int((e - s) * 1000))
        # 배너 구간 → 하단 스타일만 중앙으로(강조=원래 중앙, 정보=상단이라 안 겹침)
        if inb and style in ("Dialogue", "DialogueM"):
            tag += f"{{\\an5\\pos({width / 2:.0f},{height * BANNER_DLG_CY:.0f})}}"
        elif inb and style == "Narration":
            tag += f"{{\\an5\\pos({width / 2:.0f},{height * BANNER_NAR_CY:.0f})}}"
        L.append(f"Dialogue: 1,{_ass_time(s)},{_ass_time(e)},{style},,0,0,0,,{tag}{txt}")
    Path(out_ass).write_text("\n".join(L) + "\n", encoding="utf-8")
    return out_ass


"""배너 모션 기본값 — 브라우저 미리보기(/preview/data)와 반드시 같은 값을 쓴다."""
BANNER_ANIM = {"hold": 4.0, "fade": 0.5, "blur": 16, "wm_start": 4.1, "wm_slide": 40}


def _prep_banner_layers(banner, workdir, blur=16, vid_wh=None):
    """레이어 PNG를 굽기용으로 준비 — 내용 크기로 crop(오버레이 비용↓) + 인포카드 블러본 1회 생성.
    ffmpeg에서 gblur을 매 프레임 돌리면 수십 배 느려지므로 블러는 여기서 미리 굽는다.
    vid_wh=(w,h)를 주면 배너 캔버스(1920×1080)를 영상 해상도에 맞춰 스케일한다
    — 720p 영상에 1080p 좌표로 얹으면 인포카드가 화면 밖으로 잘리는 버그 방지.
    반환: {키: (경로, x, y)}  x,y = 영상 좌표계 위치"""
    try:
        from PIL import Image, ImageFilter
    except Exception:
        return {}
    out = {}
    pad = int(blur * 2.5)      # 블러가 가장자리에서 잘리지 않도록 여유
    for k in ("frame", "info", "wm"):
        p = banner.get(k)
        if not p or not Path(p).is_file():
            continue
        im = Image.open(p).convert("RGBA")
        s = 1.0
        if vid_wh and im.width and im.height:
            s = min(vid_wh[0] / im.width, vid_wh[1] / im.height)
        bb = im.getbbox()
        if not bb:
            continue
        if k == "info":        # 블러 번짐 여유를 두고 자른다
            bb = (max(0, bb[0] - pad), max(0, bb[1] - pad),
                  min(im.width, bb[2] + pad), min(im.height, bb[3] + pad))
        crop = im.crop(bb)
        if abs(s - 1.0) > 1e-3:
            crop = crop.resize((max(1, round(crop.width * s)),
                                max(1, round(crop.height * s))), Image.LANCZOS)
        cp = Path(workdir) / f"_bn_{k}.png"
        crop.save(cp)
        out[k] = (cp.name, round(bb[0] * s), round(bb[1] * s))
        if k == "info":
            bp = Path(workdir) / "_bn_info_blur.png"
            crop.filter(ImageFilter.GaussianBlur(round(blur * s) or 1)).save(bp)
            out["info_blur"] = (bp.name, round(bb[0] * s), round(bb[1] * s))
    return out


def _banner_filter(prep, anim):
    """배너 오버레이 filter_complex 조각. 미리보기와 동일 타이밍.
    인포카드: 페이드인(흐림→선명) → hold → 페이드아웃(선명→흐림)  워터마크: 페이드인+슬라이드.
    · blend=all_expr(픽셀별 수식)은 매우 느려서, 미리 구운 블러본과의 크로스디졸브로 대체
    · 애니메이션이 끝난 뒤엔 enable로 오버레이 자체를 꺼서 남은 구간 비용을 없앤다"""
    hold, fade = float(anim.get("hold", 2.0)), float(anim.get("fade", 0.5))
    wm_st, slide = float(anim.get("wm_start", 2.1)), float(anim.get("wm_slide", 40))
    end = hold + fade + 0.1
    inputs, fc, idx, last = [], "", 1, "0:v"
    order = [k for k in ("frame", "info_blur", "info", "wm") if k in prep]
    labels = {}
    for k in order:
        name, x, y = prep[k]
        inputs += ["-loop", "1", "-i", name]
        labels[k] = (idx, x, y)
        idx += 1
    # 레이어별 알파 애니메이션
    if "info" in labels:
        fc += (f"[{labels['info'][0]}]fade=t=in:st=0:d=0.4:alpha=1,"
               f"fade=t=out:st={hold}:d={fade}:alpha=1[ic];")
    if "info_blur" in labels:   # 등장 초반·퇴장 후반에만 겹쳐 흐림 효과를 만든다
        fc += (f"[{labels['info_blur'][0]}]fade=t=out:st=0:d={fade}:alpha=1,"
               f"fade=t=in:st={hold}:d={fade}:alpha=1,"
               f"fade=t=out:st={hold + fade}:d=0.1:alpha=1[icb];")
    if "wm" in labels:
        fc += f"[{labels['wm'][0]}]fade=t=in:st={wm_st}:d={fade}:alpha=1[wm];"
    # 합성 — 프레임 → 흐린 인포카드 → 선명 인포카드 → 워터마크
    n = 0
    if "frame" in labels:
        _, x, y = labels["frame"]
        n += 1
        fc += f"[{last}][{labels['frame'][0]}]overlay={x}:{y}[b{n}];"; last = f"b{n}"
    for key, lbl in (("info_blur", "icb"), ("info", "ic")):
        if key in labels:
            _, x, y = labels[key]
            n += 1
            fc += f"[{last}][{lbl}]overlay={x}:{y}:enable='lt(t,{end})'[b{n}];"; last = f"b{n}"
    if "wm" in labels:
        _, x, y = labels["wm"]
        n += 1
        sp = slide / max(fade, 0.01)     # 슬라이드 속도(px/s)
        fc += (f"[{last}][wm]overlay=x={x}:"
               f"y='{y}+min(0,-{slide}+(t-{wm_st})*{sp})'[b{n}];"); last = f"b{n}"
    return inputs, fc, last


def _cutin_filter(cutins, w, h, start_idx, pos="tr", scale=0.26, fade=0.2):
    """상황별 짤(움짤) 오버레이 — (inputs, 필터조각들, 다음 입력번호).

    cutins=[(start, end, path)]. 각 짤은 별도 입력으로 붙이고 enable로 구간만 보여준다.
    · gif/webm/png의 투명 배경은 그대로 살린다(format=rgba 후 overlay).
    · 화면의 26% 크기로 줄여 구석에 얹는다(자막·배너를 안 가리게 기본은 우상단).
    · 등장/퇴장 0.2초 페이드 — 툭 튀어나오면 조악해 보인다.
    """
    STILL = (".png", ".webp", ".jpg", ".jpeg")
    inputs, parts = [], []
    idx = start_idx
    for k, (s, e, p) in enumerate(cutins):
        dur = max(0.3, float(e) - float(s))
        # 정지 이미지는 -loop 1(계속 같은 프레임), 움짤/영상은 -stream_loop -1(구간 내내 반복)
        if str(p).lower().endswith(STILL):
            inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(p)]
        else:
            inputs += ["-stream_loop", "-1", "-t", f"{dur:.3f}", "-i", str(p)]
        fi = min(fade, dur / 3)
        # 페이드는 짤 자신의 시간축(0부터)에서 걸고, 그 뒤 setpts로 등장 시각까지 밀어준다
        parts.append(
            f"[{idx}:v]scale={int(w * scale)}:-1,format=rgba,"
            f"fade=t=in:st=0:d={fi:.2f}:alpha=1,"
            f"fade=t=out:st={max(0.0, dur - fi):.2f}:d={fi:.2f}:alpha=1,"
            f"setpts=PTS-STARTPTS+{float(s):.3f}/TB[g{k}]")
        idx += 1
    return inputs, parts, idx


def burn_subs(video, dialogue_srt, narration_srt, out_video, styles=None,
              narration_json=None, dialogue_json=None, log=print,
              banner=None, banner_anim=None, subs=True, screen_flash=True,
              flash_intensity=0.16, cutins=None, cutin_pos="tr", cutin_scale=0.26):
    """자막(+선택: 배너·워터마크)을 영상에 굽는다.
    banner={'frame':png,'info':png,'wm':png} 를 주면 자막과 같은 인코딩 1패스에서
    함께 합성한다(따로 굽는 2패스 대비 인코딩 1회 절약).
    banner에서 키를 빼면 그 레이어는 빠진다(미리보기 체크 그대로 굽기).
    subs=False면 자막 없이 배너만 굽는다."""
    w, h = video_wh(video)
    if dialogue_json and Path(dialogue_json).is_file():        # 화자(speaker) 포함 대사
        dd = json.loads(Path(dialogue_json).read_text(encoding="utf-8"))
        dlg = [(float(d["start"]), float(d["end"]), d["text"], d.get("speaker", "여")) for d in dd]
    else:
        dlg = srt_parse(dialogue_srt) if dialogue_srt and Path(dialogue_srt).is_file() else []
    if narration_json and Path(narration_json).is_file():     # 유형(style) 포함 내레이션
        data = json.loads(Path(narration_json).read_text(encoding="utf-8"))
        nar = [(float(d["start"]), float(d["end"]), d["text"], d.get("style", "기본")) for d in data]
    else:
        nar = srt_parse(narration_srt) if narration_srt and Path(narration_srt).is_file() else []
    if subs and not dlg and not nar:
        raise RuntimeError("입힐 자막(SRT)이 없습니다.")
    if not subs and not banner:
        raise RuntimeError("자막·배너 둘 다 꺼져 있어 구울 게 없습니다.")
    ass_path = Path(out_video).with_suffix(".ass")
    if subs:
        # 인포카드가 같이 구워지면 그 구간(0~hold+fade)의 하단 자막은 중앙으로 이동
        b_end = 0.0
        if banner and banner.get("info") and Path(banner["info"]).is_file():
            a = banner_anim or BANNER_ANIM
            b_end = float(a.get("hold", BANNER_ANIM["hold"])) + \
                float(a.get("fade", BANNER_ANIM["fade"])) + 0.1
        build_ass(dlg, nar, str(ass_path), w, h, styles, banner_end=b_end)
        log(f"자막 굽기 (ffmpeg ass, {w}x{h}, 대사 {len(dlg)} · 내레이션 {len(nar)})...")
        if b_end:
            log(f"인포카드 {b_end:.1f}s 동안 겹치는 대사·내레이션은 화면 중앙으로 이동(가림 방지)")
    else:
        ass_path.parent.mkdir(parents=True, exist_ok=True)
        log(f"자막 없이 배너만 굽기 ({w}x{h})...")

    # 강조 자막이 박히는 순간 → 화면 붉은 플래시. 자막(ass)보다 **먼저** 걸어야
    # 자막까지 빨갛게 물들지 않는다(화면만 달아오르고 글자는 선명하게 남는다).
    flash = ""
    if screen_flash and subs:
        # 유형(style)은 narration_json에서만 온다(SRT는 3튜플이라 유형이 없다)
        ev = [(n[0], "red") for n in nar
              if len(n) >= 4 and STYLE_TAGNAME.get(n[3], "Narration") == "Emphasis"]
        flash = screen_fx(ev, w, h, log, intensity=float(flash_intensity))

    inputs, fc, last = [], "", "0:v"
    if banner:
        prep = _prep_banner_layers(banner, ass_path.parent,
                                   (banner_anim or {}).get("blur", BANNER_ANIM["blur"]),
                                   vid_wh=(w, h))
        if prep:
            inputs, fc, last = _banner_filter(prep, banner_anim or BANNER_ANIM)
            log(f"배너 동시 굽기: {', '.join(prep)} (재인코딩 1패스 — 추가 비용 적음)")

    # 짤(움짤) 오버레이 — 배너 입력 다음 번호부터 붙인다
    cut_inputs, cut_parts = [], []
    cuts = [(float(a), float(b), p) for a, b, p in (cutins or [])]
    if cuts:
        n_in = 1 + sum(1 for v in inputs if v == "-i")   # 0=원본, 그다음이 배너 입력들
        cut_inputs, cut_parts, _ = _cutin_filter(cuts, w, h, n_in,
                                                 pos=cutin_pos, scale=cutin_scale)
        log(f"짤 오버레이 {len(cuts)}개 ({cutin_pos}, 화면의 {int(cutin_scale * 100)}%)")

    if fc or flash or cut_parts:
        # 순서: 배너 → 화면 플래시 → 짤 → 자막(ass). 자막이 맨 위에 와야 안 가려진다.
        if not fc:
            fc = ""
            last = "0:v"
        head = f"[{last}]{flash + ',' if flash else ''}null[fx];"
        chain = head
        cur = "fx"
        for part in cut_parts:
            chain += part + ";"
        for k in range(len(cuts)):
            s, e, _p = cuts[k]
            nxt = f"cv{k}"
            chain += (f"[{cur}][g{k}]overlay=x={{X}}:y={{Y}}:"
                      f"enable='between(t,{s:.3f},{e:.3f})'[{nxt}];")
            cur = nxt
        # overlay 좌표는 _cutin_filter와 같은 규칙 — 여기서 치환
        POS = {"tr": (f"W-w-{int(w * 0.03)}", f"{int(h * 0.06)}"),
               "tl": (f"{int(w * 0.03)}", f"{int(h * 0.06)}"),
               "br": (f"W-w-{int(w * 0.03)}", f"H-h-{int(h * 0.22)}"),
               "bl": (f"{int(w * 0.03)}", f"H-h-{int(h * 0.22)}")}
        px, py = POS.get(cutin_pos, POS["tr"])
        chain = chain.replace("{X}", px).replace("{Y}", py)
        fc = fc + chain + (f"[{cur}]ass={ass_path.name}[out]" if subs
                           else f"[{cur}]null[out]")
        inputs = inputs + cut_inputs

    # -loop 1 로 넣은 배너 PNG는 무한 스트림이라 원본이 끝나도 인코딩이 계속된다.
    # 원본 길이로 명시적으로 끊어준다.
    dur = video_duration(video) if fc else 0.0

    tmp_out = _part_path(out_video)   # 중간에 죽어도 잘린 파일이 '완료'로 안 보이게

    def _cmd(use_gpu):
        enc = _vcodec_args(use_gpu)
        if fc:
            tail = (["-t", f"{dur:.3f}"] if dur > 0 else ["-shortest"])
            return ["ffmpeg", "-y", "-i", str(video)] + inputs + \
                   ["-filter_complex", fc, "-map", "[out]", "-map", "0:a?"] + enc + \
                   ["-c:a", "copy"] + tail + [tmp_out]
        return ["ffmpeg", "-y", "-i", str(video), "-vf", f"ass={ass_path.name}"] + enc + \
               ["-c:a", "copy", tmp_out]

    gpu = has_nvenc()
    # libass 필터는 파일명만(작업폴더 cwd로) → 윈도우 드라이브 콜론 이스케이프 회피
    try:
        try:
            subprocess.run(_cmd(gpu), cwd=str(ass_path.parent), check=True, timeout=FFMPEG_TIMEOUT)
        except subprocess.CalledProcessError:
            if not gpu:
                raise
            log("NVENC 실패 → libx264로 폴백")
            subprocess.run(_cmd(False), cwd=str(ass_path.parent), check=True, timeout=FFMPEG_TIMEOUT)
        _finalize(tmp_out, out_video)
    finally:
        for tmp in ass_path.parent.glob("_bn_*.png"):   # 배너 중간 산출물 정리
            try:
                tmp.unlink()
            except OSError:
                pass
        try:                                            # 실패로 남은 .part 제거
            if Path(tmp_out).is_file() and str(tmp_out) != str(out_video):
                Path(tmp_out).unlink()
        except OSError:
            pass
    log(f"자막 영상: {out_video}")
    return out_video

