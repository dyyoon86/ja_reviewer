# -*- coding: utf-8 -*-
"""MIDA-762 재클린 프로브 — 3중 필터를 다시 돌리되 **1️⃣ NN 단계는 자르지 않고 비교만** 한다.

왜 — ja16 1차 클린에서 116분 → 1.1분이 됐는데, 마지막 NN 단계가 9.7분 중 8.7분을
     걷어간 게 원인이었다. 그 8.7분의 근거가 '직접노출 116프레임'보다 '살노출 312프레임'
     (SKIN_RATIO 윈도우 판정)에 쏠려 있어, FNS-235 식사대화 39분 오판과 같은 유형일
     가능성이 있다. 중간본이 지워져 확인이 불가능하므로 s2(9.7분)를 되살려 보존하고
     NN을 현재 설정 / 완화 설정 두 가지로 '측정만' 해서 차이를 눈으로 본다.

출력: {out}/MIDA-762_s2.mp4 (2️⃣+3️⃣ 통과분) + 두 설정의 제거 구간 리포트
사용: .venv\\Scripts\\python.exe tools\\_reclean_762.py
"""
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401  (repo 루트 sys.path 등록)
from server import pipeline as P
from server import stages
from server.core import moan, nsfw, intimacy

CODE = "MIDA-762"
SRC = Path(r"C:\Users\yoon\Desktop\2026-04-23_JA_Review\ja16\MIDA-762-이시카와 미오.mp4")
OUT = Path(r"F:\ja_reviewer_out\ja16") / CODE

# 완화 설정 — 살노출 윈도우 판정을 얼마나 느슨하게 볼지
RELAXED_SKIN_RATIO = 0.60   # 기본 0.30 → 윈도우의 60% 이상이 살노출일 때만 정사로 본다


def log(m):
    print(f"[{CODE}] {m}", flush=True)


def prog(label):
    box = {"d": -1}

    def f(fr):
        d = int(max(0.0, min(1.0, fr)) * 10)
        if d != box["d"]:
            box["d"] = d
            print(f"[{CODE}]    {label} {d * 10}%", flush=True)
    return f


def spans_report(spans, total, title):
    cut = sum(b - a for a, b in spans)
    log(f"  ── {title}: {len(spans)}구간 / {cut / 60:.1f}분 제거 "
        f"→ {(total - cut) / 60:.1f}분 유지")
    for a, b in spans:
        log(f"       {a / 60:>5.1f}m ~ {b / 60:>5.1f}m  ({b - a:.0f}s)")
    return cut


def main():
    c = _common.load_cfg()
    c["out_dir"] = str(OUT.parent)          # ★ config는 ja14를 가리키므로 ja16으로 덮어씀
    OUT.mkdir(parents=True, exist_ok=True)
    if not SRC.is_file():
        raise SystemExit(f"원본 없음: {SRC}")

    t0 = time.time()
    src = str(SRC)
    total = P.video_duration(src) or 0.0
    log(f"원본 {total / 60:.1f}분 — {SRC.name}")

    # ── 2️⃣ 소리(신음·정사) ─────────────────────────────────────────
    s1 = OUT / f"{CODE}_재클린_s1.mp4"
    if s1.is_file():
        log(f"s1 재사용: {s1}")
    else:
        log("── (1/3) 2️⃣ 소리(신음·정사) 스캔")
        bad = moan.scan_audio(src, model_name=c.get("scan_model", "small"), log=log,
                              progress=prog("소리 스캔"),
                              pad=float(c.get("cut_pad_moan", 5.0)))[0]
        keep = nsfw.complement(bad, total, min_len=float(c.get("nsfw_min_clip", 3.0)))
        if not keep:
            raise SystemExit("2️⃣에서 전부 제거됨")
        spans_report(bad, total, "2️⃣ 소리")
        try:
            P.cut_video_smart(src, keep, str(s1), log, prog("소리 컷"))
        except Exception as e:
            log(f"  스마트 컷 실패({e}) → 재인코딩 폴백")
            P.cut_video(src, keep, str(s1), log, prog("소리 컷"))
    src = str(s1)
    total = P.video_duration(src) or 0.0
    log(f"s1 = {total / 60:.1f}분")

    # ── 3️⃣ 의미(스킨십·애무) ───────────────────────────────────────
    s2 = OUT / f"{CODE}_재클린_s2.mp4"
    if s2.is_file():
        log(f"s2 재사용: {s2}")
    else:
        log("── (2/3) 3️⃣ 의미(스킨십·애무) 스캔")
        bad = intimacy.scan_intimacy(src, step=float(c.get("intimacy_step", 2.0)),
                                     threshold=float(c.get("intimacy_threshold", 0.02)),
                                     min_dur=float(c.get("intimacy_min_dur", 14.0)),
                                     log=log, duration=total, progress=prog("의미 스캔"))
        keep = nsfw.complement(bad, total, min_len=float(c.get("nsfw_min_clip", 3.0)))
        if not keep:
            raise SystemExit("3️⃣에서 전부 제거됨")
        spans_report(bad, total, "3️⃣ 의미")
        try:
            P.cut_video_smart(src, keep, str(s2), log, prog("의미 컷"))
        except Exception as e:
            log(f"  스마트 컷 실패({e}) → 재인코딩 폴백")
            P.cut_video(src, keep, str(s2), log, prog("의미 컷"))
    src = str(s2)
    total = P.video_duration(src) or 0.0
    log(f"★ s2 보존 = {total / 60:.1f}분  ({s2})")

    # ── 1️⃣ 화면(NN) — 자르지 않고 두 설정으로 측정만 ────────────────
    log("── (3/3) 1️⃣ 화면(NN 노출) — 컷 없이 측정만, 두 설정 비교")
    step = float(c.get("nsfw_scan_step", 1.0))
    th = float(c.get("nsfw_clean_threshold", 0.22))
    pad = float(c.get("nsfw_pad", 3.0))
    gap = float(c.get("nsfw_merge_gap", 12.0))

    base_ratio = nsfw.SKIN_RATIO
    log(f"[A] 현재 설정 (SKIN_RATIO={base_ratio})")
    a_spans = nsfw.build_map(src, step=step, threshold=th, pad=pad, merge_gap=gap,
                             cache=None, log=log, duration=total,
                             progress=prog("화면 스캔 A"))
    a_cut = spans_report(a_spans, total, f"[A] SKIN_RATIO={base_ratio}")

    nsfw.SKIN_RATIO = RELAXED_SKIN_RATIO          # 모듈 상수 일시 완화(이 프로세스 한정)
    log(f"[B] 완화 설정 (SKIN_RATIO={RELAXED_SKIN_RATIO})")
    b_spans = nsfw.build_map(src, step=step, threshold=th, pad=pad, merge_gap=gap,
                             cache=None, log=log, duration=total,
                             progress=prog("화면 스캔 B"))
    b_cut = spans_report(b_spans, total, f"[B] SKIN_RATIO={RELAXED_SKIN_RATIO}")
    nsfw.SKIN_RATIO = base_ratio

    log("=" * 60)
    log(f"s2 총 {total / 60:.1f}분")
    log(f"  [A] 현재  → {(total - a_cut) / 60:.1f}분 남음")
    log(f"  [B] 완화  → {(total - b_cut) / 60:.1f}분 남음")
    log(f"  차이 {(a_cut - b_cut) / 60:.1f}분 — 이 구간이 '살노출 윈도우'로만 잘린 부분")
    log(f"완료 ({(time.time() - t0) / 60:.1f}분)")


if __name__ == "__main__":
    main()
