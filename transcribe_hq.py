#!/usr/bin/env python3
"""
transcribe_hq.py — 고품질 일본어 전사 + Claude 검증 (단독 실행 / 반복 테스트용)

일어를 몰라도 전사 품질을 판단할 수 있게:
  1) faster-whisper large-v3 (환청 억제 파라미터 + 후처리 필터)로 일본어 전사
  2) (선택) 품번으로 메타 조회 → Whisper initial_prompt 힌트 + 검증 맥락
  3) Claude CLI로 각 세그먼트 분류(대사/신음/잡음/환청) + 한국어 번역
  4) 결과물:
     - <name>_ja.srt        일본어 전사(환청 1차 필터 후)
     - <name>_ko.srt        한국어 번역(실대사만)
     - <name>_verify.md     검증 리포트(일/한 나란히, ✅=스토리대사) ← 이걸 보고 품질 판단
     - <name>_story.txt      스토리 대사만 이어붙인 텍스트(요약 입력용)

사용:
  python transcribe_hq.py --video CLIP.mp4
  python transcribe_hq.py --video CLIP.mp4 --code CAWD-980 --meta-api http://192.168.0.x:8770
  python transcribe_hq.py --video CLIP.mp4 --model large-v3 --no-verify   # 전사만
"""
import argparse, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from server import pipeline as P


def _fmt(t):
    h, r = divmod(float(t), 3600); m, s = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):06.3f}".replace(".", ",")


def write_srt(rows, path, field):
    # (start,end,text) 형태로 정상화 후 기록 — 타임스탬프 역전/겹침 제거
    triples = [(r["start"], r["end"], (r.get(field) or "").strip())
               for r in rows if (r.get(field) or "").strip()]
    triples = P.sanitize_segments(triples)
    lines = []
    for n, (a, b, txt) in enumerate(triples, 1):
        lines.append(f"{n}\n{_fmt(a)} --> {_fmt(b)}\n{txt}\n")
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return len(triples)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True, help="입력 영상(mp4 권장)")
    ap.add_argument("--code", default="", help="품번(메타 조회 → initial_prompt/검증맥락)")
    ap.add_argument("--meta-api", default="", help="메타 API base (예: http://192.168.0.x:8770)")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--which", default="claude", choices=["claude", "codex"])
    ap.add_argument("--no-verify", action="store_true", help="Claude 검증 생략(전사만)")
    ap.add_argument("--out", default="", help="출력 접두(기본: 영상파일명)")
    args = ap.parse_args()

    video = Path(args.video)
    if not video.exists():
        sys.exit(f"영상 없음: {video}")
    base = args.out or str(video.with_suffix(""))

    # 메타(선택) → initial_prompt
    meta = None
    if args.code and args.meta_api:
        try:
            meta = P.fetch_meta(args.meta_api, args.code)
        except Exception as e:
            print(f"[메타 조회 실패, 무시] {e}")
    init_prompt = P.build_initial_prompt(meta)
    if init_prompt:
        print(f"[initial_prompt] {init_prompt}")

    # ① 전사
    segs = P.transcribe(video, model_name=args.model, initial_prompt=init_prompt)
    if not segs:
        sys.exit("전사 결과 0. (VAD가 다 걸렀거나 무음) 파라미터/영상 확인 필요")

    # 전사 원본(일본어) SRT
    ja_rows = [{"start": s, "end": e, "ja": t, "ko": ""} for s, e, t in segs]
    write_srt(ja_rows, f"{base}_ja.srt", "ja")
    print(f"[저장] {base}_ja.srt  ({len(ja_rows)} 세그먼트)")

    if args.no_verify:
        print("검증 생략(--no-verify). 일본어 전사만 완료.")
        return

    # ②③ Claude 검증 + 번역
    print("Claude 검증/번역 중...")
    rows = P.verify_transcript(segs, meta=meta, which=args.which)

    # 리포트(일/한 나란히)
    P.write_verify_report(rows, f"{base}_verify.md")
    print(f"[저장] {base}_verify.md  ← 이 파일로 품질 판단(✅=스토리대사)")

    # 한국어 SRT(실대사만)
    kept = [r for r in rows if r["keep"]]
    write_srt(kept, f"{base}_ko.srt", "ko")
    print(f"[저장] {base}_ko.srt  (스토리대사 {len(kept)}줄)")

    # 스토리 텍스트(요약 입력용)
    story = " ".join((r["ko"] or "").strip() for r in kept if (r["ko"] or "").strip())
    Path(f"{base}_story.txt").write_text(story, encoding="utf-8")
    print(f"[저장] {base}_story.txt  (요약/내레이션 입력용)")

    # 요약 통계
    from collections import Counter
    c = Counter(r["type"] for r in rows)
    print(f"\n판정 요약: {dict(c)}  |  스토리대사 {len(kept)}/{len(rows)}")
    print("→ _verify.md 의 한국어가 말이 되면 전사 OK. 이상하면 Whisper부터 재점검.")


if __name__ == "__main__":
    main()
