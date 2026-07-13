#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""원본 BGM 제거 — 최종 컷의 오디오에서 배경음악을 걷어내고 사람 목소리만 남긴다.

왜 — 원본 AV의 BGM이 깔린 채로 내레이션(TTS)을 얹으면 소리가 지저분하게 엉킨다.
목소리만 남기면 ① 대사가 또렷하게 들리고 ② 우리 채널 BGM을 마음대로 깔 수 있다.

Meta의 Demucs v4(htdemucs)로 4-stem 분리 후 vocals만 사용.
★ ja_reviewer venv에는 torch가 없다(2.5GB) — **시스템 파이썬에 이미 설치된 demucs를
  외부 프로세스로 호출**한다(윈도우 실측: 시스템 python + CUDA 사용 가능).
  bgm_python 설정으로 다른 인터프리터를 지정할 수 있다.

적용 시점: ② AI 처리가 만든 {code}_final.mp4 (보통 1~3분) — 원본 2시간이 아니라
컷 결과에만 돌리므로 GPU에서 수 초~수십 초면 끝난다.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .common import FFMPEG_TIMEOUT, _finalize, _part_path

DEMUCS_TIMEOUT = 3600


def _pythons(cfg_python=None):
    """demucs가 있을 만한 인터프리터 후보 — 설정 → 시스템 python → 현재 venv."""
    out = []
    if cfg_python:
        out.append(str(cfg_python))
    p = shutil.which("python")
    if p:
        out.append(p)
    out.append(sys.executable)
    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def find_demucs(cfg_python=None):
    """demucs를 실행할 수 있는 파이썬 경로. 없으면 None."""
    for py in _pythons(cfg_python):
        try:
            r = subprocess.run([py, "-c", "import demucs"], capture_output=True, timeout=60)
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return None


def remove_bgm(video, out_video, log=print, python=None, model="htdemucs",
               progress=None):
    """영상의 오디오에서 BGM을 제거(vocals만 남김)하고 같은 영상에 다시 입힌다.
    비디오는 스트림 카피(무손실). 실패 시 예외 → 호출측이 '건너뜀'으로 처리."""
    py = find_demucs(python)
    if not py:
        raise RuntimeError(
            "demucs가 설치된 파이썬을 찾지 못했습니다 — `pip install -U demucs`"
            "(시스템 파이썬에 설치하면 자동으로 찾습니다)")
    # 제자리 교체(in-place)를 허용한다 — ffmpeg는 입력==출력이면 파일을 망가뜨리므로
    # 임시 경로로 쓴 뒤 바꿔치기한다.
    in_place = Path(video).resolve() == Path(out_video).resolve()
    dst = (Path(out_video).with_name(Path(out_video).stem + "_nobgm.mp4")
           if in_place else Path(out_video))

    with tempfile.TemporaryDirectory(prefix="jabgm_") as td:
        td = Path(td)
        wav = td / "in.wav"
        log("BGM 제거 — 오디오 추출...")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video),
                        "-vn", "-ac", "2", "-ar", "44100", str(wav)],
                       check=True, timeout=FFMPEG_TIMEOUT)
        if progress:
            progress(0.15)

        # --two-stems=vocals 면 vocals / no_vocals 두 개만 뽑아 더 빠르다
        log(f"BGM 제거 — demucs({model}) 분리 중 (GPU면 수 초~수십 초)...")
        cmd = [py, "-m", "demucs", "-n", model, "--two-stems", "vocals",
               "-o", str(td), str(wav)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=DEMUCS_TIMEOUT)
        if r.returncode != 0:
            raise RuntimeError(f"demucs 실패: {(r.stderr or '')[-300:]}")
        if progress:
            progress(0.8)

        voc = next(td.glob(f"**/{wav.stem}/vocals.wav"), None)
        if not voc or not voc.is_file():
            raise RuntimeError("demucs 결과(vocals.wav)를 찾지 못했습니다")

        log("BGM 제거 — 목소리만 남긴 오디오로 교체...")
        tmp = _part_path(str(dst))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(voc),
                        "-map", "0:v", "-map", "1:a", "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "192k", "-shortest", tmp],
                       check=True, timeout=FFMPEG_TIMEOUT)
        _finalize(tmp, str(dst))
    if in_place:
        shutil.move(str(dst), str(out_video))
    if progress:
        progress(1.0)
    log(f"✔ BGM 제거 완료 — 목소리만 남음: {out_video}")
    return str(out_video)
