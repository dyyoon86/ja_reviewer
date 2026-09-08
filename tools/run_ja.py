# -*- coding: utf-8 -*-
r"""ja 배치 원커맨드 — 섹션1 → 섹션4를 한 번에 돌린다.

배치마다 명령을 예닐곱 개 순서대로 치던 것을 하나로 묶는다. **사람이 반드시 봐야 하는
두 지점에서만 멈춘다**:

  · 노출 검출로 격리된 편(`_검수필요`) — 프레임을 뽑아 두고 멈춘다. 기계는 못 본다.
    ja20 6/11, ja21 2/2 가 전부 오검출이었다. 로그만 보고 재컷하면 멀쩡한 편을 망친다.
  · 인트로 대본(`section4/멘트.txt`) 과 주간 신작 총량(`--pool`) — 매주 다르다.

단계
  1 clean    batch_clean.py     ⓪ 3중 필터 클린
  2 review   rank_review.py     ①②③ 전사·AI·자막
  3 check    check_before_tts.py --fix   자막 누락 자동 수리 + 순위 호명 검사
  4 produce  rank_produce.py    ①내레이션 ②배너 ③TTS ④번인
  5 eyecheck 격리분 프레임 추출 → 사람 확인 대기(있을 때만)
  6 intro    section4_intro.py tts → build → render
  7 narsub   add_narsub.py      납품본에 해설 음성 + 해설 자막
  8 tidy     ★최종_업로드용/ 구성 + 00_안내.md

사용:
  .venv\Scripts\python.exe tools\run_ja.py --src "C:\...\ja23" --out F:\ja_reviewer_out\ja23
  ... --pool 500            # 인트로 1번 줄 숫자(주간 신작 총량)
  ... --from produce        # 중간부터 (clean|review|check|produce|intro|narsub|tidy)
  ... --to check            # 여기까지만
  ... --skip-intro          # 섹션4 없이 편별 납품본까지만

★voicebox 는 미리 띄워 둘 것(3·4·6단계가 쓴다). 안 떠 있으면 4단계에서 멈춘다.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
T = Path(__file__).resolve().parent
STEPS = ["clean", "review", "check", "produce", "eyecheck", "intro", "narsub", "tidy"]


def run(title, cmd, allow_fail=False):
    print(f"\n{'=' * 72}\n▌{title}\n{'=' * 72}\n$ " + " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run([str(c) for c in cmd], cwd=str(ROOT))
    if r.returncode and not allow_fail:
        print(f"\n[X] {title} 실패(exit {r.returncode}) — 여기서 멈춥니다")
    return r.returncode


def voicebox_alive(base):
    try:
        import urllib.request
        urllib.request.urlopen(base, timeout=3)
        return True
    except Exception as e:
        return "refus" not in str(e).lower() and "실패" not in str(e)


def eyecheck(out, log=print):
    """격리분에서 검출 시각 프레임을 뽑아 둔다 — 사람이 볼 거리를 만들어 놓고 멈춘다."""
    q = out / "_검수필요"
    files = sorted(q.glob("*.mp4")) if q.is_dir() else []
    if not files:
        log("격리분 없음 — 눈검사 건너뜀")
        return []
    import re
    logf = out / "_로그" / "produce.log"
    text = logf.read_text(encoding="utf-8", errors="replace") if logf.is_file() else ""
    dst = out / "_검수프레임"
    dst.mkdir(parents=True, exist_ok=True)
    for f in files:
        code = f.stem
        times = [float(x) for x in re.findall(rf"\[{re.escape(code)}\]\s+([\d.]+)s\s+[A-Z_]+ 0\.",
                                              text)][:4]
        if not times:
            import subprocess as sp
            d = sp.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(f)], capture_output=True, text=True).stdout.strip()
            dur = float(d or 60)
            times = [dur * r for r in (0.25, 0.5, 0.75)]
        for t in times:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(f),
                            "-frames:v", "1", "-vf", "scale=900:-1",
                            str(dst / f"{code}_{t:.1f}s.jpg")])
        log(f"  {code}: 프레임 {len(times)}장 → {dst}")
    return [f.stem for f in files]


def tidy(out, src, log=print):
    """★최종_업로드용/ 구성 — 재생 순번(꼴찌→1위)을 파일명 앞에 붙인 하드링크."""
    from _ranklist import load_rank, find_rank_file
    items = load_rank(find_rank_file(src))
    fin = out / "_납품_자막"
    dst = out / "★최종_업로드용"
    dst.mkdir(parents=True, exist_ok=True)
    for f in dst.glob("*.mp4"):
        f.unlink()
    intro = out / "_인트로" / "인트로.mp4"
    if intro.is_file():
        os.link(intro, dst / "00_인트로.mp4")
    n = 0
    for seq, (rank, code) in enumerate(sorted(items, key=lambda x: -x[0]), 1):
        srcf = fin / f"{code}.mp4"
        if not srcf.is_file():
            log(f"  [!] {code}: 최종본 없음")
            continue
        os.link(srcf, dst / f"{seq:02d}_{rank}위_{code}.mp4")
        n += 1
    log(f"  ★최종_업로드용 {n}편" + (" + 인트로" if intro.is_file() else ""))
    for junk in ("_시안",):
        d = out / junk
        if d.is_dir():
            import shutil
            shutil.rmtree(d, ignore_errors=True)
    return n


def main():
    ap = argparse.ArgumentParser(description="ja 배치 원커맨드(섹션1~4)")
    ap.add_argument("--src", required=True, help="원본 영상 폴더(랭킹 txt 포함)")
    ap.add_argument("--out", required=True, help="배치 출력 폴더")
    ap.add_argument("--pool", type=int, default=500, help="인트로 1번 줄 숫자(주간 신작 총량)")
    ap.add_argument("--from", dest="start", default="clean", choices=STEPS)
    ap.add_argument("--to", dest="stop", default="tidy", choices=STEPS)
    ap.add_argument("--skip-intro", action="store_true", help="섹션4 없이 편별 납품본까지만")
    ap.add_argument("--style", default="3min", help="문체(기본 3min)")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    src, out = Path(args.src), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "_로그").mkdir(exist_ok=True)
    lo, hi = STEPS.index(args.start), STEPS.index(args.stop)
    todo = [s for i, s in enumerate(STEPS) if lo <= i <= hi]
    if args.skip_intro:
        todo = [s for s in todo if s not in ("intro",)]
    print(f"배치 {out.name} — 단계: {' → '.join(todo)}")
    print(f"소스 {src}\nvoicebox {cfg.get('tts_base')} / meta {cfg.get('meta_api')}")

    if any(s in todo for s in ("produce", "intro")) and not voicebox_alive(cfg["tts_base"]):
        print("\n[X] voicebox 가 응답하지 않습니다 — 먼저 띄우고 다시 실행하세요")
        return 1

    if "clean" in todo:
        if run("1 클린 (섹션1)", [PY, T / "batch_clean.py", src, "--out", out]):
            return 1
    if "review" in todo:
        if run("2 리뷰 생성 (섹션2)",
               [PY, T / "rank_review.py", "--src", src, "--out", out, "--style", args.style]):
            return 1
    if "check" in todo:
        # 자막 누락은 자동 수리한다. 그래도 남으면 사람이 볼 문제라 멈춘다.
        if run("3 TTS 전 점검 + 자동 수리", [PY, T / "check_before_tts.py", "--out", out,
                                            "--src", src, "--fix"], allow_fail=True):
            print("\n[!] 점검에서 걸린 편이 남아 있습니다. 위 안내대로 고친 뒤")
            print(f"    python tools\\run_ja.py --src {src} --out {out} --from produce")
            return 1
    if "produce" in todo:
        if run("4 최종 생산 (섹션3)",
               [PY, T / "rank_produce.py", "--src", src, "--out", out, "--reverse", "--keep-nar"]):
            return 1
    if "eyecheck" in todo:
        print(f"\n{'=' * 72}\n▌5 노출 격리분 눈검사\n{'=' * 72}")
        flagged = eyecheck(out)
        if flagged:
            print(f"\n[일시정지] {len(flagged)}편이 격리됐습니다: {', '.join(flagged)}")
            print(f"  {out / '_검수프레임'} 의 프레임을 **눈으로** 보세요.")
            print("  · 오검출이면:  _검수필요 → _완성 으로 옮기고 아래 명령으로 재개")
            print("  · 진짜 노출이면: tools\\_dropfinal.py 로 재컷")
            print(f"\n  재개: python tools\\run_ja.py --src {src} --out {out} --from intro")
            return 2
    if "intro" in todo:
        script = ROOT / "section4" / "멘트.txt"
        print(f"\n[확인] 인트로 대본: {script}")
        print(f"       주간 신작 총량 --pool {args.pool}")
        for c in ("tts", "build", "render"):
            cmd = [PY, T / "section4_intro.py", c, "--out", out]
            if c == "build":
                cmd += ["--src", src, "--pool", args.pool]
            if run(f"6 인트로 {c} (섹션4)", cmd):
                return 1
    if "narsub" in todo:
        if run("7 해설 음성 + 해설 자막",
               [PY, T / "section4_intro.py", "merge", "--out", out, "--src", src,
                "--transition", "0"], allow_fail=True):
            print("  [!] 합본은 실패했지만 _납품 은 만들어졌을 수 있습니다 — 계속합니다")
        if run("7b 해설 자막 얹기", [PY, T / "add_narsub.py", "--out", out]):
            return 1
    if "tidy" in todo:
        print(f"\n{'=' * 72}\n▌8 정리\n{'=' * 72}")
        tidy(out, src)

    print(f"\n{'=' * 72}\n✔ 끝. 업로드할 것: {out / '★최종_업로드용'}\n{'=' * 72}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
