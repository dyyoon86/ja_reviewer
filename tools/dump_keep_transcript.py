# -*- coding: utf-8 -*-
"""각 품번의 현재 plan.keep 구간을 large-v3로 정밀 전사 → {code}_keep전사.json 저장.
   (자막 전체 채우기용 — 이 JSON을 번역해 plan.dialogue를 교체한다.)"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")
import _common  # noqa
from server import pipeline as P
from server import stages
from server.core import transcribe as T

codes = [c for c in sys.argv[1:] if c]
cfg = _common.load_cfg()
for code in codes:
    outdir = Path(cfg["out_dir"]) / code
    plan = json.loads((outdir / f"{code}_plan.json").read_text(encoding="utf-8"))
    keep = P.parse_keep(plan["keep"])
    st = stages.load_state(outdir, code)
    src = st.get("video")
    print(f"\n=== {code}  keep {len(keep)}개  src={src}", flush=True)
    segs = T.transcribe_ranges(src, keep, model_name="large-v3", log=lambda *a: None)
    data = [{"start": round(s, 2), "end": round(e, 2), "ja": t} for s, e, t in segs]
    (outdir / f"{code}_keep전사.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  {len(data)}개 세그먼트 저장 → {code}_keep전사.json")
print("\n완료")
