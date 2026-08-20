# -*- coding: utf-8 -*-
"""섹션 ②(리뷰생성: ①전사 → ②AI → ③자막)를 '13' 리스트 12건에만, 랭킹 역순으로 실행.
batch_review.py 와 동일 로직이되 폴더 전체가 아니라 명시한 12품번만 처리한다.
클린본({out_dir}/{품번}/{품번}_클린.mp4)이 있어야 함(섹션1 선행).
사용: .venv\\Scripts\\python.exe tools\\_review_sel.py [--meta URL] [--redo]
"""
import argparse
import json
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

# '13' 리스트 1순위→12순위 (섹션2도 역순으로 처리)
ITEMS = [
    ("PRED-879",   "1위"),
    ("START-600",  "2위"),
    ("EBWH-348",   "3위"),
    ("PRWF-014",   "4위"),
    ("PRED-886",   "5위"),
    ("MFYD-165",   "6위"),
    ("START-614",  "7위"),
    ("HMN-880",    "8위"),
    ("DANDYA-043", "9위"),
    ("EBWH-342",   "10위"),
    ("MFYD-161",   "11위"),
    ("MIZD-531",   "12위"),
]


def main():
    ap = argparse.ArgumentParser(description="섹션 ② 리뷰생성 (12건 역순)")
    ap.add_argument("--meta", help="meta_api 주소 오버라이드")
    ap.add_argument("--redo", action="store_true", help="plan.json 있어도 재실행")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    if args.meta:
        cfg["meta_api"] = args.meta
    mode = cfg.get("fullauto_mode", "summary")
    target = int(cfg.get("target_sec", 60))
    llm = cfg.get("llm", "claude")
    order = list(reversed(ITEMS))  # 12위 → 1위
    print(f"대상 {len(order)}개 / meta={cfg['meta_api']} / llm={llm} / mode={mode} / target={target}s / pos=solo")

    results = []
    for i, (code, rank) in enumerate(order, 1):
        print(f"\n{'=' * 70}\n({i}/{len(order)}) [{rank}] {code}", flush=True)
        em = CliEmitter(code)
        outdir = stages.work_dir(cfg, code)
        if not args.redo and (outdir / f"{code}_plan.json").is_file():
            print(f"[{code}] plan.json 이미 존재 — 건너뜀(--redo 로 재실행)")
            results.append((code, "이미 완료 — 건너뜀", None))
            continue

        st = stages.load_state(outdir, code)
        video = st.get("video")
        if not video or not Path(video).is_file():
            clean = outdir / f"{code}_클린.mp4"
            video = str(clean) if clean.is_file() else None
        if not video:
            results.append((code, "✘ 클린본 없음 — 섹션1(클린)을 먼저 실행", None))
            print(f"[{code}] ✘ 클린본 없음", flush=True)
            continue

        t0 = time.time()
        try:
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
            stages.stage_ai(cfg, code, video, target, llm, mode, "", em,
                            gpu=NullLock(), pos="solo", style="3min")
            stages.stage_subs(cfg, code, em)

            el = time.time() - t0
            plan = json.loads((outdir / f"{code}_plan.json").read_text(encoding="utf-8"))
            keep = P.parse_keep(plan.get("keep", []))
            kept = sum(b - a for a, b in keep)
            results.append((code, f"✔ keep {len(keep)}구간 {kept:.0f}s, "
                                  f"내레이션 {len(plan.get('narration', []))}개", el))
        except Exception as e:
            el = time.time() - t0
            results.append((code, f"✘ 실패: {e}", el))
            traceback.print_exc()

    print(f"\n{'=' * 70}\n요약")
    for code, note, el in results:
        t = f" ({el / 60:.1f}분)" if el else ""
        print(f"  {code}: {note}{t}")
    fails = sum(1 for _, n, _ in results if n.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
