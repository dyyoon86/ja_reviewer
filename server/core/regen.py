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
from .prompts import (prompt_manual, narration_lines,
                      NAR_SEC_PER_LINE, NAR_LINES_MIN, NAR_LINES_MAX)
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
SLOT_WANT_MAX = 6.5   # 창 길이 상한(초) — 7.5자/초 기준 약 48자
SHORT_CLIP_SEC = 100  # 이보다 짧으면 문장 수를 줄이고 본편 유도로 닫는다
INTRO_START = 0.3     # 첫 줄(소개)을 붙일 시각(초)
INTRO_MAX_START = 5.0 # 첫 줄이 이보다 늦게 시작하면 앞으로 당긴다
MAX_GAP_SEC = 15.0    # 내레이션 사이 최대 공백(초) — 넘으면 문장 수를 늘린다
                      # ★대사 위에 덮어도 되므로(2026-09-07 사용자 확정) 빈 구간을
                      #   그냥 두는 것이 손해다. 15초면 화면이 심심해지지 않는다.
# ★2026-09-07 사용자 지시: "짧으면 짧은 대로 가되, 내용 요약만 하고 본편에서 확인하라고
#   하면 된다." 실제로 6위(91초)는 소개→질문→CTA로 끝나 작품 내용이 한 줄도 없었다.
#   줄이 적을수록 요약이 더 중요하다 — 질문·떡밥으로 줄을 낭비하면 안 된다.
SHORT_CLIP_RULE = (
    "[★이 편은 짧은 클립이다 — 구성 원칙]\n"
    " · 줄 수가 적으니 **작품 내용 요약을 최우선**으로 넣어라. "
    "'이번 작품은 ~라는 내용입니다'가 반드시 있어야 한다.\n"
    " · 장면 묘사는 가장 인상적인 것 한둘만. 질문·떡밥으로 줄을 낭비하지 마라.\n"
    " · 마지막은 본편 유도로 닫는다.\n")
CONCEPT_MAX_CHARS = 46  # 컨셉·전개 슬롯만 예외 — 작품 개괄은 30자에 안 담긴다
                        # (TTS 7.5자/초 기준 46자 ≈ 6.1초. 창이 그만큼 있어야 읽힌다)


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


def _final_to_trim(off, keep_segs):
    """최종 영상 기준 초 → 클린본(trim) 기준 초. retime의 역함수.

    ★경계는 **다음 구간의 머리**로 보낸다. `off <= acc + d`로 잡으면 이음매에 정확히
    걸린 값이 앞 구간의 끝으로 떨어져, 그 뒤 keep 경계 클램프와 만나면 길이 0짜리
    창이 되어 슬롯이 통째로 사라진다(START-627 7번째 슬롯 = 영상 마지막 20초 공백).
    """
    if not keep_segs:
        return 0.0
    acc = 0.0
    last = len(keep_segs) - 1
    for i, (a, b) in enumerate(keep_segs):
        d = b - a
        if off < acc + d or i == last:
            return a + max(0.0, min(off - acc, d))
        acc += d
    return keep_segs[last][1]


def narration_slots(video_sec, lo=NAR_LINES_MIN, hi=NAR_LINES_MAX, per=NAR_SEC_PER_LINE):
    """영상 길이 → 내레이션 슬롯 수.

    ★밀도 규칙은 prompts.narration_lines 한 곳에만 있다 — 예전엔 여기(15초당 1줄)와
    프롬프트 예산(narration_budget, 5.4초당 1문장)이 3배 어긋나 있었다. 이 함수는
    기존 호출부(GUI·tools) 호환을 위한 얇은 위임이다."""
    return narration_lines(video_sec, lo, hi, per)


def _rank_facts(folder: Path, code: str, rank: dict, plan: dict, log=print):
    """순위·인기·대사량을 '판단 전용' 팩트로 만든다 — **내레이션에 옮기면 안 되는** 정보다.

    왜 필요한가(2026-09-07 실측): 섹션②는 클린본만 보고 stars를 매긴다. START-621은
    원본 전사가 651줄인데 클린 후 77줄, 최종 컷 38줄만 남아 **대사의 94%가 편집으로
    사라졌다.** 모델은 그걸 모르고 '대사가 적은 작품'으로 읽어 stars=3을 줬고,
    섹션③은 그 3점만 보고 "밋밋한 작품"이라고 총평을 썼다. 실제로는 2주간 신작 중
    3위(👍36/👎4, 호감 90%)인 작품이다.

    → 근거를 주되 **말하지 말라고** 못박는다. 팩트는 틀린 말을 막는 용도지
      낭독용이 아니다(편집 사정·조회수는 시청자가 알 바 아니다).
    """
    lines = []
    if rank.get("rank") and rank.get("total"):
        lines.append(f"인기 순위: 이번 모음집 {rank['total']}편 중 **{rank['rank']}위**")
    lk, dk = rank.get("likes"), rank.get("dislikes")
    if lk is not None and dk is not None and (lk + dk) > 0:
        lines.append(f"호감도: 👍{lk} / 👎{dk} (호감 {lk / (lk + dk) * 100:.0f}%)"
                     + (f", 조회 {rank['views']:,}" if rank.get("views") else ""))
    # 대사량 3단계 — 파일에서 직접 센다
    try:
        oj = folder / f"{code}_원본전사.json"
        orig = len(json.loads(oj.read_text(encoding="utf-8"))) if oj.is_file() else 0
        cj = folder / f"{code}_전사.json"
        clean = len(json.loads(cj.read_text(encoding="utf-8"))) if cj.is_file() else 0
        final = len(plan.get("dialogue") or [])
        if orig and final:
            lines.append(
                f"대사량: 원본 {orig}줄 → 노출 제거 후 {clean}줄 → 최종 컷 {final}줄"
                f"({final / orig * 100:.0f}%)")
            lines.append(
                "  ※ 최종본에 대사가 적어 보이는 것은 **편집의 결과**다. "
                "'대사가 적은 작품', '설정이 얇다', '밋밋하다'처럼 **작품 탓으로 평가하지 마라.**")
    except Exception:
        pass
    if not lines:
        return ""
    log(f"  판단용 팩트 {len(lines)}줄 (순위·호감도·대사량)")
    return ("[판단 전용 정보 — ★내레이션에 옮겨 말하지 말 것]\n"
            "아래는 네가 잘못된 평가를 하지 않도록 주는 배경이다. 조회수·호감도·대사량·"
            "편집 사정은 **시청자가 알 바 아니므로 문장으로 만들지 마라**. "
            "순위만은 예외로, 소개 슬롯에서 말해도 된다.\n"
            + "\n".join(f" · {x}" for x in lines) + "\n")


def regen_narration(folder: Path, meta_api: str, log=print, seq=None, slots=None,
                    style="3min", rank=None, loose=False):
    """내레이션만 6슬롯(인트로 2 + 갭 3 + 아웃트로 1) 규칙으로 재생성.
    메타(배우/신체/레이블) 반영 + keep 갭 창에 길이 비례 배분 + retime(snap)으로
    {code}_내레이션.srt/.json 을 갱신한다. 반환: 새 narration 리스트.

    style: '3min'(기본, 딸감별사 정중체) | 'gootabari'(구타바리형 반말 병맛 초압축).
      구타바리형은 예시·말투 규칙만 갈아끼운다 — 슬롯 구조(소개→배우→컨셉→장면→마무리)와
      배치 로직은 그대로다. 괄호 드립자막은 여기서 만들지 않는다(슬롯 = TTS가 읽을 자리라
      화면 전용 드립을 끼우면 슬롯이 낭비된다). 드립은 ② AI 처리에서 나온다."""
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
        # ★2026-09-07 — 짧은 클립은 문장 수를 줄인다. 100초에 7문장이면 창이 2~3초씩밖에
        #   안 나와서 작품 소개 한 문장이 안 들어간다(SNOS-401 108초: 37자를 2.5초에).
        #   문장을 줄이면 창이 그만큼 길어져 제대로 된 문장을 쓸 수 있다.
        # ★공백 상한 — 줄 수가 적으면 구획이 넓어져 중간이 텅 빈다(DLDSS-531: 91초에
        #   3줄 → 36.7초 공백). 구획이 MAX_GAP_SEC 을 넘지 않도록 줄 수의 하한을 잡는다.
        # ★순서 주의 — 짧은 클립 축소를 **먼저** 하고 공백 방지를 나중에 건다.
        #   반대로 하면 공백 때문에 늘린 문장 수를 축소가 도로 깎아 공백이 남는다.
        if vsec and vsec < SHORT_CLIP_SEC:
            slim = max(4, int(SLOT_TARGET * 0.7))
            if slim < SLOT_TARGET:
                log(f"  짧은 클립({vsec:.0f}s) — 문장 수 {SLOT_TARGET}→{slim}개로 줄여 "
                    f"각 문장에 시간을 더 준다")
                SLOT_TARGET = slim
        need = int(vsec / MAX_GAP_SEC) + 1 if vsec else 0
        if need > SLOT_TARGET:
            log(f"  공백 방지: 문장 수 {SLOT_TARGET}→{need}개 "
                f"(공백이 {MAX_GAP_SEC:g}초를 넘지 않도록)")
            SLOT_TARGET = need
        log(f"  내레이션 슬롯 목표 {SLOT_TARGET}개 (영상 {vsec:.0f}s 기준)")

    # ── 메타 정보 ──────────────────────────────────────────────────────────
    log("메타 조회 중...")
    try:
        meta = fetch_meta(meta_api, code, log=log)
    except Exception as e:
        log(f"  메타 조회 실패 ({e}), 코드명만 사용")
        meta = {}
    actress = meta.get("actress") or code
    # ★배우가 여럿이면 이름을 다 부르지 않는다. DSOD-001(5인)에서 소개 줄이
    #   "두 번째 작품은 미소노 와카, 나카마루 미쿠루, 유라 카나, 마츠마루 카스미,
    #   모미지 히라기입니다"로 55자가 되어(다른 줄은 17~31자) 자막이 화면 밖으로
    #   넘쳤다. 3명 이상이면 대표 1명 + "외 N명"으로 줄인다(2인은 그대로 부른다).
    _names = [x.strip() for x in re.split(r"[,、]", actress) if x.strip()]
    if len(_names) >= 3:
        actress = f"{_names[0]} 외 {len(_names) - 1}명"
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

    rank = rank or {}
    fact_block = _fact_lines() + "\n" + _rank_facts(folder, code, rank, plan, log)

    # ── 카운트다운 연출 ──────────────────────────────────────────────────────
    # 역순(꼴찌→1위) 재생일 때 순위가 올라갈수록 온도를 올리고, 각 편 마무리에
    # 다음 편을 당기는 브릿지를 붙인다. 편마다 독립 호출이지만 rank/total을 알면
    # 자기 위치를 알기 때문에 가능하다.
    rk, tot = rank.get("rank"), rank.get("total")
    intro_rule = tier_rule = bridge_rule = ""
    # ★2026-09-07 — 짧은 클립 CTA를 1위 아닌 편에만 걸어놔서 1위(0.9분)가 본편 유도 없이
    #   "안 보인다는 말은 거짓말."로 뚝 끊겼다. 순위와 무관하게 길이로만 판단한다.
    _fin = folder / f"{code}_final.mp4"
    _vsec = video_duration(str(_fin)) if _fin.is_file() else 0
    is_short = bool(_vsec and _vsec < SHORT_CLIP_SEC)
    short_cta = ("" if not is_short else
                 " · ★이 편은 보여준 분량이 짧다. 총평으로 단정하지 말고 "
                 "**본편에서 확인해 보라는 쪽**으로 닫아라 — "
                 "'나머지는 본편에서 보셔야 합니다.' 식. 상투적이지 않게 "
                 "이 작품 소재를 걸어서 말할 것.\n")
    if rk and tot:
        if rk == 1:
            intro_rule = (f"[★소개 문구] 이 편이 **대망의 1위**다. "
                          f"'대망의 1위, {actress}입니다.' 형태로 연다.\n")
            tier_rule = ("[온도] 모음집의 정점이다. 이번 편만은 확신을 갖고 강하게 밀어라. "
                         "유보적 표현('~하기에는 아쉬우나')을 쓰지 마라.\n")
            bridge_rule = ("[★마무리 = 총평] 모음집의 마지막 편이다. "
                           "이 작품을 한 줄로 매듭짓되, 1위다운 확신을 담아 닫는다.\n"
                           + short_cta +
                           " · 다음 편 예고·남은 편수 언급 금지.\n"
                           " · '1위'를 반복하지 마라 — 소개 줄에서 이미 말했다.\n"
                           " · 뜻 없는 추상 총평 금지 (나쁨: '편안한 공기 하나로 1위를 가져갔죠').\n")
        else:
            intro_rule = (f"[★소개 문구] '{rk}위, {actress}입니다.' 형태로 연다"
                          f"('{rk}번째 작품은' 같은 서수 표현을 쓰지 말 것).\n")
            ratio = rk / tot
            if ratio > 0.66:
                tier_rule = ("[온도] 아직 하위권이다. 담백하게 소개하고 아쉬운 점도 솔직히 짚는다.\n")
            elif ratio > 0.33:
                tier_rule = ("[온도] 중반부다. 조금 힘을 실어 말하되 과열되지는 않는다.\n")
            else:
                tier_rule = (f"[온도] 상위권({rk}위)이다. 확실히 좋다는 톤으로 밀어라. "
                             f"'여기서부터는 급이 다릅니다' 같은 상승 신호를 써도 좋다.\n")
            # ★2026-09-07 — 카운트다운 브릿지("이제 11편 남았습니다")를 뺀다.
            #   숫자만 세는 진행 안내라 긴장감이 아니라 지루함을 만들고, 12편 내내
            #   반복되며 모음집 구조를 노출해 몰입을 깬다(사용자 지적).
            #   카운트다운 긴장감은 소개 문구('12위 / 3위 / 대망의 1위')가 이미 만든다.
            #   → 마무리는 이 작품을 닫는 **총평 한 줄**로 돌아간다.
            # ★2026-09-07 — 클린본이 짧으면 보여준 게 얼마 안 되므로 총평보다
            #   '본편에서 확인하라'는 유도가 자연스럽다(사용자 제안).
            bridge_rule = (
                "[★마무리 = 총평] 이 작품을 한 줄로 매듭짓는다. "
                "'~작품이었습니다' 같은 리뷰어의 마무리 어투로 닫아라.\n" + short_cta +
                " · 남은 편수를 세거나 다음 편을 예고하지 마라.\n"
                " · 순위를 다시 말하지 마라 — 소개 줄에서 이미 말했다.\n"
                " · 영상을 못 봤으므로 연출 완성도를 단정하지 말고, "
                "본 내용과 배우·레이블 맥락 안에서 평가한다.\n")
    # ★2026-09-07 — [줄거리]에 plan['summary']를 넣고 있었는데, 그건 줄거리가 아니라
    #   섹션② 가 남긴 **컷 선정 메모**다("노골 구간은 제외하고 100초로 압축했습니다").
    #   작품 내용이 한 줄도 없으니 컨셉 슬롯이 쓸 재료가 없었다 — 사용자: "작품설명은 어딨어?"
    #   섹션① 이 만들어 둔 {code}_줄거리.txt(원본 전체 요약·등장인물·전환점)를 1순위로 쓰고,
    #   summary 는 보조로 뒤에 붙인다.
    cut_memo = plan.get("summary", "")
    story_f = folder / f"{code}_줄거리.txt"
    story_txt = ""
    if story_f.is_file():
        try:
            story_txt = story_f.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    if story_txt:
        log(f"  줄거리 파일 반영: {story_f.name} ({len(story_txt)}자)")
        summary = story_txt + (f"\n\n[컷 선정 메모 — 섹션② 기록]\n{cut_memo}" if cut_memo else "")
    else:
        log("  ※ _줄거리.txt 없음 — 컷 선정 메모만으로 진행(작품 설명이 얇아질 수 있음)")
        summary = cut_memo

    # ── 슬롯별 어미 매핑 및 설명 ────────────────────────────────────────
    # 슬롯 순서: 인트로(1) → 갭0(2) → 갭1(3) → 갭2+(4,5) → 아웃트로(6)
    # 아웃트로 스타일 로테이션 — 모음집에서 11편이 전부 "어떻게 될까요?"로 끝나면 지루하다.
    # seq(몇 번째 꼭지)에 따라 순환해 연속 작품이 같은 끝맺음을 쓰지 않게 한다.
    # ★마무리는 '총평'으로 닫는다 (2026-09-07 사용자 지시).
    #   예전엔 질문형·명사형·여운형 같은 **어미 변주**를 돌렸는데, 3분휴지 문체의 정석은
    #   작품을 저울질해 한 줄로 매듭짓는 총평이다. 실측(START-621)에서 어미 변주가
    #   "밋밋한 연출을 소라 혼자 끌고 갑니다."로 나와 말투가 어색했고, 사용자는
    #   "명작까지는 아니어도 별점 셋 작품이었습니다." 쪽을 원했다.
    #   → 6종 전부 총평으로 통일하되 **평가 각도**를 돌려 12편이 겹치지 않게 한다.
    #   채널 아웃트로('지금까지 ~였습니다')는 여전히 금지 — 모음집 맨 끝에서 사람이 붙인다.
    OUTRO_STYLES = [
        "총평=명작 저울질 — '명작까지는 아니어도 충분히 볼만한 작품이었습니다.' 식",
        "총평=기대 대비 — '기대만큼은 아니었지만 무난한 작품이었습니다.' 식",
        "총평=취향 조건부 — '취향만 맞으면 꽤 만족스러운 작품이었습니다.' 식",
        "총평=아쉬움 명시 — '아쉬운 점은 있지만 볼만했던 작품이었습니다.' 식",
        "총평=배우 중심 — '작품보다 배우가 남는 작품이었습니다.' 식",
        "총평=추천 여부 — '가볍게 보기에는 나쁘지 않은 작품이었습니다.' 식",
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
    if loose:
        # 느슨 모드 — 슬롯 '역할'만 알려주고 세부 규칙은 뺀다. 순위 소개와 브릿지는
        # 카운트다운 포맷 자체라 유지한다.
        ROLE_DESC = {
            "소개":   intro_rule or f"역할=작품 소개. 배우 이름을 밝히며 담백하게 연다.",
            "소개배우": intro_rule or f"역할=작품 소개 + 배우 한마디.",
            "소개컨셉": (intro_rule or "역할=작품 소개") + " + 이 작품이 어떤 내용인지 한 문장.",
            "배우":   "역할=배우 이야기. [배우 팩트]를 근거로 이 배우를 소개한다.",
            "컨셉":   "역할=작품 소개. '이번 작품은 ~라는 내용입니다' 형태로 무슨 작품인지 알려준다.",
            "전개":   "역할=이야기가 어디로 흘러가는지 한 문장. [줄거리] 근거. 결말은 빼고.",
            "장면":   "역할=이 시각의 장면. '화면:'과 '직전:/직후:' 대사를 근거로 자유롭게 쓴다.",
            "마무리": bridge_rule or "역할=마무리 한 줄.",
        }
    else:
        ROLE_DESC = {
           "소개":   (intro_rule or "역할=작품 소개. '○ 번째 작품은 {a}입니다.' 형태로 연다. "
                                   "이 줄만은 담백하게.").format(a=actress),
           "배우":   ("역할=배우 맥락 + 기대치. " + actor_angle + ". [배우 팩트]를 근거로 하되 "
                     "**정보를 얹어라**. 숫자 낭독('B85 W57 H82') 금지. 리뷰어 의견 1스푼 허용. "
                     "★지정된 각도를 지켜라 — 매 편 레이블 소개로만 때우면 모음집에서 같은 말이 반복된다."),
           # ★2026-08-12 — 사용자: "작품 설명이 하나도 안 들어갔다. 장면 설명도 좋지만
           #   일단 작품 개괄은 해줘야지." 실제로 DSOD-001은 컨셉 슬롯이 바로 장면으로 새서
           #   ("이삿날 들이닥친 남자들이 게임을 걸고") 이게 무슨 작품인지 끝까지 안 나왔다.
           #   컨셉 슬롯을 "작품 개괄"로 못박고, 담아야 할 것을 항목으로 지정한다.
           # ★2026-09-07 사용자 지시: "'이번 작품은 어떤어떤 내용입니다'가 명확히 있으면 좋겠다."
           #   이전엔 "'~라는 작품입니다'로 밋밋하게 끝내지 말 것"이라고 **금지**해 놓아서
           #   모델이 그 형식을 피해 장면 묘사로 새어 나갔다. 3분휴지 채널의 기본 공식이기도
           #   하므로(①작품 소개 한 문장 '~라는 내용의 작품입니다') 오히려 강제한다.
           "컨셉":   ("역할=**작품 개괄**. 이 슬롯은 장면 중계가 아니라 '이 작품이 어떤 작품인가'를 "
                     "시청자에게 알려주는 자리다.\n"
                     "   ★반드시 **'이번 작품은 ~라는 내용입니다'** 또는 "
                     "**'~라는 내용의 작품입니다'** 형태로 끝맺어라. 이 형식을 피하지 마라.\n"
                     "   한 문장에 담을 것 — ①어디서 누가(무대·인물 관계) ②무슨 상황인지(설정) "
                     "③그게 왜 곤란하거나 볼만한지. [줄거리]와 [작품 판정]이 1순위 근거다. "
                     "[작품 판정]의 '패러디:'가 '없음'이 아니면 여기서 그 작품명을 밝혀라.\n"
                     "   ★특정 시각의 장면 하나를 설명하는 것은 실패다 — 작품 전체를 요약해야 한다. "
                     "구체적인 소재(무대·직업·관계·장치)를 반드시 넣어라 — "
                     "'확인할 게 있다고 합니다' 같은 두루뭉술한 표현은 실패다.\n"
                     # ★2026-09-07 — 글자수를 46자로 늘리고 '①②③을 한 문장에'를 요구했더니
                     #   자리를 채우려고 절을 겹쳐 붙여 문장이 어색해졌다
                     #   ('건강검진으로 바뀌어 숫자로 재는 내용입니다' — 사용자 지적).
                     #   정보량보다 읽히는 문장이 우선이다. 못 담은 것은 다음 '전개' 슬롯이 받는다.
                     "   ★★단, **자연스럽게 읽히는 것이 정보량보다 우선이다.** 절을 두 개 이상 "
                     "겹쳐 붙이지 마라. 주어진 글자수를 다 채울 필요 없다 — 짧고 깔끔한 한 문장이 "
                     "길고 빽빽한 문장보다 낫다.\n"
                     "     (나쁨: '인터뷰가 갑자기 건강검진으로 바뀌어 숫자로 재는 내용입니다' — 절이 겹침)\n"
                     "     (좋음: '인터뷰가 갑자기 건강검진으로 바뀌는 내용입니다')\n"
                     "   담지 못한 세부는 다음 '전개' 슬롯에서 이어 말하면 된다."),
           # ★2026-09-07 — 사용자: "줄거리도 전사도 다 뽑아놨는데 상황 설명하고 땡이다."
           #   컨셉이 '설정'을 말하면 그 다음엔 '이야기가 어디로 가는지'가 있어야 리뷰가 된다.
           #   장면 슬롯은 각자 제 시각만 보므로 아무도 전체 흐름을 말하지 않았다.
           "전개":   ("역할=**이야기 요약**. 이 작품이 어디로 흘러가는지 한 문장으로 말한다. "
                     "[줄거리]의 '이야기가 꺾이는 지점'을 근거로, 처음 상황이 무엇 때문에 "
                     "어떻게 달라지는지를 담아라. 특정 시각의 장면 묘사가 아니다. "
                     "★결말은 밝히지 말 것 — 어디까지 가는지가 아니라 '무엇 때문에 달라지는지'다."),
           "장면":   ("역할=장면 중계 **+ 한 겹**. '화면:'과 '직전:/직후:' 대사에서 사실을 가져오되, "
                     "거기에 인물의 속마음·상황의 의미·관계 변화 중 하나를 반드시 얹는다. "
                     "★화면에 보이는 것만 그대로 옮겨 적는 것은 실패다 — 시청자는 이미 그 화면을 보고 있다. "
                     "('손을 모읍니다' ✗ / '좀처럼 손이 내려오지 않네요' ○). "
                     "화면에 없는 사건을 지어내지는 말 것 — 얹는 것은 해석이지 사실이 아니다."),
           # 평가 각도도 로테이션 — v6에서 10편 중 5편이 "설정은 뻔한데/흔한데/익숙해도"로 끝났다.
           # 어미(outro_rule)만 돌리고 평가 '내용'은 안 돌려서 같은 말이 반복됐다.
           # ★2026-09-07 — 마무리에서 '완성도 평가'를 뺀다. 영상을 못 본 상태에서 stars 하나로
           #   "밋밋하다"를 만들어냈고(START-621: 실제 3위 작품), 그건 근거 없는 단정이다.
           #   카운트다운이면 브릿지로, 아니면 관찰된 내용 요약으로 닫는다.
           "마무리": (bridge_rule or (outro_rule + " 평가는 " + eval_angle
                                    + " 관점에서 한 조각만 곁들인다."))
                     + " 결말은 절대 밝히지 말 것. ★'설정은 뻔한데'류 상투구 금지.",
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
        # ★2026-09-07 — 8줄 이상이면 '전개'(이야기 요약)를 한 줄 넣는다.
        #   컨셉이 설정을 말하고 장면들이 순간을 중계하는데, 그 사이를 잇는
        #   "그래서 이야기가 어떻게 흘러가는가"가 아무 슬롯에도 없었다.
        if n >= 8:
            return ["소개", "배우", "컨셉", "전개"] + ["장면"] * (n - 5) + ["마무리"]
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

    def windows_by_scene(keep_segs, n_slots, min_len=NAR_SLOT_MIN):
        """장면(keep 구간)에 맞춰 배치 — 시간 등분이 아니라 **내용 단위**로 나눈다.

        ★2026-09-07 사용자 지시: "순수 등분이 아니라 내용에 맞게 채워 넣어야지."
          최종 영상은 keep 구간을 이어 붙인 것이라 **구간 하나 = 장면 하나**다.
          시간만 등분하면 한 장면에 두 줄이 몰리고 옆 장면은 비는 일이 생긴다.
          → 구간 길이에 비례해 줄을 배분하고, 각 구간 안에서 고르게 놓는다.
            그러면 각 줄이 자기가 설명할 장면 위에 앉는다.
          (대사와 겹치는지는 보지 않는다 — 원음은 더킹되므로 덮어도 된다.)
        """
        total = sum(b - a for a, b in keep_segs)
        if total <= 0 or n_slots < 1:
            return None
        # 최종 좌표 기준 구간 목록
        segs, acc = [], 0.0
        for a, b in keep_segs:
            segs.append((acc, acc + (b - a)))
            acc += b - a
        # 길이 비례 배분(각 구간 최소 1줄, 짧은 구간은 0줄일 수 있다)
        quota = []
        for s0, s1 in segs:
            quota.append(max(0, round((s1 - s0) / total * n_slots)))
        while sum(quota) < n_slots:                    # 모자라면 긴 구간부터
            i = max(range(len(segs)), key=lambda k: (segs[k][1] - segs[k][0]) / (quota[k] + 1))
            quota[i] += 1
        while sum(quota) > n_slots:                    # 넘치면 짧은 구간부터
            cand = [k for k in range(len(segs)) if quota[k] > 0]
            if not cand:
                break
            i = min(cand, key=lambda k: (segs[k][1] - segs[k][0]) / quota[k])
            quota[i] -= 1
        raw = []
        for (s0, s1), q in zip(segs, quota):
            if q <= 0:
                continue
            seglen = s1 - s0
            want = max(min_len, min(SLOT_WANT_MAX, seglen / q * 0.55))
            step = seglen / q
            for j in range(q):
                st = s0 + j * step + (step - want) / 2
                raw.append((max(s0, st), min(s1, st + want)))
        raw.sort()
        return _finish_windows(raw, keep_segs, total, min_len)

    def windows_even(keep_segs, dlg, n_slots, min_len=NAR_SLOT_MIN):
        """최종 영상 시간축을 n_slots 등분해 **구획마다 한 자리씩** — 전체에 고르게 깔린다.

        구획 안에 '대사 없는 틈'이 있으면 그 틈을 쓰고(대사와 안 겹침), 없으면 구획
        안에 그냥 놓는다. 3min(딸감별사) 문체는 프롬프트 설계상 "내레이션이 영상을
        거의 다 덮는" 문체이고 원음은 mux에서 더킹되므로 대사 위에 얹혀도 된다.
        대사를 피하는 것만 우선하면 대사가 빽빽한 하이라이트에서 자리가 말라
        내레이션이 한쪽으로 뭉친다 — ja20 START-627 실측: 대사가 영상의 67%를 덮어
        쓸 틈이 3개뿐 → 98초 중 0~6초에 2줄, 45초 공백, 마지막 11초에 4줄.
        cinema 문체는 대사 원음을 들려주는 것이 핵심이라 이 함수를 쓰지 않는다.
        """
        total = sum(b - a for a, b in keep_segs)
        if total <= 0 or n_slots < 1:
            return None
        # 대사 없는 틈을 최종 좌표로 환산해 둔다(구획 안에서 우선 후보로 쓴다)
        freef, acc = [], 0.0
        for ka, kb in keep_segs:
            for fa, fb in free_intervals(keep_segs, dlg):
                a, b = max(fa, ka), min(fb, kb)
                if b - a > 0.05:
                    freef.append((acc + (a - ka), acc + (b - ka)))
            acc += kb - ka
        span = total / n_slots
        # ★2026-09-07 — 예전엔 want = min(min_len, span*0.8) 라 **모든 창이 정확히 2.5초**로
        #   고정됐다(구획이 15초여도 2.5초만 씀). 2.5초 = 약 18자라 작품 소개 한 문장이
        #   안 들어가고, 넘기면 TTS 가 다음 문장을 밀어낸다(SNOS-401: 37자/2.5초).
        #   → 구획이 허락하는 만큼 창을 늘린다. 상한 SLOT_WANT_MAX(6.5초 ≈ 48자).
        want = max(min_len, min(SLOT_WANT_MAX, span * 0.55))
        raw = []
        for k in range(n_slots):
            lo, hi = k * span, (k + 1) * span
            # ★2026-09-07 사용자 확정(재확인): "내레이션이 대사를 덮어도 전혀 상관없다."
            #   → 3min·gootabari 는 대사 위치를 **아예 보지 않는다**. 구획을 등분해
            #     그 중앙에 놓을 뿐이다. 원음은 mux 에서 더킹되므로 겹쳐도 들린다.
            #   예전엔 '대사 없는 틈'을 조건(→슬롯 소실, 91초에 3줄)으로, 그 다음엔
            #   선호(→배치가 대사에 끌려다님)로 뒀는데 둘 다 필요 없는 제약이었다.
            #   cinema 문체만 대사 원음을 살려야 해서 windows_from_free 를 쓴다.
            mid = (lo + hi) / 2.0
            raw.append((max(lo, mid - want / 2), min(hi, mid + want / 2)))
        # ★2026-09-07 — 첫 줄은 '12위, ○○입니다' 같은 소개라 영상 시작에 붙어야 한다.
        #   첫 구획의 '대사 없는 틈'을 찾다 보니 시작부에 대사가 깔린 편은 10~15초까지
        #   밀렸다(SNOS-401 14.8s, DLDSS-531 10.6s). 도입은 대사와 겹쳐도 되므로
        #   앞쪽으로 당긴다(3min 은 원음 더킹이라 문제없다).
        return _finish_windows(raw, keep_segs, total, min_len)

    def _finish_windows(raw, keep_segs, total, want):
        """공통 마무리 — 도입 당기기 · 겹침 정리 · 최종→trim 좌표 환산 · keep 경계 가둠."""
        if raw and raw[0][0] > INTRO_MAX_START:
            w = raw[0][1] - raw[0][0]
            raw[0] = (INTRO_START, INTRO_START + w)
        out, prev = [], -1e9
        for a, b in raw:
            a = max(a, prev + NAR_ITEM_GAP, 0.0)
            b = min(max(b, a + 0.6), total)
            if a >= total:
                break
            prev = b
            ts, te = _final_to_trim(a, keep_segs), _final_to_trim(b, keep_segs)
            # 창이 keep 경계를 넘으면 뒤쪽을 잘라 한 구간 안에 가둔다
            # (넘어가면 retime에서 시간이 튀어 문장이 엉뚱한 장면에 붙는다)
            edge = next((kb for ka, kb in keep_segs if ka <= ts <= kb), None)
            if edge is not None:
                te = min(te, edge)
                if te - ts < 0.4:      # 구간 꼬리에 걸렸으면 앞으로 늘려 살린다
                    te = min(edge, ts + want)
            if te - ts < 0.4:
                # 창이 keep 경계에 걸려 사라지면 슬롯이 통째로 없어진다 — 앞으로 당겨 살린다
                ts = max(ts - want, next((ka for ka, kb in keep_segs if ka <= ts <= kb), ts))
                te = min(ts + want, edge if edge is not None else ts + want)
            if te - ts < 0.4:
                continue
            out.append((round(ts, 2), round(te, 2)))
        return out if len(out) >= 3 else None

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
        # 3min·gootabari는 내레이션이 주 오디오다(원음 더킹) → 영상 전체에 고르게 깐다.
        # cinema만 대사 원음을 살려야 하므로 '대사 없는 틈'에만 놓는다.
        if style != "cinema":
            w = windows_by_scene(keep_segs, n_slots)      # 장면(keep 구간) 기준
            if w:
                return w
            w = windows_even(keep_segs, dialogue, n_slots)  # 폴백: 시간 등분
            if w:
                log("  ※ 장면 배치 실패 — 시간 등분으로 후퇴")
                return w
            log("  ※ 균등 배치 실패 — 대사-회피 배치로 후퇴")
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
        why = ("대사 원음을 살려야 해 대사 없는 틈에만 놓는다" if style == "cinema"
               else "컷이 짧거나 이음매에 걸려 자리가 줄었다")
        log(f"  확보된 자리 {MAX_SLOTS}개 / 목표 {SLOT_TARGET}개 — {why}")
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
        # ★2026-09-07 — 컨셉/전개 슬롯만 상한을 따로 올린다.
        #   "①어디서 누가 ②무슨 상황 ③왜 볼만한지를 한 문장에"라는 지시를 주면서
        #   글자수는 30자(NAR_MAX_CHARS)로 묶어 놨었다. 30자에 그걸 담는 건 불가능해서
        #   모델이 장면 한 줄로 도망쳤고, 결국 "이게 무슨 작품인지"가 끝까지 안 나왔다
        #   (MIDA-798 12위 실측: 컨셉 슬롯이 '인터뷰가 확인 절차로 바뀌네요' 17자).
        #   TTS 7.5자/초 기준 45자 = 6초라 슬롯 창만 확보되면 충분히 읽힌다.
        if ek in ("소개", "소개배우", "소개컨셉"):
            lo = min(NAR_MAX_CHARS, len(actress) + 14)
        elif ek in ("컨셉", "전개"):
            lo = 34
        elif ek == "배우":
            lo = 20
        else:
            lo = 12
        cap = CONCEPT_MAX_CHARS if ek in ("컨셉", "전개") else NAR_MAX_CHARS
        budgets.append(_char_budget(we - ws, lo=lo, hi=cap))
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
        # ★순위 카운트다운이면 서수 대신 순위로 연다 — intro_rule이 이미 문구를 정했는데
        #   여기서 서수를 또 지시하면 충돌한다(1위가 '열두 번째 작품은'으로 나갔다).
        if intro_rule:
            seq_rule = (f"[연속 리뷰] 모음집 {sn}편 중 {si}번째 꼭지다. "
                        f"S1 문구는 아래 [★소개 문구] 지시를 따른다 — "
                        f"**서수('{nth} 작품은')를 쓰지 마라.** '다음 작품은' 금지. "
                        f"개별 마무리 인사는 넣지 않는다.\n")
        else:
            seq_rule = (f"[연속 리뷰] 모음집 {sn}편 중 {si}번째 꼭지. "
                        f"S1은 '{nth} 작품은 {actress}입니다.' 형태로 담백하게 연다 "
                        f"(훅·질문을 붙이지 말 것). '다음 작품은' 금지. "
                        f"개별 마무리 인사는 넣지 않는다.\n")

    # ★ v3: _human_tone()을 빼는 이유 — 그 블록은 내손내싼(쇼츠) 감각을 강제한다
    #   ("결론 리액션을 먼저 던진다", "셀프 츳코미"). 순서대로 설명하는 이 구조와 정면충돌해
    #   화면과 무관한 리액션 문장을 만들어냈다(사용자: "뜬금없는 내레이션이 많다").
    #   ★단 style=naeson 은 그 감각이 **목적**이므로 아래에서 따로 갈아끼운다.
    if loose:
        # ★2026-09-07 사용자 지시: "너무 프롬프트를 조이지 말고 AI가 알아서 하도록 해봐.
        #   19금 단어만 순화하도록." — 규칙이 20개까지 불어나면서 문장이 규칙을 만족시키는
        #   쪽으로만 쓰이고 생기가 죽었다. 여기서는 **안전·물리 제약만** 남기고 전부 뺀다.
        #   (남기는 것: 순화 / 채널 아웃트로 금지 / 결말 스포 금지 / 지어내기 금지)
        tone_rule = (
            "[말투] 유튜브 리뷰어가 옆에서 같이 보며 말해 주는 입말. 정중체(~입니다) 기본. "
            "나머지 표현은 네 판단에 맡긴다 — 자연스럽고 재미있게 써라.\n"
            "[★순화 — 이것만은 반드시] 성적 묘사·신체 부위·행위를 직접 쓰지 마라. "
            "정사=액션신/플레이, 관계를 가지다=선을 넘다 로 눙친다. "
            "약물류는 '몽롱해지는 것'으로. 감금·협박·불륜·근친 같은 금기 관계는 "
            "그대로 못박지 말고 인물의 처지로 바꿔 말한다.\n"
            "[금지] 채널 인사·구독 요청('지금까지 ~였습니다', '시청 감사') 금지 "
            "— 모음집 맨 끝에서 사람이 따로 붙인다. 결말은 밝히지 마라. "
            "화면에 없는 사건을 지어내지 마라.\n")
        # ★2026-09-07 — 규칙만 빼고 '좋은 문장이 뭔지'를 안 알려주니 설명문만 나왔다
        #   ("부끄럼이 다 살린 작품이었습니다" — 사용자: "재미없고 식상하다").
        #   _human_tone()은 규칙이 아니라 **실제 인기 채널 문장 예시**다. 재료는 줘야 한다.
        from .prompts import _human_tone
        tone_rule += _human_tone()
        tone_rule += (
            "[★후킹 — 리뷰는 설명문이 아니다]\n"
            " · 매 줄이 '무엇이 보인다'로만 끝나면 볼 이유가 없다. "
            "리뷰어의 반응·판단·농담이 최소 2~3줄에는 들어가야 한다.\n"
            " · 마지막 줄은 밋밋한 총평이 아니라 **한 방**이어야 한다 — "
            "역설 추천, 조건부 강권, 드라이한 농담, 되받아치는 한마디 중 하나.\n"
            # ★2026-09-07 — '보는 내가 더 민망한 작품입니다'가 나왔다. 틀린 말은 아닌데
            #   12편 아무 데나 붙는 반응이라 이 작품을 기억하게 만들지 못한다.
            #   사용자 대안: '모르던 정보까지 주는 작품이었습니다' — 줄거리의 '미오도
            #   몰랐던 곳까지 확인된다'를 받아 시청자에게도 걸리는 이중 의미를 만들었다.
            "   ★★한 방은 **이 작품에만 있는 소재**에서 끌어와라. [줄거리]의 설정·장치·"
            "대사 한 조각을 비틀어 쓰는 것이 가장 좋다. 어느 작품에나 붙는 일반 반응"
            "('민망하다', '부끄럽다', '대단하다')은 후킹이 안 된다.\n"
            "   (나쁨: '부끄럼이 다 살린 작품이었습니다' — 아무 맛이 없다)\n"
            "   (나쁨: '보는 내가 더 민망한 작품입니다' — 어느 편에나 붙는다)\n"
            "   (좋음: '모르던 정보까지 주는 작품이었습니다' — 검진 컨셉을 받아 비틀었다)\n"
            " · 뻔한 마무리 상투구 금지 — '~가 다 살린', '~가 인상적인', "
            "'취향이면 볼만한', '무난한 작품이었습니다'.\n")
        if is_short:
            tone_rule += SHORT_CLIP_RULE
        log("  프롬프트: 느슨 모드(순화·안전 + 사람 문장 예시, 표현은 AI 재량)"
            + (" · 짧은 클립 구성" if is_short else ""))
    else:
        tone_rule = ("[말투] 유튜브 리뷰어가 옆에서 같이 보며 말해주는 입말. 정중체(~입니다) 기본이되 "
                    "**어미를 반드시 섞어라** — ~네요/~죠/~더군요/의문형/명사형 끊기"
                    "('없던 게 하나 생깁니다', '남은 건 사진 한 장뿐.'). "
                    "★같은 어미 3연속이면 실패다(직전 반려본은 9줄 전부 '~습니다'였다).\n"
                    "AI티 상투어 금지 — '매력적인/인상적인/주목할 만한' '기대가 됩니다' "
                    "'~하는 모습을 보여줍니다'. 느낌표·과장 감탄 금지. "
                    # ★2026-09-07 — 영상을 못 본 채 완성도를 단정하는 형용사가 5개 문체 전부에서
                    #   나왔다("밋밋해도", "느슨해도", "초반은 헐겁지만"). 근거는 stars 하나뿐이고
                    #   그 stars조차 클린본 대사 밀도를 보고 매긴 값이다 → 원천 차단한다.
                    # ★2026-09-07 — "편안한 공기 하나로 1위를 가져갔죠"가 나왔다. 무슨 말인지
                    #   알 수 없는 전형적 AI 문장이다. 추상 명사를 주어·수단으로 쓰면 문장이
                    #   그럴듯해 보이면서 아무 정보도 없다. 패턴 자체를 금지한다.
                    "★[추상어 금지 — AI 문장의 주범] '공기·분위기·여운·온도·에너지·기운·결·"
                    "매력·존재감' 같은 추상 명사를 **주어나 수단으로 쓰지 마라**. "
                    "'~ 하나로', '~만으로', '~가 살아 있다' 구문도 금지.\n"
                    "  (나쁨: '편안한 공기 하나로 1위를 가져갔죠' — 무슨 말인지 알 수 없다)\n"
                    "  (좋음: '카메라 보고 웃는 얼굴 하나로 끌고 갑니다' — 화면에 있는 것)\n"
                    "  문장의 주어는 **사람·행동·사물**이어야 한다. 화면에서 볼 수 있는 것만 쓴다.\n"
                    "★[완성도 단정 금지] 영상을 보지 않았으므로 연출·완성도를 단정하지 마라 — "
                    "'밋밋하다/느슨하다/헐겁다/늘어진다/평범한 연출/설정이 얇다/무난한 구성' 금지. "
                    "말할 수 있는 것은 **화면에서 관찰된 사건**과 메타 사실(레이블·장르·순위)뿐이다. "
                    "stars 값을 근거로 작품을 깎아내리지 마라(자막만 보고 매긴 값이다).\n"
                    "마무리 인사('지금까지 ~였습니다' '시청 감사' '구독') 절대 금지.\n"
                    f"[★리뷰어가 있어야 한다 — 이번 개정의 핵심] {n_total}줄 중 최소 2줄에는 "
                    "**화면에 없는 것**(인물의 속마음, 상황의 의미, 리뷰어 개인 감상·평가)이 들어가야 한다. "
                    "화면에 보이는 것만 나열하면 실패다.\n"
                    "[떡밥] 질문은 최대 1개까지 허용하되, 던졌으면 **뒤 슬롯에서 반드시 받아라**. "
                    "받을 생각이 없으면 아예 던지지 마라.\n"
                    "[★구체 소재 의무] [줄거리]에 적힌 **직업·무대·장치·숫자·관계**를 그대로 살려 써라. "
                    "줄거리가 '건강검진처럼 꾸며 숫자로 확인한다'고 하면 내레이션도 '건강검진'과 "
                    "'숫자'를 써야 한다. 상위어로 바꿔 뭉개지 마라(검진→'확인', 측정 담당자→'누군가'). "
                    "줄거리를 다 읽고도 무슨 작품인지 모르겠는 문장이 나오면 실패다.\n"
                    "[근거] 소개·배우·컨셉 슬롯은 아래 [배우 팩트]/[작품 팩트]/[줄거리]에서, "
                    "장면 슬롯은 그 슬롯의 '화면:'과 '직전:/직후:' 대사에서 사실을 가져온다. "
                    "해석은 얹되 없는 사건을 지어내지는 마라.\n"
                    "[장면 순서] 장면 슬롯은 시간 순으로 이야기가 쌓이게 쓴다. "
                    "평가·별점·필모 비교는 마지막 마무리 슬롯에만.\n"
                    "[순화] 성적 묘사 금지 — 정사=액션신/플레이, 관계를 가지다=선을 넘다 로 눙친다.\n"
                    # ★2026-09-07 — 이 규칙이 과잉 적용돼 '건강검진·측정 담당자·데뷔 5년차'처럼
                    #   아무 문제 없는 소재까지 뭉갰다("검진하듯 기록한다는 내용입니다").
                    #   줄거리 파일에 구체적인 재료를 다 넣어줬는데도 두루뭉술해지는 원인이었다.
                    #   → 적용 범위를 **범죄·금기 관계로 한정**하고, 그 외 소재는 오히려 구체적으로.
                    "[★소재 직접 지목 금지 — 유튜브 안전] **범죄·금기 관계에 한정된 규칙이다.** "
                    "감금/협박/불륜/근친/시아버지/교사와 학생 **이런 것만** 그대로 못박지 말고 "
                    "그 상황이 인물에게 어떤 처지인지로 바꿔 말한다.\n"
                    "  ★그 밖의 소재는 **반대로 구체적으로 밝혀라** — 직업·무대·장치·컨셉·"
                    "숫자·소품(건강검진, 측정, 온천 여관, 면접, 마사지, 데뷔 5년차 등)은 "
                    "위험하지 않다. 이런 것까지 '확인할 게 있다고 합니다'처럼 뭉개면 "
                    "**무슨 작품인지 알 수 없는 내레이션**이 된다. "
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

    # ── 구타바리형 오버라이드 ────────────────────────────────────────────
    # 위 examples/seq_rule/tone_rule 은 전부 정중체(~입니다)를 전제로 쓰였다. 문체만
    # 반말로 바꾸면 예시와 지시가 서로 싸우므로(모델이 예시를 따라 존댓말로 돌아간다)
    # 세 블록을 통째로 갈아끼운다. 슬롯 배치·역할 구조는 건드리지 않는다.
    # ★_plain_korean()은 줄거리(prompt_story)에만 걸려 있었다 — 정작 최종 대본을 쓰는
    #   여기에 없어서 평론투·추상어가 그대로 통과했다(2026-09-07 사용자 지적).
    from .prompts import _plain_korean
    tone_rule += _plain_korean()

    if style in ("jindong", "naeson"):
        # 두 문체는 3min과 '무엇을 말하는가' 자체가 다르다 — 3min 톤 위에 정체성 블록을
        # 덧씌운다(어미 변주·AI티 금지·순화 규칙 같은 공통 안전장치는 그대로 유효하다).
        from .prompts import _style_jindong, _style_naeson
        tone_rule += (_style_jindong() if style == "jindong" else _style_naeson())
        log(f"  문체: {'진동기형(스펙 브리핑)' if style == 'jindong' else '내손내싼형(리액션 개그)'}")
    if style == "gootabari":
        examples = f"""[좋은 예 — 이 감각 (문장을 베끼지 말고 이 작품 내용으로 쓸 것)]
S1: "세 번째, {actress}."                              ← 명사로 툭. 소개는 이 한 줄이면 끝
S2: "에스원이 사무실물을 자주 뽑는데, 대체로 평타는 치고."   ← 레이블 맥락+의견
S3: "상사한테 시달리던 직원이 단둘이 야근에 남는, 도망갈 구석 없는 설정."  ← 작품 개괄+스테이크
S4: "다들 퇴근한 사무실. 아직은 서류 얘기뿐이고."          ← 화면+지금 상태
S5: "건네는 손이 필요 이상으로 오래 머문다."               ← 화면 사실에 해석 한 겹
S6: "웃고는 있는데 눈은 안 웃고."                         ← 심리
S7: "그 답이 다음 한마디에서 나온다."                      ← 회수
S8: "여기서부터 두 사람 사이에 없던 게 하나 생긴다."        ← 전환점
S{n_total}: "표정 하나로 다 끌고 가는 작품. 완벽한 참교육의 서막."  ← 결말 미공개 + 명사형 피날레
(감각 예시다. 실제 슬롯 수·시간은 아래 '슬롯:' 목록을 따른다)

[나쁜 예 1 — 존댓말] "세 번째 작품은 {actress}입니다." / "손이 오래 머무네요."
 → 구타바리는 반말이다. 정중체가 한 줄이라도 섞이면 실패.
[나쁜 예 2 — 화면 낭독] "제단 앞에 세 사람이 앉아 있다." / "여성은 두 손을 모은다."
 → 시청자가 이미 보고 있는 화면을 그대로 읽었을 뿐. 정보량 0.
[나쁜 예 3 — 근거 없는 추상 훅] "심상찮다." / "공기가 묘하고."
 → 무슨 일인지 하나도 안 알려준다.
[나쁜 예 4] "B85 W57 H82 키166." → 숫자 낭독 금지.
"""
        if seq:
            si, _sn = seq
            ordinal = ["", "첫", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉", "열",
                       "열한", "열두"]
            nth = f"{ordinal[si]} 번째" if si < len(ordinal) else f"{si}번째"
            seq_rule = (f"[연속 리뷰] 모음집 {seq[1]}편 중 {si}번째 꼭지. "
                        f"S1은 '{nth}, {actress}.' 형태로 **명사로 툭** 끊는다"
                        f"(존댓말·훅·질문 금지). '다음 작품은' 금지. "
                        f"개별 마무리 인사는 넣지 않는다.\n")
        tone_rule = (
            "[말투] 쇼츠 리뷰어 '구타바리' — 냉소적이고 날카로우며 인과응보와 병맛을 즐긴다. "
            "**처음부터 끝까지 반말**로 쓴다. 존댓말·정중체('~입니다','~습니다','~네요','~죠') "
            "한 줄이라도 섞이면 실패다.\n"
            "[★슬롯 역할 설명의 어투는 무시] 아래 '슬롯:' 목록의 역할 설명에 나오는 "
            f"'○ 번째 작품은 {actress}입니다' 같은 문구는 **담을 내용을 알려주는 예시**일 뿐이다. "
            f"실제 출력은 '○ 번째, {actress}.'처럼 반말·명사형으로 쓴다.\n"
            "[★① 끊어치기] **[현상·상태 정의(명사)] + [이유·배경(과거형)]** 구조로 쪼갠다 "
            "('평범한 회사원 같지만 실상은 살인 청부가 본업인 미친 회사였고.'). 최소 2회.\n"
            "[★② 어감 변주] 같은 종결어미 반복 엄금. '~고 / ~데 / ~다 / (명사)'를 섞어 리듬을 만든다. "
            "★'~며'는 어떤 경우에도 쓰지 않는다. 같은 어미 3연속이면 실패.\n"
            "[★③ 앵무새 금지] '직전:/직후:' 대사를 그대로 옮겨 적지 마라. "
            "대사의 속뜻·상황의 본질·심리를 짚어 임팩트를 보강한다.\n"
            "[★④ 명사형 피날레] 변곡점과 마지막 슬롯은 강렬한 명사로 매듭짓는다 "
            "('분노 게이지 MAX.', '완벽한 자업자득.').\n"
            "[길이] 한 문장 20단어 이내. 짧을수록 세다. "
            "AI티 상투어 금지 — '매력적인/인상적인/주목할 만한', '기대가 된다', '~하는 모습을 보여준다'. "
            "느낌표는 전체에서 한두 번까지. "
            "마무리 인사('지금까지 ~였습니다','시청 감사','구독')와 교훈적 마무리 절대 금지 — "
            "오직 쾌락과 참교육.\n"
            f"[★리뷰어가 있어야 한다] {n_total}줄 중 최소 2줄에는 **화면에 없는 것**"
            "(인물의 속마음, 상황의 의미, 리뷰어의 냉소 한 스푼)이 들어가야 한다. "
            "화면에 보이는 것만 나열하면 실패다.\n"
            "[떡밥] 질문은 최대 1개까지. 던졌으면 뒤 슬롯에서 반드시 받아라.\n"
            "[근거] 소개·배우·컨셉 슬롯은 [배우 팩트]/[작품 팩트]/[줄거리]에서, "
            "장면 슬롯은 그 슬롯의 '화면:'과 '직전:/직후:' 대사에서 사실을 가져온다. "
            "해석은 얹되 없는 사건을 지어내지는 마라. 결말은 절대 밝히지 마라.\n"
            "[장면 순서] 장면 슬롯은 시간 순으로 이야기가 쌓이게 쓴다.\n"
            "[순화] 성적 묘사 금지 — 정사=액션신/플레이, 관계를 가지다=선을 넘다 로 눙친다.\n"
            "[★소재 직접 지목 금지 — 유튜브 안전] 범죄·금기 관계를 내레이션이 그대로 못박지 마라. "
            "감금/협박/불륜/근친/시아버지/교사와 학생 같은 말을 쓰지 말고, 그 상황이 인물에게 "
            "어떤 처지인지로 바꿔 말한다. 약물도 직접 언급 금지 — '몽롱물', '몸 상태가 이상해지고.'로 대체.\n"
            "[★패러디는 반드시 짚어라] [작품 판정]의 '패러디:'가 '없음'이 아니면 그 작품명을 "
            "컨셉 슬롯에서 **반드시 한 번** 언급하라.\n"
            "[★사건 먼저] 장면 슬롯은 그 시각의 **주된 사건**을 먼저 말하라 — 누가 들어왔다, "
            "무엇을 건넸다, 무엇이 드러났다. 화면 구석의 사소한 자세·소품을 주인공으로 삼지 마라.\n"
            "[★없는 변화 금지] '더 ~해진다/굳어진다/좁혀진다'는 두 시각의 '화면:'에 실제로 차이가 "
            "적혀 있을 때만 쓴다.\n"
            "[★주어] 사람을 가리킬 때는 누구인지 밝혀라(형·동생·비서·코치).\n"
            "[귀로 듣는 매체] 음성으로 먼저 들린다. 줄임말·구어 축약으로 뭉치지 마라. "
            "지시대상이 불분명한 문장도 금지.\n"
            "[소재 반복 금지] 한 작품 안에서 같은 소재를 세 번 이상 우려먹지 마라.\n")
        log("  문체: 구타바리형(반말·병맛 초압축)")

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
