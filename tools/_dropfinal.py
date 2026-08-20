# -*- coding: utf-8 -*-
"""완성본에서 잡힌 노출을 **역매핑**해 원본 구간을 제거하고 다시 굽는다.

_safecut.py 는 클린본(소스) 프레임을 크롭해서 판정한다. 그런데 실제 납품본은
crop→1920x1080 lanczos 업스케일→재인코딩을 거치므로 픽셀이 달라지고, NudeNet
점수도 달라진다. 실측(ja19 MBDD-2190): 소스 크롭 프레임에서는 0.22 미만이라
통과했는데 굽힌 뒤에는 같은 장면이 FEMALE_BREAST_EXPOSED 0.68 로 잡혔다.

그래서 이 도구는 **나가는 물건 자체**를 스캔하고, 잡힌 시각을 keep 매핑으로
소스 시각으로 되돌려 그 구간을 버린다. 스캔 대상과 판정 기준이 최종 게이트
(stage_burn 의 nsfw_final_check)와 완전히 같으므로 통과할 때까지 수렴한다.

TTS 는 다시 돌리지 않는다 — 살아남은 문장의 기존 음성을 재사용한다(_safecut 과 동일).

사용: .venv\\Scripts\\python.exe tools\\_dropfinal.py --out "C:\\...\\ja19" --only MBDD-2190
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
from server.stages import NullLock
from server.core import nsfw
from batch_clean import CliEmitter
from _finish_ja19 import finish_one
from _safecut import snapshot_tts, reuse_tts, in_keep


def to_source(t, keep):
    """완성본 시각 → 소스(클린본) 시각 + 그 구간 index. 범위를 벗어나면 (None, None)."""
    off = 0.0
    for i, (a, b) in enumerate(keep):
        dur = float(b) - float(a)
        if t < off + dur:
            return float(a) + (t - off), i
        off += dur
    return None, None


def main():
    ap = argparse.ArgumentParser(description="완성본 노출 역매핑 제거")
    ap.add_argument("--out", help="out_dir 오버라이드")
    ap.add_argument("--only", default="", help="이 품번만(쉼표 구분)")
    ap.add_argument("--skip", default="", help="제외할 품번(쉼표 구분)")
    ap.add_argument("--threshold", type=float, default=0.22, help="NN 임계")
    ap.add_argument("--step", type=float, default=0.25, help="스캔 간격(최종 게이트와 동일)")
    ap.add_argument("--rounds", type=int, default=3, help="최대 반복 횟수")
    ap.add_argument("--duck", type=float, default=0.3)
    args = ap.parse_args()

    cfg = _common.load_cfg()
    if args.out:
        cfg["out_dir"] = args.out
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}
    skip = {c.strip().upper() for c in args.skip.split(",") if c.strip()}

    root = Path(cfg["out_dir"])
    codes = sorted(d.name for d in root.iterdir()
                   if d.is_dir() and not d.name.startswith("_")
                   and (d / f"{d.name}_plan.json").is_file())
    codes = [c for c in codes if c not in skip and (not only or c in only)]
    print(f"대상 {len(codes)}개 / 임계 {args.threshold} · 간격 {args.step}s · 최대 {args.rounds}회")

    results = []
    for n, code in enumerate(codes, 1):
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        print(f"\n{'=' * 70}\n({n}/{len(codes)}) {code}", flush=True)
        t0 = time.time()
        note = ""
        try:
            for rnd in range(1, args.rounds + 1):
                sub = outdir / f"{code}_final_subbed.mp4"
                if not sub.is_file():
                    raise RuntimeError(f"완성본 없음: {sub}")
                em.log(f"[{rnd}회차] 완성본 스캔 — {sub.name}")
                hits = nsfw.check_final(str(sub), step=args.step,
                                        threshold=args.threshold, log=em.log)
                if not hits:
                    note = f"✔ 통과 ({rnd}회차)"
                    break

                plan = json.loads((outdir / f"{code}_plan.json").read_text(encoding="utf-8"))
                keep = [[float(a), float(b)] for a, b in P.parse_keep(plan.get("keep", []))]
                bad = {}
                for t, cls, sc in hits:
                    st, i = to_source(float(t), keep)
                    if i is None:
                        continue
                    bad.setdefault(i, []).append((round(t, 2), cls, sc, round(st, 2)))
                if not bad:
                    raise RuntimeError("역매핑 실패 — 완성본 시각이 keep 범위 밖")

                for i, xs in sorted(bad.items()):
                    t, cls, sc, st = xs[0]
                    em.log(f"🚫 구간{i + 1} {keep[i][0]:.0f}~{keep[i][1]:.0f}s 제거 — "
                           f"완성본 {t}s(소스 {st}s) {cls} {sc}, {len(xs)}프레임")
                new_keep = [s for j, s in enumerate(keep) if j not in bad]
                if not new_keep:
                    raise RuntimeError("남는 구간 없음 — 사람이 판단 필요")

                bak = outdir / f"{code}_plan.json.predrop"
                if not bak.is_file():
                    bak.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

                snap = snapshot_tts(outdir, code, em)
                plan["keep"] = new_keep
                plan["dialogue"] = [d for d in plan.get("dialogue", [])
                                    if in_keep(d.get("start", 0), new_keep)]
                plan["narration"] = [x for x in plan.get("narration", [])
                                     if in_keep(x.get("start", 0), new_keep)]
                (outdir / f"{code}_plan.json").write_text(
                    json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
                total = sum(b - a for a, b in new_keep)
                em.log(f"keep {len(keep)}→{len(new_keep)}구간, {total:.0f}s")

                final = str(outdir / f"{code}_final.mp4")
                st_ = stages.load_state(outdir, code)
                clean = st_.get("video") or str(outdir / f"{code}_클린.mp4")
                with NullLock():
                    P.cut_video(clean, new_keep, final, em.log, lambda fr: None)
                P.invalidate_derived(outdir, code, em.log)
                marker = outdir / f"{code}_final.nobgm"
                if marker.is_file():
                    marker.unlink()
                stages.stage_subs(cfg, code, em)
                if not reuse_tts(outdir, code, snap, em):
                    raise RuntimeError("기존 음성 재사용 실패 — TTS 필요")
                finish_one(cfg, code, em, duck=args.duck)
            else:
                note = f"✘ {args.rounds}회차까지 노출 잔존"
            results.append((code, note or "·", time.time() - t0))
        except Exception as e:
            traceback.print_exc()
            results.append((code, f"✘ {e}", time.time() - t0))

    print(f"\n{'=' * 70}\n요약")
    for code, note, el in results:
        print(f"  {code}: {note} ({el / 60:.1f}분)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
