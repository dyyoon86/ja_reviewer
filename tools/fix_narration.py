# -*- coding: utf-8 -*-
r"""내레이션 **줄 단위 수정** — 시간은 그대로 두고 텍스트만 바꾼다.

왜 필요한가: regen_narration 은 호출할 때마다 전 줄을 새로 쓴다. 마지막 한 줄이
마음에 안 들어 규칙을 고치고 다시 돌리면 **멀쩡하던 8줄까지 통째로 바뀐다**
(2026-09-07 사용자: "내용이 왜 다 싹 바뀌었어, 마지막 줄 빼고는 다 괜찮았는데").
검수는 한 줄씩 하는데 생성은 전부라 반복 수정이 불가능했다.

이 도구는 `{code}_내레이션.json` 의 슬롯 시간을 그대로 두고 text 만 갈아끼운 뒤
SRT 를 다시 쓴다. 글자수 상한도 여기서 검사한다.

사용:
  # 9번 줄만 교체
  python tools\fix_narration.py --out F:\...\ja21_v2 --code MIDA-798 \
      --set 9="모르던 정보까지 주는 작품이었습니다."

  # 여러 줄 한 번에 (--set 반복)
  python tools\fix_narration.py --out ... --code X --set 1="..." --set 9="..."

  # 파일로 통째 교체(한 줄에 한 문장, 빈 줄 무시)
  python tools\fix_narration.py --out ... --code X --from-file lines.txt

  # 현재 내용 보기
  python tools\fix_narration.py --out ... --code X --show
"""
import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server.core.common import s2srt, srt_parse
from server.core.regen import NAR_MAX_CHARS, CONCEPT_MAX_CHARS

# ★2026-09-07 — 이 도구는 원래 {code}_내레이션.json 을 읽어 SRT 를 다시 썼는데,
#   **둘은 좌표계가 다르다**: JSON 은 클린본(trim) 기준, SRT 는 최종 영상 기준이다
#   (regen_narration 이 retime 을 거쳐 SRT 만 변환하고 JSON 은 "참고용"으로 원좌표 저장).
#   그래서 이 도구를 쓴 편은 SRT 시각이 영상 길이를 넘어갔다(MIDA-798: 142초 영상에
#   201.6초). → **SRT 를 진실의 원천으로 삼고** 시각은 SRT 것을 그대로 유지한다.
#   JSON 은 텍스트만 같은 순서로 갱신한다(시각은 건드리지 않는다).


def write_srt(rows, path):
    """드립(화면 전용)은 빼고 SRT 로 — write_narration 과 같은 규칙."""
    out, n = [], 0
    for r in rows:
        if (r.get("style") or "기본") == "드립":
            continue
        n += 1
        out += [str(n), f"{s2srt(r['start'])} --> {s2srt(r['end'])}", r["text"], ""]
    path.write_text("\n".join(out), encoding="utf-8-sig")
    return n


def main():
    ap = argparse.ArgumentParser(description="내레이션 줄 단위 수정(시간 유지)")
    ap.add_argument("--out", help="out_dir. 생략 시 config out_dir")
    ap.add_argument("--code", required=True, help="품번")
    ap.add_argument("--set", action="append", default=[], metavar="N=문장",
                    help="N번째 줄을 이 문장으로 교체(1부터). 여러 번 지정 가능")
    ap.add_argument("--from-file", help="한 줄에 한 문장인 파일로 전체 교체")
    ap.add_argument("--show", action="store_true", help="현재 내용만 출력")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    outdir = Path(args.out or cfg["out_dir"]) / args.code
    jf = outdir / f"{args.code}_내레이션.json"
    sf = outdir / f"{args.code}_내레이션.srt"
    if not sf.is_file():
        print(f"✗ 내레이션 SRT 없음: {sf}")
        return 1
    # ★시각은 SRT 것을 쓴다(최종 영상 좌표). JSON 시각은 클린본 좌표라 쓰면 안 된다.
    rows = [{"start": a, "end": b, "text": t, "style": "기본"}
            for a, b, t in srt_parse(sf)]
    if not rows:
        print(f"✗ SRT 에 항목이 없습니다: {sf}")
        return 1

    if args.show or (not args.set and not args.from_file):
        print(f"{args.code} — {len(rows)}줄")
        for i, r in enumerate(rows, 1):
            t = r["text"]
            print(f"  {i:2}. [{r['start']:6.1f}~{r['end']:6.1f}s] {t}  ({len(t)}자)")
        return 0

    new_texts = {}
    if args.from_file:
        lines = [l.strip() for l in Path(args.from_file).read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        if len(lines) != len(rows):
            print(f"✗ 줄 수가 다릅니다 — 파일 {len(lines)}줄 vs 내레이션 {len(rows)}줄")
            return 1
        new_texts = {i + 1: t for i, t in enumerate(lines)}
    for kv in args.set:
        k, _, v = kv.partition("=")
        try:
            idx = int(k.strip())
        except ValueError:
            print(f"✗ --set 형식은 'N=문장' 입니다: {kv}")
            return 1
        if not 1 <= idx <= len(rows):
            print(f"✗ {idx}번 줄이 없습니다(1~{len(rows)})")
            return 1
        new_texts[idx] = v.strip().strip('"').strip("'")

    changed = 0
    for idx, txt in sorted(new_texts.items()):
        r = rows[idx - 1]
        if r["text"] == txt:
            continue
        # 슬롯 길이로 상한을 잡는다 — 넘으면 TTS 가 다음 문장을 밀어낸다
        span = r["end"] - r["start"]
        cap = min(CONCEPT_MAX_CHARS, max(NAR_MAX_CHARS, int(span * 7.5)))
        if len(txt) > cap:
            print(f"  ⚠ {idx}번 {len(txt)}자 — 이 슬롯({span:.1f}초)의 상한 {cap}자를 넘습니다. "
                  f"TTS 가 뒤 문장을 밀어냅니다")
        print(f"  {idx:2}. {r['text']}\n   →  {txt}")
        r["text"] = txt
        changed += 1

    if not changed:
        print("바뀐 줄이 없습니다.")
        return 0
    n = write_srt(rows, sf)
    # JSON 은 텍스트만 같은 순서로 맞춰 둔다 — 시각(클린본 좌표)은 절대 건드리지 않는다.
    if jf.is_file():
        try:
            j = json.loads(jf.read_text(encoding="utf-8"))
            spoken = [r for r in j if (r.get("style") or "기본") != "드립"]
            if len(spoken) == len(rows):
                for jr, nr in zip(spoken, rows):
                    jr["text"] = nr["text"]
                jf.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                print(f"  ※ JSON 줄 수({len(spoken)})가 SRT({len(rows)})와 달라 "
                      f"JSON 텍스트는 갱신하지 않았습니다")
        except Exception as e:
            print(f"  ※ JSON 갱신 실패({e}) — SRT 는 정상 저장됨")
    print(f"\n✔ {changed}줄 수정 — SRT {n}줄 다시 씀 (시각은 SRT 원본 유지)")
    print(f"   {sf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
