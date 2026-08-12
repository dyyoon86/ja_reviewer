# -*- coding: utf-8 -*-
"""내레이션 시나리오 검수 — 다 쓴 내레이션을 **다른 눈으로 다시 읽어** 결함을 잡는다.

내레이션을 쓰는 LLM은 자기가 쓴 걸 검사하지 않는다. 그래서 ja18에서 이런 것들이
그대로 납품까지 갔다:

  · DSOD-001 — 화면에 초록 트레이닝복·번호표·붉은 감시자·카운트다운이 다 나오는데
    **오징어게임이라는 말을 끝까지 안 했다**. 게다가 '이 작품이 어떤 작품인지'를
    설명하는 줄이 아예 없어 장면 중계만 9줄 이어졌다.
  · ABF-375 — 형이 케이크를 들고 들어오는 장면에 "형이 들어서자 팔짱이 더
    단단해집니다"를 썼다. 누구 팔짱인지 없고(주어 없음), '더 단단해졌다'는 근거도
    브리핑에 없다(한 시점만 보고 변화를 지어냄). 정작 그 순간의 사건(케이크·생일)은
    안 짚었다.

둘 다 사람이 한 번 읽으면 즉시 걸리는 종류다. 그래서 **작성과 분리된 검수 패스**를
둔다. 판정 근거는 전부 파일에 있는 사실(시각브리핑·줄거리·대사)로 한정하고,
취향 문제는 건드리지 않는다 — 고칠 수 있는 결함만 집는다.

출력: {code}_내레이션검수.json  {ok, issues:[{n,text,type,why,fix}], note}
실패는 soft-fail(ok=None) — 검수 자체가 파이프라인을 막지 않는다.
"""
import json
import os
import re
import subprocess
from pathlib import Path

from server.core.llm import _cli_path

# 고칠 수 있는 결함만. '재미없다' 같은 취향은 넣지 않는다(판정이 흔들린다).
CHECKS = [
    ("개괄없음", "이 작품이 **어떤 작품인지** 알려주는 줄이 없다. 무대·인물 관계·상황을 "
                "요약하는 줄이 최소 1줄은 있어야 한다. 장면 중계만 이어지면 결함이다."),
    ("패러디누락", "[작품 판정]의 '패러디:'가 '없음'이 아닌데 내레이션이 그 작품명을 "
                  "한 번도 언급하지 않았다."),
    ("근거없는변화", "'더 ~해진다 / 굳어진다 / 좁혀진다'처럼 앞뒤를 비교하는 표현인데, "
                    "시각브리핑의 두 시점에 그런 차이가 적혀 있지 않다."),
    ("주어없음", "누구를 가리키는지 알 수 없는 신체·동작 묘사('팔짱이 단단해집니다'). "
                "인물을 밝혀야 한다."),
    ("화면불일치", "시각브리핑·대사와 어긋나는 사실을 말한다(없는 사건·없는 소품·틀린 인물)."),
    ("사건놓침", "그 시각에 벌어지는 주된 사건을 두고 화면 구석의 사소한 자세·소품을 "
                "주인공으로 삼았다."),
    ("소재반복", "같은 소재(표정·미소·눈빛 등)를 세 번 이상 우려먹는다."),
    ("AI상투어", "'매력적인/인상적인/주목할 만한/기대가 됩니다/~하는 모습을 보여줍니다' 같은 "
                "상투어, 또는 '지금까지 ~였습니다' 류 마무리 인사."),
    ("지시불명", "무엇을 가리키는지 없는 문장('받아든 순간' — 무엇을 받아들었는지 없음)."),
    ("결말누설", "결말·반전의 결과를 그대로 밝힌다."),
]


def _load(folder: Path, code: str):
    """검수에 필요한 재료를 모은다. 없으면 빈 값(soft)."""
    def rd(name):
        p = folder / name
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    nar = []
    nf = folder / f"{code}_내레이션.json"
    if nf.is_file():
        try:
            nar = [d for d in json.loads(nf.read_text(encoding="utf-8"))]
            nar.sort(key=lambda d: float(d.get("start", 0)))
        except Exception:
            nar = []
    brief = rd(f"{code}_시각브리핑.txt")
    overview = "\n".join(ln for ln in brief.splitlines()
                         if re.match(r"\s*(설정|파러디|패러디|장르)\s*[:：]", ln))
    summary, dlg = "", []
    pf = folder / f"{code}_plan.json"
    if pf.is_file():
        try:
            plan = json.loads(pf.read_text(encoding="utf-8"))
            summary = plan.get("summary") or ""
            dlg = plan.get("dialogue") or []
        except Exception:
            pass
    return nar, brief, overview, summary, dlg


def review(folder, code=None, model="sonnet", log=print):
    """내레이션 검수. 반환 dict(ok/issues/note). 실패해도 예외를 올리지 않는다."""
    folder = Path(folder)
    code = code or folder.name
    nar, brief, overview, summary, dlg = _load(folder, code)
    if not nar:
        log("※ 검수: 내레이션 없음 — 건너뜀")
        return {"ok": None, "issues": [], "note": "내레이션 없음"}

    lines = "\n".join(f'{i}. [{float(d.get("start", 0)):.0f}s] {d.get("text","")}'
                      for i, d in enumerate(nar, 1))
    # ★plan.dialogue 의 본문 키는 'ko' 다('text'가 아니다 — 처음에 text로 꺼내다
    #   대사가 통째로 빈 채 프롬프트에 들어가, 근거 있는 줄까지 '화면불일치'로
    #   오탐이 쏟아졌다). 다른 경로에서 온 dict도 받도록 둘 다 본다.
    dlg_txt = "\n".join(
        f'[{float(d.get("start", 0)):.0f}s] {d.get("ko") or d.get("text") or ""}'
        for d in dlg[:400]) or "(없음)"
    checks = "\n".join(f" - {k}: {v}" for k, v in CHECKS)
    prompt = f"""너는 영상 리뷰 채널의 내레이션 검수자다. 아래 내레이션을 읽고 **고칠 수 있는 결함만**
집어내라. 재미·취향에 대한 감상은 쓰지 마라.

[내레이션 슬롯 구조 — 줄마다 역할이 다르다. 역할대로 쓴 줄을 결함으로 잡지 마라]
 1번 = 작품 소개("N 번째 작품은 {{배우}}입니다") — 배우 이름이 나오는 게 정상이다.
 2번 = 배우 맥락(레이블·경력·나이대 등) — 화면 사건과 무관한 게 정상이다.
 3번 = **작품 개괄**(무대·인물 관계·설정·왜 볼만한지).
 4번~ = 장면 중계.
 마지막 = 마무리(총평·관전포인트) — 작품 전체 감상으로 끝나는 게 정상이다.
 ※ 슬롯이 적은 편은 1·2·3번이 한두 줄로 합쳐질 수 있다. 합쳐졌으면 그것으로 충족이다.

[검사 항목]
{checks}

[작품 판정]
{overview or "(없음)"}

[줄거리]
{summary or "(없음)"}

[대사 — 인물 이름·발언의 근거다. 여기 나오는 이름은 정당한 정보다]
{dlg_txt}

[화면 시각정보 — 행동·소품·거리감의 근거]
{brief or "(없음)"}

규칙 — ★오탐을 내지 마라. 애매하면 보고하지 않는다:
 · **배우 이름·제작사(레이블)·배우 신체정보는 메타 DB에서 온다.** 시각브리핑에 없다고
   '화면불일치'로 잡지 마라 — 이건 정상이다.
 · **등장인물 이름은 대사에서 온다.** 위 [대사]에 나오면 근거 있는 정보다.
 · '화면불일치'는 **자료와 정면으로 어긋날 때만** 쓴다(없는 사건을 지어냄, 인물을 바꿔 씀).
   '자료에 안 보인다'는 이유만으로는 결함이 아니다.
 · '근거없는변화'는 비교 표현('더 ~해진다')이 실제로 있을 때만 본다. 상태 서술
   ('팔짱을 풀지 않네요')은 결함이 아니다.
 · 결함이 있는 줄만 보고한다. 멀쩡한 줄은 쓰지 마라. 없으면 issues를 빈 배열로 둔다.
 · '개괄없음'·'패러디누락'은 전체 문제이므로 n=0 으로 적는다.
 · fix에는 **그 자리에 대신 쓸 문장**을 한 줄로 제안한다(같은 길이 안에서).

[검수 대상 내레이션]
{lines}

출력: JSON만. 설명·머리말 금지.
{{"ok": true|false, "issues": [{{"n": 줄번호, "text": "문제 문장", "type": "항목명",
  "why": "왜 결함인지 한 줄", "fix": "대신 쓸 문장"}}], "note": "총평 한 줄"}}"""

    try:
        exe = _cli_path("claude")
        env = dict(os.environ, DISABLE_OMC="1")
        r = subprocess.run([exe, "-p", "--model", model], input=prompt,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=600)
    except Exception as e:
        log(f"※ 검수 호출 실패({type(e).__name__}: {e}) — 검수 없이 진행")
        return {"ok": None, "issues": [], "note": f"호출 실패: {e}"}

    raw = (r.stdout or "").strip().replace("```json", "").replace("```", "").strip()
    s, e = raw.find("{"), raw.rfind("}") + 1
    if s < 0 or e <= s:
        log("※ 검수: JSON 응답 아님 — 건너뜀")
        return {"ok": None, "issues": [], "note": "응답 파싱 실패"}
    try:
        out = json.loads(raw[s:e])
    except Exception as ex:
        log(f"※ 검수: JSON 파싱 실패({ex}) — 건너뜀")
        return {"ok": None, "issues": [], "note": "응답 파싱 실패"}

    out.setdefault("issues", [])
    out["ok"] = not out["issues"]
    (folder / f"{code}_내레이션검수.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if out["issues"]:
        log(f"⚠ 내레이션 검수 결함 {len(out['issues'])}건")
        for it in out["issues"][:6]:
            log(f"   [{it.get('type')}] {it.get('n')}번: {str(it.get('why'))[:70]}")
    else:
        log("✔ 내레이션 검수 통과")
    return out
