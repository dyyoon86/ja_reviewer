# -*- coding: utf-8 -*-
r"""섹션4 인트로 — HyperFrames 컴포지션(HTML+GSAP) 생성기.

`section4_intro.py build` 가 호출한다. 대본 6줄이 곧 6개 씬이고, 각 씬의 시작·길이는
TTS 실측 길이(lines.json)에서 온다 — 화면이 말을 따라간다.

비주얼 방향은 `section4/design.md`("심야 랭킹 방송")를 따른다. 요약:
  · 지속 프레임 — 상단 러닝헤드 + 하단 순위 티커가 2씬부터 끝까지 유지(씬마다 리셋되지 않는다)
  · 좌측 정렬 편집 레이아웃, 큰 숫자만 오른쪽으로 bleed
  · 그레인 + 비네트 질감, 강조색은 핑크 하나
  · 하드컷 우선, 숫자는 오버슛으로 꽂힌다

  1 신작 500편 …            몽타주(가운데 띠만) + 0→500 카운터
  2 여러분들의 추천 수 …     좌측 카피 + 인포카드 그리드 + 집계
  3 1위는 46대 1 …           1위 카드 블러 + 스코어보드
  4 12위부터 1위까지 …       숫자 12→1 한 장씩 교체(티커가 같이 움직인다)
  5 끝까지 시청해 주세요     CTA
  6 먼저 12위입니다          꼴찌 카드 → 본편

HyperFrames 규칙: 등장 애니메이션만(마지막 씬 제외), 씬마다 track-index 분리,
난수·시계 금지, repeat:-1 금지, 타임라인 동기 생성 후 __timelines 등록.
"""
import re

BG0, BG1 = "#1c1116", "#0d0a0c"      # 라디얼 안쪽 / 바깥
C1 = "#ff2d55"                        # 핑크 — 솔리드 배경 위
C1T = "#ff98af"                       # 핑크 — 영상 위(밝은 프레임에서도 3:1, check 실측)
FG = "#ffffff"
SUB = "#b6acb4"
FONT = '"Pretendard Variable", sans-serif'
DISP = '"Paperlogy", "Pretendard Variable", sans-serif'

# 그레인 — feTurbulence SVG(결정적, JS 없음)
GRAIN = ("url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' "
         "width='180' height='180'><filter id='n'><feTurbulence type='fractalNoise' "
         "baseFrequency='0.85' numOctaves='3' stitchTiles='stitch'/></filter>"
         "<rect width='180' height='180' filter='url(%23n)' opacity='0.55'/></svg>\")")

CAP_LABEL = {"punch": "펀치줌+패닝", "type": "타자체", "slide": "슬라이드"}

NUM = re.compile(r"(\d+(?:,\d{3})*(?:위|편|개|분|초)?)")


def hl(text):
    """숫자는 자동으로, `*...*` 로 감싼 말은 수동으로 강조색을 입힌다.

    펀치라인(예: "*다른 것*도 빠질 뻔했습니다")은 숫자가 아니라서 자동으로는 안 잡힌다 —
    대본에서 사람이 직접 표시한다. TTS 입력에서는 이 표시를 지운다.
    """
    out = NUM.sub(lambda m: f'<span class="n">{m.group(1)}</span>', text)
    return re.sub(r"\*([^*]+)\*", lambda m: f'<span class="hi">{m.group(1)}</span>', out)


def slam_size(text):
    """슬램 글자 크기 — 한 줄에 들어가도록 글자 수에서 역산(1840px 폭 기준)."""
    n = max(1, len(text.replace(" ", "")) + text.count(" ") * 0.4)
    return int(max(90, min(215, 1840 / n)))


def chunk(text, max_chars=20):
    """한 문장을 화면에 끊어 띄울 조각으로 나눈다.

    ★멘트.txt 에 ` / ` 를 넣으면 **그 자리에서 끊는다**(사람이 정한 호흡이 우선).
      자동 분할은 어절 길이만 보고 자르기 때문에 "…눈도 빠지고 다른 / 것도…" 같은
      어색한 경계가 생긴다. TTS 입력에서는 이 표시를 지운다.

    47자를 한 덩어리로 띄우면 두 줄로 흘러 읽는 속도가 화면을 못 따라간다(초안5 피드백).
    쉼표·마침표를 1순위 경계로 자르고, 그래도 긴 조각은 어절 단위로 다시 쪼갠다.
    """
    if "/" in text:
        return [x.strip() for x in text.split("/") if x.strip()]
    parts, buf = [], ""
    for tok in re.split(r"(?<=[,.!?])\s*", text):
        if tok.strip():
            parts.append(tok.strip())
    out = []
    for part in parts:
        if len(part) <= max_chars:
            out.append(part)
            continue
        buf = ""
        for word in part.split(" "):
            cand = (buf + " " + word).strip()
            if len(cand) > max_chars and buf:
                out.append(buf)
                buf = word
            else:
                buf = cand
        if buf:
            out.append(buf)
    return out or [text]


def build(rows, lines, total, pool=500, has_audio=False, cap_style="punch",
          has_bgm=False):
    rows = sorted(rows, key=lambda r: r["rank"])
    desc = sorted(rows, key=lambda r: -r["rank"])          # 12위 → 1위 (재생 순서)
    top1 = rows[0] if rows else None
    last = desc[0] if desc else None
    likes = sum(r.get("likes") or 0 for r in rows)
    dis = sum(r.get("dislikes") or 0 for r in rows)
    views = sum(r.get("views") or 0 for r in rows)
    dur_total = round(total + 0.8, 2)

    def win(i):
        ln = lines[i - 1]
        st = max(0.0, ln["start"] - 0.3)
        en = ln["start"] + ln["dur"] + 0.35
        if i < len(lines):
            en = max(en, lines[i]["start"])
        return round(st, 2), round(en - st, 2)

    s = [win(i) for i in range(1, len(lines) + 1)]

    def sc(i, what):
        """i번째 씬의 start/dur. 대본에 그 줄이 없으면 None — 씬을 통째로 뺀다."""
        if i >= len(s):
            return None
        return s[i][0] if what == 0 else s[i][1]

    # 1씬 몽타주 — 납품본 가운데 띠만(구워진 배너·자막이 잘려 나간다)
    mont = [r for r in rows if r.get("anim")]
    seg = round(s[0][1] / max(1, len(mont)), 3) if mont else s[0][1]
    mont_html = "\n".join(
        f'    <video id="m{k}" class="clip mont" data-start="{round(s[0][0] + k * seg, 3)}" '
        f'data-duration="{seg}" data-media-start="{r.get("mstart", 0.5)}" data-track-index="0" '
        f'src="{r["anim"]}" muted playsinline></video>'
        for k, r in enumerate(mont))

    grid = "".join(
        f'<figure class="card"><img src="{r["card"]}" alt="">'
        f'<figcaption><b>{r["rank"]}</b><span>{r["actress"]}</span></figcaption></figure>'
        for r in desc)

    cds = "".join(
        f'<div class="cdi{" top" if r["rank"] == 1 else ""}" id="cdi{r["rank"]}">'
        f'<img src="{r["card"]}" alt="">'
        f'<div class="num">{r["rank"]}<i>위</i></div></div>'
        for r in desc)

    ticks = "".join(f'<span class="tk" id="tk{r["rank"]}">{r["rank"]}</span>' for r in desc)

    # 한 문장을 통째로 띄우면 두 줄로 흘러 읽기 전에 지나간다(초안5 피드백) —
    # 조각으로 끊어 순차 표시하고, 조각별 노출 시간은 글자 수에 비례해 나눈다.
    cap_items, caps = [], ""
    for ln in lines:
        pieces = chunk(ln["text"])
        span = sum(len(x) for x in pieces) or 1
        at = ln["start"]
        for j, piece in enumerate(pieces):
            d = round(ln["dur"] * len(piece) / span, 2)
            if j == len(pieces) - 1:
                d = round(ln["start"] + ln["dur"] - at, 2)
            cid = f'{ln["i"]}_{j}'
            # ★펀치라인(물음표로 끝나는 조각)은 화면을 통째로 덮는 슬램으로 띄운다.
            #   "싫어요 한 분 누구죠?" 처럼 받아치는 말은 자막 크기로는 안 산다.
            slam = piece.rstrip().endswith("?")
            cap_items.append((cid, round(at, 2), max(0.35, d), slam))
            caps += ('    <div id="cap%s" class="clip cap%s" data-start="%.2f" '
                     'data-duration="%.2f" data-track-index="12"%s>'
                     '<p%s>%s</p></div>\n' % (cid, ' slam' if slam else '', at, max(0.35, d),
                        ' data-layout-allow-overlap' if slam else '',
                        (' style="font-size:%dpx"' % slam_size(piece)) if slam else '',
                        hl(piece)))
            at += d

    audio = (f'    <audio id="nar" class="clip" data-start="0" data-duration="{dur_total}" '
             f'data-track-index="13" src="assets/narration.wav" data-volume="1"></audio>\n'
             if has_audio else "")
    # BGM 은 빌드에서 -26 LUFS 로 깔았고, 내레이션과 겹치는 구간을 위해 한 번 더 낮춘다
    audio += (f'    <audio id="bgm" class="clip" data-start="0" data-duration="{dur_total}" '
              f'data-track-index="14" src="assets/bgm.mp3" data-volume="0.55"></audio>\n'
              if has_bgm else "")

    # ★펀치라인 슬램이 화면을 통째로 덮는 동안 뒤 씬을 살려 두면 유령처럼 비친다.
    #   슬램이 시작되는 순간 그 줄의 씬을 끝낸다(화면을 넘겨준다).
    for idx, ln in enumerate(lines):
        first_slam = next((it[1] for it in cap_items
                           if it[0].startswith(f'{ln["i"]}_') and it[3]), None)
        if first_slam is not None and idx < len(s):
            st = s[idx][0]
            s[idx] = (st, round(max(0.5, first_slam - st), 2))

    open_html = ""
    if len(s) > 5:
        open_html = (
            f'      <div id="open" class="clip sheet" data-start="{s[5][0]}" '
            f'data-duration="{s[5][1]}" data-track-index="7">\n'
            f'        <div class="badge">{last['rank'] if last else 12}<i>위</i></div>\n'
            f'        <img src="{last['card'] if last else ''}" alt="">\n'
            f'      </div>\n')

    frame_from = round(max(0.0, s[1][0] - 0.3), 2)   # 러닝헤드·티커는 2씬부터
    tl = _timeline(s, rows, desc, lines, pool, dur_total, frame_from, cap_items,
                   likes, dis, views, cap_style)

    return f"""<!doctype html>
<html lang="ko">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=1920, height=1080" />
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
      /* 렌더러 자동 폰트 목록에 한글이 없다 — 프로젝트에 직접 번들 */
      @font-face {{ font-family:'Pretendard Variable'; font-weight:45 920; font-display:block;
        src:url('assets/fonts/PretendardVariable.woff2') format('woff2-variations'); }}
      @font-face {{ font-family:'Paperlogy'; font-weight:700; font-display:block;
        src:url('assets/fonts/Paperlogy-7Bold.woff2') format('woff2'); }}
      @font-face {{ font-family:'Paperlogy'; font-weight:800 900; font-display:block;
        src:url('assets/fonts/Paperlogy-8ExtraBold.woff2') format('woff2'); }}

      * {{ margin:0; padding:0; box-sizing:border-box; }}
      html, body {{ width:1920px; height:1080px; overflow:hidden; background:{BG1}; }}
      body {{ font-family:{FONT}; color:{FG}; font-variant-numeric:tabular-nums;
        -webkit-font-smoothing:antialiased; }}
      .clip {{ position:absolute; inset:0; }}

      /* ── 지속 레이어: 배경 / 비네트 / 그레인 */
      #bg {{ position:absolute; inset:0; z-index:0;
        background: radial-gradient(1400px 820px at 62% 34%, {BG0}, {BG1} 72%); }}
      /* 비네트는 **영상 위·그래픽 아래**(z 15). 위로 올리면 검사기가 전 텍스트를 '가려짐'으로 잡는다 */
      #vig {{ position:absolute; inset:0; z-index:15; pointer-events:none;
        background: radial-gradient(120% 90% at 50% 45%, rgba(0,0,0,0) 42%, rgba(0,0,0,.72) 100%); }}
      #grain {{ position:absolute; inset:0; z-index:61; pointer-events:none;
        opacity:.15; mix-blend-mode:overlay;
        background-image:{GRAIN}; background-size:180px 180px; }}

      /* ── 방송 프레임 */
      .runhead {{ position:absolute; left:74px; top:52px; right:auto; bottom:auto;
        width:max-content; height:max-content; display:flex; align-items:center; gap:18px;
        z-index:70; }}
      .runhead .bar {{ width:6px; height:34px; background:{C1}; border-radius:3px; }}
      .runhead .t {{ font-size:30px; font-weight:700; letter-spacing:.14em; color:{FG}; }}
      .runhead .t em {{ font-style:normal; color:{SUB}; font-weight:500; }}

      .ticker {{ position:absolute; left:74px; right:74px; bottom:48px; top:auto;
        height:max-content; display:flex; align-items:center; z-index:70; }}
      .ticker .rail {{ display:flex; gap:12px; align-items:flex-end; }}
      .tk {{ font-family:{DISP}; font-size:28px; font-weight:700; color:{SUB}; opacity:.45;
        letter-spacing:-.02em; min-width:44px; text-align:center;
        border-bottom:4px solid rgba(155,146,154,.22); padding-bottom:8px; }}
      .tk.on {{ color:{FG}; opacity:1; border-bottom-color:{C1}; }}

      /* ── 씬 공통: 좌측 정렬 편집 레이아웃 */
      .sheet {{ opacity:0; position:absolute; inset:0; z-index:20;
        display:flex; flex-direction:column; justify-content:center;
        padding:150px 110px 200px 118px; }}
      .lead {{ position:relative; padding-left:34px; }}
      .lead::before {{ content:''; position:absolute; left:0; top:8px; bottom:8px;
        width:8px; background:{C1}; border-radius:4px; }}
      .kicker {{ font-size:34px; font-weight:700; letter-spacing:.16em; color:{SUB};
        margin-bottom:16px; }}

      /* ── 1씬 */
      /* 납품본에는 배너가 구워져 있다 — 위 26% / 아래 22% 를 잘라 필름 스트립처럼 쓴다 */
      .mont {{ z-index:5; width:1920px; height:1080px; object-fit:cover;
        clip-path: inset(26% 0 22% 0);
        filter:brightness(.6) saturate(.9) contrast(1.05); }}
      /* 자막대(하단 128px)와 겹치지 않게 카운터 블록은 화면 중앙에 둔다(check 실측) */
      #s1 {{ justify-content:center; z-index:22; }}
      #s1 .kicker {{ color:{FG}; opacity:.9; }}
      #counter {{ font-family:{DISP}; font-size:330px; font-weight:900; line-height:.86;
        letter-spacing:-.05em; color:{FG}; text-shadow:0 14px 60px rgba(0,0,0,.9); }}
      #counter i {{ font-style:normal; font-size:120px; color:{C1T}; margin-left:14px; }}
      #flash {{ position:absolute; inset:0; background:#fff; opacity:0; z-index:59; }}

      /* ── 2씬 */
      #grid {{ flex-direction:row; align-items:center; gap:58px; }}
      #grid .side {{ flex:0 0 380px; }}
      #grid .big {{ font-family:{DISP}; font-size:132px; font-weight:900; line-height:.92;
        letter-spacing:-.05em; }}
      #grid .big em {{ font-style:normal; color:{C1}; }}
      #grid .stat {{ margin-top:30px; display:flex; flex-direction:column; gap:12px; }}
      #grid .stat div {{ font-size:34px; font-weight:700; color:{FG};
        text-shadow:0 3px 14px rgba(0,0,0,.95); }}
      #grid .stat b {{ font-family:{DISP}; font-weight:800; }}
      #grid .stat i {{ font-style:normal; color:#d3ccd1; font-weight:500; margin-right:14px;
        letter-spacing:.06em; }}
      #grid .wrap {{ flex:1; display:grid; grid-template-columns:repeat(4, 1fr); gap:13px; }}
      .card {{ position:relative; border-radius:10px; overflow:hidden;
        box-shadow:0 12px 34px rgba(0,0,0,.6); }}
      .card img {{ width:100%; display:block; }}
      .card figcaption {{ position:absolute; left:0; right:0; bottom:0; padding:26px 12px 9px;
        display:flex; align-items:baseline; gap:9px;
        background:linear-gradient(transparent, rgba(0,0,0,.92)); }}
      .card b {{ font-family:{DISP}; font-size:29px; font-weight:900; color:{C1}; }}
      .card span {{ font-size:20px; font-weight:600; color:{FG}; }}

      /* ── 3씬 */
      #top1 {{ flex-direction:row; align-items:center; gap:76px; }}
      #top1 .shot {{ position:relative; flex:0 0 800px; border-radius:14px; overflow:hidden;
        box-shadow:0 22px 70px rgba(0,0,0,.75); }}
      /* 너무 어두우면 카드인 줄도 모른다(초안4) — 형태는 보이되 얼굴은 안 보이는 선 */
      #top1 .shot img {{ width:100%; display:block;
        filter:blur(11px) brightness(.68); transform:scale(1.06); }}
      #top1 .qm {{ position:absolute; inset:0; display:flex; align-items:center;
        justify-content:center; font-family:{DISP}; font-size:290px; font-weight:900;
        color:{FG}; text-shadow:0 10px 46px rgba(0,0,0,.9); }}
      #top1 .score {{ font-family:{DISP}; font-size:220px; font-weight:900; line-height:.88;
        letter-spacing:-.05em; }}
      #top1 .score em {{ font-style:normal; color:{C1}; }}
      #top1 .score span {{ color:{SUB}; font-size:120px; margin:0 18px; }}
      #top1 .who {{ margin-top:20px; font-size:50px; font-weight:800; color:{FG}; }}
      #top1 .who u {{ text-decoration:none; color:{C1}; }}

      /* ── 4씬: 한 장씩 교체(나열 금지 — 초안1 실패) */
      #rush {{ padding:0; }}
      .cdi {{ position:absolute; inset:0; display:flex; align-items:center;
        justify-content:flex-end; padding-right:90px; opacity:0; }}
      /* 인포카드 좌상단에 채널 워터마크 알약이 박혀 있다 — 전체화면으로 깔면 러닝헤드와
         겹친다(초안4 실측). 위 15% 를 잘라 낸다 */
      .cdi img {{ position:absolute; left:0; top:0; width:1920px; height:1080px;
        object-fit:cover; clip-path:inset(15% 0 0 0); filter:brightness(.4) saturate(.85); }}
      .cdi .num {{ position:relative; font-family:{DISP}; font-size:400px; font-weight:900;
        line-height:.82; letter-spacing:-.06em; color:{FG};
        text-shadow:0 16px 70px rgba(0,0,0,.9); }}
      .cdi .num i {{ font-style:normal; font-size:145px; color:{C1T}; margin-left:6px; }}
      .cdi.top .num {{ color:{C1T}; }}

      /* ── 5씬 */
      #cta .big {{ font-family:{DISP}; font-size:165px; font-weight:900; line-height:.96;
        letter-spacing:-.05em; }}
      #cta .big em {{ font-style:normal; color:{C1}; }}
      #cta .sub {{ margin-top:26px; font-size:44px; color:{SUB}; font-weight:600;
        letter-spacing:.04em; }}

      /* ── 6씬 */
      #open {{ flex-direction:row; align-items:center; gap:64px; }}
      #open .badge {{ font-family:{DISP}; font-size:280px; font-weight:900; line-height:.86;
        letter-spacing:-.05em; color:{C1}; }}
      #open .badge i {{ font-style:normal; font-size:105px; color:{FG}; }}
      #open img {{ flex:1; min-width:0; border-radius:14px;
        box-shadow:0 22px 70px rgba(0,0,0,.75); }}

      /* ── 자막: 회색 박스 대신 좌측 정렬 + 핑크 세로 룰 */
      .cap {{ z-index:90; display:flex; align-items:flex-end; padding:0 120px 128px; }}
      .cap p {{ transform-origin:left center; will-change:transform, filter;
        position:relative; max-width:1480px; padding-left:30px; text-align:left;
        font-size:72px; font-weight:800; line-height:1.28; letter-spacing:-.02em; color:{FG};
        text-shadow:0 4px 18px rgba(0,0,0,.95), 0 0 5px rgba(0,0,0,.95); }}
      .cap p::before {{ content:''; position:absolute; left:0; top:10px; bottom:10px;
        width:7px; background:{C1}; border-radius:4px; }}
      .cap .n {{ color:{C1T}; }}
      /* 펀치라인 강조 — 대본의 *...* 표시 */
      .cap .hi {{ color:{C1T}; font-size:1.12em; text-shadow:0 0 34px rgba(255,45,85,.55),
        0 4px 18px rgba(0,0,0,.95); }}
      .cap.slam .hi {{ color:{C1}; }}

      /* ── 슬램 자막: 펀치라인이 화면을 통째로 덮는다(핑크로 때리고 어둡게 가라앉음) */
      .cap.slam {{ opacity:0; z-index:95; align-items:center; justify-content:center;
        padding:0; background:rgba(8,4,6,.86); }}
      /* 크기는 글자 수에 맞춰 생성기가 계산해 인라인으로 준다 — 고정값이면 한 글자가 흘러넘친다 */
      .cap.slam p {{ transform-origin:center center; max-width:1780px; padding:0 40px;
        white-space:nowrap; text-align:center; font-family:{DISP}; font-weight:900;
        line-height:1.0; letter-spacing:-.05em; color:{FG};
        text-shadow:0 18px 70px rgba(0,0,0,.9); }}
      .cap.slam p::before {{ display:none; }}
      .cap.slam .n {{ color:{C1}; }}
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="{dur_total}"
         data-width="1920" data-height="1080">

      <div id="bg"></div>

{mont_html}

      <div id="s1" class="clip sheet" data-start="{s[0][0]}" data-duration="{s[0][1]}"
           data-track-index="1">
        <div class="lead">
          <div class="kicker">9월 첫 주 신작</div>
          <div id="counter">0<i>편</i></div>
        </div>
      </div>
      <div id="flash" class="clip" data-start="{s[0][0]}" data-duration="{s[0][1]}"
           data-track-index="2"></div>

      <div id="grid" class="clip sheet" data-start="{s[1][0]}" data-duration="{s[1][1]}"
           data-track-index="3">
        <div class="side lead">
          <div class="kicker">여러분이 뽑은</div>
          <div class="big">최고의 <em>12편</em></div>
          <div class="stat">
            <div><i>좋아요</i><b id="stLike">0</b></div>
            <div><i>싫어요</i><b id="stDis">0</b></div>
            <div><i>조회수</i><b id="stView">0</b></div>
          </div>
        </div>
        <div class="wrap">{grid}</div>
      </div>

      <div id="top1" class="clip sheet" data-start="{s[2][0]}" data-duration="{s[2][1]}"
           data-track-index="4">
        <div class="shot" data-layout-allow-overflow>
          <img src="{top1['card'] if top1 else ''}" alt=""><div class="qm">?</div>
        </div>
        <div class="lead">
          <div class="kicker">이번 주 1위</div>
          <div class="score"><em>{top1['likes'] if top1 else 0}</em><span>:</span>{top1['dislikes'] if top1 else 0}</div>
          <div class="who" id="who">싫어요 한 분, <u>누구죠?</u></div>
        </div>
      </div>

      <div id="rush" class="clip sheet" data-start="{s[3][0]}" data-duration="{s[3][1]}"
           data-track-index="5">{cds}</div>

      <div id="cta" class="clip sheet" data-start="{s[4][0]}" data-duration="{s[4][1]}"
           data-track-index="6">
        <div class="lead">
          <div class="big"><em>끝까지</em> 시청해 주세요</div>
        </div>
      </div>

{open_html}

      <div id="runhead" class="clip runhead" data-start="{frame_from}"
           data-duration="{round(dur_total - frame_from, 2)}" data-track-index="9">
        <div class="bar"></div>
        <div class="t">9월 첫 주 <em>· JAV 신작 랭킹</em></div>
      </div>

      <div id="ticker" class="clip ticker" data-start="{frame_from}"
           data-duration="{round(dur_total - frame_from, 2)}" data-track-index="10">
        <div class="rail">{ticks}</div>
      </div>

{caps}{audio}
      <div id="vig" data-layout-ignore></div>
      <div id="grain" data-layout-ignore></div>
    </div>

    <script>
{tl}
    </script>
  </body>
</html>
"""


def _timeline(s, rows, desc, lines, pool, dur_total, frame_from, cap_items,
              likes, dis, views, cap_style="punch"):
    js = []
    a = js.append
    a("      window.__timelines = window.__timelines || {};")
    a("      const tl = gsap.timeline({ paused: true });")
    a("")
    a("      // ── 1씬: 카운터 0 → %d (몽타주는 가운데 띠만)" % pool)
    cnt_dur = max(0.9, s[0][1] * 0.58)
    a("      const cnt = { v: 0 }, cntEl = document.getElementById('counter');")
    a("      tl.to('#s1', { opacity: 1, duration: 0.12, ease: 'none' }, %.2f);" % s[0][0])
    a("      tl.to(cnt, { v: %d, duration: %.2f, ease: 'power2.out'," % (pool, cnt_dur))
    a("        onUpdate: () => { cntEl.innerHTML = Math.round(cnt.v) + '<i>편</i>'; } }, %.2f);"
      % (s[0][0] + 0.2))
    a("      tl.from('#s1 .kicker', { x: -30, opacity: 0, duration: 0.45, ease: 'power3.out' }, %.2f);"
      % (s[0][0] + 0.15))
    a("      tl.to('#counter', { scale: 1.05, transformOrigin: 'left center',"
      " duration: 0.16, ease: 'power2.out' }, %.2f);" % (s[0][0] + 0.2 + cnt_dur))
    a("      tl.to('#flash', { opacity: 0.9, duration: 0.05, ease: 'none' }, %.2f);"
      % (s[0][0] + 0.24 + cnt_dur))
    a("      tl.to('#flash', { opacity: 0, duration: 0.3, ease: 'power2.out' }, %.2f);"
      % (s[0][0] + 0.3 + cnt_dur))
    # 플래시 퇴장이 클립 경계에서 끝나면 되감기 때 흰 화면이 남는다(lint) — 하드 킬
    a("      tl.set('#flash', { opacity: 0 }, %.2f);" % (s[0][0] + 0.6 + cnt_dur))
    a("")
    a("      // ── 방송 프레임(2씬부터 끝까지 유지)")
    a("      tl.from('#runhead', { x: -40, opacity: 0, duration: 0.45, ease: 'expo.out' }, %.2f);"
      % frame_from)
    a("      tl.from('#ticker .tk', { y: 18, opacity: 0, duration: 0.3, ease: 'power2.out',"
      " stagger: 0.03 }, %.2f);" % (frame_from + 0.12))
    a("")
    a("      // ── 2씬: 좌측 카피 + 카드 그리드")
    a("      tl.to('#grid', { opacity: 1, duration: 0.12, ease: 'none' }, %.2f);" % s[1][0])
    a("      tl.from('#grid .kicker', { x: -26, opacity: 0, duration: 0.4, ease: 'power3.out' }, %.2f);"
      % (s[1][0] + 0.12))
    a("      tl.from('#grid .big', { x: -40, opacity: 0, duration: 0.5, ease: 'expo.out' }, %.2f);"
      % (s[1][0] + 0.2))
    a("      tl.from('#grid .card', { y: 46, opacity: 0, duration: 0.42,"
      " ease: 'back.out(1.5)', stagger: 0.042 }, %.2f);" % (s[1][0] + 0.3))
    a("      tl.from('#grid .stat div', { x: -22, opacity: 0, duration: 0.36,"
      " ease: 'power3.out', stagger: 0.1 }, %.2f);" % (s[1][0] + 0.95))
    # ★카드가 착지한 뒤 그대로 6초를 버티면 정지 화면이 된다(초안5 피드백).
    #   ① 집계 숫자 카운트업 ② 12위→1위 하이라이트 스윕 ③ 그리드 전체 서서히 밀어넣기
    a("      const st = { a: 0, b: 0, c: 0 };")
    a("      const stL = document.getElementById('stLike'),"
      " stD = document.getElementById('stDis'), stV = document.getElementById('stView');")
    a("      tl.to(st, { a: %d, b: %d, c: %d, duration: %.2f, ease: 'power2.out',"
      % (likes, dis, views, max(1.0, s[1][1] * 0.45)))
    a("        onUpdate: () => { stL.textContent = Math.round(st.a);"
      " stD.textContent = Math.round(st.b);"
      " stV.textContent = Math.round(st.c).toLocaleString('en-US'); } }, %.2f);"
      % (s[1][0] + 1.0))
    a("      tl.to('#grid .wrap', { scale: 1.045, transformOrigin: 'center center',"
      " duration: %.2f, ease: 'none' }, %.2f);" % (s[1][1], s[1][0]))
    sweep0 = s[1][0] + 1.45
    sweep_step = max(0.12, (s[1][1] - 2.0) / max(1, len(desc)))
    for k in range(len(desc)):
        at = sweep0 + k * sweep_step
        a("      tl.to('#grid .card:nth-child(%d)', { scale: 1.075, zIndex: 5,"
          " boxShadow: '0 0 0 4px %s, 0 18px 44px rgba(0,0,0,.75)',"
          " duration: 0.16, ease: 'power2.out' }, %.2f);" % (k + 1, C1, at))
        a("      tl.to('#grid .card:nth-child(%d)', { scale: 1, zIndex: 1,"
          " boxShadow: '0 12px 34px rgba(0,0,0,.6)',"
          " duration: 0.26, ease: 'power2.in' }, %.2f);" % (k + 1, at + 0.16))
    a("")
    a("      // ── 3씬: 1위 단독 + 스코어보드")
    a("      tl.to('#top1', { opacity: 1, duration: 0.12, ease: 'none' }, %.2f);" % s[2][0])
    a("      tl.from('#top1 .shot', { x: -80, opacity: 0, duration: 0.55, ease: 'expo.out' }, %.2f);"
      % (s[2][0] + 0.15))
    a("      tl.from('#top1 .qm', { scale: 0.55, opacity: 0, duration: 0.45,"
      " ease: 'back.out(2.2)' }, %.2f);" % (s[2][0] + 0.42))
    a("      tl.from('#top1 .kicker', { x: -24, opacity: 0, duration: 0.38, ease: 'power3.out' }, %.2f);"
      % (s[2][0] + 0.3))
    a("      tl.from('#top1 .score', { scale: 0.68, opacity: 0, transformOrigin: 'left center',"
      " duration: 0.5, ease: 'back.out(2.4)' }, %.2f);" % (s[2][0] + 0.5))
    a("      tl.from('#who', { y: 22, opacity: 0, duration: 0.4, ease: 'power3.out' }, %.2f);"
      % (s[2][0] + max(1.1, s[2][1] * 0.5)))
    a("")
    a("      // ── 4씬: 카운트다운 — 한 장씩 교체 + 티커 동기")
    a("      tl.to('#rush', { opacity: 1, duration: 0.12, ease: 'none' }, %.2f);" % s[3][0])
    t0 = s[3][0] + 0.25
    step = max(0.16, (s[3][1] - 0.9) / max(1, len(desc)))
    for k, r in enumerate(desc):
        at = t0 + k * step
        is_last = (k == len(desc) - 1)
        a("      tl.set('#cdi%d', { opacity: 1 }, %.2f);" % (r["rank"], at))
        a("      tl.set('#tk%d', { attr: { class: 'tk on' } }, %.2f);" % (r["rank"], at))
        a("      tl.fromTo('#cdi%d .num', { scale: 0.62, opacity: 0 },"
          " { scale: 1, opacity: 1, duration: %.2f, ease: 'back.out(2.4)' }, %.2f);"
          % (r["rank"], min(0.3, step * 0.7), at))
        if not is_last:
            a("      tl.set('#cdi%d', { opacity: 0 }, %.2f);" % (r["rank"], at + step))
            a("      tl.set('#tk%d', { attr: { class: 'tk' } }, %.2f);" % (r["rank"], at + step))
        else:
            a("      tl.to('#cdi%d img', { scale: 1.05, duration: %.2f, ease: 'power1.out' }, %.2f);"
              % (r["rank"], max(0.4, s[3][1] - (at - s[3][0])), at))
    a("")
    a("      // ── 5씬: CTA")
    if len(s) > 4:
        a("      tl.to('#cta', { opacity: 1, duration: 0.12, ease: 'none' }, %.2f);" % s[4][0])
        a("      tl.from('#cta .big', { x: -50, opacity: 0, duration: 0.5, ease: 'expo.out' }, %.2f);"
          % (s[4][0] + 0.14))
        pass
    a("")
    a("      // ── 6씬: 꼴찌 카드 → 본편 (있을 때만. 마지막 씬이라 퇴장 허용)")
    if len(s) > 5:
        a("      tl.to('#open', { opacity: 1, duration: 0.12, ease: 'none' }, %.2f);" % s[5][0])
        a("      tl.from('#open .badge', { x: -40, opacity: 0, duration: 0.45, ease: 'back.out(2)' }, %.2f);"
          % (s[5][0] + 0.12))
        a("      tl.from('#open img', { x: 90, opacity: 0, duration: 0.5, ease: 'expo.out' }, %.2f);"
          % (s[5][0] + 0.22))
        a("      tl.to('#open', { opacity: 0, duration: 0.3, ease: 'power2.in' }, %.2f);"
          % (s[5][0] + s[5][1] - 0.3))
    a("")
    if len(s) == 5:
        a("      tl.to('#cta', { opacity: 0, duration: 0.3, ease: 'power2.in' }, %.2f);"
          % (s[4][0] + s[4][1] - 0.3))
    a("      // ── 자막 — 조각 단위로 끊어 띄우고, 조각마다 %s 연출" % CAP_LABEL[cap_style])
    for cid, at, dur, slam in cap_items:
        if slam:
            # 화면 꽉 채우기 — 2.6배에서 0.14초 만에 꽂히고, 배경은 핑크로 때렸다 가라앉는다
            a("      tl.fromTo('#cap%s', { opacity: 0, backgroundColor: 'rgba(255,45,85,.92)' },"
              " { opacity: 1, duration: 0.06, ease: 'none' }, %.2f);" % (cid, at))
            a("      tl.to('#cap%s', { backgroundColor: 'rgba(8,4,6,.86)', duration: 0.28,"
              " ease: 'power2.out' }, %.2f);" % (cid, at + 0.06))
            a("      tl.fromTo('#cap%s p', { scale: 2.6, opacity: 0 },"
              " { scale: 1, opacity: 1, duration: 0.16, ease: 'expo.out' }, %.2f);" % (cid, at))
            a("      tl.to('#cap%s p', { scale: 1.04, duration: %.2f, ease: 'none' }, %.2f);"
              % (cid, max(0.3, dur - 0.2), at + 0.16))
            a("      tl.set('#cap%s', { opacity: 0 }, %.2f);" % (cid, at + dur))
            continue
        if cap_style == "type":
            # 타자체 — clip-path 를 오른쪽에서 걷어낸다(steps 로 글자 느낌, 시킹 안전)
            a("      tl.fromTo('#cap%s p', { clipPath: 'inset(0 100%% 0 0)' },"
              " { clipPath: 'inset(0 0%% 0 0)', duration: %.2f, ease: 'steps(14)' }, %.2f);"
              % (cid, min(0.55, dur * 0.55), at))
            a("      tl.fromTo('#cap%s p', { opacity: 0 }, { opacity: 1, duration: 0.08,"
              " ease: 'none' }, %.2f);" % (cid, at))
        elif cap_style == "slide":
            a("      tl.fromTo('#cap%s p', { y: 46, opacity: 0 },"
              " { y: 0, opacity: 1, duration: 0.26, ease: 'expo.out' }, %.2f);" % (cid, at))
            a("      tl.to('#cap%s p', { y: -34, opacity: 0, duration: 0.16,"
              " ease: 'power2.in' }, %.2f);" % (cid, at + dur - 0.16))
            a("      tl.set('#cap%s p', { opacity: 0 }, %.2f);" % (cid, at + dur))
        else:
            # 펀치줌 + 휙 패닝 — 오른쪽에서 흐릿하게 밀려들어와 딱 멈추고, 왼쪽으로 휙 빠진다
            a("      tl.fromTo('#cap%s p', { x: 90, scale: 1.16, opacity: 0,"
              " filter: 'blur(7px)' }, { x: 0, scale: 1, opacity: 1, filter: 'blur(0px)',"
              " duration: %.2f, ease: 'expo.out' }, %.2f);"
              % (cid, min(0.26, dur * 0.45), at))
            a("      tl.to('#cap%s p', { x: -70, scale: 0.97, opacity: 0,"
              " filter: 'blur(5px)', duration: %.2f, ease: 'power2.in' }, %.2f);"
              % (cid, min(0.16, dur * 0.3), at + dur - min(0.16, dur * 0.3)))
            # 퇴장이 클립 경계에서 끝나면 되감기·점프 시 잔상이 남는다 — 하드 킬 명시(lint 요구)
            a("      tl.set('#cap%s p', { opacity: 0 }, %.2f);" % (cid, at + dur))
    a("")
    a('      window.__timelines["main"] = tl;')
    return "\n".join(js)
