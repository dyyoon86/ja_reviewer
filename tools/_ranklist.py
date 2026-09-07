# -*- coding: utf-8 -*-
r"""랭킹 목록 파서 — 소스 폴더의 `NN.txt`(사이트에서 복사한 랭킹 덤프)를 읽어
**게재 순서 = 순위**로 품번 목록을 만든다.

ja20까지는 랭킹을 도구 파일 안에 ITEMS로 하드코딩했다(`_review_rank_ja20.py` 등).
배치마다 도구 3개를 복사·수정해야 했고, 순서가 틀려도 알 수가 없었다.
ja21부터는 사용자가 소스 폴더에 넣어 두는 랭킹 txt 하나가 진실의 원천이다.

txt 형식(사이트 복사본 그대로):
    ABF-382
    👁 71,338
    👍 46 / 👎 1 (+45)
    ...
    MIKR-118
    ...
품번만 줄 단독으로 나오는 것을 이용해 등장 순서대로 집는다(중복은 첫 등장만).

사용:
    from _ranklist import load_rank, match_videos
    items = load_rank(r"C:\...\ja21\21.txt")          # [(1,'ABF-382'), (2,'MIKR-118'), ...]
    pairs, missing, extra = match_videos(items, src)  # 폴더 mp4와 대조
"""
import re
from pathlib import Path

# 줄 전체가 품번인 경우만 — 본문 설명의 'ABF382,ABF 382' 같은 꼬리는 잡지 않는다.
CODE_LINE = re.compile(r"^([A-Za-z]{2,6})-(\d{2,5})$")
# 파일명에서 품번 추정 — batch_clean.guess_code / app.guess_code 와 동일 규칙
CODE_RE = re.compile(r"([A-Za-z]{2,6})-?(\d{2,5})")


def guess_code(name):
    m = CODE_RE.search(Path(name).stem)
    return f"{m.group(1)}-{m.group(2)}".upper() if m else ""


def find_rank_file(src):
    """소스 폴더에서 랭킹 txt를 찾는다 — 폴더명 숫자(ja21→21.txt)를 먼저, 없으면 유일한 txt."""
    src = Path(src)
    m = re.search(r"(\d+)\s*$", src.name)
    if m:
        f = src / f"{m.group(1)}.txt"
        if f.is_file():
            return f
    txts = sorted(src.glob("*.txt"))
    if len(txts) == 1:
        return txts[0]
    if not txts:
        raise FileNotFoundError(f"랭킹 txt를 찾을 수 없습니다: {src}")
    raise RuntimeError(f"txt가 여러 개라 어느 것이 랭킹인지 모릅니다: "
                       f"{', '.join(t.name for t in txts)} — 경로를 직접 주세요")


def load_rank(path):
    """랭킹 txt → [(순위, 품번), ...] 게재 순서대로. 중복 품번은 첫 등장만 남긴다."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    out, seen = [], set()
    for ln in text.splitlines():
        m = CODE_LINE.match(ln.strip())
        if not m:
            continue
        code = f"{m.group(1)}-{m.group(2)}".upper()
        if code in seen:
            continue
        seen.add(code)
        out.append((len(out) + 1, code))
    if not out:
        raise RuntimeError(f"랭킹 txt에서 품번을 한 개도 못 찾았습니다: {path}")
    return out


def load_details(path):
    """랭킹 txt → {품번: {actress, maker, label, director, runtime, desc}}.

    사이트에서 복사한 덤프라 항목이 라벨/값 두 줄로 번갈아 나온다. 작품 설명은
    ISO 타임스탬프(`2026-09-04T03:00:08`) 바로 앞의 마지막 비어 있지 않은 줄이다 —
    라벨이 붙어 있지 않아 위치로 잡는 게 가장 안정적이다.

    이 설명은 섹션② 줄거리 브리핑의 **크로스 검증 기준**으로 쓴다: 전사로 만든
    줄거리가 사이트 소개와 어긋나면 전사를 잘못 읽었다는 뜻이다.
    """
    lines = [l.rstrip() for l in Path(path).read_text(encoding="utf-8",
                                                      errors="replace").splitlines()]
    ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
    LABELS = {"제작사": "maker", "레이블": "label", "감독": "director", "장르": "genre"}
    out, cur = {}, None
    for i, ln in enumerate(lines):
        s = ln.strip()
        m = CODE_LINE.match(s)
        if m:
            cur = f"{m.group(1)}-{m.group(2)}".upper()
            out.setdefault(cur, {"code": cur})
            # 품번 다음 다섯 줄 안에 배우명(이모지·#·기호가 아닌 줄)이 온다
            for nxt in lines[i + 1:i + 7]:
                t = nxt.strip()
                if t and not t[0] in "👁👍🔥📅⏱📂⚠#" and not t.startswith("("):
                    out[cur].setdefault("actress", t)
                    break
            continue
        if not cur:
            continue
        if s in LABELS:
            val = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if val and val != "—":
                out[cur][LABELS[s]] = val
        elif s.startswith("⏱"):
            out[cur]["runtime"] = s[1:].strip()
        elif s.startswith("👁"):
            m2 = re.search(r"([\d,]+)", s)
            if m2:
                out[cur]["views"] = int(m2.group(1).replace(",", ""))
        elif s.startswith("👍"):
            # "👍 46 / 👎 1 (+45)"
            m2 = re.search(r"👍\s*([\d,]+).*?👎\s*([\d,]+)", s)
            if m2:
                out[cur]["likes"] = int(m2.group(1).replace(",", ""))
                out[cur]["dislikes"] = int(m2.group(2).replace(",", ""))
        elif ISO.match(s):
            for prev in range(i - 1, -1, -1):
                p = lines[prev].strip()
                if p:
                    if len(p) > 20 and not ISO.match(p):
                        out[cur]["desc"] = p
                    break
    return out


def match_videos(items, src):
    """랭킹 목록을 소스 폴더의 mp4와 대조.
    반환: (pairs, missing, extra)
      pairs   = [(순위, 품번, Path)]  — 랭킹 순서, 영상이 실제로 있는 것만
      missing = 랭킹엔 있는데 영상이 없는 품번
      extra   = 영상은 있는데 랭킹에 없는 품번(뒤에 순서대로 덧붙일 후보)
    """
    have = {}
    for v in sorted(Path(src).glob("*.mp4")):
        c = guess_code(v.name)
        if c:
            have.setdefault(c, v)
    pairs, missing = [], []
    for rank, code in items:
        if code in have:
            pairs.append((rank, code, have[code]))
        else:
            missing.append(code)
    ranked = {c for _, c in items}
    extra = [c for c in have if c not in ranked]
    return pairs, missing, extra


def codes_in_rank(src, rank_file=None):
    """소스 폴더 → 랭킹 순 품번 리스트(영상 있는 것만). 도구들이 쓰는 입구."""
    items = load_rank(rank_file or find_rank_file(src))
    pairs, missing, extra = match_videos(items, src)
    return [c for _, c, _ in pairs], missing, extra


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    src = sys.argv[1]
    items = load_rank(sys.argv[2] if len(sys.argv) > 2 else find_rank_file(src))
    pairs, missing, extra = match_videos(items, src)
    print(f"랭킹 {len(items)}건 / 영상 매칭 {len(pairs)}건")
    for rank, code, v in pairs:
        print(f"  {rank:2}위  {code:11} {v.name}")
    if missing:
        print(f"  ⚠ 영상 없음: {', '.join(missing)}")
    if extra:
        print(f"  ⚠ 랭킹에 없는 영상: {', '.join(extra)}")
