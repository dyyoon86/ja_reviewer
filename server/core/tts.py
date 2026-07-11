#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""⑤ TTS(voicebox REST) — 내레이션 합성, 더킹, 먹싱."""
import json
import subprocess
import urllib.request
from pathlib import Path

from .common import FFPROBE_TIMEOUT, FFMPEG_TIMEOUT, _part_path, _finalize

# ─── ⑤ TTS (voicebox REST) — 한국어 내레이션 음성 ───────────────────────────
# voicebox(jamiepine/voicebox) 로컬 REST API(기본 127.0.0.1:17493)
#   POST /generate {text, profile_id, language}  GET /profiles
# 한국어는 Qwen3-TTS 엔진 + 한국어 보이스 profile 사용.
import base64 as _b64

def tts_profiles(base):
    with urllib.request.urlopen(base.rstrip("/") + "/profiles", timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def tts_generate(base, text, profile_id, language, out_wav, seed=None, log=print):
    """voicebox /generate 호출 → out_wav(WAV) 저장. 응답이 오디오바이트/JSON(path|url|base64) 모두 대응.
    seed 지정 시 재현 가능(같은 seed=같은 음색/억양). voicebox가 seed 필드를 받으면 적용됨."""
    url = base.rstrip("/") + "/generate"
    payload = {"text": text, "profile_id": profile_id, "language": language}
    if seed is not None and str(seed) != "":
        try:
            payload["seed"] = int(seed)
        except Exception:
            pass
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        ctype = (r.headers.get_content_type() or "").lower()
        data = r.read()
    tmp = out_wav + ".src"
    is_temp = True  # tmp이 우리가 만든 임시파일이면 True (voicebox가 준 기존 파일이면 False → 삭제 금지)
    if ctype.startswith("audio/") or ctype == "application/octet-stream":
        Path(tmp).write_bytes(data)
    else:
        j = json.loads(data.decode("utf-8"))
        b64 = j.get("audio_base64") or j.get("audio") or j.get("data")
        path = j.get("path") or j.get("file") or j.get("output")
        url2 = j.get("url")
        if b64:
            Path(tmp).write_bytes(_b64.b64decode(b64))
        elif path and Path(path).is_file():
            tmp = path; is_temp = False
        elif url2:
            with urllib.request.urlopen(url2, timeout=120) as r2:
                Path(tmp).write_bytes(r2.read())
        elif "id" in j and j.get("status") in ("generating", "pending", "queued"):
            # 비동기 API: /history/{id} 폴링 → /audio/{id} 다운로드
            import time as _time
            gen_id = j["id"]
            base_url = url.rstrip("/generate").rstrip("/")
            log(f"  async 생성 중(id={gen_id[:8]}...)...")
            # GPU 느릴 때 한 문장에 수 분 걸릴 수 있음 — 도중 포기하면 voicebox 큐에
            # 좀비 잡이 쌓여 뒤 요청까지 밀리므로 넉넉히(600s) 기다린다.
            # 폴링 자체의 일시 오류(소켓 타임아웃 등)는 실패로 치지 않고 계속 재시도.
            for _ in range(200):
                _time.sleep(3)
                try:
                    with urllib.request.urlopen(f"{base_url}/history/{gen_id}", timeout=10) as rh:
                        hj = json.loads(rh.read())
                    st = hj.get("status", "")
                    if st == "completed":
                        with urllib.request.urlopen(f"{base_url}/audio/{gen_id}", timeout=60) as ra:
                            Path(tmp).write_bytes(ra.read())
                        break
                    elif st in ("failed", "error"):
                        raise RuntimeError(f"voicebox 생성 실패: {hj.get('error')}")
                except RuntimeError:
                    raise
                except Exception:
                    pass    # 폴링 일시 오류 — 다음 폴링에서 재시도
            else:
                raise RuntimeError("voicebox 생성 타임아웃(600s)")
        else:
            raise RuntimeError(f"voicebox 응답에서 오디오를 못 찾음: keys={list(j.keys())}")
    # 표준 WAV(48k stereo)로 정규화
    subprocess.run(["ffmpeg", "-y", "-i", tmp, "-ar", "48000", "-ac", "2", out_wav],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=FFMPEG_TIMEOUT)
    if is_temp:
        try: Path(tmp).unlink()
        except Exception: pass
    return out_wav


def audio_duration(path):
    """오디오 길이(초). 실패 시 0."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)], timeout=FFPROBE_TIMEOUT)
        return float(out.decode().strip())
    except Exception:
        return 0.0


MIN_GAP = 0.12       # 문장 사이 최소 숨돌림(초)
MAX_TEMPO = 1.35     # 이 이상 빠르게 하면 알아듣기 어렵다


def build_narration_wav(clips, out_wav, log=print, video_sec=None,
                        min_gap=MIN_GAP, max_tempo=MAX_TEMPO):
    """clips=[(start_sec, wav_path)] → 단일 내레이션 트랙.

    TTS 실제 발화가 LLM이 잡은 슬롯보다 길면 다음 문장과 겹쳐 두 목소리가 동시에 난다
    (예전 구현은 겹치면 그대로 amix). 여기서는:
      1) 슬롯에 안 들어가면 atempo로 살짝 빠르게(최대 max_tempo) → 영상 싱크 유지
      2) 그래도 넘치면 다음 문장 시작을 뒤로 민다(싱크가 조금 밀리더라도 겹침보다 낫다)
    """
    if not clips:
        raise RuntimeError("내레이션 클립이 없습니다.")
    items = sorted(((float(s), str(p)) for s, p in clips), key=lambda x: x[0])
    durs = [audio_duration(p) for _, p in items]

    placed, cursor = [], 0.0     # placed: (start, path, tempo)
    sped = pushed = 0
    for i, ((st, p), d) in enumerate(zip(items, durs)):
        start = max(st, cursor)
        if start > st + 1e-6:
            pushed += 1
        # 다음 문장 시작(마지막이면 영상 끝)까지가 이 문장의 슬롯
        nxt = items[i + 1][0] if i + 1 < len(items) else (video_sec or (start + d + min_gap))
        slot = max(0.5, nxt - start - min_gap)
        tempo = 1.0
        if d > slot and d > 0:
            tempo = min(max_tempo, d / slot)
            if tempo > 1.001:
                sped += 1
        eff = (d / tempo) if tempo > 0 else d
        placed.append((start, p, tempo))
        cursor = start + eff + min_gap

    inputs, filt = [], []
    for i, (st, p, tempo) in enumerate(placed):
        inputs += ["-i", p]
        ms = int(max(0.0, st) * 1000)
        chain = f"[{i}:a]"
        if tempo > 1.001:
            chain += f"atempo={tempo:.4f},"
        chain += f"adelay={ms}|{ms}[a{i}];"
        filt.append(chain)
    mix = "".join(f"[a{i}]" for i in range(len(placed))) + f"amix=inputs={len(placed)}:normalize=0[a]"
    _tmp = _part_path(out_wav)
    subprocess.run(["ffmpeg", "-y", *inputs, "-filter_complex", "".join(filt) + mix,
                    "-map", "[a]", _tmp], check=True, timeout=FFMPEG_TIMEOUT)
    _finalize(_tmp, out_wav)

    end = cursor - min_gap
    msg = f"내레이션 WAV 합성 완료: {out_wav} ({len(placed)}문장, 끝 {end:.1f}s"
    if video_sec:
        msg += f" / 영상 {video_sec:.1f}s"
    msg += ")"
    log(msg)
    if sped:
        log(f"  · 슬롯이 좁아 {sped}문장을 최대 {max_tempo}배까지 빠르게 조정")
    if pushed:
        log(f"  · {pushed}문장은 앞 문장과 겹쳐 뒤로 밀었습니다(내레이션이 촘촘합니다)")
    if video_sec and end > video_sec + 0.5:
        log(f"  ※ 내레이션이 영상보다 {end - video_sec:.1f}s 깁니다 — "
            f"문장 수를 줄이거나 목표 길이를 늘리세요")

    # 실제로 목소리가 나는 구간 — 원음 더킹을 이 구간에만 정확히 걸기 위해 돌려준다
    spans = []
    for (st, p, tempo), d in zip(placed, durs):
        spans.append((st, st + (d / tempo if tempo > 0 else d)))
    return out_wav, spans


def merge_spans(spans, gap=0.25):
    """가까운 구간은 하나로 합친다 — 더킹이 잘게 오르내리는 것(펌핑) 방지."""
    if not spans:
        return []
    out = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(a, b) for a, b in out]


def _duck_expr(spans, level=0.25, fade=0.18, release=0.40):
    """내레이션 구간에서만 원음을 level(0~1)로 낮추는 volume 표현식.
    구간 앞은 fade초 동안 내려가고, 뒤는 release초 동안 올라온다(딸깍 방지).
    사이드체인 컴프레서와 달리 감쇄량이 신호 세기에 안 흔들리고 정확하다."""
    spans = merge_spans(spans)
    if not spans:
        return None
    ramps = []
    for s, e in spans:
        s0 = max(0.0, s - fade)
        ramps.append(f"min(1,max(0,min((t-{s0:.3f})/{fade:.3f},({e + release:.3f}-t)/{release:.3f})))")
    m = ramps[0]
    for r in ramps[1:]:
        m = f"max({m},{r})"
    return f"1-{1 - level:.3f}*({m})"


ORIG_AUDIO_MODES = {
    "duck": "현장음 살리고 해설 중에만 줄이기 (권장)",
    "keep": "현장음 그대로 + 해설 겹치기",
    "mute": "현장음 끄기 (해설만)",
}


def mux_narration(video, narration_wav, out_video, narration_gain=1.0,
                  orig_gain=None, mode="duck", duck_level=0.3, duck_spans=None,
                  log=print):
    """영상에 내레이션 WAV를 입힌다.

    mode='duck'(기본) : 원음(현장음)을 살리고, 해설이 나오는 동안만 duck_level로 낮춘다.
                        duck_spans(실제 발화 구간)를 주면 그 구간에만 정확히 건다.
                        없으면 사이드체인 컴프레서로 근사한다.
    mode='keep'       : 원음 그대로 + 해설 겹치기.
    mode='mute'       : 원음 음소거 — 해설만.
    orig_gain을 직접 주면 mode보다 우선한다(기존 호출부 호환).
    """
    try:
        duck_level = max(0.0, min(1.0, float(duck_level)))   # ffmpeg 볼륨식에 넣기 전 0~1로
    except (TypeError, ValueError):
        duck_level = 0.3
    if orig_gain is not None:
        fc = (f"[0:a]volume={orig_gain}[oa];[1:a]volume={narration_gain}[na];"
              f"[oa][na]amix=inputs=2:duration=first:normalize=0[a]")
    elif mode == "duck":
        expr = _duck_expr(duck_spans, duck_level) if duck_spans else None
        if expr:
            # 발화 구간을 알고 있으므로 정확한 볼륨 자동화 — 감쇄량이 흔들리지 않는다
            fc = (f"[0:a]volume=volume='{expr}':eval=frame[oa];"
                  f"[1:a]volume={narration_gain}[na];"
                  f"[oa][na]amix=inputs=2:duration=first:normalize=0[a]")
        else:
            # 구간을 모르면 내레이션을 사이드체인으로 써서 눌러준다(근사)
            fc = (f"[1:a]volume={narration_gain},asplit=2[na][sc];"
                  f"[0:a]volume=1.0[oa];"
                  f"[oa][sc]sidechaincompress=threshold=0.02:ratio=12:attack=15:release=350[ducked];"
                  f"[ducked][na]amix=inputs=2:duration=first:normalize=0[a]")
    elif mode == "keep":
        fc = (f"[0:a]volume=1.0[oa];[1:a]volume={narration_gain}[na];"
              f"[oa][na]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        fc = (f"[0:a]volume=0[oa];[1:a]volume={narration_gain}[na];"
              f"[oa][na]amix=inputs=2:duration=first:normalize=0[a]")
    _tmp = _part_path(out_video)
    subprocess.run(["ffmpeg", "-y", "-i", str(video), "-i", str(narration_wav),
                    "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", _tmp],
                   check=True, timeout=FFMPEG_TIMEOUT)
    _finalize(_tmp, out_video)
    extra = ""
    if mode == "duck":
        extra = f" · 해설 중 원음 {int(duck_level * 100)}%"
    log(f"내레이션 입힌 영상({ORIG_AUDIO_MODES.get(mode, mode)}{extra}): {out_video}")
    return out_video


