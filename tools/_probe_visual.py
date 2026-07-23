# -*- coding: utf-8 -*-
"""probe: 클린본 keep 구간에서 키프레임 추출 → claude -p(sonnet) 1콜로 시각 브리핑 생성 →
현재 plan.dialogue(시각정보 없이 만든 대사)와 나란히 출력해 '상황파악' 향상 여지를 검증.
stage_ai 는 아직 안 건드림(격리 probe).
사용: .venv\\Scripts\\python.exe tools\\_probe_visual.py START-600 [--step 4]
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages


def extract_frames(video, t0, t1, step, outdir, wh=640):
    """keep 구간 [t0,t1]을 step초 간격으로 프레임 추출. (keep상대초, 경로) 리스트 반환."""
    frames = []
    t = t0
    i = 0
    while t < t1:
        rel = t - t0
        dst = outdir / f"f_{i:02d}_{rel:04.0f}s.png"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(video),
             "-frames:v", "1", "-vf", f"scale={wh}:-1", str(dst), "-loglevel", "error"],
            capture_output=True)
        if dst.is_file():
            frames.append((rel, dst))
        t += step
        i += 1
    return frames


def caption(frames, frame_dir, log=print):
    """claude -p --model sonnet 한 콜로 프레임 전부 캡션."""
    lines = ["아래 프레임들은 한 영상에서 순서대로 뽑은 것이다(초는 구간 시작 기준).",
             "각 프레임을 정확히 `[초s] 장면설명 / 화면글자` 형식으로 한 줄씩만 답하라.",
             "장면설명은 인물 수·자세·행동·감정을 사실 그대로. 화면글자 없으면 '없음'.", ""]
    for rel, path in frames:
        lines.append(f"{rel:.0f}s: {path}")
    prompt = "\n".join(lines)
    import os
    from server.core.llm import _cli_path
    exe = _cli_path("claude")
    env = dict(os.environ, DISABLE_OMC="1")
    # ★ 프롬프트는 stdin으로(argv는 긴 다중행 잘림 — 메모리 교훈). claude -p는 인자 없으면 stdin을 읽음.
    # --add-dir: 워크스페이스 밖 프레임 폴더 Read 허용 / --allowedTools: 헤드리스에서 Read 무프롬프트 허용
    r = subprocess.run([exe, "-p", "--model", "sonnet",
                        "--add-dir", str(frame_dir), "--allowedTools", "Read"],
                       input=prompt, capture_output=True, text=True,
                       encoding="utf-8", env=env, timeout=600)
    out = r.stdout or ""
    if not out.strip() and r.stderr:
        out = f"(stderr) {r.stderr[:500]}"
    # bkit 푸터/구분선 이후 잘라내기(혹시 DISABLE_OMC로도 남으면)
    out = re.split(r"\n[─-]{5,}", out)[0].strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--step", type=float, default=4.0, help="프레임 간격(초)")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    outdir = stages.work_dir(cfg, args.code)
    plan = json.loads((outdir / f"{args.code}_plan.json").read_text(encoding="utf-8"))
    clean = outdir / f"{args.code}_클린.mp4"
    if not clean.is_file():
        st = stages.load_state(outdir, args.code)
        clean = Path(st.get("video"))
    keep = plan.get("keep", [])
    dlg = plan.get("dialogue", [])
    print(f"[{args.code}] clean={clean.name} / keep={keep} / dialogue={len(dlg)}줄 / step={args.step}s\n")

    tmp = Path(tempfile.mkdtemp(prefix="probe_vis_"))
    all_frames = []
    for (t0, t1) in keep:
        all_frames += extract_frames(clean, t0, t1, args.step, tmp)
    print(f"추출 프레임 {len(all_frames)}장 → claude -p(sonnet) 1콜 캡션 중...\n")

    brief = caption(all_frames, tmp)
    print("=" * 70)
    print("① 시각 브리핑 (claude -p sonnet, 프레임 → 상황)")
    print("=" * 70)
    print(brief)

    print("\n" + "=" * 70)
    print("② 현재 대사 (시각정보 없이 전사만으로 만든 plan.dialogue)")
    print("=" * 70)
    base = keep[0][0] if keep else 0
    for d in dlg:
        rel = d.get("start", 0) - base
        print(f"  [{rel:5.1f}s] ({d.get('speaker','?')}) {d.get('ko','')}")

    print("\n" + "=" * 70)
    print("③ 판단 포인트: 위 브리핑의 화면상황이 아래 대사의 화자배정/톤/맥락을 "
          "얼마나 보정해줄 수 있는지 비교")
    print("=" * 70)


if __name__ == "__main__":
    main()
