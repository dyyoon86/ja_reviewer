#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""②③ 메타 조회 + LLM CLI(codex/claude) 호출 — 프롬프트는 반드시 stdin."""
import os
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path


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



