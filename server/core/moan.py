#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""②단계(수동) — 전사(small)로 신음·정사 구간을 찾아 삭제 후보로 준다.

왜 필요한가 — NN(NudeNet) 단독의 결정적 한계:
  NN은 화면의 '노출 부위'만 본다. 노출 있는 의상으로 식사하며 대화하는 장면도
  살 노출이 지속되면 정사로 오판한다(실측: FNS-235의 84~123분 39분이 전부
  "이거 진짜 맛있어" "새우 좋아해?" 하는 식사 대화였는데 NN은 정사로 판정).

핵심 규칙 — **내용 있는 대사가 있으면 정사가 아니다**:
  · 정사신 = 신음·흥분 감탄("やばい、やばい" "動いていい?")만 있고 내용 있는 대사가 없다
  · 대화신 = 짧든 길든 실제 대사가 촘촘히 오간다
  그래서 화면(NN)이 아니라 **소리**로 판정한다. small 모델이면 2시간이 1~2분.

★ 30초 윈도우 방식의 구멍 (2026-07-13 FNS-235 클립 실측으로 폐기):
  "윈도우에 실대사 하나라도 있으면 통째 보존" 규칙은
  ① 윈도우 경계에 걸친 무대사 정사 43초를 통과시켰고(양옆 윈도우에 대사가 있었음)
  ② 어두운 조명의 옷 입은 애무씬(NN도 사각)이 속삭임 대사 몇 줄로 89초 전체를 지켰다.
  → **대사 버블 방식**으로 교체: '강한 대사' 주변 ±pad초만 보호하고,
    보호 밖 공백이 min_len초 이상이면 삭제 후보. 대화신은 대사가 촘촘해 버블이
    이어지므로 안 잘리고(식사대화 0컷 실측), 정사신은 대사가 성겨서 뚫린다.
"""
import re

# 일본 AV 신음/무의미 발화 패턴. Whisper가 신음을 옮길 때 흔히 나오는 형태들.
MOAN_RE = re.compile(
    r"^[あぁアぃいイぅうウぇえエぉおオんンっッはハひヒふフへヘほホやヤゆユよヨ"
    r"~ー…。、!?！？\s]*$"
)
MOAN_WORDS = ("イク", "いく", "いっちゃ", "気持ちい", "きもちい", "だめ", "ダメ",
              "やば", "ヤバ", "あん", "アン", "はぁ", "ハァ", "んん", "ンン")

# 정사 중 속삭임에서 나오는 흥분 상용구 — 이런 토큰**만**으로 된 줄은 '내용 있는 대사'가
# 아니다. (실측: "やばい、いいじゃん、やろう" "ほんと?動いていい?" "できるとこまでやろう")
# 일상 대화에서도 나오는 단어지만, 여기 걸려도 주변에 진짜 대사가 촘촘하면 버블이
# 이어져 잘리지 않으므로(보호는 이웃이 해줌) 과감히 넣는 쪽이 안전하다.
EXCITE_WORDS = ("やば", "ヤバ", "だめ", "ダメ", "いい", "イイ", "すご", "スゴ",
                "気持ち", "きもち", "イク", "いく", "イッ", "いっちゃ", "でる", "出る",
                "もっと", "やろう", "やる", "うそ", "ほんと", "エロ", "ちょっと",
                "待って", "まって", "動いて", "うごいて")


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


def _tokens(t):
    return [x for x in re.split(r"[、。,.!?！？\s・…~ー]+", t) if x]


def is_weak(text):
    """보호 버블을 만들지 못하는 발화 — 신음, 흥분 감탄 연발, 같은 말 반복, 짧은 호명.
    약한 발화는 삭제 트리거가 아니라 '보호를 못 할 뿐'이다: 주변에 강한 대사가
    있으면 그 버블 안에서 같이 살아남는다."""
    t = (text or "").strip()
    if is_moan(t):
        return True
    toks = _tokens(t)
    if not toks:
        return True
    # 같은 토큰 반복이 절반 이상 (やばい、やばい、やばい / いいじゃん、いいじゃん)
    if len(toks) >= 2 and len(set(toks)) <= max(1, len(toks) // 2):
        return True
    # 모든 토큰이 흥분 상용구(또는 2자 이하 잔사)뿐 = 내용 있는 문장이 아님
    if all(any(w in tok for w in EXCITE_WORDS) or len(tok) <= 2 for tok in toks):
        return True
    # 아주 짧은 호명/한 단어 (トミー / ね / うん)
    if len(t) <= 4:
        return True
    return False


def scan_audio(video, model_name="small", log=print, progress=None,
               window=None, min_len=8.0, pad=5.0, merge_gap=8.0):
    """영상을 전사해 '내용 있는 대사가 없는' 구간(신음/흥분속삭임/무음)을 삭제 후보로
    돌려준다. 반환: (ranges, stats) — ranges=[(a,b)], stats={dialogue, moan, ...}

    대사 버블 방식: 강한 대사 [시작-pad, 끝+pad]만 보호, 보호 밖 공백 min_len초
    이상이면 삭제 후보. window 인자는 구 30초 윈도우 방식의 잔재로 무시된다(호환용).
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

    strong = []    # 내용 있는 대사 (보호 버블의 씨앗)
    n_weak = 0
    for a, b, t in segs:
        if is_weak(t):
            n_weak += 1
        else:
            strong.append((float(a), float(b)))

    log(f"전사 {len(segs)}세그 → 내용 대사 {len(strong)} · 신음/흥분/무의미 {n_weak}")
    if not total:
        return [], {"dialogue": len(strong), "moan": n_weak, "silent": 0}

    # 보호 버블 = 강한 대사 ±pad, 겹치면 병합
    prot = []
    for a, b in strong:
        s, e = max(0.0, a - pad), min(total, b + pad)
        if prot and s <= prot[-1][1]:
            prot[-1] = (prot[-1][0], max(prot[-1][1], e))
        else:
            prot.append((s, e))

    # 삭제 후보 = 보호 여집합 (min_len 이상) — 영상 시작·끝 공백도 똑같이 잡는다
    #   (구 윈도우 방식이 놓친 '경계에 걸친 무대사 정사 43초'가 바로 이 케이스)
    cuts, cur = [], 0.0
    for s, e in prot:
        if s - cur >= min_len:
            cuts.append((cur, s))
        cur = max(cur, e)
    if total - cur >= min_len:
        cuts.append((cur, total))

    # 근접 병합 — 사이의 짧은 보호 조각을 남겨봐야 컷만 파편화된다
    spans = []
    for a, b in cuts:
        if spans and a - spans[-1][1] <= merge_gap:
            spans[-1] = (spans[-1][0], b)
        else:
            spans.append((a, b))

    cut = sum(b - a for a, b in spans)
    log(f"내용 대사 없는 구간(신음/흥분속삭임/무음): {len(spans)}개 / {cut / 60:.1f}분 "
        f"— 내용 대사 ±{pad:.0f}s 버블은 전부 보존됩니다")
    return spans, {"dialogue": len(strong), "moan": n_weak,
                   "cut_sec": round(cut, 1), "total": round(total, 1)}
