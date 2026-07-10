#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ja_reviewer 파이프라인 — UI 무관 순수 로직 (Tkinter/웹 공용).

기존 ddalddalgi_studio.py 에서 검증된 함수들을 분리:
  transcribe / fetch_meta / call_llm / prompt_auto / prompt_manual /
  keep_from_exclude / cut_video / retime / write_srt / ranges_from_text / video_duration

log 콜백은 진행상황 출력용(기본 print). 서버에선 SSE 큐로 연결.
"""
import os
import re
import json
import shutil
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
    # 전체 ms로 환산 후 분해 → 반올림이 1000ms로 넘쳐 ',1000'이 되는 버그 방지
    total = int(round(max(0.0, x) * 1000))
    h = total // 3600000; total %= 3600000
    m = total // 60000; total %= 60000
    s = total // 1000; ms = total % 1000
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


# 끊기 좋은 한국어 어말(절/구/문장 경계) — 이 패턴으로 끝나는 단어 '뒤'에서 우선 분할
_BREAK_SUFFIX = (
    # 연결어미(절 경계)
    "는데", "은데", "지만", "다만", "으며", "면서", "으면", "니까", "으니까",
    "거나", "든지", "어서", "아서", "여서", "해서", "고서", "다가", "도록", "려고", "으려고",
    "면", "고", "며", "서", "자", "듯",
    # 종결어미(문장 경계)
    "습니다", "입니다", "니다", "어요", "에요", "예요", "아요", "이죠", "죠", "요",
    "다", "까", "네", "군", "라", "지", "랍니다", "거든요",
    # 조사(구 경계)
    "에서", "에게", "한테", "으로", "라고", "라는", "처럼", "만큼", "까지", "부터", "보다",
    "은", "는", "이", "가", "을", "를", "에", "의", "와", "과", "로", "도", "만",
)
_BREAK_PUNCT = set("，,。.!?…)]」』”’~·:;")


def _good_break(word):
    """이 단어 뒤에서 끊으면 문법적으로 자연스러운가."""
    if not word:
        return False
    if word[-1] in _BREAK_PUNCT:
        return True
    return any(word.endswith(s) for s in _BREAK_SUFFIX)


def _wrap_chunks(text, maxlen=25):
    """텍스트를 maxlen 이하로 — 절/구 경계(연결어미·조사·문장부호)에서 우선 끊는다."""
    text = " ".join(str(text).split())
    if len(text) <= maxlen:
        return [text] if text else [""]
    words = []
    for w in text.split(" "):
        while len(w) > maxlen:
            words.append(w[:maxlen]); w = w[maxlen:]
        if w:
            words.append(w)
    chunks = []
    i, n = 0, len(words)
    while i < n:
        cur = ""; j = i; lastgood = -1; lastgood_len = 0
        while j < n:
            cand = words[j] if not cur else cur + " " + words[j]
            if len(cand) > maxlen:
                break
            cur = cand
            if _good_break(words[j]):
                lastgood = j; lastgood_len = len(cur)
            j += 1
        if j >= n:                         # 마지막 청크
            chunks.append(cur); break
        # 절/구 경계가 있고 너무 짧지 않으면 거기서, 아니면 들어간 데까지
        end = lastgood if (lastgood >= i and lastgood_len >= maxlen * 0.5) else j - 1
        chunks.append(" ".join(words[i:end + 1]))
        i = end + 1
    return chunks or [text[:maxlen]]


def split_entries(entries, maxlen=25):
    """긴 자막을 maxlen 이하 여러 항목으로 분할 — 시간은 글자수 비례로 배분(싱크 유지)."""
    out = []
    for a, b, t, *extra in entries:
        chunks = _wrap_chunks(t, maxlen)
        if len(chunks) <= 1:
            out.append((a, b, chunks[0] if chunks else "", *extra)); continue
        total = sum(len(c) for c in chunks) or 1
        span = max(0.0, b - a); cur = a
        for k, c in enumerate(chunks):
            e = b if k == len(chunks) - 1 else cur + span * (len(c) / total)
            if e <= cur:
                e = cur + 0.3
            out.append((cur, e, c, *extra)); cur = e
    return out


def sanitize_segments(entries, min_dur=0.2):
    """SRT 타임스탬프 정상화: 역전(start>end) 교정 + 시간순 정렬 + 겹침 제거.
    Whisper가 가끔 뱉는 뒤집힌/겹친 타임코드를 SRT로 내보내기 전에 무조건 통과시킨다."""
    rows = []
    for e in entries:
        a, b, *rest = e
        a = max(0.0, float(a)); b = float(b)
        if b < a:                       # start>end → 교환(역전 교정)
            a, b = b, a
        if b - a < min_dur:             # 0길이/과소 → 최소 길이
            b = a + min_dur
        rows.append([a, b, *rest])
    rows.sort(key=lambda x: (x[0], x[1]))
    for i in range(len(rows) - 1):      # 앞 세그 end가 다음 start를 넘으면 클램프(겹침 제거)
        if rows[i][1] > rows[i + 1][0]:
            rows[i][1] = max(rows[i][0] + min_dur * 0.5, rows[i + 1][0])
    return [tuple(r) for r in rows]


def clamp_durations(entries, sec_per_char=0.4, base=1.2, hard_cap=7.0, min_dur=0.8):
    """자막이 글자 수에 비해 과하게 오래 떠 있는 것 방지 — end를 '읽을 만한 최대 시간'으로 컷.
    Whisper가 신음/무음/음악 구간을 채우려 세그먼트 end를 길게 늘리는 문제를 잡는다.
    (대사는 없는데 자막이 계속 유지되는 현상 → 이 클램프로 제거)
    허용시간 = base + sec_per_char*글자수 (상한 hard_cap). 텍스트가 없으면 원본 유지."""
    out = []
    for e in entries:
        a, b, *rest = e
        a = float(a); b = float(b)
        txt = (rest[0] if rest else "") or ""
        n = len(txt.strip())
        if n <= 0:
            out.append((a, b, *rest)); continue
        allow = min(hard_cap, base + sec_per_char * n)
        allow = max(allow, min_dur)
        nb = min(b, a + allow)
        if nb <= a:
            nb = a + min_dur
        out.append((a, nb, *rest))
    return out


def write_srt(entries, path, maxlen=25):
    """SRT 출력. maxlen 글자 이하로 자동 분할(시간 비례 배분). maxlen=0이면 분할 안 함.
    출력 직전 항상 sanitize_segments 로 타임스탬프 역전/겹침을 정상화한다."""
    entries = sanitize_segments(entries)
    if maxlen:
        entries = split_entries(entries, maxlen)
    entries = sanitize_segments(entries)   # 분할 후에도 한 번 더 보장
    out = [f"{i}\n{s2srt(a)} --> {s2srt(b)}\n{t}" for i, (a, b, t) in enumerate(entries, 1)]
    Path(path).write_text("\n\n".join(out) + "\n", encoding="utf-8")


def video_duration(path):
    try:
        out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                       "format=duration", "-of", "csv=p=0", str(path)])
        return float(out.decode().strip())
    except Exception:
        return 0.0


def parse_keep(raw, total=None):
    """LLM/사용자 keep 파싱 — [a,b] 또는 [a,b,'라벨'](3개+) 모두 허용. 앞 2개만 사용.
    total(영상 길이)을 주면 [0, total]로 클램프해 LLM이 범위를 넘겨도 안전하게 만든다.
    NaN/inf는 유한값이 아니므로 버린다."""
    import math
    out = []
    for item in (raw or []):
        try:
            if isinstance(item, dict):
                a = float(item.get("start")); b = float(item.get("end"))
            else:
                a = float(item[0]); b = float(item[1])
            if not (math.isfinite(a) and math.isfinite(b)):
                continue
            if total and total > 0:                 # 영상 밖으로 나간 구간 잘라내기
                a = max(0.0, min(a, total)); b = max(0.0, min(b, total))
            if b > a:
                out.append((a, b))
        except Exception:
            continue
    return out


def clamp_stars(v, lo=0, hi=5):
    """별점을 0~5 정수로. None·NaN·문자열·범위밖 모두 안전 처리('★'*stars 크래시 방지)."""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return 0
    return max(lo, min(hi, n))


def parse_lines(raw, text_keys, extra=None, log=None):
    """LLM의 dialogue/narration 배열을 방어적으로 파싱.
    항목 하나가 키 누락/NaN/타입오류여도 그 항목만 건너뛰고 나머지는 살린다
    (기존엔 float(d['start']) 하드접근이라 한 항목만 깨져도 단계 전체가 크래시).
    text_keys: 텍스트를 담은 키 후보(먼저 맞는 것 사용). extra: (키, 기본값) 추가 필드.
    반환: [(start, end, text, *extra_values)]  — 시간순 정렬."""
    import math
    out, dropped = [], 0
    for d in (raw or []):
        try:
            if not isinstance(d, dict):
                dropped += 1; continue
            s = float(d.get("start")); e = float(d.get("end"))
            if not (math.isfinite(s) and math.isfinite(e)):
                dropped += 1; continue
            if e <= s:                       # 역전/0길이 → 최소 길이 부여
                e = s + 0.5
            txt = ""
            for k in text_keys:
                v = d.get(k)
                if v not in (None, ""):
                    txt = str(v); break
            if not txt.strip():
                dropped += 1; continue
            vals = [d.get(k, dv) for k, dv in (extra or [])]
            out.append((s, e, txt, *vals))
        except (TypeError, ValueError):
            dropped += 1; continue
    out.sort(key=lambda x: x[0])
    if dropped and log:
        log(f"※ LLM 항목 {dropped}개가 형식 오류(키 누락·NaN·타입)로 제외됐습니다")
    return out


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
# 성인영상 전사의 고질병 = 신음·무음 구간에서 Whisper가 '환청자막(hallucination)'을
# 지어내거나 같은 말을 반복함. 아래 파라미터 + 후처리 필터로 최대한 억제한다.

# JA Whisper 상습 환청 문구(자막 크레딧류) — 발견 즉시 버림
HALLUCINATION_JA = (
    "ご視聴ありがとうございました", "ご視聴ありがとうございます", "チャンネル登録",
    "高評価", "最後までご視聴", "字幕", "提供", "お楽しみください",
    "ありがとうございました", "この動画は", "次の動画でお会いしましょう",
)

def _looks_hallucinated(t):
    """환청/무의미 세그먼트 판별(신음·반복·자막크레딧)."""
    s = (t or "").strip()
    if not s:
        return True
    if any(h in s for h in HALLUCINATION_JA):
        return True
    comp = s.replace(" ", "")
    if len(comp) >= 2:
        # 같은 문자 반복 비율이 과도(예: ああああ, んんん, wwww)
        uniq = len(set(comp))
        if uniq <= 2 and len(comp) >= 4:
            return True
        # 한 글자가 전체의 70%↑
        from collections import Counter
        top = Counter(comp).most_common(1)[0][1]
        if top / len(comp) >= 0.7 and len(comp) >= 5:
            return True
    return False


_CUDA_DLL_DONE = False

def _ensure_cuda_dll_path(log=print):
    """faster-whisper(CTranslate2)가 cublas64_12.dll 등을 찾도록 nvidia 패키지 bin 경로를 등록.
    CTranslate2는 cudnn만 자동 등록하고 cublas는 안 해서 PATH에 시스템 CUDA가 없으면 실패한다.
    → venv 안 nvidia-*-cu12 패키지의 bin을 DLL 검색 경로/PATH에 직접 넣어 환경 무관하게 동작."""
    global _CUDA_DLL_DONE
    if _CUDA_DLL_DONE:
        return
    import os, sys, site
    bases = []
    try:
        bases += site.getsitepackages()
    except Exception:
        pass
    bases += [p for p in sys.path if p.endswith("site-packages")]
    subs = ("nvidia/cublas/bin", "nvidia/cudnn/bin",
            "nvidia/cuda_runtime/bin", "nvidia/cuda_nvrtc/bin")
    added = []
    seen = set()
    for base in bases:
        for sub in subs:
            d = os.path.join(base, *sub.split("/"))
            if os.path.isdir(d) and d not in seen:
                seen.add(d)
                try:
                    os.add_dll_directory(d)
                except Exception:
                    pass
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                added.append(d)
    if added:
        log(f"CUDA DLL 경로 등록: {len(added)}개 (cublas/cudnn)")
    _CUDA_DLL_DONE = True


def transcribe(video, model_name="large-v3", log=print, progress=None, initial_prompt=None):
    """
    고도화 전사. initial_prompt(작품 제목·배우명 등 맥락)를 주면 정확도↑.
    환청 억제 파라미터 + 후처리 필터로 신음/무음발 가짜자막을 걸러낸다.
    progress(frac 0~1) 콜백을 주면 전사 진행률을 보고한다.
    """
    log(f"Whisper 전사 (모델 {model_name})...")
    _ensure_cuda_dll_path(log)
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="auto", compute_type="auto")
    segs, info = model.transcribe(
        str(video),
        language="ja",
        task="transcribe",
        beam_size=5,
        temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],   # 실패 시 온도 폴백
        condition_on_previous_text=False,             # ★ 신음→직전텍스트 반복 폭주 차단(핵심)
        compression_ratio_threshold=2.4,              # 반복 텍스트 세그 폐기
        log_prob_threshold=-1.0,                      # 저확신 세그 폐기
        no_speech_threshold=0.6,                      # 무음/비음성 컷
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200, threshold=0.5),
        initial_prompt=initial_prompt or None,
    )
    dur = float(getattr(info, "duration", 0) or 0)
    log(f"모델 로드/오디오 분석 완료 (길이 {dur:.0f}s). 전사 시작…")
    out, dropped = [], 0
    for s in segs:  # faster-whisper는 지연 생성 → 세그 처리할수록 s.end 증가
        t = (s.text or "").strip()
        if t:
            if _looks_hallucinated(t):
                dropped += 1
            else:
                out.append((float(s.start), float(s.end), t))
        if progress and dur:
            progress(max(0.0, min(0.99, float(s.end) / dur)))
        if out and len(out) % 50 == 0:
            log(f"   …{len(out)} 세그먼트")
    if progress:
        progress(1.0)
    out = sanitize_segments(out)   # 타임스탬프 역전/겹침/순서 정상화
    before = out
    out = clamp_durations(out)     # 글자수 대비 과길이 자막 컷(무음/신음 구간 끌림 제거)
    trimmed = sum(1 for (a, b, *_), (a2, b2, *_2) in zip(before, out) if b - b2 > 0.3)
    log(f"전사 완료: {len(out)} 세그먼트 (환청/무의미 {dropped}개 제거, 과길이 자막 {trimmed}개 단축)")
    return out


def build_initial_prompt(meta):
    """메타(제목·배우)를 Whisper initial_prompt 힌트로. 정확도 소폭↑."""
    if not meta:
        return None
    bits = []
    if meta.get("title_ja"):
        bits.append(str(meta["title_ja"]))
    if meta.get("actress_ja"):
        bits.append(str(meta["actress_ja"]))
    return "。".join(bits)[:200] if bits else None


# ─── ①-b 전사 검증 (Claude) ──────────────────────────────────────────────────
def verify_transcript(segments, meta=None, which="claude", batch=40, log=print):
    """
    Whisper 일본어 전사를 Claude로 검증. 일어를 몰라도 판단 가능하게:
      - 각 세그먼트를 dialogue(실대사)/moan(신음)/noise(잡음)/hallucination(환청)으로 분류
      - 한국어 번역을 나란히 제공
      - keep=false(신음·환청) 는 스토리 요약 입력에서 제외
    반환: [{i,start,end,ja,ko,type,keep}] (입력 순서 정렬)
    """
    ctx = _meta_block(meta) if meta else "(메타 없음)"
    results = [None] * len(segments)
    for b0 in range(0, len(segments), batch):
        chunk = segments[b0:b0 + batch]
        lines = "\n".join(f"{b0+k}\t{ja}" for k, (_s, _e, ja) in enumerate(chunk))
        prompt = (
            "너는 일본 영상 자막 검수·번역 전문가다. 아래는 Whisper가 뽑은 일본어 전사(줄마다 '번호<TAB>일본어').\n"
            "각 줄을 판정하고 자연스러운 한국어로 번역하라.\n"
            "판정 type: dialogue(스토리 대사)/moan(신음·탄성)/noise(잡음·의미없음)/hallucination(무음인데 지어낸 가짜자막).\n"
            "keep: 스토리 요약에 쓸 실제 대사면 true, 신음/잡음/환청이면 false.\n"
            "ko: 실제 대사는 매끄러운 한국어 구어체로 번역. 신음/잡음은 '(신음)'·'(가쁜 숨)' 등 짧은 지문으로.\n"
            "환청 의심(맥락과 동떨어지거나 자막크레딧·반복)은 반드시 hallucination.\n"
            f"작품 맥락:\n{ctx}\n\n"
            f"전사:\n{lines}\n\n"
            '반드시 JSON만 출력: {"items":[{"i":번호,"type":"...","keep":true/false,"ko":"한국어"}]}'
        )
        try:
            res = call_llm(prompt, which=which, log=log)
        except Exception as e:
            log(f"  검증 배치 실패({b0}) {type(e).__name__}: {e}")
            res = {"items": []}
        for it in (res or {}).get("items", []):
            try:
                i = int(it["i"])
            except Exception:
                continue
            if 0 <= i < len(segments):
                s, e, ja = segments[i]
                results[i] = {
                    "i": i, "start": s, "end": e, "ja": ja,
                    "ko": (it.get("ko") or "").strip(),
                    "type": it.get("type") or "dialogue",
                    "keep": bool(it.get("keep", True)),
                }
        log(f"  검증 {min(b0+batch, len(segments))}/{len(segments)}")
    # 누락(LLM이 빠뜨린 줄)은 원문 유지 + keep True로 보수
    for i, (s, e, ja) in enumerate(segments):
        if results[i] is None:
            results[i] = {"i": i, "start": s, "end": e, "ja": ja, "ko": "", "type": "dialogue", "keep": True}
    return results


def write_verify_report(rows, out_md):
    """검증 결과를 사람이 눈으로 보는 리포트(MD)로. 일어 몰라도 한국어로 품질 판단."""
    from pathlib import Path as _P
    def ts(x):
        m, s = divmod(int(x), 60); return f"{m:02d}:{s:02d}"
    kept = [r for r in rows if r["keep"]]
    lines = [f"# 전사 검증 리포트  (전체 {len(rows)} · 스토리대사 {len(kept)})\n"]
    lines.append("| # | 시간 | 판정 | 일본어 | 한국어 |")
    lines.append("|---|------|------|--------|--------|")
    for r in rows:
        mark = "✅" if r["keep"] else "⬜"
        ja = (r["ja"] or "").replace("|", "／")
        ko = (r["ko"] or "").replace("|", "／")
        lines.append(f"| {r['i']} | {ts(r['start'])} | {mark}{r['type']} | {ja} | {ko} |")
    _P(out_md).write_text("\n".join(lines), encoding="utf-8")
    return out_md


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
def _cli_path(name):
    """Windows의 npm 글로벌 CLI는 name.cmd 가 실제 실행 래퍼. subprocess(['codex',...])는
    PATHEXT를 안 뒤져 WinError2(파일 못 찾음) → .cmd/.exe/.bat 풀경로를 직접 찾아 반환."""
    import shutil
    if os.name == "nt":
        for ext in (".cmd", ".exe", ".bat"):
            p = shutil.which(name + ext)
            if p:
                return p
    p = shutil.which(name)
    if not p:
        raise RuntimeError(f"{name} CLI를 찾을 수 없습니다 — 설치/PATH 확인하거나 다른 LLM을 선택하세요.")
    return p


def call_llm(prompt, which="claude", log=print):
    log(f"LLM({which}) 호출...")
    # ★ 프롬프트는 반드시 STDIN으로 전달한다. 인자(argv)로 넘기면 긴 다중행(자막 본문)이
    #   잘려 모델이 본문을 못 받고 "자막을 보내달라"는 헛응답을 한다(거부처럼 보임). stdin이면 정상.
    if which == "codex":
        exe = _cli_path("codex")
        with tempfile.TemporaryDirectory() as td:
            outf = Path(td) / "o.json"
            # stderr를 버리면 인증실패(401)·레이트리밋을 '빈 응답'으로만 보게 된다 → 캡처한다
            p = subprocess.run([exe, "exec", "--ephemeral", "--skip-git-repo-check",
                                "-c", 'model_reasoning_effort="high"', "-o", str(outf)],
                               input=prompt, timeout=900, text=True, encoding="utf-8",
                               errors="replace", capture_output=True)
            txt = outf.read_text(encoding="utf-8") if outf.exists() else ""
            if not txt.strip():
                err = (p.stderr or p.stdout or "").strip()
                low = err.lower()
                if "401" in err or "unauthorized" in low or "bearer" in low or "auth" in low:
                    raise RuntimeError("codex 로그인 안 됨 — 'codex login' 후 다시 시도하세요. "
                                       f"(원문: {err[-200:]})")
                if "429" in err or "rate limit" in low:
                    raise RuntimeError(f"codex 레이트리밋/한도 — 잠시 후 재시도. (원문: {err[-200:]})")
                if err:
                    raise RuntimeError(f"codex 응답 없음: {err[-240:]}")
    else:
        exe = _cli_path("claude")
        p = subprocess.run([exe, "-p"], input=prompt, timeout=900, text=True,
                           encoding="utf-8", errors="replace", capture_output=True)
        txt = p.stdout or ""
    s = txt.strip(); i = s.find("{")
    if i < 0:
        # 거부 문구가 텍스트로 온 경우 그 내용을 그대로 보여준다(원인 파악 가능하게)
        hint = f" 응답: {s[:200]}" if s else ""
        raise RuntimeError(f"LLM JSON 응답 없음 (빈 응답/로그인/콘텐츠 거부 확인).{hint}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(s, i)
        return obj
    except json.JSONDecodeError:
        j = s.rfind("}")
        if j <= i:
            raise RuntimeError("LLM JSON 응답 없음 (빈 응답/로그인 확인 — 헤드리스 거부 시 수동 모드 사용)")
        return json.loads(s[i:j + 1])


def llm_ping(which):
    """CLI 연결/로그인/응답 확인용 — 자명한 프롬프트로 JSON 왕복. (콘텐츠 정책과 무관한 헬스체크)
    반환: (ok: bool, msg: str)."""
    try:
        exe = _cli_path(which)
    except Exception as e:
        return False, str(e)
    try:
        r = call_llm('아래 JSON 한 줄만 출력: {"ok": true}', which, log=lambda *_: None)
        if isinstance(r, dict) and r.get("ok") is True:
            return True, "정상 (설치·로그인·응답 OK)"
        return True, "응답함(형식은 약간 다름) — 로그인 정상으로 보임"
    except Exception as e:
        return False, str(e)[:120]


def _meta_block(meta):
    g = ", ".join(meta.get("genres") or []) or (meta.get("genre") or "")
    out = (f"품번:{meta.get('code')} 배우:{meta.get('actress')}({meta.get('actress_ja')}) "
           f"신체:{meta.get('meas')}"
           + (f" 생일:{meta.get('birthday')}" if meta.get('birthday') else "")
           + (f" 혈액형:{meta.get('blood_type')}" if meta.get('blood_type') else "")
           + f" 레이블:{meta.get('label')} 메이커:{meta.get('maker')} "
           f"감독:{meta.get('director')} 시리즈:{meta.get('series_ja') or '-'} 장르:{g} "
           f"발매:{(meta.get('release_date') or '')[:10]} 런타임:{meta.get('runtime_mins')}분 "
           f"인기:조회{meta.get('views')}/좋아요{meta.get('likes')}/싫어요{meta.get('dislikes')}\n"
           f"일본원제:{meta.get('title_ja')}\n한국어시놉시스:{meta.get('description')}")
    if meta.get("hook_title") or meta.get("hook_desc"):
        out += f"\n편집훅(내레이션 활용): {meta.get('hook_title') or ''} — {meta.get('hook_desc') or ''}"
    return out


# 번역 규칙 — jav-subtitle-translate 스킬(AVDBS Eddy_Wind / 사용자 노션 원안)에서 추출.
# 자동 파이프라인(claude/codex CLI)은 스킬 파일을 못 읽으므로 핵심을 여기 직접 박는다.
def _translate():
    return ("[대사 번역 규칙 — 성인 콘텐츠 현지화 전문가] "
            "P1 정확성: 의미·감정·어조·의도 100% 재현(오역/왜곡/누락 금지). "
            "P2 자연스러움: 번역투 전면 제거, 유창한 현대 한국어 구어체(일본식 한국어 금지). "
            "P3 현지화: 줄거리·장면·인물 맥락 종합해 가장 한국적인 어조/어휘로. "
            "P4 뉘앙스: 성적 긴장·심리·비언어 함의를 한국어 표현력으로. "
            "[19금] 저속함 지양·세련된 성적 담론. 강도(흥분/쾌감/고통)는 어미·어휘로 정밀 제어. "
            "신음·짧은탄성은 음차(아앙/앗) 금지 → 맥락 감정표현 '(쾌감 섞인 신음)'·'(가쁜 숨)' 또는 자연스러운 감탄사. "
            "단어는 직역 말고 상황 정서로 의역(예: 気持ちいい→가, 간다!/미칠 것 같아, 余裕→더 할 수 있어/더 원해, "
            "イク→갈게/간다, 締め→숨 막히는 쾌감). 같은 단어도 장면 맥락 따라 달라짐.")


NARRATION_STYLES = {
    "3min": "3분휴지형 (담백한 총평 리뷰 · 원음 끄고 해설만)",
    "cinema": "고몽·김시선형 (대사와 주고받는 해설 · 원음 살림)",
}


def _style_cinema():
    """영화 해설 채널(고몽·김시선) 문체. 실측 근거:
      · 대사 삽입 빈도 — 고몽 15.1회/분(4.0초마다), 김시선 9.8회/분(6.1초마다)
      · 종결 '~니다' 최다에 '~죠/~인데요/~네요/~예요' 혼용(해요체 섞인 합니다체)
      · 고몽은 플롯 추진(하지만·그러자), 김시선은 해석·추정(보여주다·추정·같아요)
    핵심은 '내레이션이 대사를 감싼다'는 것 — 설명하다 대사로 넘기고, 대사가 끝나면 받아친다."""
    return (
        "[캐릭터] 딸딸기튜브 해설자 '딸감별사'. 고몽·김시선 같은 영화 해설 채널 문체를 따른다. "
        "[★어조] 현재형 서술로 장면을 중계하듯 쓴다. 종결은 '~습니다'를 기본으로 하되 "
        "'~죠', '~인데요', '~네요', '~거예요'를 자연스럽게 섞어 말맛을 준다. "
        "3분휴지처럼 딱딱한 총평체가 아니라, 옆에서 같이 보며 얘기해 주는 말투다. 느낌표·과장 감탄은 여전히 금지. "
        "[★대사와 주고받기 — 이 스타일의 핵심] 내레이션은 실제 대사(dialogue)를 '감싸는' 형태로 쓴다.\n"
        " · 대사 직전 내레이션: 상황을 세우고 대사로 넘긴다. 문장을 끝맺지 않고 이어지게 둘 수도 있다.\n"
        "   (예: '남자가 원하는 건 단 하나였습니다.' → [대사] → '그 말에 여자는 아무 대답도 하지 못하죠.')\n"
        " · 대사 직후 내레이션: 방금 나온 대사를 받아 해석하거나 다음으로 넘긴다.\n"
        " · 같은 말을 반복하지 말 것. 대사가 이미 말한 내용을 내레이션이 또 설명하면 지루해진다.\n"
        "[★타이밍 — 반드시 지킬 것] narration 항목은 dialogue 항목의 시간과 **절대 겹치지 않게** 배치한다. "
        "대사가 흐르는 동안은 내레이션을 넣지 않는다(대사 원음이 들려야 한다). 대사와 대사 사이 빈 구간에만 넣는다. "
        "대사가 촘촘하면 내레이션을 짧게 끊어 여러 항목으로 나눈다. "
        "[리듬] 대사 사이 간격이 4~6초가 되도록 대사를 골라 배치하면 좋다. 내레이션만 길게 이어지는 구간을 만들지 말 것. "
        "[해석] 장면의 의미·인물의 속내를 한 겹 더 짚어준다. 다만 화면을 못 봤으므로 자막에서 확인되는 범위 안에서만. "
        "추측은 '~인 것 같습니다', '~라고 볼 수 있겠죠'처럼 추측임을 드러낸다. "
        "[구성] 훅이 되는 장면·대사로 연다 → 인물과 관계 소개 → 갈등이 커지는 대목 → 전환점 → "
        "여운 남기는 마무리. 총평·별점은 마지막에 짧게. "
        "**[AI 문체 금지]** '~에 초점을 둔 구성', '~을 선사합니다', 형용사 3개 이상 나열, 같은 종결어미 3연속. **자막·대사를 메타적으로 지칭하지 말 것** — '자막을 보면', '자막에서', '~라는 대사에서'는 금지. "
        "**[유튜브 안전]** 약물 직접 언급 금지 — '약 먹이다/약물/수면제/최음제/약 기운' → '몽롱물', '분위기가 달라지는데..', '몸 상태가 이상해지는데..'로 대체. "
        "[대사] 자연스러운 한국어 구어체(번역투 금지), 신음류 제외/(신음). 각 대사에 speaker 지정 — '여'/'남'. "
        "[자막 길이] 각 항목 25자 이내, 길면 의미 단위로 끊어 여러 항목. "
        "[내레이션 유형] style 지정 — '기본'(장면 중계)/'강조'(훅·반전·총평)/'정보'(스펙·레이블·수치).\n"
        + _translate())


def _style(kind="3min"):
    """내레이션 문체 선택. 3min=3분휴지형(총평 리뷰), cinema=고몽·김시선형(대사 주고받기)."""
    return _style_cinema() if kind == "cinema" else _style_3min()


def _must_have(kind="3min"):
    """스타일별 '내레이션 필수 요소' + 자기점검. 두 스타일은 요구가 다르므로 섞으면 안 된다
    (예: cinema에 '~작품이었습니다 총평'·'호불호 고지'를 강요하면 문체가 무너진다)."""
    if kind == "cinema":
        return (
            "[내레이션 필수 요소 — 하나라도 빠지면 실패]\n"
            " (a) 전환 한 문장 — 위 [묶음 리뷰] 지시대로 연다.\n"
            " (b) 인물·관계 소개 — 자막에서 확인되는 범위 안에서.\n"
            " (c) 대사와 주고받기 — narration 항목이 dialogue 항목 사이 빈 구간에만 놓이고,"
            " 대사 직전/직후를 자연스럽게 받아야 한다. 대사가 이미 말한 내용을 되풀이하지 말 것.\n"
            " (d) 장면 해석 최소 1회 — 인물의 속내나 상황의 의미를 한 겹 더 짚는다(추측은 추측으로 표시).\n"
            " (e) 갈등이 커지는 대목과 전환점을 짚는다.\n"
            " (f) 마무리 — 여운을 남기고 총평·별점(stars)은 짧게 한두 줄.\n"
            "[자기점검] 출력 전에 확인: narration 시간이 dialogue 시간과 겹치지 않는가? "
            "느낌표·과장 감탄이 없는가? 같은 종결어미를 3연속 쓰지 않았는가? "
            "대사가 말한 내용을 내레이션이 다시 설명하고 있지 않은가? "
            "첫 문장이 전환 문구로 시작하는가? 채널 인사·구독 요청을 넣지 않았는가?\n")
    return (
        "[내레이션 필수 요소 — 하나라도 빠지면 실패]\n"
        " (a) 전환+소개 한 문장 — 위 [묶음 리뷰] 지시대로 열고, '~라는 내용의 작품입니다'로 설정을 요약.\n"
        " (b) 설정·전개 포인트 — 반드시 위 자막에서 확인되는 내용만.\n"
        " (c) 배우 필모 맥락 — 메타의 배우·레이블·발매일·인기지표를 근거로 '지금까지의 작품들 중에서',"
        " '요즘 작품들이 아쉬웠는데' 같은 비교를 최소 1회.\n"
        " (d) 단점·아쉬운 점 최소 1개 — 장점만 늘어놓으면 안 된다.\n"
        " (e) 호불호 갈릴 요소 고지 — '호불호가 갈릴 수 있지만' 형태로 최소 1회.\n"
        " (f) 총평 — '~작품이었습니다'로 닫고 별점(stars) 근거를 한 줄로.\n"
        "[자기점검] 출력 전에 확인: 반말·느낌표·과장 감탄이 하나라도 있으면 다시 쓴다. "
        "모든 문장이 '~입니다/~습니다'로 끝나는가? 단점을 짚었는가? 배우 맥락이 있는가? "
        "첫 문장이 전환 문구로 시작하는가? 채널 인사·구독 요청을 넣지 않았는가?\n")


def _style_3min():
    """내레이션 문체. 3분휴지 채널 실제 자막 3편(325문장) 분석에 근거해 작성.
    측정값: 정중체 100%(입니다 138·습니다 135), 반말/감탄사/느낌표 0회,
    문장 평균 45자, '굉장히' 32회, 종결 1위 '…작품입니다' 83회, '…작품이었습니다' 20회.
    → 과장·반말·훅 강요는 실제 스타일이 아니므로 넣지 않는다."""
    return (
        "[캐릭터] 딸딸기튜브 리뷰어 '딸감별사'. 3분휴지 채널 문체를 따른다. "
        "[★어조 — 반드시 지킬 것] 처음부터 끝까지 정중체(~입니다/~습니다)로만 쓴다. "
        "반말·반존대('~거든요','~겠죠?','~야','자,') 금지. 느낌표 금지. "
        "'미쳤습니다','이거 물건입니다' 같은 과장된 감탄 금지. 담백하고 차분하게, 솔직하게. "
        "[문장] 한 문장 35~50자. 짧고 평이하게. 수식어 겹치지 말 것. 부사는 '굉장히'를 아껴 쓴다. "
        "[★평가 어법 — 유보적·균형적] 단정보다 저울질하는 말투를 쓴다. "
        "'재밌다고 하기에는 아쉬움이 남는', '엄청 알차다고 하기보다는 무난한 느낌', "
        "'나쁘지는 않았으나 그렇다고', '다만 아쉬운 점은', '~수도 있지만' 처럼. 장점만 늘어놓지 말고 단점을 반드시 하나는 짚는다. "
        "[★배우 맥락 — 핵심] 배우의 필모 흐름 안에서 평가한다. "
        "'지금까지의 OOO 작품들 중에서', '요즘 작품들이 아쉬웠는데 다시 한번 클라스를', "
        "'데뷔 이후', '이번에는 데뷔초 때처럼 좋은 폼을' 같은 비교를 넣는다. "
        "[★호불호 고지] 취향 갈릴 요소는 미리 알려준다. '호불호가 갈릴 수 있지만', '많은 분들이 실망하실 수 있지만 금방 넘어갑니다'. "
        "[어휘] 플레이·컨셉·매력·폼·기본기·알차다·무난하다·클라스·호불호·명작·시추에이션·오피스물·상황극. "
        "[구성] ①작품 소개 한 문장('~라는 내용의 작품입니다') → ②설정·전개 포인트(자막 근거) → "
        "③배우 필모 맥락 평가 → ④호불호 고지 → ⑤총평('~작품이었습니다')+별점. "
        "첫 줄은 억지 훅 대신 '이번 작품은 ~라는 내용입니다'처럼 담담히 연다. "
        "[★신뢰성 — 창작 절제] 영상을 못 봤으므로 '카메라 워크가 터진다'처럼 화면을 본 척하는 구체 평가는 금지. "
        "평가 근거는 시놉/설정/전개(자막) + 인기지표(좋아요·싫어요) + 배우스펙/레이블(메타)뿐. "
        "추측은 '설정상 ~할 겁니다','~라 기대됩니다'처럼 추측임을 드러낸다. "
        "**[AI 문체 금지]** '~에 초점을 둔 구성', '~의 훅이 작동합니다', '~을 선사합니다', 형용사 3개 이상 나열. **자막·대사를 메타적으로 지칭하지 말 것** — '자막을 보면', '자막에서', '~라는 대사에서', '~라는 대사가'는 금지. 시청자는 그 장면을 보고 있다. 내용을 바로 서술하라. "
        "(단, '~작품입니다'·'~내용입니다' 종결은 이 채널의 기본 어투이므로 적극 사용한다.) "
        "**[유튜브 안전]** 약물 직접 언급 금지 — '약 먹이다/약물/수면제/최음제/약 기운' → '몽롱물', '분위기가 달라지는데..', '몸 상태가 이상해지는데..'로 대체. "
        "섹스 스킵 구간은 브릿지('이후 장면은 생략하고…'). "
        "[대사] 자연스러운 한국어 구어체(번역투 금지), 신음류 제외/(신음). 각 대사에 speaker 지정 — '여'/'남'. "
        "[자막 길이] 각 항목 25자 이내, 길면 의미 단위로 끊어 여러 항목. "
        "[내레이션 유형] style 지정 — '기본'(해설)/'강조'(총평·별점·반전 등 핵심 한두 줄)/'정보'(스펙·레이블·수치). "
        "'강조'는 남발하지 말고 총평과 결정적 한 줄에만.\n"
        "[문장 예시 — 이 톤을 그대로 따를 것]\n"
        "· 평일에는 조용한 여교사이지만 주말이 되면 미쳐 날뛴다는 내용입니다.\n"
        "· 설정은 딱히 중요하지 않은 작품으로, 그냥 열심히 하는 작품입니다.\n"
        "· 엄청 재밌고 내용이 알차다고 하기보다는 내용 자체는 무난한 느낌입니다.\n"
        "· 컨셉을 메인으로 내세웠으면 끝까지 제대로 해야 됐는데 그러지 못한 느낌입니다.\n"
        "· 지금까지의 작품들 중에서 의상이 가장 베스트라고 할 수 있겠습니다.\n"
        "· 요즘 작품들이 조금 아쉬웠는데 다시 한번 클라스를 느낄 수 있었습니다.\n"
        "· 명작이라고 하기에는 조금 아쉬우나 충분히 재밌는 작품이었습니다.\n"
        "· 다만 아쉬운 점은 처음부터 끝까지 의상을 입고 나오는 게 조금 아쉬웠습니다.\n"
        + _translate())


def _hint_block(hint):
    h = (hint or "").strip()
    return f"[사용자 추가 지시 — 최우선 반영]\n{h}\n" if h else ""


def _timeline_rule():
    """narration/dialogue 시간이 keep 밖으로 나가면 안 되는 이유:
    최종 영상은 keep 구간만 이어붙인 것이라, 밖의 항목은 retime에서 끝점으로 밀려
    길이 0으로 뭉친다(총평·별점이 통째로 사라진다). 실제 LLM 출력에서 재현됨."""
    return (
        "[★시간 규칙 — 어기면 결과물이 깨진다]\n"
        " · narration과 dialogue의 start/end는 **반드시 keep 구간 안**에 있어야 한다. "
        "최종 영상은 keep만 이어붙인 것이라, 밖에 있는 항목은 잘려 사라진다.\n"
        " · 특히 마지막 총평·별점을 keep 밖(영상이 끝난 뒤)에 두지 말 것. "
        "keep의 마지막 구간 안에서 끝내라.\n"
        " · 내레이션 전체 발화 길이가 keep 구간 길이의 합을 넘지 않게 한다.\n")


# 3분휴지 실측(3편·55작품·1746초·14965자): 문장당 5.4초, 발화 8.6자/초, 문장 평균 45자.
# 다만 8.6자/초는 '편집된 방송' 속도다. TTS(voicebox 한국어)는 대략 6.5~7자/초라
# 그대로 쓰면 문장이 슬롯을 넘어 겹친다(build_narration_wav가 보정하긴 하나 촘촘해진다).
# → 실제 예산은 TTS 속도로 잡고, 문장 사이 숨돌림 여유(0.85)를 곱한다.
_TTS_CHARS_PER_SEC = 6.8
_BREATH = 0.85           # 전체 길이 중 실제 발화가 차지할 비율
_SEC_PER_SENT = 5.4      # 3분휴지 실측 — 문장 개수 산정용


# cinema(고몽·김시선형)는 대사 원음이 흐르는 동안 내레이션이 빠지므로 발화 시간이 줄어든다.
# 자동자막에는 화자 라벨이 없어(모든 전환이 '>>') 실제 내레이션/대사 비율은 측정 불가.
# → 아래 값은 측정치가 아니라 설계 판단이다. 대사가 화면의 40% 안팎을 쓴다고 보고 잡았다.
_CINEMA_SPEECH_RATIO = 0.6


def narration_budget(target_sec, style="3min"):
    """목표 길이(초) → (문장 수 하한, 상한, 대략 글자수). 글자수는 TTS 발화속도 기준.
    3min : 내레이션이 영상을 거의 다 덮는다 → 60초 = 10~13문장·346자
    cinema: 대사 구간에는 내레이션이 없다 → 60초 = 6~9문장·207자"""
    try:
        target_sec = float(target_sec)
    except (TypeError, ValueError):
        target_sec = 60.0
    target_sec = max(10.0, min(1800.0, target_sec))    # 10초~30분 안으로
    ratio = _CINEMA_SPEECH_RATIO if style == "cinema" else 1.0
    speak_sec = target_sec * ratio
    n = max(3, round(speak_sec / _SEC_PER_SENT))
    chars = int(speak_sec * _TTS_CHARS_PER_SEC * _BREATH)
    return n - 1, n + 2, chars


def _roundup_block(pos="mid", target_sec=60, style="3min"):
    """묶음 리뷰(여러 작품을 한 영상에 이어붙임)의 '한 꼭지'로 쓰이는 내레이션 규칙.
    3분휴지 실측: 영상 1편에 작품 16~23개, 작품당 29~36초.
    첫 꼭지 '먼저 ~', 중간 '다음은 ~', 끝 '마지막으로 ~'. 인사·구독은 영상 맨 끝에 한 번만."""
    pos = pos if pos in ("first", "mid", "last") else "mid"
    opener = {"first": "먼저", "mid": "다음은", "last": "마지막으로"}[pos]
    lo, hi, chars = narration_budget(target_sec, style)
    kind = "첫" if pos == "first" else ("마지막" if pos == "last" else "중간")
    tail = (" 대사가 흐르는 구간에는 내레이션이 없으므로 문장 수가 적다.\n"
            if style == "cinema" else "\n")
    b = ("[★묶음 리뷰의 한 꼭지] 이 내레이션은 여러 작품을 이어붙인 리뷰 영상의 "
         f"'{kind}' 꼭지다. 독립 영상이 아니다.\n"
         f" · 첫 문장은 반드시 '{opener} OOO(배우)의 신작입니다.' 또는 '{opener} OOO의 작품입니다.'로 연다.\n"
         " · 채널 인사·자기소개·구독/좋아요 요청·'오늘은 ~을 준비했습니다' 금지. 바로 작품 얘기로 들어간다.\n"
         f" · 분량은 약 {int(target_sec)}초 — 내레이션 {lo}~{hi}문장, 합쳐 {chars}자 안팎. "
         f"이보다 길게 늘이지도, 짧게 줄이지도 말 것." + tail +
         " · 마지막 문장은 '~작품이었습니다.'로 닫고 다음 꼭지로 넘어갈 수 있게 끝낸다.\n")
    if pos == "last":
        b += (" · 이 꼭지가 마지막이므로, 총평 뒤에 마무리 인사 한 줄을 덧붙인다"
              "('오늘도 이상한 영상을 시청해 주셔서 감사합니다. 지금까지 딸감별사였습니다.').\n")
    return b


def prompt_auto(meta, segs, target_sec=60, hint="", pos="mid", style="3min"):
    body = "\n".join(f"{k}\t{a:.2f}\t{b:.2f}\t{t}" for k, (a, b, t) in enumerate(segs, 1))
    return (f"너는 일본 영상 리뷰어다. 아래 영상의 일본어 자막을 보고 '스토리 핵심만' 골라 "
            f"**약 {target_sec}초 내외 하이라이트 영상**으로 압축하고, 한글 대사자막과 해설 내레이션을 만든다.\n"
            f"{_hint_block(hint)}"
            f"[메타]\n{_meta_block(meta)}\n[일본어자막] 번호\\t시작초\\t끝초\\t대사\n{body}\n"
            f"{_roundup_block(pos, target_sec, style)}{_timeline_rule()}{_style(style)}\n"
            f"[규칙] (1)무음·잡담·반복·비스토리 라인은 버린다. "
            f"(2)스토리(설정·관계·전환·갈등·결말)를 드러내는 핵심 구간만 keep으로 골라 **합쳐서 {target_sec}초 ±20% 목표**. "
            f"(3)도입~결말 흐름이 보이게 고루 분포. 시간은 원본 영상 기준 초.\n"
            f"[출력 JSON만] {{\"summary\":\"3~5줄\",\"stars\":1~5,\"keep\":[[시작,끝],...],"
            f"\"dialogue\":[{{\"start\":초,\"end\":초,\"ko\":\"\",\"speaker\":\"여|남\"}}],\"narration\":[{{\"start\":초,\"end\":초,\"text\":\"\",\"style\":\"기본|강조|정보\"}}]}}")


def prompt_highlight(meta, segs, target_sec=60, hint="", pos="mid", style="3min"):
    """AlphaCut식 하이라이트 추출 — '고루 분포' 대신 '가장 후킹되는 순간'만 골라 몰아 뽑는다."""
    body = "\n".join(f"{k}\t{a:.2f}\t{b:.2f}\t{t}" for k, (a, b, t) in enumerate(segs, 1))
    return (f"너는 일본 영상 리뷰어다. 아래 영상 자막에서 **가장 후킹되는(클릭·시청유지 유발) 순간**만 골라 "
            f"**약 {target_sec}초 내외 하이라이트**로 압축한다. 줄거리 요약이 아니라 '반전·긴장·감정 절정'의 밀도 높은 컷.\n"
            f"{_hint_block(hint)}"
            f"[메타]\n{_meta_block(meta)}\n[일본어자막] 번호\\t시작초\\t끝초\\t대사\n{body}\n"
            f"{_roundup_block(pos, target_sec, style)}{_timeline_rule()}{_style(style)}\n"
            f"[하이라이트 규칙] "
            f"(1)잡담·무음·반복은 버린다. "
            f"(2)★고루 분포 금지★ — 앞·중간·뒤 균등이 아니라 **후킹 밀도가 가장 높은 순간에 집중**. "
            f"(3)각 컷마다 hook(후킹점수 1~5, 5=최고)과 reason(왜 후킹인지 한 줄)을 매긴다. "
            f"(4)점수 높은 컷들로 합쳐서 {target_sec}초 ±20% 채운다(부족하면 낮은 점수도 채택, 넘치면 상위만). "
            f"(5)각 컷은 2~15초 권장. 시간은 원본 영상 기준 초.\n"
            f"[출력 JSON만] {{\"summary\":\"3~5줄\",\"stars\":1~5,"
            f"\"picks\":[{{\"start\":초,\"end\":초,\"hook\":1~5,\"reason\":\"\"}}],"
            f"\"keep\":[[시작,끝],...],"  # picks 와 동일 구간(렌더 호환용)
            f"\"dialogue\":[{{\"start\":초,\"end\":초,\"ko\":\"\",\"speaker\":\"여|남\"}}],"
            f"\"narration\":[{{\"start\":초,\"end\":초,\"text\":\"\",\"style\":\"기본|강조|정보\"}}]}}")


def prompt_manual(meta, segs, target_sec=60, hint="", pos="mid", style="3min"):
    body = "\n".join(f"{k}\t{a:.2f}\t{b:.2f}\t{t}" for k, (a, b, t) in enumerate(segs, 1))
    return (f"너는 일본 영상 리뷰어다. 아래는 '정사장면을 이미 제거한' 영상의 일본어 자막이다. "
            f"여기서 **스토리 핵심만 골라 약 {target_sec}초 내외로 압축**하고, 한글 대사자막과 해설 내레이션을 만든다.\n"
            f"{_hint_block(hint)}"
            f"[메타]\n{_meta_block(meta)}\n[일본어자막] 번호\\t시작초\\t끝초\\t대사\n{body}\n"
            f"{_roundup_block(pos, target_sec, style)}{_timeline_rule()}{_style(style)}\n"
            f"[규칙] (1)무음·잡담·반복·의미없는 짧은 라인은 버린다. "
            f"(2)스토리(설정·관계·전환·갈등·결말)를 드러내는 핵심 구간만 keep으로 골라 **합쳐서 {target_sec}초 ±20% 목표**. "
            f"(3)정사 선별은 하지 말 것(이미 제거됨). 시간은 이 자막 기준 초.\n"
            f"{_must_have(style)}"
            f"[출력 JSON만] {{\"summary\":\"3~5줄\",\"stars\":1~5,\"keep\":[[시작,끝],...],"
            f"\"dialogue\":[{{\"start\":초,\"end\":초,\"ko\":\"\",\"speaker\":\"여|남\"}}],\"narration\":[{{\"start\":초,\"end\":초,\"text\":\"\",\"style\":\"기본|강조|정보\"}}]}}")


# ─── ④ 컷 / 재타이밍 ─────────────────────────────────────────────────────────
def _fit_tail(rows, total, min_dur=0.8, log=None):
    """영상 끝으로 밀려 길이 0으로 뭉친 항목을 살린다.

    LLM이 keep 밖에 내레이션을 두면(실측에서 11개 중 4개가 그랬다) snap 처리에서
    전부 total 지점에 길이 0으로 쌓여 총평·별점이 통째로 사라진다.
    끝에서부터 최소 길이를 확보하며 앞으로 밀어 넣는다 — 프롬프트 위반의 안전망.
    """
    if not rows or total <= 0:
        return rows
    rows = [list(r) for r in sorted(rows, key=lambda x: (x[0], x[1]))]
    fixed, limit = 0, float(total)
    for i in range(len(rows) - 1, -1, -1):
        s, e = float(rows[i][0]), float(rows[i][1])
        dur = max(min_dur, e - s)
        if e > limit + 1e-6 or (e - s) < min_dur * 0.5:
            e = min(limit, max(e, s + min_dur))
            s = max(0.0, e - dur)
            rows[i][0], rows[i][1] = s, e
            fixed += 1
        limit = min(limit, float(rows[i][0]))
    # 길이 0은 조용히 사라진다 → 최소 길이를 보장한다. 이때 앞 항목과 겹칠 수 있는데,
    # 겹침은 build_narration_wav가 가속/밀어내기로 처리한다(무음보다 낫다).
    for r in rows:
        if r[1] - r[0] < min_dur:
            if r[0] <= 1e-6:                 # 맨 앞이면 앞으로 못 미니 뒤로 늘린다
                r[1] = min(float(total), r[0] + min_dur)
            else:
                r[0] = max(0.0, r[1] - min_dur)
    if fixed and log:
        log(f"※ 내레이션 {fixed}개 시간 재배치(컷 밖으로 나간 항목을 끝에서부터 밀어 넣음)")
        need = sum(len(str(r[2])) for r in rows if len(r) > 2) / 6.8
        if need > total * 1.05:
            log(f"※ 내레이션 발화가 {need:.0f}초로 영상({total:.0f}초)보다 깁니다 — "
                f"문장 수를 줄이거나 목표 길이를 늘리세요(그대로 두면 뒷부분이 빨라지거나 잘립니다)")
    return [tuple(r) for r in rows]


def retime(entries, keep, snap=False, default_dur=4.0, log=None):
    """원본 시간 entries[(s,e,text)] → 컷(keep 이어붙임) 새 타임라인.
    keep 밖: snap=False(대사) 버림 / snap=True(내레이션) 컷 경계로 당김."""
    keep = sorted(keep); offs, acc = [], 0.0
    for a, b in keep:
        offs.append(acc); acc += (b - a)
    total = acc; out = []
    for s, e, *rest in entries:
        placed = False
        for (a, b), off in zip(keep, offs):
            if s >= a - 0.05 and s < b + 0.05:
                ns = off + max(0.0, s - a); ne = off + min(b - a, e - a)
                if ne <= ns:
                    ne = ns + 0.5
                out.append((ns, ne, *rest)); placed = True; break
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
        out.append((ns, ne, *rest))
    out.sort(key=lambda x: x[0])
    if snap:                       # keep 밖으로 나가 끝에 뭉친 항목 구조
        out = _fit_tail(out, total, log=log)
    return out


_NVENC = None  # None=미확인, True/False=캐시

def has_nvenc():
    """h264_nvenc를 실제로 쓸 수 있으면 True. 1회 확인 후 캐시.
    ffmpeg 빌드에 인코더가 있어도 NVIDIA 드라이버/GPU가 없으면 런타임에 실패하므로
    GPU 존재까지 확인한다(GPU 없는 서버에서 헛된 시도·폴백 비용 방지)."""
    global _NVENC
    if _NVENC is None:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
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
            subprocess.run(cmd, check=True)
            return
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                universal_newlines=True, encoding="utf-8", errors="replace")
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:  # 둘 다 마이크로초로 출력되는 ffmpeg 빌드가 많음
                    sec = min(dur, int(line.split("=", 1)[1]) / 1_000_000)
                    progress(max(0.0, min(0.99, (base + sec) / total)))
                except Exception:
                    pass
        proc.wait()
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
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf),
                        "-c", "copy", str(out_path)], check=True)
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
            capture_output=True, text=True, encoding="utf-8", errors="replace")
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
                check=True)
            segs.append(seg)
            if progress:
                progress(min(0.95, (i + 1) / (len(snapped) + 1)))
        listf = td / "list.txt"
        listf.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs), encoding="utf-8")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(listf), "-c", "copy", str(out_path)], check=True)
    if progress:
        progress(1.0)
    log(f"무손실 컷 완료: {out_path}")


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
            for _ in range(90):
                _time.sleep(3)
                try:
                    with urllib.request.urlopen(f"{base_url}/history/{gen_id}", timeout=10) as rh:
                        hj = json.loads(rh.read())
                    st = hj.get("status", "")
                    if st == "completed":
                        with urllib.request.urlopen(f"{base_url}/audio/{gen_id}", timeout=30) as ra:
                            Path(tmp).write_bytes(ra.read())
                        break
                    elif st in ("failed", "error"):
                        raise RuntimeError(f"voicebox 생성 실패: {hj.get('error')}")
                except urllib.error.HTTPError:
                    pass
            else:
                raise RuntimeError("voicebox 생성 타임아웃(270s)")
        else:
            raise RuntimeError(f"voicebox 응답에서 오디오를 못 찾음: keys={list(j.keys())}")
    # 표준 WAV(48k stereo)로 정규화
    subprocess.run(["ffmpeg", "-y", "-i", tmp, "-ar", "48000", "-ac", "2", out_wav],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if is_temp:
        try: Path(tmp).unlink()
        except Exception: pass
    return out_wav


def audio_duration(path):
    """오디오 길이(초). 실패 시 0."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)])
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
    subprocess.run(["ffmpeg", "-y", *inputs, "-filter_complex", "".join(filt) + mix,
                    "-map", "[a]", str(out_wav)], check=True)

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
    subprocess.run(["ffmpeg", "-y", "-i", str(video), "-i", str(narration_wav),
                    "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", str(out_video)], check=True)
    extra = ""
    if mode == "duck":
        extra = f" · 해설 중 원음 {int(duck_level * 100)}%"
    log(f"내레이션 입힌 영상({ORIG_AUDIO_MODES.get(mode, mode)}{extra}): {out_video}")
    return out_video


# ─── ⑥ 자막 굽기 (하드섭, ASS — 폰트/크기/위치/색상 설정 + 템플릿) ───────────
def video_wh(path):
    try:
        out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                       "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)])
        w, h = out.decode().strip().split("x")
        return int(w), int(h)
    except Exception:
        return 1920, 1080


def _ass_color(hexstr, alpha="00"):
    h = str(hexstr or "").lstrip("#")
    if len(h) != 6:
        return f"&H{alpha}FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()


def _ass_time(t):
    t = max(0.0, t); h = int(t // 3600); m = int(t % 3600 // 60); s = int(t % 60); cs = int(round((t - int(t)) * 100))
    if cs >= 100:
        s += 1; cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


_ALIGN = {("bottom", "left"): 1, ("bottom", "center"): 2, ("bottom", "right"): 3,
          ("middle", "left"): 4, ("middle", "center"): 5, ("middle", "right"): 6,
          ("top", "left"): 7, ("top", "center"): 8, ("top", "right"): 9}

# 기본 스타일 — 대사(하단 흰), 내레이션=기본(상단 노랑), 강조(중앙 큰 빨강), 정보(우상단 작은 하늘)
# anim: 등장 효과 — none | fade | pop | punch | slide  (_ass_anim 참고)
# 대사는 차분하게(none), 내레이션·강조·정보는 튀어나오게 → 리듬감
STYLE_DEFAULT = {
    "dialogue":  {"font": "Malgun Gothic", "size": 42, "color": "#FFFFFF", "outline_color": "#000000",
                  "outline": 2.2, "shadow": 0.4, "bold": True, "v": "bottom", "h": "center", "margin": 46,
                  "anim": "none"},
    "dialogue_m": {"font": "Malgun Gothic", "size": 42, "color": "#7FD0FF", "outline_color": "#000000",
                   "outline": 2.2, "shadow": 0.4, "bold": True, "v": "bottom", "h": "center", "margin": 46,
                   "anim": "none"},
    "narration": {"font": "Malgun Gothic", "size": 38, "color": "#FFD400", "outline_color": "#000000",
                  "outline": 2.2, "shadow": 0.4, "bold": True, "v": "top", "h": "center", "margin": 40,
                  "anim": "pop"},
    "emphasis":  {"font": "Malgun Gothic", "size": 52, "color": "#FF3B3B", "outline_color": "#000000",
                  "outline": 2.8, "shadow": 0.6, "bold": True, "v": "middle", "h": "center", "margin": 60,
                  "anim": "punch"},
    "info":      {"font": "Malgun Gothic", "size": 32, "color": "#8FE3FF", "outline_color": "#00243A",
                  "outline": 2.0, "shadow": 0.3, "bold": True, "v": "top", "h": "right", "margin": 30,
                  "anim": "slide"},
}

# LLM이 붙이는 내레이션 유형 → ASS 스타일명
STYLE_TAGNAME = {"기본": "Narration", "일반": "Narration", "강조": "Emphasis", "정보": "Info",
                 "normal": "Narration", "emphasis": "Emphasis", "info": "Info"}
# 대사 화자 → ASS 스타일명 (여=기본 Dialogue, 남=DialogueM)
SPEAKER_TAGNAME = {"여": "Dialogue", "여자": "Dialogue", "f": "Dialogue", "female": "Dialogue",
                   "남": "DialogueM", "남자": "DialogueM", "m": "DialogueM", "male": "DialogueM"}


def _style_line(name, st):
    st = {**STYLE_DEFAULT["dialogue"], **(st or {})}
    align = _ALIGN.get((st.get("v", "bottom"), st.get("h", "center")), 2)
    return (f"Style: {name},{st.get('font','Malgun Gothic')},{int(st.get('size',40))},"
            f"{_ass_color(st.get('color','#FFFFFF'))},&H000000FF,"
            f"{_ass_color(st.get('outline_color','#000000'))},&H64000000,"
            f"{-1 if st.get('bold') else 0},0,0,0,100,100,0,0,1,"
            f"{st.get('outline',2)},{st.get('shadow',0)},{align},40,40,{int(st.get('margin',40))},1")


def _ass_anim(kind, dur_ms):
    """자막 등장 효과 → ASS 인라인 오버라이드 태그. \\t(t1,t2,...)의 시각은 이벤트 시작 기준(ms).
    none  : 없음(그냥 뜸)
    pop   : 작게 나타나 살짝 커졌다가(오버슈트) 제자리 — '휙' 튀어나오는 느낌
    punch : pop보다 강하게. 강조 문구용
    fade  : 부드럽게 페이드
    slide : 아래에서 살짝 밀려 올라옴
    """
    if kind == "pop":
        return r"{\fad(0,120)\fscx70\fscy70\t(0,110,\fscx108\fscy108)\t(110,190,\fscx100\fscy100)}"
    if kind == "punch":
        return (r"{\fad(0,140)\fscx40\fscy40\t(0,90,\fscx118\fscy118)"
                r"\t(90,170,\fscx94\fscy94)\t(170,240,\fscx100\fscy100)}")
    if kind == "fade":
        return r"{\fad(180,180)}"
    if kind == "slide":
        # 아래에서 위로 24px — \move는 절대좌표라 여기선 원점 이동(\org) 대신 fad+scaleY로 대체
        return r"{\fad(0,120)\fscy60\t(0,150,\fscy105)\t(150,230,\fscy100)}"
    return ""


def build_ass(dialogue, narration, out_ass, width, height, styles=None):
    styles = styles or {}
    S = {k: {**STYLE_DEFAULT[k], **(styles.get(k) or {})}
         for k in ("dialogue", "dialogue_m", "narration", "emphasis", "info")}
    # 스타일 태그명 → 애니 종류
    ANIM = {"Dialogue": S["dialogue"].get("anim", "none"),
            "DialogueM": S["dialogue_m"].get("anim", "none"),
            "Narration": S["narration"].get("anim", "none"),
            "Emphasis": S["emphasis"].get("anim", "none"),
            "Info": S["info"].get("anim", "none")}
    L = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
         "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
         "[V4+ Styles]",
         "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
         "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
         "Alignment, MarginL, MarginR, MarginV, Encoding",
         _style_line("Dialogue", S["dialogue"]),
         _style_line("DialogueM", S["dialogue_m"]),
         _style_line("Narration", S["narration"]),
         _style_line("Emphasis", S["emphasis"]),
         _style_line("Info", S["info"]),
         "", "[Events]",
         "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    evs = []
    for it in dialogue:                        # (s,e,text) 또는 (s,e,text,speaker)
        spk = it[3] if len(it) > 3 else "여"
        evs.append((it[0], it[1], SPEAKER_TAGNAME.get(spk, "Dialogue"), it[2]))
    for it in narration:                       # (s,e,text) 또는 (s,e,text,style)
        tag = it[3] if len(it) > 3 else "기본"
        evs.append((it[0], it[1], STYLE_TAGNAME.get(tag, "Narration"), it[2]))
    evs.sort(key=lambda x: x[0])
    for s, e, style, t in evs:
        txt = str(t).replace("\n", "\\N")
        tag = _ass_anim(ANIM.get(style, "none"), int((e - s) * 1000))
        L.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},{style},,0,0,0,,{tag}{txt}")
    Path(out_ass).write_text("\n".join(L) + "\n", encoding="utf-8")
    return out_ass


"""배너 모션 기본값 — 브라우저 미리보기(/preview/data)와 반드시 같은 값을 쓴다."""
BANNER_ANIM = {"hold": 2.0, "fade": 0.5, "blur": 16, "wm_start": 2.1, "wm_slide": 40}


def _prep_banner_layers(banner, workdir, blur=16):
    """레이어 PNG를 굽기용으로 준비 — 내용 크기로 crop(오버레이 비용↓) + 인포카드 블러본 1회 생성.
    ffmpeg에서 gblur을 매 프레임 돌리면 수십 배 느려지므로 블러는 여기서 미리 굽는다.
    반환: {키: (경로, x, y)}  x,y = 원본 캔버스에서의 위치"""
    try:
        from PIL import Image, ImageFilter
    except Exception:
        return {}
    out = {}
    pad = int(blur * 2.5)      # 블러가 가장자리에서 잘리지 않도록 여유
    for k in ("frame", "info", "wm"):
        p = banner.get(k)
        if not p or not Path(p).is_file():
            continue
        im = Image.open(p).convert("RGBA")
        bb = im.getbbox()
        if not bb:
            continue
        if k == "info":        # 블러 번짐 여유를 두고 자른다
            bb = (max(0, bb[0] - pad), max(0, bb[1] - pad),
                  min(im.width, bb[2] + pad), min(im.height, bb[3] + pad))
        crop = im.crop(bb)
        cp = Path(workdir) / f"_bn_{k}.png"
        crop.save(cp)
        out[k] = (cp.name, bb[0], bb[1])
        if k == "info":
            bp = Path(workdir) / "_bn_info_blur.png"
            crop.filter(ImageFilter.GaussianBlur(blur)).save(bp)
            out["info_blur"] = (bp.name, bb[0], bb[1])
    return out


def _banner_filter(prep, anim):
    """배너 오버레이 filter_complex 조각. 미리보기와 동일 타이밍.
    인포카드: 페이드인(흐림→선명) → hold → 페이드아웃(선명→흐림)  워터마크: 페이드인+슬라이드.
    · blend=all_expr(픽셀별 수식)은 매우 느려서, 미리 구운 블러본과의 크로스디졸브로 대체
    · 애니메이션이 끝난 뒤엔 enable로 오버레이 자체를 꺼서 남은 구간 비용을 없앤다"""
    hold, fade = float(anim.get("hold", 2.0)), float(anim.get("fade", 0.5))
    wm_st, slide = float(anim.get("wm_start", 2.1)), float(anim.get("wm_slide", 40))
    end = hold + fade + 0.1
    inputs, fc, idx, last = [], "", 1, "0:v"
    order = [k for k in ("frame", "info_blur", "info", "wm") if k in prep]
    labels = {}
    for k in order:
        name, x, y = prep[k]
        inputs += ["-loop", "1", "-i", name]
        labels[k] = (idx, x, y)
        idx += 1
    # 레이어별 알파 애니메이션
    if "info" in labels:
        fc += (f"[{labels['info'][0]}]fade=t=in:st=0:d=0.4:alpha=1,"
               f"fade=t=out:st={hold}:d={fade}:alpha=1[ic];")
    if "info_blur" in labels:   # 등장 초반·퇴장 후반에만 겹쳐 흐림 효과를 만든다
        fc += (f"[{labels['info_blur'][0]}]fade=t=out:st=0:d={fade}:alpha=1,"
               f"fade=t=in:st={hold}:d={fade}:alpha=1,"
               f"fade=t=out:st={hold + fade}:d=0.1:alpha=1[icb];")
    if "wm" in labels:
        fc += f"[{labels['wm'][0]}]fade=t=in:st={wm_st}:d={fade}:alpha=1[wm];"
    # 합성 — 프레임 → 흐린 인포카드 → 선명 인포카드 → 워터마크
    n = 0
    if "frame" in labels:
        _, x, y = labels["frame"]
        n += 1
        fc += f"[{last}][{labels['frame'][0]}]overlay={x}:{y}[b{n}];"; last = f"b{n}"
    for key, lbl in (("info_blur", "icb"), ("info", "ic")):
        if key in labels:
            _, x, y = labels[key]
            n += 1
            fc += f"[{last}][{lbl}]overlay={x}:{y}:enable='lt(t,{end})'[b{n}];"; last = f"b{n}"
    if "wm" in labels:
        _, x, y = labels["wm"]
        n += 1
        sp = slide / max(fade, 0.01)     # 슬라이드 속도(px/s)
        fc += (f"[{last}][wm]overlay=x={x}:"
               f"y='{y}+min(0,-{slide}+(t-{wm_st})*{sp})'[b{n}];"); last = f"b{n}"
    return inputs, fc, last


def burn_subs(video, dialogue_srt, narration_srt, out_video, styles=None,
              narration_json=None, dialogue_json=None, log=print,
              banner=None, banner_anim=None, subs=True):
    """자막(+선택: 배너·워터마크)을 영상에 굽는다.
    banner={'frame':png,'info':png,'wm':png} 를 주면 자막과 같은 인코딩 1패스에서
    함께 합성한다(따로 굽는 2패스 대비 인코딩 1회 절약).
    banner에서 키를 빼면 그 레이어는 빠진다(미리보기 체크 그대로 굽기).
    subs=False면 자막 없이 배너만 굽는다."""
    w, h = video_wh(video)
    if dialogue_json and Path(dialogue_json).is_file():        # 화자(speaker) 포함 대사
        dd = json.loads(Path(dialogue_json).read_text(encoding="utf-8"))
        dlg = [(float(d["start"]), float(d["end"]), d["text"], d.get("speaker", "여")) for d in dd]
    else:
        dlg = srt_parse(dialogue_srt) if dialogue_srt and Path(dialogue_srt).is_file() else []
    if narration_json and Path(narration_json).is_file():     # 유형(style) 포함 내레이션
        data = json.loads(Path(narration_json).read_text(encoding="utf-8"))
        nar = [(float(d["start"]), float(d["end"]), d["text"], d.get("style", "기본")) for d in data]
    else:
        nar = srt_parse(narration_srt) if narration_srt and Path(narration_srt).is_file() else []
    if subs and not dlg and not nar:
        raise RuntimeError("입힐 자막(SRT)이 없습니다.")
    if not subs and not banner:
        raise RuntimeError("자막·배너 둘 다 꺼져 있어 구울 게 없습니다.")
    ass_path = Path(out_video).with_suffix(".ass")
    if subs:
        build_ass(dlg, nar, str(ass_path), w, h, styles)
        log(f"자막 굽기 (ffmpeg ass, {w}x{h}, 대사 {len(dlg)} · 내레이션 {len(nar)})...")
    else:
        ass_path.parent.mkdir(parents=True, exist_ok=True)
        log(f"자막 없이 배너만 굽기 ({w}x{h})...")

    inputs, fc, last = [], "", "0:v"
    if banner:
        prep = _prep_banner_layers(banner, ass_path.parent, (banner_anim or {}).get("blur", BANNER_ANIM["blur"]))
        if prep:
            inputs, fc, last = _banner_filter(prep, banner_anim or BANNER_ANIM)
            log(f"배너 동시 굽기: {', '.join(prep)} (재인코딩 1패스 — 추가 비용 적음)")

    if fc:
        # 자막(ass)은 배너 오버레이 뒤에 얹는다 — 배너가 자막을 가리지 않게
        fc += (f"[{last}]ass={ass_path.name}[out]" if subs
               else f"[{last}]null[out]")

    # -loop 1 로 넣은 배너 PNG는 무한 스트림이라 원본이 끝나도 인코딩이 계속된다.
    # 원본 길이로 명시적으로 끊어준다.
    dur = video_duration(video) if fc else 0.0

    def _cmd(use_gpu):
        enc = _vcodec_args(use_gpu)
        if fc:
            tail = (["-t", f"{dur:.3f}"] if dur > 0 else ["-shortest"])
            return ["ffmpeg", "-y", "-i", str(video)] + inputs + \
                   ["-filter_complex", fc, "-map", "[out]", "-map", "0:a?"] + enc + \
                   ["-c:a", "copy"] + tail + [str(out_video)]
        return ["ffmpeg", "-y", "-i", str(video), "-vf", f"ass={ass_path.name}"] + enc + \
               ["-c:a", "copy", str(out_video)]

    gpu = has_nvenc()
    # libass 필터는 파일명만(작업폴더 cwd로) → 윈도우 드라이브 콜론 이스케이프 회피
    try:
        try:
            subprocess.run(_cmd(gpu), cwd=str(ass_path.parent), check=True)
        except subprocess.CalledProcessError:
            if not gpu:
                raise
            log("NVENC 실패 → libx264로 폴백")
            subprocess.run(_cmd(False), cwd=str(ass_path.parent), check=True)
    finally:
        for tmp in ass_path.parent.glob("_bn_*.png"):   # 배너 중간 산출물 정리
            try:
                tmp.unlink()
            except OSError:
                pass
    log(f"자막 영상: {out_video}")
    return out_video
