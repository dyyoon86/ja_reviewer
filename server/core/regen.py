#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""③ 결과 재생성 — 내레이션 재생성(6슬롯) + keep 구간 재선정(replan).

GUI(③ 결과 화면 버튼)와 tools/ CLI가 공유한다.
- regen_narration: plan.json의 내레이션만 6슬롯 규칙(인트로/갭/아웃트로 어미)으로
  다시 쓰고 SRT/JSON을 갱신한다. keep·대사·영상은 그대로.
- replan: LLM에게 keep 구간을 다시 고르게 해 plan.json 갱신 + final.mp4 재컷.
  (대사/내레이션 SRT는 호출부에서 stage_subs로 다시 굽는다)
"""
import json
import re
import subprocess
from pathlib import Path

from .common import s2srt, retime, parse_keep, video_duration, invalidate_derived
from .llm import fetch_meta, _cli_path, call_llm
from .prompts import prompt_manual
from .cutter import cut_video

# 내레이션 슬롯 배분 기준 (2026-07-30, "초반 내레이션이 숨도 안 쉰다" 대응)
# voicebox 실측 발화속도 7~8.5자/초 → 프롬프트 상한인 25자 문장에 3.3초가 필요하다.
# 슬롯 길이와 글자수 상한은 **한 쌍**이다 — 발화속도 실측 7.5자/초 기준으로
# 글자수 ≈ 슬롯초 × 7.5 를 넘으면 TTS가 슬롯을 넘겨 대사를 덮는다.
#   3.5s / 25자 : 문장이 여유롭지만 대사 빽빽한 작품은 자리가 3~4개뿐
#   2.5s / 18자 : 자리가 1.5~2배 늘어난다(2026-07-31 사용자 요청, 현재 설정)
NAR_SLOT_MIN = 2.5    # 한 문장이 압축 없이 들어가는 최소 슬롯(초)
NAR_ITEM_GAP = 0.35   # 같은 창 안 문장 사이 숨돌림(초). tts.MIN_GAP과 짝을 맞춘다
NAR_DLG_PAD = 0.35    # 대사 앞뒤로 비워둘 여유(초) — 내레이션이 대사를 앞지르지 않게
NAR_CPS = 7.5         # voicebox 실측 발화속도(자/초) — 글자수↔슬롯초 환산 기준
NAR_MAX_CHARS = 30    # 절대 상한(자막 한 줄이 넘치지 않는 선)


def _char_budget(win_sec, cps=NAR_CPS, lo=12, hi=NAR_MAX_CHARS):
    """창 길이 → 그 안에 압축 없이 들어가는 글자수. 슬롯마다 다르게 준다."""
    return max(lo, min(hi, int(win_sec * cps)))


# ─── keep 구간 재선정 ─────────────────────────────────────────────────────────
def replan(folder: Path, meta_api: str, llm="claude", target=60, log=print):
    """전사(trim 기준)를 LLM에 다시 보내 keep을 재선정하고 final.mp4를 다시 컷."""
    folder = Path(folder)
    code = folder.name
    tj = folder / f"{code}_전사.json"
    pf = folder / f"{code}_plan.json"
    vf = folder / f"{code}_trim.mp4"       # 이미 trim된 영상 사용

    if not tj.exists(): raise RuntimeError(f"전사 파일 없음: {tj}")
    if not vf.exists(): raise RuntimeError(f"trim 영상 없음: {vf}")

    segs = [(d["start"], d["end"], d["text"])
            for d in json.loads(tj.read_text(encoding="utf-8"))]
    log(f"전사 라인: {len(segs)}개")

    log("메타 조회 중...")
    try:
        meta = fetch_meta(meta_api, code, log=log)
    except Exception as e:
        log(f"  메타 실패: {e} — 빈 메타로 진행")
        meta = {"code": code}

    prompt = prompt_manual(meta, segs, target)
    log(f"프롬프트 {len(prompt)}자 — {llm} 호출 중...")

    res = call_llm(prompt, llm, log=log)
    keep = parse_keep(res.get("keep", []))
    if not keep:
        raise RuntimeError("LLM이 keep 구간을 못 골랐습니다.")

    total = sum(e - s for s, e in keep)
    log(f"새 keep: {len(keep)}구간, 합계 {total:.1f}초 (target {target}초)")
    for s, e in keep:
        log(f"  [{s:.1f}, {e:.1f}] = {e-s:.1f}초")

    pf.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"plan.json 저장: {pf}")

    final = str(folder / f"{code}_final.mp4")
    log("컷 영상 생성 중...")
    cut_video(str(vf), keep, final, log=log)
    invalidate_derived(folder, code, log)
    dur = video_duration(final)
    log(f"완료: {final} ({dur:.1f}초)")
    return res


# ─── 내레이션 재생성 (6슬롯) ─────────────────────────────────────────────────
def _dialogue_after(slot_end, dialogue, window=25.0):
    lines = []
    for d in dialogue:
        s = d.get("start", 0)
        if slot_end <= s <= slot_end + window:
            lines.append(d.get("ko", "")[:22])
        if len(lines) >= 2: break
    return lines


def _dialogue_before(slot_start, dialogue, window=15.0):
    lines = []
    for d in reversed(dialogue):
        e = d.get("end", 0)
        if slot_start - window <= e <= slot_start:
            lines.insert(0, d.get("ko", "")[:22])
        if len(lines) >= 2: break
    return lines


def narration_slots(video_sec, lo=5, hi=10, per=15.0):
    """영상 길이 → 내레이션 슬롯 수.

    2026-08-03 사용자 지시("내레이션 짧아도 된다")로 10초당 1줄(6~14) → 15초당 1줄(5~10)로
    낮췄다. 촘촘하게 채우면 쓸 말이 떨어져 같은 소재를 반복한다(ADN-795: 14줄 중 4줄이
    '웃음기/표정/얼굴/미소'). 벤치마킹 휴지도둑(120만 조회)도 '전환점만' 짚는 저밀도다."""
    try:
        v = float(video_sec)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, round(v / per)))


def regen_narration(folder: Path, meta_api: str, log=print, seq=None, slots=None):
    """내레이션만 6슬롯(인트로 2 + 갭 3 + 아웃트로 1) 규칙으로 재생성.
    메타(배우/신체/레이블) 반영 + keep 갭 창에 길이 비례 배분 + retime(snap)으로
    {code}_내레이션.srt/.json 을 갱신한다. 반환: 새 narration 리스트."""
    folder = Path(folder)
    code = folder.name
    plan_file = folder / f"{code}_plan.json"
    if not plan_file.exists():
        raise RuntimeError(f"plan.json 없음: {plan_file}")

    plan      = json.loads(plan_file.read_text(encoding="utf-8"))
    narration = plan.get("narration", [])
    dialogue  = plan.get("dialogue", [])
    keep      = plan.get("keep", [])

    if not narration:
        raise RuntimeError("narration 항목 없음")

    # 화면 시각정보(섹션2 stage_ai가 저장) — 슬롯 시각에 맞춰 붙여 최종 내레이션도 화면 근거를 갖게 한다.
    # 좌표는 클린본 기준으로 keep/narration과 동일하다.
    vis_entries, vis_overview = [], []
    vbf = folder / f"{code}_시각브리핑.txt"
    if vbf.is_file():
        for ln in vbf.read_text(encoding="utf-8").splitlines():
            mm = re.match(r"\s*\[?(\d+)\s*s\]?\s*[:：]?\s*(.+)", ln)
            if mm:
                vis_entries.append((int(mm.group(1)), mm.group(2).strip()))
            # ★ visual._overview 가 맨 앞에 붙인 작품 판정 3줄(설정/패러디/장르).
            #   프레임 캡션은 6초씩 따로 노는 조각이라 "이게 오징어게임 패러디다" 같은
            #   전체 정체를 아무도 말해주지 않는다(DSOD-001 실패). 따로 집어 프롬프트
            #   맨 앞에 넣어 컨셉 슬롯이 반드시 쓰게 한다.
            elif re.match(r"\s*(설정|패러디|장르)\s*[:：]", ln):
                vis_overview.append(ln.strip())
        if vis_entries:
            log(f"  화면 시각정보 {len(vis_entries)}줄 반영")
        if vis_overview:
            log("  작품 판정: " + " / ".join(vis_overview))

    # 슬롯 수 목표: 영상 길이에 비례(6~14). 실제 개수는 '대사 없는 틈'이 몇 개
    # 나오는지에 따라 아래에서 다시 정한다(압축은 gap_windows 확정 후에 한다).
    if slots:
        SLOT_TARGET = max(3, int(slots))
    else:
        fin = folder / f"{code}_final.mp4"
        vsec = video_duration(str(fin)) if fin.is_file() else \
            sum(b - a for a, b in parse_keep(keep))
        SLOT_TARGET = narration_slots(vsec)
        log(f"  내레이션 슬롯 목표 {SLOT_TARGET}개 (영상 {vsec:.0f}s 기준)")

    # ── 메타 정보 ──────────────────────────────────────────────────────────
    log("메타 조회 중...")
    try:
        meta = fetch_meta(meta_api, code, log=log)
    except Exception as e:
        log(f"  메타 조회 실패 ({e}), 코드명만 사용")
        meta = {}
    actress = meta.get("actress") or code
    meas    = meta.get("meas") or ""          # "B83(C컵) W57 H89 키168"
    label   = meta.get("label") or ""         # "S1 NO.1 STYLE"
    # 신체 요약: 키+컵만 (짧게)
    height = meta.get("height") or ""
    cup    = meta.get("cup") or ""
    body_short = ""
    if height and cup:
        body_short = f"키{height} {cup}컵"
    elif meas:
        body_short = meas[:15]

    label_short = ""
    meta_line = f"{code}, {actress}."
    if body_short:
        meta_line += f" {body_short}"
    if label:
        label_short = label.replace("NO.1 STYLE","").replace("넘버.원 스타일","").strip()
        meta_line += f" / {label_short}"

    # ★ v3(2026-08-03) — 예전엔 배우명·신체·레이블과 줄거리 60자만 넘겼다. LLM이 작품을
    #   모르는 채로 '훅을 던져라'는 지시만 받으니 화면과 무관한 추상어("심상찮습니다")로
    #   도망쳤다(사용자: "뜬금없는 내레이션이 많다"). 배우/작품 팩트를 근거로 준다.
    #   ※ meta['description']·title_ja는 노골 원문이라 넣지 않는다(헤드리스 거부 유발).
    #     줄거리는 ②AI가 이미 순화해 만든 plan['summary']를 통째로 쓴다.
    def _fact_lines():
        actor, work = [], []
        actor.append(f"이름: {actress}")
        bd = str(meta.get("birthday") or "")
        rel = str(meta.get("release_date") or "")
        if len(bd) >= 4 and len(rel) >= 4:
            try:
                age = int(rel[:4]) - int(bd[:4])
                if (rel[5:10] or "12-31") < (bd[5:10] or "01-01"):
                    age -= 1
                if 15 < age < 70:
                    actor.append(f"나이: 발매 시점 만 {age}세({age // 10 * 10}대)")
            except ValueError:
                pass
        if meas:
            actor.append(f"신체: {meas}")
        # 레이블 표기 고정 — v5에서 '어태커스'(오타), '팔레노스타'/'팔레노 스타'(띄어쓰기
        # 불일치)가 나왔다. 한글 메이커명을 정답으로 주고 변형을 금지한다.
        canon = (meta.get("maker") or "").strip() or label_short or label
        if canon:
            actor.append(f"레이블 표기: '{canon}' — 부를 때 이 표기를 그대로 쓸 것"
                         f"(철자·띄어쓰기 변형 금지, 영문·축약 금지)")
        if rel:
            work.append(f"발매일: {rel}")
        if meta.get("runtime_mins"):
            work.append(f"원본 러닝타임: {meta['runtime_mins']}분")
        gs = meta.get("genres") or []
        if isinstance(gs, list) and gs:
            work.append("장르 태그: " + ", ".join(str(g) for g in gs[:6]))
        # ★ 등장인물·주도권 (2026-08-03 검수에서 걸린 오류 2건 차단)
        #   ① "며느리와 시아버지만 남았습니다" — 대사에 아들이 있는데 인물을 빠뜨림
        #   ② "남편 친구와 단둘, 물러설 데가 없죠" — 치녀물이라 밀리는 쪽은 남자인데 반대로 씀
        #   대사 화자 분포를 넘겨 인물 수와 주도권을 사실로 못박는다.
        spk = {}
        for x in dialogue or []:
            k = x.get("speaker") or "?"
            spk[k] = spk.get(k, 0) + 1
        if spk:
            work.append("대사 화자 분포: "
                        + ", ".join(f"{k} {v}줄" for k, v in sorted(spk.items(), key=lambda kv: -kv[1]))
                        + " — 말을 많이 하고 상황을 끌고 가는 쪽이 주도자다. "
                          "이 분포와 장르 태그에 어긋나게 주도권을 뒤집어 쓰지 말 것. "
                          "여기 없는 인물을 지어내지도, 대사에 나오는 인물을 빠뜨리지도 말 것")
        return ("[배우 팩트]\n" + "\n".join(" · " + x for x in actor) + "\n"
                + ("[작품 팩트]\n" + "\n".join(" · " + x for x in work) + "\n" if work else ""))

    fact_block = _fact_lines()
    summary = plan.get("summary", "")

    # ── 슬롯별 어미 매핑 및 설명 ────────────────────────────────────────
    # 슬롯 순서: 인트로(1) → 갭0(2) → 갭1(3) → 갭2+(4,5) → 아웃트로(6)
    # 아웃트로 스타일 로테이션 — 모음집에서 11편이 전부 "어떻게 될까요?"로 끝나면 지루하다.
    # seq(몇 번째 꼭지)에 따라 순환해 연속 작품이 같은 끝맺음을 쓰지 않게 한다.
    OUTRO_STYLES = [
        "어미=질문형 '○○는 어떻게 될까요?' 딱 1회",
        "어미=명사형 피날레 — '점점 깊어지는 두 사람.'처럼 명사로 뚝 끊기",
        "어미=단언형 — '이건 직접 봐야 압니다.'처럼 짧게 단언",
        "어미=여운형 — '이 다음은 상상에 맡기겠습니다.'처럼 여운 남기기",
        "어미=관전포인트형 — '~가 이 작품의 관전 포인트입니다.'",
        "어미=한줄평형 — '개인적으로 꽤 볼만한 작품입니다.' 식 짧은 평",
    ]
    outro_rule = OUTRO_STYLES[(seq[0] - 1) % len(OUTRO_STYLES)] if seq else OUTRO_STYLES[0]

    # 배우 슬롯 각도 로테이션 — 2026-08-03 검수: 10편 전부 2번째 줄이 '레이블은 이런 곳이죠'
    # 한 가지였고, 무디즈 디바가 4편 연속이라 같은 소개를 네 번 들었다. 각도를 돌려 겹침을 막는다.
    ACTOR_ANGLES = [
        "각도=레이블 성격 — 이 레이블이 어떤 작품을 잘 만드는 곳인지",
        "각도=배우의 현재 위치 — 나이대·연차로 보아 지금 어느 자리에 있는 배우인지",
        "각도=배역 선택 — 이번에 맡은 역의 결이 이 배우에게 어떤 선택인지",
        "각도=라인업 맥락 — 발매 시기와 장르로 보아 이 작품이 어떤 카드인지",
    ]
    actor_angle = ACTOR_ANGLES[(seq[0] - 1) % len(ACTOR_ANGLES)] if seq else ACTOR_ANGLES[0]

    EVAL_ANGLES = [
        "연기·표정이 얼마나 받쳐주는지",
        "구성·전개가 늘어지지 않는지",
        "분위기·연출이 살아 있는지",
        "후반으로 갈수록 힘이 붙는지 빠지는지",
        "이 배우를 보러 온 사람에게 값을 하는지",
    ]
    eval_angle = EVAL_ANGLES[(seq[0] - 1) % len(EVAL_ANGLES)] if seq else EVAL_ANGLES[0]

    # 슬롯 역할 v3 (2026-08-03, 사용자 지시) — v2는 '인트로=훅 / 설명문 금지 / 질문 필수'로
    # 강제해 화면과 무관한 추상 훅을 짜내게 만들었다(ja15 실물: "심상찮습니다", "공기가 좀
    # 묘합니다", "웃음부터 터집니다"). 사용자 판정: "뜬금없는 내레이션이 많다".
    # 원하는 구조는 순서대로 설명하는 리뷰다 —
    #   ① 서수+배우 소개 → ② 배우 설명 → ③ 이번 작품 컨셉 → ④~ 각 장면별 내용 → 마무리
    # 장면 슬롯은 그 시각의 '화면:'/'직전:'/'직후:' 근거로만 쓴다(뜬금없음 차단).
    # v4(2026-08-03) — v3는 구조는 맞췄지만 '담백하게/설명형'을 강조하다 리뷰어를 통째로
    # 지웠다(사용자: "전혀 재미없다"). 결과물이 화면 해설 방송 수준이었다:
    #   "제단 앞에 세 사람이 나란히 앉아 있습니다 / 고개를 숙인 채 움직이지 않습니다"
    #   → 9줄 전부 ~습니다, 시청자가 화면으로 이미 보는 것만 반복, 의견·맥락 0.
    # 벤치마킹(3분휴지 분석 §2): **장면 중계 = 화면 묘사 반 + 인물 속마음 추측 반**.
    # 구조(사용자 지시 순서)는 그대로 두고 각 슬롯이 '화면에 없는 것'을 얹게 만든다.
    ROLE_DESC = {
        "소개":   ("역할=작품 소개. '○ 번째 작품은 {a}입니다.' 형태로 연다. 이 줄만은 담백하게."
                  ).format(a=actress),
        "배우":   ("역할=배우 맥락 + 기대치. " + actor_angle + ". [배우 팩트]를 근거로 하되 "
                  "**정보를 얹어라**. 숫자 낭독('B85 W57 H82') 금지. 리뷰어 의견 1스푼 허용. "
                  "★지정된 각도를 지켜라 — 매 편 레이블 소개로만 때우면 모음집에서 같은 말이 반복된다."),
        # ★2026-08-12 — 사용자: "작품 설명이 하나도 안 들어갔다. 장면 설명도 좋지만
        #   일단 작품 개괄은 해줘야지." 실제로 DSOD-001은 컨셉 슬롯이 바로 장면으로 새서
        #   ("이삿날 들이닥친 남자들이 게임을 걸고") 이게 무슨 작품인지 끝까지 안 나왔다.
        #   컨셉 슬롯을 "작품 개괄"로 못박고, 담아야 할 것을 항목으로 지정한다.
        "컨셉":   ("역할=**작품 개괄**. 이 슬롯은 장면 중계가 아니라 '이 작품이 어떤 작품인가'를 "
                  "시청자에게 알려주는 자리다. 다음을 한 문장에 담아라 — "
                  "①어디서 누가(무대·인물 관계) ②무슨 상황인지(설정) ③그게 왜 곤란하거나 "
                  "볼만한지(스테이크). [작품 판정]과 [줄거리]가 1순위 근거다. "
                  "[작품 판정]의 '패러디:'가 '없음'이 아니면 여기서 그 작품명을 밝혀라. "
                  "★특정 시각의 장면 하나를 설명하는 것은 실패다 — 작품 전체를 요약해야 한다. "
                  "'~라는 작품입니다'로 밋밋하게 끝내지 말 것."),
        "장면":   ("역할=장면 중계 **+ 한 겹**. '화면:'과 '직전:/직후:' 대사에서 사실을 가져오되, "
                  "거기에 인물의 속마음·상황의 의미·관계 변화 중 하나를 반드시 얹는다. "
                  "★화면에 보이는 것만 그대로 옮겨 적는 것은 실패다 — 시청자는 이미 그 화면을 보고 있다. "
                  "('손을 모읍니다' ✗ / '좀처럼 손이 내려오지 않네요' ○). "
                  "화면에 없는 사건을 지어내지는 말 것 — 얹는 것은 해석이지 사실이 아니다."),
        # 평가 각도도 로테이션 — v6에서 10편 중 5편이 "설정은 뻔한데/흔한데/익숙해도"로 끝났다.
        # 어미(outro_rule)만 돌리고 평가 '내용'은 안 돌려서 같은 말이 반복됐다.
        "마무리": outro_rule + " + 결말은 절대 밝히지 말 것. 평가는 " + eval_angle
                  + " 관점에서 한 조각 곁들인다(칭찬만 하는 리뷰는 신뢰가 안 간다). "
                    "★'설정은 뻔한데'류 상투구로 시작하지 마라.",
    }
    ROLE_DESC["소개배우"] = ("역할=소개+배우맥락. '○ 번째 작품은 {a}입니다' 뒤에 레이블·배우 맥락을 "
                          "한 문장으로 붙인다.").format(a=actress)
    # 짧은 편(슬롯 4개 이하)은 소개와 개괄이 한 줄에 합쳐진다 — 그래도 개괄은 빠지면 안 된다.
    ROLE_DESC["소개컨셉"] = ("역할=소개+**작품 개괄**. '○ 번째 작품은 {a}입니다' 뒤에 "
                          "어디서 누가 무슨 상황인지와 그게 왜 볼만한지를 한 문장으로 붙인다. "
                          "[작품 판정]의 '패러디:'가 '없음'이 아니면 그 작품명을 반드시 넣는다."
                          ).format(a=actress)

    def _roles_for(n):
        """슬롯 수 → 역할 배열.

        ★2026-08-03 커버리지 검수: 슬롯을 5~6개로 줄이자 소개·배우·컨셉·마무리 4개가 고정으로
        먹어 장면이 1~2줄만 남았고, 줄거리의 핵심(ADN-788 예정일 반전, FNS-228 퇴거 통보,
        MIDA-727 협박)이 통째로 빠졌다. 줄여야 할 것은 도입부지 내용이 아니다 —
        짧은 편은 소개+배우를 한 줄로 합쳐 **장면 자리를 먼저 지킨다**."""
        if n <= 1:
            return ["소개컨셉"]
        if n == 2:
            return ["소개컨셉", "마무리"]
        if n == 3:
            return ["소개컨셉", "장면", "마무리"]
        if n == 4:
            return ["소개컨셉", "장면", "장면", "마무리"]
        if n <= 6:                      # 5~6줄: 도입 2줄만 쓰고 장면에 2~3줄
            return ["소개배우", "컨셉"] + ["장면"] * (n - 3) + ["마무리"]
        return ["소개", "배우", "컨셉"] + ["장면"] * (n - 4) + ["마무리"]

    # ── gap_windows 먼저 계산 — 프롬프트 슬롯 설명에 사용 ───────────────
    def free_intervals(keep_segs, dlg, pad=NAR_DLG_PAD):
        """keep 안에서 **대사가 말하지 않는 틈**만 남긴다(대사 앞뒤 pad 확보).
        예전 배치는 대사 타임라인을 안 봐서 내레이션이 대사를 앞지르거나 덮었다
        ("대사도 안 나왔는데 대본 자막이 먼저 나온다", 2026-07-30 SNOS-301)."""
        busy = []
        for d in dlg or []:
            try:
                busy.append([float(d["start"]) - pad, float(d["end"]) + pad])
            except (KeyError, TypeError, ValueError):
                continue
        busy.sort()
        merged = []
        for a, b in busy:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        free = []
        for ks, ke in keep_segs:
            cur = ks
            for a, b in merged:
                if b <= cur or a >= ke:
                    continue
                if a > cur:
                    free.append((cur, min(a, ke)))
                cur = max(cur, b)
                if cur >= ke:
                    break
            if cur < ke:
                free.append((cur, ke))
        return [(round(a, 2), round(b, 2)) for a, b in free if b - a >= 0.05]

    def windows_from_free(keep_segs, dlg, n_slots):
        """대사 없는 틈에만 슬롯을 놓는다 → 내레이션이 대사를 앞지르지 않는다.
        목표 개수보다 틈이 적으면 긴 틈을 반으로 쪼개 늘리고(각 조각 ≥ NAR_SLOT_MIN),
        많으면 첫/끝(인트로·아웃트로)은 남기고 짧은 중간 틈을 버린다.
        쓸 틈이 3개도 안 되면 None → 호출측이 옛 방식(keep 머리)으로 후퇴."""
        free = free_intervals(keep_segs, dlg)
        wins = [(a, b) for a, b in free if b - a >= NAR_SLOT_MIN]
        if len(wins) < 3:
            # 틈이 적으면 기준을 낮춰 한 번 더 — 조금 짧은 슬롯은 TTS가 살짝
            # 압축(≤1.12배)하거나 뒤로 밀어 흡수한다. 옛 방식(대사 위에 얹기)보다 낫다.
            wins = [(a, b) for a, b in free if b - a >= NAR_SLOT_MIN * 0.7]
        # ★ 쪼개기를 먼저 하고 개수를 판정한다 — 긴 틈 하나가 여러 슬롯이 되므로
        #   쪼개기 전 개수로 잘라내면 쓸 수 있는 자리를 놓친다(SNOS-293: 8s+4s → 3슬롯).
        # 목표보다 틈이 적으면 각 틈에 **길이 비례로** 조각 수를 배정한다.
        # ★ 예전엔 '가장 긴 틈'을 반복해서 쪼갰는데, 첫 창(인트로)을 보호하느라 두 번째로
        #   긴 틈만 계속 갈려 내레이션이 한 곳에 몰렸다(2026-08-03 ADN-788: 최종 86초 중
        #   57~65초에 4줄, 마지막 18초는 텅 빔). 비례 배분이면 긴 틈이 고르게 몫을 갖는다.
        if len(wins) < n_slots:
            def _cap(w):                      # 한 문장이 들어가는 최대 조각 수
                return max(1, int((w[1] - w[0]) // NAR_SLOT_MIN))
            total = sum(b - a for a, b in wins) or 1e-6
            quota = [min(_cap(w), max(1, round((w[1] - w[0]) / total * n_slots)))
                     for w in wins]
            while sum(quota) < n_slots:       # 여유 있는 창부터 한 조각씩 더
                cand = [i for i in range(len(wins)) if quota[i] < _cap(wins[i])]
                if not cand:
                    break
                quota[max(cand, key=lambda i: (wins[i][1] - wins[i][0]) / quota[i])] += 1
            while sum(quota) > n_slots:       # 넘치면 조각당 길이가 짧은 창부터 회수
                cand = [i for i in range(len(wins)) if quota[i] > 1]
                if not cand:
                    break
                quota[min(cand, key=lambda i: (wins[i][1] - wins[i][0]) / quota[i])] -= 1
            out = []
            for (a, b), q in zip(wins, quota):
                step = (b - a) / q
                out += [(round(a + j * step, 2), round(a + (j + 1) * step, 2))
                        for j in range(q)]
            wins = out
        wins.sort()
        if len(wins) < 3:
            return None                     # 인트로·중간·아웃트로도 못 놓으면 후퇴
        if len(wins) > n_slots and n_slots >= 3:
            # ★ 예전엔 '가장 긴 틈' 순으로 골랐다. 무대사 구간(제단·정적 장면)에 긴 틈이
            #   몰린 작품에서는 내레이션이 거기 다 뭉치고 나머지가 통째로 빈다
            #   (2026-08-03 ADN-788: 9줄 중 5줄이 204~211초 8초 안, 이후 5분 공백).
            #   시간축을 need등분해 구획마다 가장 긴 틈을 하나씩 집어 끝까지 이어지게 한다.
            head, tail, mid_pool = wins[0], wins[-1], wins[1:-1]
            need = n_slots - 2
            if mid_pool and need > 0:
                lo, hi = head[1], tail[0]
                span = max(hi - lo, 1e-6)
                used = set()
                for k in range(need):
                    a = lo + span * k / need
                    b = lo + span * (k + 1) / need
                    cand = [i for i, w in enumerate(mid_pool)
                            if i not in used and a <= (w[0] + w[1]) / 2 < b]
                    if cand:
                        used.add(max(cand, key=lambda i: mid_pool[i][1] - mid_pool[i][0]))
                if len(used) < need:   # 빈 구획이 있으면 남은 것 중 긴 순으로 채운다
                    rest = sorted((i for i in range(len(mid_pool)) if i not in used),
                                  key=lambda i: mid_pool[i][1] - mid_pool[i][0], reverse=True)
                    used.update(rest[:need - len(used)])
                wins = [head] + [mid_pool[i] for i in sorted(used)] + [tail]
            else:
                wins = [head, tail]
        return wins

    def compute_gap_windows(keep_segs, n_slots=6):
        if not keep_segs: return []
        w = windows_from_free(keep_segs, dialogue, n_slots)
        if w:
            return w
        log("  ※ 대사가 빽빽해 대사-회피 배치 불가 — keep 머리 기준으로 배치")
        windows = []
        k0s, k0e = keep_segs[0]
        seg0 = k0e - k0s
        # 인트로 2슬롯이 이 창을 반으로 갈라 쓴다. 예전엔 (seg0*0.4)만 봤더니 첫 keep이
        # 짧은 작품에서 슬롯이 1~2초로 나와 오프닝 2문장이 최대속도로 압축됐다
        # ("초반 내레이션이 숨도 안 쉰다", 2026-07-30). 실측 발화속도 7~8.5자/초이므로
        # 25자 한 문장에 NAR_SLOT_MIN(3.5s)은 있어야 한다 → 두 문장 몫을 우선 확보하고,
        # 그래도 첫 keep이 그보다 짧으면 그 90%까지만(뒤 대사를 다 먹지 않게).
        want = 2 * NAR_SLOT_MIN + NAR_ITEM_GAP
        intro_span = min(max(seg0 * 0.4, want), 12.0, seg0 * 0.9)
        mid_i = round(k0s + intro_span / 2, 2)
        end_i = round(k0s + intro_span, 2)
        windows.extend([(k0s, mid_i), (mid_i, end_i)])
        for i in range(1, len(keep_segs)):
            ks, ke = keep_segs[i]
            span = min(6.0, (ke - ks) * 0.3)
            windows.append((ks, round(ks + span, 2)))
            if len(windows) >= n_slots - 1: break
        while len(windows) < n_slots - 1:
            idx = max(range(len(windows)), key=lambda i: windows[i][1] - windows[i][0])
            s, e = windows[idx]; m = round((s + e) / 2, 2)
            windows[idx] = (s, m); windows.insert(idx + 1, (m, e))
        lks, lke = keep_segs[-1]
        outro_s = round(max(lks, lke - 3.5), 2)
        windows.append((outro_s, round(lke - 0.1, 2)))
        return windows

    gap_windows = compute_gap_windows(keep, SLOT_TARGET)
    if not gap_windows:
        gap_windows = [(n["start"], n["end"]) for n in narration]

    # 실제 슬롯 수 = 확보된 창 개수. 대사가 빽빽한 작품은 목표보다 적게 나오는데,
    # 그게 정상이다 — 자리가 없는데 밀어넣던 것이 대사와 겹치는 원인이었다.
    MAX_SLOTS = len(gap_windows)
    if MAX_SLOTS < SLOT_TARGET:
        log(f"  대사 없는 틈이 {MAX_SLOTS}개 — 내레이션을 그만큼만 놓는다"
            f"(목표 {SLOT_TARGET}개, 대사와 겹치지 않게)")
    # ── 슬롯 설명 빌드 — **창 개수가 곧 문장 수**다 ──────────────────────
    # ★ 예전엔 n_total을 기존 plan의 내레이션 개수에서 가져왔다. 그러면 앞선 실행이
    #   plan을 적은 개수로 덮어쓴 뒤에는 창이 늘어나도 그만큼만 요청하게 된다
    #   (2026-07-31: 창 4개인데 3개만 요청). 창을 진실의 원천으로 삼는다.
    n_total = MAX_SLOTS
    roles = _roles_for(n_total)
    log(f"  내레이션 {len(narration)}줄 → {n_total}줄로 재작성"
        if len(narration) != n_total else f"  내레이션 {n_total}줄")
    log("  슬롯 구성: " + " → ".join(roles))
    slots_desc = []
    budgets = []
    for i, (ws, we) in enumerate(gap_windows):
        ek = roles[i]

        before = _dialogue_before(ws, dialogue)
        after  = _dialogue_after(we, dialogue)

        # 슬롯마다 제 창 길이에 맞는 글자수를 준다 — 전역 상한 하나로 묶으면
        # 소개 슬롯("다섯 번째 작품은 하츠미 나노카입니다"만 20자)에 자리가 없다.
        # 앞머리 3종(소개/배우/컨셉)은 담을 팩트가 정해져 있어 하한을 올려준다.
        # ★소개 슬롯은 '열두 번째 작품은 {배우}입니다.'가 통째로 들어가야 한다 — 2인 작품
        #   (아오이 이부키, 아마미야 카난 = 14자)에서 상한 20자에 걸려 '여덟 번째는 …'으로
        #   형식이 깎였다(2026-08-03 MIDA-734). 배우명 길이에 맞춰 하한을 잡는다.
        if ek in ("소개", "소개배우", "소개컨셉"):
            lo = min(NAR_MAX_CHARS, len(actress) + 14)
        elif ek in ("배우", "컨셉"):
            lo = 20
        else:
            lo = 12
        budgets.append(_char_budget(we - ws, lo=lo))
        line = f"S{i+1}({ws:.0f}~{we:.0f}초, {budgets[-1]}자 이내) {ROLE_DESC[ek]}"
        if before: line += " 직전:" + "/".join(f'「{t}」' for t in before)
        if after:  line += " 직후:" + "/".join(f'「{t}」' for t in after)
        vis_here = [d for (t, d) in vis_entries if ws - 3 <= t <= we + 3][:2]
        if vis_here: line += " 화면:" + " / ".join(vis_here)
        slots_desc.append(line)

    examples = f"""[좋은 예 — 이 감각 (문장을 베끼지 말고 이 작품 내용으로 쓸 것)]
S1: "세 번째 작품은 사카키바라 모에입니다."          ← 이 줄만 담백
S2: "에스원이 사무실 소재를 자주 꺼내는데, 대체로 평타는 칩니다."   ← 레이블 맥락+의견
S3: "상사한테 시달리던 직원이 단둘이 야근에 남는, 도망갈 구석 없는 설정이죠."  ← 설정+스테이크
S4: "다들 퇴근한 사무실. 아직은 서류 얘기뿐입니다."   ← 화면+지금 상태
S5: "건네는 손이 필요 이상으로 오래 머무네요."        ← 화면 사실에 해석을 얹음
S6: "웃고는 있는데 눈은 안 웃습니다. 무슨 생각일까요?" ← 심리+떡밥
S7: "그 답이 다음 한마디에서 나옵니다."               ← 떡밥 회수
S8: "여기서부터 두 사람 사이에 없던 게 하나 생깁니다." ← 전환점(명사형·어미 변주)
S{n_total}: "과연 이 관계는 어떻게 될까요? 설정은 뻔한데 표정이 다 끌고 갑니다."  ← 결말 미공개+솔직 평가
(감각 예시다. 실제 슬롯 수·시간은 아래 '슬롯:' 목록을 따른다)

[나쁜 예 1 — 화면 해설. 직전 버전이 "전혀 재미없다"고 반려된 실제 문장]
"제단 앞에 세 사람이 나란히 앉아 있습니다." / "여성은 두 손을 모읍니다."
 → 시청자가 이미 보고 있는 화면을 그대로 읽었을 뿐. 정보량 0, 어미도 전부 '~습니다'.
[나쁜 예 2 — 근거 없는 추상 훅. 그 전 버전이 "뜬금없다"고 반려된 문장]
"심상찮습니다." / "공기가 좀 묘합니다." / "웃음부터 터집니다."
 → 화면 근거 없이 분위기만 잡는 문장. 무슨 일인지 하나도 안 알려준다.
[나쁜 예 3] "B85 W57 H82 키166입니다." → 숫자 낭독 금지.
"""

    # 모음집 연속 리뷰 — seq=(i, n)이면 i번째 꼭지로서 앞 작품에서 이어지는 인트로를 쓴다.
    # 마무리 인사는 모음집 맨 끝에서 사람이 붙이므로 개별 꼭지에는 절대 넣지 않는다.
    seq_rule = ""
    if seq:
        si, sn = seq
        ordinal = ["", "첫", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉", "열",
                   "열한", "열두"]
        nth = f"{ordinal[si]} 번째" if si < len(ordinal) else f"{si}번째"
        seq_rule = (f"[연속 리뷰] 모음집 {sn}편 중 {si}번째 꼭지. "
                    f"S1은 '{nth} 작품은 {actress}입니다.' 형태로 담백하게 연다 "
                    f"(훅·질문을 붙이지 말 것). '다음 작품은' 금지. "
                    f"개별 마무리 인사는 넣지 않는다.\n")

    # ★ v3: _human_tone()을 빼는 이유 — 그 블록은 내손내싼(쇼츠) 감각을 강제한다
    #   ("결론 리액션을 먼저 던진다", "셀프 츳코미"). 순서대로 설명하는 이 구조와 정면충돌해
    #   화면과 무관한 리액션 문장을 만들어냈다(사용자: "뜬금없는 내레이션이 많다").
    tone_rule = ("[말투] 유튜브 리뷰어가 옆에서 같이 보며 말해주는 입말. 정중체(~입니다) 기본이되 "
                 "**어미를 반드시 섞어라** — ~네요/~죠/~더군요/의문형/명사형 끊기"
                 "('없던 게 하나 생깁니다', '남은 건 사진 한 장뿐.'). "
                 "★같은 어미 3연속이면 실패다(직전 반려본은 9줄 전부 '~습니다'였다).\n"
                 "AI티 상투어 금지 — '매력적인/인상적인/주목할 만한' '기대가 됩니다' "
                 "'~하는 모습을 보여줍니다'. 느낌표·과장 감탄 금지. "
                 "마무리 인사('지금까지 ~였습니다' '시청 감사' '구독') 절대 금지.\n"
                 f"[★리뷰어가 있어야 한다 — 이번 개정의 핵심] {n_total}줄 중 최소 2줄에는 "
                 "**화면에 없는 것**(인물의 속마음, 상황의 의미, 리뷰어 개인 감상·평가)이 들어가야 한다. "
                 "화면에 보이는 것만 나열하면 실패다.\n"
                 "[떡밥] 질문은 최대 1개까지 허용하되, 던졌으면 **뒤 슬롯에서 반드시 받아라**. "
                 "받을 생각이 없으면 아예 던지지 마라.\n"
                 "[근거] 소개·배우·컨셉 슬롯은 아래 [배우 팩트]/[작품 팩트]/[줄거리]에서, "
                 "장면 슬롯은 그 슬롯의 '화면:'과 '직전:/직후:' 대사에서 사실을 가져온다. "
                 "해석은 얹되 없는 사건을 지어내지는 마라.\n"
                 "[장면 순서] 장면 슬롯은 시간 순으로 이야기가 쌓이게 쓴다. "
                 "평가·별점·필모 비교는 마지막 마무리 슬롯에만.\n"
                 "[순화] 성적 묘사 금지 — 정사=액션신/플레이, 관계를 가지다=선을 넘다 로 눙친다.\n"
                 "[★소재 직접 지목 금지 — 유튜브 안전] 범죄·금기 관계를 내레이션이 그대로 못박지 마라. "
                 "감금/협박/불륜/근친/시아버지/교사와 학생 같은 말을 쓰지 말고, 그 상황이 "
                 "인물에게 어떤 처지인지로 바꿔 말한다. 대사 자막에 이미 나오는 내용이라도 "
                 "내레이션이 한 번 더 못박으면 위험이 커진다. "
                 "★단, 이 지시문과 위 예시에 나온 문구를 그대로 베껴 쓰지 마라 — 이 작품 상황에 맞는 "
                 "표현을 새로 지어라(직전 회차에서 서로 다른 두 편이 똑같은 문구를 썼다).\n"
                 "[귀로 듣는 매체] 내레이션은 음성으로 먼저 들린다. 줄임말·구어 축약으로 뭉치지 마라 "
                 "('카와이 치고는'을 '카와이치곤'으로 줄이면 들어서 알아들을 수 없다). "
                 "지시대상이 불분명한 문장('받아든 순간' — 무엇을 받아들었는지 없음)도 금지.\n"
                 "[소재 반복 금지] 한 작품 안에서 같은 소재를 세 번 이상 우려먹지 마라 — "
                 "표정·얼굴·미소 얘기만 반복하면 볼 게 없다. 행동·소품·거리·말투·상황 변화로 분산하라.\n"
                 # ★2026-08-12 ja18 실물 오류 2건 차단
                 "[★패러디는 반드시 짚어라] [작품 판정]의 '패러디:'가 '없음'이 아니면 그 작품명을 "
                 "컨셉 슬롯에서 **반드시 한 번** 언급하라. 시청자가 화면만 봐도 알아채는 것을 말하지 "
                 "않으면 맥이 빠진다(DSOD-001: 초록 트레이닝복·번호·붉은 감시자·카운트다운이 다 "
                 "나오는데 오징어게임을 끝까지 안 말해 실패했다).\n"
                 "[★사건 먼저] 장면 슬롯은 그 시각의 **주된 사건**을 먼저 말하라 — 누가 들어왔다, "
                 "무엇을 건넸다, 무엇이 드러났다. 화면 구석의 사소한 자세·소품을 주인공으로 삼지 마라.\n"
                 "[★없는 변화 금지] '더 ~해진다/굳어진다/좁혀진다'처럼 앞뒤를 비교하는 말은 두 시각의 "
                 "'화면:'에 실제로 차이가 적혀 있을 때만 쓴다. 한 시점만 보고 변화를 지어내면 시청자 "
                 "눈에는 헛소리다(ABF-375: 형이 케이크를 들고 들어오는 장면에 '팔짱이 더 단단해집니다'를 "
                 "썼다 — 주어도 없고 근거도 없었다).\n"
                 "[★주어] 사람을 가리킬 때는 누구인지 밝혀라(형·동생·비서·코치). 주어 없는 신체 "
                 "묘사는 누구 얘긴지 알 수 없어 실패다.\n")

    # 작품 판정(설정/패러디/장르) — 캡션 조각으로는 안 잡히는 '이 작품이 뭔지'다.
    # 컨셉 슬롯이 제일 먼저 보도록 팩트 블록보다 앞에 둔다.
    ov_block = ("[작품 판정 — 화면 전체를 보고 내린 결론. 컨셉 슬롯의 1순위 근거]\n"
                + "\n".join(vis_overview) + "\n\n") if vis_overview else ""

    prompt = f"""영상 리뷰 채널의 전연령 시청용 '작품 소개' 나레이션 작업이다 — 성적 묘사 없이
배우·컨셉 소개와 장면 설명만 한다. {n_total}슬롯 나레이션.
구성 순서는 ①작품 소개 → ②배우 설명 → ③작품 컨셉 → ④~ 각 장면 설명 → 마무리 다.
각 S의 '역할' 지시를 반드시 지켜라.

{examples}
{seq_rule}{tone_rule}작품: {meta_line}
{ov_block}{fact_block}[줄거리 — ★참고자료가 아니라 필수 포함 항목이다]
아래에서 '반전/하이라이트/핵심/가장 후킹/~이 포인트'로 지목한 대목은 장면 슬롯 중 하나에
**반드시** 담아라. 장면 자리가 모자라면 사소한 동작 묘사를 버리고 이것부터 넣는다.
(직전 회차에서 예정일 반전·퇴거 통보·협박 같은 핵심이 통째로 빠졌다.)
{summary}

슬롯:
{chr(10).join(slots_desc)}

출력: JSON 배열만. start/end 슬롯 시간 사용.
★슬롯 1개당 항목 정확히 1개 — 총 {n_total}개. 항목을 쪼개 개수를 늘리지 마라.
★각 항목은 **한 문장**이고, 길이는 그 슬롯에 적힌 '○자 이내'를 지킨다(슬롯마다 다르다).
 한 항목에 문장 여러 개를 몰아넣는 것은 개수를 늘리는 것과 똑같이 금지다.
 (나쁨: "어깨를 짚는 손. 왜 저렇게 여유로울까요? 혼났는데 웃는 얼굴, 무슨 생각일까요?" ← 3문장 45자
  좋음: "혼났는데 왜 웃고 있을까요?" ← 1문장 15자)
 담을 내용이 많으면 **덜 중요한 것을 버려라**. 늘려 쓰면 음성이 슬롯을 넘겨 대사를 덮는다.
[{{"start":초,"end":초,"text":"내용","style":"기본"}},...] """

    log(f"프롬프트 {len(prompt)}자 — Claude 호출 중...")

    # 프롬프트는 stdin으로 (argv로 넘기면 긴 다중행이 잘림 — call_llm과 동일 원칙)
    exe = _cli_path("claude")
    r = subprocess.run([exe, "-p", "--output-format", "text"],
                       input=prompt, timeout=600, text=True,
                       encoding="utf-8", errors="replace", capture_output=True)

    raw = (r.stdout or "").strip()
    raw = raw.replace("```json","").replace("```","").strip()
    s = raw.find("["); e = raw.rfind("]") + 1
    if not raw or s < 0 or e <= s:
        # claude가 작품 소재(배경 요약)를 이유로 거부하면 JSON 없이 사과문만 온다.
        # 같은 프롬프트를 codex는 정상 처리하므로(메인 ② 파이프라인이 codex) 폴백한다.
        # ※ call_llm은 JSON '객체'({}) 파서라 배열([]) 출력엔 못 쓴다 — 원문을 직접 받는다.
        log(f"  claude 응답에 JSON 없음(거부/빈 응답 추정) → codex 폴백: {raw[:80]}…")
        import tempfile
        exe = _cli_path("codex")
        with tempfile.TemporaryDirectory() as td:
            outf = Path(td) / "o.json"
            p = subprocess.run([exe, "exec", "--ephemeral", "--skip-git-repo-check",
                                "-c", 'model_reasoning_effort="high"', "-o", str(outf)],
                               input=prompt, timeout=900, text=True, encoding="utf-8",
                               errors="replace", capture_output=True)
            raw = outf.read_text(encoding="utf-8") if outf.exists() else ""
        if not raw.strip():
            raise RuntimeError(f"codex도 응답 없음: {(p.stderr or '')[-240:]}")
        raw = raw.replace("```json","").replace("```","").strip()
        s = raw.find("["); e = raw.rfind("]") + 1
    if s < 0 or e <= s:
        raise RuntimeError(f"JSON 파싱 실패:\n{raw[:500]}")
    try:
        new_nar = json.loads(raw[s:e])
    except json.JSONDecodeError:
        items = re.findall(r'\{[^{}]+\}', raw[s:])
        new_nar = []
        for item in items:
            try: new_nar.append(json.loads(item))
            except: pass
        if not new_nar:
            raise RuntimeError(f"JSON 파싱 실패:\n{raw[:500]}")
        log(f"  부분 파싱: {len(new_nar)}개")

    # 글자수 강제 — LLM이 '슬롯당 1개'를 지키려고 한 항목에 문장을 여러 개 몰아넣는
    # 일이 있다(실측: 22줄 중 16줄이 25자 초과, 최대 47자). 그러면 자막 한 줄이 넘치고
    # TTS가 슬롯을 넘겨 대사를 덮는다 → 문장 단위로 앞에서부터 담아 상한 안에 맞춘다.
    trimmed = 0
    for i, it in enumerate(new_nar):
        t = str(it.get("text", "")).strip()
        lim = budgets[i] if i < len(budgets) else NAR_MAX_CHARS
        if len(t) <= lim:
            it["text"] = t
            continue
        parts = re.findall(r"[^.!?]+[.!?]?", t)
        keep_txt = ""
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if keep_txt and len(keep_txt) + 1 + len(p) > lim:
                break
            keep_txt = (keep_txt + " " + p).strip() if keep_txt else p
        it["text"] = keep_txt or t[:lim]
        trimmed += 1
    if trimmed:
        log(f"  글자수 정리: {trimmed}개 항목을 슬롯별 상한 안으로 줄임")

    # Claude 타이밍 무시 — gap_windows 시간으로 강제 배분 (길이 비례)
    total_dur = sum(e - s for s, e in gap_windows)
    n_items = len(new_nar)
    # 각 창에 배분할 항목 수 (길이 비례, 최소 1)
    ratio = [(e - s) / total_dur * n_items for s, e in gap_windows]
    counts = [max(1, round(c)) for c in ratio]
    # 아웃트로는 항상 1개
    counts[-1] = 1
    # 나머지 창에서 총합 맞추기
    diff = sum(counts) - n_items
    if diff > 0:
        for _ in range(diff):
            idx = max(range(len(counts) - 1), key=lambda i: counts[i])
            if counts[idx] > 1: counts[idx] -= 1
    elif diff < 0:
        for _ in range(-diff):
            # 초당 항목 수가 가장 적은 창(아웃트로 제외)에 추가
            idx = min(range(len(counts) - 1),
                      key=lambda i: counts[i] / (gap_windows[i][1] - gap_windows[i][0]))
            counts[idx] += 1

    result = []
    item_idx = 0
    for si, (ws, we) in enumerate(gap_windows):
        cnt = counts[si]
        chunk = new_nar[item_idx : item_idx + cnt]
        if not chunk:
            item_idx += cnt; continue
        # 한 창에 여러 문장이 들어갈 때 예전엔 end==다음 start로 딱 붙여 배분해
        # 간격이 0이었다 → TTS가 쉼 없이 이어 붙어 "숨도 안 쉬는" 소리가 났다.
        # 문장 사이에 NAR_ITEM_GAP만큼 호흡을 끼워 나눈다.
        gaps = NAR_ITEM_GAP * (len(chunk) - 1)
        dur = max(0.6, (we - ws - gaps) / len(chunk))
        for j, entry in enumerate(chunk):
            st = ws + j * (dur + NAR_ITEM_GAP)
            entry["start"] = round(st, 2)
            entry["end"]   = round(st + dur, 2)
        result.extend(chunk)
        item_idx += cnt
    new_nar = result

    # plan.json 저장 (trim 좌표 보존)
    plan["narration"] = new_nar
    plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    # retime: trim좌표 → final 좌표 (갭 밖 나레이션은 keep 경계로 스냅)
    nar_tuples = [(n["start"], n["end"], n["text"], n.get("style", "기본")) for n in new_nar]
    retimed    = retime(nar_tuples, keep, snap=True)

    srt_lines = []
    for i, (s, e, text, *_) in enumerate(retimed, 1):
        srt_lines += [str(i), f"{s2srt(s)} --> {s2srt(e)}", text, ""]
    srt_path = folder / f"{code}_내레이션.srt"
    srt_path.write_text("\n".join(srt_lines), encoding="utf-8-sig")

    # json은 trim 좌표 기반으로 저장 (참고용)
    json_path = folder / f"{code}_내레이션.json"
    json_path.write_text(json.dumps(new_nar, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"완료: {srt_path}")
    log(f"\n[새 내레이션] {len(new_nar)}줄")
    for n in new_nar:
        flag = "⚠️" if len(n["text"]) > NAR_MAX_CHARS else "  "
        log(f"  {flag}[{n.get('style','기본')}] {n['text']}  ({len(n['text'])}자)")
    return new_nar
