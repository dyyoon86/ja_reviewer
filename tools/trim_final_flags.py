# -*- coding: utf-8 -*-
"""final 노출 검출 → plan.keep에서 그 구간만 제거 → final 재컷 + 자막 재생성, 검출 0까지 반복.

NudeNet 점수는 임계 근처에서 요동쳐(추출 프레임 위치·리인코딩에 따라 ±) 클린본을
한 번 다시 잘라도 최종 검사에서 새 1초짜리가 튀어나온다. 원본을 또 자르는 대신,
완성 직전 final(1~2분)을 직접 검사→keep 축소→재컷하는 게 결정적이고 싸다.

사용: .venv\\Scripts\\python.exe tools\\trim_final_flags.py SNOS-269 SNOS-295 START-608
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import pipeline as P
from server import stages
from server.core import nsfw
from batch_clean import CliEmitter

# ★ ffmpeg fps 샘플링은 간격의 중간점 프레임을 뽑아 step이 다르면 '다른 프레임'을 본다
#   (0.25s 스캔이 깨끗해도 0.5s 스캔이 잡는 프레임이 존재). 두 그리드를 합집합으로 스캔.
STEPS, PAD, MAX_PASS, MIN_PIECE = (0.25, 0.5), 0.75, 4, 1.5


def final_to_src(spans, keep):
    """final(컷 결과) 좌표의 구간들을 원본 좌표로 환원 — keep 경계에 걸치면 분할."""
    out = []
    for a, b in spans:
        acc = 0.0
        for ks, ke in keep:
            d = ke - ks
            s0, e0 = max(a, acc), min(b, acc + d)
            if e0 > s0:
                out.append((ks + (s0 - acc), ks + (e0 - acc)))
            acc += d
    return out


def subtract(keep, bad):
    """keep 구간 목록에서 bad 구간들을 빼고, 너무 짧아진 조각은 버린다."""
    out = list(keep)
    for bs, be in bad:
        nxt = []
        for ks, ke in out:
            if be <= ks or bs >= ke:
                nxt.append((ks, ke))
                continue
            if ks < bs:
                nxt.append((ks, bs))
            if be < ke:
                nxt.append((be, ke))
        out = nxt
    return [(s, e) for s, e in out if e - s >= MIN_PIECE]


def main():
    codes = [c for c in sys.argv[1:] if c]
    if not codes:
        print("사용: trim_final_flags.py <품번>...")
        sys.exit(1)
    cfg = _common.load_cfg()
    thr = float(cfg.get("nsfw_clean_threshold", 0.22))
    fails = 0

    for code in codes:
        outdir = Path(cfg["out_dir"]) / code
        em = CliEmitter(code)
        plan_f = outdir / f"{code}_plan.json"
        final = outdir / f"{code}_final.mp4"
        st = stages.load_state(outdir, code)
        src = st.get("video")
        print(f"\n{'=' * 70}\n{code}", flush=True)
        if not (plan_f.is_file() and final.is_file() and src and Path(src).is_file()):
            print(f"[{code}] ✘ plan/final/원본 누락 — 건너뜀")
            fails += 1
            continue

        ok = False
        for pw in range(1, MAX_PASS + 1):
            dur = P.video_duration(final) or 0.0
            bad = []
            for stp in STEPS:
                bad += nsfw.build_map(str(final), step=stp, threshold=thr, pad=PAD,
                                      merge_gap=2.0, cache=None, log=em.log, duration=dur)
            bad = sorted(bad)
            if not bad:
                print(f"[{code}] ✔ 패스 {pw}: 검출 0 — 통과")
                ok = True
                break
            plan = json.loads(plan_f.read_text(encoding="utf-8"))
            keep = P.parse_keep(plan.get("keep", []))
            bad_src = final_to_src(bad, keep)
            new_keep = subtract(keep, bad_src)
            if not new_keep:
                print(f"[{code}] ✘ keep이 전부 사라짐 — 수동 검수 필요")
                break
            cut = sum(e - s for s, e in keep) - sum(e - s for s, e in new_keep)
            print(f"[{code}] 패스 {pw}: 노출 {len(bad)}건 → keep에서 {cut:.1f}s 제거 후 재컷")
            plan["keep"] = [[round(s, 3), round(e, 3)] for s, e in new_keep]
            plan_f.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
            P.cut_video(str(src), new_keep, str(final), em.log, lambda fr: None)
            stages.stage_subs(cfg, code, em)
            stages.worklog(outdir, code, f"final 노출 재컷(패스 {pw}) — {cut:.1f}s 제거")
        if not ok:
            fails += 1

    print(f"\n결과: {'전부 통과' if not fails else f'{fails}건 미해결'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
