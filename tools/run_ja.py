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
             rank_tighten.py    ├ 마무리①: keep 안 대사 없는 빈 구간 제거(템포)
             rank_renarrate.py  └ 마무리②: 꼴찌 → 1위 순위 호명 내레이션(Claude)
  3 check    check_before_tts.py --fix   자막 누락 자동 수리 + 순위 호명 검사
  3b precheck 사전 눈검사 — 컷본 2초 몽타주 + 후킹 제목 누락 검사 → 사람 확인 대기
             (통과면 `_사전눈검사/통과.txt` 를 만들고 --from produce)
  4 produce  rank_produce.py --phase burn   배너 + 번인 + 노출 자동검사(TTS 없음)
  5 eyecheck 격리분 프레임 추출 → 사람 확인 대기(있을 때만)
  5b tts     rank_produce.py --phase tts    내레이션 음성 — 재컷 가능성이 끝난 뒤에 뽑는다
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
STEPS = ["clean", "review", "check", "precheck", "produce", "eyecheck", "tts", "intro", "narsub", "tidy"]


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
    # ★검출 시각은 번인 로그에만 있다. 예전엔 `_로그/produce.log` 한 파일만 봤는데 run_ja 는
    #   그 파일을 만들지 않아 늘 25/50/75% 지점으로 떨어졌다(ja22 실측) — 로그 폴더 전체를 본다.
    logs = sorted((out / "_로그").glob("*.log")) if (out / "_로그").is_dir() else []
    text = "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in logs)
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


def precheck(out, src, cfg, log=print):
    """★사전 눈검사 — 번인·TTS 에 돈을 쓰기 전에 컷본을 사람이 본다.

    ja22 실측: 섹션3 전에 2초 몽타주로 훑어 결박·접촉·가터 클로즈업·화면 'セックス' 자막을
    4편에서 걸렀다. NudeNet 은 옷 입은 성적 접촉·모자이크·화면 글자를 원리적으로 못 본다.
    같은 자리에서 **후킹 제목 누락**도 본다 — 비어 있으면 배너가 일본어 원제
    ('中出し浮気セックス…')를 인트로 5초 동안 크게 띄운다(ja22 12편 전부 비어 있었다).

    반환: 사람이 확인해야 할 게 남았으면 True(멈춤), 통과 표시가 최신이면 False.
    """
    from _ranklist import load_rank, find_rank_file, match_videos
    from server import pipeline as P
    pairs, _m, _e = match_videos(load_rank(find_rank_file(src)), src)
    dst = out / "_사전눈검사"
    dst.mkdir(parents=True, exist_ok=True)
    ok_mark = dst / "통과.txt"
    finals, no_hook = [], []
    for rank, code, _v in pairs:
        fin = out / code / f"{code}_final.mp4"
        if not fin.is_file():
            log(f"  [!] {code}: final.mp4 없음 — 섹션2를 먼저")
            continue
        finals.append(fin)
        img = dst / f"{rank:02d}위_{code}.jpg"
        if not img.is_file() or img.stat().st_mtime < fin.stat().st_mtime:
            dur = P.video_duration(str(fin)) or 60.0
            rows = max(1, -(-int(dur // 2 + 1) // 8))
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(fin), "-vf",
                            f"fps=1/2,scale=240:-1,tile=8x{rows}", "-frames:v", "1", str(img)])
        try:
            m = P.fetch_meta(cfg["meta_api"], code, lambda *_: None)
            if not str(m.get("hook_title") or "").strip():
                no_hook.append(code)
        except Exception as e:
            log(f"  [!] {code}: 메타 조회 실패({e}) — 후킹 제목 확인 못 함")
            no_hook.append(code)
    log(f"  몽타주 {len(finals)}장 → {dst}  (한 칸 = 2초, 가로 8칸 = 16초)")
    if no_hook:
        log(f"  ★후킹 제목 없음 {len(no_hook)}편: {', '.join(no_hook)}")
        log("    → works.hook_title 을 채울 것(한글 20자 이내·노골 표현 금지, 우분투 DB). "
            "비우면 배너에 일본어 원제가 뜬다")
        return True
    newest = max((f.stat().st_mtime for f in finals), default=0)
    if ok_mark.is_file() and ok_mark.stat().st_mtime >= newest:
        log("  통과 표시가 최신 컷본보다 새것 — 계속 진행")
        return False
    return True


def tidy(out, src, log=print):
    """★최종_업로드용/ 구성 — 재생 순번(꼴찌→1위)을 파일명 앞에 붙인 하드링크."""
    from _ranklist import load_rank, find_rank_file, match_videos
    # 영상 있는 편만, 빠진 순위는 당겨 매긴 번호로(내레이션 호명과 같은 번호)
    pairs, _m, _e = match_videos(load_rank(find_rank_file(src)), src)
    items = [(r, c) for r, c, _v in pairs]
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

    if any(s in todo for s in ("tts", "intro")) and not voicebox_alive(cfg["tts_base"]):
        print("\n[X] voicebox 가 응답하지 않습니다 — 먼저 띄우고 다시 실행하세요")
        return 1

    if "clean" in todo:
        if run("1 클린 (섹션1)", [PY, T / "batch_clean.py", src, "--out", out]):
            return 1
    if "review" in todo:
        if run("2 리뷰 생성 (섹션2)",
               [PY, T / "rank_review.py", "--src", src, "--out", out, "--style", args.style]):
            return 1
        # 대사 없는 빈 구간을 잘라 템포를 올린다 — keep 이 바뀌므로 내레이션보다 먼저.
        if run("2a 빈 구간 제거 (섹션2 마무리)", [PY, T / "rank_tighten.py", "--out", out]):
            return 1
        # 섹션2 초안은 단독형("이번 작품은~")이라 순위를 모른다. 섹션3은 --keep-nar 로
        # 대본을 그대로 쓰므로, 섹션2 마무리로 꼴찌 → 1위 순위 호명 대본을 확정해 둔다.
        if run("2b 순위 호명 내레이션 (섹션2 마무리)",
               [PY, T / "rank_renarrate.py", "--src", src, "--out", out, "--style", args.style]):
            return 1
    if "check" in todo:
        # 자막 누락은 자동 수리한다. 그래도 남으면 사람이 볼 문제라 멈춘다.
        if run("3 TTS 전 점검 + 자동 수리", [PY, T / "check_before_tts.py", "--out", out,
                                            "--src", src, "--fix"], allow_fail=True):
            print("\n[!] 점검에서 걸린 편이 남아 있습니다. 위 안내대로 고친 뒤")
            print(f"    python tools\\run_ja.py --src {src} --out {out} --from precheck")
            return 1
    if "precheck" in todo:
        print(f"\n{'=' * 72}\n▌3b 사전 눈검사 (번인·TTS 전)\n{'=' * 72}")
        if precheck(out, src, cfg):
            d = out / "_사전눈검사"
            print(f"\n[일시정지] {d} 의 몽타주를 **눈으로** 보세요.")
            print("  · 걸리는 구간(노출·성적 접촉·속옷·화면 속 노골 자막·미성년 설정)은 keep 에서 빼고")
            print("    tools\\rank_renarrate.py 로 대본을 다시 맞춘 뒤 몽타주를 새로 봅니다")
            print("  · 제외할 편은 원본 폴더 _제외.txt 에 품번을 적습니다")
            print(f"  · 통과면:  {d / '통과.txt'} 를 만들고 아래 명령으로 재개")
            print(f"\n  재개: python tools\\run_ja.py --src {src} --out {out} --from produce")
            return 2
    if "produce" in todo:
        if run("4 배너·번인·노출검사 (섹션3)",
               [PY, T / "rank_produce.py", "--src", src, "--out", out, "--reverse", "--keep-nar",
                "--phase", "burn"]):
            return 1
    if "eyecheck" in todo:
        print(f"\n{'=' * 72}\n▌5 노출 격리분 눈검사\n{'=' * 72}")
        flagged = eyecheck(out)
        if flagged:
            print(f"\n[일시정지] {len(flagged)}편이 격리됐습니다: {', '.join(flagged)}")
            print(f"  {out / '_검수프레임'} 의 프레임을 **눈으로** 보세요.")
            print("  · 오검출이면:  _검수필요 → _완성 으로 옮기고 아래 명령으로 재개")
            print("  · 진짜 노출이면: tools\\_dropfinal.py 로 재컷")
            print(f"\n  재개: python tools\\run_ja.py --src {src} --out {out} --from tts")
            return 2
    if "tts" in todo:
        # ★음성은 맨 뒤 — 노출검사·눈검사로 재컷될 일이 끝난 영상 길이에 맞춰 한 번만 뽑는다
        if run("5b 내레이션 TTS (섹션3 마무리)",
               [PY, T / "rank_produce.py", "--src", src, "--out", out, "--reverse", "--keep-nar",
                "--phase", "tts"]):
            return 1
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
