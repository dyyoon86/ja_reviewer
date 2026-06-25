#!/usr/bin/env python3
"""
딸딸기튜브 신작 리뷰 나래이션 생성기 — "3분휴지" 스타일.

입력: 품번 리스트 + 각 품번의 일본어 SRT(대사) 파일.
메타: jav_2026.db 에서 품번으로 자동 조회(배우·레이블·메이커·감독·시리즈·장르·스리사이즈).
처리: codex(gpt)로 작품당 4단 구조(전환→시놉시스→솔직평가→총평) 풀리뷰 생성.
      ※ 평가/비교는 영상을 못 보므로 그럴듯하게 '창작'(사용자 선택 옵션2). 메타·시놉시스와 모순 없게.
출력: 전체 나래이션 대본 .txt (인트로 + 작품들 + 아웃트로), 작품당 별점 포함.

사용:
  srt/ 폴더에 <품번>.srt 넣고:
    python gen_narration.py JUR-086 JUR-090 ... --srt-dir srt --out script.txt
  품번 목록 파일로:
    python gen_narration.py --codes-file codes.txt --srt-dir srt
"""
import os
import re
import sys
import json
import argparse
import subprocess
import sqlite3
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT  = Path(__file__).parent
DB    = ROOT / "jav_2026.db"
CODEX = os.path.expanduser("~/.hermes/node/bin/codex")

# ─── 시그니처 (브랜드) — 여기만 바꾸면 색이 바뀜 ──────────────────────────────
CHANNEL    = "딸딸기튜브"
CATCHPHRASE = "딸기 한 알 챙겨 가세요 🍓"   # 클로징에 녹임
SRT_CHARS  = 2600     # 작품당 대사 최대 글자(프롬프트 절약)

SCHEMA = {
    "type": "object",
    "properties": {
        "intro": {"type": "string"},
        "works": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code":  {"type": "string"},
                    "stars": {"type": "integer", "minimum": 1, "maximum": 5},
                    "text":  {"type": "string"},
                },
                "required": ["code", "stars", "text"],
            },
        },
        "outro": {"type": "string"},
    },
    "required": ["intro", "works", "outro"],
}

STYLE = f"""당신은 일본 신작 AV를 리뷰하는 한국 유튜브 채널 "{CHANNEL}"의 나래이터다.
아래 작품들을 '3분휴지' 채널과 같은 스타일로 리뷰하는 나래이션 대본을 써라.

[작품당 4단 구조] (작품마다 표현을 다르게, 절대 반복 금지)
1) 전환: 첫 작품은 "먼저 OOO의 신작입니다", 이후는 "다음은 OOO의 신작입니다"(데뷔작이면 "데뷔작입니다"). OOO=배우 한국어명.
2) 시놉시스: 대사(SRT)로 파악한 설정/줄거리를 한 문장으로. "~을 다룬 작품입니다 / ~라는 내용의 작품입니다".
3) 솔직 평가: 배우의 폼·매력·컨셉 적합성·완성도·레이블/시리즈 맥락을 솔직한 호불호로. 칭찬도 비판도 자연스럽게.
4) 총평: "~작품이었습니다 / 추천드립니다 / 아쉬웠습니다" 류로 마무리.

[톤·문체]
- 정중체(~습니다) 1인칭 리뷰어. 솔직하고 직설적. 가끔 가벼운 촌평 유머 한 스푼.
- 마니아 은어 자연스럽게 사용: 미드(가슴)·포텐·폼·피지컬·육덕·고인물·하메리(POV데이트물)·1인칭(POV)·해금작·평타·NTR·진삼국무쌍(다대일).
- 레이블명(마돈나·S1·무디즈·SOD 등)과 시리즈를 맥락에 녹여라.
- 배우 스리사이즈가 주어지면 가슴 컵/키 등을 자연스럽게 언급(예: H컵, 키 168).

[중요 — 사실성]
- 시놉시스·배우명·레이블·시리즈·신체는 주어진 데이터에 '정확히' 근거할 것.
- 영상 디테일(연기·조명·특정 장면·외모변화·배우 비교)은 그럴듯하게 써도 되나, 주어진 설정/장르와 모순되면 안 됨.

[인트로] {CHANNEL} 채널 톤으로 짧게 시작(인사+오늘 신작 소개 시작). 과하지 않게 1~2문장.
[아웃트로] 마무리 인사 + "{CATCHPHRASE}" 를 자연스럽게 녹이고 "지금까지 {CHANNEL}였습니다"로 끝.
[별점] 작품마다 1~5 정수로 평가(stars).

반드시 아래 JSON 스키마로만 출력: {{intro, works:[{{code,stars,text}}], outro}}.
text 는 전환~총평까지 이어진 한국어 나래이션 한 단락(별점 문구는 넣지 말 것)."""


def parse_srt(path: Path) -> str:
    """SRT/텍스트에서 대사만 추출 → 합쳐서 SRT_CHARS로 절단."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln or ln.isdigit():
            continue
        if re.match(r"^\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}\s*-->", ln):
            continue
        ln = re.sub(r"<[^>]+>", "", ln)
        lines.append(ln)
    txt = " ".join(lines)
    return txt[:SRT_CHARS]


def fetch_meta(con, code: str) -> dict:
    con.row_factory = sqlite3.Row
    r = con.execute(
        "SELECT w.code,w.actress,w.actress_ja,w.label,w.label_ja,w.maker,w.maker_ja,"
        "w.director,w.director_ja,w.series_ja,w.title,w.title_ja,w.title_en,"
        "w.description,w.tags,w.genre,w.release_date,w.runtime_mins,"
        "w.views,w.likes,w.dislikes,w.hook_title,w.hook_desc,"
        "a.bust,a.waist,a.hip,a.cup,a.height,a.birthday,a.blood_type "
        "FROM works w LEFT JOIN actresses a ON a.name_ja=w.actress_ja WHERE w.code=?",
        (code,)).fetchone()
    if not r:
        return {"code": code}
    d = dict(r)
    try:
        d["genres"] = [x[0] for x in con.execute(
            "SELECT c.name_ko FROM work_categories wc JOIN categories c ON c.id=wc.cat_id "
            "WHERE wc.code=? AND COALESCE(c.hide,0)=0 AND c.name_ko!='' ORDER BY wc.rowid LIMIT 6",
            (code,)).fetchall()]
    except Exception:
        d["genres"] = []
    return d


def meas_str(d):
    if d.get("bust"):
        s = f"B{d['bust']}" + (f"({d['cup']}컵)" if d.get("cup") else "")
        if d.get("waist"): s += f" W{d['waist']}"
        if d.get("hip"):   s += f" H{d['hip']}"
        if d.get("height"): s += f" 키{d['height']}"
        return s
    return ""


def build_prompt(works):
    blocks = []
    for i, (m, srt) in enumerate(works, 1):
        meas = meas_str(m)
        bio = " / ".join(x for x in [
            f"생일 {m.get('birthday')}" if m.get('birthday') else "",
            f"{m.get('blood_type')}형" if m.get('blood_type') else "",
        ] if x)
        pop = f"조회 {m.get('views') or 0} · 👍{m.get('likes') or 0} 👎{m.get('dislikes') or 0}"
        blocks.append(
            f"[{i}] 품번:{m.get('code')}\n"
            f"  배우:{m.get('actress') or '?'} ({m.get('actress_ja') or '?'}){(' · '+bio) if bio else ''}\n"
            f"  신체:{meas or '-'}\n"
            f"  레이블:{m.get('label') or m.get('label_ja') or '?'} / 메이커:{m.get('maker') or '?'} / 감독:{m.get('director') or '?'}\n"
            f"  시리즈:{m.get('series_ja') or '-'} / 발매:{(m.get('release_date') or '')[:10]} / 런타임:{m.get('runtime_mins') or '?'}분 / {pop}\n"
            f"  장르:{', '.join(m.get('genres') or []) or (m.get('genre') or '-')}\n"
            f"  태그:{(m.get('tags') or '-')[:200]}\n"
            f"  제목(일):{m.get('title_ja') or '-'}\n"
            f"  시놉시스(DB, 한국어): {(m.get('description') or '-')[:400]}\n"
            f"  대사(일본어, 보조 — 줄거리 디테일): {srt[:1200] if srt else '(없음)'}\n"
        )
    note = ("\n\n[시놉시스 출처 우선순위] DB 시놉시스(한국어)·제목(일)·태그·장르를 1순위 근거로, "
            "대사(SRT)는 디테일 보강용으로만 쓴다. DB에 시놉시스가 있으면 SRT 없어도 충분히 리뷰 가능.")
    return STYLE + note + "\n\n===== 작품 데이터 =====\n" + "\n".join(blocks)


def call_codex(prompt):
    with tempfile.TemporaryDirectory() as td:
        schema_f = Path(td) / "schema.json"; schema_f.write_text(json.dumps(SCHEMA))
        out_f = Path(td) / "out.json"
        cmd = [CODEX, "exec", "--ephemeral", "--skip-git-repo-check",
               "--output-schema", str(schema_f), "-o", str(out_f), prompt]
        env = dict(os.environ)
        env["PATH"] = os.path.expanduser("~/.hermes/node/bin") + ":" + env.get("PATH", "")
        subprocess.run(cmd, env=env, timeout=420, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        txt = out_f.read_text() if out_f.exists() else ""
    s = txt.strip()
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try: return json.loads(s[i:j+1])
        except Exception: return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*", help="품번 목록")
    ap.add_argument("--codes-file", help="품번 목록 파일(한 줄에 하나)")
    ap.add_argument("--srt-dir", default="srt", help="SRT 폴더 (기본 srt/, 파일명=<품번>.srt)")
    ap.add_argument("--out", default="narration.txt", help="출력 대본 파일")
    args = ap.parse_args()

    codes = list(args.codes)
    if args.codes_file:
        codes += [l.strip() for l in Path(args.codes_file).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not codes:
        sys.exit("품번을 인자나 --codes-file 로 주세요.")

    srt_dir = Path(args.srt_dir)
    con = sqlite3.connect(DB)
    works = []
    for code in codes:
        m = fetch_meta(con, code)
        srt_path = srt_dir / f"{code}.srt"
        if not srt_path.exists():
            srt_path = srt_dir / f"{code}.txt"
        srt = parse_srt(srt_path) if srt_path.exists() else ""
        if not srt_path.exists():
            print(f"  ⚠ SRT 없음: {code} (장르·제목으로 추정 생성)")
        works.append((m, srt))
    con.close()

    print(f"[생성] {len(works)}작품 → codex 호출 중...")
    res = call_codex(build_prompt(works))
    if not res:
        sys.exit("codex 응답 파싱 실패. 다시 시도해 주세요.")

    # 대본 조립
    by_code = {w["code"]: w for w in res.get("works", [])}
    lines = [res.get("intro", "").strip(), ""]
    for code in codes:
        w = by_code.get(code)
        if not w:
            continue
        stars = "★" * int(w.get("stars", 3)) + "☆" * (5 - int(w.get("stars", 3)))
        lines.append(f"── {code}  {stars}")
        lines.append(w.get("text", "").strip())
        lines.append("")
    lines.append(res.get("outro", "").strip())

    out = Path(args.out)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[완료] {out}  ({len(by_code)}작품 멘트 생성)")


if __name__ == "__main__":
    main()
