# -*- coding: utf-8 -*-
r"""ja20 — SNOS-360(13위) 을 9번째로 끼워 넣으면서 밀린 뒤 3편의 서수를 고친다.

내레이션 첫 줄에 서수가 **텍스트로** 박힌다("아홉 번째 작품은 …"). 총 편수가
11 → 12 로 늘면 SNOS-360 뒤 편들의 서수가 하나씩 밀린다.

★영상 재번인은 필요 없다 — 이 파이프라인의 번인은 내레이션 자막을 숨기고 굽기 때문에
  (batch_produce.hide_narration) 내레이션 텍스트는 화면에 없다. 바뀌는 것은 사람이
  나중에 조합할 `{code}_내레이션.wav` 뿐이라, 텍스트를 고치고 TTS 만 다시 만든다.

사용: .venv\Scripts\python.exe tools\_fixseq_ja20.py [--dry]
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server import pipeline as P
from batch_clean import CliEmitter
from batch_produce import ensure_voicebox

OUT = r"F:\ja_reviewer_out\ja20"

# 품번 → (옛 서수, 새 서수)
FIXES = [
    ("SNOS-409", "아홉 번째", "열 번째"),
    ("JUR-820",  "열 번째",   "열한 번째"),
    ("SDMM-238", "열한 번째", "열두 번째"),
]


def patch_line0(path, old, new, em):
    """내레이션 json/srt 의 첫 줄 서수만 바꾼다."""
    p = Path(path)
    if not p.is_file():
        return False
    if p.suffix == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        if not data or old not in data[0].get("text", ""):
            return False
        data[0]["text"] = data[0]["text"].replace(old, new, 1)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        s = p.read_text(encoding="utf-8")
        if old not in s:
            return False
        s = s.replace(old, new, 1)     # 첫 등장만
        p.write_text(s, encoding="utf-8")
    em.log(f"  {p.name}: '{old}' → '{new}'")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="텍스트만 고치고 TTS 는 건너뜀")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    cfg["out_dir"] = OUT
    results = []

    for code, old, new in FIXES:
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        print(f"\n{'=' * 70}\n{code}: {old} → {new}", flush=True)
        t0 = time.time()
        try:
            hit = False
            for suf in ("_내레이션.json", "_내레이션.srt"):
                hit |= patch_line0(outdir / f"{code}{suf}", old, new, em)
            if not hit:
                results.append((code, f"— 대상 문구 없음('{old}') — 건너뜀"))
                continue
            if args.dry:
                results.append((code, "✔ 텍스트만 수정(--dry)"))
                continue

            em.log("TTS 재생성 (문장이 바뀐 첫 줄 포함 전체 재합성)…")
            for attempt in (1, 2, 3):
                if not ensure_voicebox(cfg["tts_base"], em.log):
                    raise RuntimeError("voicebox 재기동 실패")
                try:
                    stages.stage_tts(cfg, code, cfg["tts_base"], cfg["tts_profile"],
                                     cfg.get("tts_language", "ko"), cfg.get("tts_seed"), False, em)
                    break
                except Exception as e:
                    if attempt == 3:
                        raise
                    em.log(f"⚠ TTS 실패({e}) — 재시도 {attempt}/2")
            results.append((code, f"✔ 완료 ({(time.time() - t0) / 60:.1f}분)"))
        except Exception as e:
            import traceback
            traceback.print_exc()
            results.append((code, f"✘ 실패: {e}"))

    print(f"\n{'=' * 70}\n요약")
    for code, note in results:
        print(f"  {code:10s} {note}")
    fails = sum(1 for _, x in results if x.startswith("✘"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
