# -*- coding: utf-8 -*-
"""구타바리형 문체 첫 실물 테스트 — SOAV-139 원본 한 편으로 ①전사 → ②AI → ③자막.

batch_review.py 와 같은 흐름이되 (a) 클린본이 아니라 **원본 mp4**를 그대로 넣고
(정사 제거는 ②의 노출지도 스캔이 처리한다) (b) 문체를 style='gootabari'로 준다.
사용: .venv\\Scripts\\python.exe tools\\_goota_soav139.py
"""
import json
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server import pipeline as P
from server.stages import NullLock

CODE = "SOAV-139"
VIDEO = r"C:\Users\yoon\Downloads\SOAV-139-츠키노에 스이.mp4"
TARGET = 90          # 구타바리는 초압축 문체라 첫 실물은 짧게(90초) 잡는다
MODE = "highlight"   # 알파컷식 — 후킹 밀도 우선
STYLE = "gootabari"


class Em(stages.Emitter):
    def __init__(self):
        self.t0 = time.time()

    def _p(self, msg):
        print(f"[{time.time() - self.t0:6.0f}s] {msg}", flush=True)

    def log(self, msg):
        self._p(msg)

    def step(self, n, total, label):
        self._p(f"── ({n}/{total}) {label}")

    def prog(self, frac, label=None):
        pass

    def file(self, tag, path):
        self._p(f"   ▸ {tag}: {path}")


def main():
    cfg = _common.load_cfg()
    em = Em()
    outdir = stages.work_dir(cfg, CODE)
    em.log(f"출력 폴더: {outdir}")
    em.log(f"원본: {VIDEO}")

    # ① 전사 — 메타로 initial_prompt 힌트(인명·설정을 알려주면 환청이 줄어든다)
    if stages.transcribe_fresh(outdir, CODE, VIDEO):
        em.log("① 전사 이미 있음(같은 영상) — 재사용")
    else:
        init = None
        try:
            m = P.fetch_meta(cfg["meta_api"], CODE, em.log)
            init = P.build_initial_prompt(m) or None
        except Exception as e:
            em.log(f"※ 메타 조회 실패({e}) → 힌트 없이 전사 진행")
        stages.stage_transcribe(cfg, CODE, VIDEO, cfg["whisper_model"], em,
                                initial_prompt=init)

    # ② AI — 구타바리형
    stages.stage_ai(cfg, CODE, VIDEO, TARGET, cfg["llm"], MODE, "", em,
                    gpu=NullLock(), pos="solo", style=STYLE)

    # ③ 자막(대사·내레이션 SRT/JSON)
    stages.stage_subs(cfg, CODE, em)

    plan = json.loads((outdir / f"{CODE}_plan.json").read_text(encoding="utf-8"))
    keep = P.parse_keep(plan.get("keep", []))
    nar = plan.get("narration", [])
    drips = [n for n in nar if n.get("style") == "드립"]
    em.log(f"✔ 완료 — keep {len(keep)}구간 {sum(b - a for a, b in keep):.0f}s / "
           f"내레이션 {len(nar) - len(drips)}줄 + 드립 {len(drips)}개")
    print("\n" + "=" * 70)
    for n in nar:
        print(f"{n['start']:7.1f}~{n['end']:6.1f} [{n.get('style', '기본')}] {n['text']}")
    print("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
