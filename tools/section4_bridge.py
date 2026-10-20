# -*- coding: utf-8 -*-
r"""순위 브릿지(편과 편 사이 3초) — HyperFrames 렌더.

연출(참고: 스탐 'KBO 황당한 순간 TOP 10'의 순위 전환):
  0.00  흰 번쩍 → 마스코트+채널명 패턴이 대각선으로 블러와 함께 밀려 들어와 흐른다
  1.05  가운데 타일로 확 줌인(색 번짐)
  1.25  어두운 별 모양이 가운데서 터지며 화면을 덮는다
  1.45  어두운 배경 + 거대한 영문 순위(TEN) 반투명 + 흰 숫자(10)가 오버슛하며 꽂힌다
  3.00  끝(다음 편으로 하드컷)

디자인은 section4/design.md(핑크 하나만 강조, 숫자는 Paperlogy)를 따른다.

사용:
  .venv\Scripts\python.exe tools\section4_bridge.py --out F:\ja_reviewer_out\ja27 --rank 10
  .venv\Scripts\python.exe tools\section4_bridge.py --out F:\ja_reviewer_out\ja27 --all --total 11
결과: {out}/_브릿지/브릿지_10.mp4
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "section4"
MASCOT = ROOT / "server" / "assets" / "mascot.png"
DUR = 3.0

WORDS = ["", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE", "TEN",
         "ELEVEN", "TWELVE", "THIRTEEN", "FOURTEEN", "FIFTEEN", "SIXTEEN", "SEVENTEEN",
         "EIGHTEEN", "NINETEEN", "TWENTY"]

PINK, PINK_DEEP = "#ff2d55", "#8c1430"
BG0, BG1 = "#0d0a0c", "#1c1116"
CHANNEL = "딸딸기TUBE"


def pattern_html():
    """대각선 패턴 타일. 가운데(행 4, 열 3) 타일이 화면 정중앙에 오도록 배치한다."""
    rows, cols, tw, th = 7, 5, 820, 300
    out = []
    for r in range(rows):
        shift = (tw / 2) if r % 2 else 0
        for c in range(cols):
            x = (c - 2) * tw - shift
            y = (r - 3) * th
            col = PINK if (r + c) % 2 == 0 else PINK_DEEP
            out.append(
                f'<div class="tile" style="left:{x:.0f}px;top:{y:.0f}px;color:{col}">'
                f'<img src="assets/mascot.png" alt="">{CHANNEL}</div>')
    return "\n".join(out)


def streaks_html():
    # 결정적 값(렌더는 프레임을 아무 순서로 찍으므로 random 금지)
    spec = [(140, 900, .22), (262, 1400, .14), (488, 620, .18), (702, 1100, .12),
            (866, 760, .2), (958, 1300, .1), (60, 500, .12), (1010, 980, .16)]
    return "\n".join(
        f'<div class="streak" id="s{i}" style="top:{y}px;width:{w}px;opacity:{o}"></div>'
        for i, (y, w, o) in enumerate(spec))


def build_html(rank):
    word = WORDS[rank] if rank < len(WORDS) else str(rank)
    word_size = min(700, int(1760 / (len(word) * 0.74)))  # Paperlogy 900 실측 폭 ≈0.74em
    num_size = 340 if rank < 10 else 320
    grain = ("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='300' height='300'>"
             "<filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2' "
             "seed='7'/></filter><rect width='100%' height='100%' filter='url(%23n)' opacity='.5'/></svg>")
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=1920, height=1080" />
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>
  @font-face {{ font-family:'Paperlogy'; font-weight:800 900; font-display:block;
    src:url('assets/fonts/Paperlogy-8ExtraBold.woff2') format('woff2'); }}
  @font-face {{ font-family:'Pretendard Variable'; font-weight:45 920; font-display:block;
    src:url('assets/fonts/PretendardVariable.woff2') format('woff2-variations'); }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{ width:1920px; height:1080px; overflow:hidden; background:#000; }}
  #root {{ position:relative; width:1920px; height:1080px; overflow:hidden; }}
  .layer {{ position:absolute; inset:0; overflow:hidden; }}

  /* ── 1막: 밝은 패턴 */
  #light {{ background:#f7eef1; }}
  #pat {{ position:absolute; left:960px; top:540px; width:0; height:0; transform:rotate(-12deg); }}
  #patInner {{ position:absolute; left:0; top:0; }}
  .tile {{ position:absolute; width:820px; height:300px; margin:-150px 0 0 -410px; display:flex;
    align-items:center; justify-content:center; gap:22px; font:900 118px/1 'Paperlogy';
    letter-spacing:-.04em; white-space:nowrap; }}
  .tile img {{ width:170px; height:auto; }}
  #flash {{ background:#fff; opacity:0; }}

  /* ── 전환: 별 모양 와이프 */
  #wipe {{ position:absolute; left:960px; top:540px; width:200px; height:200px; margin:-100px 0 0 -100px;
    transform:scale(0); background:{BG1};
    clip-path:polygon(50% 0,61% 39%,100% 50%,61% 61%,50% 100%,39% 61%,0 50%,39% 39%); }}

  /* ── 2막: 어두운 순위 카드 */
  #dark {{ background:radial-gradient(ellipse at 50% 45%, {BG1} 0%, {BG0} 75%); }}
  #glow {{ position:absolute; left:50%; top:50%; width:1500px; height:900px; margin:-450px 0 0 -750px;
    background:radial-gradient(ellipse, rgba(255,45,85,.28), rgba(255,45,85,0) 65%); }}
  #grain {{ position:absolute; inset:-40px; background:url("{grain}"); opacity:.16; mix-blend-mode:screen; }}
  .streak {{ position:absolute; left:0; height:2px;
    background:linear-gradient(90deg, rgba(255,255,255,0), #fff 40%, rgba(255,255,255,0)); }}
  #word {{ position:absolute; left:0; right:0; top:50%; text-align:center; transform-origin:50% 50%;
    font:900 {word_size}px/0.82 'Paperlogy'; letter-spacing:-.04em; color:rgba(255,232,238,.30);
    -webkit-mask-image:linear-gradient(180deg,#000 0%,#000 30%,rgba(0,0,0,.15) 50%,#000 70%,#000 100%);
    mask-image:linear-gradient(180deg,#000 0%,#000 30%,rgba(0,0,0,.15) 50%,#000 70%,#000 100%); }}
  #num {{ position:absolute; left:0; right:0; top:50%; text-align:center; color:#fff;
    font:900 {num_size}px/1 'Paperlogy'; letter-spacing:-.05em; }}
  #rule {{ position:absolute; left:50%; top:50%; width:120px; height:10px; margin-left:-60px;
    background:{PINK}; }}
  #vig {{ position:absolute; inset:0; background:radial-gradient(ellipse at center, rgba(0,0,0,0) 55%, rgba(0,0,0,.55) 100%); }}
</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{DUR}" data-width="1920" data-height="1080">

  <div id="light" class="layer clip" data-start="0" data-duration="1.5" data-track-index="1">
    <div id="pat"><div id="patInner">
{pattern_html()}
    </div></div>
  </div>
  <div id="flash" class="layer clip" data-start="0" data-duration="0.5" data-track-index="2"></div>

  <div id="dark" class="layer clip" data-start="1.42" data-duration="{DUR - 1.42:.2f}" data-track-index="3">
    <div id="glow"></div>
{streaks_html()}
    <div id="word">{word}</div>
    <div id="num">{rank}</div>
    <div id="rule"></div>
    <div id="grain"></div>
    <div id="vig"></div>
  </div>

  <div id="wipe" class="clip" data-start="1.2" data-duration="0.24" data-track-index="4"></div>

  <audio id="sfx" class="clip" data-start="0" data-duration="{DUR}" data-track-index="10"
    src="assets/bridge_sfx.wav" data-volume="1"></audio>
</div>

<script>
  window.__timelines = window.__timelines || {{}};
  const tl = gsap.timeline({{ paused: true }});

  // 1막 — 번쩍 → 패턴이 블러와 함께 밀려 들어와 흐른다
  tl.fromTo("#flash", {{ opacity: 0.0 }}, {{ opacity: 1, duration: 0.06, ease: "none" }}, 0)
    .to("#flash", {{ opacity: 0, duration: 0.22, ease: "power2.out" }}, 0.08)
    .fromTo("#patInner", {{ x: 1100, filter: "blur(22px)" }},
            {{ x: 0, filter: "blur(0px)", duration: 0.42, ease: "power3.out" }}, 0.04)
    .to("#patInner", {{ x: -220, duration: 0.6, ease: "none" }}, 0.46)
    // 가운데 타일로 확 줌인 + 색 번짐
    .to("#pat", {{ scale: 2.6, duration: 0.32, ease: "power3.in" }}, 1.04)
    .to("#patInner", {{ filter: "blur(8px) drop-shadow(10px 0 0 rgba(0,220,255,.7)) drop-shadow(-10px 0 0 rgba(255,45,85,.8))",
            duration: 0.3, ease: "power2.in" }}, 1.06)

  // 전환 — 어두운 별이 터지며 덮는다
    .fromTo("#wipe", {{ scale: 0, rotation: 0 }}, {{ scale: 16, rotation: 35, duration: 0.22, ease: "power2.in" }}, 1.2)
    .set("#wipe", {{ autoAlpha: 0 }}, 1.44)

  // 2막 — 거대한 영문 + 숫자 오버슛
    .fromTo("#word", {{ yPercent: -50, scale: 1.18, opacity: 0, skewX: -12 }},
            {{ yPercent: -50, scale: 1.0, opacity: 1, skewX: -12, duration: 0.45, ease: "power3.out" }}, 1.42)
    .to("#word", {{ scale: 0.95, duration: 1.13, ease: "none" }}, 1.87)
    .fromTo("#num", {{ yPercent: -50, scale: 1.9, opacity: 0, skewX: -10, filter: "blur(16px)",
              textShadow: "26px 0 0 rgba(255,45,85,.9), -26px 0 0 rgba(0,220,255,.8)" }},
            {{ yPercent: -50, scale: 1, opacity: 1, skewX: -10, filter: "blur(0px)",
              textShadow: "0px 0 0 rgba(255,45,85,0), 0px 0 0 rgba(0,220,255,0)",
              duration: 0.42, ease: "back.out(2.4)" }}, 1.58)
    .to("#num", {{ scale: 1.04, duration: 1.0, ease: "none" }}, 2.0)
    .fromTo("#rule", {{ y: {num_size // 2 + 10}, scaleX: 0 }}, {{ y: {num_size // 2 + 10}, scaleX: 1, duration: 0.25, ease: "power3.out" }}, 1.8)
    .fromTo("#glow", {{ scale: 0.8, opacity: 0 }}, {{ scale: 1.1, opacity: 1, duration: 1.4, ease: "power1.out" }}, 1.42)
    .fromTo("#grain", {{ x: 0, y: 0 }}, {{ x: -30, y: 18, duration: 1.58, ease: "steps(12)" }}, 1.42);
  // 가로줄이 서로 다른 속도로 흐른다
  [[-900, 1900, 1.3], [1920, -1400, 1.0], [-600, 1700, 1.5], [1920, -1100, 1.2],
   [-760, 1500, 0.9], [1920, -1300, 1.4], [-500, 2000, 1.1], [1920, -980, 1.3]].forEach((v, i) => {{
    tl.fromTo("#s" + i, {{ x: v[0] }}, {{ x: v[1], duration: v[2], ease: "none" }}, 1.42 + (i % 3) * 0.08);
  }});
  window.__timelines["main"] = tl;
</script>
</body>
</html>
"""


def make_sfx(dst):
    """휙(노이즈 스윕) + 와이프 순간 저음 쿵 + 숫자 꽂힐 때 pop."""
    pop = ROOT / "assets" / "sfx" / "pop.wav"
    fc = ("anoisesrc=color=pink:d=0.55:a=0.6,highpass=f=500,lowpass=f=7000,"
          "afade=t=in:d=0.18,afade=t=out:st=0.25:d=0.3,volume=0.55[w1];"
          "anoisesrc=color=pink:d=0.4:a=0.6,highpass=f=300,afade=t=in:d=0.2,afade=t=out:st=0.2:d=0.2,"
          "volume=0.6,adelay=1000|1000[w2];"
          "sine=f=55:d=0.35,afade=t=out:st=0.05:d=0.3,volume=0.9,adelay=1420|1420[b];")
    inputs = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    if pop.is_file():
        inputs += ["-i", str(pop)]
        fc += "[1:a]adelay=1600|1600,volume=0.8[p];[w1][w2][b][p]amix=inputs=4:normalize=0[m]"
    else:
        fc += "[w1][w2][b]amix=inputs=3:normalize=0[m]"
    fc += f";[m]aformat=channel_layouts=stereo,apad,atrim=0:{DUR},alimiter=limit=0.9[o]"
    cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", fc, "-map", "[o]",
           "-ar", "48000", str(dst)]
    return subprocess.run(cmd).returncode == 0


def prep_project(proj):
    assets = proj / "assets"
    (assets / "fonts").mkdir(parents=True, exist_ok=True)
    for f in (TPL / "fonts").glob("*.woff2"):
        shutil.copy2(f, assets / "fonts" / f.name)
    shutil.copy2(MASCOT, assets / "mascot.png")
    for f in ("hyperframes.json", "package.json"):
        s = TPL / "ja-intro" / f
        if s.is_file() and not (proj / f).is_file():
            shutil.copy2(s, proj / f)
    if not make_sfx(assets / "bridge_sfx.wav"):
        print("  [!] 효과음 생성 실패 — 무음으로 진행")


def render(proj, dst):
    if dst.is_file():
        dst.unlink()
    cmd = ["npx", "--yes", "hyperframes@0.8.31", "render", "--output", str(dst)]
    r = subprocess.run(cmd, cwd=str(proj), shell=True)
    # 렌더러가 성공해도 비정상 종료코드를 내는 경우가 있어(section4_intro 와 같음) 파일로 판정
    ok = dst.is_file() and dst.stat().st_size > 20_000
    print(("[OK] " if ok else "[X] 렌더 실패 ") + str(dst) + ("" if ok else f" (code {r.returncode})"))
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="배치 출력 폴더")
    ap.add_argument("--rank", type=int, help="이 순위 하나만")
    ap.add_argument("--all", action="store_true", help="--total 부터 1위까지 전부")
    ap.add_argument("--total", type=int, help="--all 일 때 꼴찌 순위")
    a = ap.parse_args()
    ranks = [a.rank] if a.rank else (list(range(a.total, 0, -1)) if a.all and a.total else [])
    if not ranks:
        ap.error("--rank N 또는 --all --total N")
    base = Path(a.out) / "_브릿지"
    proj = base / "hf"
    proj.mkdir(parents=True, exist_ok=True)
    prep_project(proj)
    fails = 0
    for r in ranks:
        (proj / "index.html").write_text(build_html(r), encoding="utf-8")
        print(f"── {r}위 렌더")
        fails += not render(proj, base / f"브릿지_{r:02d}.mp4")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
