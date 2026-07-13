#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""⑦ self-eval — 렌더 '결과물'을 기계로 검사해 결함을 리포트한다.

왜 — 지금까지 컷 경계 팝·정지 화면·자막 누락은 사람이 눈/귀로 찾아야 했다.
video-use의 self-eval(렌더 후 컷 경계 ±1.5s의 프레임·파형을 떠서 검사)을 우리 맥락에
맞춘 것. **판정만 하고 고치지는 않는다** — 결함 목록을 로그/리포트로 내보내고,
사람이 볼지 재생성할지 정한다(3회 룰: 같은 결함이 3번 반복되면 자동수정 포기 표시).

검사 항목
  1) 오디오 팝 — 컷 경계에서 파형이 급변하면 "틱" 소리가 난다. 경계 ±40ms 구간의
     샘플 최대 진폭 점프를 astats로 측정. cutter의 30ms 페이드가 실제로 먹었는지 검증.
  2) 정지 화면 — freezedetect. 컷이 어긋나 같은 프레임이 반복되면 잡힌다.
  3) 무음 — silencedetect. 오디오가 통째로 빠진 컷(더킹 사고 등).
  4) 자막 공백 — 완성본 길이 대비 자막이 덮는 시간 비율. 자막 굽기가 실패하거나
     타이밍이 어긋나면 급락한다.
전부 ffmpeg 필터라 추가 의존성이 없고, 3분짜리 완성본에 수 초.
"""
import re
import subprocess
from pathlib import Path

from .common import FFMPEG_TIMEOUT, srt_parse, video_duration


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=FFMPEG_TIMEOUT)


def _pcm(video, start, dur, sr=48000):
    """구간의 모노 PCM(float -1~1). numpy 없으면 None."""
    try:
        import numpy as np
    except ImportError:
        return None
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.4f}", "-i", str(video),
         "-t", f"{dur:.4f}", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
        capture_output=True, timeout=FFMPEG_TIMEOUT)
    if r.returncode != 0 or not r.stdout:
        return None
    return np.frombuffer(r.stdout, dtype="<i2").astype("float32") / 32768.0


def audio_pops(video, boundaries, win=0.2, slew=0.45, quiet=0.01, log=print, sr=48000):
    """컷 경계의 팝 = **소리가 0으로 떨어지는 속도가 급격한가**로 판정한다.

    ★ 실측으로 확정한 두 가지 (2026-07-13, step2_stt.mp4 큰 소리 지점 컷):
      ① 세그먼트를 각각 AAC 인코딩해 concat하면 **오디오 접합점이 '누적 영상 길이'보다
         세그먼트당 ~30ms씩 누적으로 밀린다**(측정: 1번째 경계 +28ms, 2번째 +63ms).
         → 계산한 경계 시각에서 재면 접합점을 통째로 빗나간다. 넓은 창(±win)에서
           **엔벨로프 최저점**을 찾아 그게 진짜 접합점이라고 본다.
      ② AAC 프라이밍 때문에 페이드가 없어도 접합점 자체는 무음이 된다 → '골짜기 유무'로는
         판정할 수 없다. 팝을 만드는 건 그 무음으로 **들어가는 속도**다.
         측정: 페이드 없음 60.8%/ms(급격=팝) vs 30ms 페이드 9.5~33.9%/ms(완만).

    판정: 접합점 직전 40ms에서 1ms당 최대 낙폭이 peak의 slew(기본 45%)를 넘으면 팝.
    원래 조용한 컷(peak<quiet)은 팝이 날 수 없으므로 건너뛴다.
    반환: [(t, t_splice, drop_pct, peak)]"""
    try:
        import numpy as np
    except ImportError:
        log("  ※ numpy 없음 — 컷 경계 검사 생략")
        return []
    hits = []
    blk = max(1, int(0.001 * sr))          # 1ms 블록
    for t in boundaries:
        x = _pcm(video, t - win, win * 2, sr)
        if x is None or len(x) < blk * 8:
            continue
        n = (len(x) // blk) * blk
        env = np.abs(x[:n].reshape(-1, blk)).max(axis=1)
        peak = float(env.max())
        if peak < quiet:                   # 원래 조용한 컷 — 팝이 날 수 없다
            continue
        i_min = int(env.argmin())          # 접합점 = 가장 조용한 지점
        t_splice = t - win + i_min * 0.001
        lead = env[max(0, i_min - 40): i_min + 1]      # 골짜기로 들어가는 40ms
        if len(lead) < 2:
            continue
        drop = float(np.abs(np.diff(lead)).max()) / peak
        if drop > slew:
            hits.append((round(t, 2), round(t_splice, 3), round(drop * 100, 1),
                         round(peak, 4)))
    return hits


def freezes(video, min_sec=1.5, noise=0.003):
    """정지 화면 구간 — freezedetect. 반환: [(start, dur)]"""
    r = _run(["ffmpeg", "-v", "info", "-i", str(video),
              "-vf", f"freezedetect=n={noise}:d={min_sec}", "-map", "0:v",
              "-f", "null", "-"])
    out, start = [], None
    for m in re.finditer(r"freeze_(start|duration|end):\s*(-?\d+\.?\d*)", r.stderr or ""):
        kind, val = m.group(1), float(m.group(2))
        if kind == "start":
            start = val
        elif kind == "duration" and start is not None:
            out.append((round(start, 2), round(val, 2)))
            start = None
    return out


def silences(video, min_sec=2.0, thresh_db=-45.0):
    """무음 구간 — silencedetect. 반환: [(start, dur)]"""
    r = _run(["ffmpeg", "-v", "info", "-i", str(video),
              "-af", f"silencedetect=n={thresh_db}dB:d={min_sec}", "-f", "null", "-"])
    out, start = [], None
    for m in re.finditer(r"silence_(start|duration):\s*(-?\d+\.?\d*)", r.stderr or ""):
        kind, val = m.group(1), float(m.group(2))
        if kind == "start":
            start = val
        elif start is not None:
            out.append((round(start, 2), round(val, 2)))
            start = None
    return out


def subtitle_coverage(srt_files, duration):
    """완성본 길이 중 자막이 덮는 비율(0~1). 자막 굽기 실패/타이밍 붕괴를 잡는다."""
    if not duration:
        return 0.0
    spans = []
    for f in srt_files:
        if f and Path(f).is_file():
            spans += [(a, b) for a, b, _t in srt_parse(f)]
    if not spans:
        return 0.0
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return min(1.0, sum(b - a for a, b in merged) / duration)


def boundaries_from_keep(keep):
    """원본 keep → **완성본 타임라인**의 컷 경계 시각(누적 길이). 시작/끝은 제외."""
    ts, acc = [], 0.0
    for a, b in keep[:-1]:
        acc += float(b) - float(a)
        ts.append(acc)
    return ts


def evaluate(video, keep=None, srt_files=(), log=print, cfg=None):
    """완성본 자체 검사. 반환: {"issues":[{kind,detail,...}], "ok":bool, ...}
    판정만 한다 — 고치지 않는다."""
    cfg = cfg or {}
    dur = video_duration(video) or 0.0
    log(f"자체 검사(self-eval) — 완성본 {dur:.0f}초")
    issues = []

    bounds = boundaries_from_keep(keep or [])
    if bounds:
        pops = audio_pops(video, bounds, slew=float(cfg.get("eval_pop_slew", 0.45)), log=log)
        for t, ts, drop, pk in pops:
            issues.append({"kind": "pop", "t": t,
                           "detail": f"컷 경계 {t:.1f}s(접합 {ts:.2f}s) 소리가 1ms 만에 "
                                     f"{drop:.0f}% 급락 — 팝. 페이드가 안 걸렸는지 확인"})
        log(f"  컷 경계 {len(bounds)}곳 페이드 검사 → 팝 {len(pops)}건")

    fz = freezes(video, min_sec=float(cfg.get("eval_freeze_sec", 1.5)))
    for t, d in fz:
        issues.append({"kind": "freeze", "t": t,
                       "detail": f"{t:.1f}s부터 {d:.1f}초 정지 화면"})
    si = silences(video, min_sec=float(cfg.get("eval_silence_sec", 2.5)))
    for t, d in si:
        issues.append({"kind": "silence", "t": t,
                       "detail": f"{t:.1f}s부터 {d:.1f}초 무음"})
    log(f"  정지 화면 {len(fz)}건 · 무음 {len(si)}건")

    cov = subtitle_coverage(srt_files, dur)
    min_cov = float(cfg.get("eval_min_sub_coverage", 0.30))
    if cov < min_cov:
        issues.append({"kind": "subs", "t": 0,
                       "detail": f"자막이 영상의 {cov * 100:.0f}%만 덮음(기준 {min_cov * 100:.0f}%) "
                                 f"— 자막 누락/타이밍 붕괴 의심"})
    log(f"  자막 커버리지 {cov * 100:.0f}%")

    if issues:
        log(f"⚠ 자체 검사: 결함 {len(issues)}건 — 결과를 확인하세요")
        for it in issues[:8]:
            log(f"   · {it['detail']}")
    else:
        log("✔ 자체 검사 통과 — 팝·정지·무음·자막 이상 없음")
    return {"ok": not issues, "issues": issues, "duration": round(dur, 1),
            "sub_coverage": round(cov, 3), "boundaries": len(bounds)}
