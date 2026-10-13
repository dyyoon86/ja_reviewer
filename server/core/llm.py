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


ROOT = Path(__file__).resolve().parents[2]
USAGE_LOG = ROOT / "_로그" / "llm_usage.jsonl"
# 기본 시스템 프롬프트(Claude Code 본체 ~9K + CLAUDE.md/MCP 도구목록 수십K)를 통째로 이걸로 바꾼다.
SYS_PROMPT = ("너는 지시대로만 답하는 영상 리뷰 작문 도우미다. "
              "설명·머리말·코드펜스 없이 요구한 형식으로만 출력한다.")
_cfg_cache = {"mtime": None, "data": {}}


def _cfg():
    """studio_config.json 의 llm_* 키만 쓴다(mtime 캐시). 호출자들이 cfg 를 안 넘기므로 직접 읽는다."""
    f = ROOT / "studio_config.json"
    try:
        mt = f.stat().st_mtime
        if _cfg_cache["mtime"] != mt:
            _cfg_cache["data"] = json.loads(f.read_text(encoding="utf-8"))
            _cfg_cache["mtime"] = mt
    except Exception:
        pass
    return _cfg_cache["data"]


# ─── 안전장치(차단기) ─────────────────────────────────────────────────────────
# 2026-10-12 ja26 섹션2 시각캡션 1편이 450턴 폭주(이미지 떨굼→재독 8회)로 하루 한도의 1/4을 먹었다.
# 같은 일이 다시 나도 "한 편 손해"에서 끝나도록 호출 레이어에서 막는다. 값 0 = 해당 검사 끄기.
BREAKER = ROOT / "_로그" / "llm_breaker.json"
GUARD_DEFAULTS = {
    "llm_guard_daily_usd": 150,      # 오늘 누적 환산 $ 상한(어제 파이프라인 ≈ $1,100, 수정 후 15편 예상 ≈ $75)
    "llm_guard_call_in": 200_000,    # 호출 1회 입력(입력+캐시쓰기+캐시읽기) 상한 — 캡션 60장 실측 24K
    "llm_guard_call_out": 30_000,    # 호출 1회 출력 상한 — 내레이션 실측 3.7K
    "llm_guard_est_in": 150_000,     # 보내기 전 추정 입력 상한(글자/2 + 이미지×400) — 초과면 보내지도 않음
    "llm_guard_max_turns": 2,        # claude -p --max-turns (도구 없으면 1턴이지만 이중 안전)
}
PRICE = {"in": 15, "cw": 18.75, "cr": 1.5, "out": 75}   # Opus 공개 단가 $/M — 환산 참고치


class LLMGuardError(RuntimeError):
    """안전장치가 호출을 막았다. 토큰은 쓰지 않았거나(사전) 더 이상 쓰지 않는다(차단기)."""


def _guard(key):
    v = _cfg().get(key, GUARD_DEFAULTS[key])
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(GUARD_DEFAULTS[key])


def _usd(u):
    return sum(int(u.get(k, 0)) * PRICE[k] / 1e6 for k in PRICE)


def today_usd():
    """_로그/llm_usage.jsonl 에서 오늘 누적 환산 $."""
    if not USAGE_LOG.is_file():
        return 0.0
    today = __import__("datetime").date.today().isoformat()
    tot = 0.0
    with open(USAGE_LOG, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith('{"ts": "' + today):
                continue
            try:
                tot += _usd(json.loads(line))
            except Exception:
                pass
    return tot


def _trip(reason, detail, log=print):
    """차단기 내림 — 이후 모든 run_claude 가 CLI 를 부르기 전에 즉시 실패한다."""
    try:
        BREAKER.parent.mkdir(parents=True, exist_ok=True)
        BREAKER.write_text(json.dumps({
            "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "reason": reason, "detail": detail}, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    msg = (f"★LLM 차단기 내림: {reason} — {detail}\n"
           f"   원인 확인 후 해제: .venv\\Scripts\\python.exe tools\\llm_usage.py --reset")
    log(msg)
    raise LLMGuardError(msg)


def breaker_status():
    """None(정상) 또는 차단 기록 dict."""
    if not BREAKER.is_file():
        return None
    try:
        return json.loads(BREAKER.read_text(encoding="utf-8"))
    except Exception:
        return {"reason": "파일 깨짐", "detail": str(BREAKER)}


def reset_breaker():
    try:
        BREAKER.unlink()
        return True
    except FileNotFoundError:
        return False


def _precheck(est_in, log):
    b = breaker_status()
    if b:
        raise LLMGuardError(f"★LLM 차단기 내려가 있음({b.get('ts', '')}): {b.get('reason')} — {b.get('detail')}\n"
                            f"   해제: .venv\\Scripts\\python.exe tools\\llm_usage.py --reset")
    cap = _guard("llm_guard_est_in")
    if cap and est_in > cap:
        raise LLMGuardError(f"★LLM 호출 거부(보내기 전): 추정 입력 {est_in:,} 토큰 > 상한 {cap:,.0f} "
                            f"(config llm_guard_est_in) — 프롬프트/프레임 수를 줄여라")
    cap = _guard("llm_guard_daily_usd")
    if cap:
        spent = today_usd()
        if spent >= cap:
            _trip("오늘 누적 한도 초과", f"환산 ${spent:,.0f} ≥ 상한 ${cap:,.0f} (config llm_guard_daily_usd)", log)


def _postcheck(usage, tag, log):
    tin = sum(int(usage.get(k, 0)) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    out = int(usage.get("output_tokens", 0))
    cap = _guard("llm_guard_call_in")
    if cap and tin > cap:
        _trip("호출 1회 입력 폭주", f"[{tag}] 입력 {tin:,} > 상한 {cap:,.0f} (config llm_guard_call_in)", log)
    cap = _guard("llm_guard_call_out")
    if cap and out > cap:
        _trip("호출 1회 출력 폭주", f"[{tag}] 출력 {out:,} > 상한 {cap:,.0f} (config llm_guard_call_out)", log)


def claude_args(model=None, effort=None, system=None, stream=False):
    """★격리 `claude -p` 인자. 2026-10-12 토큰 실측(ja26): 격리 없는 호출은 매번 22~57K 토큰
    (CLAUDE.md·훅·MCP 도구목록)이 시스템 프롬프트로 들어갔고, `--system-prompt` 로 교체하면 761 토큰.
      --setting-sources ""      사용자/프로젝트 설정·CLAUDE.md·훅 전부 미로드
      --strict-mcp-config       ~/.claude.json 의 MCP 서버(coplay·figma… 수십K 도구 스키마) 미로드
      --disable-slash-commands  스킬 목록 미주입
      --tools ""                내장 도구 없음(이미지는 stream-json 으로 인라인 전달)
      --system-prompt           기본 대형 프롬프트를 한 줄로 교체(append 아님)
      --effort                  사고(thinking) 토큰 상한 — 출력 토큰의 90%가 사고였다(config llm_effort)
    `--bare` 는 OAuth 를 막으므로 못 쓴다."""
    c = _cfg()
    a = [_cli_path("claude"), "-p",
         "--output-format", "stream-json" if stream else "json",
         "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
         "--tools", "", "--system-prompt", system or SYS_PROMPT]
    if stream:
        a += ["--input-format", "stream-json", "--verbose"]
    model = model or c.get("llm_model")
    if model:
        a += ["--model", str(model)]
    effort = effort or c.get("llm_effort")
    if effort:
        a += ["--effort", str(effort)]
    mt = int(_guard("llm_guard_max_turns"))
    if mt > 0:
        a += ["--max-turns", str(mt)]
    return a


def _log_usage(tag, usage, model, effort, secs, prompt_chars):
    try:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
               "tag": tag, "model": model or "", "effort": effort or "", "secs": round(secs, 1),
               "prompt_chars": prompt_chars,
               "in": usage.get("input_tokens", 0), "cw": usage.get("cache_creation_input_tokens", 0),
               "cr": usage.get("cache_read_input_tokens", 0), "out": usage.get("output_tokens", 0)}
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def run_claude(prompt=None, *, content=None, model=None, effort=None, system=None,
               timeout=900, log=print, tag=""):
    """격리된 claude -p 한 번. `prompt`(문자열, stdin) 또는 `content`(텍스트/이미지 블록 리스트 —
    stream-json 으로 한 턴에 인라인 전달; 도구 Read 로 한 장씩 읽히던 방식은 60장이면 100턴·
    프레임당 컨텍스트 재독이라 호출당 170K 캐시쓰기+500K 캐시읽기였다).
    반환 (text, usage). 빈 응답·is_error 는 RuntimeError."""
    import time
    stream = content is not None
    c = _cfg()
    model = model or c.get("llm_model")
    effort = effort or c.get("llm_effort")
    args = claude_args(model=model, effort=effort, system=system, stream=stream)
    if stream:
        inp = json.dumps({"type": "user", "message": {"role": "user", "content": content}},
                         ensure_ascii=False) + "\n"
        pchars = sum(len(b.get("text", "")) for b in content if b.get("type") == "text")
        n_img = sum(1 for b in content if b.get("type") == "image")
    else:
        inp = prompt
        pchars = len(prompt or "")
        n_img = 0
    # ★안전장치: 차단기·사전 추정 크기·오늘 누적 한도. 통과 못 하면 CLI 를 부르지 않는다.
    _precheck(len(system or SYS_PROMPT) // 2 + pchars // 2 + n_img * 400, log)
    env = dict(os.environ, DISABLE_OMC="1")
    t0 = time.time()
    p = subprocess.run(args, input=inp, timeout=timeout, text=True, env=env,
                       encoding="utf-8", errors="replace", capture_output=True)
    secs = time.time() - t0
    out = (p.stdout or "").strip()
    res = None
    for line in out.splitlines()[::-1]:          # stream-json 이면 마지막 result 이벤트
        try:
            o = json.loads(line)
        except Exception:
            continue
        if isinstance(o, dict) and o.get("type") == "result":
            res = o
            break
    if res is None:
        try:
            res = json.loads(out)
        except Exception:
            res = {}
    text = str(res.get("result") or "")
    usage = res.get("usage") or {}
    _log_usage(tag, usage, model, effort, secs, pchars)
    if usage:
        log(f"  LLM 토큰: 출력 {usage.get('output_tokens', 0):,} / 입력 "
            f"{usage.get('input_tokens', 0) + usage.get('cache_creation_input_tokens', 0) + usage.get('cache_read_input_tokens', 0):,}"
            f" ({secs:.0f}s)")
    _postcheck(usage, tag, log)       # ★호출 1회 폭주면 차단기 내림(이번 결과는 버린다)
    if res.get("is_error") or not text.strip():
        err = (p.stderr or text or out[:300]).strip()
        raise RuntimeError(f"claude 응답 없음(로그인/한도 확인): {err[-240:]}")
    return text, usage


def call_llm(prompt, which="claude", log=print, _retry=1):
    """JSON 파싱 실패(문자열 안 따옴표 미이스케이프 등 — claude 가 15편 중 2편꼴, 2026-10-12)면
    이스케이프 지시를 덧붙여 한 번 더 부른다. 재시도 성공률이 높아(SNOS-378) 폴백보다 싸다."""
    try:
        return _call_llm_once(prompt, which, log)
    except RuntimeError as e:
        if _retry > 0 and "JSON" in str(e):
            log(f"  LLM 응답 JSON 깨짐({str(e)[:80]}) → 1회 재시도")
            hint = ('\n\n★출력은 유효한 JSON 객체 하나만. 문자열 값 안의 큰따옴표(")는 반드시 \\" 로 '
                    '이스케이프하고, 줄바꿈은 \\n 으로. 코드펜스·설명문 금지.')
            return call_llm(prompt + hint, which, log, _retry=_retry - 1)
        raise


def _call_llm_once(prompt, which="claude", log=print):
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
        txt, _ = run_claude(prompt, log=log, tag="call_llm")
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
        try:
            return json.loads(s[i:j + 1])
        except json.JSONDecodeError as e2:
            raise RuntimeError(f"LLM JSON 파싱 실패({e2})") from e2


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



