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
import subprocess
from pathlib import Path

from .common import s2srt, retime, parse_keep, video_duration, invalidate_derived
from .llm import fetch_meta, _cli_path, call_llm
from .prompts import prompt_manual
from .cutter import cut_video


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


def regen_narration(folder: Path, meta_api: str, log=print, seq=None):
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

    # 슬롯 6개로 압축: 인트로 1 + 중간 4 + 아웃트로 1
    MAX_SLOTS = 6
    if len(narration) > MAX_SLOTS:
        intro  = [narration[0]]
        outro  = [narration[-1]]
        middle = narration[1:-1]
        step   = max(1, len(middle) // (MAX_SLOTS - 2))
        mid6   = middle[::step][: MAX_SLOTS - 2]
        narration = intro + mid6 + outro
        log(f"  슬롯 압축: {len(plan['narration'])}→{len(narration)}개")

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

    ending_map = {
        "인트로":  "어미=~입니다 (배우명/신체/레이블/컨셉 소개, 2줄 분리)",
        "갭0":     "어미=~는데.. 또는 명사절 ('달아오르는 에미카.' 형식) — ~입니다 절대 금지",
        "갭1":     "어미=~는데.. 또는 명사절 — ~입니다 절대 금지",
        "갭2":     "어미=~입니다 또는 ~거 같습니다 (작품 흐름/컨셉 요약)",
        "갭3+":    "어미=~거 같습니다 또는 ~지 않나 싶습니다 (개인 평가/인상)",
        "아웃트로": outro_rule,
    }

    # ── gap_windows 먼저 계산 — 프롬프트 슬롯 설명에 사용 ───────────────
    def compute_gap_windows(keep_segs, n_slots=6):
        if not keep_segs: return []
        windows = []
        k0s, k0e = keep_segs[0]
        intro_span = min(12.0, (k0e - k0s) * 0.4)
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

    gap_windows = compute_gap_windows(keep, MAX_SLOTS)
    if not gap_windows:
        gap_windows = [(n["start"], n["end"]) for n in narration]

    # ── 슬롯 설명 빌드 (인덱스 기반 슬롯 분류) ──────────────────────────
    n_total = len(narration)
    slots_desc = []
    _gi = 0
    for i, n in enumerate(narration):
        # 인덱스 기반: i<2 → 인트로, i==last → 아웃트로, else → 갭
        if i < 2:              ek = "인트로"
        elif i == n_total - 1: ek = "아웃트로"
        elif _gi == 0:         ek = "갭0";  _gi += 1
        elif _gi == 1:         ek = "갭1";  _gi += 1
        elif _gi == 2:         ek = "갭2";  _gi += 1
        else:                  ek = "갭3+"; _gi += 1

        ws, we = gap_windows[i] if i < len(gap_windows) else (n["start"], n["end"])
        before = _dialogue_before(ws, dialogue)
        after  = _dialogue_after(we, dialogue)

        line = f"S{i+1}({ws:.0f}~{we:.0f}초) {ending_map[ek]}"
        if before: line += " 직전:" + "/".join(f'「{t}」' for t in before)
        if after:  line += " 직후:" + "/".join(f'「{t}」' for t in after)
        slots_desc.append(line)

    examples = f"""[좋은 예 — 이렇게 써라]
S1: "{actress}입니다." / "키{height or '?'} {cup or '?'}컵, {label_short or '신작'}입니다."
S2: "낯선 제안이 {actress}앞에 놓이는데.."
S3: "이상한 기운이 도는데.." / "점점 달아오르는 {actress}."
S4: "이런 전개를 가진 작품입니다."
S5: "{actress} 피지컬은 확실한 거 같습니다."
S6: (아웃트로 — 아래 슬롯의 어미 지시를 그대로 따를 것)
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
                    f"S1 첫 줄은 반드시 '{nth} 작품은 {actress}입니다.'로 시작한다"
                    f"('다음 작품은' 금지 — 몇 번째인지 명시).\n")

    tone_rule = ("[말투] 유튜브 리뷰어의 자연스러운 입말로. AI티 나는 문어체 금지 — "
                 "'~하는 모습입니다' '~을 보여줍니다' '기대가 됩니다' "
                 "'매력적인/인상적인/주목할 만한' 같은 상투어 금지. 같은 어미 3연속 금지. "
                 "마무리 인사('지금까지 ~였습니다' '시청 감사' '구독') 절대 금지.\n")

    prompt = f"""영상 리뷰 채널의 전연령 시청용 '작품 소개' 나레이션 작업이다 — 성적 묘사 없이
배우·컨셉 소개와 스토리 호기심 유발만 한다. 6슬롯 나레이션. 각 S의 '어미' 규칙을 반드시 지켜라.

{examples}
{seq_rule}{tone_rule}작품: {meta_line}
배경: {summary[:60]}

슬롯:
{chr(10).join(slots_desc)}

출력: JSON 배열만. start/end 슬롯 시간 사용. 25자 초과 시 2항목 분리.
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
        import re
        items = re.findall(r'\{[^{}]+\}', raw[s:])
        new_nar = []
        for item in items:
            try: new_nar.append(json.loads(item))
            except: pass
        if not new_nar:
            raise RuntimeError(f"JSON 파싱 실패:\n{raw[:500]}")
        log(f"  부분 파싱: {len(new_nar)}개")

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
        dur = (we - ws) / len(chunk)
        for j, entry in enumerate(chunk):
            entry["start"] = round(ws + j * dur, 2)
            entry["end"]   = round(ws + (j + 1) * dur, 2)
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
        flag = "⚠️" if len(n["text"]) > 25 else "  "
        log(f"  {flag}[{n.get('style','기본')}] {n['text']}  ({len(n['text'])}자)")
    return new_nar
