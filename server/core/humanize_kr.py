# -*- coding: utf-8 -*-
"""im-not-ai(humanize-korean) 룰북을 내레이션 검수용으로 잘라 쓰는 어댑터.

내레이션 검수(narreview)의 'AI 문체' 항목은 오래 손으로 적은 상투어 다섯 개가
전부였다. 그 목록에 없는 티(번역투 '~를 통해', 이중피동 '판단되어진다',
분열문 '핵심은 ~라는 점이다', 관통 은유 '잠식한다' 등)는 그대로 통과했다.

그래서 판정 근거를 외부 룰북으로 옮긴다 — GitHub `epoko77-ai/im-not-ai` 의
humanize-korean 스킬이 쓰는 `quick-rules.md`(한국어 AI 티 분류 SSOT에서
자동 생성되는 슬림 룰북)다. 설치돼 있으면 설치본을 그대로 읽고, 없으면
저장소에 떠 둔 스냅샷(`server/core/refs/quick-rules.md`)으로 떨어진다.

  설치: /plugin marketplace add epoko77-ai/im-not-ai
        /plugin install humanize-korean@im-not-ai
  갱신: python tools/sync_humanize_rules.py   (스냅샷을 설치본으로 덮어씀)

내레이션은 **낭독되는 구어체 다큐 자막**이라 서식·레이아웃 규칙(불릿·이모지·
콜론 헤딩·따옴표 장식 등)은 애초에 발생할 수 없다. 그런 항목까지 프롬프트에
넣으면 검수자가 없는 결함을 찾으러 다니므로 EXCLUDE 로 걸러낸다.
"""
import os
import re
from pathlib import Path

# 낭독 자막에서는 물리적으로 나올 수 없는 서식/레이아웃 규칙 — 프롬프트에서 뺀다.
#   B-1 괄호 영어병기 · C-2 연속 불릿 · C-5 이모지 · C-9 숫자 인덱싱
#   C-10 콜론 부제 헤딩 · I-4 당위 문단 배치 · J-2 따옴표 강조 · J-3 대시 부가설명
#   G-3 균형 lexicon(룰북 자체가 '실증 부족 — hold')
EXCLUDE = {"B-1", "C-2", "C-5", "C-9", "C-10", "G-3", "I-4", "J-2", "J-3"}

# 설치본 우선 탐색 경로. 앞에서부터 먼저 존재하는 것을 쓴다.
_REL = "skills/humanize-korean/references/quick-rules.md"


def _candidates():
    env = os.environ.get("IMNOTAI_HOME")
    if env:
        yield Path(env) / _REL
        yield Path(env) / "references/quick-rules.md"
    home = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
    yield home / "plugins/marketplaces/im-not-ai" / _REL
    yield home / _REL
    yield home / "skills/humanize-korean/references/quick-rules.md"
    yield Path.home() / _REL                       # install.sh --claude-only 배치
    yield Path(__file__).with_name("refs") / "quick-rules.md"   # 저장소 스냅샷


def rulebook_path():
    """실제로 읽을 룰북 경로. 하나도 없으면 None."""
    for p in _candidates():
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


_RULE = re.compile(r"^-\s+\*\*([A-Z]-\d+)\*\*\s*(.+)$")
_SECT = re.compile(r"^##\s+([A-Z])\.\s*(.+?)\s*$")


def parse(text):
    """quick-rules.md → [(섹션제목, [(ID, 본문), ...]), ...]. 자체검증/등급 절은 버린다."""
    out, cur = [], None
    for ln in text.splitlines():
        m = _SECT.match(ln)
        if m:
            cur = (f"{m.group(1)}. {m.group(2)}", [])
            out.append(cur)
            continue
        m = _RULE.match(ln)
        if m and cur is not None:
            cur[1].append((m.group(1), m.group(2).strip()))
    return [(t, rs) for t, rs in out if rs]


def rules_block(exclude=EXCLUDE):
    """검수 프롬프트에 그대로 끼울 규칙 본문. 룰북이 없으면 빈 문자열."""
    p = rulebook_path()
    if not p:
        return ""
    try:
        secs = parse(p.read_text(encoding="utf-8"))
    except Exception:
        return ""
    buf = []
    for title, rules in secs:
        keep = [(i, b) for i, b in rules if i not in exclude]
        if not keep:
            continue
        buf.append(f"\n[{title}]")
        buf.extend(f" - {i} {b}" for i, b in keep)
    return "\n".join(buf).strip()


def rule_ids(exclude=EXCLUDE):
    """프롬프트에 실린 규칙 ID 집합 — 응답 검증용."""
    p = rulebook_path()
    if not p:
        return set()
    try:
        secs = parse(p.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {i for _, rs in secs for i, _ in rs if i not in exclude}


def source_note():
    """리포트에 남길 룰북 출처 한 줄."""
    p = rulebook_path()
    if not p:
        return "im-not-ai 룰북 없음(내장 상투어 목록만 사용)"
    kind = "저장소 스냅샷" if p.parent.name == "refs" else "설치본"
    return f"im-not-ai quick-rules ({kind}: {p})"


if __name__ == "__main__":                                    # 점검용
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(source_note())
    ids = sorted(rule_ids())
    print(f"규칙 {len(ids)}개: {', '.join(ids)}")
    print()
    print(rules_block()[:1200])
