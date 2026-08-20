# -*- coding: utf-8 -*-
"""섹션 ①(3중 필터 순차 클린)을 '13' 리스트의 12건에만 일괄 실행.
batch_clean.py 와 동일 로직이되, 폴더 전체가 아니라 명시한 파일만 처리한다.
사용: .venv\\Scripts\\python.exe tools\\_clean_sel.py
"""
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401  (repo 루트 sys.path 등록)
from server import stages
from server.stages import Emitter, NullLock

SRC = Path(r"C:\Users\yoon\Desktop\2026-04-23_JA_Review\ja13")

# '13' 문서에 실린 12작품 (품번 → 실제 파일명)
ITEMS = [
    ("PRED-879",   "PRED-879-미요시 유카.mp4"),
    ("START-600",  "START-600-나츠메 히비키.mp4"),
    ("EBWH-348",   "EBWH-348-아오이 이부키 (1).mp4"),
    ("PRWF-014",   "PRWF-014-코마츠 소라.mp4"),
    ("PRED-886",   "PRED-886-사츠키 나오 (1).mp4"),
    ("MFYD-165",   "MFYD-165-사에구사 레이.mp4"),
    ("START-614",  "START-614-미야지마 메이.mp4"),
    ("HMN-880",    "HMN-880-이츠카이치 메이.mp4"),
    ("DANDYA-043", "DANDYA-043-나가세 마미.mp4"),
    ("EBWH-342",   "EBWH-342-카시와기 후미카.mp4"),
    ("MFYD-161",   "MFYD-161-토츠키 루이사.mp4"),
    ("MIZD-531",   "MIZD-531-나나사와 미아.mp4"),
]


class CliEmitter(Emitter):
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
    cfg = _common.load_cfg()
    print(f"대상 {len(ITEMS)}개 / out_dir={cfg['out_dir']} / clean_mode={cfg.get('clean_mode', 'chain')}")

    # '13' 리스트는 1순위→12순위. 랭킹 역순(12위 → 1위)으로 처리.
    order = list(reversed(list(enumerate(ITEMS, 1))))  # (rank, (code, fname))
    results = []
    for i, (rank, (code, fname)) in enumerate(order, 1):
        v = SRC / fname
        print(f"\n{'=' * 70}\n({i}/{len(order)}) [{rank}위] {fname} → 품번 {code}", flush=True)
        if not v.is_file():
            results.append((code, "파일 없음 — 건너뜀", None))
            print(f"[{code}] ✘ 파일 없음: {v}", flush=True)
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
            results.append((code, f"✔ {note}", el))
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
