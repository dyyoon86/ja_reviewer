#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""영화 시상식 스타일 배너 생성 — 각 폴더에 PNG 저장"""
import sys, json, math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

sys.stdout.reconfigure(encoding="utf-8")

OUT_DIR = Path.home() / "ja_reviewer_out"
W, H = 1920, 1080

# ── 색상 팔레트 (Cinema Gold) ────────────────────────────────────────────────
BG_TOP      = (8,   8,  20)
BG_BTM      = (18,  14,  35)
GOLD        = (212, 175,  55)
GOLD_LIGHT  = (255, 223, 100)
GOLD_DARK   = (140, 110,  20)
WHITE       = (255, 255, 255)
WHITE_DIM   = (200, 200, 220)
SILVER      = (192, 192, 210)
RED_DIM     = (220,  60,  60)
BLUE_DIM    = ( 60, 140, 220)
DARK_PANEL  = ( 0,   0,  10, 210)   # RGBA
ACCENT_LINE = (180, 140,  40)

# ── 폰트 로드 ────────────────────────────────────────────────────────────────
def load_font(size, bold=False):
    names = (
        ["malgunbd.ttf", "malgunsl.ttf", "malgun.ttf"] if bold
        else ["malgunsl.ttf", "malgun.ttf", "NanumGothic.ttf"]
    )
    extra = ["arial.ttf", "arialbd.ttf"]
    for name in names + extra:
        for base in [
            Path(r"C:\Windows\Fonts"),
            Path(r"C:\Windows\Fonts"),
        ]:
            p = base / name
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size)
                except Exception:
                    pass
    return ImageFont.load_default()


# ── 작품 데이터 ──────────────────────────────────────────────────────────────
WORKS = [
    # code, actress, label, series_tag, stars, views, likes, dislikes
    ("JUR-088",  "쿠온 미오",      "Madonna",        "마돈나 시리즈", 4, 312_847, 11_423, 892),
    ("JUR-762",  "사츠키 메이",    "Madonna",        "마돈나 시리즈", 5, 487_291, 24_817, 623),
    ("SNOS-213", "하야사카 카논",  "S1 NO.1 STYLE",  "에로스각성",   5, 203_558, 9_741,  471),
    ("SNOS-256", "쿠라키 하나",    "S1 NO.1 STYLE",  "S1 시리즈",    4, 178_234, 7_382,  543),
    ("SNOS-257", "미츠 코노하",    "S1 NO.1 STYLE",  "S1 신인",      4, 143_912, 5_628,  318),
    ("SNOS-275", "카와키타 사이카","S1 NO.1 STYLE",  "S1 시리즈",    4, 167_445, 6_914,  402),
    ("SNOS-282", "유메노 아이카",  "S1 NO.1 STYLE",  "S1 시리즈",    4, 194_337, 8_256,  517),
    ("SNOS-285", "시라카미 에미카","S1 NO.1 STYLE",  "S1 시리즈",    4, 158_623, 6_143,  389),
    ("START-585","혼조 스즈",      "SOD スター",      "SOD 스타",     4, 229_874, 9_832,  741),
    ("START-597","미야지마 메이",  "SOD スター",      "SOD 스타",     3, 98_412,  2_973,  634),
]


def fmt_num(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)


def draw_gradient_bg(draw, w, h):
    for y in range(h):
        t = y / h
        r = int(BG_TOP[0] + (BG_BTM[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BTM[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BTM[2] - BG_TOP[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))


def draw_gold_line(draw, y, w, thick=3):
    # 금색 그라데이션 가로선
    for i, col in enumerate([GOLD_DARK, GOLD, GOLD_LIGHT, GOLD, GOLD_DARK]):
        xi = int(i * w / 4)
        xe = int((i + 1) * w / 4)
        draw.rectangle([(xi, y), (xe, y + thick - 1)], fill=col)


def draw_film_holes(draw, x_start, y_start, n=20, size=16, gap=42, color=(30, 25, 55)):
    for i in range(n):
        y = y_start + i * gap
        draw.rounded_rectangle(
            [(x_start, y), (x_start + size, y + size * 0.6)],
            radius=3, fill=color
        )


def draw_star_rating(draw, x, y, stars, max_stars=5, font_size=36):
    fnt = load_font(font_size, bold=True)
    filled = "★" * stars
    empty  = "☆" * (max_stars - stars)
    draw.text((x, y), filled, font=fnt, fill=GOLD)
    tw = draw.textlength(filled, font=fnt)
    draw.text((x + tw, y), empty, font=fnt, fill=(80, 70, 40))


def draw_badge(draw, img, x, y, text, fnt, bg=(30, 20, 60), border=GOLD):
    tw = draw.textlength(text, font=fnt)
    pad = 14
    rect = [(x, y), (x + tw + pad*2, y + fnt.size + pad)]
    # 배경
    overlay = Image.new("RGBA", img.size, (0,0,0,0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle(rect, radius=6, fill=(*bg, 220))
    od.rounded_rectangle(rect, radius=6, outline=border, width=2)
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0,0))
    draw.text((x + pad, y + pad//2), text, font=fnt, fill=GOLD_LIGHT)
    return tw + pad*2 + 12  # 다음 badge x 오프셋


def make_banner(code, actress, label, series_tag, stars, views, likes, dislikes, out_path):
    img = Image.new("RGB", (W, H), BG_TOP)
    draw = ImageDraw.Draw(img)

    # ── 배경 그라데이션 ──
    draw_gradient_bg(draw, W, H)

    # ── 필름 홀 장식 (좌우) ──
    for xi in [12, W-38]:
        draw_film_holes(draw, xi, 0, n=28, size=16, gap=40, color=(30, 25, 55))

    # ── 중앙 패널 (반투명) ──
    panel = Image.new("RGBA", (W, H), (0,0,0,0))
    pd = ImageDraw.Draw(panel)
    pd.rectangle([(80, 60), (W-80, H-60)], fill=(0, 0, 15, 160))
    img = Image.alpha_composite(img.convert("RGBA"), panel).convert("RGB")
    draw = ImageDraw.Draw(img)

    # ── 상단 금선 ──
    draw_gold_line(draw, 90, W, thick=4)
    draw_gold_line(draw, 100, W, thick=1)

    # ── 하단 금선 ──
    draw_gold_line(draw, H - 200, W, thick=1)
    draw_gold_line(draw, H - 196, W, thick=4)

    # ── 코너 장식 ──
    corner_size = 30
    for cx, cy, dx, dy in [(90,90,1,1),(W-90,90,-1,1),(90,H-90,1,-1),(W-90,H-90,-1,-1)]:
        draw.line([(cx, cy), (cx + dx*corner_size, cy)], fill=GOLD, width=2)
        draw.line([(cx, cy), (cx, cy + dy*corner_size)], fill=GOLD, width=2)

    # ── "6월 마지막주 신작" 상단 뱃지 ──
    fnt_badge = load_font(26, bold=True)
    fnt_small = load_font(28)
    fnt_mid   = load_font(42)
    fnt_large = load_font(90, bold=True)
    fnt_xl    = load_font(120, bold=True)
    fnt_code  = load_font(52)
    fnt_stats = load_font(38)
    fnt_stat_label = load_font(28)

    # 헤더 뱃지들
    badge_y = 115
    bx = 110
    bx += draw_badge(draw, img, bx, badge_y, "6월 마지막주 신작", fnt_badge, bg=(60, 20, 10), border=(220, 100, 40))
    draw = ImageDraw.Draw(img)  # img 갱신 후 draw 재생성
    bx += draw_badge(draw, img, bx, badge_y, series_tag, fnt_badge, bg=(20, 40, 80), border=GOLD)
    draw = ImageDraw.Draw(img)

    # 레이블 (우측 상단)
    label_w = draw.textlength(label, font=fnt_small)
    draw.text((W - 110 - label_w, badge_y + 4), label, font=fnt_small, fill=SILVER)

    # ── 중앙 콘텐츠 ──
    cx = W // 2

    # 품번 (code)
    code_w = draw.textlength(code, font=fnt_code)
    draw.text((cx - code_w//2, 210), code, font=fnt_code, fill=SILVER)

    # 배우명 (메인 타이틀)
    name_w = draw.textlength(actress, font=fnt_xl)
    # 그림자
    draw.text((cx - name_w//2 + 3, 313), actress, font=fnt_xl, fill=(20, 15, 40))
    draw.text((cx - name_w//2, 310), actress, font=fnt_xl, fill=WHITE)

    # 금색 서브라인
    draw_gold_line(draw, 455, W, thick=2)

    # 별점
    star_text = "★" * stars + "☆" * (5 - stars)
    star_w = draw.textlength(star_text[:stars], font=fnt_large)
    empty_w = draw.textlength(star_text[stars:], font=fnt_large)
    total_star_w = star_w + empty_w
    sx = cx - total_star_w // 2
    draw.text((sx, 470), "★" * stars, font=fnt_large, fill=GOLD)
    draw.text((sx + int(star_w), 470), "☆" * (5 - stars), font=fnt_large, fill=(60, 50, 30))

    # 별점 숫자
    rating_val = 3.0 + stars * 0.38  # stars 기반 환산 (3.38~4.90)
    rating_str = f"{min(rating_val, 5.0):.1f} / 5.0"
    rv_w = draw.textlength(rating_str, font=fnt_mid)
    draw.text((cx - rv_w//2, 580), rating_str, font=fnt_mid, fill=GOLD_LIGHT)

    # ── 하단 통계 바 ──
    stats_y = H - 178

    # 배경 바
    draw.rectangle([(80, stats_y - 10), (W - 80, H - 62)], fill=(0, 0, 10, 0))

    # 구분선
    def stat_col(icon, label_text, value, x, color=WHITE_DIM):
        iw = draw.textlength(icon, font=fnt_stats)
        draw.text((x, stats_y + 2), icon, font=fnt_stats, fill=color)
        draw.text((x + iw + 8, stats_y + 2), value, font=fnt_stats, fill=WHITE)
        draw.text((x + iw//2 - draw.textlength(label_text, font=fnt_stat_label)//2 + 4,
                   stats_y + 48), label_text, font=fnt_stat_label, fill=(140, 130, 160))

    # 통계 4개 균등 배치
    stat_items = [
        ("▶", "조회수",  fmt_num(views),   SILVER),
        ("♥", "좋아요",  fmt_num(likes),   (100, 200, 100)),
        ("✕", "싫어요",  fmt_num(dislikes),(180,  80,  80)),
    ]
    total_items = len(stat_items)
    col_w = (W - 200) // (total_items + 1)
    for i, (icon, lbl, val, col) in enumerate(stat_items):
        stat_col(icon, lbl, val, 110 + col_w * (i + 0) + col_w * i // 3, col)

    # 오른쪽: AI 평점
    ai_label = "AI 추천도"
    ai_val   = f"{min(rating_val, 5.0):.1f}"
    ai_x = W - 300
    draw.text((ai_x, stats_y - 6), "AI", font=load_font(24, bold=True), fill=GOLD)
    draw.text((ai_x + 36, stats_y - 8), ai_label, font=fnt_stat_label, fill=GOLD)
    draw.text((ai_x, stats_y + 16), ai_val, font=load_font(72, bold=True), fill=GOLD_LIGHT)
    draw.text((ai_x + draw.textlength(ai_val, font=load_font(72, bold=True)) + 6,
               stats_y + 42), "/ 5.0", font=fnt_stats, fill=GOLD_DARK)

    # ── 하단 금선 ──
    draw_gold_line(draw, H - 68, W, thick=2)
    draw_gold_line(draw, H - 64, W, thick=1)

    # ── 저장 ──
    img.save(str(out_path), "PNG", optimize=True)
    print(f"  저장: {out_path.name} ({W}x{H})")


def main():
    for code, actress, label, series_tag, stars, views, likes, dislikes in WORKS:
        folder = OUT_DIR / code
        if not folder.exists():
            print(f"[{code}] 폴더 없음 — SKIP")
            continue
        out_path = folder / f"{code}_banner.png"
        print(f"[{code}] {actress} 배너 생성 중...")
        try:
            make_banner(code, actress, label, series_tag, stars, views, likes, dislikes, out_path)
        except Exception as e:
            import traceback
            print(f"  ERROR: {e}")
            traceback.print_exc()

    print("\n완료")


if __name__ == "__main__":
    main()
