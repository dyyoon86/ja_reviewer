# -*- coding: utf-8 -*-
r"""섹션 ①(3중 필터 순차 클린)을 ja20 12편에 **랭킹 순서대로** 실행.

batch_clean.py 는 폴더를 sorted(glob) 로 도는 탓에 가나다순이 된다. ja20 은
`ja20_1~16.txt` 의 게재 순서(= 좋아요 순위)가 곧 랭킹이라, 그 순서를 코드에 박아둔다.
txt 16건 중 실제 영상이 있는 12건만 남기고 4건(JUR-799·ALDN-609·JUR-100·SNOS-346)은 제외.

이미 만들어진 `{code}_클린.mp4` 는 stage_clean 이 재사용(스킵)하므로 중간에 끊고
다시 돌려도 안전하다.

사용: .venv\Scripts\python.exe tools\_clean_rank_ja20.py [--reverse]
  --reverse : 랭킹 역순(꼴찌 → 1위)으로 처리. ja13 때 쓰던 순서.
"""
import argparse
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401  (repo 루트 sys.path 등록)
from server import stages
from server.stages import Emitter, NullLock

SRC = Path(r"C:\Users\yoon\Desktop\2026-04-23_JA_Review\ja20")
OUT = r"F:\ja_reviewer_out\ja20"   # C: 여유 6GB — 원본 5.4GB짜리가 있어 F: 로 뺀다

# (원본랭킹, 품번, 파일명) — ja20_1~16.txt 게재 순서. 영상 없는 4건은 제외했다.
ITEMS = [
    (1,  "SNOS-373", "SNOS-373-하야사카 카논.mp4"),
    (2,  "SNOS-371", "SNOS-371-카와키타 사이카.mp4"),
    (3,  "START-627", "START-627-미야지마 메이.mp4"),
    (4,  "FNS-248",  "FNS-248-아카리 츠무기.mp4"),
    (5,  "SNOS-365", "SNOS-365-시라카미 에미카.mp4"),
    (6,  "START-622", "START-622-타다이 마히로.mp4"),
    (7,  "JUR-819",  "JUR-819-시노하라 이요.mp4"),
    (8,  "JUR-823",  "JUR-823-타케우치 유키.mp4"),
    (13, "SNOS-360", "SNOS-360-아사노 코코로.mp4"),
    (14, "SNOS-409", "SNOS-409-호시조라 네루.mp4"),
    (15, "JUR-820",  "JUR-820-사츠키 메이.mp4"),
    (16, "SDMM-238", "SDMM-238.mp4"),
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
    ap = argparse.ArgumentParser(description="ja20 섹션① 랭킹순 클린")
    ap.add_argument("--reverse", action="store_true", help="랭킹 역순(꼴찌 → 1위)")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    cfg["out_dir"] = OUT

    order = list(reversed(ITEMS)) if args.reverse else list(ITEMS)
    arrow = "꼴찌 → 1위" if args.reverse else "1위 → 꼴찌"
    print(f"대상 {len(order)}개 / 순서={arrow} / out_dir={cfg['out_dir']} / "
          f"clean_mode={cfg.get('clean_mode', 'chain')}")

    results = []
    for i, (rank, code, fname) in enumerate(order, 1):
        v = SRC / fname
        print(f"\n{'=' * 70}\n({i}/{len(order)}) [{rank}위] {fname} → 품번 {code}", flush=True)
        if not v.is_file():
            results.append((rank, code, "✘ 파일 없음 — 건너뜀", None))
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
            results.append((rank, code, f"✔ {note}", el))
        except Exception as e:
            el = time.time() - t0
            results.append((rank, code, f"✘ 실패: {e}", el))
            traceback.print_exc()

    print(f"\n{'=' * 70}\n요약 (랭킹순)")
    for rank, code, note, el in sorted(results):
        t = f" ({el / 60:.1f}분)" if el else ""
        print(f"  [{rank:2d}위] {code:10s} {note}{t}")
    fails = sum(1 for _, _, n, _ in results if n.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
