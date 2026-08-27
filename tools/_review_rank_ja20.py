# -*- coding: utf-8 -*-
r"""섹션 ②(리뷰생성: ①전사 → ②AI → ③자막)를 ja20 에 **랭킹 순서대로** 실행.

batch_review.py 와 같은 스테이지·프리셋(pos=solo, style=3min, mode=config fullauto_mode)을
쓰되, 폴더 sorted(glob) 가나다순 대신 `ja20_1~16.txt` 게재 순서(= 좋아요 순위)로 돈다.
`_clean_rank_ja20.py` 와 짝.

★batch_review 대비 달라진 점 2가지:
  1. `stage_ai` 앞에서 **클린본 길이를 먼저 재고**, keep 재료가 target×min_keep_ratio 에
     못 미치면 stage_ai 를 부르지 않고 건너뛴다. stage_ai 의 P.fetch_meta 는 try 로
     감싸여 있지 않아, 어차피 중단될 편을 태우면 예외 추적만 지저분해진다.
  2. 편별 target 오버라이드(`--target-override 품번=초`). 클린본이 짧은 편만 낮춰 돌린다.

사용: .venv\Scripts\python.exe tools\_review_rank_ja20.py [--reverse] [--redo]
                                  [--only A,B] [--skip C] [--target-override SNOS-409=60]
"""
import argparse
import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server import pipeline as P
from server.stages import NullLock
from batch_clean import CliEmitter

SRC = Path(r"C:\Users\yoon\Desktop\2026-04-23_JA_Review\ja20")
OUT = r"F:\ja_reviewer_out\ja20"
META = "http://172.30.1.40:8770"        # 우분투. 죽으면 _meta_shim.py 로 갈아탄다

# (원본랭킹, 품번) — ja20_1~16.txt 게재 순서. 영상 없는 4건 제외.
ITEMS = [
    (1, "SNOS-373"), (2, "SNOS-371"), (3, "START-627"), (4, "FNS-248"),
    (5, "SNOS-365"), (6, "START-622"), (7, "JUR-819"), (8, "JUR-823"),
    (13, "SNOS-360"), (14, "SNOS-409"), (15, "JUR-820"), (16, "SDMM-238"),
]

# 클린본이 본편형이라 재료가 안 나오는 편 — 섹션① 실측으로 확정된 것만 적는다.
#   SNOS-360: 142분 → 7.4초. 2️⃣소리가 138.3분(대사 세그먼트 33개뿐인 ASMR/펠라물),
#             남은 4.0분마저 3️⃣의미가 통째로 애무 판정. ja13 MFYD-161·ja18 SNOS-357 과 동종.
DEFAULT_SKIP = {"SNOS-360"}


def probe_dur(path):
    try:
        o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True)
        return float(o.stdout.strip())
    except Exception:
        return 0.0


def main():
    ap = argparse.ArgumentParser(description="ja20 섹션② 랭킹순 리뷰생성")
    ap.add_argument("--reverse", action="store_true", help="랭킹 역순(꼴찌 → 1위)")
    ap.add_argument("--redo", action="store_true", help="plan.json 있어도 다시 실행")
    ap.add_argument("--only", default="", help="이 품번만(쉼표 구분)")
    ap.add_argument("--skip", default="", help="추가로 제외할 품번(쉼표 구분)")
    ap.add_argument("--no-default-skip", action="store_true",
                    help="DEFAULT_SKIP(본편형 제외 목록)을 무시하고 전부 시도")
    ap.add_argument("--target", type=int, help="목표 길이(초). 생략 시 config target_sec")
    ap.add_argument("--target-override", action="append", default=[],
                    metavar="품번=초", help="편별 목표 길이 (여러 번 지정 가능)")
    ap.add_argument("--meta", default=META, help="meta_api 주소")
    ap.add_argument("--llm", help="LLM 오버라이드(codex|claude). 생략 시 config llm. "
                                 "codex OAuth 가 죽었을 때 claude 로 우회한다 — 다만 "
                                 "성인물 대본은 헤드리스 거부가 날 수 있다.")
    args = ap.parse_args()

    skip = set(() if args.no_default_skip else DEFAULT_SKIP)
    skip |= {c.strip().upper() for c in args.skip.split(",") if c.strip()}
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}
    tov = {}
    for kv in args.target_override:
        k, _, v = kv.partition("=")
        tov[k.strip().upper()] = int(v)

    cfg = _common.load_cfg()
    cfg["out_dir"] = OUT
    cfg["meta_api"] = args.meta
    mode = cfg.get("fullauto_mode", "summary")
    base_target = int(args.target or cfg.get("target_sec", 60))
    llm = args.llm or cfg.get("llm", "claude")
    min_ratio = float(cfg.get("min_keep_ratio", 0.5))

    order = list(reversed(ITEMS)) if args.reverse else list(ITEMS)
    arrow = "꼴찌 → 1위" if args.reverse else "1위 → 꼴찌"
    print(f"대상 {len(order)}개 / 순서={arrow} / out_dir={cfg['out_dir']} / meta={cfg['meta_api']} / "
          f"llm={llm} / mode={mode} / target={base_target}s / pos=solo / style=3min")
    if skip:
        print(f"제외: {', '.join(sorted(skip))}")

    results = []
    for i, (rank, code) in enumerate(order, 1):
        print(f"\n{'=' * 70}\n({i}/{len(order)}) [{rank}위] {code}", flush=True)
        if code in skip or (only and code not in only):
            print(f"[{code}] 대상 아님 — 건너뜀")
            results.append((rank, code, "— 제외", None))
            continue

        em = CliEmitter(code)
        outdir = stages.work_dir(cfg, code)
        if not args.redo and (outdir / f"{code}_plan.json").is_file():
            print(f"[{code}] plan.json 이미 존재 — 건너뜀(--redo 로 재실행)")
            results.append((rank, code, "✔ 이미 완료 — 건너뜀", None))
            continue

        st = stages.load_state(outdir, code)
        video = st.get("video")
        if not video or not Path(video).is_file():
            clean = outdir / f"{code}_클린.mp4"
            video = str(clean) if clean.is_file() else None
        if not video:
            results.append((rank, code, "✘ 클린본 없음 — 섹션①을 먼저", None))
            print(f"[{code}] ✘ 클린본 없음")
            continue

        target = tov.get(code, base_target)
        dur = probe_dur(video)
        need = target * min_ratio
        if dur < need:
            msg = (f"⚠ 재료부족 — 클린본 {dur:.0f}s < 필요 {need:.0f}s "
                   f"(target {target}s × {min_ratio}). 본편형으로 보입니다")
            print(f"[{code}] {msg}")
            results.append((rank, code, msg, None))
            continue
        print(f"[{code}] 클린본 {dur / 60:.1f}분 / target {target}s", flush=True)

        t0 = time.time()
        try:
            # ① 전사 — 메타로 initial_prompt 힌트(실패해도 힌트 없이 진행)
            if stages.transcribe_fresh(outdir, code, video):
                em.log("① 전사 이미 있음(같은 영상) — 재사용")
            else:
                init = None
                try:
                    m = P.fetch_meta(cfg["meta_api"], code, em.log)
                    init = P.build_initial_prompt(m) or None
                except Exception as e:
                    em.log(f"※ 메타 조회 실패({e}) → 힌트 없이 전사 진행")
                stages.stage_transcribe(cfg, code, video, cfg["whisper_model"], em,
                                        initial_prompt=init)
            # ② AI (★fetch_meta 가 try 밖이라 meta_api 가 죽으면 여기서 즉사한다)
            stages.stage_ai(cfg, code, video, target, llm, mode, "", em,
                            gpu=NullLock(), pos="solo", style="3min")
            # ③ 자막
            stages.stage_subs(cfg, code, em)

            el = time.time() - t0
            plan = json.loads((outdir / f"{code}_plan.json").read_text(encoding="utf-8"))
            keep = P.parse_keep(plan.get("keep", []))
            kept = sum(b - a for a, b in keep)
            nar = len(plan.get("narration", []))
            dia = len(plan.get("dialogue", []))
            flag = "  ★대사 0 — _apply_dialogue 필요" if dia == 0 else ""
            results.append((rank, code, f"✔ keep {len(keep)}구간 {kept:.0f}s / "
                                        f"내레이션 {nar} / 대사 {dia}{flag}", el))
        except Exception as e:
            el = time.time() - t0
            results.append((rank, code, f"✘ 실패: {e}", el))
            traceback.print_exc()

    print(f"\n{'=' * 70}\n요약 (랭킹순)")
    for rank, code, note, el in sorted(results):
        t = f" ({el / 60:.1f}분)" if el else ""
        print(f"  [{rank:2d}위] {code:10s} {note}{t}")
    fails = sum(1 for _, _, n, _ in results if n.startswith("✘"))
    done = sum(1 for _, _, n, _ in results if n.startswith("✔"))
    print(f"\n성공 {done} / 실패 {fails} / 전체 {len(results)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
