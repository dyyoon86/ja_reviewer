#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
딸딸기 스튜디오 — 윈도우 GUI (영상 1개 → 자동 파이프라인).

흐름:
  [영상 선택 + 품번]
    → ① Whisper 일본어 SRT 추출 (faster-whisper, 로컬)
    → ② LAN 메타 API로 품번 정보 조회 (우분투 DB)
    → ③ LLM(codex/claude CLI)으로 스토리 분석 → 스토리 구간 선정 + 한글 대사 + 내레이션
    → [미리보기: 요약·구간·내레이션 — 수정 가능] → 사용자 [확정]
    → ④ ffmpeg로 스토리 구간만 컷&이어붙이기 + SRT 새 타임라인 재계산
  출력: 잘린영상.mp4 + 대사.srt + 내레이션.srt   (내레이션 음성 TTS는 사용자가 별도)

요구사항(윈도우):
  - Python 3.10+, ffmpeg(PATH), pip install faster-whisper
  - LLM CLI 중 하나 로그인:  codex  또는  claude
  - 같은 네트워크의 우분투 meta-api 가동 (http://172.30.1.40:8770)

사용: python ddalddalgi_studio.py
"""
import os
import re
import json
import queue
import threading
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

CONFIG = Path(__file__).with_name("studio_config.json")
DEFAULTS = {
    "meta_api": "http://172.30.1.40:8770",
    "llm": "claude",            # "codex" | "claude"
    "whisper_model": "large-v3",
    "out_dir": str(Path.home() / "ddalddalgi_out"),
}


def load_cfg():
    c = dict(DEFAULTS)
    if CONFIG.exists():
        try: c.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        except Exception: pass
    return c


def save_cfg(c):
    try: CONFIG.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception: pass


# ─── 시간 유틸 ────────────────────────────────────────────────────────────────
def s2srt(x):
    h = int(x // 3600); m = int(x % 3600 // 60); s = int(x % 60); ms = int(round((x - int(x)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(entries, path):
    """entries: [(start_sec, end_sec, text)]"""
    out = []
    for i, (a, b, t) in enumerate(entries, 1):
        out.append(f"{i}\n{s2srt(a)} --> {s2srt(b)}\n{t}")
    Path(path).write_text("\n\n".join(out) + "\n", encoding="utf-8")


# ─── ① Whisper 전사 ──────────────────────────────────────────────────────────
def transcribe(video, model_name, log):
    log(f"① Whisper 전사 시작 (모델 {model_name}) — 시간이 걸립니다...")
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="auto", compute_type="auto")
    segs, _ = model.transcribe(video, language="ja", vad_filter=True)
    out = []
    for s in segs:
        txt = (s.text or "").strip()
        if txt:
            out.append((float(s.start), float(s.end), txt))
        if len(out) % 50 == 0 and out:
            log(f"   …{len(out)}개 세그먼트")
    log(f"① 완료: {len(out)} 세그먼트")
    return out


# ─── ② 메타 조회 (LAN) ───────────────────────────────────────────────────────
def fetch_meta(api, code, log):
    url = f"{api.rstrip('/')}/work/{urllib.request.quote(code)}"
    log(f"② 메타 조회: {url}")
    with urllib.request.urlopen(url, timeout=15) as r:
        m = json.loads(r.read().decode("utf-8"))
    if m.get("error"):
        raise RuntimeError(f"메타 조회 실패: {m['error']}")
    log(f"② 메타 OK: {m.get('actress')} / {m.get('label')} / {m.get('meas')}")
    return m


# ─── ③ LLM 호출 (codex / claude CLI) ─────────────────────────────────────────
def call_llm(prompt, which, log):
    log(f"③ LLM({which}) 호출 — 스토리 분석/번역/내레이션 생성...")
    txt = ""
    if which == "codex":
        with tempfile.TemporaryDirectory() as td:
            outf = Path(td) / "o.json"
            cmd = ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "-o", str(outf), prompt]
            subprocess.run(cmd, timeout=600, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            txt = outf.read_text(encoding="utf-8") if outf.exists() else ""
    else:  # claude
        p = subprocess.run(["claude", "-p", prompt], timeout=600, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
        txt = p.stdout or ""
    s = txt.strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise RuntimeError("LLM 응답에서 JSON을 못 찾음. CLI 로그인 상태 확인.")
    return json.loads(s[i:j+1])


def build_prompt(meta, segs):
    lines = []
    for k, (a, b, t) in enumerate(segs, 1):
        lines.append(f"{k}\t{a:.2f}\t{b:.2f}\t{t}")
    srt_block = "\n".join(lines)
    g = ", ".join(meta.get("genres") or []) or (meta.get("genre") or "")
    return f"""너는 딸딸기튜브의 일본 신작 AV 해설영상 작가다. 아래 작품의 일본어 자막(타임코드 포함)과 메타데이터를 보고,
'스토리가 있는 구간만' 골라 영상을 자르고, 한글 대사 자막과 해설 내레이션을 만든다.

[작품 메타]
- 품번:{meta.get('code')}  배우:{meta.get('actress')}({meta.get('actress_ja')})  생일:{meta.get('birthday')} {meta.get('blood_type') or ''}
- 신체:{meta.get('meas')}  레이블:{meta.get('label')}  메이커:{meta.get('maker')}  감독:{meta.get('director')}  시리즈:{meta.get('series_ja') or '-'}
- 장르:{g}  발매:{(meta.get('release_date') or '')[:10]}  런타임:{meta.get('runtime_mins')}분  인기: 조회{meta.get('views')} 좋아요{meta.get('likes')} 싫어요{meta.get('dislikes')}
- 일본어 원제:{meta.get('title_ja')}
- 한국어 시놉시스:{meta.get('description')}

[일본어 자막]  (형식: 번호\\t시작초\\t끝초\\t대사)
{srt_block}

[작업 규칙]
1) 스토리 구간 선정: 설정·관계·상황전환·갈등·결말이 드러나는 '대화 구간'만 keep. 신음·짧은탄성·반복감탄·비스토리 섹스대사는 제외. 전체 타임라인에 걸쳐 고른다(초반만 X).
2) 한글 대사: keep한 세그먼트의 일본어를 자연스러운 한국어 구어체로 번역(번역투 금지). 신음류는 빼거나 (신음) 처리.
3) 내레이션: 3분휴지 스타일(정중체+솔직 호불호+마니아 은어). 인트로→상황설명→평가→총평. 섹스로 넘어가는 구간은 내레이션으로 브릿지("이후 호텔로 자리를 옮겨…"). 평가/감상은 그럴듯하게 창작하되 메타·시놉과 모순 금지.
4) 모든 시간은 '원본 영상 기준 초'로 출력(컷 재계산은 프로그램이 함).

[출력 — JSON만, 다른 텍스트 금지]
{{
 "summary": "스토리 3~5줄 요약",
 "stars": 1~5 정수,
 "keep": [[시작초,끝초], ...],            // 남길 스토리 구간(섹스 제외)
 "dialogue": [{{"start":초,"end":초,"ko":"한글대사"}}, ...],   // keep 구간 내 대사
 "narration": [{{"start":초,"end":초,"text":"내레이션"}}, ...] // 타임라인 해설
}}"""


# ─── ④ 컷 + 재타이밍 ─────────────────────────────────────────────────────────
def retime(entries, keep, snap=False, default_dur=4.0):
    """원본 시간 entries[(s,e,text)] → 컷(keep 구간 이어붙임) 새 타임라인.
    keep 밖 항목: snap=False(대사)면 버림, snap=True(내레이션)면 컷 경계로 당김
    (영상 맨앞 인트로 → 0초, 스킵된 섹스장면 자리 브릿지 내레이션 → 그 컷 이음새)."""
    keep = sorted(keep)
    offs, acc = [], 0.0
    for a, b in keep:
        offs.append(acc); acc += (b - a)
    total = acc
    out = []
    for s, e, t in entries:
        placed = False
        for (a, b), off in zip(keep, offs):
            if s >= a - 0.05 and s < b + 0.05:
                ns = off + max(0.0, s - a)
                ne = off + min(b - a, e - a)
                if ne <= ns:
                    ne = ns + 0.5
                out.append((ns, ne, t)); placed = True
                break
        if placed or not snap:
            continue
        # keep 밖 + snap → 컷 경계로 스냅
        if s < keep[0][0]:
            ns = 0.0
        else:
            ns = total
            for (a, b), off in zip(keep, offs):
                if s < a:
                    ns = off; break
        ne = min(total, ns + (e - s if e > s else default_dur))
        if ne <= ns:
            ne = min(total, ns + default_dur)
        out.append((ns, ne, t))
    out.sort(key=lambda x: x[0])
    return out


def cut_video(video, keep, out_path, log):
    log(f"④ 영상 컷: {len(keep)}구간 이어붙이기 (ffmpeg, 재인코딩)...")
    parts_v, parts_a, filt = [], [], []
    for i, (a, b) in enumerate(sorted(keep)):
        filt.append(f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[v{i}];")
        filt.append(f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{i}];")
        parts_v.append(f"[v{i}]"); parts_a.append(f"[a{i}]")
    n = len(keep)
    concat = "".join(f"{parts_v[i]}{parts_a[i]}" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
    fc = "".join(filt) + concat
    cmd = ["ffmpeg", "-y", "-i", video, "-filter_complex", fc,
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
           "-c:a", "aac", out_path]
    subprocess.run(cmd, check=True)
    log(f"④ 컷 완료: {out_path}")


# ─── GUI ─────────────────────────────────────────────────────────────────────
class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_cfg()
        self.q = queue.Queue()
        self.result = None
        self.video = None
        root.title("딸딸기 스튜디오")
        root.geometry("820x640")

        top = ttk.Frame(root, padding=8); top.pack(fill="x")
        ttk.Button(top, text="영상 선택", command=self.pick).pack(side="left")
        self.vlbl = ttk.Label(top, text="(영상 없음)"); self.vlbl.pack(side="left", padx=8)
        ttk.Label(top, text="품번:").pack(side="left")
        self.code = ttk.Entry(top, width=14); self.code.pack(side="left", padx=4)
        ttk.Label(top, text="LLM:").pack(side="left")
        self.llm = ttk.Combobox(top, width=8, values=["claude", "codex"], state="readonly")
        self.llm.set(self.cfg.get("llm", "claude")); self.llm.pack(side="left", padx=4)
        self.start_btn = ttk.Button(top, text="시작", command=self.start); self.start_btn.pack(side="left", padx=8)

        cfgf = ttk.Frame(root, padding=(8, 0)); cfgf.pack(fill="x")
        ttk.Label(cfgf, text="메타API:").pack(side="left")
        self.api = ttk.Entry(cfgf, width=28); self.api.insert(0, self.cfg["meta_api"]); self.api.pack(side="left", padx=4)
        ttk.Label(cfgf, text="Whisper:").pack(side="left")
        self.wm = ttk.Entry(cfgf, width=12); self.wm.insert(0, self.cfg["whisper_model"]); self.wm.pack(side="left", padx=4)
        ttk.Label(cfgf, text="출력:").pack(side="left")
        self.outd = ttk.Entry(cfgf, width=24); self.outd.insert(0, self.cfg["out_dir"]); self.outd.pack(side="left", padx=4)

        self.log = scrolledtext.ScrolledText(root, height=10); self.log.pack(fill="both", expand=False, padx=8, pady=6)

        pv = ttk.LabelFrame(root, text="미리보기 (확정 전 수정 가능)", padding=6); pv.pack(fill="both", expand=True, padx=8, pady=4)
        ttk.Label(pv, text="스토리 요약 / 내레이션 (JSON)").pack(anchor="w")
        self.preview = scrolledtext.ScrolledText(pv, height=12); self.preview.pack(fill="both", expand=True)

        bot = ttk.Frame(root, padding=8); bot.pack(fill="x")
        self.confirm_btn = ttk.Button(bot, text="확정 → 컷 & SRT 생성", command=self.confirm, state="disabled")
        self.confirm_btn.pack(side="right")

        self.root.after(120, self.pump)

    def logln(self, s): self.q.put(("log", s))
    def pump(self):
        try:
            while True:
                k, v = self.q.get_nowait()
                if k == "log":
                    self.log.insert("end", v + "\n"); self.log.see("end")
                elif k == "preview":
                    self.preview.delete("1.0", "end"); self.preview.insert("1.0", v)
                    self.confirm_btn.config(state="normal")
                elif k == "done":
                    self.start_btn.config(state="normal")
                    messagebox.showinfo("완료", v)
                elif k == "err":
                    self.start_btn.config(state="normal"); self.confirm_btn.config(state="normal")
                    messagebox.showerror("오류", v)
        except queue.Empty:
            pass
        self.root.after(120, self.pump)

    def pick(self):
        f = filedialog.askopenfilename(filetypes=[("영상", "*.mp4 *.mkv *.avi *.mov *.wmv"), ("모든", "*.*")])
        if f:
            self.video = f; self.vlbl.config(text=Path(f).name)

    def save_settings(self):
        self.cfg.update({"meta_api": self.api.get().strip(), "llm": self.llm.get(),
                         "whisper_model": self.wm.get().strip(), "out_dir": self.outd.get().strip()})
        save_cfg(self.cfg)

    def start(self):
        if not self.video: return messagebox.showwarning("", "영상을 선택하세요.")
        if not self.code.get().strip(): return messagebox.showwarning("", "품번을 입력하세요.")
        self.save_settings(); self.start_btn.config(state="disabled"); self.confirm_btn.config(state="disabled")
        threading.Thread(target=self._run_analyze, daemon=True).start()

    def _run_analyze(self):
        try:
            code = self.code.get().strip()
            segs = transcribe(self.video, self.cfg["whisper_model"], self.logln)
            self.segs = segs
            meta = fetch_meta(self.cfg["meta_api"], code, self.logln)
            res = call_llm(build_prompt(meta, segs), self.cfg["llm"], self.logln)
            self.result = res; self.meta = meta
            pretty = json.dumps(res, ensure_ascii=False, indent=2)
            self.logln("③ 완료 — 미리보기에서 확인/수정 후 [확정] 누르세요.")
            self.q.put(("preview", pretty))
        except Exception as e:
            self.q.put(("err", f"{type(e).__name__}: {e}"))

    def confirm(self):
        # 미리보기 JSON(수정본) 반영
        try:
            res = json.loads(self.preview.get("1.0", "end").strip())
        except Exception as e:
            return messagebox.showerror("JSON 오류", f"미리보기 JSON을 파싱 못함: {e}")
        self.confirm_btn.config(state="disabled")
        threading.Thread(target=self._run_render, args=(res,), daemon=True).start()

    def _run_render(self, res):
        try:
            code = self.code.get().strip()
            outdir = Path(self.cfg["out_dir"]); outdir.mkdir(parents=True, exist_ok=True)
            keep = [(float(a), float(b)) for a, b in res.get("keep", [])]
            if not keep: raise RuntimeError("keep 구간이 없습니다.")
            cut_path = str(outdir / f"{code}_cut.mp4")
            cut_video(self.video, keep, cut_path, self.logln)
            dlg = [(float(d["start"]), float(d["end"]), d["ko"]) for d in res.get("dialogue", [])]
            nar = [(float(d["start"]), float(d["end"]), d["text"]) for d in res.get("narration", [])]
            write_srt(retime(dlg, keep, snap=False), outdir / f"{code}_대사.srt")
            write_srt(retime(nar, keep, snap=True), outdir / f"{code}_내레이션.srt")
            self.logln(f"④ 완료 → {outdir}")
            self.q.put(("done", f"출력 완료\n{cut_path}\n{code}_대사.srt\n{code}_내레이션.srt"))
        except Exception as e:
            self.q.put(("err", f"{type(e).__name__}: {e}"))


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
