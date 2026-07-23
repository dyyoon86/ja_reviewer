# -*- coding: utf-8 -*-
"""폴더 안 완성본(*.mp4) 전부에 demucs BGM 제거(vocals만)를 in-place 적용하는 배치.

번인이 끝난 최종본에 원본 BGM/현장음을 걷어내고 사람 목소리만 남긴다(채널 BGM을 따로
얹을 때 유용). 비디오(배너·자막·워터마크 번인)는 스트림 카피(무손실), 오디오만 교체.

  ⚠ 목소리만 남으므로 현장음(발소리·옷스침)도 사라져 드라이해진다 — 음악 깔린 작품용.
  demucs는 torch가 있는 파이썬이 필요하다. config `bgm_python`(시스템 파이썬 경로)로 지정하거나
  PATH의 python에서 자동 탐지한다.

사용: .venv\\Scripts\\python.exe tools\\batch_bgm.py "C:\\...\\_완성" [--python <py>] [--model htdemucs]
"""
import argparse
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server.core import bgm


def main():
    ap = argparse.ArgumentParser(description="완성본 폴더에 BGM 제거(vocals만) 일괄 적용")
    ap.add_argument("folder", help="완성본 폴더 (mp4 전부 in-place 처리)")
    ap.add_argument("--python", help="demucs 설치된 파이썬 경로 (기본 config bgm_python)")
    ap.add_argument("--model", help="demucs 모델 (기본 config bgm_model=htdemucs)")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    py = args.python or cfg.get("bgm_python")
    model = args.model or cfg.get("bgm_model") or "htdemucs"
    vids = sorted(Path(args.folder).glob("*.mp4"))
    if not vids:
        print(f"mp4 없음: {args.folder}")
        sys.exit(1)
    print(f"대상 {len(vids)}편 / demucs={model} / python={py or 'PATH 자동탐지'}")

    results = []
    for i, v in enumerate(vids, 1):
        print(f"\n{'=' * 60}\n({i}/{len(vids)}) {v.name}", flush=True)
        t0 = time.time()
        try:
            bgm.remove_bgm(str(v), str(v),
                           log=lambda m: print("  " + str(m)[:80], flush=True),
                           python=py, model=model)
            results.append((v.name, f"✔ {time.time() - t0:.0f}s"))
        except Exception as e:
            results.append((v.name, f"✘ {e}"))
            print("  실패:", e, flush=True)

    print(f"\n{'=' * 60}\n요약")
    for n, r in results:
        print(f"  {n}: {r}")
    fails = sum(1 for _, r in results if r.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
