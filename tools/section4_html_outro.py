# -*- coding: utf-8 -*-
r"""섹션4 아웃트로 — HyperFrames 컴포지션(HTML+GSAP) 생성기.

`section4_outro.py build` 가 호출한다. 인트로(`section4_html.py`)와 **같은 디자인 언어**를 쓴다
(`section4/design.md` — "심야 랭킹 방송"). 색·폰트·자막 연출·그레인은 인트로에서 그대로 가져오고
씬 구성만 다르다.

★★대전제 — 랭킹의 좋아요/싫어요는 **퍼온 소스 사이트 수치**다. 이 영상 시청자가 누른 게 아니다.
  그러니 아웃트로에서 "여러분이 누른 게 순위가 됩니다" 류로 말하면 거짓말이 된다.
  아웃트로가 유도할 것은 딱 둘: **이 영상의 유튜브 좋아요·하이프·구독**, 그리고 **지난주 영상 클릭**.
  (초안에서 '다음 주 순위는 여러분 손에' / 집계 미터 412:53 씬을 넣었다가 이 이유로 들어냈다.)

대본 7줄 = 6개 씬(1·2줄이 한 씬 — 2줄은 슬램으로 화면을 덮는다).
시작·길이는 TTS 실측(lines.json)에서 온다 — 화면이 말을 따라간다.

  1+2 이번 주 열두 편 … 잘 보셨나요?   완주 그리드(12장) → 슬램 질문
  3   좋아요 한 번만 눌러 주세요        좋아요 버튼이 스스로 눌린다(할 행동을 보여 준다)
  4   하이프까지 누르면 큰 힘이 됩니다   하이프 버튼 + 위로 솟는 아이콘
  5   구독하시면 …                      구독 버튼 + 구독하면 뭐가 좋은지
  6   지난주에 못 보신 게 있다면         지난주 패널
  7   여기를 클릭해 주세요               ★엔드카드 — 유튜브 엔드스크린 슬롯 + 화살표

★엔드카드는 마지막 줄이 끝난 뒤 `tail` 초(기본 5.0)를 더 버틴다. 유튜브 엔드스크린 요소는
  최소 5초가 있어야 붙는다 — 여기를 줄이면 클릭할 시간이 없다.
★오른쪽 슬롯 자리(SLOT)에는 자막도 티커도 오지 않는다. 업로드 후 그 위에 엔드스크린을 얹는다.
★이모지 금지(헤드리스 렌더에서 깨진다) — 엄지 아이콘은 인라인 SVG 로 그린다.

HyperFrames 규칙: 등장 애니메이션만(마지막 씬 제외), 씬마다 track-index 분리,
난수·시계 금지, repeat:-1 금지, 타임라인 동기 생성 후 __timelines 등록.
"""
from section4_html import BG0, BG1, C1, C1T, FG, SUB, FONT, DISP, GRAIN, hl, chunk, slam_size

# 엔드스크린 슬롯 — 유튜브 엔드스크린 '영상' 요소를 얹을 자리(16:9).
SLOT = dict(left=1000, top=250, w=800, h=450)

# 엄지 — 이모지는 헤드리스에서 깨지므로 인라인 SVG(Material thumb_up 경로)
THUMB = ('<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
         '<path d="M2 21h3.2V9.4H2V21zm19.8-10.7c0-.95-.78-1.73-1.73-1.73h-5.44l.82-3.94.03-.28'
         'c0-.36-.15-.69-.38-.93L14.16 2.4 7.98 8.6c-.32.31-.51.75-.51 1.23v9.44c0 .95.78 1.73'
         ' 1.73 1.73h7.78c.72 0 1.34-.43 1.6-1.06l2.61-6.09c.08-.2.12-.4.12-.63v-1.92z"/></svg>')

# 하이프 — 위로 솟는 이중 꺾쇠. 유튜브 하이프는 '밀어 올린다'는 뜻이라 상승 기호로 그린다
BOOST = ('<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
         '<path d="M12 3.3 3.3 12l2.2 2.2L12 7.7l6.5 6.5 2.2-2.2L12 3.3z"/>'
         '<path d="M12 10.9 3.3 19.6l2.2 2.2L12 15.3l6.5 6.5 2.2-2.2L12 10.9z"/></svg>')


def build(rows, lines, total, has_audio=False, cap_style="punch", has_bgm=False,
          week="9월 첫 주", prev_thumb=None, tail=5.0, pool=None, span_label=None):
    rows = sorted(rows, key=lambda r: r["rank"])
    desc = sorted(rows, key=lambda r: -r["rank"])          # 12위 → 1위 (본편 재생 순서)
    top1 = rows[0] if rows else None
    dur_total = round(total + tail, 2)

    def win(i):
        """i번째 줄(1-base)의 화면 구간 — 말보다 0.3초 먼저 들어오고 다음 줄에 붙여 끝낸다."""
        ln = lines[i - 1]
        st = max(0.0, ln["start"] - 0.3)
        en = ln["start"] + ln["dur"] + 0.35
        if i < len(lines):
            en = max(en, lines[i]["start"])
        return round(st, 2), round(en - st, 2)

    w = [win(i) for i in range(1, len(lines) + 1)]

    # ── 자막 조각 — 인트로와 같은 규칙(` / ` 우선 분할, `?` 로 끝나면 슬램)
    cap_items, caps = [], ""
    tail_from = w[-1][0] if w else 0.0
    for ln in lines:
        pieces = chunk(ln["text"])
        span_n = sum(len(x) for x in pieces) or 1
        at = ln["start"]
        for j, piece in enumerate(pieces):
            d = round(ln["dur"] * len(piece) / span_n, 2)
            if j == len(pieces) - 1:
                d = round(ln["start"] + ln["dur"] - at, 2)
            cid = f'{ln["i"]}_{j}'
            slam = piece.rstrip().endswith("?")
            cap_items.append((cid, round(at, 2), max(0.35, d), slam))
            cls = "cap"
            if slam:
                cls += " slam"
            elif at >= tail_from:
                cls += " tail"      # 엔드카드 구간 — 왼쪽으로 좁혀 슬롯을 피한다
            caps += ('    <div id="cap%s" class="clip %s" data-start="%.2f" '
                     'data-duration="%.2f"%s>'
                     '<p%s>%s</p></div>\n'
                     % (cid, cls, at, max(0.35, d),
                        ' data-track-index="12" data-layout-allow-overlap' if slam
                        else ' data-track-index="12"',
                        (' style="font-size:%dpx"' % slam_size(piece)) if slam else '',
                        hl(piece)))
            at += d

    def slam_at(i):
        """i번째 줄에서 화면을 덮는 슬램이 시작되는 시각(없으면 None)."""
        return next((it[1] for it in cap_items
                     if it[0].startswith(f'{lines[i - 1]["i"]}_') and it[3]), None)

    def span(a, b):
        """a~b번째 줄을 한 씬으로 묶는다(1-base). 슬램이 있으면 거기서 화면을 넘겨준다."""
        a, b = min(a, len(w)), min(b, len(w))
        st = w[a - 1][0]
        en = w[b - 1][0] + w[b - 1][1]
        for i in range(a, b + 1):
            sa = slam_at(i)
            if sa is not None:
                en = min(en, sa)
                break
        return round(st, 2), round(max(0.5, en - st), 2)

    n = len(lines)
    # 씬 = 줄 묶음. 대본이 짧으면 뒤 씬부터 없는 것으로 접힌다.
    SC = {
        "recap": span(1, min(2, n)),
        "like":  span(3, 3) if n >= 4 else None,
        "hype":  span(4, 4) if n >= 5 else None,
        "sub":   span(5, 5) if n >= 6 else None,
        "prev":  span(6, 6) if n >= 7 else None,
        "end":   span(n, n),
    }
    # ★엔드카드는 마지막 말이 끝난 뒤에도 tail 초를 버틴다(엔드스크린 클릭 시간).
    SC["end"] = (SC["end"][0], round(dur_total - SC["end"][0], 2))

    frame_from = round(SC["recap"][0], 2)          # 러닝헤드·티커는 첫 씬부터 끝까지

    # ★카드 이미지는 **영상 스틸**이다. 인포카드 PNG 는 투명 배경 오버레이라 그대로 쓰면
    #   뒤 배경이 비쳐 카드마다 구멍이 뚫린 것처럼 보인다(실측). 스틸이 없을 때만 카드로 떨어진다.
    grid = "".join(
        f'<figure class="card{" top" if r["rank"] == 1 else ""}">'
        f'<img src="{r.get("still") or r["card"]}" alt="">'
        f'<figcaption><b>{r["rank"]}</b><span>{r["actress"]}</span></figcaption></figure>'
        for r in desc)

    # 티커는 12칸 전부 켜진 채 끝까지 간다 — "이번 주 열두 편 다 봤다"는 표시다
    ticks = "".join(f'<span class="tk on" id="tk{r["rank"]}">{r["rank"]}</span>' for r in desc)

    # ★배경 몽타주 — 평평한 검정이 "빈 슬라이드"로 보인다(design.md).
    #   리뷰한 영상들이 뒤에서 **실제로 재생된다**. 전체 길이를 편 수로 나눠 한 편씩 하드컷.
    #   납품본에는 배너(우상단)와 자막(하단)이 구워져 있어 scale(1.7) 로 가운데만 확대해 밀어낸다.
    #   그 위에 스크림(#washveil)을 덮어 왼쪽 텍스트 자리는 어둡게 유지 — 대비를 깎지 않는다.
    bgs = [r for r in desc if r.get("bg")]
    if bgs:
        bseg = round(dur_total / len(bgs), 3)
        wash = "\n".join(
            f'      <video id="bgv{k}" class="clip washv" data-start="{round(k * bseg, 3)}" '
            f'data-duration="{bseg}" data-media-start="0" data-track-index="0" '
            f'src="{r["bg"]}" muted playsinline></video>'
            for k, r in enumerate(bgs))
    else:
        # 영상을 못 찾았을 때만 인포카드 정지 그리드로 떨어진다
        wash = ('      <div id="washgrid" data-layout-ignore>'
                + "".join(f'<img src="{r["card"]}" alt="">' for r in desc) + '</div>')

    prev_panel = (f'<img src="{prev_thumb}" alt="">' if prev_thumb else
                  '<div class="ph">'
                  '<div class="ph-run"><i></i><span>지난주 · JAV 신작 랭킹</span></div>'
                  '<div class="ph-big">TOP<em>12</em></div>'
                  '<div class="ph-row"><u></u><u></u><u></u><u></u><u></u><u></u></div>'
                  '</div>')

    audio = (f'    <audio id="nar" class="clip" data-start="0" data-duration="{dur_total}" '
             f'data-track-index="13" src="assets/narration.wav" data-volume="1"></audio>\n'
             if has_audio else "")
    audio += (f'    <audio id="bgm" class="clip" data-start="0" data-duration="{dur_total}" '
              f'data-track-index="14" src="assets/bgm.mp3" data-volume="0.55"></audio>\n'
              if has_bgm else "")

    def blk(key, cls, track, inner):
        if not SC.get(key):
            return ""
        st, du = SC[key]
        return (f'      <div id="{key}" class="clip sheet {cls}" data-start="{st}" '
                f'data-duration="{du}" data-track-index="{track}">\n{inner}      </div>\n')

    recap_html = blk("recap", "board", 1,
        '        <div class="side lead">\n'
        '          <div class="kicker">이번 주 랭킹</div>\n'
        '          <div class="big">열두 편 <em>완주</em></div>\n'
        f'          <div class="note">1위 <u>{top1["actress"] if top1 else ""}</u></div>\n'
        '        </div>\n'
        f'        <div class="wrap">{grid}</div>\n')

    # ★좋아요 씬 — 말로만 부탁하지 않고 버튼이 화면에서 한 번 눌린다(할 행동을 보여 준다)
    like_html = blk("like", "cta2", 3,
        '        <div class="side lead">\n'
        '          <div class="kicker">재미있으셨다면</div>\n'
        '          <div class="big">좋아요 <em>한 번</em></div>\n'
        '        </div>\n'
        '        <div class="btnwrap">\n'
        f'          <div class="ybtn like" id="btnLike"><i class="ico">{THUMB}</i>'
        '<span>좋아요</span></div>\n'
        '        </div>\n')

    # ★하이프 씬 — 유튜브 '하이프' 버튼. 아이콘이 위로 솟는다(밀어 올린다는 뜻 그대로)
    hype_html = blk("hype", "cta2", 4,
        '        <div class="side lead">\n'
        '          <div class="kicker">하이프까지 누르면</div>\n'
        '          <div class="big">영상 제작에<br><em>큰 힘</em>이 됩니다</div>\n'
        '        </div>\n'
        '        <div class="btnwrap">\n'
        f'          <div class="ybtn hype" id="btnHype"><i class="ico">{BOOST}</i>'
        '<span>하이프</span></div>\n'
        '        </div>\n')

    # ★구독 씬 — "매주 다 본다"는 말은 근거가 있어야 한다. 수집 DB 실측치(pool)와
    #   그 회차 범위(span_label)를 화면에 같이 박는다. 숫자는 0에서 세어 올라간다.
    #   pool 이 없으면(측정 실패) 숫자 없는 문장으로 떨어진다 — 지어내지 않는다.
    sub_html = blk("sub", "cta2", 5,
        '        <div class="side lead">\n'
        '          <div class="kicker">이번 주 수집</div>\n'
        + (f'          <div class="big"><em id="poolN">0</em>편에서<br>열두 편</div>\n'
           if pool else
           '          <div class="big">이번 주 신작에서<br><em>열두 편</em></div>\n')
        + (f'          <div class="note">{span_label} 수집분</div>\n' if span_label else '')
        + '        </div>\n'
        '        <div class="btnwrap">\n'
        '          <div class="ybtn sub" id="btnSub">구독</div>\n'
        '        </div>\n')

    prev_html = blk("prev", "lastwk", 6,
        '        <div class="side lead">\n'
        '          <div class="kicker">지난주</div>\n'
        '          <div class="big">못 보신 편이<br><em>있다면</em></div>\n'
        '        </div>\n'
        f'        <div class="pcard">{prev_panel}</div>\n')

    end_html = blk("end", "endcard", 7,
        '        <div class="lead">\n'
        '          <div class="kicker">지난주 랭킹</div>\n'
        '          <div class="big"><em>TOP 12</em><br>보러 가기</div>\n'
        '          <div class="arrow" id="arrow"><i></i></div>\n'
        '        </div>\n'
        '        <div class="slot16" id="slot16" data-layout-allow-overlap>\n'
        f'          <div class="ghost">{prev_panel}</div>\n'
        '          <div class="play"></div>\n'
        '        </div>\n')

    bounds = sorted({round(v[0], 2) for v in SC.values() if v} | {c[1] for c in cap_items})
    tl = _timeline(SC, dur_total, frame_from, cap_items, cap_style, bounds, pool)

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

      /* ── 지속 레이어 — 인트로와 동일 */
      #bg {{ position:absolute; inset:0; z-index:0;
        background: radial-gradient(1400px 820px at 62% 34%, {BG0}, {BG1} 72%); }}
      /* ★배경 몽타주 — 리뷰한 영상이 뒤에서 실제로 돈다(정지 이미지가 아니다).
         scale(1.7) = 가운데 59%만 보인다 → 우상단 인포배너와 하단 자막이 화면 밖으로 밀린다.
         블러·감광·스크림이 겹치므로 소스는 960x540 이면 충분하다(build 가 그 크기로 자른다). */
      .washv {{ z-index:2; width:1920px; height:1080px; object-fit:cover;
        transform:scale(1.7); transform-origin:center center;
        filter:blur(1px) saturate(1) brightness(1.02); opacity:.95; }}
      /* 영상을 못 찾았을 때만 쓰는 정지 그리드 폴백 */
      #washgrid {{ position:absolute; inset:-6% -4%; z-index:2; overflow:hidden;
        display:grid; grid-template-columns:repeat(4, 1fr); grid-auto-rows:1fr; gap:10px;
        opacity:.34; filter:blur(2px) saturate(.6) brightness(1.05); }}
      #washgrid img {{ width:100%; height:100%; object-fit:cover; border-radius:10px;
        display:block; }}
      /* 스크림 — 왼쪽(텍스트 자리)은 거의 불투명하게, 오른쪽만 워시가 살짝 비친다.
         이게 없으면 대비 검사가 자막·헤드라인을 전부 잡는다(실측). */
      #washveil {{ position:absolute; inset:0; z-index:3; pointer-events:none;
        background:
          /* ① 하단 — 자막대와 티커가 앉는 자리. 여기만 확실히 눌러 준다 */
          linear-gradient(to top, rgba(13,10,12,.94) 0%, rgba(13,10,12,.66) 15%,
            rgba(13,10,12,0) 32%),
          /* ② 좌측 — 헤드라인 기둥. 오른쪽은 거의 열어 둬서 배우가 보이게 한다 */
          linear-gradient(100deg, rgba(13,10,12,.94) 0%, rgba(13,10,12,.80) 28%,
            rgba(13,10,12,.22) 58%, rgba(13,10,12,.38) 100%),
          /* ③ 전체 비네트 — 가장자리만 살짝 */
          radial-gradient(130% 96% at 56% 44%, rgba(13,10,12,0) 34%, rgba(13,10,12,.46) 100%); }}
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
      .big {{ font-family:{DISP}; font-size:132px; font-weight:900; line-height:.98;
        letter-spacing:-.05em; }}
      .big em {{ font-style:normal; color:{C1}; }}
      .note {{ margin-top:28px; font-size:40px; font-weight:700; color:#d3ccd1;
        letter-spacing:.02em; }}
      .note em, .note u {{ font-style:normal; text-decoration:none; color:{C1}; }}

      /* ── 1씬: 완주 그리드 */
      .board {{ flex-direction:row; align-items:center; gap:58px; }}
      .board .side {{ flex:0 0 400px; }}
      .board .wrap {{ flex:1; display:grid; grid-template-columns:repeat(4, 1fr); gap:13px; }}
      /* 배경이 비치지 않게 카드에 바탕을 깐다 — 스틸이 못 붙어도 구멍으로 보이지 않는다 */
      .card {{ position:relative; border-radius:10px; overflow:hidden; background:#151013;
        aspect-ratio:16/9; box-shadow:0 12px 34px rgba(0,0,0,.6); }}
      .card img {{ width:100%; height:100%; object-fit:cover; display:block;
        filter:brightness(.86) saturate(.95); }}
      /* ★카드가 영상 스틸이 되면서 캡션이 밝은 프레임 위에 앉는다 — 스크림을 키우고
         숫자는 '영상 위' 핑크(C1T)를 쓴다. C1 은 밝은 프레임에서 2.12:1 로 떨어졌다(실측). */
      .card figcaption {{ position:absolute; left:0; right:0; bottom:0; padding:38px 12px 10px;
        display:flex; align-items:baseline; gap:9px;
        background:linear-gradient(transparent, rgba(0,0,0,.62) 42%, rgba(0,0,0,.96)); }}
      .card b {{ font-family:{DISP}; font-size:29px; font-weight:900; color:{C1T};
        text-shadow:0 2px 10px rgba(0,0,0,.95); }}
      .card span {{ font-size:20px; font-weight:600; color:{FG};
        text-shadow:0 2px 10px rgba(0,0,0,.95); }}
      .card.top {{ box-shadow:0 0 0 4px {C1}, 0 18px 44px rgba(0,0,0,.7); }}

      /* ── 2~4씬: 좋아요 / 하이프 / 구독 — 왼쪽 카피 + 오른쪽에 진짜 버튼처럼 생긴 것 */
      .cta2 {{ flex-direction:row; align-items:center; gap:60px; }}
      .cta2 .side {{ flex:0 0 760px; }}
      .cta2 .big {{ font-size:118px; }}
      .btnwrap {{ flex:1; display:flex; align-items:center; justify-content:center; }}
      .ybtn {{ display:inline-flex; align-items:center; gap:24px; padding:34px 60px;
        border-radius:999px; font-size:58px; font-weight:800; letter-spacing:-.01em;
        white-space:nowrap; }}
      /* ★버튼은 배경 몽타주를 막는 불투명 바탕을 깐다 — 반투명이면 밝은 프레임에서 글자가
         묻힌다(대비 검사 실측 1.34:1). 유튜브 버튼도 실제로 불투명하다. */
      .ybtn.like {{ background:rgba(22,17,20,.90); color:{FG};
        border:3px solid rgba(255,255,255,.32); }}
      .ybtn.like .ico {{ display:block; width:72px; height:72px; }}
      .ybtn.like .ico svg {{ display:block; width:72px; height:72px; }}
      .ybtn.hype {{ background:rgba(36,10,18,.93); color:{C1T};
        border:3px solid rgba(255,45,85,.85); }}
      .ybtn.hype .ico, .ybtn.hype .ico svg {{ display:block; width:66px; height:66px; }}
      .ybtn.sub {{ background:{C1}; color:{FG}; font-family:{DISP}; font-weight:900;
        font-size:72px; padding:34px 92px; letter-spacing:-.03em;
        box-shadow:0 18px 60px rgba(255,45,85,.38); }}

      /* ── 5씬: 지난주 패널 */
      .lastwk {{ flex-direction:row; align-items:center; gap:70px; }}
      .lastwk .side {{ flex:0 0 700px; }}
      /* 132px 이면 '못 보신 편이' 가 620px 안에서 세 줄로 쪼개진다(실측) */
      .lastwk .big {{ font-size:112px; }}
      .pcard {{ flex:1; min-width:0; aspect-ratio:16/9; border-radius:16px; overflow:hidden;
        box-shadow:0 22px 70px rgba(0,0,0,.75); }}
      .pcard img {{ width:100%; height:100%; object-fit:cover; }}
      /* 지난주 썸네일이 없을 때 — 같은 디자인 언어로 만든 대체 패널(빈 칸으로 두지 않는다) */
      .ph {{ width:100%; height:100%; position:relative; padding:52px 56px;
        display:flex; flex-direction:column; justify-content:center;
        background:radial-gradient(70% 90% at 30% 30%, {BG0}, {BG1} 76%); }}
      .ph-run {{ display:flex; align-items:center; gap:14px; margin-bottom:26px; }}
      .ph-run i {{ width:5px; height:26px; background:{C1}; border-radius:3px; }}
      .ph-run span {{ font-size:23px; font-weight:700; letter-spacing:.14em; color:{SUB}; }}
      .ph-big {{ font-family:{DISP}; font-size:132px; font-weight:900; line-height:.9;
        letter-spacing:-.05em; color:{FG}; }}
      .ph-big em {{ font-style:normal; color:{C1}; margin-left:16px; }}
      .ph-row {{ margin-top:34px; display:flex; gap:11px; }}
      .ph-row u {{ display:block; width:74px; height:52px; border-radius:7px;
        background:rgba(255,255,255,.1); border-bottom:4px solid rgba(255,45,85,.5); }}

      /* ── 6씬: 엔드카드 — 오른쪽은 유튜브 엔드스크린 자리로 비워 둔다 */
      .endcard {{ justify-content:center; align-items:flex-start; }}
      .endcard .lead {{ max-width:800px; }}
      .endcard .big {{ font-size:112px; }}
      .arrow {{ margin-top:46px; display:flex; align-items:center; }}
      .arrow i {{ position:relative; display:block; width:190px; height:10px;
        background:{C1}; border-radius:5px; }}
      .arrow i::after {{ content:''; position:absolute; right:-4px; top:-22px;
        border-left:40px solid {C1}; border-top:27px solid transparent;
        border-bottom:27px solid transparent; }}
      /* ★슬롯은 배경 워시를 막는다 — 비치면 남의 품번이 썸네일 안에 떠서 지저분해진다 */
      .slot16 {{ background:{BG1}; position:absolute; left:{SLOT['left']}px; top:{SLOT['top']}px;
        width:{SLOT['w']}px; height:{SLOT['h']}px; border-radius:16px; overflow:hidden;
        border:4px dashed rgba(255,45,85,.75); box-shadow:0 22px 70px rgba(0,0,0,.75); }}
      .slot16 .ghost {{ position:absolute; inset:0; opacity:.55; }}
      .slot16 .ghost img {{ width:100%; height:100%; object-fit:cover; }}
      /* ★슬롯 안 대체 패널은 'TOP 12' 를 지운다 — 왼쪽 카피가 이미 같은 말을 하고 있다.
         남는 건 러닝헤드 한 줄 + 카드 칩 + 재생 삼각형 = '지난주 영상 썸네일'로 읽힌다 */
      .slot16 .ph {{ justify-content:flex-start; padding:40px 44px; }}
      .slot16 .ph-big {{ display:none; }}
      .slot16 .ph-row {{ margin-top:26px; gap:14px; }}
      .slot16 .ph-row u {{ width:96px; height:66px; }}
      .slot16 .play {{ position:absolute; left:50%; top:70%; width:0; height:0;
        margin:-44px 0 0 -30px;
        border-left:88px solid rgba(255,255,255,.94); border-top:52px solid transparent;
        border-bottom:52px solid transparent;
        filter:drop-shadow(0 10px 30px rgba(0,0,0,.85)); }}

      /* ── 자막 — 인트로와 동일 */
      .cap {{ z-index:90; display:flex; align-items:flex-end; padding:0 120px 128px; }}
      .cap p {{ transform-origin:left center; will-change:transform, filter;
        position:relative; max-width:1480px; padding-left:30px; text-align:left;
        font-size:72px; font-weight:800; line-height:1.28; letter-spacing:-.02em; color:{FG};
        text-shadow:0 4px 18px rgba(0,0,0,.95), 0 0 5px rgba(0,0,0,.95); }}
      .cap p::before {{ content:''; position:absolute; left:0; top:10px; bottom:10px;
        width:7px; background:{C1}; border-radius:4px; }}
      .cap .n {{ color:{C1T}; }}
      .cap .hi {{ color:{C1T}; font-size:1.12em; text-shadow:0 0 34px rgba(255,45,85,.55),
        0 4px 18px rgba(0,0,0,.95); }}
      /* ★엔드카드 구간 자막은 왼쪽으로 좁힌다 — 오른쪽 엔드스크린 슬롯을 절대 가리지 않는다 */
      .cap.tail p {{ max-width:800px; font-size:58px; }}

      .cap.slam .hi {{ color:{C1}; }}
      .cap.slam {{ opacity:0; z-index:95; align-items:center; justify-content:center;
        padding:0; background:rgba(8,4,6,.86); }}
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
{wash}
      <div id="washveil" data-layout-ignore></div>

{recap_html}{like_html}{hype_html}{sub_html}{prev_html}{end_html}
      <div id="runhead" class="clip runhead" data-start="{frame_from}"
           data-duration="{round(dur_total - frame_from, 2)}" data-track-index="9">
        <div class="bar"></div>
        <div class="t">{week} <em>· JAV 신작 랭킹</em></div>
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


def _timeline(SC, dur_total, frame_from, cap_items, cap_style="punch", bounds=(), pool=None):
    js = []
    a = js.append

    def kill_at(end):
        """퇴장이 끝나는 시각 + 그 직후에 붙어 있는 클립 경계(있으면).

        경계 바로 앞에서 퇴장이 끝나면 되감기·점프가 그 사이에 착지해 잔상이 남는다
        (hyperframes lint: gsap_exit_missing_hard_kill)."""
        out = [round(end, 2)]
        out += [b for b in bounds if 0 < b - end <= 0.12]
        return sorted(set(out))

    a("      window.__timelines = window.__timelines || {};")
    a("      const tl = gsap.timeline({ paused: true });")
    a("")
    # 배경 몽타주는 영상 클립 자체가 움직인다 — 추가 트윈 없음(클립마다 하드컷으로 넘어간다)
    a("      // ── 방송 프레임(첫 씬부터 끝까지). 티커 12칸은 켜진 채로 끝까지 간다 = 완주")
    a("      tl.from('#runhead', { x: -40, opacity: 0, duration: 0.45, ease: 'expo.out' }, %.2f);"
      % frame_from)
    a("      tl.from('#ticker .tk', { y: 18, opacity: 0, duration: 0.3, ease: 'power2.out',"
      " stagger: 0.03 }, %.2f);" % (frame_from + 0.12))
    a("")

    st, du = SC["recap"]
    a("      // ── 1씬: 완주 그리드 — 12장이 좍 깔리고 1위만 앞으로 나온다")
    a("      tl.to('#recap', { opacity: 1, duration: 0.12, ease: 'none' }, %.2f);" % st)
    a("      tl.from('#recap .kicker', { x: -26, opacity: 0, duration: 0.38,"
      " ease: 'power3.out' }, %.2f);" % (st + 0.12))
    a("      tl.from('#recap .big', { x: -40, opacity: 0, duration: 0.48, ease: 'expo.out' }, %.2f);"
      % (st + 0.2))
    a("      tl.from('#recap .card', { y: 40, opacity: 0, duration: 0.36,"
      " ease: 'back.out(1.6)', stagger: 0.03 }, %.2f);" % (st + 0.26))
    a("      tl.from('#recap .note', { x: -20, opacity: 0, duration: 0.34,"
      " ease: 'power3.out' }, %.2f);" % (st + 0.78))
    a("      tl.to('#recap .wrap', { scale: 1.035, transformOrigin: 'center center',"
      " duration: %.2f, ease: 'none' }, %.2f);" % (du, st))
    a("      tl.to('#recap .card.top', { scale: 1.06, zIndex: 5, duration: 0.3,"
      " ease: 'back.out(2)' }, %.2f);" % (st + max(0.9, du * 0.62)))
    a("")

    if SC.get("like"):
        st, du = SC["like"]
        a("      // ── 2씬: 좋아요 — 버튼이 스스로 한 번 눌린다(말로만 부탁하지 않는다)")
        a("      tl.to('#like', { opacity: 1, duration: 0.12, ease: 'none' }, %.2f);" % st)
        a("      tl.from('#like .kicker', { x: -26, opacity: 0, duration: 0.36,"
          " ease: 'power3.out' }, %.2f);" % (st + 0.1))
        a("      tl.from('#like .big', { x: -44, opacity: 0, duration: 0.5, ease: 'expo.out' }, %.2f);"
          % (st + 0.18))
        # 불투명도는 빨리 올린다 — 오래 반투명하면 대비 검사가 그 프레임을 잡는다
        a("      tl.from('#btnLike', { y: 44, scale: 0.86,"
          " transformOrigin: 'center center', duration: 0.46, ease: 'back.out(2.2)' }, %.2f);"
          % (st + 0.16))
        a("      tl.from('#btnLike', { opacity: 0, duration: 0.14, ease: 'none' }, %.2f);"
          % (st + 0.16))
        press = st + max(0.85, du * 0.48)
        a("      // 눌리는 순간 — 살짝 들어갔다 나오고 핑크로 채워진 뒤 그대로 남는다")
        a("      tl.to('#btnLike', { scale: 0.92, transformOrigin: 'center center',"
          " duration: 0.12, ease: 'power2.in' }, %.2f);" % press)
        a("      tl.to('#btnLike', { scale: 1, duration: 0.34, ease: 'back.out(3)' }, %.2f);"
          % (press + 0.12))
        a("      tl.to('#btnLike', { backgroundColor: '%s', borderColor: '%s',"
          " duration: 0.22, ease: 'power2.out' }, %.2f);" % (C1, C1, press + 0.06))
        a("      tl.to('#btnLike .ico', { rotation: -14, transformOrigin: '60%% 70%%',"
          " duration: 0.14, ease: 'power2.out' }, %.2f);" % press)
        a("      tl.to('#btnLike .ico', { rotation: 0, duration: 0.36, ease: 'back.out(3)' }, %.2f);"
          % (press + 0.14))
        a("")

    if SC.get("hype"):
        st, du = SC["hype"]
        a("      // ── 3씬: 하이프 — 아이콘이 위로 솟았다 제자리로(밀어 올린다는 뜻 그대로)")
        a("      tl.to('#hype', { opacity: 1, duration: 0.12, ease: 'none' }, %.2f);" % st)
        a("      tl.from('#hype .kicker', { x: -26, opacity: 0, duration: 0.36,"
          " ease: 'power3.out' }, %.2f);" % (st + 0.1))
        a("      tl.from('#hype .big', { x: -44, opacity: 0, duration: 0.5, ease: 'expo.out' }, %.2f);"
          % (st + 0.18))
        # 불투명도는 빨리 올린다 — 오래 반투명하면 대비 검사가 그 프레임을 잡는다
        a("      tl.from('#btnHype', { y: 44, scale: 0.86,"
          " transformOrigin: 'center center', duration: 0.46, ease: 'back.out(2.2)' }, %.2f);"
          % (st + 0.16))
        a("      tl.from('#btnHype', { opacity: 0, duration: 0.14, ease: 'none' }, %.2f);"
          % (st + 0.16))
        boost = st + max(0.85, du * 0.45)
        for k in range(3):
            at = boost + k * 0.62
            if at + 0.5 > st + du:
                break
            a("      tl.to('#btnHype .ico', { y: -22, duration: 0.16,"
              " ease: 'power2.out' }, %.2f);" % at)
            a("      tl.to('#btnHype .ico', { y: 0, duration: 0.3, ease: 'back.out(3)' }, %.2f);"
              % (at + 0.18))
            a("      tl.to('#btnHype', { borderColor: 'rgba(255,45,85,1)',"
              " backgroundColor: 'rgba(96,16,38,.96)', duration: 0.16,"
              " ease: 'power2.out' }, %.2f);" % at)
            a("      tl.to('#btnHype', { borderColor: 'rgba(255,45,85,.85)',"
              " backgroundColor: 'rgba(36,10,18,.93)', duration: 0.3,"
              " ease: 'power2.inOut' }, %.2f);" % (at + 0.18))
        a("")

    if SC.get("sub"):
        st, du = SC["sub"]
        a("      // ── 4씬: 구독 — 버튼이 꽂히고 두 번 숨쉰다(repeat:-1 금지 → 개별 트윈)")
        a("      tl.to('#sub', { opacity: 1, duration: 0.12, ease: 'none' }, %.2f);" % st)
        a("      tl.from('#sub .kicker', { x: -26, opacity: 0, duration: 0.36,"
          " ease: 'power3.out' }, %.2f);" % (st + 0.1))
        a("      tl.from('#sub .big', { x: -44, opacity: 0, duration: 0.5, ease: 'expo.out' }, %.2f);"
          % (st + 0.18))
        # 불투명도는 빨리 올린다 — 오래 반투명하면 대비 검사가 그 프레임을 잡는다
        a("      tl.from('#btnSub', { y: 44, scale: 0.8,"
          " transformOrigin: 'center center', duration: 0.5, ease: 'back.out(2.4)' }, %.2f);"
          % (st + 0.16))
        a("      tl.from('#btnSub', { opacity: 0, duration: 0.14, ease: 'none' }, %.2f);"
          % (st + 0.16))
        a("      tl.from('#sub .note', { x: -20, opacity: 0, duration: 0.34,"
          " ease: 'power3.out' }, %.2f);" % (st + 0.62))
        if pool:
            # 수집량은 0에서 세어 올라간다 — 인트로 카운터와 같은 문법이라 시리즈로 읽힌다
            cd = max(0.7, du * 0.42)
            a("      const pl = { v: 0 }, plEl = document.getElementById('poolN');")
            a("      tl.to(pl, { v: %d, duration: %.2f, ease: 'power2.out'," % (pool, cd))
            a("        onUpdate: () => { plEl.textContent = Math.round(pl.v); } }, %.2f);"
              % (st + 0.28))
        beat = st + 0.95
        for k in range(3):
            at = beat + k * 0.78
            if at + 0.6 > st + du:
                break
            a("      tl.to('#btnSub', { scale: 1.07, transformOrigin: 'center center',"
              " duration: 0.2, ease: 'power2.out' }, %.2f);" % at)
            a("      tl.to('#btnSub', { scale: 1, duration: 0.38, ease: 'power2.inOut' }, %.2f);"
              % (at + 0.22))
        a("")

    if SC.get("prev"):
        st, du = SC["prev"]
        a("      // ── 5씬: 지난주 패널")
        a("      tl.to('#prev', { opacity: 1, duration: 0.12, ease: 'none' }, %.2f);" % st)
        a("      tl.from('#prev .kicker', { x: -24, opacity: 0, duration: 0.34,"
          " ease: 'power3.out' }, %.2f);" % (st + 0.1))
        a("      tl.from('#prev .big', { x: -44, opacity: 0, duration: 0.48,"
          " ease: 'expo.out' }, %.2f);" % (st + 0.18))
        a("      tl.from('#prev .pcard', { x: 90, opacity: 0, duration: 0.52,"
          " ease: 'expo.out' }, %.2f);" % (st + 0.28))
        a("")

    st, du = SC["end"]
    a("      // ── 6씬: 엔드카드 — 슬롯이 꽂히고 화살표가 그쪽을 민다(repeat:-1 금지 → 개별 트윈)")
    a("      tl.to('#end', { opacity: 1, duration: 0.12, ease: 'none' }, %.2f);" % st)
    a("      tl.from('#end .kicker', { x: -24, opacity: 0, duration: 0.34,"
      " ease: 'power3.out' }, %.2f);" % (st + 0.1))
    a("      tl.from('#end .big', { x: -44, opacity: 0, duration: 0.48, ease: 'expo.out' }, %.2f);"
      % (st + 0.18))
    a("      tl.from('#slot16', { scale: 0.72, opacity: 0, transformOrigin: 'center center',"
      " duration: 0.5, ease: 'back.out(2.2)' }, %.2f);" % (st + 0.3))
    a("      tl.from('#arrow', { x: -60, opacity: 0, duration: 0.4, ease: 'expo.out' }, %.2f);"
      % (st + 0.55))
    push = st + 1.0
    for k in range(4):
        at = push + k * 1.15
        if at + 0.75 > st + du:
            break
        # 밀기와 돌아오기가 끝점에서 맞닿으면 lint 가 겹친 트윈으로 잡는다 — 0.02초 띄운다
        a("      tl.to('#arrow i', { x: 34, duration: 0.28, ease: 'power2.out' }, %.2f);" % at)
        a("      tl.to('#arrow i', { x: 0, duration: 0.42, ease: 'power2.inOut' }, %.2f);"
          % (at + 0.30))
        a("      tl.to('#slot16', { borderColor: 'rgba(255,45,85,1)',"
          " duration: 0.28, ease: 'power2.out' }, %.2f);" % at)
        a("      tl.to('#slot16', { borderColor: 'rgba(255,45,85,.45)',"
          " duration: 0.42, ease: 'power2.inOut' }, %.2f);" % (at + 0.30))
    a("")

    a("      // ── 자막 — 조각 단위로 끊어 띄운다(엔드카드 구간은 .tail 로 왼쪽에 좁게)")
    for cid, at, dur, slam in cap_items:
        if slam:
            a("      tl.fromTo('#cap%s', { opacity: 0, backgroundColor: 'rgba(255,45,85,.92)' },"
              " { opacity: 1, duration: 0.06, ease: 'none' }, %.2f);" % (cid, at))
            a("      tl.to('#cap%s', { backgroundColor: 'rgba(8,4,6,.86)', duration: 0.28,"
              " ease: 'power2.out' }, %.2f);" % (cid, at + 0.06))
            a("      tl.fromTo('#cap%s p', { scale: 2.6, opacity: 0 },"
              " { scale: 1, opacity: 1, duration: 0.16, ease: 'expo.out' }, %.2f);" % (cid, at))
            a("      tl.to('#cap%s p', { scale: 1.04, duration: %.2f, ease: 'none' }, %.2f);"
              % (cid, max(0.3, dur - 0.2), at + 0.16))
            for t in kill_at(at + dur):
                a("      tl.set('#cap%s', { opacity: 0 }, %.2f);" % (cid, t))
            continue
        if cap_style == "type":
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
            for t in kill_at(at + dur):
                a("      tl.set('#cap%s p', { opacity: 0 }, %.2f);" % (cid, t))
        else:
            a("      tl.fromTo('#cap%s p', { x: 90, scale: 1.16, opacity: 0,"
              " filter: 'blur(7px)' }, { x: 0, scale: 1, opacity: 1, filter: 'blur(0px)',"
              " duration: %.2f, ease: 'expo.out' }, %.2f);"
              % (cid, min(0.26, dur * 0.45), at))
            a("      tl.to('#cap%s p', { x: -70, scale: 0.97, opacity: 0,"
              " filter: 'blur(5px)', duration: %.2f, ease: 'power2.in' }, %.2f);"
              % (cid, min(0.16, dur * 0.3), at + dur - min(0.16, dur * 0.3)))
            for t in kill_at(at + dur):
                a("      tl.set('#cap%s p', { opacity: 0 }, %.2f);" % (cid, t))
    a("")
    a('      window.__timelines["main"] = tl;')
    return "\n".join(js)
