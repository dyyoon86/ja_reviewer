# -*- coding: utf-8 -*-
r"""TTS 직전 점검 — 대사·내레이션 자막이 final 영상과 실제로 맞는지 확인한다.

왜 TTS **앞**인가: TTS 는 내레이션 SRT 를 그대로 읽어 음성을 만들고, 그 다음 번인이
자막을 굽는다. 자막이 어긋난 채 여기를 지나가면 음성과 하드섭까지 전부 다시 만들어야
한다. 되돌리는 비용이 가장 싼 지점이 여기다.

★좌표계 주의 — 반드시 **SRT** 를 본다.
  `{code}_내레이션.json` / `{code}_정밀전사.json` 은 **클린본(trim)** 좌표이고
  SRT 만 **최종 영상** 좌표다. JSON 으로 영상 길이와 비교하면 멀쩡한 편도 전부
  "영상 초과"로 뜬다(2026-09-07 오검출 11편의 원인).

두 겹으로 본다.
  ① 구조(빠름)   — SRT 자체의 역순·겹침·길이0·영상길이 초과, 내레이션 밀림/공백/도입
  ② 오디오(정확) — final 영상을 다시 전사해 "소리 나는데 자막 없는 구간"과
                    "소리 없는데 자막 있는 줄"을 센다. ②가 2026-09-08 에
                    MIKR-118·SNOS-372·MIDA-798 의 자막 누락(21~31%)을 잡아냈다.
                    구조 점검(①)은 그 세 편을 전부 '정상'으로 통과시켰다.
"""
import json
import subprocess
from pathlib import Path

from .common import srt_parse, video_duration

CPS = 7.5                # voicebox 실측 발화속도(자/초) — 슬롯이 이보다 짧으면 뒤로 밀린다
MISS_PCT = 20.0          # 무자막 발화가 전체 발화 시간의 이 %를 넘으면 누락으로 본다
GHOST_MAX = 2            # 소리 없는 자막 줄 허용치
PUSH_MAX = 3.0           # 누적 TTS 밀림 허용(초)
GAP_MAX = 25.0           # 내레이션 최대 공백(초)
INTRO_MAX = 8.0          # 첫 내레이션이 이보다 늦고 영상의 15%를 넘으면 도입 지연


def _rows(p):
    return [] if not Path(p).is_file() else [(a, b, t) for a, b, t in srt_parse(p)]


def _structure(rs, vs, who):
    """SRT 내부 정합 — 역순 / 길이0 / 겹침 / 영상 길이 초과."""
    bad = []
    for i, (a, b, _t) in enumerate(rs):
        if b <= a:
            bad.append(f"{who} {i+1}번 길이0")
        if a < -0.01:
            bad.append(f"{who} {i+1}번 음수시각")
        if i and a < rs[i - 1][0] - 0.01:
            bad.append(f"{who} {i+1}번 역순")
        if i and a < rs[i - 1][1] - 0.05:
            bad.append(f"{who} {i+1}번 겹침")
    if rs and rs[-1][1] - vs > 0.5:
        bad.append(f"{who} 영상초과 {rs[-1][1] - vs:.1f}s")
    return bad[:4]


def _hear(video, model="small", log=print):
    """final 영상을 다시 전사해 '실제로 소리 나는 구간'만 (시작, 끝) 으로 돌려준다."""
    from .transcribe import _ensure_cuda_dll_path
    _ensure_cuda_dll_path(lambda *a: None)
    from faster_whisper import WhisperModel
    try:
        m = WhisperModel(model, device="cuda", compute_type="float16")
    except Exception as e:
        log(f"   (cuda 사용 불가 {type(e).__name__} — cpu 로 점검)")
        m = WhisperModel(model, device="cpu", compute_type="int8")
    segs, _ = m.transcribe(str(video), language="ja", vad_filter=True,
                           condition_on_previous_text=False)
    return [(s.start, s.end) for s in segs]


def check_edition(outdir, code, audio=True, model="small", log=print):
    """한 편을 점검해 문제 문자열 목록을 돌려준다(빈 목록 = 이상 없음).

    outdir: 그 품번의 작업 폴더(…/{code}). audio=False 면 구조 점검만(전사 안 함).
    """
    d = Path(outdir)
    fin = d / f"{code}_final.mp4"
    ds = d / f"{code}_대사.srt"
    ns = d / f"{code}_내레이션.srt"
    bad = []
    if not fin.is_file():
        return [f"final 영상 없음: {fin.name}"]
    vs = video_duration(str(fin))
    dl, nr = _rows(ds), _rows(ns)
    if not dl:
        bad.append("대사 SRT 없음/빈파일")
    if not nr:
        bad.append("내레이션 SRT 없음/빈파일")
    bad += _structure(dl, vs, "대사")
    bad += _structure(nr, vs, "나레")

    if nr:
        push = sum(max(0.0, len(t) / CPS - (b - a)) for a, b, t in nr)
        gaps = [nr[i + 1][0] - nr[i][1] for i in range(len(nr) - 1)]
        if push > PUSH_MAX:
            bad.append(f"TTS밀림 {push:.0f}s (슬롯보다 문장이 길어 뒤 문장을 밀어냄)")
        if gaps and max(gaps) > GAP_MAX:
            bad.append(f"나레공백 {max(gaps):.0f}s")
        if nr[0][0] > INTRO_MAX and nr[0][0] > vs * 0.15:
            bad.append(f"도입늦음 {nr[0][0]:.0f}s")

    if audio and dl:
        heard = _hear(fin, model, log)
        if heard:
            tot = sum(b - a for a, b in heard) or 1.0
            miss = sum(b - a for a, b in heard
                       if not any(min(y, b) - max(x, a) > 0 for x, y, _ in dl))
            ghost = [1 for a, b, _t in dl
                     if not any(min(y, b) - max(x, a) > 0 for x, y in heard)]
            pct = miss / tot * 100
            if pct > MISS_PCT:
                bad.append(f"자막누락 {pct:.0f}% ({miss:.0f}s 는 소리가 나는데 자막이 없음)")
            if len(ghost) > GHOST_MAX:
                bad.append(f"유령자막 {len(ghost)}줄 (소리 없는 자리에 자막)")
    return bad


def guard(outdir, code, log=print, audio=True, model="small", mode="block"):
    """TTS 앞에서 부르는 게이트. mode='block' 이면 문제가 있을 때 RuntimeError.

    mode: 'block'(기본) | 'warn'(로그만) | 'off'(점검 안 함)
    """
    if mode == "off":
        return []
    log("TTS 전 자막 점검 — 대사·내레이션이 final 영상과 맞는지 확인합니다")
    bad = check_edition(outdir, code, audio=audio, model=model, log=log)
    if not bad:
        log("   ✔ 이상 없음")
        return []
    for b in bad:
        log(f"   ⚠ {b}")
    if mode != "block":
        return bad
    raise RuntimeError(
        f"'{code}' 자막 점검에서 문제가 나와 TTS 를 멈췄습니다:\n"
        + "\n".join(f"  · {b}" for b in bad)
        + "\n\n  고치는 법\n"
        f"   · 자막누락 →  python tools\\fix_dialogue.py --out <out_dir> --code {code}\n"
        f"   · 내레이션 문제(밀림/공백/도입) →  섹션3 ①(내레이션 재생성) 다시 실행,\n"
        f"     한 줄만 고칠 거면  python tools\\fix_narration.py --out <out_dir> --code {code}\n"
        "   · 점검을 건너뛰려면 config 에 \"tts_precheck\": \"warn\" (또는 \"off\")")
