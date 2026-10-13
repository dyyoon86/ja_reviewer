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


def breaker_stop(code=""):
    """편별 루프의 except 에서 호출: LLM 차단기가 내려가 있으면 True(→ break). 남은 편을
    하나하나 실패시키며 지나가지 말고 바로 멈춰서 run_ja 가 사람에게 묻게 한다."""
    try:
        from server.core import llm
        b = llm.breaker_status()
    except Exception:
        return False
    if not b:
        return False
    print(f"\n★LLM 차단기 내려감 → 배치 중단({code}): {b.get('reason')} — {b.get('detail')}", flush=True)
    return True
