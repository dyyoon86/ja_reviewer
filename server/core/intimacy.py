#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""3️⃣ 의미 — CLIP zero-shot으로 '스킨십(애무·키스) 장면'을 찾는다.

왜 필요한가 — NN·STT 둘 다 못 잡는 사각지대(2026-07-13 FNS-235 실측):
  어두운 조명에서 옷을 입은 채 진행되는 애무씬은
  · NN(NudeNet): 노출 부위가 없어 EXPOSED 0 → 완전 사각
  · STT(moan): "動いていい?" 같은 속삭임 대사가 보호 버블을 만든다
  CLIP은 부위·대사가 아니라 **장면의 의미**("두 사람이 키스한다" vs "식탁에서
  대화한다")를 보므로 정확히 이 틈을 메운다.

방식: 프레임(2초 간격, 320px) → CLIP 이미지 임베딩 → 스킨십/일상 프롬프트와
  코사인 유사도 margin(si-sn) → 이동평균 스무딩 → 임계 이상이 min_dur초
  지속되면 스킨십 구간. '지속' 조건이 핵심 — 파티 장면의 1~2프레임 스파이크
  (쓰러진 사람 부축 등)는 고립돼 있어 걸러지고, 애무는 수십 초 이어진다.

검증(step2_stt.mp4 정답지): 애무 158~238s 한 스팬으로 검출, 식사대화 오탐 0.
  softmax 확률 방식은 노출 의상 파티 장면까지 다 끌어올려 실패 → margin 채택.

모델: Xenova/clip-vit-base-patch32 ONNX 양자화(vision 85M+text 62M+tokenizer).
  onnxruntime CPU로 26fps — 2시간 영상 ≈ 2.5분. 완전 로컬, 프레임 외부 유출 없음.
  models/clip/에 없으면 최초 1회 HuggingFace에서 자동 다운로드(~150MB).
"""
import glob
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "clip"
_HF = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main"
_FILES = {
    "vision_model_quantized.onnx": f"{_HF}/onnx/vision_model_quantized.onnx",
    "text_model_quantized.onnx": f"{_HF}/onnx/text_model_quantized.onnx",
    "tokenizer.json": f"{_HF}/tokenizer.json",
}

# 스킨십(잡는 쪽) — 애무·키스·정사의 시각적 변주들
PROMPTS_INTIMATE = [
    "two people kissing passionately",
    "a couple making out on a couch",
    "a man and a woman being sexually intimate",
    "a man touching and groping a woman's body",
    "foreplay scene, caressing and undressing",
    "people having sex",
    "a couple embracing closely in a dim room",
]
# 일상(놓아주는 쪽) — 이 작품군에서 애무와 헷갈리기 쉬운 장면들을 명시적으로 커버:
# 노출 의상 파티, 취해서 쓰러짐/부축, 인터뷰. margin 방식이라 이쪽이 이기면 통과.
PROMPTS_NEUTRAL = [
    "people sitting and talking at a party",
    "friends eating and drinking at a table",
    "a woman being interviewed",
    "a person standing in a room talking",
    "people playing a card game",
    "an empty room",
    "a drunk person lying down while others help",
    "people toasting drinks at a house party in dim lighting",
    "a person sleeping on the floor",
]

DEFAULT_STEP = 2.0        # 프레임 샘플 간격(초)
DEFAULT_THRESHOLD = 0.02  # 스무딩된 margin 임계 (실측: 애무 0.027~0.046 / 대화 -0.004~+0.014)
DEFAULT_MIN_DUR = 14.0    # 이 시간 이상 지속돼야 스킨십 구간 (고립 스파이크 오탐 제거)
SMOOTH_SEC = 14.0         # 이동평균 폭(초)

_SESS = None  # (vision, text_embeds_intimate, text_embeds_neutral)


def _ensure_models(log=print):
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in _FILES.items():
        fp = MODEL_DIR / name
        if fp.is_file() and fp.stat().st_size > 0:
            continue
        log(f"CLIP 모델 다운로드: {name} …")
        tmp = fp.with_suffix(fp.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(fp)
        log(f"  ✔ {name} ({fp.stat().st_size / 1e6:.0f}MB)")


def _load(log=print):
    """vision 세션 + 정규화된 텍스트 임베딩(프롬프트는 고정이라 1회 계산)."""
    global _SESS
    if _SESS is not None:
        return _SESS
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer
    _ensure_models(log)
    vis = ort.InferenceSession(str(MODEL_DIR / "vision_model_quantized.onnx"),
                               providers=["CPUExecutionProvider"])
    txt = ort.InferenceSession(str(MODEL_DIR / "text_model_quantized.onnx"),
                               providers=["CPUExecutionProvider"])
    tok = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))

    def embed(prompts):
        encs = [tok.encode(p) for p in prompts]
        L = max(len(e.ids) for e in encs)
        ids = np.array([e.ids + [0] * (L - len(e.ids)) for e in encs], dtype=np.int64)
        out = txt.run(None, {"input_ids": ids})[0]
        return out / np.linalg.norm(out, axis=1, keepdims=True)

    _SESS = (vis, embed(PROMPTS_INTIMATE), embed(PROMPTS_NEUTRAL))
    return _SESS


_MEAN = (0.48145466, 0.4578275, 0.40821073)
_STD = (0.26862954, 0.26130258, 0.27577711)


def _preprocess(files):
    import cv2
    import numpy as np
    mean = np.array(_MEAN, dtype=np.float32)
    std = np.array(_STD, dtype=np.float32)
    batch = []
    for f in files:
        img = cv2.imread(f)
        if img is None:
            continue
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        h, w = img.shape[:2]
        s = 224 / min(h, w)
        img = cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
        y, x = (h - 224) // 2, (w - 224) // 2
        img = img[y:y + 224, x:x + 224].astype(np.float32) / 255.0
        img = (img - mean) / std
        batch.append(img.transpose(2, 0, 1))
    return np.stack(batch) if batch else None


def scan_intimacy(video, step=DEFAULT_STEP, threshold=DEFAULT_THRESHOLD,
                  min_dur=DEFAULT_MIN_DUR, pad=2.0, merge_gap=10.0,
                  log=print, progress=None, duration=None):
    """영상 전 구간을 CLIP으로 훑어 스킨십 구간을 돌려준다. 반환: [(a, b), ...]
    진행률: 프레임 추출 0~40%, 추론 40~100%."""
    import numpy as np
    vis, ti, tn = _load(log)
    log(f"스킨십 장면 스캔(CLIP, {step:g}s 간격) — 화면의 '의미'로 판정")
    ts, margins = [], []
    with tempfile.TemporaryDirectory() as td:
        p = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
             "-i", str(video), "-vf", f"fps={1.0 / step:g},scale=320:-1",
             os.path.join(td, "f%05d.jpg")],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for line in p.stdout:
            if duration and progress and line.startswith("out_time_ms="):
                try:
                    sec = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                progress(min(0.40, sec / duration * 0.40))
        p.wait(timeout=3600)
        files = sorted(glob.glob(os.path.join(td, "*.jpg")))
        if not files:
            return []
        log(f"   프레임 {len(files)}장 → CLIP 판정 시작")
        B = 16
        for i in range(0, len(files), B):
            px = _preprocess(files[i:i + B])
            if px is None:
                continue
            emb = vis.run(None, {"pixel_values": px})[0]
            emb = emb / np.linalg.norm(emb, axis=1, keepdims=True)
            si = (emb @ ti.T).max(axis=1)
            sn = (emb @ tn.T).max(axis=1)
            for k in range(len(px)):
                ts.append((i + k) * step)
                margins.append(float(si[k] - sn[k]))
            if progress:
                progress(0.40 + 0.60 * min(1.0, (i + B) / len(files)))
            if (i // B) % 8 == 0:
                log(f"   CLIP 판정 {min(i + B, len(files))}/{len(files)}장")

    # 이동평균 스무딩 → 임계 이상이 min_dur 지속되는 스팬만 채택
    k = max(1, int(SMOOTH_SEC / step))
    sm = np.convolve(np.array(margins), np.ones(k) / k, mode="same")
    spans, cur = [], None
    for t, v in zip(ts, sm):
        if v >= threshold:
            cur = [t, t] if cur is None else [cur[0], t]
        elif cur:
            spans.append(cur); cur = None
    if cur:
        spans.append(cur)
    end = duration or (ts[-1] + step if ts else 0.0)
    spans = [(max(0.0, a - pad), min(end, b + step + pad))
             for a, b in spans if b + step - a >= min_dur]

    # 근접 병합
    merged = []
    for a, b in spans:
        if merged and a - merged[-1][1] <= merge_gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    total = sum(b - a for a, b in merged)
    log(f"스킨십 구간: {len(merged)}개 / {total / 60:.1f}분 "
        f"(임계 {threshold}, 지속 {min_dur:g}s 이상)")
    return merged
