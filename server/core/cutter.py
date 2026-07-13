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

# 컷 경계 오디오 페이드 — 파형이 0이 아닌 지점에서 뚝 잘려 이어붙으면 매 컷마다 "틱" 팝이
# 난다(3분휴지형은 현장음을 30% 더킹으로 깔아 그대로 들린다). 30ms면 귀에 안 들리면서
# 팝만 없앤다. ★keep '경계'에만 넣는다 — smart-cut은 keep 내부를 GOP 조각으로 쪼개므로
# 조각마다 넣으면 문장 한가운데서 소리가 꺼진다.
FADE = 0.03


def _fade_af(dur, fade_in=True, fade_out=True, fade=FADE):
    """세그먼트 오디오 페이드 필터. 너무 짧은 조각은 페이드 폭을 줄인다.
    반환: ['-af', '...'] 또는 [] (넣을 게 없으면)."""
    dur = float(dur)
    d = min(float(fade), max(0.005, dur / 3.0))
    parts = []
    if fade_in:
        parts.append(f"afade=t=in:st=0:d={d:.3f}")
    if fade_out and dur > d:
        parts.append(f"afade=t=out:st={dur - d:.3f}:d={d:.3f}")
    return ["-af", ",".join(parts)] if parts else []

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
                        + ["-c:a", "aac"] + _fade_af(dur)   # 컷 경계 팝 제거
                        + ["-avoid_negative_ts", "make_zero",
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


def _video_codec(video):
    """v:0 코덱명(h264/hevc/...). 실패 시 ''."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(video)],
            timeout=FFPROBE_TIMEOUT)
        return out.decode().strip().lower()
    except Exception:
        return ""


def _kf_scan(video, a, b):
    """[a,b] 구간의 비디오 키프레임 pts 목록(read_intervals — 전체 디먹스 안 함)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0",
         "-read_intervals", f"{max(0.0, a):.3f}%{b:.3f}", str(video)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=FFPROBE_TIMEOUT)
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                out.append(float(parts[0]))
            except ValueError:
                continue
    return sorted(out)


def _kf_before(video, t, window=30.0):
    """t 이전(같음 포함) 마지막 키프레임 pts. 못 찾으면 window 넓혀 1회 재시도 후 None."""
    for w in (window, window * 4):
        kfs = [k for k in _kf_scan(video, t - w, t + 1.0) if k <= t + 0.02]
        if kfs:
            return kfs[-1]
    return None


def cut_video_smart(video, keep, out_path, log=print, progress=None):
    """스마트 컷 — keep 경계 근방(GOP 몇 초)만 재인코딩하고 중간은 스트림 카피.

    · 정밀도: 재인코딩 컷과 같은 frame-accurate 경계 (카피 컷의 '키프레임 스냅 손실' 없음)
    · 속도: 작업량이 '경계 조각 몇 초'에만 비례 — 2시간 영상에서 대부분 남겨도 수십 초
    · 방법: 각 keep을 [경계 재인코딩][키프레임~키프레임 카피][경계 재인코딩] 조각으로 나눠
      MPEG-TS(코덱 파라미터 in-band라 concat에 관대)로 만든 뒤 이어붙여 mp4로 재먹싱.
      오디오는 조각 균일성을 위해 전 조각 AAC 재인코딩(속도 영향 미미).
    · h264 소스 전용. 실패/길이 불일치 시 RuntimeError → 호출부에서 카피/재인코딩 폴백.
    """
    keep = [(float(a), float(b)) for a, b in sorted(keep) if float(b) - float(a) > 0.05]
    if not keep:
        raise RuntimeError("남길 구간이 없습니다.")
    codec = _video_codec(video)
    if codec != "h264":
        raise RuntimeError(f"smart-cut은 h264 소스 전용(현재 '{codec or '?'}')")
    expected = sum(b - a for a, b in keep)
    gpu = [has_nvenc()]
    log(f"스마트 컷: {len(keep)}구간 — 경계만 재인코딩({'NVENC' if gpu[0] else 'libx264'}), 중간은 카피...")

    # 조각 목록 만들기: (mode, start, dur, fin, fout)  mode='copy'|'enc'
    #   fin/fout = 이 조각이 keep의 '첫/마지막' 조각인가 → 거기에만 오디오 페이드를 건다.
    #   (조각마다 걸면 keep 내부 GOP 경계에서 소리가 꺼져 문장이 끊긴다)
    pieces = []
    for a, b in keep:
        kf_a = _kf_after(video, a)
        kf_b = _kf_before(video, b)
        # 키프레임을 못 찾거나 카피할 중간이 사실상 없으면 통째로 재인코딩
        if kf_a is None or kf_b is None or kf_b - kf_a < 1.0 or kf_a >= b or kf_b <= a:
            pieces.append(("enc", a, b - a, True, True))
            continue
        head = kf_a - a > 0.05
        tail = b - kf_b > 0.05
        if head:
            pieces.append(("enc", a, kf_a - a, True, False))
        pieces.append(("copy", kf_a, kf_b - kf_a, not head, not tail))
        if tail:
            pieces.append(("enc", kf_b, b - kf_b, False, True))
    n_enc = sum(1 for p in pieces if p[0] == "enc")
    enc_sec = sum(p[2] for p in pieces if p[0] == "enc")
    log(f"  조각 {len(pieces)}개 (재인코딩 {n_enc}개·{enc_sec:.1f}s / 카피 {len(pieces) - n_enc}개)")

    def _run(cmd):
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=FFMPEG_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or "").strip()[-300:] or f"ffmpeg 실패({r.returncode})")

    with tempfile.TemporaryDirectory(prefix="jasmart_") as td:
        td = Path(td)
        files = []
        for i, (mode, st, dur, fin, fout) in enumerate(pieces):
            seg = td / f"p{i:03d}.ts"
            af = _fade_af(dur, fade_in=fin, fade_out=fout)   # keep 경계에만
            if mode == "copy":
                _run(["ffmpeg", "-y", "-loglevel", "error",
                      "-ss", f"{st:.6f}", "-i", str(video), "-t", f"{dur:.6f}",
                      "-c:v", "copy", "-c:a", "aac"] + af
                     + ["-avoid_negative_ts", "make_zero", "-f", "mpegts", str(seg)])
            else:
                def enc_cmd(use_gpu, _af=af, _st=st, _dur=dur, _seg=seg):
                    return (["ffmpeg", "-y", "-loglevel", "error",
                             "-ss", f"{_st:.6f}", "-i", str(video), "-t", f"{_dur:.6f}"]
                            + _vcodec_args(use_gpu)
                            + ["-c:a", "aac"] + _af
                            + ["-avoid_negative_ts", "make_zero",
                               "-f", "mpegts", str(_seg)])
                if gpu[0]:
                    try:
                        _run(enc_cmd(True))
                    except Exception as e:
                        log(f"  NVENC 실패({e}) → 이후 libx264")
                        gpu[0] = False
                        _run(enc_cmd(False))
                else:
                    _run(enc_cmd(False))
            files.append(seg)
            if progress:
                progress(min(0.9, (i + 1) / (len(pieces) + 1)))

        listf = td / "list.txt"
        listf.write_text("".join(f"file '{p.as_posix()}'\n" for p in files), encoding="utf-8")
        tmp = _part_path(out_path)
        _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
              "-i", str(listf), "-c", "copy", "-bsf:a", "aac_adtstoasc",
              "-movflags", "+faststart", tmp])

        # 길이 검증 — 조각 이어붙이기가 어긋나면 폴백하도록 여기서 실패시킨다
        from .common import video_duration
        got = video_duration(tmp)
        tol = max(2.0, expected * 0.02)
        if abs(got - expected) > tol:
            try:
                Path(tmp).unlink()
            except OSError:
                pass
            raise RuntimeError(f"결과 길이 불일치(기대 {expected:.1f}s, 실제 {got:.1f}s)")
        _finalize(tmp, out_path)
    if progress:
        progress(1.0)
    log(f"스마트 컷 완료: {out_path} ({expected:.1f}s)")


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
    """고속 컷 — **비디오는 스트림 카피(무손실)**, 오디오만 AAC 재인코딩해 컷 경계에
    30ms 페이드를 건다(팝 제거. 오디오 재인코딩은 몇 초라 속도에 영향 없음).
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
                 "-c:v", "copy", "-c:a", "aac"] + _fade_af(b - a)
                + ["-avoid_negative_ts", "make_zero", str(seg)],
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


