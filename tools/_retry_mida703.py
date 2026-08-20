# -*- coding: utf-8 -*-
"""MIDA-703 ②AI 재시도 — 시각브리핑 없이.

배치에서 이 편만 codex가 두 번 다 빈 응답을 냈다(다른 12편은 같은 설정으로 통과).
시각브리핑에 '여러 남성에게 둘러싸인', '남성 1명 뒤에서 접촉' 같은 서술이 들어가
프롬프트가 거부에 걸린 것으로 의심 — 브리핑만 빼고 같은 조건으로 다시 돌려 본다.
전사는 캐시되어 있어 ②AI만 재실행된다.

사용: .venv\\Scripts\\python.exe tools\\_retry_mida703.py [--visual]
"""
import argparse
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

CODE = "MIDA-703"
OUT = r"C:\Users\yoon\ja_reviewer_out\ja19"
META = "http://127.0.0.1:8770"


class Em(stages.Emitter):
    def __init__(self):
        self.t0 = time.time()

    def _p(self, m):
        print(f"[{time.time() - self.t0:6.0f}s] {m}", flush=True)

    def log(self, m):
        self._p(m)

    def step(self, n, t, label):
        self._p(f"── ({n}/{t}) {label}")

    def prog(self, frac, label=None):
        pass

    def file(self, tag, path):
        self._p(f"   ▸ {tag}: {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--visual", action="store_true", help="시각브리핑 켜고 재시도(기본은 끔)")
    ap.add_argument("--target", type=int, default=120)
    args = ap.parse_args()

    cfg = _common.load_cfg()
    cfg["out_dir"] = OUT
    cfg["meta_api"] = META
    em = Em()
    outdir = stages.work_dir(cfg, CODE)
    st = stages.load_state(outdir, CODE)
    video = st.get("video") or str(outdir / f"{CODE}_클린.mp4")
    em.log(f"입력: {video}")
    em.log(f"시각브리핑: {'ON' if args.visual else 'OFF'} / target={args.target}s")

    stages.stage_ai(cfg, CODE, video, args.target, cfg["llm"],
                    cfg.get("fullauto_mode", "highlight"), "", em,
                    gpu=NullLock(), pos="solo", style="3min",
                    visual_brief=bool(args.visual))
    stages.stage_subs(cfg, CODE, em)

    plan = json.loads((outdir / f"{CODE}_plan.json").read_text(encoding="utf-8"))
    keep = P.parse_keep(plan.get("keep", []))
    em.log(f"✔ keep {len(keep)}구간 {sum(b - a for a, b in keep):.0f}s / "
           f"내레이션 {len(plan.get('narration', []))}줄 / 대사 {len(plan.get('dialogue', []))}줄")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
