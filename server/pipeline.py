#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ja_reviewer 파이프라인 — UI 무관 순수 로직 (Tkinter/웹 공용).

기존 ddalddalgi_studio.py 에서 검증된 함수들을 분리:
  transcribe / fetch_meta / call_llm / prompt_auto / prompt_manual /
  keep_from_exclude / cut_video / retime / write_srt / ranges_from_text / video_duration

log 콜백은 진행상황 출력용(기본 print). 서버에선 SSE 큐로 연결.
"""
import re
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path

# 리뷰 길이 선택지 (라벨 → 초). 기본 1분.
TARGETS = {"1분": 60, "2분": 120, "5분": 300, "10분": 600}


def sec2label(sec):
    for k, v in TARGETS.items():
        if v == sec:
            return k
    return "1분"


# ─── 시간 / SRT 유틸 ──────────────────────────────────────────────────────────
def s2srt(x):
    x = max(0.0, x)
    h = int(x // 3600); m = int(x % 3600 // 60); s = int(x % 60); ms = int(round((x - int(x)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def hhmmss(x):
    x = int(max(0, x)); return f"{x//3600:02d}:{x%3600//60:02d}:{x%60:02d}"


def parse_time(s):
    s = str(s).strip()
    if not s:
        return None
    if ":" in s:
        parts = [float(p) for p in s.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return float(s)


def ranges_from_text(text):
    """'12:30-18:00, 45:00-52:00' → [(750,1080),(2700,3120)]"""
    out = []
    for chunk in re.split(r"[,\n]", text or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.split(r"[-~]", chunk)
        if len(m) != 2:
            continue
        a, b = parse_time(m[0]), parse_time(m[1])
        if a is not None and b is not None and b > a:
            out.append((a, b))
    return sorted(out)


def _wrap_chunks(text, maxlen=24):
    """텍스트를 maxlen 이하 덩어리로 (단어/공백 기준 그리디, 긴 단어는 강제 분할)."""
    text = " ".join(str(text).split())
    if len(text) <= maxlen:
        return [text] if text else [""]
    norm = []
    for w in text.split(" "):
        while len(w) > maxlen:
            norm.append(w[:maxlen]); w = w[maxlen:]
        if w:
            norm.append(w)
    chunks, cur = [], ""
    for w in norm:
        cand = w if not cur else cur + " " + w
        if len(cand) <= maxlen:
            cur = cand
        else:
            if cur:
                chunks.append(cur)
            cur = w
    if cur:
        chunks.append(cur)
    return chunks or [text[:maxlen]]


def split_entries(entries, maxlen=24):
    """긴 자막을 maxlen 이하 여러 항목으로 분할 — 시간은 글자수 비례로 배분(싱크 유지)."""
    out = []
    for a, b, t in entries:
        chunks = _wrap_chunks(t, maxlen)
        if len(chunks) <= 1:
            out.append((a, b, chunks[0] if chunks else "")); continue
        total = sum(len(c) for c in chunks) or 1
        span = max(0.0, b - a); cur = a
        for k, c in enumerate(chunks):
            e = b if k == len(chunks) - 1 else cur + span * (len(c) / total)
            if e <= cur:
                e = cur + 0.3
            out.append((cur, e, c)); cur = e
    return out


def write_srt(entries, path, maxlen=24):
    """SRT 출력. maxlen 글자 이하로 자동 분할(시간 비례 배분). maxlen=0이면 분할 안 함."""
    if maxlen:
        entries = split_entries(entries, maxlen)
    out = [f"{i}\n{s2srt(a)} --> {s2srt(b)}\n{t}" for i, (a, b, t) in enumerate(entries, 1)]
    Path(path).write_text("\n\n".join(out) + "\n", encoding="utf-8")


def video_duration(path):
    try:
        out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                       "format=duration", "-of", "csv=p=0", str(path)])
        return float(out.decode().strip())
    except Exception:
        return 0.0


def keep_from_exclude(total, excludes, min_gap=0.3):
    """제외 구간 → 남길(keep) 구간(여집합)."""
    ex = sorted(excludes); keep = []; cur = 0.0
    for a, b in ex:
        a = max(0.0, a); b = min(total, b)
        if a - cur > min_gap:
            keep.append((cur, a))
        cur = max(cur, b)
    if total - cur > min_gap:
        keep.append((cur, total))
    return keep


# ─── ① Whisper ───────────────────────────────────────────────────────────────
def transcribe(video, model_name="large-v3", log=print):
    log(f"Whisper 전사 (모델 {model_name})...")
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="auto", compute_type="auto")
    segs, info = model.transcribe(str(video), language="ja", vad_filter=True)
    out = []
    for s in segs:
        t = (s.text or "").strip()
        if t:
            out.append((float(s.start), float(s.end), t))
        if out and len(out) % 50 == 0:
            log(f"   …{len(out)}")
    log(f"전사 완료: {len(out)} 세그먼트")
    return out


# ─── ② 메타 ──────────────────────────────────────────────────────────────────
def fetch_meta(api, code, log=print):
    url = f"{api.rstrip('/')}/work/{urllib.request.quote(code)}"
    log(f"메타 조회: {url}")
    with urllib.request.urlopen(url, timeout=15) as r:
        m = json.loads(r.read().decode("utf-8"))
    if m.get("error"):
        raise RuntimeError(m["error"])
    log(f"메타 OK: {m.get('actress')} / {m.get('label')} / {m.get('meas')}")
    return m


# ─── ③ LLM ───────────────────────────────────────────────────────────────────
def call_llm(prompt, which="claude", log=print):
    log(f"LLM({which}) 호출...")
    if which == "codex":
        with tempfile.TemporaryDirectory() as td:
            outf = Path(td) / "o.json"
            subprocess.run(["codex", "exec", "--ephemeral", "--skip-git-repo-check", "-o", str(outf), prompt],
                           timeout=600, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            txt = outf.read_text(encoding="utf-8") if outf.exists() else ""
    else:
        p = subprocess.run(["claude", "-p", prompt], timeout=600, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
        txt = p.stdout or ""
    s = txt.strip(); i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise RuntimeError("LLM JSON 응답 없음 (CLI 로그인 확인)")
    return json.loads(s[i:j + 1])


def _meta_block(meta):
    g = ", ".join(meta.get("genres") or []) or (meta.get("genre") or "")
    return (f"품번:{meta.get('code')} 배우:{meta.get('actress')}({meta.get('actress_ja')}) "
            f"신체:{meta.get('meas')} 레이블:{meta.get('label')} 메이커:{meta.get('maker')} "
            f"감독:{meta.get('director')} 시리즈:{meta.get('series_ja') or '-'} 장르:{g} "
            f"발매:{(meta.get('release_date') or '')[:10]} 런타임:{meta.get('runtime_mins')}분 "
            f"인기:조회{meta.get('views')}/좋아요{meta.get('likes')}/싫어요{meta.get('dislikes')}\n"
            f"일본원제:{meta.get('title_ja')}\n한국어시놉시스:{meta.get('description')}")


# 번역 규칙 — jav-subtitle-translate 스킬(AVDBS Eddy_Wind / 사용자 노션 원안)에서 추출.
# 자동 파이프라인(claude/codex CLI)은 스킬 파일을 못 읽으므로 핵심을 여기 직접 박는다.
def _translate():
    return ("[대사 번역 규칙 — 19금 현지화 전문가] "
            "P1 정확성: 의미·감정·어조·의도 100% 재현(오역/왜곡/누락 금지). "
            "P2 자연스러움: 번역투 전면 제거, 유창한 현대 한국어 구어체(일본식 한국어 금지). "
            "P3 현지화: 줄거리·장면·인물 맥락 종합해 가장 한국적인 어조/어휘로. "
            "P4 뉘앙스: 성적 긴장·심리·비언어 함의를 한국어 표현력으로. "
            "[19금] 저속함 지양·세련된 성적 담론. 강도(흥분/쾌감/고통)는 어미·어휘로 정밀 제어. "
            "신음·짧은탄성은 음차(아앙/앗) 금지 → 맥락 감정표현 '(쾌감 섞인 신음)'·'(가쁜 숨)' 또는 자연스러운 감탄사. "
            "단어는 직역 말고 상황 정서로 의역(예: 気持ちいい→가, 간다!/미칠 것 같아, 余裕→더 할 수 있어/더 원해, "
            "イク→갈게/간다, 締め→숨 막히는 쾌감). 같은 단어도 장면 맥락 따라 달라짐.")


def _style():
    return ("[톤] 3분휴지 스타일 — 정중체(~습니다)+솔직 호불호+마니아 은어(미드/포텐/피지컬/육덕/하메리/1인칭/펠라/시추에이션)"
            "+레이블 맥락. [내레이션 구성] 인트로→상황설명→평가→총평, 섹스 스킵 구간은 브릿지('이후 호텔로…'). "
            "평가/감상은 그럴듯하게 창작하되 메타·시놉과 모순 금지. [대사] 자연스러운 한국어 구어체(번역투 금지), 신음류 제외/(신음). "
            "[자막 길이] 대사·내레이션의 각 항목 텍스트는 25자 이내로, 길면 의미 단위(절·구)로 끊어 여러 항목으로 나눠라.\n"
            + _translate())


def prompt_auto(meta, segs, target_sec=60):
    body = "\n".join(f"{k}\t{a:.2f}\t{b:.2f}\t{t}" for k, (a, b, t) in enumerate(segs, 1))
    return (f"너는 딸딸기튜브 AV 해설영상 작가다. 아래 작품의 일본어 자막을 보고 '스토리 핵심만' 골라 "
            f"**약 {target_sec}초 내외 하이라이트 영상**으로 압축하고, 한글 대사자막과 해설 내레이션을 만든다.\n"
            f"[메타]\n{_meta_block(meta)}\n[일본어자막] 번호\\t시작초\\t끝초\\t대사\n{body}\n{_style()}\n"
            f"[규칙] (1)신음·짧은탄성·반복감탄·비스토리 섹스대사·무음/잡담·중복은 버린다. "
            f"(2)스토리(설정·관계·전환·갈등·결말)를 드러내는 핵심 구간만 keep으로 골라 **합쳐서 {target_sec}초 ±20% 목표**. "
            f"(3)도입~결말 흐름이 보이게 고루 분포. 시간은 원본 영상 기준 초.\n"
            f"[출력 JSON만] {{\"summary\":\"3~5줄\",\"stars\":1~5,\"keep\":[[시작,끝],...],"
            f"\"dialogue\":[{{\"start\":초,\"end\":초,\"ko\":\"\"}}],\"narration\":[{{\"start\":초,\"end\":초,\"text\":\"\"}}]}}")


def prompt_manual(meta, segs, target_sec=60):
    body = "\n".join(f"{k}\t{a:.2f}\t{b:.2f}\t{t}" for k, (a, b, t) in enumerate(segs, 1))
    return (f"너는 딸딸기튜브 AV 해설영상 작가다. 아래는 '정사장면을 이미 제거한' 영상의 일본어 자막이다. "
            f"여기서 **스토리 핵심만 골라 약 {target_sec}초 내외로 압축**하고, 한글 대사자막과 해설 내레이션을 만든다.\n"
            f"[메타]\n{_meta_block(meta)}\n[일본어자막] 번호\\t시작초\\t끝초\\t대사\n{body}\n{_style()}\n"
            f"[규칙] (1)무음·잡담·반복·의미없는 짧은 라인은 버린다. "
            f"(2)스토리(설정·관계·전환·갈등·결말)를 드러내는 핵심 구간만 keep으로 골라 **합쳐서 {target_sec}초 ±20% 목표**. "
            f"(3)정사 선별은 하지 말 것(이미 제거됨). 시간은 이 자막 기준 초.\n"
            f"[출력 JSON만] {{\"summary\":\"3~5줄\",\"stars\":1~5,\"keep\":[[시작,끝],...],"
            f"\"dialogue\":[{{\"start\":초,\"end\":초,\"ko\":\"\"}}],\"narration\":[{{\"start\":초,\"end\":초,\"text\":\"\"}}]}}")


# ─── ④ 컷 / 재타이밍 ─────────────────────────────────────────────────────────
def retime(entries, keep, snap=False, default_dur=4.0):
    """원본 시간 entries[(s,e,text)] → 컷(keep 이어붙임) 새 타임라인.
    keep 밖: snap=False(대사) 버림 / snap=True(내레이션) 컷 경계로 당김."""
    keep = sorted(keep); offs, acc = [], 0.0
    for a, b in keep:
        offs.append(acc); acc += (b - a)
    total = acc; out = []
    for s, e, t in entries:
        placed = False
        for (a, b), off in zip(keep, offs):
            if s >= a - 0.05 and s < b + 0.05:
                ns = off + max(0.0, s - a); ne = off + min(b - a, e - a)
                if ne <= ns:
                    ne = ns + 0.5
                out.append((ns, ne, t)); placed = True; break
        if placed or not snap:
            continue
        if s < keep[0][0]:
            ns = 0.0
        else:
            ns = total
            for (a, b), off in zip(keep, offs):
                if s < a:
                    ns = off; break
        ne = min(total, ns + (e - s if e > s else default_dur))
        if ne <= ns:
            ne = min(total, ns + default_dur)
        out.append((ns, ne, t))
    out.sort(key=lambda x: x[0]); return out


def cut_video(video, keep, out_path, log=print):
    keep = sorted(keep)
    log(f"ffmpeg 컷: {len(keep)}구간 이어붙이기 (재인코딩)...")
    filt = []
    for i, (a, b) in enumerate(keep):
        filt.append(f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[v{i}];")
        filt.append(f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{i}];")
    n = len(keep)
    fc = "".join(filt) + "".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
    subprocess.run(["ffmpeg", "-y", "-i", str(video), "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", str(out_path)], check=True)
    log(f"컷 완료: {out_path}")


# ─── ⑤ TTS (voicebox REST) — 한국어 내레이션 음성 ───────────────────────────
# voicebox(jamiepine/voicebox) 로컬 REST API(기본 127.0.0.1:17493)
#   POST /generate {text, profile_id, language}  GET /profiles
# 한국어는 Qwen3-TTS 엔진 + 한국어 보이스 profile 사용.
import base64 as _b64

def srt_parse(path):
    """SRT → [(start_sec, end_sec, text)]"""
    out = []
    blocks = re.split(r"\n\s*\n", Path(path).read_text(encoding="utf-8").strip())
    for b in blocks:
        lines = [x for x in b.splitlines() if x.strip()]
        if len(lines) < 2:
            continue
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        a, _, c = lines[ti].partition("-->")

        def _s(t):
            t = t.strip().replace(",", ".")
            h, m, s = t.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
        text = " ".join(lines[ti + 1:]).strip()
        if text:
            out.append((_s(a), _s(c), text))
    return out


def tts_profiles(base):
    with urllib.request.urlopen(base.rstrip("/") + "/profiles", timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def tts_generate(base, text, profile_id, language, out_wav, log=print):
    """voicebox /generate 호출 → out_wav(WAV) 저장. 응답이 오디오바이트/JSON(path|url|base64) 모두 대응."""
    url = base.rstrip("/") + "/generate"
    body = json.dumps({"text": text, "profile_id": profile_id, "language": language}).encode("utf-8")
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
        else:
            raise RuntimeError(f"voicebox 응답에서 오디오를 못 찾음: keys={list(j.keys())}")
    # 표준 WAV(48k stereo)로 정규화
    subprocess.run(["ffmpeg", "-y", "-i", tmp, "-ar", "48000", "-ac", "2", out_wav],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if is_temp:
        try: Path(tmp).unlink()
        except Exception: pass
    return out_wav


def build_narration_wav(clips, out_wav, log=print):
    """clips=[(start_sec, wav_path)] → 각 클립을 시작시간에 배치(A안: 자연길이, 겹치면 믹스)한 단일 WAV."""
    if not clips:
        raise RuntimeError("내레이션 클립이 없습니다.")
    inputs, filt = [], []
    for i, (st, p) in enumerate(clips):
        inputs += ["-i", str(p)]
        ms = int(max(0.0, st) * 1000)
        filt.append(f"[{i}:a]adelay={ms}|{ms}[a{i}];")
    mix = "".join(f"[a{i}]" for i in range(len(clips))) + f"amix=inputs={len(clips)}:normalize=0[a]"
    subprocess.run(["ffmpeg", "-y", *inputs, "-filter_complex", "".join(filt) + mix,
                    "-map", "[a]", str(out_wav)], check=True)
    log(f"내레이션 WAV 합성 완료: {out_wav}")
    return out_wav


def mux_narration(video, narration_wav, out_video, narration_gain=1.0, orig_gain=0.0, log=print):
    """영상에 내레이션 WAV를 입힌다. orig_gain=0 이면 원음 음소거(내레이션만)."""
    fc = (f"[0:a]volume={orig_gain}[oa];[1:a]volume={narration_gain}[na];"
          f"[oa][na]amix=inputs=2:duration=first:normalize=0[a]")
    subprocess.run(["ffmpeg", "-y", "-i", str(video), "-i", str(narration_wav),
                    "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", str(out_video)], check=True)
    log(f"내레이션 입힌 영상: {out_video}")
    return out_video
