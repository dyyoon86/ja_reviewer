#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""① Whisper 전사 — 환청 억제 + CUDA DLL 경로 + Claude 검증."""
from .common import sanitize_segments, clamp_durations
from .llm import call_llm
from .prompts import _meta_block

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


def _whisper_kwargs(beam_size=5, initial_prompt=None):
    """환청 억제 공통 파라미터 — 순차/배치 양쪽에서 동일하게 쓴다."""
    return dict(
        language="ja",
        task="transcribe",
        beam_size=beam_size,
        temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],   # 실패 시 온도 폴백
        condition_on_previous_text=False,             # ★ 신음→직전텍스트 반복 폭주 차단(핵심)
        compression_ratio_threshold=2.4,              # 반복 텍스트 세그 폐기
        log_prob_threshold=-1.0,                      # 저확신 세그 폐기
        no_speech_threshold=0.6,                      # 무음/비음성 컷
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200, threshold=0.5),
        initial_prompt=initial_prompt or None,
    )


def _collect(segs, dur, log, progress):
    """지연 생성 세그먼트를 소비하며 환청 필터 + 진행률 보고."""
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
    return out, dropped


def _finish(out, dropped, log):
    out = sanitize_segments(out)   # 타임스탬프 역전/겹침/순서 정상화
    before = out
    out = clamp_durations(out)     # 글자수 대비 과길이 자막 컷(무음/신음 구간 끌림 제거)
    trimmed = sum(1 for (a, b, *_), (a2, b2, *_2) in zip(before, out) if b - b2 > 0.3)
    log(f"전사 완료: {len(out)} 세그먼트 (환청/무의미 {dropped}개 제거, 과길이 자막 {trimmed}개 단축)")
    return out


def transcribe(video, model_name="large-v3", log=print, progress=None, initial_prompt=None,
               beam_size=5, batched=True, batch_size=8):
    """
    고도화 전사. initial_prompt(작품 제목·배우명 등 맥락)를 주면 정확도↑.
    환청 억제 파라미터 + 후처리 필터로 신음/무음발 가짜자막을 걸러낸다.
    progress(frac 0~1) 콜백을 주면 전사 진행률을 보고한다.
    batched=True면 BatchedInferencePipeline(VAD 음성구간 병렬 디코딩)로 4~8배 빠름.
    실패(OOM 등) 시 순차 전사로 자동 폴백.
    """
    log(f"Whisper 전사 (모델 {model_name}{f', 배치×{batch_size}' if batched else ''})...")
    _ensure_cuda_dll_path(log)
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="auto", compute_type="auto")
    kw = _whisper_kwargs(beam_size, initial_prompt)
    if batched:
        try:
            from faster_whisper import BatchedInferencePipeline
            segs, info = BatchedInferencePipeline(model).transcribe(
                str(video), batch_size=batch_size, **kw)
            dur = float(getattr(info, "duration", 0) or 0)
            log(f"모델 로드/오디오 분석 완료 (길이 {dur:.0f}s). 배치 전사 시작…")
            out, dropped = _collect(segs, dur, log, progress)
            return _finish(out, dropped, log)
        except Exception as e:
            log(f"※ 배치 전사 실패({type(e).__name__}: {e}) → 순차 전사로 폴백")
    segs, info = model.transcribe(str(video), **kw)
    dur = float(getattr(info, "duration", 0) or 0)
    log(f"모델 로드/오디오 분석 완료 (길이 {dur:.0f}s). 전사 시작…")
    out, dropped = _collect(segs, dur, log, progress)
    return _finish(out, dropped, log)


def transcribe_scan(video, model_name="small", log=print, progress=None, initial_prompt=None,
                    batch_size=16):
    """1차 러프 스캔 — 구간 '선정'용이라 오탈자 허용. 작은 모델+beam1+배치로 최고 속도.
    최종 자막 품질은 2차 정밀 전사(transcribe_ranges)가 책임진다."""
    log(f"1차 스캔 전사(러프, {model_name}) — 구간 선정용. 최종 자막은 2차 정밀 전사로 확보")
    return transcribe(video, model_name, log, progress, initial_prompt,
                      beam_size=1, batched=True, batch_size=batch_size)


def transcribe_ranges(video, ranges, model_name="large-v3", log=print, progress=None,
                      initial_prompt=None):
    """2차 정밀 전사 — 선정된 keep 구간만 ffmpeg로 오디오 슬라이스해 정밀 전사.
    타임스탬프는 원본 영상 기준 초로 환원해 반환. 2시간짜리도 실제 작업량은 keep 합계(1~3분)뿐."""
    import os
    import subprocess
    import tempfile
    ranges = [(float(a), float(b)) for a, b in ranges if float(b) > float(a)]
    if not ranges:
        return []
    total = sum(b - a for a, b in ranges)
    log(f"2차 정밀 전사({model_name}): {len(ranges)}구간 합계 {total:.0f}s")
    _ensure_cuda_dll_path(log)
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="auto", compute_type="auto")
    kw = _whisper_kwargs(5, initial_prompt)
    out, dropped, done = [], 0, 0.0
    with tempfile.TemporaryDirectory() as td:
        for idx, (a, b) in enumerate(ranges, 1):
            wav = os.path.join(td, f"r{idx:03d}.wav")
            # 슬라이스에 앞뒤 0.3s 패딩 — 경계 단어 잘림 방지(타임스탬프 환원 시 보정)
            pa = max(0.0, a - 0.3)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                            "-ss", f"{pa:.3f}", "-to", f"{b + 0.3:.3f}", "-i", str(video),
                            "-vn", "-ac", "1", "-ar", "16000", wav],
                           check=True, timeout=600)
            segs, _info = model.transcribe(wav, **kw)
            n = 0
            for s in segs:
                t = (s.text or "").strip()
                if not t:
                    continue
                if _looks_hallucinated(t):
                    dropped += 1
                    continue
                out.append((pa + float(s.start), min(b, pa + float(s.end)), t))
                n += 1
            done += b - a
            if progress and total:
                progress(min(0.99, done / total))
            log(f"   구간 {idx}/{len(ranges)} ({a:.0f}~{b:.0f}s): 대사 {n}줄")
    if progress:
        progress(1.0)
    return _finish(out, dropped, log)


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


