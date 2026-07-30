#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""내레이션 TTS 화자 선별 — "가끔 이상한 목소리로 나온다"의 해결.

왜 — voicebox(qwen 클로닝)는 **생성마다 화자 유사도 편차가 크다**. 실측(2026-07-30,
같은 프로필·같은 텍스트를 seed만 바꿔 생성):
    유사도 0.815 ~ 0.920 (폭 0.105)
게다가 파이프라인이 seed를 42로 **고정**하고 있었기 때문에, 어떤 문장이 나쁜 draw에
걸리면 재실행해도 영원히 같은 나쁜 목소리가 나왔다(seed 고정 = 결정론적. 실측으로
seed=42 3회가 바이트 단위 동일함을 확인). 즉 seed 고정은 재현성은 주지만 품질은
보장하지 않는다.

해결 — 문장마다 seed를 바꿔 후보 N개를 만들고, **기준 임베딩에 가장 가까운 것**을
자동 채택한다. 같은 텍스트끼리 비교하므로 내용 차이가 상쇄되고 화자 차이만 남는다
(짧은 클립에서 resemblyzer 임베딩은 내용/프로소디에 크게 흔들려서, 서로 다른 문장
사이의 절대 유사도나 medoid로는 이상치를 가릴 수 없다 — 실측으로 확인).

★ ja_reviewer venv에는 torch가 없다 → bgm.py와 동일하게 **시스템 파이썬에 설치된
  resemblyzer를 외부 프로세스로 호출**한다. voice_python 설정으로 변경 가능.
  미설치/실패 시 조용히 None을 돌려 호출측이 선별 없이 진행하게 한다(파이프라인 중단 금지).
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from .bgm import _pythons

SCORE_TIMEOUT = 900

# 시스템 파이썬에서 돌아가는 채점 스크립트 — resemblyzer/torch/numpy만 쓴다.
# stdin: {"ref": "ref.npy"|null, "ref_wavs": [...], "cands": [...], "save_ref": "path"|null}
# stdout: {"scores": {path: cos}, "ref_saved": bool}
_SCORER = r'''
import json, sys
import numpy as np
from resemblyzer import VoiceEncoder, preprocess_wav
from pathlib import Path

req = json.loads(sys.stdin.read())
enc = VoiceEncoder(verbose=False)

def emb(p):
    return enc.embed_utterance(preprocess_wav(Path(p)))

def cos(a, b):
    n = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / n) if n else 0.0

ref = None
if req.get("ref") and Path(req["ref"]).is_file():
    ref = np.load(req["ref"])
elif req.get("ref_wavs"):
    E = []
    for w in req["ref_wavs"]:
        try:
            E.append(emb(w))
        except Exception:
            pass
    if E:
        E = np.array(E)
        m = E.mean(axis=0)
        # 하위 25%(나쁜 draw로 추정)를 떼고 다시 평균 — '좋은 목소리' 중심을 잡는다
        if len(E) >= 8:
            s = np.array([cos(e, m) for e in E])
            keep = E[s >= np.percentile(s, 25)]
            if len(keep) >= 4:
                m = keep.mean(axis=0)
        ref = m

out = {"scores": {}, "ref_saved": False}
if ref is not None:
    for c in req.get("cands", []):
        try:
            out["scores"][c] = cos(emb(c), ref)
        except Exception:
            pass
    if req.get("save_ref"):
        np.save(req["save_ref"], ref)
        out["ref_saved"] = True
json.dump(out, sys.stdout)
'''


def find_python(cfg_python=None):
    """resemblyzer를 돌릴 수 있는 파이썬 경로. 없으면 None."""
    for py in _pythons(cfg_python):
        try:
            r = subprocess.run([py, "-c", "import resemblyzer, numpy"],
                               capture_output=True, timeout=120)
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return None


def _run(py, req):
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "score.py"
        sf.write_text(_SCORER, encoding="utf-8")
        r = subprocess.run([py, str(sf)], input=json.dumps(req), text=True,
                           encoding="utf-8", errors="replace",
                           capture_output=True, timeout=SCORE_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "")[-300:])
    return json.loads(r.stdout.strip() or "{}")


def build_ref(ref_wavs, out_npy, python=None, log=print):
    """정상 판정된 내레이션 wav들의 평균 임베딩을 만들어 out_npy에 저장.
    하위 25%를 떼고 재평균해 나쁜 draw의 영향을 줄인다. 성공 시 경로, 실패 시 None."""
    py = find_python(python)
    if not py:
        log("※ resemblyzer 있는 파이썬을 못 찾음 — 화자 선별 비활성")
        return None
    wavs = [str(w) for w in ref_wavs if Path(w).is_file()]
    if len(wavs) < 4:
        log(f"※ 기준 wav가 {len(wavs)}개뿐 — 화자 선별 비활성(4개 이상 필요)")
        return None
    Path(out_npy).parent.mkdir(parents=True, exist_ok=True)
    try:
        res = _run(py, {"ref_wavs": wavs, "cands": [], "save_ref": str(out_npy)})
    except Exception as e:
        log(f"※ 기준 임베딩 생성 실패({e}) — 화자 선별 비활성")
        return None
    if not res.get("ref_saved"):
        log("※ 기준 임베딩 저장 실패 — 화자 선별 비활성")
        return None
    log(f"화자 기준 임베딩 생성: {out_npy} (표본 {len(wavs)}개)")
    return str(out_npy)


def score(cands, ref_npy, python=None, log=print):
    """후보 wav들을 기준 임베딩 대비 코사인 유사도로 채점 → {path: score}.
    실패하면 빈 dict (호출측은 선별 없이 첫 후보를 쓰면 된다)."""
    py = find_python(python)
    if not py or not ref_npy or not Path(ref_npy).is_file():
        return {}
    try:
        return _run(py, {"ref": str(ref_npy),
                         "cands": [str(c) for c in cands]}).get("scores", {})
    except Exception as e:
        log(f"※ 화자 채점 실패({e}) — 선별 없이 진행")
        return {}
