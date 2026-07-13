#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""⑥ 자막 굽기(ASS 하드섭) + 인포배너 오버레이."""
import json
import subprocess
from pathlib import Path

from .common import FFMPEG_TIMEOUT, _part_path, _finalize, srt_parse, video_duration, video_wh
from .cutter import has_nvenc, _vcodec_args

def _ass_color(hexstr, alpha="00"):
    h = str(hexstr or "").lstrip("#")
    if len(h) != 6:
        return f"&H{alpha}FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()


def _ass_time(t):
    t = max(0.0, t); h = int(t // 3600); m = int(t % 3600 // 60); s = int(t % 60); cs = int(round((t - int(t)) * 100))
    if cs >= 100:
        s += 1; cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


_ALIGN = {("bottom", "left"): 1, ("bottom", "center"): 2, ("bottom", "right"): 3,
          ("middle", "left"): 4, ("middle", "center"): 5, ("middle", "right"): 6,
          ("top", "left"): 7, ("top", "center"): 8, ("top", "right"): 9}

# 기본 스타일 — 대사(하단 흰), 내레이션=기본(대사 바로 위 노랑), 강조/정보(현재 미사용)
# anim: 등장 효과 — none | fade | pop | punch | slide  (_ass_anim 참고)
#
# ★내레이션 위치 (2026-07-13 사용자 피드백): 예전엔 화면 '상단'이라 대사(하단)와 멀리
#   떨어져 시선이 위아래로 튀었다 → **대사 바로 위**(하단 정렬, 대사 한 줄 높이만큼 띄움).
#   대사 margin 46 + 대사 2줄 높이(42*2.2≈92) = 138 → 대사가 2줄로 늘어도 안 겹친다.
STYLE_DEFAULT = {
    "dialogue":  {"font": "Malgun Gothic", "size": 42, "color": "#FFFFFF", "outline_color": "#000000",
                  "outline": 2.2, "shadow": 0.4, "bold": True, "v": "bottom", "h": "center", "margin": 46,
                  "anim": "none"},
    "dialogue_m": {"font": "Malgun Gothic", "size": 42, "color": "#7FD0FF", "outline_color": "#000000",
                   "outline": 2.2, "shadow": 0.4, "bold": True, "v": "bottom", "h": "center", "margin": 46,
                   "anim": "none"},
    "narration": {"font": "Malgun Gothic", "size": 38, "color": "#FFD400", "outline_color": "#000000",
                  "outline": 2.2, "shadow": 0.4, "bold": True, "v": "bottom", "h": "center", "margin": 138,
                  "anim": "pop"},
    "emphasis":  {"font": "Malgun Gothic", "size": 52, "color": "#FF3B3B", "outline_color": "#000000",
                  "outline": 2.8, "shadow": 0.6, "bold": True, "v": "middle", "h": "center", "margin": 60,
                  "anim": "punch"},
    "info":      {"font": "Malgun Gothic", "size": 32, "color": "#8FE3FF", "outline_color": "#00243A",
                  "outline": 2.0, "shadow": 0.3, "bold": True, "v": "top", "h": "right", "margin": 30,
                  "anim": "slide"},
}

# LLM이 붙이는 내레이션 유형 → ASS 스타일명
STYLE_TAGNAME = {"기본": "Narration", "일반": "Narration", "강조": "Emphasis", "정보": "Info",
                 "normal": "Narration", "emphasis": "Emphasis", "info": "Info"}
# 대사 화자 → ASS 스타일명 (여=기본 Dialogue, 남=DialogueM)
SPEAKER_TAGNAME = {"여": "Dialogue", "여자": "Dialogue", "f": "Dialogue", "female": "Dialogue",
                   "남": "DialogueM", "남자": "DialogueM", "m": "DialogueM", "male": "DialogueM"}


def _style_line(name, st):
    st = {**STYLE_DEFAULT["dialogue"], **(st or {})}
    align = _ALIGN.get((st.get("v", "bottom"), st.get("h", "center")), 2)
    return (f"Style: {name},{st.get('font','Malgun Gothic')},{int(st.get('size',40))},"
            f"{_ass_color(st.get('color','#FFFFFF'))},&H000000FF,"
            f"{_ass_color(st.get('outline_color','#000000'))},&H64000000,"
            f"{-1 if st.get('bold') else 0},0,0,0,100,100,0,0,1,"
            f"{st.get('outline',2)},{st.get('shadow',0)},{align},40,40,{int(st.get('margin',40))},1")


def _ass_anim(kind, dur_ms):
    """자막 등장 효과 → ASS 인라인 오버라이드 태그. \\t(t1,t2,...)의 시각은 이벤트 시작 기준(ms).
    none  : 없음(그냥 뜸)
    pop   : 작게 나타나 살짝 커졌다가(오버슈트) 제자리 — '휙' 튀어나오는 느낌
    punch : pop보다 강하게. 강조 문구용
    fade  : 부드럽게 페이드
    slide : 아래에서 살짝 밀려 올라옴
    """
    if kind == "pop":
        return r"{\fad(0,120)\fscx70\fscy70\t(0,110,\fscx108\fscy108)\t(110,190,\fscx100\fscy100)}"
    if kind == "punch":
        return (r"{\fad(0,140)\fscx40\fscy40\t(0,90,\fscx118\fscy118)"
                r"\t(90,170,\fscx94\fscy94)\t(170,240,\fscx100\fscy100)}")
    if kind == "fade":
        return r"{\fad(180,180)}"
    if kind == "slide":
        # 아래에서 위로 24px — \move는 절대좌표라 여기선 원점 이동(\org) 대신 fad+scaleY로 대체
        return r"{\fad(0,120)\fscy60\t(0,150,\fscy105)\t(150,230,\fscy100)}"
    return ""


def build_ass(dialogue, narration, out_ass, width, height, styles=None):
    styles = styles or {}
    S = {k: {**STYLE_DEFAULT[k], **(styles.get(k) or {})}
         for k in ("dialogue", "dialogue_m", "narration", "emphasis", "info")}
    # 스타일 태그명 → 애니 종류
    ANIM = {"Dialogue": S["dialogue"].get("anim", "none"),
            "DialogueM": S["dialogue_m"].get("anim", "none"),
            "Narration": S["narration"].get("anim", "none"),
            "Emphasis": S["emphasis"].get("anim", "none"),
            "Info": S["info"].get("anim", "none")}
    L = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
         "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
         "[V4+ Styles]",
         "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
         "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
         "Alignment, MarginL, MarginR, MarginV, Encoding",
         _style_line("Dialogue", S["dialogue"]),
         _style_line("DialogueM", S["dialogue_m"]),
         _style_line("Narration", S["narration"]),
         _style_line("Emphasis", S["emphasis"]),
         _style_line("Info", S["info"]),
         "", "[Events]",
         "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    evs = []
    for it in dialogue:                        # (s,e,text) 또는 (s,e,text,speaker)
        spk = it[3] if len(it) > 3 else "여"
        evs.append((it[0], it[1], SPEAKER_TAGNAME.get(spk, "Dialogue"), it[2]))
    for it in narration:                       # (s,e,text) 또는 (s,e,text,style)
        tag = it[3] if len(it) > 3 else "기본"
        evs.append((it[0], it[1], STYLE_TAGNAME.get(tag, "Narration"), it[2]))
    evs.sort(key=lambda x: x[0])
    for s, e, style, t in evs:
        txt = str(t).replace("\n", "\\N")
        tag = _ass_anim(ANIM.get(style, "none"), int((e - s) * 1000))
        L.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},{style},,0,0,0,,{tag}{txt}")
    Path(out_ass).write_text("\n".join(L) + "\n", encoding="utf-8")
    return out_ass


"""배너 모션 기본값 — 브라우저 미리보기(/preview/data)와 반드시 같은 값을 쓴다."""
BANNER_ANIM = {"hold": 4.0, "fade": 0.5, "blur": 16, "wm_start": 4.1, "wm_slide": 40}


def _prep_banner_layers(banner, workdir, blur=16, vid_wh=None):
    """레이어 PNG를 굽기용으로 준비 — 내용 크기로 crop(오버레이 비용↓) + 인포카드 블러본 1회 생성.
    ffmpeg에서 gblur을 매 프레임 돌리면 수십 배 느려지므로 블러는 여기서 미리 굽는다.
    vid_wh=(w,h)를 주면 배너 캔버스(1920×1080)를 영상 해상도에 맞춰 스케일한다
    — 720p 영상에 1080p 좌표로 얹으면 인포카드가 화면 밖으로 잘리는 버그 방지.
    반환: {키: (경로, x, y)}  x,y = 영상 좌표계 위치"""
    try:
        from PIL import Image, ImageFilter
    except Exception:
        return {}
    out = {}
    pad = int(blur * 2.5)      # 블러가 가장자리에서 잘리지 않도록 여유
    for k in ("frame", "info", "wm"):
        p = banner.get(k)
        if not p or not Path(p).is_file():
            continue
        im = Image.open(p).convert("RGBA")
        s = 1.0
        if vid_wh and im.width and im.height:
            s = min(vid_wh[0] / im.width, vid_wh[1] / im.height)
        bb = im.getbbox()
        if not bb:
            continue
        if k == "info":        # 블러 번짐 여유를 두고 자른다
            bb = (max(0, bb[0] - pad), max(0, bb[1] - pad),
                  min(im.width, bb[2] + pad), min(im.height, bb[3] + pad))
        crop = im.crop(bb)
        if abs(s - 1.0) > 1e-3:
            crop = crop.resize((max(1, round(crop.width * s)),
                                max(1, round(crop.height * s))), Image.LANCZOS)
        cp = Path(workdir) / f"_bn_{k}.png"
        crop.save(cp)
        out[k] = (cp.name, round(bb[0] * s), round(bb[1] * s))
        if k == "info":
            bp = Path(workdir) / "_bn_info_blur.png"
            crop.filter(ImageFilter.GaussianBlur(round(blur * s) or 1)).save(bp)
            out["info_blur"] = (bp.name, round(bb[0] * s), round(bb[1] * s))
    return out


def _banner_filter(prep, anim):
    """배너 오버레이 filter_complex 조각. 미리보기와 동일 타이밍.
    인포카드: 페이드인(흐림→선명) → hold → 페이드아웃(선명→흐림)  워터마크: 페이드인+슬라이드.
    · blend=all_expr(픽셀별 수식)은 매우 느려서, 미리 구운 블러본과의 크로스디졸브로 대체
    · 애니메이션이 끝난 뒤엔 enable로 오버레이 자체를 꺼서 남은 구간 비용을 없앤다"""
    hold, fade = float(anim.get("hold", 2.0)), float(anim.get("fade", 0.5))
    wm_st, slide = float(anim.get("wm_start", 2.1)), float(anim.get("wm_slide", 40))
    end = hold + fade + 0.1
    inputs, fc, idx, last = [], "", 1, "0:v"
    order = [k for k in ("frame", "info_blur", "info", "wm") if k in prep]
    labels = {}
    for k in order:
        name, x, y = prep[k]
        inputs += ["-loop", "1", "-i", name]
        labels[k] = (idx, x, y)
        idx += 1
    # 레이어별 알파 애니메이션
    if "info" in labels:
        fc += (f"[{labels['info'][0]}]fade=t=in:st=0:d=0.4:alpha=1,"
               f"fade=t=out:st={hold}:d={fade}:alpha=1[ic];")
    if "info_blur" in labels:   # 등장 초반·퇴장 후반에만 겹쳐 흐림 효과를 만든다
        fc += (f"[{labels['info_blur'][0]}]fade=t=out:st=0:d={fade}:alpha=1,"
               f"fade=t=in:st={hold}:d={fade}:alpha=1,"
               f"fade=t=out:st={hold + fade}:d=0.1:alpha=1[icb];")
    if "wm" in labels:
        fc += f"[{labels['wm'][0]}]fade=t=in:st={wm_st}:d={fade}:alpha=1[wm];"
    # 합성 — 프레임 → 흐린 인포카드 → 선명 인포카드 → 워터마크
    n = 0
    if "frame" in labels:
        _, x, y = labels["frame"]
        n += 1
        fc += f"[{last}][{labels['frame'][0]}]overlay={x}:{y}[b{n}];"; last = f"b{n}"
    for key, lbl in (("info_blur", "icb"), ("info", "ic")):
        if key in labels:
            _, x, y = labels[key]
            n += 1
            fc += f"[{last}][{lbl}]overlay={x}:{y}:enable='lt(t,{end})'[b{n}];"; last = f"b{n}"
    if "wm" in labels:
        _, x, y = labels["wm"]
        n += 1
        sp = slide / max(fade, 0.01)     # 슬라이드 속도(px/s)
        fc += (f"[{last}][wm]overlay=x={x}:"
               f"y='{y}+min(0,-{slide}+(t-{wm_st})*{sp})'[b{n}];"); last = f"b{n}"
    return inputs, fc, last


def burn_subs(video, dialogue_srt, narration_srt, out_video, styles=None,
              narration_json=None, dialogue_json=None, log=print,
              banner=None, banner_anim=None, subs=True):
    """자막(+선택: 배너·워터마크)을 영상에 굽는다.
    banner={'frame':png,'info':png,'wm':png} 를 주면 자막과 같은 인코딩 1패스에서
    함께 합성한다(따로 굽는 2패스 대비 인코딩 1회 절약).
    banner에서 키를 빼면 그 레이어는 빠진다(미리보기 체크 그대로 굽기).
    subs=False면 자막 없이 배너만 굽는다."""
    w, h = video_wh(video)
    if dialogue_json and Path(dialogue_json).is_file():        # 화자(speaker) 포함 대사
        dd = json.loads(Path(dialogue_json).read_text(encoding="utf-8"))
        dlg = [(float(d["start"]), float(d["end"]), d["text"], d.get("speaker", "여")) for d in dd]
    else:
        dlg = srt_parse(dialogue_srt) if dialogue_srt and Path(dialogue_srt).is_file() else []
    if narration_json and Path(narration_json).is_file():     # 유형(style) 포함 내레이션
        data = json.loads(Path(narration_json).read_text(encoding="utf-8"))
        nar = [(float(d["start"]), float(d["end"]), d["text"], d.get("style", "기본")) for d in data]
    else:
        nar = srt_parse(narration_srt) if narration_srt and Path(narration_srt).is_file() else []
    if subs and not dlg and not nar:
        raise RuntimeError("입힐 자막(SRT)이 없습니다.")
    if not subs and not banner:
        raise RuntimeError("자막·배너 둘 다 꺼져 있어 구울 게 없습니다.")
    ass_path = Path(out_video).with_suffix(".ass")
    if subs:
        build_ass(dlg, nar, str(ass_path), w, h, styles)
        log(f"자막 굽기 (ffmpeg ass, {w}x{h}, 대사 {len(dlg)} · 내레이션 {len(nar)})...")
    else:
        ass_path.parent.mkdir(parents=True, exist_ok=True)
        log(f"자막 없이 배너만 굽기 ({w}x{h})...")

    inputs, fc, last = [], "", "0:v"
    if banner:
        prep = _prep_banner_layers(banner, ass_path.parent,
                                   (banner_anim or {}).get("blur", BANNER_ANIM["blur"]),
                                   vid_wh=(w, h))
        if prep:
            inputs, fc, last = _banner_filter(prep, banner_anim or BANNER_ANIM)
            log(f"배너 동시 굽기: {', '.join(prep)} (재인코딩 1패스 — 추가 비용 적음)")

    if fc:
        # 자막(ass)은 배너 오버레이 뒤에 얹는다 — 배너가 자막을 가리지 않게
        fc += (f"[{last}]ass={ass_path.name}[out]" if subs
               else f"[{last}]null[out]")

    # -loop 1 로 넣은 배너 PNG는 무한 스트림이라 원본이 끝나도 인코딩이 계속된다.
    # 원본 길이로 명시적으로 끊어준다.
    dur = video_duration(video) if fc else 0.0

    tmp_out = _part_path(out_video)   # 중간에 죽어도 잘린 파일이 '완료'로 안 보이게

    def _cmd(use_gpu):
        enc = _vcodec_args(use_gpu)
        if fc:
            tail = (["-t", f"{dur:.3f}"] if dur > 0 else ["-shortest"])
            return ["ffmpeg", "-y", "-i", str(video)] + inputs + \
                   ["-filter_complex", fc, "-map", "[out]", "-map", "0:a?"] + enc + \
                   ["-c:a", "copy"] + tail + [tmp_out]
        return ["ffmpeg", "-y", "-i", str(video), "-vf", f"ass={ass_path.name}"] + enc + \
               ["-c:a", "copy", tmp_out]

    gpu = has_nvenc()
    # libass 필터는 파일명만(작업폴더 cwd로) → 윈도우 드라이브 콜론 이스케이프 회피
    try:
        try:
            subprocess.run(_cmd(gpu), cwd=str(ass_path.parent), check=True, timeout=FFMPEG_TIMEOUT)
        except subprocess.CalledProcessError:
            if not gpu:
                raise
            log("NVENC 실패 → libx264로 폴백")
            subprocess.run(_cmd(False), cwd=str(ass_path.parent), check=True, timeout=FFMPEG_TIMEOUT)
        _finalize(tmp_out, out_video)
    finally:
        for tmp in ass_path.parent.glob("_bn_*.png"):   # 배너 중간 산출물 정리
            try:
                tmp.unlink()
            except OSError:
                pass
        try:                                            # 실패로 남은 .part 제거
            if Path(tmp_out).is_file() and str(tmp_out) != str(out_video):
                Path(tmp_out).unlink()
        except OSError:
            pass
    log(f"자막 영상: {out_video}")
    return out_video

