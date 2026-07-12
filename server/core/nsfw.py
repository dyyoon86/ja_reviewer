#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""②-c 비주얼 노출 가드 — keep 구간 프레임을 NN(NudeNet)으로 검사해 노출 장면을 제외.

대사 기반 가드(_guard_keep)는 '대사 0줄 = 노출 의심'이라는 간접 신호만 본다.
→ 대사를 하면서 노출되는 장면은 못 잡는다. 이 모듈은 화면을 직접 보고 판정한다.

전체 영상이 아니라 **선정된 keep 구간만** 검사한다(합계 1~3분) — ffmpeg로 N초 간격
프레임 추출 후 배치 판정. 프레임당 ~0.025s라 실제 비용은 수 초.
NudeNet은 완전 로컬 ONNX 추론 — 프레임이 외부로 나가지 않는다.
"""
import os
import subprocess
import tempfile
from pathlib import Path

# 유튜브에 나가면 안 되는 노출 클래스(NudeNet 3.x 라벨).
# 가슴·성기·항문·남성기 노출만 차단한다. BELLY/FEET/ARMPIT 등 일상 노출은 무시.
NSFW_CLASSES = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
}
DEFAULT_THRESHOLD = 0.35   # 실측: 본편 노출 0.42~0.48 / 인터뷰 구간 검출 0 — 안전하게 낮게 잡음
DEFAULT_STEP = 2.0         # 프레임 샘플 간격(초)

_DETECTOR = None


def _detector():
    global _DETECTOR
    if _DETECTOR is None:
        from nudenet import NudeDetector
        _DETECTOR = NudeDetector()
    return _DETECTOR


def scan_ranges(video, ranges, step=DEFAULT_STEP, threshold=DEFAULT_THRESHOLD, log=print):
    """keep 구간별 노출 검사. 반환: {구간index: [(t, class, score), ...]} (검출된 것만)."""
    det = _detector()
    hits = {}
    with tempfile.TemporaryDirectory() as td:
        for i, (a, b) in enumerate(ranges):
            a, b = float(a), float(b)
            # 구간이 짧아도 최소 양끝은 본다
            ts = [a + k * step for k in range(max(1, int((b - a) / step)))]
            if ts[-1] < b - 0.2:
                ts.append(max(a, b - 0.2))
            frames = []
            for k, t in enumerate(ts):
                f = os.path.join(td, f"r{i:03d}_{k:03d}.jpg")
                try:   # 폭 640으로 줄여 추론 비용↓(검출 정확도엔 영향 미미)
                    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}",
                                    "-i", str(video), "-frames:v", "1", "-vf", "scale=640:-1", f],
                                   check=True, timeout=60)
                    if os.path.isfile(f):
                        frames.append((t, f))
                except Exception:
                    continue
            found = []
            for t, f in frames:
                try:
                    for x in (det.detect(f) or []):
                        if x.get("class") in NSFW_CLASSES and float(x.get("score", 0)) >= threshold:
                            found.append((t, x["class"], round(float(x["score"]), 2)))
                except Exception as e:
                    log(f"  ※ 프레임 판정 실패({type(e).__name__}) — 건너뜀")
            if found:
                hits[i] = found
    return hits


def guard_keep_visual(keep, video, log=print, step=DEFAULT_STEP, threshold=DEFAULT_THRESHOLD):
    """노출 검출된 keep 구간을 제외한다. 대사 기반 _guard_keep과 같은 안전망 규약:
    전부 걸리면 판단 불가로 보고 원본 유지(결과 없음보단 사람 검수).
    모델 로드 실패 등은 예외를 올려 호출측이 '가드 생략'으로 처리하게 한다."""
    keep = [(float(a), float(b)) for a, b in keep]
    if not keep:
        return keep
    total = sum(b - a for a, b in keep)
    log(f"비주얼 노출 검사(NudeNet): {len(keep)}구간 합계 {total:.0f}s, {step}s 간격")
    hits = scan_ranges(video, keep, step, threshold, log)
    if not hits:
        log("비주얼 검사 통과: 노출 검출 0")
        return keep
    ok = [r for i, r in enumerate(keep) if i not in hits]
    for i, found in sorted(hits.items()):
        a, b = keep[i]
        top = max(found, key=lambda x: x[2])
        log(f"⚠ keep {a:.0f}~{b:.0f}s: 노출 검출({top[1]} {top[2]}, {len(found)}프레임) — 자동 제외")
    if not ok:
        log("※ 모든 keep에서 노출이 검출됨 — 안전장치를 건너뜁니다(원본 유지). 결과를 꼭 검수하세요")
        return keep
    log(f"비주얼 안전장치: keep {len(keep)}→{len(ok)}구간 (제외 {len(keep) - len(ok)})")
    return ok
