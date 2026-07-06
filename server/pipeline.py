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


def _wrap_chunks(text, maxlen=24):
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


def split_entries(entries, maxlen=24):
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


def write_srt(entries, path, maxlen=24):
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


def transcribe(video, model_name="large-v3", log=print, initial_prompt=None):
    """
    고도화 전사. initial_prompt(작품 제목·배우명 등 맥락)를 주면 정확도↑.
    환청 억제 파라미터 + 후처리 필터로 신음/무음발 가짜자막을 걸러낸다.
    """
    log(f"Whisper 전사 (모델 {model_name})...")
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
    out, dropped = [], 0
    for s in segs:
        t = (s.text or "").strip()
        if not t:
            continue
        if _looks_hallucinated(t):
            dropped += 1
            continue
        out.append((float(s.start), float(s.end), t))
        if out and len(out) % 50 == 0:
            log(f"   …{len(out)}")
    out = sanitize_segments(out)   # 타임스탬프 역전/겹침/순서 정상화
    log(f"전사 완료: {len(out)} 세그먼트 (환청/무의미 {dropped}개 제거)")
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
            "너는 일본 성인영상 자막 검수·번역 전문가다. 아래는 Whisper가 뽑은 일본어 전사(줄마다 '번호<TAB>일본어').\n"
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
            "각 대사에 speaker 지정 — '여'(여배우)/'남'(남배우). 화자가 바뀌면 색으로 구분됨. "
            "[자막 길이] 대사·내레이션의 각 항목 텍스트는 25자 이내로, 길면 의미 단위(절·구)로 끊어 여러 항목으로 나눠라. "
            "[내레이션 유형] 각 내레이션 항목에 style을 지정 — '기본'(일반 해설), '강조'(핵심·펀치라인·반전·리액션), "
            "'정보'(배우 스펙·수치·레이블·메타 등 정보성 자막). 리뷰 채널처럼 유형을 적절히 섞어 리듬감 있게.\n"
            + _translate())


def _hint_block(hint):
    h = (hint or "").strip()
    return f"[사용자 추가 지시 — 최우선 반영]\n{h}\n" if h else ""


def prompt_auto(meta, segs, target_sec=60, hint=""):
    body = "\n".join(f"{k}\t{a:.2f}\t{b:.2f}\t{t}" for k, (a, b, t) in enumerate(segs, 1))
    return (f"너는 딸딸기튜브 AV 해설영상 작가다. 아래 작품의 일본어 자막을 보고 '스토리 핵심만' 골라 "
            f"**약 {target_sec}초 내외 하이라이트 영상**으로 압축하고, 한글 대사자막과 해설 내레이션을 만든다.\n"
            f"{_hint_block(hint)}"
            f"[메타]\n{_meta_block(meta)}\n[일본어자막] 번호\\t시작초\\t끝초\\t대사\n{body}\n{_style()}\n"
            f"[규칙] (1)신음·짧은탄성·반복감탄·비스토리 섹스대사·무음/잡담·중복은 버린다. "
            f"(2)스토리(설정·관계·전환·갈등·결말)를 드러내는 핵심 구간만 keep으로 골라 **합쳐서 {target_sec}초 ±20% 목표**. "
            f"(3)도입~결말 흐름이 보이게 고루 분포. 시간은 원본 영상 기준 초.\n"
            f"[출력 JSON만] {{\"summary\":\"3~5줄\",\"stars\":1~5,\"keep\":[[시작,끝],...],"
            f"\"dialogue\":[{{\"start\":초,\"end\":초,\"ko\":\"\",\"speaker\":\"여|남\"}}],\"narration\":[{{\"start\":초,\"end\":초,\"text\":\"\",\"style\":\"기본|강조|정보\"}}]}}")


def prompt_manual(meta, segs, target_sec=60, hint=""):
    body = "\n".join(f"{k}\t{a:.2f}\t{b:.2f}\t{t}" for k, (a, b, t) in enumerate(segs, 1))
    return (f"너는 딸딸기튜브 AV 해설영상 작가다. 아래는 '정사장면을 이미 제거한' 영상의 일본어 자막이다. "
            f"여기서 **스토리 핵심만 골라 약 {target_sec}초 내외로 압축**하고, 한글 대사자막과 해설 내레이션을 만든다.\n"
            f"{_hint_block(hint)}"
            f"[메타]\n{_meta_block(meta)}\n[일본어자막] 번호\\t시작초\\t끝초\\t대사\n{body}\n{_style()}\n"
            f"[규칙] (1)무음·잡담·반복·의미없는 짧은 라인은 버린다. "
            f"(2)스토리(설정·관계·전환·갈등·결말)를 드러내는 핵심 구간만 keep으로 골라 **합쳐서 {target_sec}초 ±20% 목표**. "
            f"(3)정사 선별은 하지 말 것(이미 제거됨). 시간은 이 자막 기준 초.\n"
            f"[출력 JSON만] {{\"summary\":\"3~5줄\",\"stars\":1~5,\"keep\":[[시작,끝],...],"
            f"\"dialogue\":[{{\"start\":초,\"end\":초,\"ko\":\"\",\"speaker\":\"여|남\"}}],\"narration\":[{{\"start\":초,\"end\":초,\"text\":\"\",\"style\":\"기본|강조|정보\"}}]}}")


# ─── ④ 컷 / 재타이밍 ─────────────────────────────────────────────────────────
def retime(entries, keep, snap=False, default_dur=4.0):
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
STYLE_DEFAULT = {
    "dialogue":  {"font": "Malgun Gothic", "size": 42, "color": "#FFFFFF", "outline_color": "#000000",
                  "outline": 2.2, "shadow": 0.4, "bold": True, "v": "bottom", "h": "center", "margin": 46},
    "dialogue_m": {"font": "Malgun Gothic", "size": 42, "color": "#7FD0FF", "outline_color": "#000000",
                   "outline": 2.2, "shadow": 0.4, "bold": True, "v": "bottom", "h": "center", "margin": 46},
    "narration": {"font": "Malgun Gothic", "size": 38, "color": "#FFD400", "outline_color": "#000000",
                  "outline": 2.2, "shadow": 0.4, "bold": True, "v": "top", "h": "center", "margin": 40},
    "emphasis":  {"font": "Malgun Gothic", "size": 52, "color": "#FF3B3B", "outline_color": "#000000",
                  "outline": 2.8, "shadow": 0.6, "bold": True, "v": "middle", "h": "center", "margin": 60},
    "info":      {"font": "Malgun Gothic", "size": 32, "color": "#8FE3FF", "outline_color": "#00243A",
                  "outline": 2.0, "shadow": 0.3, "bold": True, "v": "top", "h": "right", "margin": 30},
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


def build_ass(dialogue, narration, out_ass, width, height, styles=None):
    styles = styles or {}
    S = {k: {**STYLE_DEFAULT[k], **(styles.get(k) or {})}
         for k in ("dialogue", "dialogue_m", "narration", "emphasis", "info")}
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
        L.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},{style},,0,0,0,,{txt}")
    Path(out_ass).write_text("\n".join(L) + "\n", encoding="utf-8")
    return out_ass


def burn_subs(video, dialogue_srt, narration_srt, out_video, styles=None,
              narration_json=None, dialogue_json=None, log=print):
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
    if not dlg and not nar:
        raise RuntimeError("입힐 자막(SRT)이 없습니다.")
    ass_path = Path(out_video).with_suffix(".ass")
    build_ass(dlg, nar, str(ass_path), w, h, styles)
    log(f"자막 굽기 (ffmpeg ass, {w}x{h}, 대사 {len(dlg)} · 내레이션 {len(nar)})...")
    # libass 필터는 파일명만(작업폴더 cwd로) → 윈도우 드라이브 콜론 이스케이프 회피
    subprocess.run(["ffmpeg", "-y", "-i", str(video), "-vf", f"ass={ass_path.name}",
                    "-c:a", "copy", str(out_video)], cwd=str(ass_path.parent), check=True)
    log(f"자막 영상: {out_video}")
    return out_video
