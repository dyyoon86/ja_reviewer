# -*- coding: utf-8 -*-
"""자막 점검에서 '진짜 누락'으로 확정된 대사만 plan.dialogue 에 끼워 넣는다.

`_subcheck_0813.py` 로 구멍을 찾고 → 앞뒤 자막과 대조해 타이밍 여유(이미 자막 있음)를
걸러내고 → `_gapdump_0813.py` 로 그 구간만 정밀 재전사해 원문을 확인한 뒤, 남은 것만
아래 표에 한국어로 적었다. 잡음·감탄사·이미 같은 뜻의 자막이 있는 건 넣지 않는다.

시각은 **현재 final 좌표**로 적는다 — final_to_src 로 클린본 좌표로 되돌려 plan 에
넣으므로, 이후 stage_subs 가 다시 최종컷 좌표로 재타이밍한다(다음 재컷에도 살아남는다).

사용: .venv\\Scripts\\python.exe tools\\_addlines_0813.py --out <out_dir>
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server import pipeline as P
from batch_clean import CliEmitter
from trim_final_flags import final_to_src

# 품번: [(final 시작, final 끝, 한국어, 화자)]   화자 '남'이면 하늘색으로 구워진다
ADD = {
    # ── 1차분(반영 완료). 다시 돌리면 중복으로 들어가니 주석 유지
    # "IPZZ-907": [(11.0, 14.5, "누나 같은 느낌이네요", "남")],
    # "IPZZ-932": [(58.2, 60.1, "춥네요", "여")],
    # "SNOS-321": [(48.7, 49.6, "응, 알겠어", "남"),
    #              (49.7, 54.1, "이런 일로 무너질 만큼 내 꿈은 약하지 않아", "여")],
    # "SNOS-353": [(65.9, 67.0, "네", "남"),
    #              (67.1, 70.2, "저, 부모님이 지금 시골에 가 계셔서", "남")],
    # ── 2차분: 재검증에서 남은 것 중 '한 문장이 잘려 뒷부분에 자막이 없는' 두 곳
    "SNOS-353": [(16.5, 19.0, "특별히 리드해 보기도 하고", "여"),
                 (53.4, 56.4, "싫은 거야?", "여")],
}


def main():
    ap = argparse.ArgumentParser(description="누락 대사 주입 + 자막 재생성")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    cfg["out_dir"] = args.out
    out = Path(args.out)

    for code, rows in ADD.items():
        d = out / code
        plan_f = d / f"{code}_plan.json"
        plan = json.loads(plan_f.read_text(encoding="utf-8"))
        keep = P.parse_keep(plan.get("keep", []))
        dlg = list(plan.get("dialogue") or [])
        print(f"\n{'=' * 66}\n{code}  (기존 대사 {len(dlg)}줄)")
        added = 0
        for a, b, ko, spk in rows:
            src = final_to_src([(a, b)], keep)
            if not src:
                print(f"  ✘ {a:.1f}~{b:.1f} 는 keep 밖 — 건너뜀")
                continue
            s, e = src[0]
            print(f"  + final {a:6.1f}~{b:6.1f} → 클린 {s:8.2f}~{e:8.2f}  [{spk}] {ko}")
            dlg.append({"start": round(s, 3), "end": round(e, 3), "ko": ko, "speaker": spk})
            added += 1
        if args.dry or not added:
            continue
        dlg.sort(key=lambda x: float(x.get("start", 0)))
        bak = plan_f.with_suffix(".json.bak_addlines")
        if not bak.exists():
            bak.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        plan["dialogue"] = dlg
        plan_f.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        n_before = len(P.srt_parse(str(d / f"{code}_대사.srt")))
        stages.stage_subs(cfg, code, CliEmitter(code))
        n_after = len(P.srt_parse(str(d / f"{code}_대사.srt")))
        print(f"  ✔ 대사 자막 {n_before} → {n_after}줄")


if __name__ == "__main__":
    main()
