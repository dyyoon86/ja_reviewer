#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""공용 유틸 — 시간/SRT 파싱, 자막 분할·정상화, keep/retime, ffprobe 조회."""
import os
import re
import subprocess
from pathlib import Path

# 서브프로세스 타임아웃(초) — 멈춘 ffmpeg가 워커를 영원히 막는 것 방지.
FFPROBE_TIMEOUT = 60        # 프로브·인코더목록 등 짧은 조회
FFMPEG_TIMEOUT = 3600       # 실제 인코딩(긴 영상도 1시간이면 충분)


def _part_path(out_path):
    """최종 경로 옆의 임시 경로(.part). 확장자 유지(ffmpeg가 컨테이너를 확장자로 판단)."""
    p = Path(out_path)
    return str(p.with_name(p.name + ".part" + p.suffix))


def _finalize(tmp, out_path):
    """성공한 임시 산출물을 최종 경로로 원자적 이동(같은 폴더 → os.replace는 원자적).
    중간에 죽으면 .part만 남아 '완료(파일 존재)'로 오판되지 않는다."""
    os.replace(tmp, str(out_path))

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
                                       "format=duration", "-of", "csv=p=0", str(path)],
                                      timeout=FFPROBE_TIMEOUT)
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


def video_wh(path):
    try:
        out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                       "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
                                      timeout=FFPROBE_TIMEOUT)
        w, h = out.decode().strip().split("x")
        return int(w), int(h)
    except Exception:
        return 1920, 1080




def invalidate_derived(outdir, code, log=print):
    """②AI 처리(또는 구간 재선정)가 최종컷(final.mp4)을 새로 만들면 이전 컨셉의
    파생물(음성본·자막굽기본·TTS 조각)은 전부 구버전이다 — 남겨두면 미리보기와
    ⑥굽기가 옛것을 집어간다(실사례 2026-07-13: 요약형 뽑고 하이라이트형으로 재생성
    했는데 미리보기가 이전 컨셉의 _voiced만 계속 표시). 새 컷 기준으로 다시
    만들도록 지운다. TTS 조각(nNNN.wav)은 줄 번호로 재사용될 수 있어 특히 위험."""
    import shutil
    from pathlib import Path
    outdir = Path(outdir)
    for stale in (outdir / f"{code}_final_voiced.mp4",
                  outdir / f"{code}_final_subbed.mp4"):
        if stale.is_file():
            try:
                stale.unlink()
            except OSError as e:
                log(f"※ 구버전 파생물 삭제 실패({stale.name}): {e}")
                continue
            log(f"이전 컨셉 파생물 삭제: {stale.name} (새 컷 기준으로 다시 생성)")
    tts = outdir / f"{code}_tts"
    if tts.is_dir():
        shutil.rmtree(tts, ignore_errors=True)
        log(f"이전 내레이션 음성 조각 삭제: {tts.name}/")


def snap_keep_to_lines(keep, segs, total=None, pad=0.15, max_snap=1.5, log=print):
    """keep 경계를 전사 대사(줄) 경계로 스냅하고 앞뒤에 패딩을 준다.

    왜 — LLM이 준 keep 시각은 전사 타임스탬프에서 유도되지만 그대로 자르면
    말 중간에서 끊긴다("…했습ㄴ"). 전사 타임스탬프 자체도 50~100ms 흔들린다.
      · 경계가 어떤 대사 줄 **안쪽**에 있으면 → 그 줄을 통째로 포함하는 쪽으로 스냅
        (단 max_snap 이내일 때. 그보다 멀면 그 줄을 버리는 쪽으로 스냅해서 구간이
         과하게 늘어나는 것을 막는다)
      · 스냅 후 앞뒤 pad(기본 150ms)를 더한다 — 타임스탬프 드리프트 흡수
    겹치게 된 구간은 병합한다. 전사 줄이 없으면 패딩만 적용."""
    keep = [(float(a), float(b)) for a, b in sorted(keep) if float(b) > float(a)]
    if not keep:
        return keep
    lines = sorted((float(s), float(e)) for s, e, *_ in (segs or []))

    def snap_start(a):
        for s, e in lines:
            if s < a < e:                      # 줄 한가운데서 시작 → 줄 시작으로 당김
                return s if (a - s) <= max_snap else e
        return a

    def snap_end(b):
        for s, e in lines:
            if s < b < e:                      # 줄 한가운데서 끝 → 줄 끝까지 밀어줌
                return e if (e - b) <= max_snap else s
        return b

    out = []
    for a, b in keep:
        a2, b2 = snap_start(a), snap_end(b)
        # ★ 과도 축소 가드 — 긴 대사 줄이 양 경계에 걸리면 둘 다 '버리는' 쪽으로 스냅돼
        #   구간이 뭉텅 사라진다(실측: 4.0s keep → 0.5s). 절반 넘게 줄어들면 스냅을
        #   포기하고 원본 경계를 쓴다(패딩만 적용) — LLM이 고른 분량을 지키는 게 우선.
        if b2 <= a2 or (b2 - a2) < 0.5 * (b - a):
            a2, b2 = a, b
        a2 = max(0.0, a2 - pad)
        b2 = b2 + pad
        if total:
            b2 = min(float(total), b2)
        if b2 - a2 > 0.05:
            out.append((a2, b2))
    # 패딩으로 맞닿거나 겹친 구간 병합
    merged = []
    for a, b in out:
        if merged and a <= merged[-1][1] + 0.02:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    before = sum(b - a for a, b in keep)
    after = sum(b - a for a, b in merged)
    log(f"컷 경계 정리: {len(keep)}→{len(merged)}구간, {before:.1f}s→{after:.1f}s "
        f"(대사 경계 스냅 + 앞뒤 {pad * 1000:.0f}ms 패딩 — 말 중간에서 안 끊기게)")
    return merged
