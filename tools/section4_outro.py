# -*- coding: utf-8 -*-
r"""섹션4 — 통합 리뷰 영상 아웃트로(마무리 후킹) 제작.

인트로(`section4_intro.py`)의 짝. 12편을 다 보여 준 **뒤**에 붙어서 두 가지를 시킨다:
  ① 이 영상의 **유튜브 좋아요·하이프·구독**을 누르게 한다
  ② 지난주 랭킹 영상으로 넘어가게 한다 → 엔드스크린 슬롯을 화면 오른쪽에 비워 둔다

★★랭킹의 좋아요/싫어요는 **퍼온 소스 사이트 수치**다. 이 영상 시청자가 누른 게 아니다.
  "여러분이 누른 게 다음 주 순위가 됩니다" 류로 말하면 거짓말이 된다 — 초안에서 그러다 들어냈다.
★대본의 `{pool}` 은 회차 수집량으로 치환된다(`jav_week_count` 실측). 내레이션과 화면 숫자가
  같은 출처를 쓰게 하는 장치다 — 대본에 숫자를 직접 박으면 다음 회차에 화면과 어긋난다.

대본은 `section4/멘트_아웃트로.txt` 가 진실의 원천이고(배치 폴더에 `_아웃트로/멘트.txt` 가
있으면 그쪽이 우선), 영상은 HyperFrames(HTML+GSAP)로 렌더한다.

  tts    멘트 → 문장별 wav(voicebox) → 실측 길이로 lines.json
  build  lines.json + 랭킹 집계 → 아웃트로 HyperFrames 프로젝트 생성(assets 복사 + index.html)
  render npx hyperframes render → 아웃트로.mp4

사용:
  .venv\Scripts\python.exe tools\section4_outro.py tts   --out F:\ja_reviewer_out\ja21_v2
  .venv\Scripts\python.exe tools\section4_outro.py build --out ... [--src ...\ja21] [--prev 지난주.png]
  .venv\Scripts\python.exe tools\section4_outro.py render --out ...

이어붙이기는 `section4_intro.py merge` 가 한다 — 아웃트로.mp4 가 있으면 맨 뒤에 자동으로 붙는다.

★TTS는 voicebox 큐를 쓴다 — 섹션3 배치가 도는 중에는 돌리지 말 것(600s 타임아웃 유발).
★엔드스크린: 업로드 후 유튜브 스튜디오에서 '영상' 요소를 화면 오른쪽 점선 박스에 맞춰 얹는다.
  마지막 말이 끝난 뒤 TAIL 초를 버티므로 엔드스크린 최소 노출(5초)을 만족한다.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import pipeline as P
from section4_html_outro import build as build_html
from jav_week_count import week_label

ROOT = Path(__file__).resolve().parent.parent
TPL_DIR = ROOT / "section4"
GAP = 0.35          # 문장 사이 여백(초)
LEAD = 0.4          # 첫 문장 앞 여백
CPS = 6.2           # 실측 발화 속도(자/초) — wav가 없을 때만 쓰는 추정치
TAIL = 5.0          # ★마지막 말 뒤 엔드카드 유지(초). 유튜브 엔드스크린 최소 노출이 5초다
BGM_SRC = "bgm/life_of_riley.mp3"   # section4/ 기준. 인트로(sneaky_snitch)와 다른 곡 — 끝은 풀어 준다
BGM_OFFSET = 12.0   # 곡에서 잘라 쓸 시작 지점(초)
BGM_LUFS = -24


def out_outro(out):
    d = Path(out) / "_아웃트로"
    d.mkdir(parents=True, exist_ok=True)
    return d


def measure_pool(args):
    """회차 수집량(지난주 화 ~ 이번주 월) — 대본 `{pool}` 과 화면 숫자의 단일 출처.

    측정이 안 되면 (None, None) 을 돌려준다. 지어낸 숫자를 쓰느니 문장에서 빼는 게 낫다.
    """
    if getattr(args, "pool", None):
        return args.pool, getattr(args, "span", None)
    try:
        import datetime as _dt
        from jav_week_count import measure
        d = getattr(args, "date", None)
        m = measure(_dt.date.fromisoformat(d) if d else _dt.date.today())
        print(f"수집 실측: {m['from']}(화)~{m['to']}(월) {m['pool']}편  [{m['source']}]")
        return m["pool"], getattr(args, "span", None) or m["span"]
    except Exception as e:
        print(f"  [!] 수집량 실측 실패({e}) — 숫자 없이 진행합니다")
        return None, getattr(args, "span", None)


def fill(texts, pool):
    """대본의 `{pool}` 을 실측치로 바꾼다. 실측이 없으면 그 줄을 통째로 뺀다."""
    out = []
    for t in texts:
        if "{pool}" in t:
            if not pool:
                continue
            t = t.replace("{pool}", str(pool))
        out.append(t)
    return out


def load_script(out):
    """멘트 원본 — 배치 폴더 것이 있으면 그것, 없으면 저장소 템플릿."""
    for p in (out_outro(out) / "멘트.txt", TPL_DIR / "멘트_아웃트로.txt"):
        if p.is_file():
            lines = [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            if lines:
                return lines, p
    raise FileNotFoundError("멘트_아웃트로.txt 를 찾을 수 없습니다")


def wav_dur(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def build_lines(out, texts):
    """문장별 (text, dur, start) — wav 가 있으면 실측, 없으면 자수 추정."""
    d = out_outro(out) / "tts"
    rows, t = [], LEAD
    for i, s in enumerate(texts, 1):
        w = d / f"o{i:03d}.wav"
        # 추정치는 실제 읽는 글자만 센다 — 화면 표시용 `/`·`*` 는 TTS가 안 읽는다
        spoken = s.replace(" / ", " ").replace("/", " ").replace("*", "").strip()
        dur = wav_dur(w) if w.is_file() else round(len(spoken) / CPS, 2)
        rows.append({"i": i, "text": s, "wav": str(w) if w.is_file() else None,
                     "dur": round(dur, 2), "start": round(t, 2)})
        t += dur + GAP
    return rows, round(t - GAP, 2)


# ────────────────────────────────────────────────────────────── 랭킹 로드

RANK_MD = re.compile(r"\|\s*\d+번째\s*\|\s*\*\*(\d+)위\*\*\s*\|\s*`([A-Z0-9-]+)`\s*\|"
                     r"\s*([^|]*?)\s*\|\s*(\d+)\s*/\s*(\d+)\s*\|")


def rank_rows(out, src=None):
    """순위·배우·좋아요/싫어요. `--src`(랭킹 txt)가 있으면 그것, 없으면 배치가 남긴 순위표.

    ★배치를 정리(`_중간산출물` 이동)하고 나면 원본 랭킹 txt 는 손을 떠난 뒤다.
      그래서 `_중간산출물/_순위/00_순위.md` 를 2순위 원천으로 읽는다 — 같은 숫자가 들어 있다.
    """
    out = Path(out)
    if src:
        from _ranklist import load_rank, load_details, find_rank_file, match_videos
        rf = find_rank_file(src)
        det = load_details(rf)
        rows = []
        # 영상 있는 편만, 빠진 순위는 당겨 매긴 번호 — 편별 내레이션 호명과 같은 번호
        for rank, code, _v in match_videos(load_rank(rf), src)[0]:
            d = det.get(code, {})
            rows.append({"rank": rank, "code": code, "actress": d.get("actress", ""),
                         "likes": d.get("likes", 0), "dislikes": d.get("dislikes", 0),
                         "views": d.get("views", 0)})
        if rows:
            print(f"랭킹: {rf}")
            return rows
    md = out / "_중간산출물" / "_순위" / "00_순위.md"
    if md.is_file():
        rows = [{"rank": int(m[0]), "code": m[1], "actress": m[2],
                 "likes": int(m[3]), "dislikes": int(m[4]), "views": 0}
                for m in RANK_MD.findall(md.read_text(encoding="utf-8"))]
        if rows:
            print(f"랭킹: {md} ({len(rows)}편)")
            return rows
    raise FileNotFoundError("랭킹을 찾을 수 없습니다 — --src 로 랭킹 txt 폴더를 주세요")


def find_clip(out, code):
    """배경 몽타주 소스 — 납품본(해설 mux) → 완성본 → 편별 자막본 → 인트로가 복사해 둔 것."""
    out = Path(out)
    for p in (out / "_납품" / f"{code}.mp4",
              out / "_완성" / f"{code}.mp4",
              out / "_중간산출물" / "_납품" / f"{code}.mp4",
              out / "_중간산출물" / "_완성" / f"{code}.mp4",
              out / code / f"{code}_final_subbed.mp4",
              out / "_인트로" / "hf" / "assets" / f"{code}_clip.mp4"):
        if p.is_file():
            return p
    return None


def luma_at(src, t):
    """t초 지점 1초의 평균 밝기(0~255). 실패하면 -1."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-t", "1", "-i", str(src),
                        "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                        "-f", "null", "-"], capture_output=True, text=True)
    vals = [float(x.split("=")[1]) for x in (r.stdout + r.stderr).split()
            if x.startswith("lavfi.signalstats.YAVG=")]
    return sum(vals) / len(vals) if vals else -1.0


def pick_start(src, seg, log=print):
    """배경으로 쓸 구간 — 후보 지점 중 **가장 밝은 곳**을 고른다.

    무조건 30% 지점에서 따면 어두운 씬에 걸려 배경에 아무것도 안 보인다(실측).
    앞머리(배너 구간)와 끝머리는 피하고 가운데를 훑는다.
    """
    d = wav_dur(src)
    if d <= seg + 1:
        return 0.0
    cands = [d * f for f in (0.22, 0.35, 0.48, 0.61, 0.74) if d * f + seg < d]
    best, bl = cands[0], -1.0
    for t in cands:
        v = luma_at(src, t)
        if v > bl:
            best, bl = t, v
    return round(best, 2)


def grab_still(src, dst, start):
    """그리드 카드용 스틸 — 가운데 62%만 잘라 640x360.

    ★인포카드 PNG 를 카드로 쓰면 안 된다. 그건 **투명 배경 오버레이**라서 뒤 배경이
      그대로 비쳐 카드마다 구멍이 뚫린 것처럼 보인다(실측). 영상 한 프레임이 정답이다.
      가운데만 자르는 이유는 배경 몽타주와 같다 — 우상단 배너와 하단 자막을 밀어내려고.
    """
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-i", str(src),
           "-frames:v", "1", "-vf",
           "crop=iw*0.62:ih*0.62,scale=640:360:force_original_aspect_ratio=increase,"
           "crop=640:360", "-q:v", "3", str(dst)]
    return subprocess.run(cmd).returncode == 0 and dst.is_file()


def cut_bg(src, dst, start, seg):
    """배경용 조각 — 960x540 / 무음 / seg 초. 원본을 통째로 복사하면 프로젝트가 수백 MB 된다.

    배경은 블러·감광 위에 스크림까지 덮이므로 절반 해상도로 충분하다(렌더도 그만큼 빨라진다).
    """
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{seg:.2f}",
           "-i", str(src), "-an", "-vf", "scale=960:540:force_original_aspect_ratio=increase,"
           "crop=960:540", "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
           "-pix_fmt", "yuv420p", str(dst)]
    return subprocess.run(cmd).returncode == 0 and dst.is_file()


def find_card(out, code):
    """인포카드 png — 배치 직후 / 정리 후 / 인트로가 이미 복사해 둔 것 순으로 찾는다."""
    out = Path(out)
    for p in (out / f"_infocard_{code}" / f"{code}_인포카드.png",
              out / "_중간산출물" / "_배너원본" / f"_infocard_{code}" / f"{code}_인포카드.png",
              out / "_인트로" / "hf" / "assets" / f"{code}_card.png"):
        if p.is_file():
            return p
    return None


# ────────────────────────────────────────────────────────────── tts

def cmd_tts(args, cfg):
    out = Path(args.out or cfg["out_dir"])
    texts, srcf = load_script(out)
    pool, _ = measure_pool(args)
    texts = fill(texts, pool)
    print(f"대본: {srcf} — {len(texts)}줄")
    d = out_outro(out) / "tts"
    d.mkdir(parents=True, exist_ok=True)
    base, prof = cfg["tts_base"], cfg["tts_profile"]
    lang, seed = cfg.get("tts_language", "ko"), cfg.get("tts_seed")
    ncand = int(cfg.get("tts_candidates", 1) or 1)
    ref = cfg.get("voice_ref") or str(ROOT / "models" / "voice_ref.npy")
    ref = ref if (ncand > 1 and Path(ref).is_file()) else None
    print(f"voicebox={base} 후보={ncand if ref else 1}개")
    for i, s in enumerate(texts, 1):
        w = d / f"o{i:03d}.wav"
        if w.is_file() and not args.force:
            print(f"  {i}. (이미 있음) {w.name}")
            continue
        s = s.replace(" / ", " ").replace("/", " ").replace("*", "").strip()  # 화면 표시는 안 읽는다
        print(f"  {i}. {s[:28]}...", flush=True)
        if ref:
            P.tts_generate_best(base, s, prof, lang, str(w), seed, candidates=ncand,
                                ref_npy=ref, python=cfg.get("voice_python"), log=print)
        else:
            P.tts_generate(base, s, prof, lang, str(w), seed, print)
    rows, total = build_lines(out, texts)
    clips = [(r["start"], r["wav"]) for r in rows if r["wav"]]
    wav = out_outro(out) / "아웃트로.wav"
    P.build_narration_wav(clips, str(wav), print, video_sec=total + TAIL)
    (out_outro(out) / "lines.json").write_text(
        json.dumps({"total": total, "lines": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"\n[OK] 내레이션 {wav}  총 {total:.1f}초 → lines.json 기록")
    return 0


# ────────────────────────────────────────────────────────────── build

def cmd_build(args, cfg):
    out = Path(args.out or cfg["out_dir"])
    proj = out_outro(out) / "hf"
    assets = proj / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    rows = rank_rows(out, args.src)
    pool, span_label = measure_pool(args)
    texts = fill(load_script(out)[0], pool)
    lj = out_outro(out) / "lines.json"
    if lj.is_file():
        data = json.loads(lj.read_text(encoding="utf-8"))
        lines, total = data["lines"], data["total"]
        print(f"lines.json 사용 — TTS 실측 타이밍 (총 {total:.1f}초)")
    else:
        lines, total = build_lines(out, texts)
        print(f"lines.json 없음 — 자수 추정 타이밍 (총 {total:.1f}초). TTS 후 다시 build 할 것")

    have = []
    for r in rows:
        png = find_card(out, r["code"])
        if not png:
            print(f"  [!] {r['code']}: 인포카드 없음 — 그리드에서 빠집니다")
            continue
        shutil.copy2(png, assets / f"{r['code']}_card.png")
        have.append({**r, "card": f"assets/{r['code']}_card.png"})

    # ★배경 몽타주 — 리뷰한 영상들이 뒤에서 실제로 돌아간다(정지 이미지가 아니다).
    #   전체 길이를 편 수로 나눠 한 조각씩, 각 편의 30% 지점(앞머리 배너 구간을 피해)에서 딴다.
    dur_total = total + TAIL
    seg = round(dur_total / max(1, len(have)), 3)
    for r in have:
        src = find_clip(out, r["code"])
        if not src:
            print(f"  [!] {r['code']}: 영상 없음 — 배경 몽타주에서 빠집니다")
            continue
        dst = assets / f"{r['code']}_bg.mp4"
        still = assets / f"{r['code']}_still.jpg"
        need = (not dst.is_file() or dst.stat().st_mtime < src.stat().st_mtime
                or abs(wav_dur(dst) - seg) > 0.15 or args.rebg)
        at = pick_start(src, seg) if (need or not still.is_file()) else None
        if need and not cut_bg(src, dst, at, seg):
            print(f"  [!] {r['code']}: 배경 조각 생성 실패")
            continue
        r["bg"] = f"assets/{r['code']}_bg.mp4"
        # 그리드 카드도 같은(밝은) 지점의 스틸을 쓴다 — 배경과 같은 순간이라 화면이 하나로 붙는다
        if (not still.is_file() or args.rebg) and at is not None:
            grab_still(src, still, at)
        if still.is_file():
            r["still"] = f"assets/{r['code']}_still.jpg"
    nbg = sum(1 for r in have if r.get("bg"))
    print(f"배경 몽타주: {nbg}편 x {seg:.2f}초 (960x540 무음)")

    # 지난주 썸네일(선택) — 없으면 생성기가 같은 디자인의 대체 패널을 그린다
    prev = None
    if args.prev:
        p = Path(args.prev)
        if p.is_file():
            dst = assets / ("prev" + p.suffix.lower())
            shutil.copy2(p, dst)
            prev = f"assets/{dst.name}"
            print(f"지난주 썸네일: {p.name}")
        else:
            print(f"  [!] --prev {p} 없음 — 대체 패널로 진행")

    wav = out_outro(out) / "아웃트로.wav"
    if wav.is_file():
        shutil.copy2(wav, assets / "narration.wav")
    # 폰트 — 렌더러 자동 목록에 한글 폰트가 없어서 직접 번들한다
    fdst = assets / "fonts"
    fdst.mkdir(parents=True, exist_ok=True)
    for f in sorted((TPL_DIR / "fonts").glob("*.woff2")):
        shutil.copy2(f, fdst / f.name)

    # ── BGM: 원곡에서 아웃트로 길이만큼 잘라 페이드·라우드니스를 맞춰 넣는다
    src = TPL_DIR / BGM_SRC
    if src.is_file():
        dst = assets / "bgm.mp3"
        fade_out = 2.2      # 끝은 인트로보다 길게 — 영상이 끝나는 자리다
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(BGM_OFFSET), "-t", f"{dur_total:.2f}",
               "-i", str(src), "-af",
               f"afade=t=in:st=0:d=0.8,"
               f"afade=t=out:st={max(0.1, dur_total - fade_out):.2f}:d={fade_out},"
               f"loudnorm=I={BGM_LUFS}:TP=-2:LRA=11",
               "-ac", "2", "-b:a", "192k", str(dst)]
        if subprocess.run(cmd).returncode == 0:
            print(f"BGM: {src.name} {BGM_OFFSET:.0f}s부터 {dur_total:.1f}초 "
                  f"(페이드 인 0.8s / 아웃 {fade_out}s, {BGM_LUFS} LUFS)")
        else:
            print("  [!] BGM 가공 실패 — 음악 없이 진행")

    for f in ("hyperframes.json", "package.json"):
        s = TPL_DIR / "ja-intro" / f
        if s.is_file() and not (proj / f).is_file():
            shutil.copy2(s, proj / f)
    html = build_html(have, lines, total,
                      has_audio=(assets / "narration.wav").is_file(),
                      cap_style=args.cap_style,
                      has_bgm=(assets / "bgm.mp3").is_file(),
                      week=args.week or week_label(), prev_thumb=prev, tail=TAIL,
                      pool=pool, span_label=span_label)
    (proj / "index.html").write_text(html, encoding="utf-8")
    print(f"\n[OK] {proj / 'index.html'}  ({len(have)}편, {dur_total:.1f}초)")
    print(f"   확인:  cd {proj} && npx hyperframes check")
    print(f"   렌더:  python tools\\section4_outro.py render --out {out}")
    return 0


# ────────────────────────────────────────────────────────────── render

def cmd_render(args, cfg):
    out = Path(args.out or cfg["out_dir"])
    proj = out_outro(out) / "hf"
    if not (proj / "index.html").is_file():
        print("[X] index.html 이 없습니다 — 먼저 build")
        return 1
    dst = out_outro(out) / "아웃트로.mp4"
    # ★렌더러는 임시 폴더에 만든 뒤 rename 으로 덮는다. 결과물이 재생기 등에 열려 있으면
    #   그 rename 이 EPERM 으로 죽는데, 낡은 파일은 그대로 남아 "성공"으로 착각하기 쉽다.
    #   그래서 미리 치운다 — 못 치우면 열려 있다는 뜻이니 먼저 알려 준다.
    if dst.is_file():
        try:
            dst.unlink()
        except OSError:
            print(f"[X] {dst.name} 이 다른 프로그램에 열려 있습니다 — 닫고 다시 실행하세요")
            return 1
    cmd = ["npx", "--yes", "hyperframes@0.8.31", "render", "--output", str(dst)]
    print("$ " + " ".join(cmd) + f"   (cwd={proj})", flush=True)
    r = subprocess.run(cmd, cwd=str(proj), shell=True)
    # ★렌더러는 산출물을 다 만들고도 후처리 검사 때문에 0이 아닌 코드로 끝날 때가 있다 —
    #   파일이 실제로 나왔는지로 판정한다(종료 코드만 믿으면 멀쩡한 렌더를 실패로 버린다).
    idx = proj / "index.html"
    if (not dst.is_file() or dst.stat().st_size < 100_000
            or dst.stat().st_mtime < idx.stat().st_mtime):
        print("[X] 렌더 실패 — 결과물이 없거나 index.html 보다 낡았습니다(위 로그 확인)")
        return r.returncode or 1
    if r.returncode:
        print(f"  [!] 렌더러 종료 코드 {r.returncode} — 파일은 정상 생성됨(경고는 위 로그 확인)")
    print(f"[OK] {dst}")
    print("   ※ 업로드 후 유튜브 스튜디오 → 엔드스크린 → '영상' 요소를")
    print("     화면 오른쪽 점선 박스(1000,250 / 800x450)에 맞춰 얹으세요.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="섹션4 — 통합 영상 아웃트로(마무리 후킹) 제작")
    ap.add_argument("cmd", choices=["tts", "build", "render"])
    ap.add_argument("--out", help="out_dir. 생략 시 config out_dir")
    ap.add_argument("--src", help="원본 폴더(랭킹 txt 위치). 생략 시 _중간산출물/_순위/00_순위.md")
    ap.add_argument("--prev", help="지난주 랭킹 영상 썸네일 이미지(선택)")
    ap.add_argument("--week", default=None, help="러닝헤드 회차 이름. 생략 시 오늘 기준 'N월 둘째 주'")
    ap.add_argument("--pool", type=int,
                    help="회차 수집량. 생략 시 수집 DB에서 실측(jav_week_count)")
    ap.add_argument("--span", help="구독 씬에 박히는 회차 범위 문구. 생략 시 실측값")
    ap.add_argument("--date", help="실측 기준일 YYYY-MM-DD (생략 시 오늘)")
    ap.add_argument("--force", action="store_true", help="tts: 기존 wav 무시하고 재생성")
    ap.add_argument("--rebg", action="store_true",
                    help="build: 배경 몽타주 조각을 다시 딴다(밝은 구간 재탐색)")
    ap.add_argument("--cap-style", default="punch", choices=["punch", "type", "slide"],
                    help="자막 연출: punch=펀치줌+휙 패닝(기본) / type=타자체 / slide=슬라이드")
    args = ap.parse_args()
    cfg = _common.load_cfg()
    return {"tts": cmd_tts, "build": cmd_build, "render": cmd_render}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
