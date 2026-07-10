#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""작업 큐 매니저 — 여러 품번을 병렬 자동 처리.

핵심 설계:
- 아이템 1개 = 영상 1개(품번 1개). 활성화된 스테이지(전사→AI→자막→TTS→번인)를 순서대로 실행.
- 리소스 레인: GPU(전사·컷·번인)=기본 1, AI(LLM 호출)=기본 2, TTS(voicebox)=1.
  → A가 GPU로 전사하는 동안 B는 LLM 처리 가능. VRAM 충돌 없음.
- 완료 판정은 결과 파일 존재(stages.steps_status) → 서버 재시작 후에도 중간부터 재개.
- 큐는 {out_dir}/queue.json 에 영속화.
- 상태: queued(대기) → running(진행) → review(검수대기) / done(완료) / error(오류) / held(일시정지)
  · review = 활성 스테이지는 다 끝났지만 번인까지는 안 간 상태(사람 검수 후 이어서).
"""
import json
import threading
import time
import uuid
from pathlib import Path

from . import stages as S

STAGE_ORDER = ["transcribe", "ai", "subs", "banner", "tts", "burn"]
STAGE_LABEL = {"transcribe": "① 전사", "ai": "② AI 처리", "subs": "③ 자막",
               "banner": "④ 배너", "tts": "⑤ TTS", "burn": "⑥ 자막번인"}
# 스테이지 → 리소스 레인 (ai는 LLM 호출이 주업이라 ai 레인, 내부 컷만 gpu 세마포어로 감쌈)
# banner는 HTML→PNG 렌더라 인코딩이 없어 레인 불필요
STAGE_LANE = {"transcribe": "gpu", "ai": "ai", "subs": None, "banner": None,
              "tts": "tts", "burn": "gpu"}
LOG_KEEP = 40      # 아이템별 로그 보관 줄 수
MAX_ITEM_WORKERS = 4


class _ItemEmitter(S.Emitter):
    def __init__(self, mgr, item):
        self.mgr, self.item = mgr, item

    def log(self, msg):
        lg = self.item.setdefault("log", [])
        lg.append(str(msg))
        del lg[:-LOG_KEEP]
        self.mgr._touch(save=False)

    def step(self, n, total, label):
        self.item["stage_label"] = f"{n}/{total} · {label}"
        self.item["progress"] = (n - 1) / total if total else 0
        self.log(f"[{n}/{total}] {label}")

    def prog(self, frac, label=None):
        self.item["progress"] = float(frac or 0)
        if label:
            self.item["stage_label"] = f"{label} {int((frac or 0) * 100)}%"
        self.mgr._touch(save=False)

    def file(self, tag, path):
        self.log(f"✔ {tag}: {path}")


class QueueManager:
    def __init__(self, load_cfg):
        self.load_cfg = load_cfg
        self.items = []
        self.version = 0
        self.cond = threading.Condition()
        self._lanes = {}          # name -> (size, Semaphore)
        self._workers = threading.Semaphore(MAX_ITEM_WORKERS)
        self._running_ids = set()
        self._load()
        threading.Thread(target=self._dispatch_loop, daemon=True).start()

    # ── 영속화 ──────────────────────────────────────────────────────────────
    def _qfile(self):
        c = self.load_cfg()
        d = Path(c["out_dir"]); d.mkdir(parents=True, exist_ok=True)
        return d / "queue.json"

    def _load(self):
        try:
            data = json.loads(self._qfile().read_text(encoding="utf-8"))
            for it in data:
                if it.get("status") == "running":   # 재시작 → 다시 대기(완료 스테이지는 파일로 스킵됨)
                    it["status"] = "queued"
                    it["stage_label"] = "재시작 대기"
            self.items = data
        except Exception:
            self.items = []

    def _save(self):
        try:
            self._qfile().write_text(
                json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    def _touch(self, save=True):
        with self.cond:
            self.version += 1
            self.cond.notify_all()
        if save:
            self._save()

    # ── 레인(리소스 세마포어) — 설정 변경 시 크기 재생성 ────────────────────
    def _lane(self, name):
        if name is None:
            return S.NullLock()
        c = self.load_cfg()
        size = max(1, int(c.get(f"queue_{name}", {"gpu": 1, "ai": 2, "tts": 1}.get(name, 1))))
        cur = self._lanes.get(name)
        if not cur or cur[0] != size:
            self._lanes[name] = (size, threading.BoundedSemaphore(size))
        return self._lanes[name][1]

    # ── 공개 API ────────────────────────────────────────────────────────────
    def snapshot(self):
        c = self.load_cfg()
        return {"version": self.version,
                "lanes": {"gpu": int(c.get("queue_gpu", 1)), "ai": int(c.get("queue_ai", 2))},
                "items": self.items}

    def add(self, videos, pipeline, opts):
        """videos: [{path, code}] — code 없으면 파일명에서 추출 실패한 것(needs_code)."""
        added = []
        for v in videos:
            path = str(v.get("path") or "").strip()
            if not path or not Path(path).is_file():
                continue
            code = (v.get("code") or "").strip().upper()
            it = {"id": uuid.uuid4().hex[:10], "code": code,
                  "video": path, "name": Path(path).name,
                  "status": "queued" if code else "needs_code",
                  "stage": None, "stage_label": "대기 중" if code else "품번 입력 필요",
                  "progress": 0, "error": None, "log": [],
                  "pipeline": {k: bool(pipeline.get(k)) for k in STAGE_ORDER},
                  "opts": dict(opts or {}), "added": time.time()}
            self.items.append(it)
            added.append(it["id"])
        self._touch()
        return added

    def _find(self, iid):
        for it in self.items:
            if it["id"] == iid:
                return it
        return None

    def set_code(self, iid, code):
        it = self._find(iid)
        if it and code.strip():
            it["code"] = code.strip().upper()
            if it["status"] == "needs_code":
                it["status"] = "queued"; it["stage_label"] = "대기 중"
            self._touch()

    def hold(self, iid):
        it = self._find(iid)
        if it and it["status"] in ("queued", "running", "review", "error"):
            it["status"] = "held"
            # running 중이면 현재 스테이지는 마저 끝내고 다음 스테이지 전에 멈춤
            it["stage_label"] = "일시정지" + (" (현재 단계 후 중단)" if it.get("stage") else "")
            self._touch()

    def resume(self, iid):
        """held/error/review → 다시 대기로(완료 스테이지는 파일 기준 스킵)."""
        it = self._find(iid)
        if it and it["status"] in ("held", "error", "review", "done"):
            it["status"] = "queued"; it["error"] = None; it["stage_label"] = "대기 중"
            self._touch()

    def remove(self, iid):
        it = self._find(iid)
        if it and it["status"] != "running":
            self.items.remove(it)
            self._touch()
            return True
        return False

    def clear_finished(self):
        self.items = [it for it in self.items
                      if it["status"] not in ("done", "review", "error")]
        self._touch()

    def wait_version(self, since, timeout=25.0):
        with self.cond:
            if self.version != since:
                return self.version
            self.cond.wait(timeout)
            return self.version

    # ── 디스패처/워커 ───────────────────────────────────────────────────────
    def _dispatch_loop(self):
        while True:
            try:
                for it in list(self.items):
                    if it["status"] == "queued" and it["id"] not in self._running_ids:
                        if self._workers.acquire(blocking=False):
                            self._running_ids.add(it["id"])
                            threading.Thread(target=self._run_item, args=(it,), daemon=True).start()
            except Exception:
                pass
            time.sleep(0.5)

    def _run_item(self, it):
        em = _ItemEmitter(self, it)
        try:
            c = self.load_cfg()
            it["status"] = "running"; it["error"] = None
            self._touch()
            code = it["code"]
            outdir = S.work_dir(c, code)
            enabled = [s for s in STAGE_ORDER if it["pipeline"].get(s)]
            for stg in enabled:
                if it.get("status") == "held":   # 실행 중 hold 요청 → 다음 스테이지 전에 멈춤
                    em.log("⏸ 일시정지 요청 → 다음 스테이지 전에 중단")
                    self._touch()
                    return
                done = S.steps_status(outdir, code)
                if done.get(stg):
                    em.log(f"{STAGE_LABEL[stg]} 이미 완료 → 건너뜀")
                    continue
                it["stage"] = stg
                lane_name = STAGE_LANE[stg]
                lane = self._lane(lane_name)
                if lane_name:
                    it["stage_label"] = f"{STAGE_LABEL[stg]} — {lane_name.upper()} 차례 대기"
                    self._touch(save=False)
                with lane:
                    it["stage_label"] = f"{STAGE_LABEL[stg]} 시작"
                    it["progress"] = 0
                    self._touch(save=False)
                    self._exec_stage(c, it, stg, em)
                em.log(f"✔ {STAGE_LABEL[stg]} 완료")
                self._touch()
            # 활성 스테이지 전부 완료 → 번인까지 갔으면 done, 아니면 검수대기
            it["stage"] = None; it["progress"] = 1
            if it["pipeline"].get("burn"):
                it["status"] = "done"; it["stage_label"] = "✓ 완료"
            else:
                it["status"] = "review"; it["stage_label"] = "✋ 검수대기 — 클릭해서 확인"
        except Exception as e:
            it["status"] = "error"; it["error"] = str(e)
            it["stage_label"] = f"✗ 오류: {str(e)[:60]}"
            em.log(f"✖ 오류: {e}")
        finally:
            self._running_ids.discard(it["id"])
            self._workers.release()
            self._touch()

    def _resolve_pos(self, it, pos, em=None):
        """묶음 리뷰에서 이 작품이 첫/중간/마지막 꼭지인지 결정.
        pos='auto'면 큐에 담긴 순서(추가순)로 판단한다 — 큐가 곧 영상의 편집 순서.
        아이템이 하나뿐이면 'first'(전환 문구만 붙고 아웃트로는 없음).
        수동 지정(first/mid/last)이면 그대로 존중.

        ※ 판정은 ② AI 처리가 '실행되는 시점'의 큐 상태 기준이다. 따라서
          · 도중에 아이템을 추가/삭제하거나 '완료 정리'를 누르면 순서가 바뀔 수 있다.
          · 10개를 한 번에 넣고 돌리는 게 안전하다. 나눠 넣을 거면 수동 지정을 쓸 것."""
        if pos in ("first", "mid", "last"):
            return pos
        ids = [x["id"] for x in self.items if (x.get("code") or "").strip()]
        if it["id"] not in ids:
            return "mid"
        i, n = ids.index(it["id"]), len(ids)
        out = "first" if i == 0 else ("last" if i == n - 1 else "mid")
        if em:
            label = {"first": "먼저", "mid": "다음은", "last": "마지막으로"}[out]
            em.log(f"묶음 위치 자동판정: {i + 1}/{n}번째 → '{label} …' ({out})")
        return out

    def _exec_stage(self, c, it, stg, em):
        code, video = it["code"], it["video"]
        o = it.get("opts") or {}
        if stg == "transcribe":
            # 원샷 리뷰(/review)처럼 메타를 Whisper 힌트로 활용 — 실패해도 전사는 계속
            init = None
            try:
                m = S.P.fetch_meta(c["meta_api"], code, em.log)
                init = "。".join(x for x in [S.P.build_initial_prompt(m), o.get("hint", "")] if x) or None
            except Exception as e:
                em.log(f"※ 메타 조회 실패({e}) → 힌트 없이 전사 진행")
            S.stage_transcribe(c, code, video, o.get("model", c["whisper_model"]), em,
                               initial_prompt=init)
        elif stg == "ai":
            pos = self._resolve_pos(it, o.get("pos", "auto"), em)
            S.stage_ai(c, code, video, int(o.get("target_sec", c["target_sec"])),
                       o.get("llm", c["llm"]), o.get("mode", "summary"),
                       (o.get("hint") or "").strip(), em, gpu=self._lane("gpu"),
                       pos=pos)
        elif stg == "subs":
            S.stage_subs(c, code, em)
        elif stg == "banner":
            S.stage_banner(c, code, em, float(o.get("hold", 2.0)))
        elif stg == "tts":
            profile = o.get("tts_profile") or c.get("tts_profile")
            if not profile:
                raise RuntimeError("TTS 보이스(profile) 미선택 — 큐 추가 전에 보이스를 고르세요.")
            S.stage_tts(c, code, o.get("tts_base") or c["tts_base"], profile,
                        o.get("tts_language", c["tts_language"]), o.get("tts_seed"),
                        bool(o.get("tts_mux", True)), em)
        elif stg == "burn":
            S.stage_burn(c, code, c.get("sub_styles") or S.P.STYLE_DEFAULT, em)
