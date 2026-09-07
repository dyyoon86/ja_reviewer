# -*- coding: utf-8 -*-
r"""원본 풀전사 + 줄거리 요약 **백필** — 섹션2를 돌리기 전에 미리 채워 둔다.

왜 필요한가:
  · `_원본전사.json` 은 섹션1의 2️⃣ 소리 필터가 남긴다. 그 기능을 넣기 **전에** 클린한
    편들은 전사본이 없다(클린 도중 코드를 고쳐도 이미 떠 있는 프로세스는 옛 코드로 돈다).
  · `_줄거리.md` 는 섹션2 산출물이라, 섹션2를 안 돌렸으면 당연히 없다.

섹션2를 돌리면 `stages.story_brief` 가 알아서 둘 다 만든다(자가치유). 다만 그러면
컷 선정과 뒤섞여 진행되어 **사람이 줄거리를 먼저 읽어 볼 수가 없다.**
이 도구는 그 부분만 떼어 미리 돌린다 — 결과를 눈으로 확인하고 섹션2로 넘어가면 된다.

하는 일(편당):
  ① `_원본전사.json` 이 없으면 원본(state.source_video)을 scan_model 로 전사 → json + srt
  ② 그 전사본으로 줄거리 브리핑 생성, 사이트 소개문(랭킹 txt)과 교차 검증
  ③ `_줄거리.md`(읽기용) + `_줄거리.txt`(프롬프트 주입용) 저장

이미 있으면 건너뛴다(`--redo` 로 강제).

사용:
  .venv\Scripts\python.exe tools\backfill_story.py --src "C:\...\ja21" --out "F:\ja_reviewer_out\ja21"
  옵션: [--only ABF-382,MIKR-118] [--redo] [--llm codex|claude]
"""
import argparse
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from _ranklist import load_rank, find_rank_file, match_videos
from server import stages
from server import pipeline as P
from batch_clean import CliEmitter


def main():
    ap = argparse.ArgumentParser(description="원본 풀전사 + 줄거리 요약 백필")
    ap.add_argument("--src", help="원본 영상 폴더(랭킹 txt가 같이 있는 곳). 순서·소개문에 쓴다")
    ap.add_argument("--out", help="out_dir. 생략 시 config out_dir")
    ap.add_argument("--only", default="", help="이 품번만(쉼표 구분)")
    ap.add_argument("--redo", action="store_true", help="줄거리가 이미 있어도 다시 만든다")
    ap.add_argument("--llm", help="LLM 오버라이드. 생략 시 config llm")
    ap.add_argument("--meta", help="meta_api 주소 오버라이드")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    if args.out:
        cfg["out_dir"] = args.out
    if args.meta:
        cfg["meta_api"] = args.meta
    if args.src:
        cfg["rank_src"] = args.src          # 사이트 소개문 출처(교차 검증 기준)
    llm = args.llm or cfg.get("llm", "claude")
    outdir = Path(cfg["out_dir"])
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}

    # 랭킹 순서가 있으면 그 순서로, 없으면 폴더 이름순
    codes = None
    if args.src:
        try:
            pairs, _m, _e = match_videos(load_rank(find_rank_file(args.src)), args.src)
            codes = [c for _r, c, _v in pairs]
        except Exception as e:
            print(f"※ 랭킹 txt를 못 읽었습니다({e}) — 폴더 이름순으로 진행")
    if codes is None:
        codes = sorted(d.name for d in outdir.iterdir()
                       if d.is_dir() and not d.name.startswith("_"))
    codes = [c for c in codes if (outdir / c).is_dir()]
    if only:
        codes = [c for c in codes if c.upper() in only]
    if not codes:
        print(f"대상이 없습니다: {outdir}")
        return 1

    print(f"out_dir={outdir} / llm={llm} / 대상 {len(codes)}편")
    if cfg.get("rank_src"):
        print(f"소개문 출처: {cfg['rank_src']}\n")

    results = []
    for i, code in enumerate(codes, 1):
        d = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        print(f"\n{'=' * 70}\n({i}/{len(codes)}) {code}", flush=True)
        md = d / f"{code}_줄거리.md"
        if md.is_file() and not args.redo:
            print(f"[{code}] 줄거리 이미 있음 — 건너뜀(--redo 로 재생성)")
            results.append((code, "✔ 이미 있음", None))
            continue
        st = stages.load_state(d, code)
        if not st.get("source_video"):
            results.append((code, "✘ 원본 경로 없음 — 섹션①을 먼저", None))
            print(f"[{code}] ✘ state에 source_video 없음")
            continue
        if args.redo:
            for f in (d / f"{code}_줄거리.txt", md):
                if f.is_file():
                    f.unlink()
        t0 = time.time()
        try:
            meta = {}
            try:
                meta = P.fetch_meta(cfg["meta_api"], code, em.log)
            except Exception as e:
                em.log(f"※ 메타 조회 실패({e}) — 메타 없이 진행")
            brief = stages.story_brief(cfg, code, meta, llm, em)
            el = time.time() - t0
            if not brief:
                results.append((code, "△ 줄거리 없음(대사 부족/실패)", el))
                continue
            head = ""
            for ln in brief.splitlines():
                if ln.startswith("[사이트 소개문과의 대조]"):
                    head = ln.replace("[사이트 소개문과의 대조]", "").strip()
                    break
            results.append((code, f"✔ 완료{('  ' + head[:60]) if head else ''}", el))
        except Exception as e:
            results.append((code, f"✘ 실패: {e}", time.time() - t0))
            traceback.print_exc()

    print(f"\n{'=' * 70}\n요약")
    for code, note, el in results:
        t = f" ({el / 60:.1f}분)" if el else ""
        print(f"  {code:12} {note}{t}")
    fails = sum(1 for _c, n, _e in results if n.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    print(f"\n읽어 볼 것: {outdir}\\{{품번}}\\_산출물\\2_리뷰\\{{품번}}_줄거리.md")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
