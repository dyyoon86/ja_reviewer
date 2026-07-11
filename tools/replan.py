#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""plan.json의 keep 구간 재생성 — LLM에게 다시 target 시간 맞게 선택하도록 요청.
사용:
    python replan.py C:/Users/yoon/ja_reviewer_out/JUR-088
    python replan.py C:/Users/yoon/ja_reviewer_out/JUR-088 --llm claude --target 60
"""
import sys, json
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import _common
from server import pipeline as P

CFG = _common.load_cfg()
META_API = CFG["meta_api"]


def replan(folder: Path, llm="claude", target=60, log=print):
    code = folder.name
    tj   = folder / f"{code}_전사.json"
    pf   = folder / f"{code}_plan.json"
    vf   = folder / f"{code}_trim.mp4"       # 이미 trim된 영상 사용

    if not tj.exists(): raise RuntimeError(f"전사 파일 없음: {tj}")
    if not vf.exists(): raise RuntimeError(f"trim 영상 없음: {vf}")

    segs = [(d["start"], d["end"], d["text"])
            for d in json.loads(tj.read_text(encoding="utf-8"))]
    log(f"전사 라인: {len(segs)}개")

    log("메타 조회 중...")
    try:
        meta = P.fetch_meta(META_API, code, log=log)
    except Exception as e:
        log(f"  메타 실패: {e} — 빈 메타로 진행")
        meta = {"code": code}

    prompt = P.prompt_manual(meta, segs, target)
    log(f"프롬프트 {len(prompt)}자 — {llm} 호출 중...")

    res = P.call_llm(prompt, llm, log=log)
    keep = P.parse_keep(res.get("keep", []))
    if not keep:
        raise RuntimeError("LLM이 keep 구간을 못 골랐습니다.")

    total = sum(e - s for s, e in keep)
    log(f"새 keep: {len(keep)}구간, 합계 {total:.1f}초 (target {target}초)")
    for s, e in keep:
        log(f"  [{s:.1f}, {e:.1f}] = {e-s:.1f}초")

    pf.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"plan.json 저장: {pf}")

    final = str(folder / f"{code}_final.mp4")
    log("컷 영상 생성 중...")
    P.cut_video(str(vf), keep, final, log=log)
    dur = P.video_duration(final)
    log(f"완료: {final} ({dur:.1f}초)")
    return res


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--llm", default=CFG["llm"])
    ap.add_argument("--target", type=int, default=CFG["target_sec"])
    args = ap.parse_args()
    replan(Path(args.folder), args.llm, args.target)
