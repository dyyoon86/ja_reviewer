#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""자막 효과음(SFX) — 강조·정보 자막이 뜨는 순간에 소리를 얹는다.

왜 — '강조'/'정보' 자막이 색만 바뀔 뿐 아무 연출이 없어 재미가 없다는 피드백
(2026-07-13). 예능 자막의 임팩트는 **등장 애니메이션 + 효과음**이 반이다.

에셋 없이 ffmpeg로 합성한다(외부 파일 의존 0):
  impact : 낮은 '쿵' (60Hz 사인 감쇠 + 노이즈 어택) — 강조 자막이 박힐 때
  blip   : 짧은 '띡' (1.2kHz 짧은 톤) — 정보 자막이 떨어질 때
  whoosh : 스치는 바람 (밴드패스 노이즈 스윕) — 전환용(예비)
사용자가 {out_dir}/_sfx/impact.wav 등을 직접 넣으면 그 파일을 우선 쓴다.
"""
import subprocess
from pathlib import Path

from .common import FFMPEG_TIMEOUT

# name -> (ffmpeg lavfi 필터, 길이초)
_SYNTH = {
    # 60Hz 사인 + 짧은 노이즈 어택을 섞고 지수 감쇠 → 묵직한 '쿵'
    "impact": ("sine=frequency=62:duration=0.45,"
               "afade=t=out:st=0.05:d=0.40:curve=exp,volume=1.6", 0.45),
    # 1.2kHz 짧은 톤 → 가벼운 '띡'
    "blip": ("sine=frequency=1180:duration=0.12,"
             "afade=t=out:st=0.02:d=0.10:curve=exp,volume=0.5", 0.12),
    # 밴드패스 노이즈 → 'ㅅ쉬익'
    "whoosh": ("anoisesrc=d=0.35:c=pink:a=0.6,"
               "bandpass=f=1400:width_type=o:w=2,"
               "afade=t=in:st=0:d=0.12,afade=t=out:st=0.15:d=0.20,volume=0.7", 0.35),
}


def sfx_path(out_dir, name, log=print):
    """효과음 파일 경로. 사용자가 넣어둔 게 있으면 그걸, 없으면 합성해 캐시한다."""
    d = Path(out_dir) / "_sfx"
    d.mkdir(parents=True, exist_ok=True)
    for ext in (".wav", ".mp3", ".m4a"):
        p = d / f"{name}{ext}"
        if p.is_file():
            return str(p)
    if name not in _SYNTH:
        return None
    flt, _dur = _SYNTH[name]
    out = d / f"{name}.wav"
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", flt,
                        "-ar", "48000", "-ac", "2", str(out)],
                       check=True, timeout=FFMPEG_TIMEOUT)
        log(f"효과음 생성: {out.name} (직접 만든 소리를 쓰려면 {d}에 같은 이름으로 넣으세요)")
        return str(out)
    except Exception as e:
        log(f"※ 효과음 합성 실패({name}): {e}")
        return None


def mix_events(video, events, out_path, out_dir, log=print, gain=0.9):
    """events=[(time_sec, sfx_name)] 시각에 효과음을 얹은 영상을 만든다.
    비디오는 스트림 카피(무손실·빠름), 오디오만 재인코딩. 이벤트가 없으면 False."""
    events = [(float(t), n) for t, n in (events or []) if n]
    if not events:
        return False
    srcs, names = [], []
    for t, n in events:
        p = sfx_path(out_dir, n, log)
        if p:
            srcs.append((t, p))
            names.append(n)
    if not srcs:
        return False

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video)]
    for _t, p in srcs:
        cmd += ["-i", p]
    # 각 효과음을 해당 시각으로 지연시킨 뒤 원본 오디오와 합친다(원본은 그대로 유지)
    parts, labels = [], ["[0:a]"]
    for i, (t, _p) in enumerate(srcs, start=1):
        ms = max(0, int(t * 1000))
        parts.append(f"[{i}:a]adelay={ms}|{ms},volume={gain}[s{i}]")
        labels.append(f"[s{i}]")
    fc = ";".join(parts) + ";" + "".join(labels) + \
         f"amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0[aout]"
    cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out_path)]
    try:
        subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.CalledProcessError as e:
        log(f"※ 효과음 믹싱 실패({e}) — 효과음 없이 진행")
        return False
    from collections import Counter
    cnt = Counter(names)
    log(f"효과음 {len(srcs)}개 삽입: " + ", ".join(f"{k}×{v}" for k, v in cnt.items()))
    return True
