#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""②단계(수동) — 전사(small)로 신음·정사 구간을 찾아 삭제 후보로 준다.

왜 필요한가 — NN(NudeNet) 단독의 결정적 한계:
  NN은 화면의 '노출 부위'만 본다. 노출 있는 의상으로 식사하며 대화하는 장면도
  살 노출이 지속되면 정사로 오판한다(실측: FNS-235의 84~123분 39분이 전부
  "이거 진짜 맛있어" "새우 좋아해?" 하는 식사 대화였는데 NN은 정사로 판정).

핵심 규칙 — **대사가 있으면 정사가 아니다**:
  · 정사신 = 신음만 있고 의미 있는 대사가 없다
  · 대화신 = 짧든 길든 실제 대사가 오간다
  그래서 화면(NN)이 아니라 **소리**로 판정한다. small 모델이면 2시간이 1~2분.

판정: 윈도우(기본 30초) 안에서
  · 실대사가 하나라도 있으면      → 보존 (오검출 방지의 핵심)
  · 실대사 0 + 신음/무의미가 있으면 → 정사 구간(삭제 후보)
  · 실대사 0 + 아무 소리도 없으면   → 무음(삭제 후보 — 리뷰에 쓸 게 없다)
"""
import re

# 일본 AV 신음/무의미 발화 패턴. Whisper가 신음을 옮길 때 흔히 나오는 형태들.
MOAN_RE = re.compile(
    r"^[あぁアぃいイぅうウぇえエぉおオんンっッはハひヒふフへヘほホやヤゆユよヨ"
    r"~ー…。、!?！？\s]*$"
)
MOAN_WORDS = ("イク", "いく", "いっちゃ", "気持ちい", "きもちい", "だめ", "ダメ",
              "やば", "ヤバ", "あん", "アン", "はぁ", "ハァ", "んん", "ンン")


def is_moan(text):
    """신음·무의미 발화 판정. 실제 대사면 False."""
    t = (text or "").strip()
    if not t:
        return True
    # 기호/장음/모음·ん 만으로 이뤄진 줄 = 신음
    if MOAN_RE.match(t):
        return True
    # 짧은데 신음 상용구뿐이면 신음(길면 대사일 수 있으니 길이 제한)
    if len(t) <= 8 and any(w in t for w in MOAN_WORDS):
        return True
    # 같은 문자 반복(ああああ, んんん)
    comp = re.sub(r"[\s~ー…。、!?！？]", "", t)
    if len(comp) >= 3 and len(set(comp)) <= 2:
        return True
    return False


def scan_audio(video, model_name="small", log=print, progress=None,
               window=30.0, min_len=10.0, pad=1.0, merge_gap=15.0):
    """영상을 전사해 '실대사 없는' 구간(신음/무음)을 삭제 후보로 돌려준다.
    반환: (ranges, stats) — ranges=[(a,b)], stats={dialogue, moan, silent}

    실대사가 한 줄이라도 있는 윈도우는 절대 삭제하지 않는다 —
    노출 의상으로 대화하는 장면을 NN이 정사로 오판하는 것을 여기서 되돌린다."""
    from .transcribe import transcribe
    from .common import video_duration

    total = video_duration(video) or 0.0
    log(f"신음·정사 구간 스캔 — {model_name} 전사({total / 60:.0f}분)")
    # ★ 배치 전사(BatchedInferencePipeline)는 절대 쓰면 안 된다 — 세그먼트를 뭉쳐버린다.
    #   실측(같은 120초): 순차 42세그 vs 배치 3세그(45초짜리 덩어리). 텍스트는 살아도
    #   타임스탬프가 뭉개져서 '대사가 있는 시각'을 알 수 없고, 대사 있는 구간을
    #   '대사 없음'으로 오판해 과다 삭제한다(실측: 123분 중 102분 삭제).
    #   여기서는 시각 정확도가 전부이므로 순차 전사를 쓴다.
    segs = transcribe(video, model_name, log=log, progress=progress,
                      beam_size=1, batched=False)

    talk = []      # 실대사가 있는 시각(초)
    n_moan = 0
    for a, b, t in segs:
        if is_moan(t):
            n_moan += 1
        else:
            talk.append((float(a), float(b)))

    log(f"전사 {len(segs)}세그 → 실대사 {len(talk)} · 신음/무의미 {n_moan}")
    if not total:
        return [], {"dialogue": len(talk), "moan": n_moan, "silent": 0}

    # 윈도우 단위로 '실대사 없음' 판정
    bad = []
    w = max(5.0, float(window))
    k = 0
    while k * w < total:
        w0, w1 = k * w, min((k + 1) * w, total)
        has_talk = any(a < w1 and b > w0 for a, b in talk)
        if not has_talk:
            bad.append((max(0.0, w0 - pad), min(total, w1 + pad)))
        k += 1

    # 인접 구간 병합 + 너무 짧은 조각 버림
    spans = []
    for a, b in bad:
        if spans and a - spans[-1][1] <= merge_gap:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))
        else:
            spans.append((a, b))
    spans = [(a, b) for a, b in spans if b - a >= min_len]

    cut = sum(b - a for a, b in spans)
    log(f"실대사 없는 구간(신음/무음): {len(spans)}개 / {cut / 60:.1f}분 "
        f"— 대사가 있는 구간은 전부 보존됩니다")
    return spans, {"dialogue": len(talk), "moan": n_moan,
                   "cut_sec": round(cut, 1), "total": round(total, 1)}
