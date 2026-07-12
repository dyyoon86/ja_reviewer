#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""단계(스테이지) 코어 로직 — HTTP 엔드포인트(app.py)와 작업 큐(queue_mgr.py)가 공유.

각 stage_* 함수는 Emitter(em)로 진행상황을 흘리고 결과 dict를 반환한다.
실패는 예외로 던진다(호출측이 SSE 잡 오류/큐 아이템 오류로 변환).
"""
import json
import re
import shutil
import threading
from pathlib import Path

from . import pipeline as P


# ─── 품번별 상태/파일 헬퍼 ───────────────────────────────────────────────────
def _hms(x):
    x = int(max(0, round(x)))
    return f"{x // 3600:02d}:{x % 3600 // 60:02d}:{x % 60:02d}"


def _safe(code):
    return re.sub(r"[^0-9A-Za-z._-]", "_", (code or "").strip()) or "untitled"


def work_dir(c, code):
    """품번별 작업 폴더 {out_dir}/{품번}/ — 전사·컷·자막·plan·tts 전부 여기에 모음."""
    d = Path(c["out_dir"]) / _safe(code)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_file(outdir, code):
    return Path(outdir) / f"{code}_state.json"


def load_state(outdir, code):
    f = _state_file(outdir, code)
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"code": code, "video": None, "target": None, "llm": None, "model": None,
            "summary": "", "stars": None}


def save_state(outdir, code, **fields):
    st = load_state(outdir, code)
    st.update(fields)
    try:
        _state_file(outdir, code).write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return st


def steps_status(outdir, code):
    """단계 완료 여부는 '결과 파일 존재'로 판정 → 서버 재시작/수동삭제에도 견고."""
    o = Path(outdir)
    # 배너 레이어는 작업폴더가 아니라 {out_dir}/_infocard_{code}/ 에 모인다
    ic = o.parent / f"_infocard_{code}"
    return {"transcribe": (o / f"{code}_전사.json").is_file(),
            "ai": (o / f"{code}_plan.json").is_file(),
            "subs": (o / f"{code}_대사.srt").is_file(),
            "banner": (ic / f"{code}_워터마크.png").is_file(),
            "tts": (o / f"{code}_내레이션.wav").is_file(),
            "burn": (o / f"{code}_final_subbed.mp4").is_file()}


def write_narration(outdir, code, nar_rt):
    """내레이션 출력 — SRT + JSON. 내레이션은 존댓말 완결문장이라 25자 분할 안 함(끊김·시간겹침 방지).
    화면 줄바꿈은 굽기(ASS)에서 자동 처리. TTS도 완결문장이 자연스러움."""
    P.write_srt([(s, e, t) for s, e, t, *_ in nar_rt], outdir / f"{code}_내레이션.srt", maxlen=0)
    data = [{"start": round(s, 3), "end": round(e, 3), "text": t, "style": (x[0] if x else "기본")}
            for s, e, t, *x in nar_rt]
    (outdir / f"{code}_내레이션.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def write_dialogue(outdir, code, dlg_rt):
    """대사 출력 — SRT(텍스트) + JSON(화자 speaker 포함, 굽기용). 둘 다 25자 분할."""
    P.write_srt([(s, e, t) for s, e, t, *_ in dlg_rt], outdir / f"{code}_대사.srt")
    dsplit = P.split_entries(dlg_rt, 24)
    data = [{"start": round(s, 3), "end": round(e, 3), "text": t, "speaker": (x[0] if x else "여")}
            for s, e, t, *x in dsplit]
    (outdir / f"{code}_대사.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ─── 진행상황 방출 ───────────────────────────────────────────────────────────
class Emitter:
    """진행상황 콜백 묶음 — 기본은 무시. app.py는 SSE 잡큐로, queue_mgr는 큐 아이템으로 연결."""
    def log(self, msg): pass
    def step(self, n, total, label): pass
    def prog(self, frac, label=None): pass
    def file(self, tag, path): pass


def heartbeat(em, label):
    """오래 걸리는 블로킹 작업(LLM 호출 등) 중 살아있음을 N초마다 로그로 알림. stop.set()로 종료."""
    stop = threading.Event()

    def run():
        n = 0
        while not stop.wait(8):
            n += 8
            em.log(f"  …{label} 진행 중 ({n}s 경과)")
    threading.Thread(target=run, daemon=True).start()
    return stop


class NullLock:
    """gpu 세마포어 자리에 넣는 no-op — 엔드포인트 단독 실행(기존 동작)용."""
    def __enter__(self): return self
    def __exit__(self, *a): return False


# ─── 스테이지 코어 ───────────────────────────────────────────────────────────
def stage_transcribe(c, code, video, model, em, initial_prompt=None):
    """① 전사 — 영상 → 일본어 STT. {code}_전사.srt/.json 저장.
    two_pass(기본 on)면 작은 모델로 러프 스캔만 한다(구간 선정용) — 최종 대사자막은
    ② AI 처리에서 keep 구간만 정밀 재전사(transcribe_ranges)로 확보한다.
    2시간짜리 원본도 정밀 전사는 keep 합계(1~3분)에만 들어가 전체가 수 분에 끝난다."""
    outdir = work_dir(c, code)
    two_pass = bool(c.get("two_pass", True))
    if two_pass:
        scan_model = c.get("scan_model", "small")
        em.step(1, 1, f"전사 1차 스캔(faster-whisper {scan_model} — 러프, 구간선정용)")
        segs = P.transcribe_scan(video, scan_model, em.log, lambda fr: em.prog(fr, "스캔"),
                                 initial_prompt=initial_prompt)
    else:
        em.step(1, 1, f"전사(faster-whisper {model})")
        segs = P.transcribe(video, model, em.log, lambda fr: em.prog(fr, "전사"),
                            initial_prompt=initial_prompt)
    data = [{"start": round(s, 3), "end": round(e, 3), "text": t} for s, e, t in segs]
    (outdir / f"{code}_전사.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    P.write_srt([(s, e, t) for s, e, t in segs], outdir / f"{code}_전사.srt")
    save_state(outdir, code, video=str(video), model=model, scan=two_pass)
    em.file("전사 자막", outdir / f"{code}_전사.srt")
    return {"step": "transcribe", "code": code, "count": len(segs),
            "srt": str(outdir / f"{code}_전사.srt")}


def _guard_keep(keep, segs, log):
    """안전장치 — 대사(전사)가 한 줄도 없는 keep 구간은 제외한다.
    전사에서 신음은 환청필터로 걸러지므로, 대사 0줄 keep = 노출 장면 의심.
    원본을 트림 없이 풀오토로 던졌을 때 노출 구간이 최종본에 섞이는 걸 억제한다.
    전부 걸리면 판단 불가로 보고 원본 유지(안전망 — 결과 없음보단 검수)."""
    ok, dropped = [], []
    for a, b in keep:
        n = sum(1 for s, e, _t in segs if s < b and e > a)
        (ok if n else dropped).append((a, b))
    if dropped and ok:
        for a, b in dropped:
            log(f"⚠ keep {_hms(a)}~{_hms(b)}: 대사 0줄 — 노출 장면 의심, 자동 제외")
        log(f"안전장치: keep {len(keep)}→{len(ok)}구간 (제외 {len(dropped)})")
        return ok
    if dropped:
        log("※ 모든 keep에 대사가 없어 안전장치를 건너뜁니다(원본 유지) — 결과를 꼭 검수하세요")
    return keep


def _reduce_transcript(meta, segs, llm, em, limit=25000, block_sec=1200):
    """전사가 토큰 한도를 넘보면 map-reduce — 20분 블록별로 '줄거리+핵심 대사 후보'만 뽑아
    최종 선정 프롬프트 입력을 항상 작게 고정한다. 반환: (선정용 세그, 전체줄거리 hint 조각).
    짧은 전사는 그대로 통과(기존 원샷 동작 불변)."""
    total_chars = sum(len(t) for _, _, t in segs) + 16 * len(segs)   # 타임스탬프 오버헤드 포함
    if total_chars <= limit or len(segs) < 80:
        return segs, ""
    blocks, cur, t0 = [], [], segs[0][0]
    for s in segs:
        if s[0] - t0 >= block_sec and cur:
            blocks.append(cur); cur = []; t0 = s[0]
        cur.append(s)
    if cur:
        blocks.append(cur)
    em.log(f"전사가 김({total_chars:,}자, {len(segs)}줄) → {len(blocks)}블록 요약 후 최종 선정(map-reduce)")
    picked, summaries = [], []
    for bi, blk in enumerate(blocks, 1):
        try:
            r = P.call_llm(P.prompt_block(meta, blk, bi, len(blocks), blk[0][0], blk[-1][1]),
                           llm, em.log)
        except Exception as e:
            em.log(f"  블록 {bi} 요약 실패({type(e).__name__}) → 블록 앞 12줄을 후보로 대체")
            picked.extend(blk[:12])
            continue
        if r.get("summary"):
            summaries.append(f"{bi}. {r['summary']}")
        idx = set()
        for k in (r.get("picks") or []):
            try:
                idx.add(int(k))
            except (TypeError, ValueError):
                pass
        sel = [blk[k - 1] for k in sorted(idx) if 1 <= k <= len(blk)]
        picked.extend(sel)
        em.log(f"  블록 {bi}/{len(blocks)}: 후보 {len(sel)}줄")
    if not picked:   # 전 블록이 빈 후보면 선정 불능 → 원본으로 후퇴(느려도 결과는 낸다)
        em.log("※ map-reduce 후보가 0줄 — 전사 원본으로 진행합니다")
        return segs, ""
    picked.sort(key=lambda x: x[0])
    hint = ("(참고) 전체 줄거리 — 블록별 요약:\n" + "\n".join(summaries)) if summaries else ""
    return picked, hint


def stage_ai(c, code, video, target, llm, mode, hint, em, gpu=None, pos="mid", style="3min"):
    """② AI 처리 — 저장된 전사 + 메타 → LLM 압축·번역·내레이션. plan.json 저장 + 컷.
    gpu: 컷(NVENC) 구간을 감쌀 세마포어(큐 병렬 시) — None이면 잠금 없음(기존 단독 동작)."""
    gpu = gpu or NullLock()
    outdir = work_dir(c, code)
    tj = outdir / f"{code}_전사.json"
    if not tj.is_file():
        raise RuntimeError("전사 결과가 없습니다. 먼저 ① 전사를 실행하세요.")
    st = load_state(outdir, code)
    video = video or st.get("video")
    if not video or not Path(video).is_file():
        raise RuntimeError("전사에 쓴 영상 경로를 찾을 수 없습니다. ① 전사를 다시 실행하세요.")
    segs = [(d["start"], d["end"], d["text"])
            for d in json.loads(tj.read_text(encoding="utf-8"))]
    em.step(1, 3, "메타 조회")
    m = P.fetch_meta(c["meta_api"], code, em.log)
    label = "하이라이트형(알파컷식)" if mode == "highlight" else "요약형(짜집기)"
    em.step(2, 3, f"AI {label} 압축·번역·내레이션 ({llm} 추론, 보통 1~3분)")
    pf = P.prompt_highlight if mode == "highlight" else P.prompt_manual
    hb = heartbeat(em, f"AI 처리({llm})")
    try:
        plan_segs, story = _reduce_transcript(m, segs, llm, em,
                                              limit=int(c.get("map_reduce_chars", 25000)))
        full_hint = "\n".join(x for x in ((hint or "").strip(), story) if x)
        res = P.call_llm(pf(m, plan_segs, target, hint=full_hint, pos=pos, style=style),
                         llm, em.log)
    finally:
        hb.set()
    keep = P.parse_keep(res.get("keep", []), total=P.video_duration(video))
    if not keep:
        raise RuntimeError("LLM이 keep 구간을 못 골랐습니다(빈 응답 — 헤드리스 거부 가능. 아래 수동 모드 사용).")
    keep = _guard_keep(keep, segs, em.log)
    res["keep"] = [[a, b] for a, b in keep]   # 제외 반영된 keep을 plan에 저장(자막 재타이밍 일치)
    # 2-pass: ①이 러프 스캔이었다면 keep 구간만 정밀 재전사 → 대사자막을 정밀본으로 교체.
    # 실패해도 러프 기반 dialogue가 남아 있으니 결과는 항상 나온다(품질만 러프로 후퇴).
    if st.get("scan"):
        try:
            precise_model = st.get("model") or c.get("whisper_model", "large-v3")
            em.log(f"2차 정밀 전사 — keep {len(keep)}구간만 {precise_model}로 재전사")
            with gpu:
                fine = P.transcribe_ranges(video, keep, precise_model, em.log,
                                           lambda fr: em.prog(fr, "정밀 전사"),
                                           initial_prompt=P.build_initial_prompt(m))
            if fine:
                fix = P.call_llm(P.prompt_dialogue_fix(m, fine), llm, em.log)
                dlg = fix.get("dialogue") or []
                if dlg:
                    res["dialogue"] = dlg
                    em.log(f"대사자막을 정밀 전사본으로 교체: {len(dlg)}줄")
            else:
                em.log("※ 정밀 전사 결과 0줄 — 러프 대사자막 유지")
        except Exception as e:
            em.log(f"※ 정밀 재전사 실패({type(e).__name__}: {e}) — 러프 대사자막으로 진행")
    # ★ 컷을 먼저 하고 성공한 뒤에 plan.json을 쓴다.
    #   완료 판정이 plan.json 존재로 되므로, 컷 도중 죽으면 plan.json이 없어 재실행된다
    #   (반대 순서면 컷이 실패해도 'AI 완료'로 오판되어 final.mp4 없이 다음 단계로 넘어감).
    final = str(outdir / f"{code}_final.mp4")
    em.step(3, 3, "핵심 구간 컷")
    with gpu:
        P.cut_video(video, keep, final, em.log, lambda fr: em.prog(fr, "컷"))
    (outdir / f"{code}_plan.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    save_state(outdir, code, target=target, llm=llm,
               summary=res.get("summary", ""), stars=P.clamp_stars(res.get("stars")))
    em.file("AI 결과(plan)", outdir / f"{code}_plan.json")
    em.file("최종 영상", final)
    return {"step": "ai", "code": code, "final": final,
            "final_sec": P.video_duration(final),
            "summary": res.get("summary", ""), "stars": P.clamp_stars(res.get("stars"))}


def stage_subs(c, code, em):
    """③ 자막 — 저장된 plan.json → 한글 대사/내레이션 SRT(+JSON) 재타이밍 저장."""
    outdir = work_dir(c, code)
    plan = outdir / f"{code}_plan.json"
    if not plan.is_file():
        raise RuntimeError("AI 결과가 없습니다. 먼저 ② AI 처리를 실행하세요.")
    res = json.loads(plan.read_text(encoding="utf-8"))
    keep = P.parse_keep(res.get("keep", []))
    if not keep:
        raise RuntimeError("plan에 keep 구간이 없습니다.")
    em.step(1, 2, "대사 자막(한글) 생성")
    dlg = P.parse_lines(res.get("dialogue", []), ("ko", "text"),
                        extra=[("speaker", "여")], log=em.log)
    write_dialogue(outdir, code, P.retime(dlg, keep, snap=False))
    em.file("대사 자막", outdir / f"{code}_대사.srt")
    em.step(2, 2, "내레이션 자막 생성")
    nar = P.parse_lines(res.get("narration", []), ("text", "ko"),
                        extra=[("style", "기본")], log=em.log)
    outside = [n for n in nar if not any(a - 0.05 <= n[0] < b + 0.05 for a, b in keep)]
    if outside:
        em.log(f"※ 내레이션 {len(outside)}/{len(nar)}개가 컷(keep) 구간 밖에 있습니다 "
               f"— 컷 안으로 재배치합니다. 프롬프트 시간 규칙 위반이니 결과를 확인하세요.")
    write_narration(outdir, code, P.retime(nar, keep, snap=True, log=em.log))
    em.file("내레이션 자막", outdir / f"{code}_내레이션.srt")
    return {"step": "subs", "code": code,
            "srt_dialogue": str(outdir / f"{code}_대사.srt"),
            "srt_narration": str(outdir / f"{code}_내레이션.srt"),
            "summary": res.get("summary", ""), "stars": P.clamp_stars(res.get("stars"))}


def stage_tts(c, code, base, profile, language, seed, mux, em,
              orig_audio="duck", duck_level=0.3):
    """④ TTS — {code}_내레이션.srt → voicebox 합성 → {code}_내레이션.wav (+선택 mux)."""
    outdir = work_dir(c, code)
    srt = outdir / f"{code}_내레이션.srt"
    if not srt.is_file():
        raise RuntimeError(f"내레이션 SRT 없음: {srt} (먼저 리뷰 생성)")
    entries = P.srt_parse(srt)
    if not entries:
        raise RuntimeError("내레이션 항목이 없습니다.")
    clipdir = outdir / f"{code}_tts"; clipdir.mkdir(parents=True, exist_ok=True)
    clips = []
    total = len(entries) + 1 + (1 if mux else 0)
    for i, (st, en, text) in enumerate(entries, 1):
        em.step(i, total, f"음성 {i}/{len(entries)}: {text[:18]}")
        w = str(clipdir / f"n{i:03d}.wav")
        P.tts_generate(base, text, profile, language, w, seed, em.log)
        clips.append((st, w))
    wav = str(outdir / f"{code}_내레이션.wav")
    em.step(len(entries) + 1, total, "내레이션 트랙 합성")
    # 영상 길이를 알려주면 마지막 문장의 슬롯 계산과 '내레이션이 영상보다 김' 경고가 정확해진다
    fin = outdir / f"{code}_final.mp4"
    _, spans = P.build_narration_wav(
        clips, wav, em.log,
        video_sec=(P.video_duration(str(fin)) if fin.is_file() else None))
    em.file("내레이션 음성", wav)
    out = {"mode": "tts", "narration_wav": wav, "count": len(clips)}
    if mux:
        final = outdir / f"{code}_final.mp4"
        if final.is_file():
            voiced = str(outdir / f"{code}_final_voiced.mp4")
            em.step(total, total, "영상에 음성 입히기")
            P.mux_narration(str(final), wav, voiced, mode=orig_audio,
                            duck_level=float(duck_level), duck_spans=spans, log=em.log)
            em.file("음성 입힌 영상", voiced)
            out["voiced"] = voiced
        else:
            em.log(f"※ {final} 없음 → 믹스 생략(내레이션 WAV만 생성)")
    return out


def banner_layers(c, code, em=None, generate=True):
    """배너 오버레이 PNG 3장 경로. 없으면 즉석 생성(인코딩 없음, 수초). 실패 시 None."""
    icdir = Path(c["out_dir"]) / f"_infocard_{code}"
    names = {"frame": f"{code}_프레임.png", "info": f"{code}_인포카드.png",
             "wm": f"{code}_워터마크.png"}
    if not all((icdir / n).is_file() for n in names.values()):
        if not generate:
            return None
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import gen_infocard as GIC
            GIC.generate(code, outdir=str(icdir), assets_only=True, preview_anim=False,
                         log=(em.log if em else print))
        except Exception as e:
            if em:
                em.log(f"※ 배너 레이어 생성 실패({e}) → 자막만 굽습니다")
            return None
    out = {k: str(icdir / n) for k, n in names.items() if (icdir / n).is_file()}
    return out or None


def stage_banner(c, code, em, hold=2.0, preview=True):
    """④' 배너 — 품번 → DB 조회 → 투명 PNG 3장(프레임·인포카드·워터마크) + 미리보기 스틸.
    인코딩 없음(수초). ⑤ 굽기가 이 레이어를 그대로 합성한다."""
    import sys
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    import gen_infocard as GIC
    icdir = Path(c["out_dir"]) / f"_infocard_{code}"
    em.step(1, 1, "배너 레이어 생성(인코딩 없음)")
    try:
        r = GIC.generate(code, outdir=str(icdir), hold=hold, assets_only=True,
                         preview_anim=preview, log=em.log)
    except GIC.MetaNotFound as e:
        raise RuntimeError(f"배너 생성 불가 — {e}. DB(works)에 품번이 있어야 합니다.")
    a = r["assets"]
    em.file("프레임(상시)", a["frame"])
    em.file("인포카드", a["info"])
    em.file("워터마크(상시)", a["wm"])
    em.file("미리보기·인포카드", r["preview_info"])
    em.file("미리보기·워터마크", r["preview_wm"])
    if r.get("preview_anim"):
        em.file("움직이는 미리보기(4초)", r["preview_anim"])
    return {"step": "banner", "code": code, "assets": a,
            "preview_info": r["preview_info"], "preview_wm": r["preview_wm"],
            "preview_anim": r.get("preview_anim") or "",
            "meta": {k: r["meta"][k] for k in ("code", "actress", "title")}}


def stage_burn(c, code, styles, em, source=None, banner=True, parts=None):
    """⑥ 굽기(하드섭) — voiced 우선 → final. {code}_final_subbed.mp4 생성.
    banner=True면 프레임·인포카드·워터마크를 같은 인코딩 1패스에서 함께 굽는다.
    parts={'frame','info','wm','subs': bool} 로 구울 요소를 고른다(미리보기 체크 그대로)."""
    outdir = work_dir(c, code)
    voiced = outdir / f"{code}_final_voiced.mp4"
    final = outdir / f"{code}_final.mp4"
    if source:
        src = Path(source)
    elif voiced.is_file():
        src = voiced            # 음성 입힌 영상 우선
    elif final.is_file():
        src = final
    else:
        raise RuntimeError(f"원본 영상이 없습니다: {final} (먼저 리뷰 생성)")
    dsrt = outdir / f"{code}_대사.srt"
    nsrt = outdir / f"{code}_내레이션.srt"
    njson = outdir / f"{code}_내레이션.json"     # 유형(style) 포함 → 타입별 스타일
    djson = outdir / f"{code}_대사.json"          # 화자(speaker) 포함 → 여/남 색 구분
    out = str(outdir / f"{code}_final_subbed.mp4")
    # parts로 레이어를 골라 굽는다(미리보기에서 체크한 것만) — 미지정이면 전부
    parts = parts or {}
    want_subs = bool(parts.get("subs", True))
    bl = banner_layers(c, code, em) if banner else None
    if bl:
        bl = {k: v for k, v in bl.items() if parts.get(k, True)}
        bl = bl or None
    picked = ([k for k in ("frame", "info", "wm") if bl and k in bl]
              + (["자막"] if want_subs else []))
    em.step(1, 1, "굽기(ffmpeg) — " + (", ".join(picked) or "없음"))
    P.burn_subs(str(src), str(dsrt), str(nsrt), out, styles,
                str(njson) if njson.is_file() else None,
                str(djson) if djson.is_file() else None, em.log,
                banner=bl, subs=want_subs)
    em.file("완성 영상", out)
    # 완성본 수거함 — 품번 폴더에 흩어진 완성본을 {out_dir}/_완성/ 한 곳에 모은다(풀오토 출구)
    try:
        dest = Path(c["out_dir"]) / "_완성" / f"{_safe(code)}.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out, dest)
        em.file("완성본 수거", dest)
    except Exception as e:
        em.log(f"※ 완성본 수거 실패({e}) — 완성본은 {out}")
    return {"mode": "burn", "subbed": out, "source": str(src),
            "banner": bool(bl), "parts": picked}
