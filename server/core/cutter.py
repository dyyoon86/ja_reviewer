#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""④ ffmpeg 컷 — NVENC 재인코딩 컷 + 무손실 스트림카피 컷."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .common import FFPROBE_TIMEOUT, FFMPEG_TIMEOUT, _part_path, _finalize, s2srt

_NVENC = None  # None=미확인, True/False=캐시

def has_nvenc():
    """h264_nvenc를 실제로 쓸 수 있으면 True. 1회 확인 후 캐시.
    ffmpeg 빌드에 인코더가 있어도 NVIDIA 드라이버/GPU가 없으면 런타임에 실패하므로
    GPU 존재까지 확인한다(GPU 없는 서버에서 헛된 시도·폴백 비용 방지)."""
    global _NVENC
    if _NVENC is None:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=FFPROBE_TIMEOUT)
            built = "h264_nvenc" in (out.stdout or "")
        except Exception:
            built = False
        gpu = (os.path.exists("/proc/driver/nvidia") or shutil.which("nvidia-smi") is not None
               or os.name == "nt")   # 윈도우(RTX 렌더 머신)는 시도해 본다
        _NVENC = bool(built and gpu)
    return _NVENC


def _vcodec_args(use_gpu):
    """비디오 코덱 인자. GPU(NVENC)면 RTX에서 수배 빠름, 아니면 CPU libx264."""
    if use_gpu:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", "veryfast"]


def cut_video(video, keep, out_path, log=print, progress=None):
    """keep 구간만 남겨 이어붙인다. 구간마다 fast-seek(-ss)로 바로 점프해 추출 →
    작업량이 '원본 길이'가 아니라 '남기는 길이'에 비례(긴 영상에서 짧게 남길 때 결정적).
    추출은 RTX(NVENC) 재인코딩(없으면 libx264), 합치기는 무재인코딩(stream copy).
    progress(frac 0~1) 콜백을 주면 전체 진행률을 보고한다."""
    keep = [(float(a), float(b)) for a, b in sorted(keep) if float(b) - float(a) > 0.05]
    if not keep:
        raise RuntimeError("남길 구간이 없습니다.")
    total = sum(b - a for a, b in keep) or 1.0
    gpu = [has_nvenc()]  # 리스트=폴백 시 가변
    log(f"ffmpeg 컷: {len(keep)}구간 추출 후 이어붙이기 "
        f"({'GPU·NVENC' if gpu[0] else 'CPU·libx264'}, fast-seek)...")

    def run(cmd, base, dur):
        """base=이전까지 완료된 누적 초. 이 세그 out_time을 전체 진행률로 환산."""
        if progress is None:
            subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT)
            return
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                universal_newlines=True, encoding="utf-8", errors="replace")
        try:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    try:  # 둘 다 마이크로초로 출력되는 ffmpeg 빌드가 많음
                        sec = min(dur, int(line.split("=", 1)[1]) / 1_000_000)
                        progress(max(0.0, min(0.99, (base + sec) / total)))
                    except Exception:
                        pass
            proc.wait(timeout=FFMPEG_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError(f"ffmpeg 컷이 {FFMPEG_TIMEOUT}s를 넘겨 중단했습니다(멈춤 의심)")
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd)

    with tempfile.TemporaryDirectory(prefix="jacut_") as td:
        td = Path(td)
        segs = []
        base = 0.0
        for i, (a, b) in enumerate(keep):
            dur = b - a
            seg = td / f"seg{i:03d}.mp4"

            def build(use_gpu):
                # GPU면 디코딩(NVDEC)+인코딩(NVENC) 둘 다 GPU → CPU 디코딩 병목 제거.
                pre = ["ffmpeg", "-y"]
                if use_gpu:
                    pre += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
                return (pre + ["-ss", f"{a:.3f}", "-i", str(video), "-t", f"{dur:.3f}"]
                        + _vcodec_args(use_gpu)
                        + ["-c:a", "aac", "-avoid_negative_ts", "make_zero",
                           "-progress", "pipe:1", "-nostats", str(seg)])

            log(f"  구간 {i+1}/{len(keep)}: {s2srt(a)}~{s2srt(b)} ({dur:.1f}s) 추출")
            if gpu[0]:
                try:
                    run(build(True), base, dur)
                except Exception as e:
                    log(f"  NVENC 실패({e}) → 이후 CPU(libx264)로 폴백")
                    gpu[0] = False
                    run(build(False), base, dur)
            else:
                run(build(False), base, dur)
            segs.append(seg)
            base += dur

        # 이어붙이기 — 같은 코덱/파라미터라 무재인코딩(copy)로 즉시 결합
        listf = td / "list.txt"
        listf.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs), encoding="utf-8")
        log("  이어붙이기(무재인코딩 concat)...")
        tmp = _part_path(out_path)
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf),
                        "-c", "copy", tmp], check=True, timeout=FFMPEG_TIMEOUT)
        _finalize(tmp, out_path)
    if progress:
        progress(1.0)
    log(f"컷 완료: {out_path}")


def _kf_after(video, t, window=30.0):
    """t 이후 첫 비디오 키프레임 pts. read_intervals로 근방만 스캔(전체 디먹스 안 함).
    못 찾으면 window를 넓혀 1회 재시도, 그래도 없으면 None."""
    for w in (window, window * 4):
        a = max(0.0, t - 1.0)
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0",
             "-read_intervals", f"{a:.3f}%{t + w:.3f}", str(video)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=FFPROBE_TIMEOUT)
        best = None
        for line in (r.stdout or "").splitlines():
            parts = line.strip().split(",")
            if len(parts) >= 2 and "K" in parts[1]:
                try:
                    pts = float(parts[0])
                except ValueError:
                    continue
                if pts >= t - 0.02 and (best is None or pts < best):
                    best = pts
        if best is not None:
            return best
    return None


def cut_video_copy(video, keep, out_path, log=print, progress=None):
    """무손실 고속 컷 — 재인코딩 없이 스트림 카피로 keep 구간을 이어붙인다.
    각 keep의 '시작'을 안쪽 다음 키프레임으로 스냅(마킹보다 조금 더 잘려나감 = 삭제 용도에 안전).
    끝은 카피 컷이 그대로 처리. 2시간짜리도 수십 초면 끝난다.
    키프레임을 못 찾는 구간이 있으면 RuntimeError → 호출부에서 재인코딩 폴백."""
    keep = [(float(a), float(b)) for a, b in sorted(keep) if float(b) - float(a) > 0.05]
    if not keep:
        raise RuntimeError("남길 구간이 없습니다.")
    log(f"무손실 컷(스트림 카피): {len(keep)}구간 — 키프레임 스냅 중...")
    snapped = []
    for a, b in keep:
        kf = _kf_after(video, a)
        if kf is None:
            raise RuntimeError(f"{s2srt(a)} 근방에서 키프레임을 못 찾음")
        if kf >= b - 0.2:   # 스냅했더니 구간이 사라짐 → 이 구간은 버림
            log(f"  구간 {s2srt(a)}~{s2srt(b)}: 키프레임 스냅 후 길이 0 → 제외")
            continue
        if kf - a > 0.05:
            log(f"  구간 시작 {s2srt(a)} → 키프레임 {s2srt(kf)} 스냅 (+{kf - a:.2f}s 더 잘림)")
        snapped.append((kf, b))
    if not snapped:
        raise RuntimeError("키프레임 스냅 후 남는 구간이 없습니다.")

    with tempfile.TemporaryDirectory(prefix="jacopy_") as td:
        td = Path(td)
        segs = []
        for i, (a, b) in enumerate(snapped):
            seg = td / f"seg{i:03d}.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-ss", f"{a:.6f}", "-i", str(video), "-t", f"{b - a:.6f}",
                 "-c", "copy", "-avoid_negative_ts", "make_zero", str(seg)],
                check=True, timeout=FFMPEG_TIMEOUT)
            segs.append(seg)
            if progress:
                progress(min(0.95, (i + 1) / (len(snapped) + 1)))
        listf = td / "list.txt"
        listf.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs), encoding="utf-8")
        tmp = _part_path(out_path)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(listf), "-c", "copy", tmp],
                       check=True, timeout=FFMPEG_TIMEOUT)
        _finalize(tmp, out_path)
    if progress:
        progress(1.0)
    log(f"무손실 컷 완료: {out_path}")


