# -*- coding: utf-8 -*-
"""tools/ 공용 — repo 루트를 sys.path에 등록하고 studio_config.json을 읽는다.

각 툴은 `import _common` 후 `_common.load_cfg()`로 서버와 동일한 설정
(meta_api, out_dir, llm, tts_*)을 공유한다. 하드코딩 금지.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_DEFAULTS = {
    "meta_api": "http://172.30.1.40:8770",
    "out_dir": str(Path.home() / "ja_reviewer_out"),
    "llm": "claude",
    "target_sec": 60,
    "tts_base": "http://127.0.0.1:17493",
    "tts_profile": "",
    "tts_language": "ko",
}


def load_cfg():
    c = dict(_DEFAULTS)
    try:
        c.update(json.loads((ROOT / "studio_config.json").read_text(encoding="utf-8")))
    except Exception:
        pass
    return c
