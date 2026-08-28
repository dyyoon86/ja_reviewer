# -*- coding: utf-8 -*-
r"""눈검사로 확정한 구간을 plan.keep 에서 **원본(클린본) 좌표로** 빼낸다.

★자동 검사가 원리적으로 못 잡는 것이 있다 — 대표가 **모자이크 행위 장면**이다.
  NudeNet 은 '부위 노출'을 보는데 모자이크가 덮으면 EXPOSED 가 안 뜬다(ja20 01회
  source 35~46s: NN 검출 0인데 화면 하단에 모자이크가 선명). 격자 주기성으로 잡아보려
  했지만 h264 매크로블록·2배 업스케일과 구분이 안 돼 실패했다(tools/_probe_mosaic.py).
  그래서 사람(또는 Claude)이 몽타주로 확정한 구간은 이 도구로 직접 빼낸다.

빼는 방식은 `_safecut` 과 같다 — keep 에서 그 구간을 잘라내고, 남은 조각이 --min-clip
미만이면 버린다. plan 은 `.presafe` 로 백업한다(_safecut 과 같은 이름이라 한쪽만 남는다).

★`--apply` 를 주면 재컷→자막→재번인까지 한다. 안 주면 plan 만 고치고 끝나는데,
  그 뒤에 `_safecut` 을 돌려 반영시킬 생각이라면 **자동 검사가 아무것도 못 잡는 경우
  plan 변경이 그대로 묻힌다**("노출 없음 — 재컷 불필요"로 빠져나간다). 눈검사로 뺀
  구간만 반영할 때는 반드시 --apply 를 쓸 것. 자동 검사도 같이 돌릴 때만 --apply 를
  생략하고 `_safecut --strict --carve-first` 로 넘긴다.
★`burn_only` 로는 안 된다 — 그건 기존 final.mp4 에 자막만 다시 굽는다(재컷 없음).

내레이션 음성은 재생성하지 않는다 — 살아남은 문장의 기존 클립을 재매칭한다(_safecut 과 동일).

사용:
  .venv\Scripts\python.exe tools\_drop_keep.py --out "F:\ja_reviewer_out\ja20" ^
      --code SNOS-373 --drop 35-46 --apply [--dry]
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server import pipeline as P
from batch_clean import CliEmitter
from _safecut import snapshot_tts, reuse_tts
from batch_produce import hide_narration


def parse_ranges(text):
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        out.append((float(a), float(b)))
    return sorted(out)


def subtract(keep, drops, min_clip):
    out = []
    for a, b in ((float(x), float(y)) for x, y in keep):
        pieces = [(a, b)]
        for da, db in drops:
            nxt = []
            for s, e in pieces:
                if db <= s or da >= e:          # 안 겹침
                    nxt.append((s, e)); continue
                if s < da:
                    nxt.append((s, min(da, e)))
                if e > db:
                    nxt.append((max(db, s), e))
            pieces = nxt
        out += [[round(s, 2), round(e, 2)] for s, e in pieces if e - s >= min_clip]
    return out


def main():
    ap = argparse.ArgumentParser(description="눈검사 확정 구간을 plan.keep 에서 제거")
    ap.add_argument("--out", required=True, help="배치 out_dir")
    ap.add_argument("--code", required=True)
    ap.add_argument("--drop", required=True, help="원본 좌표 구간, 예 35-46,120-131")
    ap.add_argument("--min-clip", type=float, default=4.0, help="남은 조각 최소 길이(초)")
    ap.add_argument("--apply", action="store_true",
                    help="재컷→자막→재번인까지 실행(안 주면 plan 만 고침)")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    outdir = Path(args.out) / args.code
    plan_f = outdir / f"{args.code}_plan.json"
    if not plan_f.is_file():
        print(f"✗ plan 없음: {plan_f}")
        return 1
    plan = json.loads(plan_f.read_text(encoding="utf-8"))
    keep = plan.get("keep") or []
    drops = parse_ranges(args.drop)
    new = subtract(keep, drops, args.min_clip)

    old_t = sum(b - a for a, b in keep)
    new_t = sum(b - a for a, b in new)
    print(f"{args.code}: 제거 {drops}")
    print(f"  keep {len(keep)}→{len(new)}구간, {old_t:.0f}s→{new_t:.0f}s")
    for a, b in new:
        print(f"    {a:7.1f} ~ {b:7.1f}  ({b - a:.0f}s)")
    if not new:
        print("  ✗ 남는 구간이 없습니다 — 중단")
        return 1
    if args.dry:
        print("  [dry] 저장하지 않았습니다.")
        return 0

    bak = plan_f.with_suffix(".json.presafe")
    if not bak.is_file():
        bak.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    plan["keep"] = new
    plan_f.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    stages.worklog(outdir, args.code, f"눈검사 확정 구간 제거: {args.drop}")
    if not args.apply:
        print("  ✔ plan 저장. --apply 를 주지 않아 재컷은 하지 않았습니다.")
        print("    ※ _safecut 으로 넘길 게 아니면 --apply 로 다시 돌리세요 "
              "(자동 검사가 아무것도 못 잡으면 이 변경이 묻힙니다).")
        return 0

    cfg = _common.load_cfg(); cfg["out_dir"] = args.out
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT
    em = CliEmitter(args.code)
    src = stages.load_state(outdir, args.code).get("video")
    if not (src and Path(src).is_file()):
        print(f"  ✗ 클린본을 찾을 수 없습니다: {src}")
        return 1
    final = outdir / f"{args.code}_final.mp4"

    snap = snapshot_tts(outdir, args.code, em)      # 재컷이 {code}_tts 를 지우기 전에
    em.log("재컷 중…")
    P.cut_video(str(src), [(a, b) for a, b in new], str(final), em.log, lambda fr: None)
    # ★재컷하면 `{code}_final_voiced.mp4`(내레이션 mux 본)는 옛 길이 그대로라 낡는다.
    #   stage_burn 은 voiced 를 **final 보다 우선**하므로, 안 지우면 새 컷을 굽는 게 아니라
    #   옛 영상을 계속 굽는다(실측: final 54s 인데 subbed 가 계속 72s 로 나왔다).
    voiced = outdir / f"{args.code}_final_voiced.mp4"
    if voiced.is_file():
        voiced.unlink()
        em.log("낡은 final_voiced.mp4 제거 — 새 컷으로 굽는다")
    stages.stage_subs(cfg, args.code, em)
    if not reuse_tts(outdir, args.code, snap, em):
        em.log("※ 기존 음성 재사용 실패 — 내레이션 문장이 바뀌었습니다. TTS 재생성이 필요합니다.")
    em.log("재번인…")
    # ★내레이션은 숨기고 굽는다 — batch_produce/burn_only 와 같은 규칙.
    #   안 숨기면 stage_burn 이 내레이션 음성을 mux 하는데, 재컷으로 문장이 빠져도
    #   `{code}_내레이션.wav` 는 옛 길이 그대로라 **영상이 그 길이까지 늘어난다**
    #   (실측: keep 54s 인데 final_subbed 가 72s 로 나왔다). 납품 규격도 내레이션은
    #   따로 얹는 방식이라 굽기에서 빼는 게 맞다.
    moved = hide_narration(outdir, args.code)
    try:
        stages.stage_burn(cfg, args.code, styles, em)
    finally:
        import os
        for hidden, orig in moved:
            os.replace(hidden, orig)
    real = P.video_duration(outdir / f"{args.code}_final_subbed.mp4") or 0.0
    stages.worklog(outdir, args.code, f"재컷 완료 — {real:.0f}s")
    print(f"\n  ✔ 완료 — {real:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
