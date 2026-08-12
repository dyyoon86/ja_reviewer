# -*- coding: utf-8 -*-
"""이미 만든 배너를 **메타 조회 없이** 새 디자인으로 다시 렌더한다.

gen_infocard는 작품이 로컬 DB에 없으면 meta_api(우분투)에서 메타를 받아온다.
우분투가 죽어 있으면 배너를 처음부터 다시 만들 수 없다. 그런데 각 편의
`_infocard_{품번}/_L_info.html`·`_L_wm.html` 에 **그때 렌더한 HTML이 통째로 남아
있어서**, 거기서 값을 도로 읽어내면 메타 서버 없이 재렌더가 된다.

읽어내는 값: 품번 · 배우(한/일) · 레이블 · 발매일 · 별점 · 좋아요% · 3사이즈 · 컵 · 키
얼굴 사진은 `_L_wm.html` 안의 base64를 그대로 되꺼내 쓴다(이미 맞는 사진이 박혀 있다).

빠진 값은 두 군데서 채운다:
  · 3사이즈/컵/키 → 로컬 actresses 테이블(이름으로 조회)
  · 제목(hook_title) → --titles JSON (AI가 쓴 한글 후킹 제목)

★배우 이름 한자 표기가 사이트마다 다르다(七島舞 vs 七嶋舞, 百田光希 vs 百田光稀).
  meta_api가 준 이름으로 로컬 DB를 못 찾으면 스펙이 통째로 비는데, 화면에서는
  '3사이즈가 없다'로만 보여 원인을 알기 어렵다. ALIAS에 등록해 잇는다.

사용:
  .venv\\Scripts\\python.exe tools\\_rebanner.py --out <out_dir> --titles titles.json [품번...]
  titles.json = {"ABF-375": "단둘이 남은 식탁, 위로가 길어졌다", ...}
"""
import argparse
import base64
import html as _html
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
sys.path.insert(0, str(Path(_common.__file__).resolve().parents[1]))
import gen_infocard as G  # noqa: E402

DB = Path(r"E:\vscode\workspace\jav_scrap\jav_2026.db")
ALIAS = {"七島舞": "七嶋舞"}          # meta_api 표기 → 로컬 DB 표기
# ★일본어 이름이 **아예 안 실려온** 편도 있다(ABF-375). 그러면 조회할 키가 없어
#   스펙이 통째로 빈다 — 한글 이름으로라도 잇는다. actresses 테이블에 한글 컬럼이
#   없어서 자동 매칭이 불가능하므로 여기에 손으로 등록한다.
KO_ALIAS = {"나나시마 마이": "七嶋舞"}

_RE_CODE = re.compile(r"<div class=code>(.*?)</div>", re.S)
_RE_NAME = re.compile(r"<div class=name>(.*?)</div>", re.S)
_RE_JA = re.compile(r'<span class="ja">(.*?)</span>', re.S)
_RE_META = re.compile(r"<div class=meta>(.*?)</div>", re.S)
_RE_SPAN = re.compile(r"<span(?: class=dot)?>(.*?)</span>", re.S)
_RE_FACE = re.compile(r"background-image:url\((data:image/(\w+);base64,([^)]*))\)")

_RE_MEAS = re.compile(r"^B(\d+)\s*·\s*W(\d+)\s*·\s*H(\d+)$")
_RE_CUP = re.compile(r"^([A-Z]+)컵$")
_RE_CM = re.compile(r"^(\d+)cm$")
_RE_DATE = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")
_RE_STAR = re.compile(r"^★\s*([\d.]+)$")
_RE_LIKE = re.compile(r"^좋아요\s*(\d+)%$")


def parse_old(icdir: Path, code: str) -> dict:
    """옛 인포카드 HTML에서 메타를 복원한다."""
    f = icdir / "_L_info.html"
    if not f.is_file():
        raise RuntimeError(f"{f.name} 없음 — 배너를 만든 적이 없는 편")
    s = f.read_text(encoding="utf-8")
    m = {"thumb": "", "code": code, "title": "", "actress": "", "actress_ja": "",
         "photo": "", "label": "", "release": "", "runtime": "", "views": "0",
         "like_pct": 0, "star": 0.0,
         "bust": None, "waist": None, "hip": None, "cup": None, "height": None}

    mo = _RE_CODE.search(s)
    if mo:
        m["code"] = _html.unescape(mo.group(1)).strip()
    mo = _RE_NAME.search(s)
    if mo:
        raw = mo.group(1)
        ja = _RE_JA.search(raw)
        if ja:
            m["actress_ja"] = _html.unescape(ja.group(1)).strip()
            raw = _RE_JA.sub("", raw)
        m["actress"] = _html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
    mo = _RE_META.search(s)
    for b in (_RE_SPAN.findall(mo.group(1)) if mo else []):
        b = _html.unescape(re.sub(r"<[^>]+>", "", b)).strip()
        if not b or b == "·":
            continue
        if (x := _RE_MEAS.match(b)):
            m["bust"], m["waist"], m["hip"] = (int(v) for v in x.groups())
        elif (x := _RE_CUP.match(b)):
            m["cup"] = x.group(1)
        elif (x := _RE_CM.match(b)):
            m["height"] = int(x.group(1))
        elif _RE_DATE.match(b):
            m["release"] = b
        elif (x := _RE_STAR.match(b)):
            m["star"] = float(x.group(1))
        elif (x := _RE_LIKE.match(b)):
            m["like_pct"] = int(x.group(1))
        elif not m["label"]:
            m["label"] = b
    return m


def face_from_wm(icdir: Path, tmpdir: Path):
    """워터마크 HTML에 박힌 얼굴 base64를 파일로 되꺼낸다(마스코트면 None)."""
    f = icdir / "_L_wm.html"
    if not f.is_file():
        return None
    mo = _RE_FACE.search(f.read_text(encoding="utf-8"))
    if not mo or mo.group(2) == "png":     # png = 딸기 마스코트 폴백
        return None
    p = tmpdir / "face.jpg"
    p.write_bytes(base64.b64decode(mo.group(3)))
    return str(p)


def fill_specs(m: dict, log=print):
    """3사이즈·컵·키가 비었으면 로컬 actresses에서 채운다."""
    if m["bust"] and m["waist"] and m["hip"] and m["cup"] and m["height"]:
        return
    name = m.get("actress_ja") or KO_ALIAS.get((m.get("actress") or "").strip(), "")
    name = ALIAS.get(name, name)
    if not name or not DB.is_file():
        if not name:
            log(f"    ※ 조회할 배우 이름이 없다 — KO_ALIAS에 '{m.get('actress')}' 등록 필요")
        return
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    # 이름이 "A, B"처럼 여러 명이면 앞사람 기준(카드에 한 명분만 들어간다)
    for cand in [name] + [x.strip() for x in re.split(r"[,、/]", name) if x.strip()]:
        cand = ALIAS.get(cand, cand)
        r = db.execute("SELECT * FROM actresses WHERE name_ja=?", (cand,)).fetchone()
        if r:
            d = dict(r)
            got = []
            for k in ("bust", "waist", "hip", "cup", "height"):
                if not m.get(k) and d.get(k):
                    m[k] = d[k]; got.append(k)
            if got:
                log(f"    스펙 보강({cand}): {', '.join(got)}")
            if not m.get("actress_ja"):
                m["actress_ja"] = cand
            break
    db.close()


def main():
    ap = argparse.ArgumentParser(description="배너 재렌더(메타 조회 없이)")
    ap.add_argument("codes", nargs="*", help="품번(생략 시 out_dir의 _infocard_* 전부)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--titles", help="hook_title JSON {품번: 제목}")
    args = ap.parse_args()

    out = Path(args.out)
    titles = json.loads(Path(args.titles).read_text(encoding="utf-8")) if args.titles else {}
    codes = [c.upper() for c in args.codes] or sorted(
        p.name[len("_infocard_"):] for p in out.glob("_infocard_*") if p.is_dir())

    rows = []
    for code in codes:
        icdir = out / f"_infocard_{code}"
        print(f"\n[{code}]", flush=True)
        try:
            m = parse_old(icdir, code)
            m["title"] = titles.get(code, "")
            fill_specs(m)
            with tempfile.TemporaryDirectory() as td:
                m["photo"] = face_from_wm(icdir, Path(td)) or ""
                if not m["photo"]:
                    print("    ※ 얼굴 사진 없음 — 마스코트로 렌더된다")
                layers = G.render_layers(m, str(icdir))
                for k, fname in (("frame", f"{code}_프레임.png"),
                                 ("info", f"{code}_인포카드.png"),
                                 ("wm", f"{code}_워터마크.png")):
                    shutil.copyfile(layers[k], icdir / fname)
            # 우상단 사본은 굽기가 새로 만든다 — 옛것이 남으면 옛 워터마크가 나간다
            (icdir / f"{code}_워터마크_tr.png").unlink(missing_ok=True)
            spec = (f'B{m["bust"]}·W{m["waist"]}·H{m["hip"]}'
                    if m["bust"] else "3사이즈 없음")
            rows.append((code, f'✔ {spec} / 제목 {"O" if m["title"] else "X"}'))
            print(f"    {m['actress']} {m['actress_ja']} · {spec} · 제목 {m['title'] or '(없음)'}")
        except Exception as e:
            import traceback; traceback.print_exc()
            rows.append((code, f"✘ {e}"))

    print("\n요약")
    for c, n in rows:
        print(f"  {c}: {n}")
    fails = sum(1 for _, n in rows if n.startswith("✘"))
    print(f"\n완료 {len(rows) - fails}/{len(rows)}" + (f", 실패 {fails}" if fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
