# -*- coding: utf-8 -*-
r"""대사 자막 **재생성** — keep·내레이션·영상은 그대로 두고 대사만 다시 만든다.

왜 필요한가: 대사가 빠졌다고 섹션2(stage_ai)를 다시 돌리면 keep 선정과 내레이션까지
전부 새로 쓴다. 검수를 끝낸 내레이션이 통째로 바뀌므로 쓸 수가 없다
(2026-09-07 사용자: "내용이 왜 다 싹 바뀌었어").

이 도구는 plan.json 의 keep 을 **읽기만** 하고
  ① 클린본의 keep 구간을 large-v3 로 재전사(정밀전사.json 갱신)
  ② 모든 줄을 번역(_translate_all — 누락분만 재요청)
  ③ retime 으로 최종영상 좌표로 옮겨 대사.srt / 대사.json 재작성
만 한다. 내레이션 파일과 final.mp4 는 손대지 않는다.

언제 쓰나: check_before_tts.py 가 "자막누락"으로 잡았을 때.

사용:
  python tools\fix_dialogue.py --out F:\ja_reviewer_out\ja21_v2 --code MIDA-798
  python tools\fix_dialogue.py --out ... --code A --code B      # 여러 편
  python tools\fix_dialogue.py --out ... --code X --dry          # 전사까지만, 저장 안 함
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from batch_clean import CliEmitter
from server import pipeline as P
from server import stages
from server.core.transcribe import transcribe_ranges


def fix_one(cfg, code, outdir, llm, dry=False):
    d = outdir / code
    plan_f = d / f"{code}_plan.json"
    if not plan_f.is_file():
        print(f"✗ {code}: plan.json 이 없습니다 — 섹션2를 먼저 돌려야 합니다")
        return False
    plan = json.loads(plan_f.read_text(encoding="utf-8"))
    keep = P.parse_keep(plan.get("keep") or [])
    if not keep:
        print(f"✗ {code}: plan 에 keep 구간이 없습니다")
        return False
    clean = d / f"{code}_클린.mp4"
    if not clean.is_file():
        clean = Path(stages.load_state(d, code).get("video") or "")
    if not clean.is_file():
        print(f"✗ {code}: 클린본 영상을 찾을 수 없습니다")
        return False

    em = CliEmitter(code)
    before = len(json.loads((d / f"{code}_정밀전사.json").read_text(encoding="utf-8"))) \
        if (d / f"{code}_정밀전사.json").is_file() else 0
    model = cfg.get("whisper_model", "large-v3")
    fine = transcribe_ranges(str(clean), keep, model, em.log)
    # 겹침 기준으로 거른다(포함관계로 거르면 구간 첫 발화가 통째로 빠진다 — 2026-09-08)
    fine = [s for s in fine if any(min(s[1], b) - max(s[0], a) > 0.05 for a, b in keep)]
    if not fine:
        print(f"✗ {code}: 정밀 전사가 0줄입니다 — 중단(기존 자막 유지)")
        return False
    print(f"   정밀전사 {before}줄 → {len(fine)}줄")
    if dry:
        for a, b, t in fine:
            print(f"     {a:7.1f}~{b:7.1f}  {t[:50]}")
        return True

    m = P.fetch_meta(cfg["meta_api"], code, em.log)
    dlg = stages._translate_all(m, fine, llm, em)
    if not dlg:
        print(f"✗ {code}: 번역이 0줄입니다 — 중단(기존 자막 유지)")
        return False
    (d / f"{code}_정밀전사.json").write_text(
        json.dumps([{"start": round(a, 3), "end": round(b, 3), "text": t}
                    for a, b, t in fine], ensure_ascii=False, indent=1), encoding="utf-8")
    # _translate_all 은 LLM 원본 dict 를 준다 — stage_subs 와 같은 변환을 거쳐야
    # retime 이 먹는 (start, end, text, speaker) 튜플이 된다.
    rows = P.parse_lines(dlg, ("ko", "text"), extra=[("speaker", "여")], log=em.log)
    stages.write_dialogue(d, code, P.retime(rows, keep, snap=False))
    # plan 의 dialogue 도 갱신해 둔다 — 섹션3 자막 재생성이 plan 을 다시 읽는다
    plan["dialogue"] = dlg
    plan_f.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✔ {code}: 한글 대사 {len(rows)}줄 → {code}_대사.srt / .json 다시 씀 "
          f"(내레이션·영상 그대로)")
    return True


def main():
    ap = argparse.ArgumentParser(description="대사 자막만 재생성(내레이션 보존)")
    ap.add_argument("--out", help="out_dir. 생략 시 config out_dir")
    ap.add_argument("--code", action="append", required=True, help="품번(여러 번 지정 가능)")
    ap.add_argument("--llm", help="llm 라우팅. 생략 시 config llm")
    ap.add_argument("--dry", action="store_true", help="전사까지만 하고 저장하지 않음")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    outdir = Path(args.out or cfg["out_dir"])
    llm = args.llm or cfg.get("llm", "codex")
    ok = 0
    for code in args.code:
        print(f"\n═══ {code}")
        if fix_one(cfg, code, outdir, llm, dry=args.dry):
            ok += 1
    print(f"\n{ok}/{len(args.code)}편 완료")
    return 0 if ok == len(args.code) else 1


if __name__ == "__main__":
    sys.exit(main())
