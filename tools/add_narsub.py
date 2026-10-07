# -*- coding: utf-8 -*-
r"""납품본에 **내레이션 자막만** 얹는다 — 다시 굽지 않는다.

왜 필요한가: 섹션3 ④(`stage_burn`)는 `hide_narration` 으로 내레이션 SRT 를 숨기고 굽는다.
편별 납품 규격이 "해설은 음성으로만" 이었기 때문이다. 노란 판 자막을 넣으려면 원래는
12편을 통째로 다시 구워야 하는데, 그러면 리프레임·demucs·NudeNet 전수검사까지 같이 돌아
한 시간이 넘는다. 이 도구는 완성된 `_납품/{품번}.mp4` 위에 내레이션 레이어만 3초에 올린다.

★2026-09-08 사용자 지적 3건을 여기서 함께 해결한다:

  ① **자막이 음성보다 먼저 뜨고 끝나도 남아 있다**
     `{품번}_내레이션.srt` 의 길이는 `narration_slots`가 잡은 **슬롯**(15초당 1줄)이지
     실제 발화 길이가 아니다. → `{품번}_tts/n###.wav` 를 ffprobe 로 재서 **실측 길이**로 다시 잡는다.

  ② **맨 앞 배너 구간에는 자막이 안 나와야 한다**
     `banner_hold`(기본 5초) 안에서 시작하는 줄은 통째로 뺀다(음성은 그대로 나간다).

  ③ **한 줄이 너무 오래 떠 있고 밋밋하다**
     한 줄을 **어절 묶음**으로 쪼개 발화 길이에 비례해 순차로 띄우고, 묶음마다
     `typein`(좌→우로 쓸려 나타남 = 타자 치는 느낌)을 건다. ASS `\clip` 애니라 렌더 안전.

사용:
  .venv\Scripts\python.exe tools\add_narsub.py --out F:\ja_reviewer_out\ja21_v2
  ... --alpha 0x80        # 노란 판 불투명 50%(기본)
  ... --chars 11          # 한 묶음 최대 글자수(기본 11)
  ... --anim typein       # typein|pop|punch|drop|smash|none
  ... --banner 5          # 배너 구간(초). 생략 시 config banner_hold
  ... --code ABF-382      # 한 편만
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import pipeline as P
from server.core.common import s2srt


def wav_dur(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


FIT_CHARS = 26   # 58px 한 줄이 액자 안(≈1840px)에 들어가는 안전 글자수


def split_long(text, limit=FIT_CHARS):
    """통짜 모드에서도 **화면 폭을 넘는 줄만** 어절 경계로 균등하게 나눈다.

    ja22 실측: 44~48자 줄이 58px 노란 판 한 줄로 뜨면서 오른쪽이 액자 밖으로 잘렸다
    ("…복수 얘기로 바"). 짧은 줄은 예전처럼 한 번에 띄운다(조각이 빨리 사라지는 문제 회피)."""
    if len(text) <= limit:
        return [text]
    words = text.split()
    n = -(-len(text) // limit)                     # 필요한 조각 수
    target = len(text) / n
    parts, buf = [], ""
    for w in words:
        cand = f"{buf} {w}".strip()
        if buf and len(cand) > target and len(parts) < n - 1:
            parts.append(buf)
            buf = w
        else:
            buf = cand
    if buf:
        parts.append(buf)
    return parts


def group_words(text, max_chars):
    """어절 단위로 묶는다 — 글자를 쪼개면 조사가 잘려 읽기 어렵다."""
    out, buf = [], ""
    for w in str(text).split():
        cand = (buf + " " + w).strip()
        if buf and len(cand) > max_chars:
            out.append(buf)
            buf = w
        else:
            buf = cand
    if buf:
        out.append(buf)
    return out or [str(text)]



def _bgr(hexstr):
    """#RRGGBB → ASS 인라인 색 &HBBGGRR&."""
    s = str(hexstr).lstrip("#")
    if len(s) != 6:
        s = "111111"
    return "&H%s%s%s&" % (s[4:6], s[2:4], s[0:2])


def karaoke(text, dur, base, hi, mode="fill", lead=0.09, hold=0.10):
    """어절 단위로 색이 바뀌는 ASS 태그를 입힌다.

    ★2026-09-08 사용자: "빨간색 안 돼, 너무 정신없어".
      원인은 색상(빨강)만이 아니라 **방식**이었다. 어절마다 색이 들어왔다 빠지면
      한 줄에서 색 전환이 어절 수 x 2번 일어나 화면이 깜빡인다.

    mode="fill"(기본) : 아직 안 읽은 어절은 흐리게, 읽은 어절은 또렷하게 — 전환은
                        어절당 **한 번**이고 되돌아오지 않는다. 읽는 위치만 따라간다.
    mode="pulse"      : 예전 방식(들어왔다 빠짐). 강조색을 쓰고 싶을 때만.
    """
    words = str(text).split()
    if not words or dur <= 0:
        return str(text)
    span = sum(len(w) for w in words) or 1
    b, h = _bgr(base), _bgr(hi)
    out, t0 = [], 0.0
    for w in words:
        d = dur * len(w) / span
        a = int(t0 * 1000)
        c = int(min(t0 + lead, t0 + d) * 1000)
        if mode == "pulse":
            e = int(min(t0 + lead + hold, t0 + d) * 1000)
            f = int((t0 + d) * 1000)
            out.append("{\\c%s\\t(%d,%d,\\c%s)\\t(%d,%d,\\c%s)}%s" % (b, a, c, h, e, f, b, w))
        else:
            out.append("{\\c%s\\t(%d,%d,\\c%s)}%s" % (b, a, c, h, w))
        t0 += d
    return " ".join(out)


def retimed(out, code, banner_end, max_chars, tail=0.7, kara=None, log=print):
    """실측 발화 길이 기준으로 다시 잡은 (start, end, text) 목록."""
    nsrt = out / code / f"{code}_내레이션.srt"
    rows = P.srt_parse(str(nsrt))
    tts = out / code / f"{code}_tts"
    evs, dropped, shrunk = [], 0, 0
    for i, (s, e, text) in enumerate(rows, 1):
        w = tts / f"n{i:03d}.wav"
        real = wav_dur(w) if w.is_file() else 0.0
        dur = real if real > 0.2 else (e - s)
        if real > 0.2 and (e - s) - real > 0.4:
            shrunk += 1
        if s < banner_end:                    # ② 배너 구간엔 자막을 띄우지 않는다
            dropped += 1
            continue
        # ★말이 끝나자마자 자막이 사라지면 읽을 틈이 없다(2026-09-08 사용자).
        #   여운을 붙이되 **다음 줄 시작 0.15초 전**까지만 — 두 줄이 겹치면 안 된다.
        nxt = rows[i][0] if i < len(rows) else None
        room = (nxt - 0.15 - (s + dur)) if nxt is not None else tail
        this_tail = max(0.0, min(tail, room))
        if max_chars <= 0:                # 통짜 — 줄을 쪼개지 않는다(조각이 너무 빨리 사라짐)
            chunks = split_long(str(text), FIT_CHARS)
        else:
            chunks = group_words(text, max_chars)
        span = sum(len(c) for c in chunks) or 1
        t = s
        for j, c in enumerate(chunks):
            d = dur * len(c) / span
            end = t + d + (this_tail if j == len(chunks) - 1 else 0.0)
            body = karaoke(c, d, kara[0], kara[1], kara[2]) if kara else c
            evs.append((round(t, 3), round(end, 3), body))
            t += d
    log(f"   줄 {len(rows)} → 조각 {len(evs)}"
        f"{f', 배너구간 {dropped}줄 제외' if dropped else ''}"
        f"{f', {shrunk}줄 길이 축소' if shrunk else ''}")
    return evs


# ─── 짤·효과음 (2026-10-07 사용자 확정 규칙) ────────────────────────────────
# · 효과음: 해설 **줄마다** 그 줄의 역할(sfx 태그)에 맞는 소리를 발화 직전(-0.05s)에 깐다.
#   소리는 assets/sfx/ (사용자 효과음 모음에서 역할별로 골라 음량을 맞춘 것). 같은 역할이
#   반복되면 돌려 쓴다. 음량은 해설 아래(피크 기준 원본 ±1dB) — 시안 v2에서 확인한 값.
# · 짤: cutin 태그가 붙은 줄에만, 발화 0.2초 뒤 **2.5초**, 좌하단(해설 자막판 위) 폭 22%,
#   0.15초 페이드 + 0.25초 슬라이드업. 첫 5.6초(인포카드)·마지막 3초엔 안 띄운다.
#   편당 최대 2개(1~3위는 3개). 짤은 assets/gifs/{태그}/ 공용 라이브러리.
ROLE_FILES = {"호명": ["call_rank"], "정보": ["info", "info2"], "반전": ["twist", "shock"],
              "긴장": ["sweat", "sweat2"], "드립": ["pop", "pop2", "pop3", "boing"],
              "마무리": ["finale"]}
CUTIN_POS, CUTIN_SCALE, CUTIN_DUR, CUTIN_DELAY, CUTIN_TAIL_GUARD = "bl", 0.22, 2.5, 0.2, 3.0


def fx_plan(out, code, banner_end, vid_dur, want_cutins=True, want_sfx=True, log=print):
    """내레이션 json 의 sfx/cutin 태그 + srt 실측 시각 → (cutins[(s,e,path)], sfx_events[(t,name)])."""
    import json as _json
    from server.core import assets as A
    jf = out / code / f"{code}_내레이션.json"
    sf = out / code / f"{code}_내레이션.srt"
    if not (jf.is_file() and sf.is_file()):
        return [], []
    try:
        nar = _json.loads(jf.read_text(encoding="utf-8"))
    except Exception:
        return [], []
    body = [n for n in nar if n.get("style") != "드립"]
    rows = P.srt_parse(str(sf))
    if not body or not any(n.get("sfx") or n.get("cutin") for n in body):
        return [], []                                   # 옛 대본(태그 없음) — 아무것도 안 한다
    if len(body) != len(rows):
        log(f"   ※ 내레이션 json {len(body)}줄 ≠ srt {len(rows)}줄 — 짤·효과음 생략")
        return [], []
    head = str(body[0].get("text", ""))
    max_cut = 3 if re.match(r"^\s*[123]\s*위", head) else 2
    cuts, events, used, cnt = [], [], {}, {}
    last = len(body) - 1
    for i, (n, (s, _e, _t)) in enumerate(zip(body, rows)):
        role = str(n.get("sfx") or "").strip()
        if want_sfx and role in ROLE_FILES:
            k = cnt.get(role, 0); cnt[role] = k + 1
            names = ROLE_FILES[role]
            events.append((round(max(0.0, s - 0.05), 3), names[k % len(names)]))
        tag = str(n.get("cutin") or "").strip()
        if (want_cutins and tag in A.TAGS and 0 < i < last and len(cuts) < max_cut
                and s >= banner_end + 0.5 and s + CUTIN_DELAY + CUTIN_DUR <= vid_dur - CUTIN_TAIL_GUARD):
            j = used.get(tag, 0)
            p = A.pick_gif(out, tag, j)
            if p:
                used[tag] = j + 1
                cuts.append((round(s + CUTIN_DELAY, 3), round(s + CUTIN_DELAY + CUTIN_DUR, 3), p))
            else:
                log(f"   ※ 짤 없음({tag}) — assets/gifs/{tag}/ 에 넣으면 들어간다")
    if cuts or events:
        log(f"   짤 {len(cuts)}개 · 효과음 {len(events)}개 "
            f"({', '.join(f'{k}×{v}' for k, v in cnt.items())})")
    return cuts, events


def write_srt(evs, path):
    out = []
    for n, (s, e, t) in enumerate(evs, 1):
        out += [str(n), f"{s2srt(s)} --> {s2srt(e)}", t, ""]
    path.write_text("\n".join(out), encoding="utf-8-sig")
    return path


def main():
    ap = argparse.ArgumentParser(description="납품본에 내레이션 자막만 얹기")
    ap.add_argument("--out", help="out_dir. 생략 시 config out_dir")
    ap.add_argument("--code", action="append", default=[], help="특정 품번만(여러 번 가능)")
    # ★2026-10-07 짤·효과음 — story 대본의 줄별 sfx/cutin 태그를 여기서 화면·소리로 바꾼다.
    #   (태그가 없는 옛 대본이면 아무것도 안 한다)
    ap.add_argument("--no-cutins", action="store_true", help="반응 짤 오버레이 끄기")
    ap.add_argument("--no-sfx", action="store_true", help="해설 효과음 끄기")
    ap.add_argument("--src-dir", default="_납품", help="입력 폴더(기본 _납품)")
    ap.add_argument("--dst-dir", default="_납품_자막", help="출력 폴더(기본 _납품_자막)")
    ap.add_argument("--alpha", default="0x80",
                    help="노란 판 투명도. 0=완전 불투명, 255=완전 투명. 기본 0x80(불투명 50%%)")
    ap.add_argument("--chars", type=int, default=0,
                    help="한 묶음 최대 글자수. 0이면 줄을 쪼개지 않고 통짜로 띄운다(기본 0)")
    ap.add_argument("--karaoke", action="store_true", default=True,
                    help="어절마다 강조색이 들어왔다 빠진다(기본 켬)")
    ap.add_argument("--no-karaoke", dest="karaoke", action="store_false")
    ap.add_argument("--base", default="#7A7264",
                    help="아직 안 읽은 어절 색(기본 #7A7264 흐린 먹색)")
    ap.add_argument("--hi", default="#111111",
                    help="읽은 어절 색(기본 #111111 진한 먹색). 빨강 같은 강조색은 산만하다")
    ap.add_argument("--tail", type=float, default=0.7,
                    help="말이 끝난 뒤 자막을 더 붙잡는 시간(초, 기본 0.7). "
                         "다음 줄 시작 0.15초 전까지만 늘어난다")
    ap.add_argument("--kara-mode", default="fill", choices=["fill", "pulse"],
                    help="fill=읽은 어절이 또렷해지고 유지(기본) / pulse=들어왔다 빠짐")
    ap.add_argument("--size", type=int, default=58,
                    help="내레이션 글씨 크기(1080p 기준, 기본 58 — 기존 69에서 축소)")
    ap.add_argument("--anim", default="typein",
                    help="조각 등장 효과 typein|pop|punch|drop|smash|none (기본 typein)")
    ap.add_argument("--banner", type=float, default=None,
                    help="배너 구간(초). 이 안에서 시작하는 줄은 자막을 뺀다. 생략 시 config banner_hold")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    out = Path(args.out or cfg["out_dir"])
    src_dir, dst_dir = out / args.src_dir, out / args.dst_dir
    dst_dir.mkdir(parents=True, exist_ok=True)
    alpha = int(args.alpha, 0)
    banner_end = args.banner if args.banner is not None else float(cfg.get("banner_hold", 5.0))

    # 720p 캔버스 기준 스타일 → 1080p 납품본용 1.5배 (굽기와 같은 규칙)
    styles = P.scale_styles(cfg.get("sub_styles") or P.STYLE_DEFAULT,
                            override={**P.STYLE_1080_OVERRIDE,
                                      **(cfg.get("sub_styles_1080") or {})})
    nar = dict(styles.get("narration") or {})
    nar["plate_alpha"] = alpha
    nar["anim"] = args.anim
    nar["size"] = args.size
    styles["narration"] = nar
    print(f"판 투명도 alpha={alpha:#04x}(불투명 {round((255 - alpha) / 255 * 100)}%) · "
          f"효과 {args.anim} · 크기 {args.size} · "
          f"{'통짜' if args.chars <= 0 else str(args.chars) + '자 묶음'} · "
          f"노래방 {args.kara_mode + ' ' + args.base + '→' + args.hi if args.karaoke else '끔'} · "
          f"여운 {args.tail:g}초 · 배너 {banner_end:g}초 구간 제외")

    codes = args.code or sorted(p.stem for p in src_dir.glob("*.mp4"))
    if not codes:
        print(f"[X] {src_dir} 에 mp4 가 없습니다")
        return 1

    ok, skip = 0, 0
    for i, code in enumerate(codes, 1):
        src = src_dir / f"{code}.mp4"
        dst = dst_dir / f"{code}.mp4"
        print(f"\n({i}/{len(codes)}) {code}", flush=True)
        if not src.is_file() or not (out / code / f"{code}_내레이션.srt").is_file():
            print("  [!] 원본 또는 내레이션 SRT 없음 — 건너뜀")
            skip += 1
            continue
        evs = retimed(out, code, banner_end, args.chars, tail=args.tail,
                      kara=(args.base, args.hi, args.kara_mode) if args.karaoke else None,
                      log=print)
        if not evs:
            print("  [!] 띄울 조각이 없습니다 — 건너뜀")
            skip += 1
            continue
        tmp = write_srt(evs, out / code / f"{code}_내레이션_화면.srt")
        cuts, sfx_events = fx_plan(out, code, banner_end, P.video_duration(str(src)) or 0.0,
                                   want_cutins=not args.no_cutins, want_sfx=not args.no_sfx)
        try:
            P.burn_subs(str(src), None, str(tmp), str(dst), styles,
                        log=lambda m: None,
                        cutins=cuts or None, cutin_pos=CUTIN_POS, cutin_scale=CUTIN_SCALE)
            if sfx_events:
                from server.core import sfx as SFX
                mixed = dst.with_name(f"{dst.stem}_sfx.mp4")
                if SFX.mix_events(str(dst), sfx_events, str(mixed), str(out), log=print, gain=1.0):
                    mixed.replace(dst)
            print(f"  [OK] {dst.name}")
            ok += 1
        except Exception as e:
            print(f"  [X] 실패: {type(e).__name__}: {e}")
            skip += 1
        finally:
            ass = dst.with_suffix(".ass")
            if ass.is_file():
                ass.unlink()

    print(f"\n완료 {ok}/{len(codes)}" + (f", 건너뜀 {skip}" if skip else ""))
    print(f"결과: {dst_dir}")
    return 0 if skip == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
