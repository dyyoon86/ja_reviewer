# -*- coding: utf-8 -*-
"""완료된 품번 전부의 내레이션을 일괄 재생성 — server/core/regen.regen_narration 사용.

6슬롯(인트로2+갭3+아웃트로1) 규칙이라 (a) 시간순 강제 배치로 순서 꼬임이 없고
(b) 아웃트로가 질문형이라 개별 영상에 채널 마무리 인사가 붙지 않는다
(모음집 맨 끝 인사는 마지막 편에서 별도 처리).

사용: .venv\\Scripts\\python.exe tools\\batch_regen_nar.py "C:\\...\\영상폴더" [--meta URL]
- plan.json 있는 품번만 대상. 실패해도 다음 품번 계속, 마지막에 요약.
"""
import argparse
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server.core.regen import regen_narration
from server import stages
from batch_clean import guess_code


def main():
    ap = argparse.ArgumentParser(description="내레이션 일괄 재생성")
    ap.add_argument("folder", help="원본 영상 폴더 (품번 결정용)")
    ap.add_argument("--meta", help="meta_api 주소 오버라이드")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    meta_api = args.meta or cfg["meta_api"]
    codes = sorted({guess_code(v.name) for v in Path(args.folder).glob("*.mp4")} - {""})
    print(f"대상 {len(codes)}개 / meta={meta_api}")

    results = []
    for i, code in enumerate(codes, 1):
        outdir = Path(cfg["out_dir"]) / code
        print(f"\n{'=' * 70}\n({i}/{len(codes)}) {code}", flush=True)
        if not (outdir / f"{code}_plan.json").is_file():
            results.append((code, "plan 없음 — 건너뜀"))
            continue
        t0 = time.time()
        try:
            nar = regen_narration(outdir, meta_api,
                                  log=lambda m: print(f"[{code}] {m}", flush=True))
            results.append((code, f"✔ 내레이션 {len(nar)}개 ({(time.time() - t0) / 60:.1f}분)"))
        except Exception as e:
            results.append((code, f"✘ 실패: {e}"))
            traceback.print_exc()

    print(f"\n{'=' * 70}\n요약")
    for code, note in results:
        print(f"  {code}: {note}")
    fails = sum(1 for _, n in results if n.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
