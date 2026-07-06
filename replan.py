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
sys.path.insert(0, str(Path(__file__).parent))
from server import pipeline as P

META_API = "http://172.30.1.40:8770"


def replan(folder: Path, llm="claude", target=60):
    code = folder.name
    tj   = folder / f"{code}_전사.json"
    pf   = folder / f"{code}_plan.json"
    vf   = folder / f"{code}_trim.mp4"       # 이미 trim된 영상 사용
    sf   = folder / f"{code}_state.json"

    if not tj.exists(): print("전사 파일 없음:", tj); sys.exit(1)
    if not vf.exists(): print("trim 영상 없음:", vf); sys.exit(1)

    segs = [(d["start"], d["end"], d["text"])
            for d in json.loads(tj.read_text(encoding="utf-8"))]
    print(f"전사 라인: {len(segs)}개")

    print("메타 조회 중...")
    try:
        meta = P.fetch_meta(META_API, code)
    except Exception as e:
        print(f"  메타 실패: {e} — 빈 메타로 진행")
        meta = {"code": code}

    prompt = P.prompt_manual(meta, segs, target)
    print(f"프롬프트 {len(prompt)}자 — {llm} 호출 중...")

    res = P.call_llm(prompt, llm)
    keep = P.parse_keep(res.get("keep", []))
    if not keep:
        print("LLM이 keep 구간을 못 골랐습니다."); sys.exit(1)

    total = sum(e - s for s, e in keep)
    print(f"새 keep: {len(keep)}구간, 합계 {total:.1f}초 (target {target}초)")
    for s, e in keep:
        print(f"  [{s:.1f}, {e:.1f}] = {e-s:.1f}초")

    pf.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"plan.json 저장: {pf}")

    final = str(folder / f"{code}_final.mp4")
    print("컷 영상 생성 중...")
    P.cut_video(str(vf), keep, final)
    dur = P.video_duration(final)
    print(f"완료: {final} ({dur:.1f}초)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--llm", default="claude")
    ap.add_argument("--target", type=int, default=60)
    args = ap.parse_args()
    replan(Path(args.folder), args.llm, args.target)
