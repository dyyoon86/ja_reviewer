#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
딸딸기 스튜디오 — 윈도우 GUI. 일본 신작 → 스토리만 컷 + 한글 대사자막 + 해설 내레이션.

두 가지 모드:
  ● 자동!  — LLM이 풀 SRT 보고 스토리/정사 구간을 알아서 선정 (편하지만 토큰 많이 씀)
  ● 수동   — 내가 정사장면 구간을 직접 체크해서 제외 → 그것만 빼고 컷
            (LLM은 번역·내레이션만 → 토큰 절약 + 정확. 컷영상으로 전사하니 재타이밍 불필요)
            마킹: 내장 플레이어(python-vlc)로 시각 체크 OR 텍스트로 구간 입력 둘 다 지원.

흐름(수동):  [영상]→제외구간 체크→컷→Whisper(짧은영상)→메타→LLM(번역+내레이션)→출력
흐름(자동):  [영상]→Whisper(풀)→메타→LLM(선정+번역+내레이션)→[미리보기]→확정→컷+재타이밍

요구사항(윈도우): Python3.10+, ffmpeg(PATH), pip install faster-whisper,
  GPU면 pip install nvidia-cublas-cu12 nvidia-cudnn-cu12, (수동 플레이어용) VLC + pip install python-vlc,
  LLM: codex 또는 claude CLI 로그인.
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
    "llm": "claude",
    "whisper_model": "large-v3",
    "out_dir": str(Path.home() / "ddalddalgi_out"),
    "target_sec": 60,
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
    x = max(0.0, x)
    h = int(x // 3600); m = int(x % 3600 // 60); s = int(x % 60); ms = int(round((x - int(x)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def hhmmss(x):
    x = int(max(0, x)); return f"{x//3600:02d}:{x%3600//60:02d}:{x%60:02d}"


def parse_time(s):
    s = s.strip()
    if not s: return None
    if ":" in s:
        parts = [float(p) for p in s.split(":")]
        while len(parts) < 3: parts.insert(0, 0)
        return parts[0]*3600 + parts[1]*60 + parts[2]
    return float(s)


def ranges_from_text(text):
    """'12:30-18:00, 45:00-52:00' → [(750,1080),(2700,3120)]"""
    out = []
    for chunk in re.split(r"[,\n]", text):
        chunk = chunk.strip()
        if not chunk: continue
        m = re.split(r"[-~]", chunk)
        if len(m) != 2: continue
        a, b = parse_time(m[0]), parse_time(m[1])
        if a is not None and b is not None and b > a:
            out.append((a, b))
    return sorted(out)


def write_srt(entries, path):
    out = [f"{i}\n{s2srt(a)} --> {s2srt(b)}\n{t}" for i, (a, b, t) in enumerate(entries, 1)]
    Path(path).write_text("\n\n".join(out) + "\n", encoding="utf-8")


def video_duration(path):
    try:
        out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                       "format=duration", "-of", "csv=p=0", path])
        return float(out.decode().strip())
    except Exception:
        return 0.0


def keep_from_exclude(total, excludes, min_gap=0.3):
    """제외 구간 → 남길(keep) 구간(여집합)."""
    ex = sorted(excludes); keep = []; cur = 0.0
    for a, b in ex:
        a = max(0.0, a); b = min(total, b)
        if a - cur > min_gap:
            keep.append((cur, a))
        cur = max(cur, b)
    if total - cur > min_gap:
        keep.append((cur, total))
    return keep


# ─── ① Whisper ───────────────────────────────────────────────────────────────
def transcribe(video, model_name, log):
    log(f"Whisper 전사 (모델 {model_name})...")
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="auto", compute_type="auto")
    segs, info = model.transcribe(video, language="ja", vad_filter=True)
    log(f"   device 추정: {getattr(info,'language','ja')} / 전사 중...")
    out = []
    for s in segs:
        t = (s.text or "").strip()
        if t: out.append((float(s.start), float(s.end), t))
        if len(out) % 50 == 0 and out: log(f"   …{len(out)}")
    log(f"전사 완료: {len(out)} 세그먼트")
    return out


# ─── ② 메타 ──────────────────────────────────────────────────────────────────
def fetch_meta(api, code, log):
    url = f"{api.rstrip('/')}/work/{urllib.request.quote(code)}"
    log(f"메타 조회: {url}")
    with urllib.request.urlopen(url, timeout=15) as r:
        m = json.loads(r.read().decode("utf-8"))
    if m.get("error"): raise RuntimeError(m["error"])
    log(f"메타 OK: {m.get('actress')} / {m.get('label')} / {m.get('meas')}")
    return m


# ─── ③ LLM ───────────────────────────────────────────────────────────────────
def call_llm(prompt, which, log):
    log(f"LLM({which}) 호출...")
    if which == "codex":
        with tempfile.TemporaryDirectory() as td:
            outf = Path(td) / "o.json"
            subprocess.run(["codex", "exec", "--ephemeral", "--skip-git-repo-check", "-o", str(outf), prompt],
                           timeout=600, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            txt = outf.read_text(encoding="utf-8") if outf.exists() else ""
    else:
        p = subprocess.run(["claude", "-p", prompt], timeout=600, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
        txt = p.stdout or ""
    s = txt.strip(); i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i: raise RuntimeError("LLM JSON 응답 없음 (CLI 로그인 확인)")
    return json.loads(s[i:j+1])


def _meta_block(meta):
    g = ", ".join(meta.get("genres") or []) or (meta.get("genre") or "")
    return (f"품번:{meta.get('code')} 배우:{meta.get('actress')}({meta.get('actress_ja')}) "
            f"신체:{meta.get('meas')} 레이블:{meta.get('label')} 메이커:{meta.get('maker')} "
            f"감독:{meta.get('director')} 시리즈:{meta.get('series_ja') or '-'} 장르:{g} "
            f"발매:{(meta.get('release_date') or '')[:10]} 런타임:{meta.get('runtime_mins')}분 "
            f"인기:조회{meta.get('views')}/좋아요{meta.get('likes')}/싫어요{meta.get('dislikes')}\n"
            f"일본원제:{meta.get('title_ja')}\n한국어시놉시스:{meta.get('description')}")


def _style():
    return ("[톤] 3분휴지 스타일 — 정중체(~습니다)+솔직 호불호+마니아 은어(미드/포텐/피지컬/육덕/하메리/1인칭/펠라/시추에이션)"
            "+레이블 맥락. [내레이션 구성] 인트로→상황설명→평가→총평, 섹스 스킵 구간은 브릿지('이후 호텔로…'). "
            "평가/감상은 그럴듯하게 창작하되 메타·시놉과 모순 금지. [대사] 자연스러운 한국어 구어체(번역투 금지), 신음류 제외/(신음).")


def prompt_auto(meta, segs, target_sec=60):
    body = "\n".join(f"{k}\t{a:.2f}\t{b:.2f}\t{t}" for k, (a, b, t) in enumerate(segs, 1))
    return (f"너는 딸딸기튜브 AV 해설영상 작가다. 아래 작품의 일본어 자막을 보고 '스토리 핵심만' 골라 "
            f"**약 {target_sec}초 내외 하이라이트 영상**으로 압축하고, 한글 대사자막과 해설 내레이션을 만든다.\n"
            f"[메타]\n{_meta_block(meta)}\n[일본어자막] 번호\\t시작초\\t끝초\\t대사\n{body}\n{_style()}\n"
            f"[규칙] (1)신음·짧은탄성·반복감탄·비스토리 섹스대사·무음/잡담·중복은 버린다. "
            f"(2)스토리(설정·관계·전환·갈등·결말)를 드러내는 핵심 구간만 keep으로 골라 **합쳐서 {target_sec}초 ±20% 목표**. "
            f"(3)도입~결말 흐름이 보이게 고루 분포. 시간은 원본 영상 기준 초.\n"
            f"[출력 JSON만] {{\"summary\":\"3~5줄\",\"stars\":1~5,\"keep\":[[시작,끝],...],"
            f"\"dialogue\":[{{\"start\":초,\"end\":초,\"ko\":\"\"}}],\"narration\":[{{\"start\":초,\"end\":초,\"text\":\"\"}}]}}")


def prompt_manual(meta, segs, target_sec=60):
    """수동: 정사장면은 이미 내가 제외함. LLM은 남은 영상에서 스토리 핵심만 골라
    ~target_sec로 압축(무음·잡담 제거) + 번역 + 내레이션."""
    body = "\n".join(f"{k}\t{a:.2f}\t{b:.2f}\t{t}" for k, (a, b, t) in enumerate(segs, 1))
    return (f"너는 딸딸기튜브 AV 해설영상 작가다. 아래는 '정사장면을 이미 제거한' 영상의 일본어 자막이다. "
            f"여기서 **스토리 핵심만 골라 약 {target_sec}초 내외로 압축**하고, 한글 대사자막과 해설 내레이션을 만든다.\n"
            f"[메타]\n{_meta_block(meta)}\n[일본어자막] 번호\\t시작초\\t끝초\\t대사\n{body}\n{_style()}\n"
            f"[규칙] (1)무음·잡담·반복·의미없는 짧은 라인은 버린다. "
            f"(2)스토리(설정·관계·전환·갈등·결말)를 드러내는 핵심 구간만 keep으로 골라 **합쳐서 {target_sec}초 ±20% 목표**. "
            f"(3)정사 선별은 하지 말 것(이미 제거됨). 시간은 이 자막 기준 초.\n"
            f"[출력 JSON만] {{\"summary\":\"3~5줄\",\"stars\":1~5,\"keep\":[[시작,끝],...],"
            f"\"dialogue\":[{{\"start\":초,\"end\":초,\"ko\":\"\"}}],\"narration\":[{{\"start\":초,\"end\":초,\"text\":\"\"}}]}}")


# ─── ④ 컷 / 재타이밍 ─────────────────────────────────────────────────────────
def retime(entries, keep, snap=False, default_dur=4.0):
    keep = sorted(keep); offs, acc = [], 0.0
    for a, b in keep: offs.append(acc); acc += (b - a)
    total = acc; out = []
    for s, e, t in entries:
        placed = False
        for (a, b), off in zip(keep, offs):
            if s >= a - 0.05 and s < b + 0.05:
                ns = off + max(0.0, s - a); ne = off + min(b - a, e - a)
                if ne <= ns: ne = ns + 0.5
                out.append((ns, ne, t)); placed = True; break
        if placed or not snap: continue
        if s < keep[0][0]: ns = 0.0
        else:
            ns = total
            for (a, b), off in zip(keep, offs):
                if s < a: ns = off; break
        ne = min(total, ns + (e - s if e > s else default_dur))
        if ne <= ns: ne = min(total, ns + default_dur)
        out.append((ns, ne, t))
    out.sort(key=lambda x: x[0]); return out


def cut_video(video, keep, out_path, log):
    keep = sorted(keep)
    log(f"ffmpeg 컷: {len(keep)}구간 이어붙이기 (재인코딩)...")
    filt = []
    for i, (a, b) in enumerate(keep):
        filt.append(f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[v{i}];")
        filt.append(f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{i}];")
    n = len(keep)
    fc = "".join(filt) + "".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
    subprocess.run(["ffmpeg", "-y", "-i", video, "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", out_path], check=True)
    log(f"컷 완료: {out_path}")


# ─── GUI ─────────────────────────────────────────────────────────────────────
class App:
    def __init__(self, root):
        self.root = root; self.cfg = load_cfg(); self.q = queue.Queue()
        self.video = None; self.result = None; self.excludes = []
        self.vlc = None; self.player = None
        root.title("딸딸기 스튜디오"); root.geometry("900x720")

        top = ttk.Frame(root, padding=8); top.pack(fill="x")
        ttk.Button(top, text="영상 선택", command=self.pick).pack(side="left")
        self.vlbl = ttk.Label(top, text="(영상 없음)"); self.vlbl.pack(side="left", padx=8)
        ttk.Label(top, text="품번:").pack(side="left")
        self.code = ttk.Entry(top, width=13); self.code.pack(side="left", padx=4)
        ttk.Label(top, text="LLM:").pack(side="left")
        self.llm = ttk.Combobox(top, width=8, values=["claude", "codex"], state="readonly")
        self.llm.set(self.cfg.get("llm", "claude")); self.llm.pack(side="left", padx=4)

        cfgf = ttk.Frame(root, padding=(8, 0)); cfgf.pack(fill="x")
        ttk.Label(cfgf, text="메타API:").pack(side="left")
        self.api = ttk.Entry(cfgf, width=26); self.api.insert(0, self.cfg["meta_api"]); self.api.pack(side="left", padx=4)
        ttk.Label(cfgf, text="Whisper:").pack(side="left")
        self.wm = ttk.Entry(cfgf, width=11); self.wm.insert(0, self.cfg["whisper_model"]); self.wm.pack(side="left", padx=4)
        ttk.Label(cfgf, text="출력:").pack(side="left")
        self.outd = ttk.Entry(cfgf, width=18); self.outd.insert(0, self.cfg["out_dir"]); self.outd.pack(side="left", padx=4)
        ttk.Label(cfgf, text="목표길이(초):").pack(side="left")
        self.tgt = ttk.Entry(cfgf, width=5); self.tgt.insert(0, str(self.cfg.get("target_sec", 60))); self.tgt.pack(side="left", padx=4)

        nb = ttk.Notebook(root); nb.pack(fill="both", expand=True, padx=8, pady=6)
        # ── 수동 탭 ──
        manual = ttk.Frame(nb, padding=6); nb.add(manual, text="수동 (정사장면 직접 제외)")
        self.video_frame = tk.Frame(manual, bg="black", height=240); self.video_frame.pack(fill="x")
        pc = ttk.Frame(manual); pc.pack(fill="x", pady=4)
        ttk.Button(pc, text="▶/⏸", command=self.toggle_play).pack(side="left")
        self.seek = ttk.Scale(pc, from_=0, to=1000, command=self.on_seek); self.seek.pack(side="left", fill="x", expand=True, padx=6)
        self.tpos = ttk.Label(pc, text="00:00:00"); self.tpos.pack(side="left")
        mk = ttk.Frame(manual); mk.pack(fill="x", pady=2)
        ttk.Button(mk, text="제외 시작 ◀", command=self.mark_start).pack(side="left")
        ttk.Button(mk, text="제외 끝 ▶", command=self.mark_end).pack(side="left", padx=4)
        ttk.Label(mk, text="  또는 텍스트 입력(12:30-18:00, 45:00-52:00):").pack(side="left")
        self.exq = ttk.Entry(mk); self.exq.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(mk, text="추가", command=self.add_text_ranges).pack(side="left")
        self.exlist = tk.Listbox(manual, height=4); self.exlist.pack(fill="x", pady=2)
        exb = ttk.Frame(manual); exb.pack(fill="x")
        ttk.Button(exb, text="선택삭제", command=self.del_range).pack(side="left")
        ttk.Button(exb, text="전체삭제", command=self.clear_ranges).pack(side="left", padx=4)
        ttk.Button(exb, text="● 수동 제작 (이 구간들 빼고)", command=self.run_manual).pack(side="right")
        self._mark_start = None

        # ── 자동 탭 ──
        auto = ttk.Frame(nb, padding=6); nb.add(auto, text="자동 (LLM이 알아서)")
        ttk.Label(auto, text="LLM이 풀 자막을 보고 스토리/정사 구간을 자동 선정합니다. (토큰 많이 씀)").pack(anchor="w")
        ttk.Button(auto, text="● 자동! (LLM 분석)", command=self.run_auto).pack(anchor="w", pady=6)
        ttk.Label(auto, text="분석 후 아래 미리보기에서 확인/수정 → 확정").pack(anchor="w")
        self.preview = scrolledtext.ScrolledText(auto, height=12); self.preview.pack(fill="both", expand=True, pady=4)
        self.confirm_btn = ttk.Button(auto, text="확정 → 컷 & SRT 생성", command=self.confirm_auto, state="disabled")
        self.confirm_btn.pack(anchor="e")

        self.log = scrolledtext.ScrolledText(root, height=8); self.log.pack(fill="x", padx=8, pady=(0, 6))
        self.root.after(120, self.pump)
        self._init_vlc()
        self.root.after(300, self._tick)

    # ── VLC ──
    def _init_vlc(self):
        try:
            import vlc
            self.vlc = vlc.Instance(); self.player = self.vlc.media_player_new()
            self.logln("플레이어(VLC) 준비됨.")
        except Exception:
            self.logln("※ VLC/python-vlc 없음 → 수동은 '텍스트 입력'으로만. (pip install python-vlc + VLC 설치 시 영상 마킹 가능)")

    def _attach_video(self):
        if not self.player: return
        try:
            wid = self.video_frame.winfo_id()
            if os.name == "nt": self.player.set_hwnd(wid)
            else: self.player.set_xwindow(wid)
        except Exception: pass

    def toggle_play(self):
        if not self.player: return
        if self.player.is_playing(): self.player.pause()
        else: self.player.play()

    def on_seek(self, v):
        if self.player and self.player.get_length() > 0 and getattr(self, "_user_seek", True):
            self.player.set_time(int(float(v) / 1000 * self.player.get_length()))

    def _tick(self):
        if self.player and self.player.get_length() > 0:
            cur = self.player.get_time(); ln = self.player.get_length()
            self._user_seek = False
            try: self.seek.set(cur / ln * 1000)
            except Exception: pass
            self._user_seek = True
            self.tpos.config(text=hhmmss(cur / 1000))
        self.root.after(300, self._tick)

    def cur_sec(self):
        return (self.player.get_time() / 1000) if (self.player and self.player.get_time() >= 0) else 0.0

    def mark_start(self): self._mark_start = self.cur_sec(); self.logln(f"제외 시작: {hhmmss(self._mark_start)}")
    def mark_end(self):
        if self._mark_start is None: return messagebox.showinfo("", "먼저 '제외 시작'을 누르세요.")
        a, b = self._mark_start, self.cur_sec()
        if b <= a: return messagebox.showinfo("", "끝이 시작보다 뒤여야 합니다.")
        self.excludes.append((a, b)); self._mark_start = None; self._refresh_ex()

    def add_text_ranges(self):
        rs = ranges_from_text(self.exq.get())
        if not rs: return messagebox.showinfo("", "형식: 12:30-18:00, 45:00-52:00")
        self.excludes += rs; self.exq.delete(0, "end"); self._refresh_ex()

    def _refresh_ex(self):
        self.excludes = sorted(self.excludes); self.exlist.delete(0, "end")
        for a, b in self.excludes: self.exlist.insert("end", f"{hhmmss(a)} ~ {hhmmss(b)}  (제외)")

    def del_range(self):
        sel = list(self.exlist.curselection())
        for i in reversed(sel): del self.excludes[i]
        self._refresh_ex()

    def clear_ranges(self): self.excludes = []; self._refresh_ex()

    # ── 공통 ──
    def logln(self, s): self.q.put(("log", s))
    def pump(self):
        try:
            while True:
                k, v = self.q.get_nowait()
                if k == "log": self.log.insert("end", v + "\n"); self.log.see("end")
                elif k == "preview":
                    self.preview.delete("1.0", "end"); self.preview.insert("1.0", v)
                    self.confirm_btn.config(state="normal")
                elif k == "done": messagebox.showinfo("완료", v)
                elif k == "err": messagebox.showerror("오류", v)
        except queue.Empty: pass
        self.root.after(120, self.pump)

    def pick(self):
        f = filedialog.askopenfilename(filetypes=[("영상", "*.mp4 *.mkv *.avi *.mov *.wmv"), ("모든", "*.*")])
        if not f: return
        self.video = f; self.vlbl.config(text=Path(f).name)
        if self.player:
            self._attach_video()
            self.player.set_media(self.vlc.media_new(f))

    def save_settings(self):
        try: tsec = int(float(self.tgt.get().strip()))
        except Exception: tsec = 60
        self.cfg.update({"meta_api": self.api.get().strip(), "llm": self.llm.get(),
                         "whisper_model": self.wm.get().strip(), "out_dir": self.outd.get().strip(),
                         "target_sec": tsec})
        save_cfg(self.cfg)

    def _ready(self):
        if not self.video: messagebox.showwarning("", "영상을 선택하세요."); return False
        if not self.code.get().strip(): messagebox.showwarning("", "품번을 입력하세요."); return False
        self.save_settings(); return True

    # ── 수동 ──
    def run_manual(self):
        if not self._ready(): return
        if not self.excludes: return messagebox.showwarning("", "제외할 정사장면 구간을 하나 이상 추가하세요.")
        threading.Thread(target=self._manual, daemon=True).start()

    def _manual(self):
        try:
            code = self.code.get().strip(); outdir = Path(self.cfg["out_dir"]); outdir.mkdir(parents=True, exist_ok=True)
            tsec = int(self.cfg.get("target_sec", 60))
            total = video_duration(self.video)
            keep1 = keep_from_exclude(total, self.excludes)            # 정사 제외 = 1차 keep
            if not keep1: raise RuntimeError("남는 구간이 없습니다.")
            story_path = str(outdir / f"{code}_story.mp4")
            cut_video(self.video, keep1, story_path, self.logln)       # ① 정사장면 제거 컷
            segs = transcribe(story_path, self.cfg["whisper_model"], self.logln)   # ② 남은 영상 전사
            meta = fetch_meta(self.cfg["meta_api"], code, self.logln)             # ③
            res = call_llm(prompt_manual(meta, segs, tsec), self.cfg["llm"], self.logln)  # ④ 스토리 압축+번역+내레이션
            keep2 = [(float(a), float(b)) for a, b in res.get("keep", [])]         # LLM이 고른 핵심 구간
            if not keep2: raise RuntimeError("LLM이 keep 구간을 못 골랐습니다. 미리보기/재시도 필요.")
            final_path = str(outdir / f"{code}_final.mp4")
            cut_video(story_path, keep2, final_path, self.logln)                  # ⑤ 핵심만 재컷(~목표초)
            dlg = [(float(d["start"]), float(d["end"]), d["ko"]) for d in res.get("dialogue", [])]
            nar = [(float(d["start"]), float(d["end"]), d["text"]) for d in res.get("narration", [])]
            write_srt(retime(dlg, keep2, snap=False), outdir / f"{code}_대사.srt")
            write_srt(retime(nar, keep2, snap=True), outdir / f"{code}_내레이션.srt")
            fdur = video_duration(final_path)
            self.logln(f"완료 → {outdir} (최종 {fdur:.0f}초)")
            self.q.put(("done", f"[수동] 출력 완료 (최종 {fdur:.0f}초, 목표 {tsec})\n{final_path}\n{code}_대사.srt\n{code}_내레이션.srt\n\n요약: {res.get('summary','')[:120]}"))
        except Exception as e:
            self.q.put(("err", f"{type(e).__name__}: {e}"))

    # ── 자동 ──
    def run_auto(self):
        if not self._ready(): return
        self.confirm_btn.config(state="disabled")
        threading.Thread(target=self._auto, daemon=True).start()

    def _auto(self):
        try:
            segs = transcribe(self.video, self.cfg["whisper_model"], self.logln)
            meta = fetch_meta(self.cfg["meta_api"], self.code.get().strip(), self.logln)
            res = call_llm(prompt_auto(meta, segs, int(self.cfg.get("target_sec", 60))), self.cfg["llm"], self.logln)
            self.result = res
            self.q.put(("preview", json.dumps(res, ensure_ascii=False, indent=2)))
            self.logln("자동 분석 완료 — 미리보기 확인/수정 후 [확정].")
        except Exception as e:
            self.q.put(("err", f"{type(e).__name__}: {e}"))

    def confirm_auto(self):
        try: res = json.loads(self.preview.get("1.0", "end").strip())
        except Exception as e: return messagebox.showerror("JSON 오류", str(e))
        self.confirm_btn.config(state="disabled")
        threading.Thread(target=self._auto_render, args=(res,), daemon=True).start()

    def _auto_render(self, res):
        try:
            code = self.code.get().strip(); outdir = Path(self.cfg["out_dir"]); outdir.mkdir(parents=True, exist_ok=True)
            keep = [(float(a), float(b)) for a, b in res.get("keep", [])]
            if not keep: raise RuntimeError("keep 구간 없음")
            cut_path = str(outdir / f"{code}_cut.mp4")
            cut_video(self.video, keep, cut_path, self.logln)
            dlg = [(float(d["start"]), float(d["end"]), d["ko"]) for d in res.get("dialogue", [])]
            nar = [(float(d["start"]), float(d["end"]), d["text"]) for d in res.get("narration", [])]
            write_srt(retime(dlg, keep, snap=False), outdir / f"{code}_대사.srt")
            write_srt(retime(nar, keep, snap=True), outdir / f"{code}_내레이션.srt")
            self.q.put(("done", f"[자동] 출력 완료\n{cut_path}\n{code}_대사.srt\n{code}_내레이션.srt"))
        except Exception as e:
            self.q.put(("err", f"{type(e).__name__}: {e}"))


if __name__ == "__main__":
    root = tk.Tk(); App(root); root.mainloop()
