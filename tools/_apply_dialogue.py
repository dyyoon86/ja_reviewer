# -*- coding: utf-8 -*-
"""keep 정밀 전사를 **전량 번역한 대사**로 plan.dialogue를 교체하고 자막을 다시 만든다.

배경(2026-08-11): 2-pass에서는 ②AI 1회차가 대사 번역을 하지 않고, 최종 대사는
`prompt_dialogue_fix` LLM 호출 하나에만 걸려 있다. 이 호출이 빈 응답이면 재시도 없이
"대사자막 없이 진행"으로 넘어가 최종본에 대사가 통째로 빠진다(ja16 MIDA-727/735/762 실측 0줄).
→ 그 호출에 기대지 않고, keep 정밀 전사(`{code}_keep전사.json`)를 사람이/Claude가 전량 번역한
   `{code}_대사번역.json`을 그대로 plan.dialogue에 넣는다. 선별 단계가 없으니 대사가 빠질 수 없다.

절차:
  1) tools\\dump_keep_transcript.py CODE...   → {code}_keep전사.json (large-v3 정밀 전사)
  2) 번역본 {code}_대사번역.json 작성          → [{start,end,ko,speaker}, ...]
  3) 이 스크립트                               → plan.dialogue 교체 + stage_subs 재실행

사용: .venv\\Scripts\\python.exe tools\\_apply_dialogue.py --out <out_dir> CODE [CODE...]

가드(2026-07-17 함정 ①): 새 번역 줄 수가 기존 dialogue보다 **적으면** 건너뛴다.
  속삭임 대사는 정밀 전사도 놓쳐 오히려 커버리지가 역행한 전례(MIDA-686 71→39%)가 있다.
  의도적으로 덮어쓰려면 --force.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401  (repo 루트 sys.path 등록)
from server import stages
from server import pipeline as P
from batch_clean import CliEmitter


def main():
    ap = argparse.ArgumentParser(description="정밀 전사 전량 번역본으로 plan.dialogue 교체")
    ap.add_argument("codes", nargs="+", help="품번")
    ap.add_argument("--out", help="out_dir 오버라이드")
    ap.add_argument("--force", action="store_true",
                    help="새 번역이 기존보다 줄 수가 적어도 덮어쓴다")
    ap.add_argument("--no-subs", action="store_true", help="plan만 교체하고 자막 재생성은 생략")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    if args.out:
        cfg["out_dir"] = args.out

    rows = []
    for code in args.codes:
        code = code.upper()
        outdir = stages.work_dir(cfg, code)
        plan_p = outdir / f"{code}_plan.json"
        tr_p = outdir / f"{code}_대사번역.json"
        keep_p = outdir / f"{code}_keep전사.json"
        if not plan_p.is_file():
            rows.append((code, "✘ plan.json 없음", 0, 0, 0))
            continue
        if not tr_p.is_file():
            rows.append((code, f"✘ {tr_p.name} 없음 — 번역본을 먼저 작성", 0, 0, 0))
            continue

        plan = json.loads(plan_p.read_text(encoding="utf-8"))
        new = json.loads(tr_p.read_text(encoding="utf-8"))
        old = plan.get("dialogue") or []
        n_src = len(json.loads(keep_p.read_text(encoding="utf-8"))) if keep_p.is_file() else 0

        # 형식 검증 — 한 줄이라도 어긋나면 자막 단계에서 조용히 사라진다.
        clean = []
        for i, d in enumerate(new):
            try:
                s, e, ko = float(d["start"]), float(d["end"]), str(d["ko"]).strip()
            except (KeyError, TypeError, ValueError):
                rows.append((code, f"✘ {i}번 항목 형식 오류(start/end/ko 필요)", len(old), 0, n_src))
                clean = None
                break
            if not ko or e <= s:
                continue                      # 빈 줄·역전 구간은 버린다
            sp = str(d.get("speaker") or "여")
            clean.append({"start": round(s, 2), "end": round(e, 2), "ko": ko,
                          "speaker": "남" if sp.startswith("남") else "여"})
        if clean is None:
            continue

        # keep 밖으로 나간 줄은 자막 재타이밍에서 어차피 버려진다 — 미리 알린다.
        keep = P.parse_keep(plan.get("keep", []))
        outside = [d for d in clean
                   if not any(a - 0.05 <= d["start"] < b + 0.05 for a, b in keep)]
        if outside:
            print(f"[{code}] ※ keep 밖 대사 {len(outside)}줄 — 자막에서 제외됩니다")

        if len(clean) < len(old) and not args.force:
            rows.append((code, f"⏭ 건너뜀: 새 번역 {len(clean)}줄 < 기존 {len(old)}줄 "
                               f"(--force 로 강제)", len(old), len(clean), n_src))
            continue

        bak = plan_p.with_suffix(".json.bak_fulldlg")
        if not bak.exists():
            shutil.copy2(plan_p, bak)
        plan["dialogue"] = clean
        plan_p.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

        note = f"✔ dialogue {len(old)} → {len(clean)}줄"
        if not args.no_subs:
            try:
                stages.stage_subs(cfg, code, CliEmitter(code))
                note += " · 자막 재생성"
            except Exception as e:
                note += f" · ✘ 자막 재생성 실패: {e}"
        rows.append((code, note, len(old), len(clean), n_src))

    print(f"\n{'=' * 70}\n요약  (정밀전사 = keep 안 일본어 줄 수)")
    for code, note, old, new, src in rows:
        cov = f"  커버리지 {new}/{src} ({new / src * 100:.0f}%)" if src else ""
        print(f"  {code}: {note}{cov}")


if __name__ == "__main__":
    main()
