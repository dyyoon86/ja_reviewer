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
SUB_TEMPLATES = {
    "기본 · 대사 하단 / 내레이션 상단(노랑)": P.STYLE_DEFAULT,
    "심플 · 대사·내레이션 모두 하단 흰색": {
        "dialogue": {**P.STYLE_DEFAULT["dialogue"]},
        "narration": {**P.STYLE_DEFAULT["narration"], "color": "#FFFFFF", "v": "bottom", "margin": 110},
    },
    "예능 · 큰 노랑 내레이션": {
        "dialogue": {**P.STYLE_DEFAULT["dialogue"], "size": 40},
        "narration": {**P.STYLE_DEFAULT["narration"], "size": 48, "color": "#FFE600"},
    },
}

DEFAULTS = {"meta_api": "http://172.30.1.40:8770", "llm": "claude",
            "whisper_model": "large-v3", "out_dir": str(Path.home() / "ja_reviewer_out"),
            "target_sec": 60,
            "tts_base": "http://127.0.0.1:17493", "tts_profile": "", "tts_language": "ko",
            "sub_styles": P.STYLE_DEFAULT, "sub_templates": SUB_TEMPLATES}

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


def jstep(jid, n, total, label):
    JOBS[jid]["q"].put({"type": "step", "n": n, "total": total, "label": label})


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
            jstep(jid, 1, 3, "전사(faster-whisper)")
            segs = P.transcribe(path, body.get("model", c["whisper_model"]), lambda m: jlog(jid, m))
            jstep(jid, 2, 3, "메타 조회")
            m = P.fetch_meta(c["meta_api"], code, lambda x: jlog(jid, x))
            jstep(jid, 3, 3, "AI 분석(구간·번역·내레이션)")
            res = P.call_llm(P.prompt_auto(m, segs, target), llm, lambda x: jlog(jid, x))
            jdone(jid, {"mode": "auto", "result": res})
        except Exception as e:
            jerr(jid, e)
    run_bg(work)
    return {"job": jid}


# ─── ① 잘라내기 (품번 불필요) — 선택 구간 삭제 후 트림 영상 생성 ─────────────
@app.post("/trim")
async def trim(req: Request):
    body = await req.json(); c = load_cfg()
    path = body["path"]
    excludes = [(float(a), float(b)) for a, b in body.get("excludes", [])]
    if not excludes:
        raise HTTPException(400, "삭제할 구간이 없습니다.")
    jid = new_job()

    def work():
        try:
            outdir = Path(c["out_dir"]); outdir.mkdir(parents=True, exist_ok=True)
            total = P.video_duration(path)
            keep = P.keep_from_exclude(total, excludes)
            if not keep:
                raise RuntimeError("남는 구간이 없습니다.")
            out = str(outdir / (Path(path).stem + "_trim.mp4"))
            jstep(jid, 1, 1, "선택 구간 삭제 컷")
            P.cut_video(path, keep, out, lambda m: jlog(jid, m))
            jfile(jid, "잘라낸 영상", out)
            jdone(jid, {"mode": "trim", "video": out, "duration": P.video_duration(out)})
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
    jid = new_job()

    def work():
        try:
            outdir = Path(c["out_dir"]); outdir.mkdir(parents=True, exist_ok=True)
            jstep(jid, 1, 4, f"전사(faster-whisper {model})")
            segs = P.transcribe(path, model, lambda m: jlog(jid, m))
            jstep(jid, 2, 4, "메타 조회")
            m = P.fetch_meta(c["meta_api"], code, lambda x: jlog(jid, x))
            jstep(jid, 3, 4, "AI 압축·번역·내레이션")
            res = P.call_llm(P.prompt_manual(m, segs, target), llm, lambda x: jlog(jid, x))
            keep = [(float(a), float(b)) for a, b in res.get("keep", [])]
            if not keep:
                raise RuntimeError("LLM이 keep 구간을 못 골랐습니다.")
            final = str(outdir / f"{code}_final.mp4")
            jstep(jid, 4, 4, "핵심 구간 컷 + 자막")
            P.cut_video(path, keep, final, lambda m: jlog(jid, m))
            jfile(jid, "최종 영상", final)
            dlg = [(float(d["start"]), float(d["end"]), d["ko"]) for d in res.get("dialogue", [])]
            nar = [(float(d["start"]), float(d["end"]), d["text"]) for d in res.get("narration", [])]
            P.write_srt(P.retime(dlg, keep, snap=False), outdir / f"{code}_대사.srt")
            jfile(jid, "대사 자막", outdir / f"{code}_대사.srt")
            P.write_srt(P.retime(nar, keep, snap=True), outdir / f"{code}_내레이션.srt")
            jfile(jid, "내레이션 자막", outdir / f"{code}_내레이션.srt")
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
            jstep(jid, 1, 2, "핵심 구간 컷")
            P.cut_video(path, keep, final, lambda m: jlog(jid, m))
            jfile(jid, "최종 영상", final)
            jstep(jid, 2, 2, "자막 생성")
            dlg = [(float(d["start"]), float(d["end"]), d["ko"]) for d in res.get("dialogue", [])]
            nar = [(float(d["start"]), float(d["end"]), d["text"]) for d in res.get("narration", [])]
            P.write_srt(P.retime(dlg, keep, snap=False), outdir / f"{code}_대사.srt")
            jfile(jid, "대사 자막", outdir / f"{code}_대사.srt")
            P.write_srt(P.retime(nar, keep, snap=True), outdir / f"{code}_내레이션.srt")
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
    if not profile:
        raise HTTPException(400, "voicebox 보이스(profile)를 선택하세요.")
    jid = new_job()

    def work():
        try:
            outdir = Path(c["out_dir"]); outdir.mkdir(parents=True, exist_ok=True)
            wav = str(outdir / "_tts_test.wav")
            jlog(jid, f"테스트 음성 생성(voicebox {base}): {text[:30]}…")
            P.tts_generate(base, text, profile, language, wav, lambda m: jlog(jid, m))
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
    mux = bool(body.get("mux", False))
    if not profile:
        raise HTTPException(400, "voicebox 보이스(profile)를 선택하세요.")
    jid = new_job()

    def work():
        try:
            outdir = Path(c["out_dir"])
            srt = outdir / f"{code}_내레이션.srt"
            if not srt.is_file():
                raise RuntimeError(f"내레이션 SRT 없음: {srt} (먼저 리뷰 생성)")
            entries = P.srt_parse(srt)
            if not entries:
                raise RuntimeError("내레이션 항목이 없습니다.")
            clipdir = outdir / f"{code}_tts"; clipdir.mkdir(parents=True, exist_ok=True)
            clips = []
            total = len(entries) + 1 + (1 if mux else 0)
            for i, (st, en, text) in enumerate(entries, 1):
                jstep(jid, i, total, f"음성 {i}/{len(entries)}: {text[:18]}")
                w = str(clipdir / f"n{i:03d}.wav")
                P.tts_generate(base, text, profile, language, w, lambda m: jlog(jid, m))
                clips.append((st, w))
            wav = str(outdir / f"{code}_내레이션.wav")
            jstep(jid, len(entries) + 1, total, "내레이션 트랙 합성")
            P.build_narration_wav(clips, wav, lambda m: jlog(jid, m))
            jfile(jid, "내레이션 음성", wav)
            out = {"mode": "tts", "narration_wav": wav, "count": len(clips)}
            if mux:
                final = outdir / f"{code}_final.mp4"
                if final.is_file():
                    voiced = str(outdir / f"{code}_final_voiced.mp4")
                    jstep(jid, total, total, "영상에 음성 입히기")
                    P.mux_narration(str(final), wav, voiced, log=lambda m: jlog(jid, m))
                    jfile(jid, "음성 입힌 영상", voiced)
                    out["voiced"] = voiced
                else:
                    jlog(jid, f"※ {final} 없음 → 믹스 생략(내레이션 WAV만 생성)")
            jdone(jid, out)
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
            outdir = Path(c["out_dir"])
            voiced = outdir / f"{code}_final_voiced.mp4"
            final = outdir / f"{code}_final.mp4"
            if body.get("source"):
                src = Path(body["source"])
            elif voiced.is_file():
                src = voiced            # 음성 입힌 영상 우선
            elif final.is_file():
                src = final
            else:
                raise RuntimeError(f"원본 영상이 없습니다: {final} (먼저 리뷰 생성)")
            dsrt = outdir / f"{code}_대사.srt"
            nsrt = outdir / f"{code}_내레이션.srt"
            out = str(outdir / f"{code}_final_subbed.mp4")
            jstep(jid, 1, 1, "자막 굽기(ffmpeg)")
            P.burn_subs(str(src), str(dsrt), str(nsrt), out, styles, lambda m: jlog(jid, m))
            jfile(jid, "자막 입힌 영상", out)
            c["sub_styles"] = styles; save_cfg(c)   # 마지막 사용 스타일 기억
            jdone(jid, {"mode": "burn", "subbed": out, "source": str(src)})
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
