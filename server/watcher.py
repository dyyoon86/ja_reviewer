#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""감시 폴더 → 작업 큐 자동 투입 (풀오토 입구).

config의 watch_enabled/watch_dir를 5초마다 확인. 폴더에 새 영상 파일이 생기고
두 스캔 연속 크기가 같으면(다운로드 완료로 판단) 파일명에서 품번을 추정해
6단계(전사→AI→자막→배너→TTS→번인) 풀오토 프리셋으로 큐에 넣는다.

- 처리한 파일은 {out_dir}/watch_seen.json 에 기록 → 재시작해도 중복 투입 없음.
- 품번 추정 실패 시에도 큐에 넣는다(needs_code — GUI에서 품번만 입력하면 진행).
- 완성본은 stage_burn이 {out_dir}/_완성/ 에 수거한다.
"""
import json
import threading
import time
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts", ".wmv", ".mov"}
SCAN_SEC = 5


class Watcher:
    def __init__(self, load_cfg, queue, guess_code):
        self.load_cfg, self.queue, self.guess_code = load_cfg, queue, guess_code
        self._pending = {}       # path -> 마지막 관측 크기(안정화 감지)
        self._seen = None        # set(path) — 지연 로드
        self.last = {"enabled": False, "dir": "", "added": 0, "waiting": 0}
        threading.Thread(target=self._loop, daemon=True).start()

    # ── 영속화(중복 투입 방지) ───────────────────────────────────────────────
    def _seen_file(self, c):
        d = Path(c["out_dir"]); d.mkdir(parents=True, exist_ok=True)
        return d / "watch_seen.json"

    def _load_seen(self, c):
        if self._seen is None:
            try:
                self._seen = set(json.loads(self._seen_file(c).read_text(encoding="utf-8")))
            except Exception:
                self._seen = set()
        return self._seen

    def _save_seen(self, c):
        try:
            self._seen_file(c).write_text(
                json.dumps(sorted(self._seen), ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def status(self):
        return dict(self.last)

    def _fullauto_opts(self, c):
        """풀오토 프리셋 — 단독(solo) 오프닝 + 1분 + TTS 덕킹 먹싱 + 번인까지.
        GUI 자동 모드 설정(config)에서 길이·방식·LLM을 읽는다."""
        return {"model": c["whisper_model"], "llm": c["llm"],
                "target_sec": int(c.get("target_sec", 60)),
                "mode": c.get("fullauto_mode", "summary"),
                "style": "3min", "pos": "solo", "orig_audio": "duck", "duck_level": 0.3,
                "tts_profile": c.get("tts_profile"), "tts_base": c.get("tts_base"),
                "tts_language": c.get("tts_language", "ko"), "tts_mux": True,
                "fullauto": True}

    # ── 루프 ────────────────────────────────────────────────────────────────
    def _loop(self):
        while True:
            try:
                self._scan()
            except Exception:
                pass
            time.sleep(SCAN_SEC)

    def _scan(self):
        c = self.load_cfg()
        wd = str(c.get("watch_dir") or "").strip()
        enabled = bool(c.get("watch_enabled")) and bool(wd)
        d = Path(wd) if enabled else None
        if not enabled or not d.is_dir():
            self.last = {"enabled": False, "dir": wd,
                         "added": self.last.get("added", 0), "waiting": 0}
            self._pending.clear()
            return
        seen = self._load_seen(c)
        with self.queue._lock:
            in_queue = {str(x.get("video")) for x in self.queue.items}
        waiting = 0
        for f in sorted(d.iterdir()):
            if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
                continue
            p = str(f)
            if p in seen or p in in_queue:
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if size <= 0:
                continue
            if self._pending.get(p) == size:
                # 두 스캔(10초) 연속 같은 크기 → 다운로드 완료 → 풀오토 투입
                code = self.guess_code(f.name)
                self.queue.add(
                    [{"path": p, "code": code}],
                    {k: True for k in ("transcribe", "ai", "subs", "banner", "tts", "burn")},
                    self._fullauto_opts(c))
                seen.add(p); self._save_seen(c)
                self._pending.pop(p, None)
                self.last["added"] = self.last.get("added", 0) + 1
            else:
                self._pending[p] = size
                waiting += 1
        self.last = {"enabled": True, "dir": str(d),
                     "added": self.last.get("added", 0), "waiting": waiting}
