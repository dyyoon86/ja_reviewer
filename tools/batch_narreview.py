# -*- coding: utf-8 -*-
"""모음집 전체 내레이션을 시나리오 검수 에이전트로 돌려 결함 리포트를 만든다.

작성(regen)과 검수를 **다른 호출로 분리**하는 게 요점이다. 같은 모델이 자기가 쓴 걸
바로 검사하면 잘 못 잡는다 — ja18에서 오징어게임 패러디 누락, 주어 없는 '팔짱이 더
단단해집니다' 같은 게 전부 그대로 통과했다.

리포트만 만들고 고치지는 않는다(무엇을 고칠지는 사람이 정한다).
`--fix` 를 주면 결함이 있는 편만 골라 내레이션을 다시 쓴다(TTS는 따로).

사용:
  .venv\\Scripts\\python.exe tools\\batch_narreview.py --out <out_dir> [품번...]
  .venv\\Scripts\\python.exe tools\\batch_narreview.py --out <out_dir> --fix
"""
import argparse
import json
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server.core import narreview
from batch_clean import CliEmitter


def main():
    ap = argparse.ArgumentParser(description="내레이션 시나리오 검수")
    ap.add_argument("codes", nargs="*", help="품번(생략 시 out_dir 전부)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="sonnet", help="검수 모델(기본 sonnet)")
    ap.add_argument("--fix", action="store_true",
                    help="결함 있는 편의 내레이션을 다시 쓴다(TTS는 batch_nar_tts로 따로)")
    args = ap.parse_args()

    out = Path(args.out)
    codes = [c.upper() for c in args.codes] or sorted(
        p.name for p in out.iterdir()
        if p.is_dir() and not p.name.startswith("_") and (p / f"{p.name}_내레이션.json").is_file())
    if not codes:
        print(f"검수 대상 없음: {out}")
        sys.exit(1)

    cfg = _common.load_cfg()
    cfg["out_dir"] = str(out)
    rows, bad = [], []
    for i, code in enumerate(codes, 1):
        folder = out / code
        print(f"\n{'=' * 70}\n({i}/{len(codes)}) {code}", flush=True)
        t0 = time.time()
        try:
            r = narreview.review(folder, code, model=args.model, log=print)
        except Exception as e:
            traceback.print_exc()
            rows.append((code, f"✘ {e}")); continue
        n = len(r.get("issues") or [])
        if r.get("ok") is None:
            rows.append((code, f"– 검수 못함({r.get('note')})"))
        elif n:
            bad.append(code)
            kinds = ", ".join(sorted({str(x.get("type")) for x in r["issues"]}))
            rows.append((code, f"⚠ {n}건 [{kinds}] ({(time.time()-t0)/60:.1f}분)"))
        else:
            rows.append((code, f"✔ 통과 ({(time.time()-t0)/60:.1f}분)"))

    print(f"\n{'=' * 70}\n검수 요약")
    for code, note in rows:
        print(f"  {code}: {note}")
    print(f"\n결함 있는 편 {len(bad)}/{len(codes)}" + (f" — {', '.join(bad)}" if bad else ""))

    # 리포트 한 장으로 모아두면 사람이 훑기 쉽다
    rep = out / "_내레이션검수.md"
    with rep.open("w", encoding="utf-8") as f:
        f.write("# 내레이션 검수 리포트\n\n")
        for code, note in rows:
            f.write(f"## {code} — {note}\n\n")
            jf = out / code / f"{code}_내레이션검수.json"
            if not jf.is_file():
                continue
            d = json.loads(jf.read_text(encoding="utf-8"))
            if d.get("note"):
                f.write(f"{d['note']}\n\n")
            for it in d.get("issues") or []:
                f.write(f"- **[{it.get('type')}] {it.get('n')}번** — {it.get('text','')}\n")
                f.write(f"  - 왜: {it.get('why','')}\n")
                f.write(f"  - 대안: {it.get('fix','')}\n")
            f.write("\n")
    print(f"리포트: {rep}")

    if args.fix and bad:
        from server.core.regen import regen_narration
        print(f"\n{'=' * 70}\n--fix — 결함 편 {len(bad)}개 내레이션 재작성")
        # 서수 인트로는 모음집 전체 기준이라 전체 목록에서 번호를 뽑아야 한다
        for code in bad:
            em = CliEmitter(code)
            seq = (codes.index(code) + 1, len(codes))
            try:
                regen_narration(out / code, cfg["meta_api"], log=em.log, seq=seq)
                print(f"  {code}: ✔ 재작성 (TTS는 batch_nar_tts로 따로)")
            except Exception as e:
                print(f"  {code}: ✘ {e}")

    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
