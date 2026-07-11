#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""내레이션 재생성 CLI — 로직은 server/core/regen.py (GUI ③ 버튼과 공유).

사용:
    python tools/regen_narration.py C:/Users/yoon/ja_reviewer_out/SNOS-285
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import _common
from server.core.regen import regen_narration

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용: python regen_narration.py <출력폴더>"); sys.exit(1)
    regen_narration(Path(sys.argv[1]), _common.load_cfg()["meta_api"])
