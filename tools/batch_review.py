# -*- coding: utf-8 -*-
"""폴더 안 영상 전부에 섹션 ②(리뷰생성: ①전사 → ②AI → ③자막)를 일괄 실행.

⓪ 클린(batch_clean.py)을 먼저 마친 품번들을 대상으로 하며, 큐 풀오토와 동일한
스테이지 함수·프리셋(pos=solo, mode=config fullauto_mode, target=config target_sec)을 쓴다.

사용: .venv\\Scripts\\python.exe tools\\batch_review.py "C:\\...\\영상폴더" [--meta URL]
- 영상은 품번 결정에만 쓰고, 실제 입력은 {out_dir}/{품번}/ 의 클린본(state.video)이다.
- {품번}_plan.json 이 이미 있으면 완료작으로 보고 건너뜀(--redo 로 강제 재실행).
- --meta 로 config의 meta_api 주소를 오버라이드(우분투 다운 시 로컬 폴백용).
- 한 품번이 실패해도 다음 품번 계속, 마지막에 요약.
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
from batch_clean import CliEmitter, guess_code


def main():
    ap = argparse.ArgumentParser(description="섹션 ② 리뷰생성(전사→AI→자막) 일괄 실행")
    ap.add_argument("folder", help="원본 영상 폴더 (품번 결정용)")
    ap.add_argument("--out", help="out_dir 오버라이드 (예: ...\\ja_reviewer_out\\ja15). "
                                  "생략 시 studio_config.json의 out_dir. batch_clean과 동일 — "
                                  "모음집을 연달아 돌릴 때 config를 건드리지 않으려고 둔다.")
    ap.add_argument("--meta", help="meta_api 주소 오버라이드 (예: http://127.0.0.1:8770)")
    ap.add_argument("--redo", action="store_true", help="plan.json 있어도 다시 실행")
    args = ap.parse_args()

    videos = sorted(Path(args.folder).glob("*.mp4"))
    if not videos:
        print(f"mp4 없음: {args.folder}")
        sys.exit(1)

    cfg = _common.load_cfg()
    if args.out:
        cfg["out_dir"] = args.out
    if args.meta:
        cfg["meta_api"] = args.meta
    mode = cfg.get("fullauto_mode", "summary")
    target = int(cfg.get("target_sec", 60))
    llm = cfg.get("llm", "claude")
    print(f"대상 {len(videos)}개 / out_dir={cfg['out_dir']} / meta={cfg['meta_api']} / "
          f"llm={llm} / mode={mode} / target={target}s / pos=solo")

    results = []
    for i, v in enumerate(videos, 1):
        code = guess_code(v.name)
        print(f"\n{'=' * 70}\n({i}/{len(videos)}) {code or '??'} ← {v.name}", flush=True)
        if not code:
            results.append((v.name, "품번 추정 실패 — 건너뜀", None))
            continue
        em = CliEmitter(code)
        outdir = stages.work_dir(cfg, code)
        if not args.redo and (outdir / f"{code}_plan.json").is_file():
            print(f"[{code}] plan.json 이미 존재 — 완료작으로 보고 건너뜀(--redo 로 재실행)")
            results.append((code, "이미 완료 — 건너뜀", None))
            continue

        st = stages.load_state(outdir, code)
        video = st.get("video")
        if not video or not Path(video).is_file():
            clean = outdir / f"{code}_클린.mp4"
            video = str(clean) if clean.is_file() else None
        if not video:
            results.append((code, "✘ 클린본 없음 — ⓪(batch_clean)을 먼저 실행", None))
            continue

        t0 = time.time()
        try:
            # ① 전사 — 큐와 동일하게 메타로 initial_prompt 힌트(실패 시 힌트 없이)
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
            # ② AI — 풀오토 프리셋과 동일(watcher._fullauto_opts 참조)
            stages.stage_ai(cfg, code, video, target, llm, mode, "", em,
                            gpu=NullLock(), pos="solo", style="3min")
            # ③ 자막
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
    for name, note, el in results:
        t = f" ({el / 60:.1f}분)" if el else ""
        print(f"  {name}: {note}{t}")
    fails = sum(1 for _, n, _ in results if n.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
