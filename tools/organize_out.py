# -*- coding: utf-8 -*-
r"""작업 폴더 정리 — 단계별 `_산출물/` 트리와 안내문을 만든다.

품번 폴더 하나에 파일이 25개쯤 평면으로 쌓여 있어 무엇이 어느 단계 결과물인지
알 수 없다. 이 도구는 **원본 파일을 건드리지 않고**(모든 코드·도구가 기존 경로를
그대로 쓴다) 하드링크로 단계별 폴더를 세운다 — 디스크를 더 먹지 않는다.

    {품번}/
      _산출물/
        안내.md          ← 이 편의 파일이 각각 무엇인지
        0_기록/          작업로그·상태·자체검사
        1_클린/          클린본 + 원본 전체 전사
        2_리뷰/          줄거리·전사·plan·컷 완료 영상
        3_자막/          대사/내레이션 SRT·JSON
        4_음성배너/      내레이션 wav·인포배너 PNG
        5_납품/          최종 납품본

하드링크라 어느 쪽을 열어도 같은 파일이다. 삭제해도 원본은 남는다.

사용:
  .venv\Scripts\python.exe tools\organize_out.py --out "F:\ja_reviewer_out\ja21"
  옵션: [--only ABF-382,MIKR-118] [--relink] [--copy]
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401

# (폴더, 파일 접미사, 설명) — 접미사가 "/"로 끝나면 디렉터리
MANIFEST = [
    ("0_기록", "_작업로그.md",      "이 편에 언제 무슨 일이 있었는지 한 줄씩. 문제 추적의 시작점"),
    ("0_기록", "_state.json",       "어느 영상을 썼는지·목표 길이·문체 등 진행 상태"),
    ("0_기록", "_검사.json",        "자체 검사 결과(컷 경계 팝·정지·무음·자막 커버리지·내레이션 줄 수)"),
    ("0_기록", "_plan.json.presafe", "눈검사 재컷 **이전**의 plan 백업. 무엇이 잘려나갔는지 비교용"),

    ("1_클린", "_클린.mp4",         "섹션1 결과. 3중 필터로 부적절 구간을 물리적으로 잘라낸 영상"),
    ("1_클린", "_원본전사.json",    "★원본 100~225분 **전체**의 일본어 전사. 잘려나간 부분까지 들어 있는 유일한 기록"),
    ("1_클린", "_원본전사.srt",     "위 전사를 사람이 읽기 좋은 자막 형식으로"),
    ("1_클린", "_노출지도.json",    "NudeNet 노출 스캔 결과(구간 목록)"),

    ("2_리뷰", "_줄거리.md",        "★★사람이 읽는 줄거리 요약. 사이트 소개문과의 대조 판정 포함 — 여기부터 열어라"),
    ("2_리뷰", "_줄거리.txt",       "위 요약을 프롬프트에 넣는 형태로 만든 것(컷 선정·내레이션의 근거)"),
    ("2_리뷰", "_전사.json",        "클린본 러프 전사(구간 선정용, small 모델)"),
    ("2_리뷰", "_전사.srt",         "위 전사의 자막 형식"),
    ("2_리뷰", "_정밀전사.json",    "최종 컷 구간만 large-v3로 다시 전사한 것. 대사 자막의 원본"),
    ("2_리뷰", "_시각브리핑.txt",   "클린본 화면을 비전 모델이 읽은 브리핑(행동·표정·소품·화면글자)"),
    ("2_리뷰", "_plan.json",        "★LLM 출력 원본. keep 구간·대사·내레이션·요약·별점이 전부 여기 있다"),
    ("2_리뷰", "_final.mp4",        "컷만 끝난 영상(자막·배너·음성 없음)"),

    ("3_자막", "_대사.srt",         "한글 대사 자막(번인에 실제로 들어가는 것)"),
    ("3_자막", "_대사.json",        "위와 같되 화자(여/남) 포함 — 색 구분용"),
    ("3_자막", "_내레이션.srt",     "해설 내레이션. **TTS 입력**이라 드립 자막은 빠져 있다"),
    ("3_자막", "_내레이션.json",    "위와 같되 유형(기본/드립) 포함 — 화면 표시용"),

    ("4_음성배너", "_내레이션.wav", "합성된 내레이션 트랙 한 벌(문장별 조각을 배치·합성한 것)"),
    ("4_음성배너", "_tts/",         "문장별 음성 조각(n001.wav …). 재컷 시 재사용된다"),

    ("5_납품", "_final_subbed.mp4", "★최종 납품본. 1080p 리프레임 + 대사 자막 + 배너/워터마크"),
    ("5_납품", "_final_voiced.mp4", "내레이션 음성을 영상에 섞은 판(있을 때)"),
    ("5_납품", "_final_subbed.ass", "번인에 쓴 자막 스크립트(스타일 확인용)"),
]

# 인포배너는 {out_dir}/_infocard_{품번}/ 에 따로 생긴다
BANNER = [
    ("_프레임.png",   "상시 노출되는 테두리 프레임"),
    ("_인포카드.png", "작품 정보 카드(도입부에만)"),
    ("_워터마크.png", "채널 워터마크(상시)"),
]

STAGE_NOTE = {
    "0_기록":     "무슨 일이 있었는지",
    "1_클린":     "섹션1 ⓪ 클린 — 부적절 구간 제거",
    "2_리뷰":     "섹션2 ①②③ — 내용 파악 · 컷 선정 · 번역",
    "3_자막":     "섹션2 ③ — 화면에 올라갈 글자",
    "4_음성배너": "섹션3 ②③ — 목소리와 그래픽",
    "5_납품":     "섹션3 ④ — 내보낼 물건",
}


def link_or_copy(src: Path, dst: Path, force_copy=False, log=print):
    """하드링크 우선(디스크 0바이트). 안 되면 작은 파일만 복사."""
    if dst.exists() or dst.is_symlink():
        return "이미 있음"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not force_copy:
        try:
            os.link(src, dst)
            return "링크"
        except OSError:
            pass
    if src.stat().st_size > 20 * 1024 * 1024:
        log(f"      ※ 링크 실패 + 20MB 초과 → 건너뜀: {src.name}")
        return "건너뜀"
    shutil.copy2(src, dst)
    return "복사"


def link_tree(src: Path, dst: Path, force_copy=False, log=print):
    """폴더 통째로(하위 파일 하드링크)."""
    if not src.is_dir():
        return "없음"
    n = 0
    for f in sorted(src.iterdir()):
        if f.is_file():
            link_or_copy(f, dst / f.name, force_copy, log)
            n += 1
    return f"{n}개"


def write_guide(work: Path, code: str, found, banner_found):
    """이 편의 파일이 각각 무엇인지 — 폴더를 열었을 때 제일 먼저 읽을 것."""
    L = [f"# {code} — 산출물 안내", "",
         "이 폴더(`_산출물/`)는 **보기용 정리본**이다. 실제 작업 파일은 한 단계 위",
         f"(`{code}/`)에 그대로 있고, 여기 있는 것은 같은 파일을 가리키는 하드링크다",
         "— 어느 쪽을 열어도 내용은 같고, 디스크를 더 쓰지 않는다.", ""]
    for stage in ["0_기록", "1_클린", "2_리뷰", "3_자막", "4_음성배너", "5_납품"]:
        items = [x for x in found if x[0] == stage]
        if not items:
            continue
        L += [f"## {stage} — {STAGE_NOTE[stage]}", ""]
        L += ["| 파일 | 설명 |", "|---|---|"]
        for _s, name, desc in items:
            L.append(f"| `{name}` | {desc} |")
        L.append("")
        if stage == "4_음성배너" and banner_found:
            L += ["| 파일 | 설명 |", "|---|---|"]
            for name, desc in banner_found:
                L.append(f"| `{name}` | {desc} |")
            L.append("")
    L += ["---", "",
          "## 자주 보게 되는 것", "",
          f"- **결과가 이상할 때** → `0_기록/{code}_작업로그.md` 부터. 단계별로 무엇이 몇 개 나왔는지 적혀 있다.",
          f"- **내용이 궁금할 때** → `2_리뷰/{code}_줄거리.md`. 잘려나간 부분까지 포함한 전체 줄거리 + 사이트 소개문과의 대조 판정.",
          f"- **자막이 빈다** → `2_리뷰/{code}_정밀전사.json`(일본어 원본)과 `3_자막/{code}_대사.srt`(한글)의 줄 수를 비교한다.",
          f"- **내레이션이 이상하다** → `2_리뷰/{code}_plan.json`의 `narration`(초안)과 `3_자막/{code}_내레이션.srt`(최종)를 비교한다.",
          f"- **납품할 것** → `5_납품/{code}_final_subbed.mp4` + `4_음성배너/{code}_내레이션.wav`", ""]
    (work / "안내.md").write_text("\n".join(L), encoding="utf-8")


def organize(outdir: Path, code: str, force_copy=False, relink=False, log=print):
    src_dir = outdir / code
    work = src_dir / "_산출물"
    if relink and work.is_dir():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    found, missing = [], []
    for stage, suffix, desc in MANIFEST:
        name = f"{code}{suffix}"
        if suffix.endswith("/"):
            name = f"{code}{suffix[:-1]}"
            s = src_dir / name
            if s.is_dir():
                link_tree(s, work / stage / name, force_copy, log)
                found.append((stage, name + "/", desc))
            else:
                missing.append(name)
            continue
        s = src_dir / name
        if s.is_file():
            link_or_copy(s, work / stage / name, force_copy, log)
            found.append((stage, name, desc))
        else:
            missing.append(name)

    banner_found = []
    icdir = outdir / f"_infocard_{code}"
    for suffix, desc in BANNER:
        s = icdir / f"{code}{suffix}"
        if s.is_file():
            link_or_copy(s, work / "4_음성배너" / f"{code}{suffix}", force_copy, log)
            banner_found.append((f"{code}{suffix}", desc))

    write_guide(work, code, found, banner_found)
    return len(found) + len(banner_found), missing


def main():
    ap = argparse.ArgumentParser(description="작업 폴더를 단계별 _산출물/ 트리로 정리")
    ap.add_argument("--out", help="out_dir. 생략 시 studio_config.json의 out_dir")
    ap.add_argument("--only", default="", help="이 품번만(쉼표 구분)")
    ap.add_argument("--relink", action="store_true", help="기존 _산출물/을 지우고 다시 만든다")
    ap.add_argument("--copy", action="store_true", help="하드링크 대신 복사(다른 드라이브로 뺄 때)")
    ap.add_argument("--src", help="원본 영상 폴더(랭킹 txt가 있는 곳). 주면 out_dir 밑에 "
                                  "_순위/ 를 만들어 'NN위_품번_배우' 이름으로 정리한다")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    outdir = Path(args.out or cfg["out_dir"])
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}

    codes = sorted(d.name for d in outdir.iterdir()
                   if d.is_dir() and not d.name.startswith("_"))
    if only:
        codes = [c for c in codes if c.upper() in only]
    if not codes:
        print(f"정리할 품번 폴더가 없습니다: {outdir}")
        return 1

    print(f"out_dir={outdir} / 대상 {len(codes)}편\n")
    # ★2026-09-07 — --src(순위 뷰)를 쓰면 품번별 _산출물/ 은 만들지 않는다.
    #   둘 다 같은 파일을 가리키는 하드링크 뷰라 완전히 중복이고, 품번 폴더 안에
    #   또 하나의 트리가 생겨 오히려 지저분해진다(사용자 지적). 순위 뷰가 상위 호환이다.
    if args.src:
        print("  (--src 지정 — 품번별 _산출물/ 대신 _순위/ 로만 정리한다)\n")
        codes = []
    for code in codes:
        n, missing = organize(outdir, code, args.copy, args.relink, print)
        gap = f"  (아직 없음 {len(missing)}종)" if missing else ""
        print(f"  {code:12} 산출물 {n}개 정리{gap}")

    # 배치 전체 안내
    idx = ["# 이 배치의 구조", "",
           "품번 폴더마다 `_산출물/` 이 있고, 그 안에 단계별로 정리돼 있다.",
           "각 폴더의 `_산출물/안내.md` 가 파일 하나하나를 설명한다.", "",
           "| 폴더 | 단계 | 무엇이 들어 있나 |", "|---|---|---|",
           "| `0_기록` | — | 작업로그·상태·자체검사 |",
           "| `1_클린` | 섹션1 ⓪ | 클린본, **원본 전체 전사** |",
           "| `2_리뷰` | 섹션2 ①②③ | 줄거리, 전사, plan, 컷 완료 영상 |",
           "| `3_자막` | 섹션2 ③ | 대사·내레이션 SRT/JSON |",
           "| `4_음성배너` | 섹션3 ②③ | 내레이션 wav, 인포배너 PNG |",
           "| `5_납품` | 섹션3 ④ | 최종 납품본 |", "",
           "## 배치 공용 폴더", "",
           "| 폴더 | 뜻 |", "|---|---|",
           "| `_infocard_{품번}` | 배너 원본(생성 중간물 포함) |",
           "| `_완성` | 검사 통과분 수거 |",
           "| `_검수필요` | 자체 검사에서 결함이 잡힌 것 |",
           "| `_제외` | 자동화 부적합으로 뺀 것 |", "",
           "알고리즘 설명은 저장소의 `docs/파이프라인_알고리즘.md` 참고.", ""]
    (outdir / "00_안내.md").write_text("\n".join(idx), encoding="utf-8")
    print(f"\n✔ 배치 안내: {outdir / '00_안내.md'}")

    # ── 순위 뷰 ──────────────────────────────────────────────────────────────
    # 품번 폴더만 보면 뭐가 1위인지 알 수 없다(사용자 지적). 폴더명을 바꾸면 코드가
    # 품번으로 경로를 찾으므로 못 바꾼다 — 대신 하드링크로 '순위 이름' 뷰를 따로 만든다.
    if not args.src:
        return 0
    try:
        from _ranklist import load_rank, find_rank_file, match_videos, load_details
        rf = find_rank_file(args.src)
        pairs, _m, _e = match_videos(load_rank(rf), args.src)
        det = load_details(rf)
    except Exception as e:
        print(f"※ 랭킹을 못 읽어 _순위/ 생략({e})")
        return 0

    rankdir = outdir / "_순위"
    if rankdir.is_dir():
        shutil.rmtree(rankdir, ignore_errors=True)
    rankdir.mkdir(parents=True, exist_ok=True)
    lines = ["# 순위별 보기", "",
             "품번 폴더는 그대로 두고 하드링크로 만든 뷰다(디스크 0바이트).",
             "재생 순서는 **꼴찌 → 1위** 카운트다운이다.", "",
             "| 재생 | 순위 | 품번 | 배우 | 👍/👎 |", "|---|---|---|---|---|"]
    for play, (rank, code, _v) in enumerate(sorted(pairs, key=lambda x: -x[0]), 1):
        src_dir = outdir / code
        if not src_dir.is_dir():
            continue
        d = det.get(code.upper()) or {}
        act = d.get("actress", "")
        name = f"{rank:02d}위_{code}" + (f"_{act}" if act else "")
        dst = rankdir / name
        n = 0
        for f in sorted(src_dir.iterdir()):
            if f.is_file() and not f.name.endswith((".part", ".hold")):
                if link_or_copy(f, dst / f.name, args.copy, print) != "건너뜀":
                    n += 1
        lk, dk = d.get("likes"), d.get("dislikes")
        pop = f"{lk}/{dk}" if lk is not None else "—"
        lines.append(f"| {play}번째 | **{rank}위** | `{code}` | {act} | {pop} |")
        print(f"  _순위/{name}  ({n}개)")
    (rankdir / "00_순위.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    top = sorted(pairs)[0][1] if pairs else "?"
    print(f"\n✔ 순위 뷰: {rankdir}   (1위 = {top})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
