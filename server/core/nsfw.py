#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""②-c 비주얼 노출 가드 — keep 구간 프레임을 NN(NudeNet)으로 검사해 노출 장면을 제외.

대사 기반 가드(_guard_keep)는 '대사 0줄 = 노출 의심'이라는 간접 신호만 본다.
→ 대사를 하면서 노출되는 장면은 못 잡는다. 이 모듈은 화면을 직접 보고 판정한다.

전체 영상이 아니라 **선정된 keep 구간만** 검사한다(합계 1~3분) — ffmpeg로 N초 간격
프레임 추출 후 배치 판정. 프레임당 ~0.025s라 실제 비용은 수 초.
NudeNet은 완전 로컬 ONNX 추론 — 프레임이 외부로 나가지 않는다.
"""
import json
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


def scan_video(video, step=0.25, threshold=DEFAULT_THRESHOLD, log=print):
    """영상 **전 구간**을 step 간격으로 전수 검사. 반환: [(t, class, score), ...].
    ffmpeg 1회 호출(fps 필터, 순차 디코딩) + detect_batch — 71초 완성본 0.25s 간격이 5초.
    구간별 -ss 재호출 방식보다 훨씬 빠르므로 짧은 완성본 검사에 쓴다."""
    import glob
    det = _detector()
    found = []
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
                        "-vf", f"fps={1.0 / step:g},scale=640:-1",
                        os.path.join(td, "f%05d.jpg")], check=True, timeout=1800)
        files = sorted(glob.glob(os.path.join(td, "*.jpg")))
        if not files:
            return found
        try:
            batch = det.detect_batch(files)
        except Exception:   # detect_batch 미지원 빌드 → 개별 판정으로 폴백
            batch = [det.detect(f) for f in files]
        for k, res in enumerate(batch):
            t = k * step
            for x in (res or []):
                if x.get("class") in NSFW_CLASSES and float(x.get("score", 0)) >= threshold:
                    found.append((round(t, 2), x["class"], round(float(x["score"]), 2)))
    return found


def check_final(video, step=0.25, threshold=DEFAULT_THRESHOLD, log=print):
    """최후 방어선 — 실제로 나가는 완성본을 전수 검사한다.
    keep 단위 가드는 2초 간격 샘플이라 컷 경계에 스치는 노출을 놓칠 수 있다.
    여기서는 최종 산출물 자체를 촘촘히 훑어 '나가는 물건에 노출 없음'을 보증한다.
    반환: [(t, class, score)] — 비어 있으면 통과."""
    log(f"완성본 전수 노출 검사(NudeNet, {step}s 간격)...")
    hits = scan_video(video, step, threshold, log)
    if not hits:
        log("✔ 완성본 전수 검사 통과 — 노출 검출 0")
        return hits
    top = max(hits, key=lambda x: x[2])
    log(f"🚨 완성본에서 노출 검출: {len(hits)}프레임 (최고 {top[1]} {top[2]} @ {top[0]:.1f}s)")
    for t, cls, sc in hits[:8]:
        log(f"   {t:6.1f}s  {cls} {sc}")
    return hits


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


def build_map(video, step=2.0, threshold=DEFAULT_THRESHOLD, pad=1.5, cache=None, log=print):
    """원본 **전체**를 훑어 노출 구간 지도를 만든다. 반환: [(a, b), ...] (병합된 노출 구간).

    사후 필터(고른 뒤 버리기)와 달리, 이 지도가 있으면
      · AI에게 노출 구간의 대사를 아예 안 보여줘 처음부터 클린 구간만 고르게 하고
      · 고른 구간에 노출이 스치면 그 부분만 도려낸다(구간 통째로 버리지 않음)
    171분 원본 실측 ≈ 3.6분(추출 1.5 + 추론 2.1). cache 경로를 주면 재실행 시 재사용.
    pad: 검출 시점 앞뒤 여유(초) — 샘플 간격 사이로 새는 프레임 대비."""
    if cache and Path(cache).is_file():
        try:
            data = json.loads(Path(cache).read_text(encoding="utf-8"))
            log(f"노출 지도 재사용: {len(data)}구간 ({cache})")
            return [(float(a), float(b)) for a, b in data]
        except Exception:
            pass
    log(f"전체 노출 스캔(NudeNet, {step}s 간격) — 원본 전 구간. 2시간이면 3~4분…")
    hits = scan_video(video, step, threshold, log)
    spans = []
    for t, _cls, _sc in hits:
        a, b = max(0.0, t - pad), t + pad
        if spans and a <= spans[-1][1]:      # 인접/겹침 → 병합
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))
        else:
            spans.append((a, b))
    total = sum(b - a for a, b in spans)
    log(f"노출 지도: {len(spans)}구간 / 합계 {total / 60:.1f}분 (검출 {len(hits)}프레임)")
    if cache:
        try:
            Path(cache).write_text(json.dumps([[round(a, 2), round(b, 2)] for a, b in spans]),
                                   encoding="utf-8")
        except Exception:
            pass
    return spans


def subtract(ranges, bad, min_len=2.0):
    """구간 차집합 — ranges에서 bad(노출)를 도려낸다. min_len 미만 조각은 버린다.
    구간 통째로 버리는 것보다 재료 손실이 적다(목표 길이를 채울 확률↑)."""
    out = []
    for a, b in ranges:
        cur = [(float(a), float(b))]
        for x, y in bad:
            nxt = []
            for s, e in cur:
                if y <= s or x >= e:          # 안 겹침
                    nxt.append((s, e)); continue
                if s < x:                      # 앞쪽 남는 조각
                    nxt.append((s, min(x, e)))
                if e > y:                      # 뒤쪽 남는 조각
                    nxt.append((max(y, s), e))
            cur = nxt
        out += [(s, e) for s, e in cur if e - s >= min_len]
    return out


def drop_segments(segs, bad):
    """노출 구간과 겹치는 전사 라인을 제거 — AI가 그 대사를 아예 못 보게 한다.
    (프롬프트에 '금지구간' 목록을 넣는 것보다 토큰이 안 들고 확실하다)"""
    return [s for s in segs if not any(s[0] < y and s[1] > x for x, y in bad)]


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
