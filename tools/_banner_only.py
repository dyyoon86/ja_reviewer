# -*- coding: utf-8 -*-
"""배너·워터마크만 일괄 생성(④' stage_banner) — TTS·번인 없이.

batch_produce.py 는 regen(내레이션 재생성)→배너→TTS→번인을 한 번에 돌린다.
배너/워터마크 결과만 먼저 눈으로 확인하고 싶을 때 이 스크립트로 그 단계만 뗀다.
인코딩이 없어 편당 수 초.

산출물: {out_dir}/_infocard_{품번}/ 에 투명 PNG 3장(프레임·인포카드·워터마크)
        + 미리보기 스틸/애니메이션. ⑥ 굽기가 이 레이어를 그대로 합성한다.

★ gen_infocard 는 meta_api 주소를 studio_config.json 에서 **직접** 읽는다(인자로 못 넘긴다).
  우분투가 죽어 _meta_shim.py 로 대체 중이라면 config 의 meta_api 가 셤을 가리켜야 한다.

사용: .venv\\Scripts\\python.exe tools\\_banner_only.py --out "C:\\...\\ja19" --skip MIDA-703
"""
import argparse
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages


class Em(stages.Emitter):
    def __init__(self, code):
        self.code = code

    def log(self, m):
        print(f"[{self.code}] {m}", flush=True)

    def step(self, n, t, label):
        print(f"[{self.code}] ── ({n}/{t}) {label}", flush=True)

    def prog(self, frac, label=None):
        pass

    def file(self, tag, path):
        print(f"[{self.code}]    ▸ {tag}: {path}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="배너/워터마크만 일괄 생성")
    ap.add_argument("--out", help="out_dir 오버라이드")
    ap.add_argument("--hold", type=float, default=None, help="배너 유지 초(기본 config banner_hold)")
    ap.add_argument("--skip", default="", help="제외할 품번(쉼표 구분)")
    ap.add_argument("--only", default="", help="이 품번만(쉼표 구분)")
    ap.add_argument("--no-preview", action="store_true", help="미리보기 애니메이션 생략(더 빠름)")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    if args.out:
        cfg["out_dir"] = args.out
    hold = args.hold if args.hold is not None else float(cfg.get("banner_hold", 5.0))
    skip = {c.strip().upper() for c in args.skip.split(",") if c.strip()}
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}

    root = Path(cfg["out_dir"])
    codes = sorted(d.name for d in root.iterdir()
                   if d.is_dir() and not d.name.startswith("_")
                   and (d / f"{d.name}_plan.json").is_file())
    codes = [c for c in codes if c not in skip and (not only or c in only)]
    print(f"대상 {len(codes)}개 / out_dir={root} / meta={cfg['meta_api']} / hold={hold}s")

    results = []
    for i, code in enumerate(codes, 1):
        print(f"\n{'=' * 70}\n({i}/{len(codes)}) {code}", flush=True)
        em = Em(code)
        t0 = time.time()
        try:
            r = stages.stage_banner(cfg, code, em, hold=hold, preview=not args.no_preview)
            el = time.time() - t0
            if r.get("skipped"):
                results.append((code, f"⚠ 배너 생략 — {r.get('reason', '')[:60]}", el))
            else:
                results.append((code, "✔ 배너·워터마크 생성", el))
        except Exception as e:
            results.append((code, f"✘ 실패: {e}", time.time() - t0))
            traceback.print_exc()

    print(f"\n{'=' * 70}\n요약")
    for code, note, el in results:
        print(f"  {code}: {note} ({el:.0f}s)")
    fails = sum(1 for _, n, _ in results if n.startswith("✘"))
    skips = sum(1 for _, n, _ in results if n.startswith("⚠"))
    print(f"\n완료 {len(results) - fails - skips}/{len(results)}"
          + (f", 생략 {skips}" if skips else "") + (f", 실패 {fails}" if fails else ""))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
