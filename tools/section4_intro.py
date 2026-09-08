# -*- coding: utf-8 -*-
r"""섹션4 — 통합 리뷰 영상 인트로(후킹) 제작.

섹션3까지가 편별 납품본을 만든다면, 섹션4는 그 12편을 **한 편의 랭킹 영상**으로
묶기 위한 앞머리를 만든다. 대본은 `section4/멘트.txt` 가 진실의 원천이고
(배치 폴더에 `_인트로/멘트.txt` 가 있으면 그쪽이 우선), 영상은 HyperFrames(HTML+GSAP)로 렌더한다.

  tts    멘트 → 문장별 wav(voicebox, 화자 후보 3개) → 실측 길이로 lines.json
  build  lines.json + 랭킹 집계 → 인트로 HyperFrames 프로젝트 생성(assets 복사 + index.html)
  render npx hyperframes render → 인트로.mp4
  merge  인트로 + _완성 12편(12위→1위) concat → 통합본

사용:
  .venv\Scripts\python.exe tools\section4_intro.py tts   --out F:\ja_reviewer_out\ja21_v2
  .venv\Scripts\python.exe tools\section4_intro.py build --out ... --src ...\ja21 --pool 500
  .venv\Scripts\python.exe tools\section4_intro.py render --out ...
  .venv\Scripts\python.exe tools\section4_intro.py merge  --out ... --src ...

★TTS는 voicebox 큐를 쓴다 — 섹션3 배치가 도는 중에는 돌리지 말 것(600s 타임아웃 유발).
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import pipeline as P
from section4_html import build as build_html

ROOT = Path(__file__).resolve().parent.parent
TPL_DIR = ROOT / "section4"
GAP = 0.35          # 문장 사이 여백(초)
LEAD = 0.4          # 첫 문장 앞 여백
CPS = 6.2           # 실측 발화 속도(자/초) — wav가 없을 때만 쓰는 추정치
BGM_SRC = "bgm/sneaky_snitch.mp3"   # section4/ 기준. 교체 시 출처.md 도 같이 고칠 것
BGM_OFFSET = 40.0   # 곡에서 잘라 쓸 시작 지점(초) — 실측 최고 에너지 구간
BGM_LUFS = -24      # 내레이션이 위에 앉도록 낮게 깐다(가벼운 곡이라 조금 올림)


def out_intro(out):
    d = Path(out) / "_인트로"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_script(out):
    """멘트 원본 — 배치 폴더 것이 있으면 그것, 없으면 저장소 템플릿."""
    for p in (out_intro(out) / "멘트.txt", TPL_DIR / "멘트.txt"):
        if p.is_file():
            lines = [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            if lines:
                return lines, p
    raise FileNotFoundError("멘트.txt 를 찾을 수 없습니다")


def wav_dur(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def build_lines(out, texts):
    """문장별 (text, dur, start) — wav 가 있으면 실측, 없으면 자수 추정."""
    d = out_intro(out) / "tts"
    rows, t = [], LEAD
    for i, s in enumerate(texts, 1):
        w = d / f"i{i:03d}.wav"
        dur = wav_dur(w) if w.is_file() else round(len(s) / CPS, 2)
        rows.append({"i": i, "text": s, "wav": str(w) if w.is_file() else None,
                     "dur": round(dur, 2), "start": round(t, 2)})
        t += dur + GAP
    return rows, round(t - GAP, 2)


# ────────────────────────────────────────────────────────────── tts

def cmd_tts(args, cfg):
    out = Path(args.out or cfg["out_dir"])
    texts, srcf = load_script(out)
    print(f"대본: {srcf} — {len(texts)}줄")
    d = out_intro(out) / "tts"
    d.mkdir(parents=True, exist_ok=True)
    base, prof = cfg["tts_base"], cfg["tts_profile"]
    lang, seed = cfg.get("tts_language", "ko"), cfg.get("tts_seed")
    ncand = int(cfg.get("tts_candidates", 1) or 1)
    ref = cfg.get("voice_ref") or str(ROOT / "models" / "voice_ref.npy")
    ref = ref if (ncand > 1 and Path(ref).is_file()) else None
    print(f"voicebox={base} 후보={ncand if ref else 1}개")
    for i, s in enumerate(texts, 1):
        w = d / f"i{i:03d}.wav"
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
    wav = out_intro(out) / "인트로.wav"
    P.build_narration_wav(clips, str(wav), print, video_sec=total + 0.6)
    (out_intro(out) / "lines.json").write_text(
        json.dumps({"total": total, "lines": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"\n[OK] 내레이션 {wav}  총 {total:.1f}초 → lines.json 기록")
    return 0


# ────────────────────────────────────────────────────────────── build

def rank_rows(out, src):
    from _ranklist import load_rank, load_details, find_rank_file
    rf = find_rank_file(src)
    det = load_details(rf)
    rows = []
    for rank, code in load_rank(rf):
        d = det.get(code, {})
        rows.append({"rank": rank, "code": code, "actress": d.get("actress", ""),
                     "likes": d.get("likes", 0), "dislikes": d.get("dislikes", 0),
                     "views": d.get("views", 0)})
    return rows


def cmd_build(args, cfg):
    out = Path(args.out or cfg["out_dir"])
    proj = out_intro(out) / "hf"
    assets = proj / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    rows = rank_rows(out, args.src)
    texts, _ = load_script(out)
    lj = out_intro(out) / "lines.json"
    if lj.is_file():
        data = json.loads(lj.read_text(encoding="utf-8"))
        lines, total = data["lines"], data["total"]
        print(f"lines.json 사용 — TTS 실측 타이밍 (총 {total:.1f}초)")
    else:
        lines, total = build_lines(out, texts)
        print(f"lines.json 없음 — 자수 추정 타이밍 (총 {total:.1f}초). TTS 후 다시 build 할 것")

    have = []
    for r in rows:
        c = r["code"]
        png = out / f"_infocard_{c}" / f"{c}_인포카드.png"
        if not png.is_file():
            print(f"  [!] {c}: 인포카드 없음 — 건너뜀")
            continue
        shutil.copy2(png, assets / f"{c}_card.png")
        # ★몽타주 소스는 **납품본**이다. `_미리보기_애니.mp4` 는 배너 오버레이 애니라
        #   화면에 인포카드가 그대로 뜬다(초안1 실측) — 영상이 아니라 카드가 지나간다.
        #   노출 게이트를 통과한 것만 쓴다: _완성 → _final_subbed 순, 둘 다 없으면 몽타주 제외.
        clip, mstart = None, 0.0
        for cand in (out / "_완성" / f"{c}.mp4", out / c / f"{c}_final_subbed.mp4"):
            if cand.is_file():
                clip = cand
                break
        if clip:
            shutil.copy2(clip, assets / f"{c}_clip.mp4")
            mstart = round(max(0.5, wav_dur(clip) * 0.3), 2)   # 앞머리(배너 구간)를 피해 30% 지점
        else:
            print(f"  [!] {c}: 납품본이 아직 없어 몽타주에서 제외(배치 완주 후 build 다시)")
        have.append({**r, "anim": f"assets/{c}_clip.mp4" if clip else None,
                     "mstart": mstart, "card": f"assets/{c}_card.png"})
    wav = out_intro(out) / "인트로.wav"
    if wav.is_file():
        shutil.copy2(wav, assets / "narration.wav")
    # 폰트 — 렌더러 자동 목록에 한글 폰트가 없어서 직접 번들한다(Pretendard 본문 / Paperlogy 숫자)
    fdst = assets / "fonts"
    fdst.mkdir(parents=True, exist_ok=True)
    for f in sorted((TPL_DIR / "fonts").glob("*.woff2")):
        shutil.copy2(f, fdst / f.name)

    # ── BGM: 원곡에서 인트로 길이만큼 잘라 페이드·라우드니스를 맞춰 넣는다
    src = TPL_DIR / BGM_SRC
    if src.is_file():
        dst = assets / "bgm.mp3"
        fade_out = 1.4
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(BGM_OFFSET), "-t", f"{total + 0.8:.2f}",
               "-i", str(src), "-af",
               f"afade=t=in:st=0:d=0.8,"
               f"afade=t=out:st={max(0.1, total + 0.8 - fade_out):.2f}:d={fade_out},"
               f"loudnorm=I={BGM_LUFS}:TP=-2:LRA=11",
               "-ac", "2", "-b:a", "192k", str(dst)]
        if subprocess.run(cmd).returncode == 0:
            print(f"BGM: {src.name} {BGM_OFFSET:.0f}s부터 {total + 0.8:.1f}초 "
                  f"(페이드 인 0.8s / 아웃 {fade_out}s, {BGM_LUFS} LUFS)")
        else:
            print("  [!] BGM 가공 실패 — 음악 없이 진행")

    for f in ("hyperframes.json", "package.json"):
        s = TPL_DIR / "ja-intro" / f
        if s.is_file() and not (proj / f).is_file():
            shutil.copy2(s, proj / f)
    html = build_html(have, lines, total, pool=args.pool,
                      has_audio=(assets / "narration.wav").is_file(),
                      cap_style=args.cap_style,
                      has_bgm=(assets / "bgm.mp3").is_file())
    (proj / "index.html").write_text(html, encoding="utf-8")
    print(f"\n[OK] {proj / 'index.html'}  ({len(have)}편, {total + 0.8:.1f}초)")
    print(f"   확인:  cd {proj} && npx hyperframes check")
    print(f"   렌더:  python tools\\section4_intro.py render --out {out}")
    return 0


# ────────────────────────────────────────────────────────────── render / merge

def cmd_render(args, cfg):
    out = Path(args.out or cfg["out_dir"])
    proj = out_intro(out) / "hf"
    if not (proj / "index.html").is_file():
        print("[X] index.html 이 없습니다 — 먼저 build")
        return 1
    dst = out_intro(out) / "인트로.mp4"
    cmd = ["npx", "--yes", "hyperframes@0.8.31", "render", "--output", str(dst)]
    print("$ " + " ".join(cmd) + f"   (cwd={proj})", flush=True)
    r = subprocess.run(cmd, cwd=str(proj), shell=True)
    if r.returncode:
        print("[X] 렌더 실패")
        return r.returncode
    print(f"[OK] {dst}")
    return 0


def voiced_copy(out, code, log=print):
    """★`_완성/*.mp4` 에는 해설 음성이 없다 — 섹션3이 mux=False 로 돌기 때문(편별 납품 규격).
    합본은 한 편의 영상이므로 여기서 `{code}_내레이션.wav` 를 얹는다(현장음은 해설 구간만 duck).
    이 단계를 빼먹으면 12편 전부 해설 없는 영상이 된다."""
    src = out / "_완성" / f"{code}.mp4"
    wav = out / code / f"{code}_내레이션.wav"
    if not src.is_file():
        return None
    if not wav.is_file():
        log(f"  [!] {code}: 내레이션 wav 없음 — 원본 그대로 사용")
        return src
    dstdir = out / "_납품"   # ★이게 최종 결과물이다(완성본 + 해설 음성)
    dstdir.mkdir(parents=True, exist_ok=True)
    dst = dstdir / f"{code}.mp4"
    if dst.is_file() and dst.stat().st_mtime > max(src.stat().st_mtime, wav.stat().st_mtime):
        log(f"  {code}: 이미 mux 됨")
        return dst
    log(f"  {code}: 내레이션 mux (duck)", )
    P.mux_narration(str(src), str(wav), str(dst), mode="duck", duck_level=0.3, log=lambda *a: None)
    return dst if dst.is_file() else src


def cmd_merge(args, cfg):
    out = Path(args.out or cfg["out_dir"])
    rows = rank_rows(out, args.src)
    parts = []
    intro = out_intro(out) / "인트로.mp4"
    if intro.is_file():
        parts.append(intro)
    else:
        print("※ 인트로.mp4 없음 — 본편만 이어붙입니다")
    print("해설 음성 입히기 (합본용)")
    for r in sorted(rows, key=lambda x: -x["rank"]):      # 꼴찌 → 1위
        f = voiced_copy(out, r["code"])
        if f:
            parts.append(f)
        else:
            print(f"  [!] {r['code']}: _완성 에 없음 — 빠집니다")
    if len(parts) < 2:
        print("[X] 이어붙일 게 없습니다")
        return 1
    dst = out / ("_통합_%s.mp4" % Path(out).name)
    xf = float(args.transition)
    if xf <= 0:
        lst = out_intro(out) / "concat.txt"
        lst.write_text("".join("file '%s'" % f.as_posix() + chr(10) for f in parts),
                       encoding="utf-8")
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
               "-c:v", "libx264", "-preset", "medium", "-crf", "20",
               "-c:a", "aac", "-b:a", "192k", "-r", "30", str(dst)]
        print("이어붙이기(하드컷) %d개 → %s" % (len(parts), dst), flush=True)
    else:
        # ★편이 바뀔 때 하드컷이면 뚝뚝 끊긴다 — xfade 로 겹쳐 넘긴다.
        #   offset 은 '지금까지 길이 - 전환시간'의 누적이라 각 구간 길이를 다 재야 한다.
        durs = [wav_dur(f) for f in parts]
        if any(d <= xf * 2 for d in durs):
            print("[X] %.1f초 전환을 감당 못 하는 짧은 구간이 있습니다" % xf)
            return 1
        ins, filt = [], []
        for f in parts:
            ins += ["-i", str(f)]
        # ★납품본은 프레임레이트가 제각각(29.94 VFR / 30000-1001 / 30)이라 그대로 xfade 하면
        #   -22 Invalid argument 로 죽는다. 30fps·yuv420p·SAR 1:1·공통 타임베이스로 먼저 맞춘다.
        for i in range(len(parts)):
            filt.append("[%d:v]fps=30,format=yuv420p,setsar=1,settb=AVTB[n%d]" % (i, i))
            filt.append("[%d:a]aresample=48000,asetpts=N/SR/TB[m%d]" % (i, i))
        vprev, aprev = "[n0]", "[m0]"
        acc = durs[0]
        n = len(parts)
        for i in range(1, n):
            off = acc - xf
            vout = "[v%d]" % i if i < n - 1 else "[vo]"
            aout = "[a%d]" % i if i < n - 1 else "[ao]"
            filt.append("%s[n%d]xfade=transition=fade:duration=%.2f:offset=%.3f%s"
                        % (vprev, i, xf, off, vout))
            filt.append("%s[m%d]acrossfade=d=%.2f:c1=tri:c2=tri%s"
                        % (aprev, i, xf, aout))
            vprev, aprev = vout, aout
            acc = off + durs[i]
        cmd = ["ffmpeg", "-y", *ins, "-filter_complex", ";".join(filt),
               "-map", "[vo]", "-map", "[ao]",
               "-c:v", "libx264", "-preset", "medium", "-crf", "20",
               "-c:a", "aac", "-b:a", "192k", "-r", "30", str(dst)]
        print("이어붙이기(%.1f초 크로스페이드) %d개 → %s  예상 %.1f분"
              % (xf, len(parts), dst, acc / 60), flush=True)
    r = subprocess.run(cmd)
    if r.returncode:
        print("[X] concat 실패")
        return r.returncode
    print("[OK] %s" % dst)
    return 0


def main():
    ap = argparse.ArgumentParser(description="섹션4 — 통합 영상 인트로 제작")
    ap.add_argument("cmd", choices=["tts", "build", "render", "merge"])
    ap.add_argument("--out", help="out_dir. 생략 시 config out_dir")
    ap.add_argument("--src", help="원본 폴더(랭킹 txt 위치) — build/merge 에 필요")
    ap.add_argument("--pool", type=int, default=500, help="이번 주 신작 총량(1번 줄 숫자)")
    ap.add_argument("--force", action="store_true", help="tts: 기존 wav 무시하고 재생성")
    ap.add_argument("--transition", default="0.6",
                    help="merge: 편 사이 크로스페이드 초. 0 이면 하드컷(기본 0.6)")
    ap.add_argument("--cap-style", default="punch", choices=["punch", "type", "slide"],
                    help="자막 연출: punch=펀치줌+휙 패닝(기본) / type=타자체 / slide=위로 슬라이드")
    args = ap.parse_args()
    cfg = _common.load_cfg()
    if args.cmd in ("build", "merge") and not args.src:
        print("[X] --src (랭킹 txt 가 있는 원본 폴더)가 필요합니다")
        return 1
    return {"tts": cmd_tts, "build": cmd_build,
            "render": cmd_render, "merge": cmd_merge}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
