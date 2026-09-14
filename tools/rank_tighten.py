# -*- coding: utf-8 -*-
r"""섹션2 마무리 ① — keep 안의 **대사 없는 빈 구간**을 잘라 템포를 올린다.

왜: AI가 고른 keep 에는 걷기·멍때리기·말 사이 긴 텀이 섞여 지루하다(ja22 실측 편당 6~55초).
    내레이션은 원음을 줄이고 그 위에 얹으므로 '빈 틈을 남겨 둘' 필요가 없다.

판정(오디오가 아니라 전사 기준 — BGM 이 무음을 가려서 silencedetect 로는 못 잰다):
  · 발화 = 정밀전사(large-v3) ∪ 러프 전사(small). 한쪽이 놓친 속삭임도 다른 쪽이 잡는다.
  · 발화 앞뒤 EDGE(0.35s)는 숨 쉴 틈으로 남긴다 → 말끝이 뚝 잘리지 않는다.
  · 그 사이 빈 구간이 MIN_GAP(1.2s) 이상일 때만 자른다. 짧은 텀은 대화 리듬이라 둔다.
  · 대사가 한 줄도 없는 keep 조각은 건드리지 않는다(화면만으로 가는 장면일 수 있다).

하는 일(편당): plan.keep 축소(백업 .bak_tighten) → 클린본에서 재컷(final.mp4) → stage_subs.
  내레이션은 keep 이 바뀌면 다시 써야 하므로 **뒤에 rank_renarrate --redo 가 필요**하다
  (run_ja 는 자동으로 그렇게 돈다). 이미 조인 편(state.tightened)은 건너뛴다.

사용:
  .venv\Scripts\python.exe tools\rank_tighten.py --out F:\ja_reviewer_out\ja22 [--only A,B] [--dry] [--redo]
"""
import argparse
import json
import shutil
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server import pipeline as P
from batch_clean import CliEmitter

EDGE = 0.35      # 발화 앞뒤로 남기는 여유
MIN_GAP = 1.2    # 이 길이 이상 빈 구간만 자른다
MIN_PIECE = 0.5  # 자르고 남은 조각이 이보다 짧으면 버린다(깜빡이는 컷 방지)


def _load(p):
    try:
        return [(d["start"], d["end"]) for d in json.loads(p.read_text(encoding="utf-8"))]
    except (OSError, ValueError, KeyError):
        return []


def tighten_keep(keep, speech):
    """keep 각 조각에서 발화 ±EDGE 로 덮이지 않는 MIN_GAP 이상 구간을 뺀다."""
    out, removed = [], 0.0
    for a, b in keep:
        iv = sorted((max(a, s - EDGE), min(b, e + EDGE)) for s, e in speech if e > a and s < b)
        if not iv:                      # 대사 없는 조각은 그대로
            out.append((a, b))
            continue
        pieces, cur_s, cur = [], a, a
        for s, e in iv:
            if s - cur >= MIN_GAP:
                pieces.append((cur_s, cur))
                removed += s - cur
                cur_s = s
            cur = max(cur, e)
        if b - cur >= MIN_GAP:
            pieces.append((cur_s, cur))
            removed += b - cur
        else:
            pieces.append((cur_s, b))
        for s, e in pieces:
            if e - s >= MIN_PIECE:
                out.append((round(s, 3), round(e, 3)))
            else:
                removed += e - s
    return out, removed


def main():
    ap = argparse.ArgumentParser(description="keep 안 대사 없는 빈 구간 제거")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--dry", action="store_true", help="계산만 하고 파일은 안 건드린다")
    ap.add_argument("--redo", action="store_true", help="이미 조인 편도 다시(백업 plan 기준)")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    cfg["out_dir"] = args.out
    out = Path(args.out)
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}
    results = []
    for pf in sorted(out.glob("*/*_plan.json")):
        code = pf.parent.name
        if only and code not in only:
            continue
        d = pf.parent
        em = CliEmitter(code)
        st = stages.load_state(d, code)
        bak = pf.with_suffix(".json.bak_tighten")
        if st.get("tightened") and not args.redo:
            results.append(f"{code:10} 이미 조임 — 건너뜀")
            continue
        src_plan = bak if (args.redo and bak.is_file()) else pf
        plan = json.loads(src_plan.read_text(encoding="utf-8"))
        keep = P.parse_keep(plan.get("keep", []))
        speech = _load(d / f"{code}_정밀전사.json") + _load(d / f"{code}_전사.json")
        new_keep, removed = tighten_keep(keep, speech)
        before = sum(b - a for a, b in keep)
        after = sum(b - a for a, b in new_keep)
        line = f"{code:10} {before:4.0f}s → {after:4.0f}s  (−{removed:.0f}s, 조각 {len(keep)}→{len(new_keep)})"
        print(f"\n[{code}] {line}", flush=True)
        if args.dry or removed < 1.0:
            results.append(line + ("  [dry]" if args.dry else "  변화 없음"))
            continue
        video = st.get("video")
        if not (video and Path(video).is_file()):
            results.append(f"{code:10} ✘ 클린본 경로 없음")
            continue
        t0 = time.time()
        try:
            if not bak.is_file():
                shutil.copy2(pf, bak)
            plan["keep"] = [[a, b] for a, b in new_keep]
            pf.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
            P.cut_video(str(video), new_keep, str(d / f"{code}_final.mp4"), em.log, lambda fr: None)
            stages.stage_subs(cfg, code, em)
            # keep 이 바뀌었으니 내레이션 시각·분량이 무효 — rank_renarrate 가 이 표시를 보고 다시 쓴다
            stages.save_state(d, code, tightened=True, renarrate_needed=True)
            stages.worklog(d, code, f"빈 구간 제거: {before:.0f}s → {after:.0f}s")
            got = P.video_duration(str(d / f"{code}_final.mp4")) or 0
            results.append(line + f"  ✔ final {got:.0f}s ({time.time() - t0:.0f}s)")
        except Exception as e:
            traceback.print_exc()
            results.append(f"{code:10} ✘ {e}")

    print(f"\n{'=' * 70}\n요약")
    for r in results:
        print("  " + r)
    return 1 if any("✘" in r for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
