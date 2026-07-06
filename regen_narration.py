#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""내레이션 재생성 — 메타(배우/신체/레이블) + keep갭 장면전환 + 15자 분할.

사용:
    python regen_narration.py C:/Users/yoon/ja_reviewer_out/SNOS-285
"""
import sys
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# pipeline.py의 retime 사용 — plan.json은 trim좌표, SRT는 final좌표
sys.path.insert(0, str(Path(__file__).parent))
from server.pipeline import retime

EXE      = r"C:\Users\yoon\AppData\Roaming\npm\claude.cmd"
META_API = "http://172.30.1.40:8770"

RULE = """너는 일본 AV 리뷰 채널 나레이터다. 슬롯마다 지정된 스타일이 다르다.

━━ [A] 3분휴지 스타일 ━━
어미: ~ㅂ니다 / ~습니다 / ~거 같습니다 / ~지 않나 싶습니다
내용: 작품 컨셉 설명, 배우 인상, 개인 의견 포함.
예) "S1에서 나온 SNOS-285로, 키168에 C컵입니다."
예) "인터뷰 형식으로 시작해서 차 한 잔이 전환점이 되는 작품입니다."
예) "다우너 계열이라 취향을 타지만 에미카 피지컬은 확실합니다."

━━ [B] 다큐 나레이터 스타일 ━━
어미: ~는데.. / ~게 되는데.. / ~합니다 / 명사절("달아오르는 에미카.")
내용: 장면 묘사. 대사 받아쓰기 금지. 상황·맥락 설명.
예) "낯선 차가 에미카 앞에 놓이는데.."
예) "차를 마신 뒤 이상한 기운이 도는데.."
예) "점점 달아오르는 에미카."

━━ 구성 (6슬롯 고정) ━━
S1 [A] 품번·배우명·신체·레이블 소개. 2줄로 분리.
S2 [B] 핵심 장면 1 — 나레이터 묘사.
S3 [B] 핵심 장면 2 — 나레이터 묘사.
S4 [A] 작품 흐름·컨셉 요약.
S5 [A] 한 줄 평가/인상. ("~거 같습니다" / "~지 않나 싶습니다")
S6 궁금증 질문: "○○는 어떻게 될까요?" — 딱 1회, 여기만.

공통 규칙:
- 1줄 25자 이내. 넘으면 2항목으로 분리.
- 약물 직접 언급 금지 → "이상한 기운이 도는데.." 로.
- "어떻게 될까요" 중간 절대 금지.
"""


def s2srt(x):
    total = int(round(max(0.0, float(x)) * 1000))
    h = total // 3600000; total %= 3600000
    m = total // 60000;   total %= 60000
    s = total // 1000;    ms = total % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fetch_meta(code: str) -> dict:
    try:
        url = f"{META_API}/work/{code}"
        with urllib.request.urlopen(url, timeout=8) as r:
            return json.load(r)
    except Exception as e:
        print(f"  메타 조회 실패 ({e}), 코드명만 사용")
        return {}


def slot_type(slot_start, keep):
    if not keep or slot_start < keep[0][0]: return "인트로"
    if slot_start >= keep[-1][1]: return "아웃트로"
    return "갭"


def dialogue_after(slot_end, dialogue, window=25.0):
    lines = []
    for d in dialogue:
        s = d.get("start", 0)
        if slot_end <= s <= slot_end + window:
            lines.append(d.get("ko", "")[:22])
        if len(lines) >= 2: break
    return lines


def dialogue_before(slot_start, dialogue, window=15.0):
    lines = []
    for d in reversed(dialogue):
        e = d.get("end", 0)
        if slot_start - window <= e <= slot_start:
            lines.insert(0, d.get("ko", "")[:22])
        if len(lines) >= 2: break
    return lines


def regen(folder: Path):
    code = folder.name
    plan_file = folder / f"{code}_plan.json"
    if not plan_file.exists():
        print(f"plan.json 없음: {plan_file}"); sys.exit(1)

    plan      = json.loads(plan_file.read_text(encoding="utf-8"))
    narration = plan.get("narration", [])
    dialogue  = plan.get("dialogue", [])
    keep      = plan.get("keep", [])

    if not narration:
        print("narration 항목 없음"); sys.exit(1)

    # 슬롯 6개로 압축: 인트로 1 + 중간 4 + 아웃트로 1
    MAX_SLOTS = 6
    if len(narration) > MAX_SLOTS:
        intro  = [narration[0]]
        outro  = [narration[-1]]
        middle = narration[1:-1]
        step   = max(1, len(middle) // (MAX_SLOTS - 2))
        mid6   = middle[::step][: MAX_SLOTS - 2]
        narration = intro + mid6 + outro
        print(f"  슬롯 압축: {len(plan['narration'])}→{len(narration)}개")

    # ── 메타 정보 ──────────────────────────────────────────────────────────
    print("메타 조회 중...")
    meta = fetch_meta(code)
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

    meta_line = f"{code}, {actress}."
    if body_short:
        meta_line += f" {body_short}"
    if label:
        label_short = label.replace("NO.1 STYLE","").replace("넘버.원 스타일","").strip()
        meta_line += f" / {label_short}"

    summary = plan.get("summary", "")[:80]

    # ── 슬롯별 어미 매핑 및 설명 ────────────────────────────────────────
    # 슬롯 순서: 인트로(1) → 갭0(2) → 갭1(3) → 갭2+(4,5) → 아웃트로(6)
    ending_map = {
        "인트로":  "어미=~입니다 (배우명/신체/레이블/컨셉 소개, 2줄 분리)",
        "갭0":     "어미=~는데.. 또는 명사절 ('달아오르는 에미카.' 형식) — ~입니다 절대 금지",
        "갭1":     "어미=~는데.. 또는 명사절 — ~입니다 절대 금지",
        "갭2":     "어미=~입니다 또는 ~거 같습니다 (작품 흐름/컨셉 요약)",
        "갭3+":    "어미=~거 같습니다 또는 ~지 않나 싶습니다 (개인 평가/인상)",
        "아웃트로": "어미=질문형 '○○는 어떻게 될까요?' 딱 1회",
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
        before = dialogue_before(ws, dialogue)
        after  = dialogue_after(we, dialogue)

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
S6: "{actress}는 어떻게 될까요?"
"""

    prompt = f"""6슬롯 나레이션. 각 S의 '어미' 규칙을 반드시 지켜라.

{examples}
작품: {meta_line}
배경: {summary[:60]}

슬롯:
{chr(10).join(slots_desc)}

출력: JSON 배열만. start/end 슬롯 시간 사용. 25자 초과 시 2항목 분리.
[{{"start":초,"end":초,"text":"내용","style":"기본"}},...] """

    print(f"프롬프트 {len(prompt)}자 — Claude 호출 중...")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", encoding="utf-8",
                                     delete=False, dir=str(folder)) as f:
        f.write(prompt)
        pf = Path(f.name)
    try:
        with open(pf, "r", encoding="utf-8") as fin:
            r = subprocess.run(
                [EXE, "-p", "--output-format", "text"],
                stdin=fin, timeout=600, text=True, encoding="utf-8", errors="replace",
                capture_output=True
            )
    finally:
        pf.unlink(missing_ok=True)

    raw = (r.stdout or "").strip()
    if not raw:
        print("응답 없음:", (r.stderr or "")[:300]); sys.exit(1)
    raw = raw.replace("```json","").replace("```","").strip()
    s = raw.find("["); e = raw.rfind("]") + 1
    if s < 0 or e <= s:
        print("JSON 파싱 실패:\n", raw[:500]); sys.exit(1)
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
            print("JSON 파싱 실패:\n", raw[:500]); sys.exit(1)
        print(f"  부분 파싱: {len(new_nar)}개")

    # gap_windows는 프롬프트 빌드 전에 이미 계산됨 (위에서 compute_gap_windows 호출)

    # Claude 타이밍 무시 — gap_windows 시간으로 강제 배분 (길이 비례)
    total_dur = sum(e - s for s, e in gap_windows)
    n_items = len(new_nar)
    # 각 창에 배분할 항목 수 (길이 비례, 최소 1)
    raw = [(e - s) / total_dur * n_items for s, e in gap_windows]
    counts = [max(1, round(c)) for c in raw]
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

    print(f"완료: {srt_path}")
    print(f"\n[새 내레이션] {len(new_nar)}줄")
    for n in new_nar:
        flag = "⚠️" if len(n["text"]) > 25 else "  "
        print(f"  {flag}[{n.get('style','기본')}] {n['text']}  ({len(n['text'])}자)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용: python regen_narration.py <출력폴더>"); sys.exit(1)
    regen(Path(sys.argv[1]))
