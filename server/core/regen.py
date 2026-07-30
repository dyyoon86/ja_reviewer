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


def narration_slots(video_sec, lo=6, hi=14, per=10.0):
    """영상 길이 → 내레이션 슬롯 수. 예전엔 6으로 고정이었는데, 목표 길이를 60→120초로
    늘리자 2분 영상에 내레이션 6줄만 남아 빈 구간이 길어졌다("지루한 부분 없게" 요구와
    정면 충돌). 10초당 1슬롯으로 비례시키고 6~14 사이로 묶는다(60초=6, 120초=12)."""
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
    vis_entries = []
    vbf = folder / f"{code}_시각브리핑.txt"
    if vbf.is_file():
        for ln in vbf.read_text(encoding="utf-8").splitlines():
            mm = re.match(r"\s*\[?(\d+)\s*s\]?\s*[:：]?\s*(.+)", ln)
            if mm:
                vis_entries.append((int(mm.group(1)), mm.group(2).strip()))
        if vis_entries:
            log(f"  화면 시각정보 {len(vis_entries)}줄 반영")

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

    summary = plan.get("summary", "")[:80]

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

    # 슬롯 역할 v2 (2026-07-18) — 어미만 지정하던 v1은 '문법만 다른 설명문' 6개가 나왔다
    # (사용자: "내레이션이 재미없다/AI 같다"). 골채널 분석(벤치마킹/) 결과를 역할로 강제:
    # 훅(내손내싼) → 팩트(락규) → 떡밥 질문(3분휴지) → 심리/전환점(3분휴지·휴지도둑) → 감상 → 아웃트로.
    ending_map = {
        "인트로":  ("역할=훅. 1줄째: 서수+배우를 짧게 밝히고 곧바로 '예상이 빗나간 리액션'이나 "
                   "'수상한 낌새'를 던진다 — 설명문 금지. 2줄째: 컨셉+레이블을 팩트로 한 줄 "
                   "(신체 스펙 나열 금지, 컨셉이 그려지게)"),
        "갭0":     "역할=떡밥 질문. '왜 ~일까요?' '무슨 생각일까요?' 화면 속 행동에 의문을 단다 — ~입니다 절대 금지",
        "갭1":     ("역할=심리 해설. 인물 속마음을 대신 읽는다 — '표정이 ~해 보입니다' "
                   "'~인가 봅니다' 또는 명사절('점점 달아오르는 두 사람.')"),
        "갭2":     "역할=전환점. 관계·상황이 변하는 순간을 굵게 한 줄 — '그렇게 ~하게 됐습니다' '이제 ~할 수 없게 됐습니다'",
        "갭3+":    ("역할=개인 감상. 구체 비유나 드라이 농담 허용 — 어미=~거 같습니다/~지 않나 싶습니다. "
                   "추상 형용사 대신 장면이 그려지는 비유로"),
        "아웃트로": outro_rule,
    }

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
        while len(wins) < n_slots:
            # 첫·마지막 창(인트로·아웃트로)은 되도록 쪼개지 않는다 — 인트로는
            # '서수+배우명'만 14자라 창이 짧으면 훅을 넣을 자리가 없다.
            inner = list(range(1, len(wins) - 1)) or list(range(len(wins)))
            cand = [k for k in inner if wins[k][1] - wins[k][0] >= 2 * NAR_SLOT_MIN]
            pool = cand or list(range(len(wins)))
            i = max(pool, key=lambda k: wins[k][1] - wins[k][0])
            a, b = wins[i]
            # 반으로만 가르면 8s/2.5s 같은 틈에서 3등분 기회를 놓친다 → 들어가는 만큼 균등분할
            k = int((b - a) // NAR_SLOT_MIN)
            if k < 2:
                break                       # 더 쪼개면 한 문장이 안 들어간다
            k = min(k, n_slots - len(wins) + 1)
            step = (b - a) / k
            wins[i:i + 1] = [(round(a + j * step, 2), round(a + (j + 1) * step, 2))
                             for j in range(k)]
        wins.sort()
        if len(wins) < 3:
            return None                     # 인트로·중간·아웃트로도 못 놓으면 후퇴
        if len(wins) > n_slots and n_slots >= 3:
            mids = sorted(wins[1:-1], key=lambda w: w[1] - w[0],
                          reverse=True)[:n_slots - 2]
            mids.sort()
            wins = [wins[0]] + mids + [wins[-1]]
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
    n_intro = 2 if n_total >= 5 else 1      # 슬롯이 적으면 인트로도 한 줄로
    log(f"  내레이션 {len(narration)}줄 → {n_total}줄로 재작성"
        if len(narration) != n_total else f"  내레이션 {n_total}줄")
    slots_desc = []
    budgets = []
    _gi = 0
    for i, (ws, we) in enumerate(gap_windows):
        if i < n_intro:        ek = "인트로"
        elif i == n_total - 1: ek = "아웃트로"
        elif _gi == 0:         ek = "갭0";  _gi += 1
        elif _gi == 1:         ek = "갭1";  _gi += 1
        elif _gi == 2:         ek = "갭2";  _gi += 1
        else:                  ek = "갭3+"; _gi += 1

        before = _dialogue_before(ws, dialogue)
        after  = _dialogue_after(we, dialogue)

        # 슬롯마다 제 창 길이에 맞는 글자수를 준다 — 전역 상한 하나로 묶으면
        # 인트로("다섯 번째 작품 하츠미 나노카"만 14자)에 훅을 넣을 자리가 없다.
        budgets.append(_char_budget(we - ws))
        line = f"S{i+1}({ws:.0f}~{we:.0f}초, {budgets[-1]}자 이내) {ending_map[ek]}"
        if before: line += " 직전:" + "/".join(f'「{t}」' for t in before)
        if after:  line += " 직후:" + "/".join(f'「{t}」' for t in after)
        vis_here = [d for (t, d) in vis_entries if ws - 3 <= t <= we + 3][:2]
        if vis_here: line += " 화면:" + " / ".join(vis_here)
        slots_desc.append(line)

    examples = f"""[좋은 예 — 이 감각을 따를 것 (문장을 그대로 베끼지 말고 이 작품 내용으로)]
S1: "첫 작품 {actress}, 분위기가 수상합니다."
S2: "영화관 알바 컨셉, {label_short or '신작'}입니다."
S3: "이 잡담, 왜 자꾸 약속 얘기죠?"
S4: "눈은 안 웃는 거 같습니다."
S5: "둘만의 비밀이 생겨버렸습니다."
S{n_total}: (마지막=아웃트로 — 아래 슬롯의 어미 지시를 그대로 따를 것)
(위는 감각 예시다. 실제 슬롯은 S1~S{n_total}이고, 아래 '슬롯:' 목록의 개수·시간을 따른다)

[나쁜 예 — 이렇게 쓰면 실패 (ja12에서 실제로 재미없다고 판정된 문장들)]
"{actress}입니다." → 훅 없이 이름만 던지는 오프닝 금지
"레이블은 ○○, 청초한 마스크입니다." → 스펙 나열 + 추상 형용사 금지
"이런 전개를 가진 작품입니다." → 아무 정보도 감정도 없는 문장 금지
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
                    f"S1 첫 줄에 '{nth} 작품'과 배우명을 반드시 포함하되, "
                    f"'{nth} 작품은 {actress}입니다.' 단독 문장으로 끝내지 말고 훅과 붙인다 "
                    f"(예: '{nth} 작품, {actress}. 시작하자마자 ~합니다.'). '다음 작품은' 금지.\n")

    from .prompts import _human_tone
    tone_rule = ("[말투] 유튜브 리뷰어의 자연스러운 입말로. AI티 나는 문어체 금지 — "
                 "'~하는 모습입니다' '~을 보여줍니다' '기대가 됩니다' "
                 "'매력적인/인상적인/주목할 만한' 같은 상투어 금지. 같은 어미 3연속 금지. "
                 "마무리 인사('지금까지 ~였습니다' '시청 감사' '구독') 절대 금지.\n"
                 + _human_tone() +
                 "[자가검열 — 출력 직전에 한 번 더] 각 문장을 소리 내어 읽는다고 상상하고, "
                 "유튜버가 절대 안 할 말(설명문 나열, 스펙 낭독, 상투어)이 있으면 사람 말로 다시 쓴다. "
                 f"{n_total}문장 중 질문이 하나도 없으면 실패다.\n"
                 "[화면 반영] 슬롯에 '화면:'이 붙어 있으면 그게 그 순간 실제 장면이다. "
                 "그 자리에서 보는 것처럼 생생히 중계하되(현재형·구체), '~물에 가까운 시추에이션' 같은 "
                 "장르 요약·평론으로 흘리지 마라. 평가·별점·필모 비교는 마지막 아웃트로 슬롯에만.\n")

    prompt = f"""영상 리뷰 채널의 전연령 시청용 '작품 소개' 나레이션 작업이다 — 성적 묘사 없이
배우·컨셉 소개와 스토리 호기심 유발만 한다. {n_total}슬롯 나레이션. 각 S의 '어미' 규칙을 반드시 지켜라.

{examples}
{seq_rule}{tone_rule}작품: {meta_line}
배경: {summary[:60]}

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
