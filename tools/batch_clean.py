# -*- coding: utf-8 -*-
"""폴더 안 영상 전부에 섹션 ①(⓪ 3중 필터 순차 클린: 2️⃣소리→3️⃣의미→1️⃣화면)만 일괄 실행.

사용: .venv\\Scripts\\python.exe tools\\batch_clean.py "C:\\...\\영상폴더"
- 파일명에서 품번 추정(서버 guess_code와 동일 정규식). 추정 실패 파일은 건너뜀.
- {out_dir}/{품번}/{품번}_클린.mp4 이 이미 있으면 stage_clean이 재사용(스킵).
- 한 파일이 실패해도 다음 파일을 계속 진행, 마지막에 전체 요약을 출력.
- GPU 경합 방지를 위해 순차 실행(한 번에 한 영상).
"""
import argparse
import re
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401  (repo 루트 sys.path 등록)
from server import stages
from server.stages import Emitter, NullLock

CODE_RE = re.compile(r"([A-Za-z]{2,6})-?(\d{2,5})")  # app.guess_code와 동일


def guess_code(name):
    m = CODE_RE.search(Path(name).stem)
    return f"{m.group(1)}-{m.group(2)}".upper() if m else ""


class CliEmitter(Emitter):
    """콘솔용 — 진행률은 10% 단위로만 찍어 로그 폭주 방지."""
    def __init__(self, code):
        self.code = code
        self._decile = -1

    def log(self, msg):
        print(f"[{self.code}] {msg}", flush=True)

    def step(self, n, total, label):
        self._decile = -1
        print(f"[{self.code}] ── ({n}/{total}) {label}", flush=True)

    def prog(self, frac, label=None):
        d = int(max(0.0, min(1.0, frac)) * 10)
        if d != self._decile:
            self._decile = d
            print(f"[{self.code}]    {label or '진행'} {d * 10}%", flush=True)

    def file(self, tag, path):
        print(f"[{self.code}] 📄 {tag}: {path}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="섹션 ① 3중 필터 클린 일괄 실행")
    ap.add_argument("folder", help="영상 폴더 (mp4 전부 처리)")
    ap.add_argument("--out", help="out_dir 오버라이드 (예: ...\\ja_reviewer_out\\ja15). "
                                  "생략 시 studio_config.json의 out_dir. 모음집을 연달아 "
                                  "돌릴 때 config를 건드리지 않으려고 둔다.")
    args = ap.parse_args()

    folder = Path(args.folder)
    videos = sorted(folder.glob("*.mp4"))
    if not videos:
        print(f"mp4 없음: {folder}")
        sys.exit(1)

    cfg = _common.load_cfg()
    if args.out:
        cfg["out_dir"] = args.out
    print(f"대상 {len(videos)}개 / out_dir={cfg['out_dir']} / clean_mode={cfg.get('clean_mode', 'chain')}")

    results = []
    for i, v in enumerate(videos, 1):
        code = guess_code(v.name)
        print(f"\n{'=' * 70}\n({i}/{len(videos)}) {v.name} → 품번 {code or '??'}", flush=True)
        if not code:
            results.append((v.name, "품번 추정 실패 — 건너뜀", None))
            continue
        t0 = time.time()
        try:
            r = stages.stage_clean(cfg, code, str(v), CliEmitter(code), gpu=NullLock())
            el = time.time() - t0
            if r.get("reused"):
                note = "기존 클린본 재사용"
            elif r.get("cut") is False:
                note = "검출 0 — 원본 그대로"
            else:
                note = (f"{r.get('removed_sec', 0) / 60:.1f}분 제거 → "
                        f"{r.get('kept_sec', 0) / 60:.1f}분 유지")
            results.append((v.name, f"✔ {note}", el))
        except Exception as e:
            el = time.time() - t0
            results.append((v.name, f"✘ 실패: {e}", el))
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
