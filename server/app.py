#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ja_reviewer Phase1 — FastAPI + 로컬 웹 UI.

실행:
    pip install fastapi uvicorn faster-whisper
    python -m server.app            # → http://127.0.0.1:8000 (브라우저 자동 오픈)

영상은 업로드가 아니라 '로컬 경로'로 다룬다. <video>는 /video/stream?path= 로 Range 스트리밍.
무거운 작업(전사·LLM·컷)은 백그라운드 잡 + SSE(/events/{job})로 진행상황을 흘린다.
"""
import os
import re
import json
import queue
import uuid
import asyncio
import threading
import webbrowser
import mimetypes
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from . import pipeline as P
from .stages import (Emitter, work_dir, load_state, save_state, steps_status,
                     write_narration, write_dialogue, _hms, _safe,
                     stage_transcribe, stage_ai, stage_subs, stage_tts, stage_burn)
from .queue_mgr import QueueManager

ROOT = Path(__file__).resolve().parent.parent
import sys as _sys
_sys.path.insert(0, str(ROOT))
try:
    import gen_infocard as GIC          # 인포배너 자동생성 모듈
except Exception:
    GIC = None
WEB = ROOT / "web"
CFG_PATH = ROOT / "studio_config.json"
SUB_TEMPLATES = {
    "기본 · 대사하단/내레이션상단/강조중앙/정보우상단": {**P.STYLE_DEFAULT},
    "심플 · 대사·내레이션 하단 흰색": {
        **P.STYLE_DEFAULT,
        "narration": {**P.STYLE_DEFAULT["narration"], "color": "#FFFFFF", "v": "bottom", "margin": 110},
    },
    "예능 · 큰 노랑 내레이션 + 빨강 강조": {
        **P.STYLE_DEFAULT,
        "dialogue": {**P.STYLE_DEFAULT["dialogue"], "size": 40},
        "narration": {**P.STYLE_DEFAULT["narration"], "size": 48, "color": "#FFE600"},
    },
}

DEFAULTS = {"meta_api": "http://172.30.1.40:8770", "llm": "claude",
            "whisper_model": "large-v3", "out_dir": str(Path.home() / "ja_reviewer_out"),
            # 2-pass 전사: ①러프 스캔(scan_model)→②keep만 정밀(whisper_model).
            # map_reduce_chars: 전사가 이 글자수를 넘으면 블록 요약 후 최종 선정(토큰 폭탄 방지)
            "two_pass": True, "scan_model": "small", "map_reduce_chars": 25000,
            "banner_hold": 4.0,   # 인포카드 유지시간(초)
            # 비주얼 노출 가드(NudeNet) — keep 구간 프레임을 NN으로 검사해 노출 장면 제외
            "nsfw_guard": True, "nsfw_step": 2.0, "nsfw_threshold": 0.35,
            # ⓪ 노출 제거(클린본) — 원본에서 노출을 물리적으로 잘라낸 뒤 파이프라인 시작.
            # NudeNet은 프레임마다 점수가 요동쳐 1패스로는 0이 안 되므로 수렴할 때까지 반복.
            "nsfw_scan_step": 1.0, "nsfw_clean_threshold": 0.22, "nsfw_pad": 3.0,
            "nsfw_merge_gap": 12.0, "nsfw_min_clip": 3.0, "nsfw_max_pass": 3,
            # 클린 단계를 안 쓸 때의 폴백(keep 구간만 스캔)
            "nsfw_full_scan": False,
            # 완성본 전수 검사(최후 방어선) — 검출 시 _완성/ 대신 _검수필요/ 로 격리
            "nsfw_final_check": True, "nsfw_final_step": 0.25,
            "fullauto_mode": "summary",   # 자동 모드 방식: summary | highlight
            # keep 합계가 목표의 이 비율 미만이면 ②에서 중단(대사 없는 본편형 = 자동화 부적합)
            "min_keep_ratio": 0.5,
            "target_sec": 60,
            "tts_base": "http://127.0.0.1:17493", "tts_profile": "", "tts_language": "ko",
            "queue_gpu": 1, "queue_ai": 2, "queue_tts": 1,
            "watch_dir": "", "watch_enabled": False,
            "sub_styles": P.STYLE_DEFAULT, "sub_templates": SUB_TEMPLATES}

app = FastAPI(title="ja_reviewer")
JOBS = {}  # job_id -> {"q": Queue, "result": ..., "error": ...}


def _load_json(path, default=None):
    """깨진/없는 JSON을 예외 없이 읽는다(손으로 편집한 자막 파일 대비)."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return [] if default is None else default


def load_cfg():
    c = dict(DEFAULTS)
    if CFG_PATH.exists():
        try: c.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))
        except Exception: pass
    return c


def save_cfg(c):
    try: CFG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception: pass


QUEUE = QueueManager(load_cfg)   # 작업 큐(병렬 자동 처리) — 리소스 레인은 config의 queue_*


# 품번별 상태/스테이지 코어는 server/stages.py 로 이관(작업 큐와 공유).

# ─── 잡 / SSE ────────────────────────────────────────────────────────────────
def new_job():
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"q": queue.Queue(), "result": None, "error": None}
    return jid


def jlog(jid, msg):
    JOBS[jid]["q"].put({"type": "log", "msg": str(msg)})


def jstep(jid, n, total, label):
    JOBS[jid]["q"].put({"type": "step", "n": n, "total": total, "label": label})


def jprog(jid, frac, label=None):
    JOBS[jid]["q"].put({"type": "progress", "frac": float(frac), "label": label})


def jfile(jid, tag, path):
    JOBS[jid]["q"].put({"type": "file", "label": tag, "path": str(path)})


def jdone(jid, result):
    JOBS[jid]["result"] = result
    JOBS[jid]["q"].put({"type": "done", "result": result})


def jerr(jid, e):
    JOBS[jid]["error"] = str(e)
    JOBS[jid]["q"].put({"type": "error", "msg": str(e)})


def run_bg(fn):
    threading.Thread(target=fn, daemon=True).start()


def heartbeat(jid, label):
    """오래 걸리는 블로킹 작업(LLM 호출 등) 중 살아있음을 N초마다 로그로 알림. stop.set()로 종료."""
    stop = threading.Event()

    def run():
        n = 0
        while not stop.wait(8):
            n += 8
            jlog(jid, f"  …{label} 진행 중 ({n}s 경과)")
    threading.Thread(target=run, daemon=True).start()
    return stop


class JobEmitter(Emitter):
    """SSE 잡큐(jid)로 진행상황을 흘리는 Emitter — 스테이지 코어(stages.py)용."""
    def __init__(self, jid): self.jid = jid
    def log(self, msg): jlog(self.jid, msg)
    def step(self, n, total, label): jstep(self.jid, n, total, label)
    def prog(self, frac, label=None): jprog(self.jid, frac, label)
    def file(self, tag, path): jfile(self.jid, tag, path)


@app.get("/events/{jid}")
async def events(jid: str):
    async def gen():
        j = JOBS.get(jid)
        if not j:
            yield 'data: {"type":"error","msg":"no such job"}\n\n'; return
        while True:
            try:
                while True:
                    item = j["q"].get_nowait()
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                    if item["type"] in ("done", "error"):
                        return
            except queue.Empty:
                pass
            await asyncio.sleep(0.2)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── LLM 연결 체크 (codex/claude 설치·로그인·응답) ──────────────────────────
@app.get("/llm/check")
def llm_check():
    out = {}
    for name in ("codex", "claude"):
        ok, msg = P.llm_ping(name)
        out[name] = {"ok": ok, "msg": msg}
    return out


# ─── 실행 전 통합 점검 (돌리기 전에 뭐가 죽어 있는지 한 번에) ────────────────
def _chk_ffmpeg():
    import shutil as _sh
    miss = [b for b in ("ffmpeg", "ffprobe") if not _sh.which(b)]
    if miss:
        return False, f"없음: {', '.join(miss)} — PATH에 설치하세요"
    return True, ("GPU 인코더(NVENC) 사용 가능" if P.has_nvenc()
                  else "CPU 인코딩(libx264) — GPU 없음/드라이버 미설치")


def _chk_whisper():
    try:
        import faster_whisper  # noqa: F401
    except Exception as e:
        return False, f"faster-whisper 임포트 실패: {str(e)[:80]}"
    return True, "faster-whisper 설치됨"


def _chk_meta(c):
    """메타 API — /work/{code} 규격이라 존재하지 않을 법한 코드로 왕복만 확인."""
    api = (c.get("meta_api") or "").rstrip("/")
    if not api:
        return False, "meta_api 설정 없음"
    try:
        import urllib.request
        with urllib.request.urlopen(f"{api}/work/__healthcheck__", timeout=6) as r:
            r.read(64)
        return True, f"응답함 ({api})"
    except Exception as e:
        # 404/에러 JSON도 '서버는 살아있음'으로 본다
        msg = str(e)
        if "HTTP Error" in msg:
            return True, f"응답함 ({api})"
        return False, f"연결 실패 ({api}): {msg[:70]}"


def _chk_tts(c):
    base = c.get("tts_base") or ""
    try:
        pr = P.tts_profiles(base)
        n = len(pr) if isinstance(pr, (list, tuple)) else len(pr or {})
        if not n:
            return False, f"연결됐지만 보이스가 0개 ({base})"
        cur = c.get("tts_profile") or ""
        return True, f"보이스 {n}개" + (f" · 선택됨: {cur}" if cur else " · 보이스 미선택")
    except Exception as e:
        return False, f"voicebox 연결 실패 ({base}): {str(e)[:70]}"


def _chk_db():
    if GIC is None:
        return False, "gen_infocard 모듈 로드 실패"
    try:
        import sqlite3
        if not Path(GIC.DB).is_file():
            return False, f"DB 파일 없음: {GIC.DB}"
        con = sqlite3.connect(GIC.DB)
        n = con.execute("SELECT COUNT(*) FROM works").fetchone()[0]
        con.close()
        return True, f"작품 {n:,}건 ({GIC.DB})"
    except Exception as e:
        return False, f"DB 조회 실패: {str(e)[:80]}"


def _chk_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False, "playwright 미설치 — pip install playwright"
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            b.close()
        return True, "chromium 실행 가능"
    except Exception as e:
        return False, f"chromium 실행 실패: {str(e)[:70]} (playwright install chromium)"


def _chk_outdir(c):
    d = Path(c.get("out_dir") or "")
    try:
        d.mkdir(parents=True, exist_ok=True)
        t = d / ".write_test"
        t.write_text("ok", encoding="utf-8")
        t.unlink()
        return True, str(d)
    except Exception as e:
        return False, f"쓰기 불가 ({d}): {str(e)[:60]}"


def _chk_llm(c):
    name = c.get("llm") or "claude"
    ok, msg = P.llm_ping(name)
    return ok, f"{name}: {msg}"


@app.get("/health")
def health(deep: bool = True):
    """돌리기 전에 외부 의존성을 한 번에 점검. deep=false면 느린 검사(LLM·chromium) 생략.
    각 항목에 '어느 단계가 막히는지'를 붙여, 실패해도 무엇을 포기하면 되는지 알 수 있게 한다."""
    c = load_cfg()
    # (키, 라벨, 함수, 막히는 단계, 필수여부)
    specs = [
        ("ffmpeg", "ffmpeg / GPU", _chk_ffmpeg, "① 전사 · ② 컷 · ⑥ 굽기", True),
        ("whisper", "faster-whisper", _chk_whisper, "① 전사", True),
        ("outdir", "출력 폴더", lambda: _chk_outdir(c), "전 단계", True),
        ("meta", "메타 API", lambda: _chk_meta(c), "② AI 처리(메타 조회)", True),
        ("db", "작품 DB", _chk_db, "④ 배너", False),
        ("tts", "voicebox TTS", lambda: _chk_tts(c), "⑤ TTS", False),
    ]
    if deep:
        specs.append(("llm", "LLM CLI", lambda: _chk_llm(c), "② AI 처리", True))
        specs.append(("chromium", "playwright chromium", _chk_playwright, "④ 배너", False))

    results = {}

    def run(key, label, fn, blocks, required):
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"점검 중 오류: {str(e)[:80]}"
        results[key] = {"label": label, "ok": bool(ok), "msg": msg,
                        "blocks": blocks, "required": required}

    ts = [threading.Thread(target=run, args=s, daemon=True) for s in specs]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=90)
    # 병렬이라 완료 순서가 뒤섞인다 → 파이프라인 순서로 다시 정렬(화면이 매번 달라지지 않게)
    results = {k: results[k] for k, *_ in specs if k in results}

    fails = [v for v in results.values() if not v["ok"]]
    blocking = [v for v in fails if v["required"]]
    return {"ok": not blocking, "checked": len(results),
            "fail": len(fails), "blocking": len(blocking),
            "items": results}


# ─── 설정 ────────────────────────────────────────────────────────────────────
@app.get("/config")
def get_config():
    return load_cfg()


@app.post("/config")
async def set_config(req: Request):
    c = load_cfg(); c.update(await req.json()); save_cfg(c); return c


# ─── 파일 열기 (네이티브 다이얼로그 + 경로 검증) ─────────────────────────────
@app.post("/browse")
def browse():
    """서버(=같은 윈도우 PC)에서 네이티브 파일 다이얼로그를 띄워 경로 반환."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        r = tk.Tk(); r.withdraw(); r.attributes("-topmost", True)
        f = filedialog.askopenfilename(
            filetypes=[("영상", "*.mp4 *.mkv *.avi *.mov *.wmv"), ("모든 파일", "*.*")])
        r.destroy()
        return {"path": f or ""}
    except Exception as e:
        return JSONResponse({"path": "", "error": str(e)}, status_code=200)


@app.post("/browse_dir")
def browse_dir():
    """출력 폴더용 네이티브 디렉토리 선택 다이얼로그."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        r = tk.Tk(); r.withdraw(); r.attributes("-topmost", True)
        d = filedialog.askdirectory()
        r.destroy()
        return {"path": d or ""}
    except Exception as e:
        return JSONResponse({"path": "", "error": str(e)}, status_code=200)


@app.post("/open_dir")
async def open_dir(req: Request):
    """{sub?} — 출력 폴더(하위 sub)를 탐색기로 연다. 자동 모드 '완성본 폴더 열기'."""
    body = await req.json() if await req.body() else {}
    c = load_cfg()
    d = Path(c["out_dir"]) / (body.get("sub") or "")
    d.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(str(d))            # noqa: S606 — 로컬 GUI 편의기능
        else:
            subprocess.Popen(["xdg-open", str(d)])
        return {"ok": True, "dir": str(d)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.post("/browse_multi")
def browse_multi():
    """작업 큐용 — 영상 여러 개 선택 다이얼로그."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        r = tk.Tk(); r.withdraw(); r.attributes("-topmost", True)
        fs = filedialog.askopenfilenames(
            filetypes=[("영상", "*.mp4 *.mkv *.avi *.mov *.wmv"), ("모든 파일", "*.*")])
        r.destroy()
        return {"paths": list(fs or [])}
    except Exception as e:
        return JSONResponse({"paths": [], "error": str(e)}, status_code=200)


@app.post("/open")
async def open_video(req: Request):
    body = await req.json()
    p = Path(body.get("path", ""))
    if not p.is_file():
        raise HTTPException(404, "파일 없음")
    return {"path": str(p), "name": p.name, "duration": P.video_duration(p)}


# ─── 영상 Range 스트리밍 ─────────────────────────────────────────────────────
@app.get("/video/stream")
def stream(path: str, request: Request):
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, "파일 없음")
    size = p.stat().st_size
    ctype = mimetypes.guess_type(p.name)[0] or "video/mp4"
    rng = request.headers.get("range")
    start, end = 0, size - 1
    status = 200
    if rng:
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if m:
            start = int(m.group(1))
            if m.group(2):
                end = int(m.group(2))
            status = 206
    end = min(end, size - 1)
    length = max(0, end - start + 1)

    def gen():
        with open(p, "rb") as f:
            f.seek(start); remaining = length
            while remaining > 0:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk); yield chunk

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length), "Content-Type": ctype}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(gen(), status_code=status, headers=headers)


@app.get("/image")
def image(path: str):
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, "파일 없음")
    ctype = mimetypes.guess_type(p.name)[0] or "image/png"
    return FileResponse(str(p), media_type=ctype)


@app.get("/download")
def download(path: str):
    """임의 산출물 다운로드(첨부). 브라우저가 재생 못 하는 .mov 오버레이 등."""
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, "파일 없음")
    return FileResponse(str(p), media_type="application/octet-stream",
                        filename=p.name)


@app.get("/download/zip")
def download_zip(paths: str, name: str = "assets.zip"):
    """여러 산출물을 zip 하나로. paths는 '|' 로 구분(투명 PNG 3장 한번에 받기용)."""
    import io, zipfile
    files = [Path(x) for x in paths.split("|") if x.strip()]
    files = [p for p in files if p.is_file()]
    if not files:
        raise HTTPException(404, "파일 없음")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(str(p), arcname=p.name)
    buf.seek(0)
    # 한글 파일명 — HTTP 헤더는 latin-1만 허용하므로 RFC 5987(filename*)로 실어보낸다.
    from urllib.parse import quote
    disp = f"attachment; filename=\"assets.zip\"; filename*=UTF-8''{quote(name)}"
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": disp})


# ─── 메타 ────────────────────────────────────────────────────────────────────
@app.get("/meta/{code}")
def meta(code: str):
    c = load_cfg()
    try:
        return P.fetch_meta(c["meta_api"], code, log=lambda *_: None)
    except Exception as e:
        raise HTTPException(502, f"메타 조회 실패: {e}")


# ─── 자동 분석 (전사→메타→LLM 선정) ─────────────────────────────────────────
@app.post("/analyze")
async def analyze(req: Request):
    body = await req.json(); c = load_cfg()
    path = body["path"]; code = body["code"]
    target = int(body.get("target_sec", c["target_sec"])); llm = body.get("llm", c["llm"])
    hint = (body.get("hint") or "").strip()
    mode = body.get("mode", "summary")  # summary(요약형) | highlight(하이라이트형)
    jid = new_job()

    def work():
        try:
            jstep(jid, 1, 3, "메타 조회")
            m = P.fetch_meta(c["meta_api"], code, lambda x: jlog(jid, x))
            init = "。".join(x for x in [P.build_initial_prompt(m), hint] if x) or None  # 맥락/지시 → Whisper 힌트
            jstep(jid, 2, 3, "전사(faster-whisper)")
            segs = P.transcribe(path, body.get("model", c["whisper_model"]), lambda m2: jlog(jid, m2), initial_prompt=init)
            label = "하이라이트" if mode == "highlight" else "요약"
            jstep(jid, 3, 3, f"AI 분석({label}·구간·번역·내레이션) ({llm} 추론, 보통 1~3분)")
            pf = P.prompt_highlight if mode == "highlight" else P.prompt_auto
            hb = heartbeat(jid, f"AI 분석({llm})")
            try:
                res = P.call_llm(pf(m, segs, target, hint=hint, pos=body.get("pos","mid"), style=body.get("style","3min")), llm, lambda x: jlog(jid, x))
            finally:
                hb.set()
            res["_mode"] = mode
            jdone(jid, {"mode": "auto", "result": res})
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


# ─── AI 선정 결과 저장/불러오기 (수기 마킹처럼 재조회) ────────────────────────
@app.post("/pick/save")
async def pick_save(req: Request):
    body = await req.json(); c = load_cfg()
    code = (body.get("code") or "").strip()
    if not code:
        raise HTTPException(400, "품번 필요")
    outdir = Path(c["out_dir"]); outdir.mkdir(parents=True, exist_ok=True)
    fp = outdir / f"{code}_pick.json"
    fp.write_text(json.dumps(body.get("result") or {}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(fp)}


@app.post("/pick/load")
async def pick_load(req: Request):
    body = await req.json(); c = load_cfg()
    code = (body.get("code") or "").strip()
    fp = Path(c["out_dir"]) / f"{code}_pick.json"
    if not fp.exists():
        return {"ok": False, "result": None}
    return {"ok": True, "result": json.loads(fp.read_text(encoding="utf-8"))}


# ─── ① 잘라내기 (품번 불필요) — 선택 구간 삭제 후 트림 영상 생성 ─────────────
@app.post("/trim")
async def trim(req: Request):
    body = await req.json(); c = load_cfg()
    path = body["path"]; code = (body.get("code") or "").strip()
    excludes = [(float(a), float(b)) for a, b in body.get("excludes", [])]
    if not excludes:
        raise HTTPException(400, "삭제할 구간이 없습니다.")
    jid = new_job()

    def work():
        try:
            outdir = work_dir(c, code) if code else Path(c["out_dir"])
            outdir.mkdir(parents=True, exist_ok=True)
            total = P.video_duration(path)
            keep = P.keep_from_exclude(total, excludes)
            if not keep:
                raise RuntimeError("남는 구간이 없습니다.")
            cut_sec = sum(b - a for a, b in excludes)
            jlog(jid, f"원본 {_hms(total)} · 삭제 {len(excludes)}구간({_hms(cut_sec)}) → 남김 {len(keep)}구간")
            for a, b in excludes:
                jlog(jid, f"  ✂ 삭제 {_hms(a)}~{_hms(b)} ({_hms(b - a)})")
            out = str(outdir / (Path(path).stem + "_trim.mp4"))
            precise = bool(body.get("precise"))
            if precise:
                jstep(jid, 1, 1, "선택 구간 삭제 컷 (정밀·재인코딩)")
                P.cut_video(path, keep, out, lambda m: jlog(jid, m),
                            lambda fr: jprog(jid, fr, "잘라내는 중"))
            else:
                jstep(jid, 1, 1, "선택 구간 삭제 컷 (스마트 — 경계만 재인코딩)")
                try:
                    P.cut_video_smart(path, keep, out, lambda m: jlog(jid, m),
                                      lambda fr: jprog(jid, fr, "잘라내는 중"))
                except Exception as se:
                    jlog(jid, f"스마트 컷 실패({se}) → 무손실 카피 컷으로 폴백")
                    try:
                        P.cut_video_copy(path, keep, out, lambda m: jlog(jid, m),
                                         lambda fr: jprog(jid, fr, "잘라내는 중"))
                    except Exception as ce:
                        jlog(jid, f"무손실 컷 실패({ce}) → 재인코딩으로 폴백")
                        P.cut_video(path, keep, out, lambda m: jlog(jid, m),
                                    lambda fr: jprog(jid, fr, "잘라내는 중"))
            dur = P.video_duration(out)
            # 잘라낸 구간 정보 사이드카 저장
            info = [f"원본: {path}", f"원본 길이: {_hms(total)}", "",
                    f"■ 삭제(잘라낸) 구간 — 합계 {_hms(cut_sec)}"]
            info += [f"  {_hms(a)} ~ {_hms(b)}  ({_hms(b - a)})" for a, b in excludes]
            info += ["", "■ 남긴 구간(이어붙임)"]
            info += [f"  {_hms(a)} ~ {_hms(b)}" for a, b in keep]
            info += ["", f"결과 영상: {out}", f"결과 길이: {_hms(dur)}"]
            info_path = str(Path(out).with_name(Path(out).stem + "_info.txt"))
            Path(info_path).write_text("\n".join(info), encoding="utf-8")
            jfile(jid, "잘라낸 영상", out)
            jfile(jid, "컷 정보", info_path)
            jdone(jid, {"mode": "trim", "video": out, "duration": dur,
                        "cut_text": [f"{_hms(a)}~{_hms(b)} ({_hms(b - a)})" for a, b in excludes],
                        "keep_text": [f"{_hms(a)}~{_hms(b)}" for a, b in keep]})
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


# ─── 단계별 리뷰 생성 ① 전사 → ② AI 처리 → ③ 자막 (각 단계 독립 재실행 가능) ──
@app.get("/state/{code}")
def state(code: str):
    c = load_cfg(); outdir = work_dir(c, code)
    st = load_state(outdir, code)
    st["steps"] = steps_status(outdir, code)
    final = outdir / f"{code}_final.mp4"
    if final.is_file():
        st["final"] = str(final); st["final_sec"] = P.video_duration(final)
    # 이전에 잘라낸(_trim.mp4) 결과가 있으면 알려줘서 바로 쓰게 함 (가장 최근 것)
    trims = sorted(outdir.glob("*_trim.mp4"), key=lambda p: p.stat().st_mtime)
    if trims:
        t = trims[-1]
        st["trim_video"] = str(t); st["trim_exists"] = True
        st["trim_sec"] = P.video_duration(t)
        info = t.with_name(t.stem + "_info.txt")
        if info.is_file():
            st["trim_info"] = info.read_text(encoding="utf-8")
    else:
        st["trim_exists"] = False
    return st


@app.post("/step/transcribe")
async def step_transcribe(req: Request):
    """① 전사 — 영상(잘라낸 것) → 일본어 STT. {code}_전사.srt/.json 저장."""
    body = await req.json(); c = load_cfg()
    path = body["path"]; code = body["code"]
    model = body.get("model", c["whisper_model"])
    jid = new_job()

    def work():
        try:
            jdone(jid, stage_transcribe(c, code, path, model, JobEmitter(jid)))
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


@app.post("/step/ai")
async def step_ai(req: Request):
    """② AI 처리 — 저장된 전사 + 메타 → LLM이 압축·번역·내레이션. plan.json 저장 + 컷."""
    body = await req.json(); c = load_cfg()
    code = body["code"]
    target = int(body.get("target_sec", c["target_sec"])); llm = body.get("llm", c["llm"])
    hint = (body.get("hint") or "").strip()
    mode = body.get("mode", "summary")  # summary(요약형·짜집기) | highlight(하이라이트형·알파컷식)
    jid = new_job()

    def work():
        try:
            jdone(jid, stage_ai(c, code, body.get("path"), target, llm, mode, hint,
                                JobEmitter(jid), pos=body.get("pos", "mid"),
                                style=body.get("style", "3min")))
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


@app.post("/step/ai/prompt")
async def step_ai_prompt(req: Request):
    """수동 모드용 — ② AI에 보낼 프롬프트 전문을 반환(복사해서 codex/claude 직접 실행)."""
    body = await req.json(); c = load_cfg()
    code = body["code"]; target = int(body.get("target_sec", c["target_sec"]))
    hint = (body.get("hint") or "").strip()
    mode = body.get("mode", "summary")
    outdir = work_dir(c, code)
    tj = outdir / f"{code}_전사.json"
    if not tj.is_file():
        raise HTTPException(400, "전사 결과가 없습니다. 먼저 ① 전사를 실행하세요.")
    segs = [(d["start"], d["end"], d["text"]) for d in json.loads(tj.read_text(encoding="utf-8"))]
    try:
        m = P.fetch_meta(c["meta_api"], code, log=lambda *_: None)
    except Exception as e:
        raise HTTPException(502, f"메타 조회 실패: {e}")
    pf = P.prompt_highlight if mode == "highlight" else P.prompt_manual
    return {"prompt": pf(m, segs, target, hint=hint, pos=body.get("pos","mid"), style=body.get("style","3min"))}


@app.post("/step/ai/manual")
async def step_ai_manual(req: Request):
    """수동 모드 — codex/claude가 준 JSON을 붙여넣으면 그대로 plan 저장 + 컷(LLM 호출 안 함)."""
    body = await req.json(); c = load_cfg()
    code = body["code"]; raw = body.get("result", "")
    jid = new_job()

    def work():
        try:
            outdir = work_dir(c, code)
            st = load_state(outdir, code)
            video = body.get("path") or st.get("video")
            if not video or not Path(video).is_file():
                raise RuntimeError("전사에 쓴 영상 경로를 찾을 수 없습니다. ① 전사를 다시 실행하세요.")
            s = (raw or "").strip()
            i, j = s.find("{"), s.rfind("}")
            if i < 0 or j <= i:
                raise RuntimeError("붙여넣은 텍스트에서 JSON({…})을 찾지 못했습니다.")
            res = json.loads(s[i:j + 1])
            keep = P.parse_keep(res.get("keep", []))
            if not keep:
                raise RuntimeError("붙여넣은 JSON에 keep 구간이 없습니다.")
            (outdir / f"{code}_plan.json").write_text(
                json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
            final = str(outdir / f"{code}_final.mp4")
            jstep(jid, 1, 1, "핵심 구간 컷")
            P.cut_video(video, keep, final, lambda mm: jlog(jid, mm),
                        lambda fr: jprog(jid, fr, "컷"))
            save_state(outdir, code, summary=res.get("summary", ""), stars=P.clamp_stars(res.get("stars")))
            jfile(jid, "AI 결과(plan)", outdir / f"{code}_plan.json")
            jfile(jid, "최종 영상", final)
            jdone(jid, {"step": "ai", "code": code, "final": final,
                        "final_sec": P.video_duration(final), "summary": res.get("summary", "")})
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


@app.post("/step/subs")
async def step_subs(req: Request):
    """③ 자막 — 저장된 plan.json → 한글 대사/내레이션 SRT(+JSON) 재타이밍 저장."""
    body = await req.json(); c = load_cfg()
    code = body["code"]
    jid = new_job()

    def work():
        try:
            jdone(jid, stage_subs(c, code, JobEmitter(jid)))
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


@app.post("/regen/narration")
async def regen_narration_route(req: Request):
    """③-b 내레이션 재생성 — plan.json의 내레이션만 6슬롯 규칙으로 다시 쓴다(컷·대사 유지)."""
    body = await req.json(); c = load_cfg()
    code = body["code"]
    jid = new_job()

    def work():
        try:
            from server.core.regen import regen_narration
            outdir = work_dir(c, code)
            new_nar = regen_narration(outdir, c["meta_api"], log=lambda m: jlog(jid, m))
            jfile(jid, "내레이션 SRT", outdir / f"{code}_내레이션.srt")
            jfile(jid, "내레이션 JSON", outdir / f"{code}_내레이션.json")
            jdone(jid, {"step": "regen_narration", "code": code, "count": len(new_nar)})
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


@app.post("/regen/plan")
async def regen_plan_route(req: Request):
    """③-c 구간 재선정 — LLM이 keep을 다시 골라 plan.json 갱신 + final.mp4 재컷 + 자막 재생성."""
    body = await req.json(); c = load_cfg()
    code = body["code"]
    target = int(body.get("target_sec", c["target_sec"])); llm = body.get("llm", c["llm"])
    jid = new_job()

    def work():
        try:
            from server.core.regen import replan
            outdir = work_dir(c, code)
            jstep(jid, 1, 2, "keep 구간 재선정 + 재컷")
            replan(outdir, c["meta_api"], llm, target, log=lambda m: jlog(jid, m))
            jstep(jid, 2, 2, "자막 재생성")
            res = stage_subs(c, code, JobEmitter(jid))
            final = str(outdir / f"{code}_final.mp4")
            jfile(jid, "최종 영상", final)
            res.update({"step": "regen_plan", "final": final,
                        "final_sec": P.video_duration(final)})
            jdone(jid, res)
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


# ─── ② 리뷰 생성 (품번 필요) — 현재 영상 전사→메타→LLM 압축→컷+SRT ──────────
@app.post("/review")
async def review(req: Request):
    body = await req.json(); c = load_cfg()
    path = body["path"]; code = body["code"]
    target = int(body.get("target_sec", c["target_sec"])); llm = body.get("llm", c["llm"])
    model = body.get("model", c["whisper_model"])
    hint = (body.get("hint") or "").strip()
    jid = new_job()

    def work():
        try:
            outdir = work_dir(c, code)
            jstep(jid, 1, 4, "메타 조회")
            m = P.fetch_meta(c["meta_api"], code, lambda x: jlog(jid, x))
            init = "。".join(x for x in [P.build_initial_prompt(m), hint] if x) or None  # 맥락/지시 → Whisper 힌트
            jstep(jid, 2, 4, f"전사(faster-whisper {model})")
            segs = P.transcribe(path, model, lambda m2: jlog(jid, m2), initial_prompt=init)
            jstep(jid, 3, 4, f"AI 압축·번역·내레이션 ({llm} 추론, 보통 1~3분)")
            hb = heartbeat(jid, f"AI 처리({llm})")
            try:
                res = P.call_llm(P.prompt_manual(m, segs, target, hint=hint, pos=body.get("pos","mid"), style=body.get("style","3min")), llm, lambda x: jlog(jid, x))
            finally:
                hb.set()
            keep = P.parse_keep(res.get("keep", []))
            if not keep:
                raise RuntimeError("LLM이 keep 구간을 못 골랐습니다.")
            final = str(outdir / f"{code}_final.mp4")
            jstep(jid, 4, 4, "핵심 구간 컷 + 자막")
            P.cut_video(path, keep, final, lambda m: jlog(jid, m))
            jfile(jid, "최종 영상", final)
            dlg = P.parse_lines(res.get("dialogue", []), ("ko", "text"), extra=[("speaker", "여")], log=lambda m: jlog(jid, m))
            nar = P.parse_lines(res.get("narration", []), ("text", "ko"), extra=[("style", "기본")], log=lambda m: jlog(jid, m))
            write_dialogue(outdir, code, P.retime(dlg, keep, snap=False))
            jfile(jid, "대사 자막", outdir / f"{code}_대사.srt")
            write_narration(outdir, code, P.retime(nar, keep, snap=True))
            jfile(jid, "내레이션 자막", outdir / f"{code}_내레이션.srt")
            jdone(jid, {"mode": "manual", "final": final,
                        "srt_dialogue": str(outdir / f"{code}_대사.srt"),
                        "srt_narration": str(outdir / f"{code}_내레이션.srt"),
                        "summary": res.get("summary", ""), "stars": P.clamp_stars(res.get("stars")),
                        "final_sec": P.video_duration(final), "target": target})
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


# ─── 자동 모드 확정 렌더 (미리보기 JSON → 컷+SRT) ───────────────────────────
@app.post("/render")
async def render(req: Request):
    body = await req.json(); c = load_cfg()
    path = body["path"]; code = body["code"]; res = body["result"]
    jid = new_job()

    def work():
        try:
            outdir = work_dir(c, code)
            keep = P.parse_keep(res.get("keep", []))
            if not keep:
                raise RuntimeError("keep 구간 없음")
            final = str(outdir / f"{code}_final.mp4")
            jstep(jid, 1, 2, "핵심 구간 컷")
            P.cut_video(path, keep, final, lambda m: jlog(jid, m))
            jfile(jid, "최종 영상", final)
            jstep(jid, 2, 2, "자막 생성")
            dlg = P.parse_lines(res.get("dialogue", []), ("ko", "text"), extra=[("speaker", "여")], log=lambda m: jlog(jid, m))
            nar = P.parse_lines(res.get("narration", []), ("text", "ko"), extra=[("style", "기본")], log=lambda m: jlog(jid, m))
            write_dialogue(outdir, code, P.retime(dlg, keep, snap=False))
            jfile(jid, "대사 자막", outdir / f"{code}_대사.srt")
            write_narration(outdir, code, P.retime(nar, keep, snap=True))
            jfile(jid, "내레이션 자막", outdir / f"{code}_내레이션.srt")
            jdone(jid, {"mode": "auto", "final": final,
                        "srt_dialogue": str(outdir / f"{code}_대사.srt"),
                        "srt_narration": str(outdir / f"{code}_내레이션.srt"),
                        "final_sec": P.video_duration(final)})
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


# ─── ⑤ TTS (voicebox) — 한국어 내레이션 음성 ────────────────────────────────
@app.get("/tts/profiles")
def tts_profiles():
    c = load_cfg()
    try:
        return {"base": c["tts_base"], "profiles": P.tts_profiles(c["tts_base"])}
    except Exception as e:
        raise HTTPException(502, f"voicebox 연결 실패({c['tts_base']}): {e}")


@app.post("/tts/test")
async def tts_test(req: Request):
    """연결+보이스 확인용 단발 합성 → 재생 가능한 wav 경로 반환."""
    body = await req.json(); c = load_cfg()
    base = body.get("tts_base") or c["tts_base"]
    profile = body.get("profile") or c["tts_profile"]
    language = body.get("language", c["tts_language"])
    text = (body.get("text") or "").strip() or "안녕하세요, 딸딸기튜브입니다. 음성 테스트입니다."
    seed = body.get("seed")
    if not profile:
        raise HTTPException(400, "voicebox 보이스(profile)를 선택하세요.")
    jid = new_job()

    def work():
        try:
            outdir = Path(c["out_dir"]); outdir.mkdir(parents=True, exist_ok=True)
            wav = str(outdir / "_tts_test.wav")
            jlog(jid, f"테스트 음성 생성(voicebox {base}{', seed '+str(seed) if seed not in (None,'') else ''}): {text[:30]}…")
            P.tts_generate(base, text, profile, language, wav, seed, lambda m: jlog(jid, m))
            jdone(jid, {"mode": "tts_test", "wav": wav})
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


@app.post("/tts")
async def tts(req: Request):
    body = await req.json(); c = load_cfg()
    code = body["code"]
    base = body.get("tts_base", c["tts_base"])
    profile = body.get("profile") or c["tts_profile"]
    language = body.get("language", c["tts_language"])
    seed = body.get("seed")
    mux = bool(body.get("mux", False))
    if not profile:
        raise HTTPException(400, "voicebox 보이스(profile)를 선택하세요.")
    jid = new_job()

    def work():
        try:
            jdone(jid, stage_tts(c, code, base, profile, language, seed, mux,
                                 JobEmitter(jid),
                                 orig_audio=body.get("orig_audio", "duck"),
                                 duck_level=float(body.get("duck_level", 0.3))))
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


# ─── ⑥ 자막 굽기 (하드섭) + 템플릿 ──────────────────────────────────────────
@app.get("/sub/templates")
def sub_templates():
    c = load_cfg()
    return c.get("sub_templates") or SUB_TEMPLATES


@app.post("/sub/templates")
async def sub_templates_save(req: Request):
    body = await req.json(); c = load_cfg()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "템플릿 이름이 필요합니다.")
    tpls = c.get("sub_templates") or dict(SUB_TEMPLATES)
    tpls[name] = body.get("styles") or {}
    c["sub_templates"] = tpls; save_cfg(c)
    return {"ok": True, "templates": tpls}


@app.post("/burn")
async def burn(req: Request):
    body = await req.json(); c = load_cfg()
    code = body["code"]
    styles = body.get("styles") or c.get("sub_styles") or P.STYLE_DEFAULT
    jid = new_job()

    def work():
        try:
            r = stage_burn(c, code, styles, JobEmitter(jid), source=body.get("source"),
                           banner=bool(body.get("banner", True)),
                           parts=body.get("parts"))
            c["sub_styles"] = styles; save_cfg(c)   # 마지막 사용 스타일 기억
            jdone(jid, r)
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


# ─── 렌더 전 미리보기 (브라우저에서 실시간 합성 — 인코딩 0) ──────────────────
# 배너·워터마크·자막을 <video> 위에 얹어 재생바로 스크럽하며 확인한다.
# 애니 파라미터는 굽기(pipeline.BANNER_ANIM)를 그대로 쓴다 — 값이 갈라지면
# 미리보기와 결과물이 달라지므로 절대 여기서 따로 정의하지 않는다.
# 레이어 PNG는 1920x1080 기준이므로 프론트는 표시 배율(width/1920)로 blur·slide를 환산한다.
BANNER_CANVAS_W = 1920

_NAR_STYLE_KEY = {"기본": "narration", "일반": "narration",
                  "강조": "emphasis", "정보": "info",
                  "normal": "narration", "emphasis": "emphasis", "info": "info"}
_NAR_STYLES = ["기본", "강조", "정보"]


# ─── 자막 편집 (③ 결과를 화면에서 고치고 다시 굽기) ─────────────────────────
@app.get("/subs/{code}")
def subs_get(code: str):
    """편집용 대사/내레이션 로드 — 이미 최종 타임라인 형식(_대사.json/_내레이션.json)."""
    outdir = work_dir(load_cfg(), code)
    dj, nj = outdir / f"{code}_대사.json", outdir / f"{code}_내레이션.json"
    if not dj.is_file() and not nj.is_file():
        raise HTTPException(404, f"자막이 없습니다({code}). 먼저 ③ 자막을 생성하세요.")
    return {"code": code,
            "dialogue": [d for d in _load_json(dj) if isinstance(d, dict)],
            "narration": [d for d in _load_json(nj) if isinstance(d, dict)],
            "styles": _NAR_STYLES, "speakers": ["여", "남"]}


@app.post("/subs/{code}")
async def subs_save(code: str, req: Request):
    """편집한 대사/내레이션을 그대로 저장(SRT+JSON) — 재타이밍 없이 최종 타임라인 그대로.
    저장 후 ⑤ 굽기를 다시 하면 반영된다. 잘못된 항목은 방어 파싱으로 걸러낸다."""
    body = await req.json()
    outdir = work_dir(load_cfg(), code)
    dlg = P.parse_lines(body.get("dialogue", []), ("text", "ko"), extra=[("speaker", "여")])
    nar = P.parse_lines(body.get("narration", []), ("text", "ko"), extra=[("style", "기본")])
    if not dlg and not nar:
        raise HTTPException(400, "저장할 자막이 없습니다(모든 항목이 비었거나 형식 오류).")
    if dlg:
        write_dialogue(outdir, code, dlg)
    if nar:
        write_narration(outdir, code, nar)
    return {"ok": True, "dialogue": len(dlg), "narration": len(nar),
            "note": "저장됨 — ⑤ 굽기를 다시 하면 반영됩니다(TTS도 재생성 필요)."}


@app.get("/preview/data")
def preview_data(code: str, source: str = ""):
    """미리보기에 필요한 것 한 번에: 영상·레이어 PNG·자막(시간+스타일키)·애니 파라미터."""
    c = load_cfg()
    code = (code or "").strip()
    if not code:
        raise HTTPException(400, "품번(code)이 필요합니다.")
    outdir = Path(c["out_dir"]) / code
    # 대상 영상: 지정 → 음성입힘 → 최종컷
    src = Path(source) if source else None
    if not (src and src.is_file()):
        for cand in (outdir / f"{code}_final_voiced.mp4", outdir / f"{code}_final.mp4"):
            if cand.is_file():
                src = cand
                break
    if not (src and src.is_file()):
        raise HTTPException(404, f"미리볼 영상이 없습니다({code}). 먼저 ②AI 처리로 컷을 만드세요.")

    # 자막 — JSON(화자/유형 포함) 우선, 없으면 SRT
    subs = []
    djson, dsrt = outdir / f"{code}_대사.json", outdir / f"{code}_대사.srt"
    njson, nsrt = outdir / f"{code}_내레이션.json", outdir / f"{code}_내레이션.srt"
    if djson.is_file():
        for s, e, t, spk in P.parse_lines(_load_json(djson), ("ko", "text"),
                                          extra=[("speaker", "여")]):
            subs.append({"start": s, "end": e, "text": t,
                         "style": "dialogue_m" if spk == "남" else "dialogue"})
    elif dsrt.is_file():
        subs += [{"start": a, "end": b, "text": t, "style": "dialogue"}
                 for a, b, t in P.srt_parse(dsrt)]
    if njson.is_file():
        for s, e, t, stl in P.parse_lines(_load_json(njson), ("text", "ko"),
                                          extra=[("style", "기본")]):
            subs.append({"start": s, "end": e, "text": t,
                         "style": _NAR_STYLE_KEY.get(stl, "narration")})
    elif nsrt.is_file():
        subs += [{"start": a, "end": b, "text": t, "style": "narration"}
                 for a, b, t in P.srt_parse(nsrt)]
    subs.sort(key=lambda x: x["start"])

    # 배너 레이어 PNG — 없으면 즉석 생성(인코딩 없음, 수초)
    layers = {}
    if GIC is not None:
        try:
            icdir = Path(c["out_dir"]) / f"_infocard_{code}"
            names = {"frame": f"{code}_프레임.png", "info": f"{code}_인포카드.png",
                     "wm": f"{code}_워터마크.png"}
            if not all((icdir / n).is_file() for n in names.values()):
                GIC.generate(code, outdir=str(icdir), assets_only=True, preview_anim=False)
            for k, n in names.items():
                p = icdir / n
                if p.is_file():
                    layers[k] = str(p)
        except Exception as e:
            layers = {"error": str(e)}

    return {"code": code, "video": str(src),
            "duration": P.video_duration(str(src)),
            "layers": layers, "subs": subs,
            "styles": c.get("sub_styles") or P.STYLE_DEFAULT,
            "anim": P.BANNER_ANIM, "canvas_w": BANNER_CANVAS_W}


# ─── ⑥ 인포배너 (품번 → 인포카드/워터마크 자동 오버레이) ─────────────────────
@app.post("/infocard")
async def infocard(req: Request):
    if GIC is None:
        raise HTTPException(500, "gen_infocard 모듈 로드 실패")
    body = await req.json(); c = load_cfg()
    code = (body.get("code") or "").strip()
    if not code:
        raise HTTPException(400, "품번(code)을 입력하세요.")
    hold = float(body.get("hold", 2.0))
    # 기본: 인코딩 없이 오버레이 소스(PNG)+미리보기만. encode=True면 mp4까지(느림).
    do_encode = bool(body.get("encode", False))
    src = body.get("source") or None
    # 가운데 투명 오버레이 영상(.mov) — 번인 대신 편집기 상위 트랙에 얹는 용도
    do_alpha = bool(body.get("alpha", False))
    alpha_format = (body.get("alpha_format") or "qtrle").strip()
    alpha_fps = int(body.get("fps", 30) or 30)
    alpha_dur = body.get("alpha_duration")
    outdir = Path(c["out_dir"])

    def work():
        try:
            outdir.mkdir(parents=True, exist_ok=True)
            jstep(jid, 1, 2, "오버레이 소스 생성(인코딩 없음)")
            out = str(outdir / (f"{code}_banner.mp4" if src else f"{code}_infocard_demo.mp4"))
            # 오버레이 길이: 지정값 → 대상 영상 길이 → generate()의 기본(hold+4초)
            dur = float(alpha_dur) if alpha_dur else (GIC.probe_duration(src) if src else None)
            r = GIC.generate(code, video=src if do_encode else None,
                             out=out if do_encode else None, hold=hold,
                             outdir=str(outdir / f"_infocard_{code}"),
                             assets_only=not do_encode,
                             alpha=do_alpha, alpha_format=alpha_format,
                             alpha_duration=dur, fps=alpha_fps,
                             log=lambda m: jlog(jid, m))
            jstep(jid, 2, 2, "완료")
            a = r["assets"]
            jfile(jid, "프레임(상시)", a["frame"])
            jfile(jid, "인포카드(앞 2초)", a["info"])
            jfile(jid, "워터마크(상시)", a["wm"])
            jfile(jid, "미리보기·인포카드", r["preview_info"])
            jfile(jid, "미리보기·워터마크", r["preview_wm"])
            if r.get("preview_anim"):
                jfile(jid, "움직이는 미리보기(4초)", r["preview_anim"])
            if r.get("overlay"):
                jfile(jid, "투명 오버레이 영상(.mov)", r["overlay"])
            if r.get("out"):
                jfile(jid, "인포배너 영상", r["out"])
            jdone(jid, {"mode": "infocard", "assets": a,
                        "preview_info": r["preview_info"], "preview_wm": r["preview_wm"],
                        "preview_anim": r.get("preview_anim") or "",
                        "overlay": r.get("overlay") or "",
                        "out": r.get("out") or "", "encoded": do_encode,
                        "meta": {"code": r["meta"]["code"], "actress": r["meta"]["actress"],
                                 "title": r["meta"]["title"]}})
        except Exception as e:
            jerr(jid, e)
    jid = new_job()
    run_bg(work)
    return {"job": jid}


# ─── 작업 큐 (병렬 자동 처리) ────────────────────────────────────────────────
CODE_RE = re.compile(r"([A-Za-z]{2,6})-?(\d{2,5})")


def guess_code(name):
    m = CODE_RE.search(Path(name).stem)
    return f"{m.group(1)}-{m.group(2)}".upper() if m else ""


# 감시 폴더(풀오토 입구) — config watch_enabled/watch_dir 로 켜고 끈다
from .watcher import Watcher
WATCHER = Watcher(load_cfg, QUEUE, guess_code)


def _queue_snap():
    s = QUEUE.snapshot()
    s["watch"] = WATCHER.status()
    # 검수/재생용 산출물 경로를 붙인다 — GUI가 큐 항목을 클릭했을 때
    # '가장 완성에 가까운 것'(번인본 > 음성본 > 컷 결과)을 바로 열 수 있게.
    c = load_cfg()
    for it in s.get("items", []):
        code = it.get("code")
        if not code:
            continue
        d = Path(c["out_dir"]) / re.sub(r"[^0-9A-Za-z._-]", "_", code)
        for key, name in (("subbed", f"{code}_final_subbed.mp4"),
                          ("voiced", f"{code}_final_voiced.mp4"),
                          ("final", f"{code}_final.mp4")):
            p = d / name
            if p.is_file():
                it[key] = str(p)
    return s


@app.get("/queue")
def queue_snapshot():
    return _queue_snap()


@app.get("/queue/events")
async def queue_events():
    """큐 전체 스냅샷 SSE — 변경(version 증가) 시마다 전송."""
    async def gen():
        since = -1
        while True:
            v = await asyncio.to_thread(QUEUE.wait_version, since, 25.0)
            if v == since:
                yield ": keepalive\n\n"
                continue
            since = v
            yield f"data: {json.dumps(_queue_snap(), ensure_ascii=False)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/queue/add")
async def queue_add(req: Request):
    """{paths:[..], pipeline:{transcribe,ai,subs,tts,burn}, opts:{model,llm,target_sec,mode,hint,tts_*}}
    opts.fullauto=true 로만 보내면(자동 모드 드롭) 나머지 옵션은 config의 풀오토 프리셋으로 채운다."""
    body = await req.json()
    opts = body.get("opts") or {}
    if opts.get("fullauto"):
        opts = {**WATCHER._fullauto_opts(load_cfg()), **opts}
    videos = [{"path": p, "code": guess_code(p)} for p in body.get("paths", [])]
    ids = QUEUE.add(videos, body.get("pipeline") or
                    {"transcribe": True, "ai": True, "subs": True}, opts)
    return {"ok": True, "added": ids}


@app.post("/queue/item/{iid}")
async def queue_item_action(iid: str, req: Request):
    """{action: hold|resume|remove|set_code|move, code?, delta?}"""
    body = await req.json()
    act = body.get("action")
    if act == "hold":
        QUEUE.hold(iid)
    elif act == "resume":
        QUEUE.resume(iid)
    elif act == "remove":
        if not QUEUE.remove(iid):
            raise HTTPException(409, "진행 중인 항목은 삭제할 수 없습니다(일시정지 후 현재 단계 종료를 기다리세요).")
    elif act == "set_code":
        QUEUE.set_code(iid, body.get("code") or "")
    elif act == "move":
        # 큐 순서 = 묶음 영상의 편집 순서 → 먼저/다음은/마지막으로 판정에 그대로 쓰인다
        if not QUEUE.move(iid, int(body.get("delta", 0))):
            raise HTTPException(409, "진행 중이거나 더 이동할 수 없는 항목입니다.")
    else:
        raise HTTPException(400, f"알 수 없는 action: {act}")
    return {"ok": True}


@app.post("/queue/clear_finished")
def queue_clear_finished():
    QUEUE.clear_finished()
    return {"ok": True}


# ─── 정적 프론트 ─────────────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


def main():
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
