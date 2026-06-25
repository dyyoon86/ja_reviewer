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

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
CFG_PATH = ROOT / "studio_config.json"
DEFAULTS = {"meta_api": "http://172.30.1.40:8770", "llm": "claude",
            "whisper_model": "large-v3", "out_dir": str(Path.home() / "ja_reviewer_out"),
            "target_sec": 60}

app = FastAPI(title="ja_reviewer")
JOBS = {}  # job_id -> {"q": Queue, "result": ..., "error": ...}


def load_cfg():
    c = dict(DEFAULTS)
    if CFG_PATH.exists():
        try: c.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))
        except Exception: pass
    return c


def save_cfg(c):
    try: CFG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception: pass


# ─── 잡 / SSE ────────────────────────────────────────────────────────────────
def new_job():
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"q": queue.Queue(), "result": None, "error": None}
    return jid


def jlog(jid, msg):
    JOBS[jid]["q"].put({"type": "log", "msg": str(msg)})


def jdone(jid, result):
    JOBS[jid]["result"] = result
    JOBS[jid]["q"].put({"type": "done", "result": result})


def jerr(jid, e):
    JOBS[jid]["error"] = str(e)
    JOBS[jid]["q"].put({"type": "error", "msg": str(e)})


def run_bg(fn):
    threading.Thread(target=fn, daemon=True).start()


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
    jid = new_job()

    def work():
        try:
            jlog(jid, "① 전사 시작…")
            segs = P.transcribe(path, body.get("model", c["whisper_model"]), lambda m: jlog(jid, m))
            jlog(jid, "② 메타 조회…")
            m = P.fetch_meta(c["meta_api"], code, lambda x: jlog(jid, x))
            jlog(jid, "③ LLM 분석…")
            res = P.call_llm(P.prompt_auto(m, segs, target), llm, lambda x: jlog(jid, x))
            jdone(jid, {"mode": "auto", "result": res})
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


# ─── 수동 컷 (정사 제거→전사→LLM 압축→2단 컷+SRT) ──────────────────────────
@app.post("/cut")
async def cut(req: Request):
    body = await req.json(); c = load_cfg()
    path = body["path"]; code = body["code"]
    excludes = [(float(a), float(b)) for a, b in body.get("excludes", [])]
    target = int(body.get("target_sec", c["target_sec"])); llm = body.get("llm", c["llm"])
    jid = new_job()

    def work():
        try:
            outdir = Path(c["out_dir"]); outdir.mkdir(parents=True, exist_ok=True)
            total = P.video_duration(path)
            keep1 = P.keep_from_exclude(total, excludes)
            if not keep1:
                raise RuntimeError("남는 구간이 없습니다.")
            story = str(outdir / f"{code}_story.mp4")
            jlog(jid, "① 선택 구간 삭제 컷…")
            P.cut_video(path, keep1, story, lambda m: jlog(jid, m))
            jlog(jid, "② 전사…")
            segs = P.transcribe(story, c["whisper_model"], lambda m: jlog(jid, m))
            jlog(jid, "③ 메타…")
            m = P.fetch_meta(c["meta_api"], code, lambda x: jlog(jid, x))
            jlog(jid, "④ LLM 압축/번역/내레이션…")
            res = P.call_llm(P.prompt_manual(m, segs, target), llm, lambda x: jlog(jid, x))
            keep2 = [(float(a), float(b)) for a, b in res.get("keep", [])]
            if not keep2:
                raise RuntimeError("LLM이 keep 구간을 못 골랐습니다.")
            final = str(outdir / f"{code}_final.mp4")
            jlog(jid, "⑤ 핵심 구간 재컷…")
            P.cut_video(story, keep2, final, lambda m: jlog(jid, m))
            dlg = [(float(d["start"]), float(d["end"]), d["ko"]) for d in res.get("dialogue", [])]
            nar = [(float(d["start"]), float(d["end"]), d["text"]) for d in res.get("narration", [])]
            P.write_srt(P.retime(dlg, keep2, snap=False), outdir / f"{code}_대사.srt")
            P.write_srt(P.retime(nar, keep2, snap=True), outdir / f"{code}_내레이션.srt")
            jdone(jid, {"mode": "manual", "final": final,
                        "srt_dialogue": str(outdir / f"{code}_대사.srt"),
                        "srt_narration": str(outdir / f"{code}_내레이션.srt"),
                        "summary": res.get("summary", ""), "stars": res.get("stars"),
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
            outdir = Path(c["out_dir"]); outdir.mkdir(parents=True, exist_ok=True)
            keep = [(float(a), float(b)) for a, b in res.get("keep", [])]
            if not keep:
                raise RuntimeError("keep 구간 없음")
            final = str(outdir / f"{code}_final.mp4")
            jlog(jid, "컷…")
            P.cut_video(path, keep, final, lambda m: jlog(jid, m))
            dlg = [(float(d["start"]), float(d["end"]), d["ko"]) for d in res.get("dialogue", [])]
            nar = [(float(d["start"]), float(d["end"]), d["text"]) for d in res.get("narration", [])]
            P.write_srt(P.retime(dlg, keep, snap=False), outdir / f"{code}_대사.srt")
            P.write_srt(P.retime(nar, keep, snap=True), outdir / f"{code}_내레이션.srt")
            jdone(jid, {"mode": "auto", "final": final,
                        "srt_dialogue": str(outdir / f"{code}_대사.srt"),
                        "srt_narration": str(outdir / f"{code}_내레이션.srt"),
                        "final_sec": P.video_duration(final)})
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


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
