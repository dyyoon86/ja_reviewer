#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""사용자 에셋 — 상황별 짤(움짤/이미지)과 자막 장식판(plate)을 폴더에서 읽어 쓴다.

설계 원칙: **코드를 고치지 않고 파일만 떨궈서** 늘린다.
  {out_dir}/_assets/
    gifs/{태그}/*.gif|*.mp4|*.webm|*.png   ← 상황별 짤 (여러 개 넣으면 돌아가며 사용)
    captions/emphasis_plate.png             ← 강조 자막 뒤에 깔 장식판(선택)
    captions/info_plate.png                 ← 정보 자막 뒤에 깔 장식판(선택)

태그는 LLM이 고른다(TAGS 목록 안에서만). 폴더가 없거나 비어 있으면 그 태그는 그냥 건너뛴다
— 에셋이 없다고 파이프라인이 죽지 않는다.
"""
import random
from pathlib import Path

# LLM이 고를 수 있는 상황 태그 — 폴더 이름과 1:1로 맞춘다.
# (채널 톤: 냉소+유머 리뷰. 필요하면 여기에 추가하고 같은 이름 폴더를 만들면 끝)
TAGS = ["놀람", "어이없음", "실망", "흥분", "웃음", "의심", "긴장", "민망", "뿌듯", "허무"]

MEDIA_EXT = (".gif", ".mp4", ".webm", ".mov", ".png", ".webp")


def assets_dir(out_dir):
    return Path(out_dir) / "_assets"


def ensure_layout(out_dir, log=print):
    """에셋 폴더 뼈대 + 사용법 README를 만든다(이미 있으면 그대로)."""
    root = assets_dir(out_dir)
    (root / "captions").mkdir(parents=True, exist_ok=True)
    for t in TAGS:
        (root / "gifs" / t).mkdir(parents=True, exist_ok=True)
    readme = root / "읽어보세요.md"
    if not readme.is_file():
        readme.write_text(
            "# 에셋 폴더 — 파일만 넣으면 자동으로 쓰입니다\n\n"
            "## 상황별 짤 (gifs/)\n"
            "`gifs/{태그}/` 안에 gif·mp4·webm·png를 넣으세요. 여러 개 넣으면 번갈아 씁니다.\n"
            "AI가 내레이션마다 상황 태그를 고르고, 그 태그 폴더의 짤이 화면 구석에 뜹니다.\n\n"
            f"사용 가능한 태그: {', '.join(TAGS)}\n\n"
            "· 투명 배경(gif/png/webm)이면 배경 없이 얹힙니다. mp4는 사각형 그대로 뜹니다.\n"
            "· 태그 폴더가 비어 있으면 그 상황은 그냥 건너뜁니다(오류 아님).\n"
            "· 새 태그를 쓰고 싶으면 server/core/assets.py 의 TAGS에 추가하세요.\n\n"
            "## 자막 장식판 (captions/)\n"
            "`captions/emphasis_plate.png` — 강조 자막 뒤에 깔 판\n"
            "`captions/info_plate.png` — 정보 자막 뒤에 깔 판\n"
            "가로로 긴 투명 PNG를 권장합니다(자막 폭에 맞춰 늘어납니다).\n",
            encoding="utf-8")
        log(f"에셋 폴더를 만들었습니다: {root}")
    return root


def gifs_for(out_dir, tag):
    """태그 폴더의 미디어 파일 목록(정렬)."""
    d = assets_dir(out_dir) / "gifs" / str(tag)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file() and p.suffix.lower() in MEDIA_EXT)


def pick_gif(out_dir, tag, index=0):
    """태그에 해당하는 짤 하나. 여러 개면 index로 돌려가며(같은 태그가 반복돼도 안 질리게).
    없으면 None."""
    files = gifs_for(out_dir, tag)
    if not files:
        return None
    return str(files[index % len(files)])


def plate(out_dir, kind):
    """자막 장식판 경로 — kind='emphasis'|'info'. 없으면 None."""
    for ext in (".png", ".webp"):
        p = assets_dir(out_dir) / "captions" / f"{kind}_plate{ext}"
        if p.is_file():
            return str(p)
    return None


def available(out_dir):
    """GUI용 — 태그별로 몇 개 들어있는지."""
    return {t: len(gifs_for(out_dir, t)) for t in TAGS}


def resolve_cutins(out_dir, cutins, log=print):
    """LLM이 준 [{start, end?, tag}] → [(start, end, 파일경로)] 로 확정.
    태그에 파일이 없으면 조용히 건너뛴다(로그만 남김)."""
    used = {}
    out = []
    miss = set()
    for c in (cutins or []):
        tag = str(c.get("tag") or "").strip()
        if tag not in TAGS:
            continue
        i = used.get(tag, 0)
        p = pick_gif(out_dir, tag, i)
        if not p:
            miss.add(tag)
            continue
        used[tag] = i + 1
        st = float(c.get("start", 0))
        en = float(c.get("end", st + 2.0))
        if en <= st:
            en = st + 2.0
        out.append((st, en, p))
    if miss:
        log(f"※ 짤 없음(건너뜀): {', '.join(sorted(miss))} — "
            f"{assets_dir(out_dir) / 'gifs'} 에 넣으면 다음부터 들어갑니다")
    if out:
        log(f"짤 삽입: {len(out)}개")
    return sorted(out)
