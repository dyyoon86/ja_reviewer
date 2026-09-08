# -*- coding: utf-8 -*-
r"""TTS 전 자막 전수 점검 — 배치 전체를 한 표로 본다.

stage_tts 는 편당 자동으로 같은 점검(server/core/precheck.guard)을 돌리지만,
TTS 를 걸기 **전에** 배치 상태를 한눈에 보고 고칠 편을 먼저 추리는 용도다.

보는 것
  · 대사 SRT 가 실제 발화 지점을 덮는가(final 영상 재전사 대조)
  · 소리 없는 자리에 자막이 있지 않은가
  · 내레이션이 영상 길이를 넘지 않는가 / 밀리지 않는가 / 도입이 늦지 않은가

사용:
  python tools\check_before_tts.py --out F:\ja_reviewer_out\ja21_v2 --src <원본폴더>
  python tools\check_before_tts.py --out ... --code MIDA-798        # 특정 편만
  python tools\check_before_tts.py --out ... --src ... --fast        # 구조만(전사 생략)
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import pipeline as P
from server.core import precheck
from _ranklist import load_rank, find_rank_file, match_videos



RANK_HEAD = re.compile(r"^\s*(\d{1,2})\s*위")


def rank_intro_issue(outdir, code, rank):
    """★내레이션 첫 줄이 순위 호명("N위, 배우이름입니다")인지 본다.

    재컷으로 keep 이 깎이면 첫 줄이 통째로 밀려나거나 문장이 잘린다. ja20 START-627,
    ja21 SNOS-310("이번 작품은 하카타 이로하, S1 넘버." — 순위도 없고 문장도 끊김)이 그랬다.
    카운트다운 영상에서 한 편만 순위 호명이 빠지면 바로 티가 난다.
    """
    f = outdir / f"{code}_내레이션.srt"
    if not f.is_file():
        return None
    rows = P.srt_parse(str(f))
    if not rows:
        return "내레이션 0줄"
    head = str(rows[0][2]).strip()
    m = RANK_HEAD.match(head)
    if not m:
        return f"1번 줄이 순위 호명이 아님(\"{head[:22]}…\")"
    if rank and int(m.group(1)) != int(rank):
        return f"1번 줄 순위가 {m.group(1)}위인데 실제는 {rank}위"
    return None


def main():
    ap = argparse.ArgumentParser(description="TTS 전 대사·내레이션 자막 전수 점검")
    ap.add_argument("--out", help="out_dir. 생략 시 config out_dir")
    ap.add_argument("--src", help="원본 폴더(랭킹 txt 위치). 주면 순위 순으로 돈다")
    ap.add_argument("--code", action="append", default=[], help="특정 품번만(여러 번 가능)")
    ap.add_argument("--fast", action="store_true", help="구조만 점검(final 재전사 생략)")
    ap.add_argument("--model", default="small", help="점검용 whisper 모델(기본 small)")
    ap.add_argument("--fix", action="store_true",
                    help="자막누락으로 잡힌 편을 fix_dialogue 로 자동 수리하고 다시 점검한다")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    out = Path(args.out or cfg["out_dir"])

    if args.code:
        todo = [(i + 1, c) for i, c in enumerate(args.code)]
    elif args.src:
        pairs, _, _ = match_videos(load_rank(find_rank_file(args.src)), args.src)
        todo = [(r, c) for r, c, _v in sorted(pairs)]
    else:
        todo = [(i + 1, p.name) for i, p in enumerate(sorted(out.iterdir()))
                if p.is_dir() and not p.name.startswith("_")]
    if not todo:
        print("점검할 편이 없습니다.")
        return 1

    print(f"{'순위':>4} {'품번':12} 판정")
    print("─" * 78)
    bad = []
    for rank, code in todo:
        issues = precheck.check_edition(out / code, code, audio=not args.fast,
                                        model=args.model, log=lambda *a: None)
        ri = rank_intro_issue(out / code, code, rank if args.src else None)
        if ri:
            issues = list(issues) + ["순위호명: " + ri]
        if issues:
            bad.append((rank, code, issues))
            print(f"{rank:>3}위 {code:12} ⚠ " + " / ".join(issues), flush=True)
        else:
            print(f"{rank:>3}위 {code:12} ✔ 정상", flush=True)

    print()
    if not bad:
        print(f"✔ {len(todo)}편 전부 이상 없음 — TTS 진행 가능")
        return 0
    print(f"⚠ 손볼 편 {len(bad)}/{len(todo)}")
    for rank, code, issues in bad:
        print(f"   {rank}위 {code}")
        for i in issues:
            print(f"      · {i}")
    need_fix = [c for _r, c, iss in bad if any("자막" in i and "누락" in i for i in iss)]
    if args.fix and need_fix:
        print(f"\n--fix: 자막누락 {len(need_fix)}편 자동 수리 → " + ", ".join(need_fix))
        cmd = [sys.executable, str(Path(__file__).with_name("fix_dialogue.py")),
               "--out", str(out)]
        for c in need_fix:
            cmd += ["--code", c]
        subprocess.run(cmd)
        print("\n수리 후 재점검")
        rest = []
        for rank, code in todo:
            if code not in need_fix:
                continue
            iss = precheck.check_edition(out / code, code, audio=not args.fast,
                                         model=args.model, log=lambda *a: None)
            print(f"{rank:>3}위 {code:12} " + ("⚠ " + " / ".join(iss) if iss else "✔ 정상"),
                  flush=True)
            if iss:
                rest.append(code)
        if not rest and len(need_fix) == len(bad):
            print("\n✔ 자동 수리로 전부 해소 — TTS 진행 가능")
            return 0

    print("\n  자막누락 →  python tools\\fix_dialogue.py --out %s --code <품번>" % out)
    print("  순위호명 →  python tools\\fix_narration.py --out %s --code <품번> --set 1=\"N위, 배우입니다.\"" % out)
    print("  내레이션 →  섹션3 ① 재생성, 또는 tools\\fix_narration.py 로 줄 단위 수정")
    return 1


if __name__ == "__main__":
    sys.exit(main())
