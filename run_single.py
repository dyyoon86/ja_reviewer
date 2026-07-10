#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""단일 품번 전체 파이프라인 — 전사 → 메타 → LLM → SRT.
trim.mp4가 있는 폴더를 대상으로 실행.

사용:
    python run_single.py START-597
"""
import sys
import json
import shutil
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
from server import pipeline as P

META_API  = "http://172.30.1.40:8770"
OUT_BASE  = Path(r"C:\Users\yoon\ja_reviewer_out")
LLM       = "claude"
TARGET_SEC = 60


def log(msg):
    print(msg, flush=True)


def main(code: str):
    folder = OUT_BASE / code
    folder.mkdir(parents=True, exist_ok=True)

    trim_mp4 = folder / f"{code}_trim.mp4"
    if not trim_mp4.exists():
        log(f"trim.mp4 없음: {trim_mp4}")
        sys.exit(1)

    trans_json = folder / f"{code}_전사.json"
    trans_srt  = folder / f"{code}_전사.srt"

    # ── ① 전사 ──────────────────────────────────────────────────────────────
    if trans_json.exists():
        log(f"[①] 전사 이미 완료, 재사용: {trans_json}")
        segs = [(s["start"], s["end"], s["text"])
                for s in json.loads(trans_json.read_text(encoding="utf-8"))]
    else:
        log("[①] Whisper 전사 시작...")
        segs = P.transcribe(trim_mp4, model_name="large-v3", log=log)
        trans_json.write_text(
            json.dumps([{"start": s, "end": e, "text": t} for s, e, t in segs],
                       ensure_ascii=False, indent=1), encoding="utf-8")
        P.write_srt(segs, trans_srt)
        log(f"[①] 전사 완료: {len(segs)}세그 → {trans_srt}")

    # ── ② 메타 ──────────────────────────────────────────────────────────────
    log("[②] 메타 조회...")
    try:
        meta = P.fetch_meta(META_API, code, log=log)
    except Exception as e:
        log(f"[②] 메타 조회 실패: {e}")
        log("    메타 없이 진행합니다 (배우명·신체 정보 미포함)")
        meta = {"code": code, "actress": "미확인", "actress_ja": "",
                "label": "", "maker": "", "director": "", "series_ja": "",
                "description": "", "genres": [], "release_date": "",
                "runtime_mins": "", "views": 0, "likes": 0, "dislikes": 0,
                "meas": ""}

    # ── ③ LLM (plan.json) ───────────────────────────────────────────────────
    plan_json = folder / f"{code}_plan.json"
    if plan_json.exists():
        log(f"[③] plan.json 이미 존재, 재사용: {plan_json}")
        plan = json.loads(plan_json.read_text(encoding="utf-8"))
    else:
        log("[③] LLM 호출 중 (trim 영상 기준, prompt_manual)...")
        # 1339세그처럼 너무 많으면 LLM 토큰 초과 → 2500자 이내로 트림
        MAX_CHARS = 2500
        total_chars = sum(len(t) for _, _, t in segs)
        if total_chars > MAX_CHARS:
            trimmed, acc = [], 0
            for seg in segs:
                acc += len(seg[2])
                trimmed.append(seg)
                if acc >= MAX_CHARS:
                    break
            log(f"  자막 트림: {len(segs)}→{len(trimmed)}세그 ({total_chars}자→{acc}자)")
            segs_for_llm = trimmed
        else:
            segs_for_llm = segs
        prompt = P.prompt_manual(meta, segs_for_llm, target_sec=TARGET_SEC)
        plan = P.call_llm(prompt, which=LLM, log=log)  # already returns dict
        plan_json.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"[③] plan.json 저장: {plan_json}")

    # ── ④ SRT 생성 ───────────────────────────────────────────────────────────
    log("[④] 대사·내레이션 SRT 생성...")

    dlg  = P.parse_lines(plan.get("dialogue", []), ("ko", "text"), extra=[("speaker", "여")], log=log)
    nar  = P.parse_lines(plan.get("narration", []), ("text", "ko"), extra=[("style", "기본")], log=log)

    if dlg:
        P.write_srt([(s, e, t) for s, e, t, *_ in dlg], folder / f"{code}_대사.srt")
        dsplit = P.split_entries(dlg, 24)
        data = [{"start": round(s, 3), "end": round(e, 3), "text": t, "speaker": sp}
                for s, e, t, sp in dsplit]
        (folder / f"{code}_대사.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"  대사: {len(dlg)}개")

    if nar:
        P.write_srt([(s, e, t) for s, e, t, *_ in nar],
                    folder / f"{code}_내레이션.srt", maxlen=0)
        ndata = [{"start": round(s, 3), "end": round(e, 3), "text": t, "style": st}
                 for s, e, t, st in nar]
        (folder / f"{code}_내레이션.json").write_text(
            json.dumps(ndata, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"  내레이션: {len(nar)}개")

    log(f"\n[완료] {code}")
    log(f"  별점: {'★'*P.clamp_stars(plan.get('stars'))}")
    log(f"  요약: {(plan.get('summary','')[:100])}")
    log("\n[내레이션]")
    for n in plan.get("narration", []):
        log(f"  [{n.get('style','기본')}] {n['text']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용: python run_single.py <품번>")
        sys.exit(1)
    main(sys.argv[1])
