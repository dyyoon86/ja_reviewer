# -*- coding: utf-8 -*-
"""우분투 meta_api(172.30.1.40:8770) 대체 — 소스 폴더의 content.txt로 /work/{품번} 응답.

ja19처럼 **작품이 로컬 jav_2026.db에 아직 없는 신작 모음집**을 우분투가 죽은 상태에서
돌려야 할 때 쓴다. stage_ai 의 `P.fetch_meta` 는 예외를 안 잡으므로 메타가 없으면
②AI가 전편 실패한다 — 그 구멍만 메우는 최소 서버.

- 작품 정보(배우/발매/런타임/제작사/레이블/감독/설명/조회·좋아요)는 content.txt에서 파싱
- 3사이즈·컵·키·생일·혈액형·일본어 배우명은 로컬 jav_scrap/jav_2026.db 의 actresses 에서
  한국어 배우명으로 역조회(있으면 채우고, 없으면 빈 값 — 인포카드에서만 아쉬운 값들)

사용: .venv\\Scripts\\python.exe tools\\_meta_shim.py \
        --content "C:\\Users\\yoon\\Desktop\\2026-04-23_JA_Review\\ja19\\content.txt" [--port 8770]
"""
import argparse
import json
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ACT_DB = Path(r"E:\vscode\workspace\jav_scrap\jav_2026.db")
CODE_RE = re.compile(r"^[A-Z]{2,6}-\d{2,5}$")
LABELED = {"제작사": "maker", "레이블": "label", "감독": "director", "장르": "genre"}


def _int(s):
    try:
        return int(re.sub(r"[^\d]", "", s or "") or 0)
    except ValueError:
        return 0


def parse_content(path: Path) -> dict:
    """content.txt → {품번: meta dict}. 레코드는 품번 줄로 시작해 다음 품번 줄 직전까지."""
    lines = [l.rstrip("\n") for l in path.read_text(encoding="utf-8").splitlines()]
    starts = [i for i, l in enumerate(lines) if CODE_RE.match(l.strip())]
    out = {}
    for n, i in enumerate(starts):
        j = starts[n + 1] if n + 1 < len(starts) else len(lines)
        out.update(_parse_block(lines[i:j]))
    return out


def _parse_block(block):
    code = block[0].strip()
    m = {"code": code, "actress": "", "actress_ja": "", "release_date": "", "runtime_mins": None,
         "maker": "", "label": "", "director": "", "genre": "", "series_ja": "",
         "title": "", "title_ja": "", "title_en": "", "description": "",
         "views": 0, "likes": 0, "dislikes": 0, "hook_title": "", "hook_desc": "",
         "tags": "", "genres": []}
    body = []          # 라벨/기호가 아닌 자유 텍스트 줄(배우명·설명이 여기 섞여 온다)
    i = 1
    while i < len(block):
        s = block[i].strip()
        i += 1
        if not s:
            continue
        if s.startswith("👁"):
            m["views"] = _int(s)
        elif s.startswith("👍"):
            nums = re.findall(r"\d[\d,]*", s)
            if nums:
                m["likes"] = _int(nums[0])
            if len(nums) > 1:
                m["dislikes"] = _int(nums[1])
        elif s.startswith("📅"):
            d = re.search(r"\d{4}-\d{2}-\d{2}", s)
            if d:
                m["release_date"] = d.group(0)
        elif s.startswith("⏱"):
            m["runtime_mins"] = _int(s) or None
        elif s.startswith(("🔥", "#", "⚠")):
            continue
        elif s in LABELED:                      # '제작사' 다음 줄이 값
            val = block[i].strip() if i < len(block) else ""
            i += 1
            if val and val != "—":
                m[LABELED[s]] = val
        elif re.match(r"^\d{4}-\d{2}-\d{2}T", s):
            continue                            # 수집 시각
        else:
            body.append(s)
    # 자유 텍스트: 첫 줄 = 배우명, 가장 긴 줄 = 시놉시스
    if body:
        m["actress"] = body[0]
        longest = max(body, key=len)
        if longest is not body[0] or len(body) == 1:
            m["description"] = longest
        # 시놉시스 꼬리의 "CODE123,CODE 123" 검색어 잔재 제거
        m["description"] = re.sub(r"\s*[A-Z]{2,6}\d{2,5}\s*,\s*[A-Z]{2,6}\s*\d{2,5}\s*$", "",
                                  m["description"]).strip()
    # ★ title 은 인포카드 제목 줄로 나간다. 품번·배우는 바로 위아래에 따로 찍히므로
    #   "품번 - 배우"를 넣으면 같은 정보가 세 번 반복된다 → 실제 제목(시놉시스)을 쓴다.
    #   더 나은 값은 _hook_titles.json 의 짧은 후킹 제목(load_hooks 가 덮어쓴다).
    m["title"] = m["description"] or code
    return {code: m}


def _ja_name(con, ko):
    """한국어 배우명 → 일본어명. actresses 에는 한국어 컬럼이 없으므로(name_ja/kana/romaji뿐)
    works.actress(한국어) ↔ works.actress_ja 로 한 번 건너뛴다."""
    for q, a in (("SELECT actress_ja, COUNT(*) n FROM works WHERE actress=? AND actress_ja!='' "
                  "GROUP BY actress_ja ORDER BY n DESC LIMIT 1", (ko,)),
                 ("SELECT actress_ja, COUNT(*) n FROM works WHERE actress LIKE ? AND actress_ja!='' "
                  "GROUP BY actress_ja ORDER BY n DESC LIMIT 1", (f"%{ko}%",))):
        try:
            r = con.execute(q, a).fetchone()
        except sqlite3.Error:
            r = None
        if r and r[0]:
            return r[0]
    return None


def enrich(m, con):
    """로컬 actresses 에서 신체정보 보강(없으면 그대로 — 인포카드에서만 아쉬운 값)."""
    name = (m.get("actress") or "").strip()
    if not name or con is None:
        return m
    con.row_factory = sqlite3.Row
    # "코우키 미아(미아 카미키)" → 본명·별명 둘 다 시도(사이트마다 표기가 갈린다)
    cands = [re.sub(r"\(.*?\)", "", name).strip()]
    alias = re.search(r"\((.*?)\)", name)
    if alias:
        cands.append(alias.group(1).strip())
    row = None
    for ko in cands:
        ja = _ja_name(con, ko)
        if not ja:
            continue
        row = con.execute("SELECT * FROM actresses WHERE name_ja=?", (ja,)).fetchone()
        if row:
            break
    if not row:
        return m
    d = dict(row)
    for k in ("bust", "waist", "hip", "cup", "height", "birthday", "blood_type"):
        if d.get(k):
            m[k] = d[k]
    if d.get("name_ja"):
        m["actress_ja"] = d["name_ja"]
    # ★2026-08-19 사진도 실어 보낸다 — 이게 빠져서 ja19 워터마크 얼굴이 딸기 마스코트로
    #   떨어졌다. gen_infocard 는 actress_ja 로 로컬 actresses 를 한 번 더 뒤지지만
    #   이름 매핑이 어긋나면 실패하므로, 여기서 확보한 경로를 그대로 넘겨 폴백을 만든다.
    if d.get("photo_path"):
        m["actress_photo"] = d["photo_path"]
    return m


def meas_str(d):
    if d.get("bust"):
        s = f"B{d['bust']}" + (f"({d['cup']}컵)" if d.get("cup") else "")
        if d.get("waist"):  s += f" W{d['waist']}"
        if d.get("hip"):    s += f" H{d['hip']}"
        if d.get("height"): s += f" 키{d['height']}"
        return s
    return ""


def load_hooks(path: Path, meta: dict, log=print):
    """_hook_titles.json({품번: 짧은 후킹 제목}) → meta[code]['hook_title'].
    gen_infocard 는 hook_title → title → 원제 순으로 인포카드 제목을 고르므로,
    이 파일이 있으면 긴 시놉시스 대신 한 줄 후킹 제목이 배너에 나간다(ja18과 같은 방식)."""
    if not path.is_file():
        log(f"후킹 제목 파일 없음 — 시놉시스를 제목으로 사용: {path}")
        return 0
    hooks = json.loads(path.read_text(encoding="utf-8"))
    n = 0
    for code, t in hooks.items():
        c = code.strip().upper()
        if c in meta and str(t).strip():
            meta[c]["hook_title"] = str(t).strip()
            n += 1
    log(f"후킹 제목 {n}건 적용 ← {path}")
    return n


def build_handler(meta):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, status=200):
            b = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path in ("/health", "/"):
                return self._json({"ok": True, "service": "meta-shim(content.txt)",
                                   "codes": sorted(meta)})
            if path.startswith("/work/"):
                code = path[len("/work/"):].strip("/").upper()
                m = meta.get(code)
                if not m:
                    return self._json({"error": f"{code} not found in content.txt"}, 404)
                return self._json(m)
            self.send_error(404)
    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--content", required=True, help="소스 폴더의 content.txt")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--hooks", help="_hook_titles.json 경로(배너 제목용 짧은 후킹 제목)")
    ap.add_argument("--dump", action="store_true", help="서버 안 띄우고 파싱 결과만 출력")
    args = ap.parse_args()

    meta = parse_content(Path(args.content))
    con = sqlite3.connect(str(ACT_DB)) if ACT_DB.is_file() else None
    for c in meta:
        meta[c] = enrich(meta[c], con)
        meta[c]["meas"] = meas_str(meta[c])
    if con:
        con.close()
    if args.hooks:
        load_hooks(Path(args.hooks), meta)

    print(f"content.txt 파싱 {len(meta)}건 ← {args.content}")
    for c in sorted(meta):
        m = meta[c]
        print(f"  {c:<11} {m['actress']:<20} {m['release_date']} {m['runtime_mins']}분 "
              f"{m['maker']}/{m['label']} 신체:{m['meas'] or '-'} "
              f"제목:{(m.get('hook_title') or m['title'])[:24]}")
    if args.dump:
        return
    srv = ThreadingHTTPServer((args.host, args.port), build_handler(meta))
    print(f"\nmeta-shim on http://{args.host}:{args.port}  (우분투 meta_api 대체)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
