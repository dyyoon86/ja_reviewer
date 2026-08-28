#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""keep 구간 재선정 CLI — 로직은 server/core/regen.py (GUI ③ 버튼과 공유).

plan.json의 keep을 LLM으로 다시 고르고 final.mp4를 재컷한다.
(SRT 재타이밍은 GUI ③ 자막 단계 또는 /step/subs 로 다시 실행)

사용:
    python tools/replan.py F:/ja_reviewer_out/JUR-088
    python tools/replan.py F:/ja_reviewer_out/JUR-088 --llm claude --target 60
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import _common
from server.core.regen import replan

CFG = _common.load_cfg()

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--llm", default=CFG["llm"])
    ap.add_argument("--target", type=int, default=CFG["target_sec"])
    args = ap.parse_args()
    replan(Path(args.folder), CFG["meta_api"], args.llm, args.target)
