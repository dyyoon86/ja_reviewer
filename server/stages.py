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


def worklog(outdir, code, line):
    """품번별 작업 로그 — '언제 뭘 했고 왜 그렇게 됐나'를 한 줄씩 append.
    재작업 추적용(같은 품번을 컨셉 바꿔 여러 번 돌리면 뭐가 뭔지 알 수 없어진다).
    실패해도 파이프라인을 막지 않는다(로그일 뿐)."""
    try:
        import datetime
        ts = datetime.datetime.now().strftime("%m-%d %H:%M")
        p = Path(outdir) / f"{code}_작업로그.md"
        with p.open("a", encoding="utf-8") as f:
            f.write(f"- `{ts}` {line}\n")
    except OSError:
        pass


def src_sig(video):
    """소스 파일 지문(크기:수정시각) — 전사 캐시가 '같은 품번, 다른 영상'에 재사용되는 것을 막는다.
    (품번 폴더로 결과를 모으므로, 같은 품번에 다른 컷/원본을 넣으면 옛 전사를 물려받았다)"""
    try:
        s = Path(video).stat()
        return f"{s.st_size}:{int(s.st_mtime)}"
    except OSError:
        return ""


def transcribe_fresh(outdir, code, video):
    """전사 결과가 '지금 그 영상'의 것인가. 지문이 없으면(구버전 state) 있는 것으로 본다."""
    if not (Path(outdir) / f"{code}_전사.json").is_file():
        return False
    st = load_state(outdir, code)
    old = st.get("src_sig")
    if not old or not video:
        return True          # 구버전 state — 기존 동작 유지(재전사 강요하지 않음)
    return old == src_sig(video)


def steps_status(outdir, code, video=None):
    """단계 완료 여부는 '결과 파일 존재'로 판정 → 서버 재시작/수동삭제에도 견고.
    video를 주면 전사는 '그 영상의 전사인지'까지 본다(소스가 바뀌면 재전사)."""
    o = Path(outdir)
    # 배너 레이어는 작업폴더가 아니라 {out_dir}/_infocard_{code}/ 에 모인다
    ic = o.parent / f"_infocard_{code}"
    # clean: 클린본이 있거나, 스캔 결과 노출이 없어 원본을 그대로 쓰기로 한 경우(state.cleaned)
    return {"clean": (o / f"{code}_클린.mp4").is_file()
                     or bool(load_state(outdir, code).get("cleaned")),
            "transcribe": (transcribe_fresh(o, code, video) if video
                           else (o / f"{code}_전사.json").is_file()),
            "ai": (o / f"{code}_plan.json").is_file(),
            "subs": (o / f"{code}_대사.srt").is_file(),
            "banner": (ic / f"{code}_워터마크.png").is_file(),
            "tts": (o / f"{code}_내레이션.wav").is_file(),
            "burn": (o / f"{code}_final_subbed.mp4").is_file()}


def write_narration(outdir, code, nar_rt):
    """내레이션 출력 — SRT + JSON. 내레이션은 완결문장이라 25자 분할 안 함(끊김·시간겹침 방지).
    화면 줄바꿈은 굽기(ASS)에서 자동 처리. TTS도 완결문장이 자연스러움.

    ★style='드립'(구타바리형 괄호 드립자막)은 **SRT에서 뺀다** — SRT는 TTS 입력이라
    남겨두면 성우가 '(지림)'을 소리내어 읽는다. 화면에는 떠야 하므로 JSON에는 남긴다
    (굽기·미리보기는 _내레이션.json을 읽는다)."""
    spoken = [(s, e, t) for s, e, t, *x in nar_rt if (x[0] if x else "기본") != "드립"]
    P.write_srt(spoken, outdir / f"{code}_내레이션.srt", maxlen=0)
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
def _chain_clean(c, code, video, em, gpu, outdir, clean):
    """⓪-A **3중 필터 순차 클린** — 수동 모드의 '⚡ 순차 자동 클린'과 같은 흐름을 자동에서도.

    순서 2️⃣소리 → 3️⃣의미 → 1️⃣화면 (실측 근거, 123분 원본):
      분당 스캔 비용이 STT 0.69s < CLIP 1.06s < NN 1.44s라 **제일 싼 스캔에게 제일 긴
      영상을 맡기고**, 제일 비싼 NN은 마지막에 남은 몇 분만 보게 한다(총 6:32 → 2:03).
    각 단계: 스캔 → 검출되면 그 자리에서 컷 → 잘린 영상으로 다음 스캔.
    NN 단독(구 방식)이 못 잡던 '옷 입은 채 어두운 조명 애무'를 3️⃣ CLIP이 잡고,
    NN이 정사로 오판하던 '노출 의상 대화'를 2️⃣ 소리가 되살린다."""
    from server.core import moan, nsfw, intimacy
    src = str(video)
    orig_total = P.video_duration(video) or 0.0
    n_all = 0

    stages = [
        ("2️⃣ 소리(신음·정사)", lambda v, t: moan.scan_audio(
            v, model_name=c.get("scan_model", "small"), log=em.log,
            progress=lambda fr: em.prog(fr, "소리 스캔"),
            pad=float(c.get("cut_pad_moan", 5.0)))[0]),
        ("3️⃣ 의미(스킨십·애무)", lambda v, t: intimacy.scan_intimacy(
            v, step=float(c.get("intimacy_step", 2.0)),
            threshold=float(c.get("intimacy_threshold", 0.02)),
            min_dur=float(c.get("intimacy_min_dur", 14.0)),
            log=em.log, duration=t,
            progress=lambda fr: em.prog(fr, "의미 스캔"))),
        ("1️⃣ 화면(NN 노출)", lambda v, t: nsfw.build_map(
            v, step=float(c.get("nsfw_scan_step", 1.0)),
            threshold=float(c.get("nsfw_clean_threshold", 0.22)),
            pad=float(c.get("nsfw_pad", 3.0)),
            merge_gap=float(c.get("nsfw_merge_gap", 12.0)),
            cache=None, log=em.log, duration=t,
            progress=lambda fr: em.prog(fr, "화면 스캔"))),
    ]
    tmp_prev = None
    for i, (label, scan) in enumerate(stages, 1):
        total = P.video_duration(src) or 0.0
        em.step(i, len(stages), f"⓪ {label} — {total / 60:.0f}분")
        with gpu:
            bad = scan(src, total)
        if not bad:
            em.log(f"  {label}: 검출 0 — 자를 것 없음")
            continue
        keep = nsfw.complement(bad, total, min_len=float(c.get("nsfw_min_clip", 3.0)))
        if not keep:
            raise RuntimeError(f"{label}에서 전부 제거돼 남는 영상이 없습니다 "
                               f"— 대사 없는 본편형으로 보입니다(자동화 부적합).")
        cut_sec = total - sum(b - a for a, b in keep)
        em.log(f"  {label}: {len(bad)}구간 {cut_sec / 60:.1f}분 제거 "
               f"→ {sum(b - a for a, b in keep) / 60:.1f}분 유지")
        dst = outdir / (f"{code}_클린.mp4" if i == len(stages) else f"{code}_클린_s{i}.mp4")
        with gpu:
            # 스마트 컷 우선(수동 ⚡ /trim과 동일 경로) — 경계 GOP만 재인코딩하고 중간은
            # 스트림 카피. 경계 정밀도는 재인코딩 컷과 같은 frame-accurate라 노출 경계가
            # 밀리지 않는다. 3중 필터는 단계마다 '남기는 분량 전체'를 다시 인코딩해 왔는데
            # 실측(ja14 FNS-230, 171분)에서 편당 66분 중 60분이 컷이었다(NVENC 1.2배속,
            # 누적 재인코딩 119분). 남길 게 많을수록 손해라 여기서도 스마트 컷을 쓴다.
            # 폴백은 재인코딩만 — cut_video_copy는 keep 시작을 앞쪽 키프레임으로 스냅해
            # 직전 제거 구간(=노출)의 꼬리를 도로 물고 올 수 있어 클린 단계엔 부적합.
            try:
                P.cut_video_smart(src, keep, str(dst), em.log,
                                  lambda fr: em.prog(fr, f"{label} 컷"))
            except Exception as se:
                em.log(f"  스마트 컷 실패({se}) → 재인코딩 컷으로 폴백")
                P.cut_video(src, keep, str(dst), em.log,
                            lambda fr: em.prog(fr, f"{label} 컷"))
        if tmp_prev:
            try:
                Path(tmp_prev).unlink()
            except OSError:
                pass
        tmp_prev = str(dst) if dst.name != f"{code}_클린.mp4" else None
        src = str(dst)
        n_all += len(bad)

    if src == str(video):          # 세 스캔 모두 검출 0 — 원본 그대로
        em.log("3중 필터 검출 0 — 원본을 그대로 씁니다(컷 생략)")
        save_state(outdir, code, video=str(video), source_video=str(video), cleaned=True)
        return {"step": "clean", "code": code, "clean": str(video), "cut": False}
    if src != str(clean):
        shutil.move(src, clean)
    final_sec = P.video_duration(clean) or 0.0
    save_state(outdir, code, video=str(clean), source_video=str(video), cleaned=True)
    worklog(outdir, code, f"⓪ 3중 필터 클린 — {n_all}구간 제거, "
                          f"{orig_total / 60:.0f}분 → {final_sec / 60:.1f}분")
    em.file("클린본(3중 필터)", clean)
    em.log(f"✔ 클린본 확정: {final_sec / 60:.1f}분 "
           f"(원본 {orig_total / 60:.0f}분에서 {(orig_total - final_sec) / 60:.1f}분 제거)")
    return {"step": "clean", "code": code, "clean": str(clean),
            "removed_sec": round(orig_total - final_sec, 1), "kept_sec": round(final_sec, 1)}


def stage_clean(c, code, video, em, gpu=None):
    """⓪ 노출 제거 — 부적절 구간을 **물리적으로 잘라낸** 클린본 {code}_클린.mp4 을 만든다.
    이후 모든 단계(전사·AI·컷·자막·번인)는 이 클린본만 본다 — 노출이 뒤 단계로 새어나갈
    여지 자체가 사라진다. 부수 효과: 전사·AI가 다룰 길이가 줄어 전체가 빨라지고
    LLM 호출도 준다(map-reduce 생략).

    기본은 **3중 필터 순차 클린**(수동 ⚡와 동일: 2️⃣소리 → 3️⃣의미 → 1️⃣화면).
    config `clean_mode="nn"`으로 두면 옛 NN 반복 방식으로 되돌아간다."""
    from server.core import nsfw
    gpu = gpu or NullLock()
    outdir = work_dir(c, code)
    clean = outdir / f"{code}_클린.mp4"
    if clean.is_file():
        em.log(f"클린본 재사용: {clean}")
        save_state(outdir, code, video=str(clean), source_video=str(video), cleaned=True)
        return {"step": "clean", "code": code, "clean": str(clean), "reused": True}

    if c.get("clean_mode", "chain") == "chain":
        return _chain_clean(c, code, video, em, gpu, outdir, clean)

    # ── (구) NN 반복 방식 — NudeNet 점수가 임계 근처에서 요동쳐 1패스로는 0이 안 된다.
    #    검출이 0이 되거나 더 줄지 않을 때까지 스캔→컷을 반복.
    step = float(c.get("nsfw_scan_step", 1.0))
    thr = float(c.get("nsfw_clean_threshold", 0.22))   # 클린 단계는 공격적으로(경계선 포착)
    pad = float(c.get("nsfw_pad", 3.0))
    gap = float(c.get("nsfw_merge_gap", 12.0))
    min_clip = float(c.get("nsfw_min_clip", 3.0))
    max_pass = int(c.get("nsfw_max_pass", 3))

    src = str(video)
    orig_total = P.video_duration(video)
    tmp_prev = None
    prev_bad_sec = None
    for p in range(1, max_pass + 1):
        total = P.video_duration(src)
        em.step(p, max_pass, f"노출 스캔 {p}패스 ({total / 60:.0f}분 분량 — 수 분 걸립니다)")
        with gpu:
            bad = nsfw.build_map(src, step=step, threshold=thr, pad=pad, merge_gap=gap,
                                 cache=(str(outdir / f"{code}_노출지도.json") if p == 1 else None),
                                 log=em.log, duration=total,
                                 progress=lambda fr: em.prog(fr, f"노출 스캔 {p}패스"))
        if not bad:
            em.log(f"✔ {p}패스: 노출 검출 0 — 수렴 완료")
            break
        bad_sec = sum(b - a for a, b in bad)
        # ★ 수렴 정지 — NudeNet 점수는 임계 근처에서 요동쳐서(같은 장면 0.45↔0.30) 패스를
        #   거듭해도 경계선 검출이 계속 나온다(실측: 2패스 31프레임 → 3패스 36프레임으로 오히려 증가).
        #   더 자르면 멀쩡한 장면만 깎여나가므로, 줄지 않으면 멈추고 완성본 전수 검사에 맡긴다.
        if prev_bad_sec is not None and bad_sec > prev_bad_sec * 0.7:
            em.log(f"※ {p}패스: 검출이 더 줄지 않습니다({prev_bad_sec / 60:.1f}→{bad_sec / 60:.1f}분) "
                   f"— 경계선 오검출로 보고 반복을 멈춥니다. "
                   f"최종 안전은 완성본 전수 검사(⑥)가 담당합니다")
            break
        prev_bad_sec = bad_sec
        keep = nsfw.complement(bad, total, min_len=min_clip)
        if not keep:
            raise RuntimeError("노출을 제거하고 나면 남는 영상이 없습니다 "
                               "— 전편이 노출입니다(자동화 부적합).")
        kept = sum(b - a for a, b in keep)
        em.log(f"{p}패스: 노출 {len(bad)}구간({(total - kept) / 60:.1f}분) 제거 "
               f"→ {kept / 60:.1f}분 유지")
        dst = outdir / (f"{code}_클린.mp4" if p == max_pass else f"{code}_클린_p{p}.mp4")
        with gpu:
            P.cut_video(src, keep, str(dst), em.log, lambda fr: em.prog(fr, f"제거 {p}패스"))
        if tmp_prev:                      # 이전 패스 중간본 정리
            try:
                Path(tmp_prev).unlink()
            except OSError:
                pass
        tmp_prev = str(dst) if dst.name != f"{code}_클린.mp4" else None
        src = str(dst)
    # 마지막 패스 산출물을 최종 클린본 이름으로 확정
    if src != str(clean):
        if Path(src) == Path(video):      # 노출이 애초에 없어 컷을 한 번도 안 함
            em.log("노출 검출 0 — 원본을 그대로 씁니다(컷 생략)")
            save_state(outdir, code, video=str(video), source_video=str(video), cleaned=True)
            return {"step": "clean", "code": code, "clean": str(video), "cut": False}
        shutil.move(src, clean)
    final_sec = P.video_duration(clean)
    # 이후 단계는 전부 클린본을 본다(state.video 교체). 원본 경로는 source_video로 보존.
    save_state(outdir, code, video=str(clean), source_video=str(video), cleaned=True)
    em.file("클린본(노출 제거)", clean)
    em.log(f"클린본 확정: {final_sec / 60:.1f}분 (원본 {orig_total / 60:.0f}분에서 "
           f"{(orig_total - final_sec) / 60:.1f}분 제거)")
    return {"step": "clean", "code": code, "clean": str(clean),
            "removed_sec": round(orig_total - final_sec, 1), "kept_sec": round(final_sec, 1)}


def stage_transcribe(c, code, video, model, em, initial_prompt=None):
    """① 전사 — 영상 → 일본어 STT. {code}_전사.srt/.json 저장.
    two_pass(기본 on)면 작은 모델로 러프 스캔만 한다(구간 선정용) — 최종 대사자막은
    ② AI 처리에서 keep 구간만 정밀 재전사(transcribe_ranges)로 확보한다.
    2시간짜리 원본도 정밀 전사는 keep 합계(1~3분)에만 들어가 전체가 수 분에 끝난다."""
    outdir = work_dir(c, code)
    two_pass = bool(c.get("two_pass", True))
    used_model = model
    if two_pass:
        scan_model = used_model = c.get("scan_model", "small")
        em.step(1, 1, f"전사 1차 스캔(faster-whisper {scan_model} — 러프, 구간선정용)")
        segs = P.transcribe_scan(video, scan_model, em.log, lambda fr: em.prog(fr, "스캔"),
                                 initial_prompt=initial_prompt)
    else:
        em.step(1, 1, f"전사(faster-whisper {model})")
        segs = P.transcribe(video, model, em.log, lambda fr: em.prog(fr, "전사"),
                            initial_prompt=initial_prompt)
    # 대사 밀도 조기 경보 — 10분당 1줄 미만이면 대사 없는 본편형(신음 위주) 의심.
    # ② AI가 keep 재료를 못 찾아 목표 길이를 못 채울 가능성이 크다(거기서 중단됨).
    dur = P.video_duration(video) or 0
    if dur > 600 and len(segs) < dur / 600:
        em.log(f"⚠ 대사가 매우 적습니다({len(segs)}줄 / {dur/60:.0f}분) — 대사 없는 본편형 의심. "
               f"AI가 구간을 못 고를 수 있습니다(그 경우 ②에서 중단됩니다)")
    data = [{"start": round(s, 3), "end": round(e, 3), "text": t} for s, e, t in segs]
    (outdir / f"{code}_전사.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    P.write_srt([(s, e, t) for s, e, t in segs], outdir / f"{code}_전사.srt")
    save_state(outdir, code, video=str(video), model=model, scan=two_pass,
               src_sig=src_sig(video))   # 소스 지문 — 다른 영상이면 다음 실행에서 재전사
    worklog(outdir, code, f"① 전사({used_model}) — {len(segs)}줄, "
                          f"소스 `{Path(video).name}` ({dur / 60:.0f}분)")
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


def stage_ai(c, code, video, target, llm, mode, hint, em, gpu=None, pos="mid", style="3min",
             nar_rich=None, remove_bgm=None, cutins=None, visual_brief=None):
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
    # ★ 전체 노출 지도 — LLM에 보내기 전에 만든다. 노출 구간 대사를 아예 빼고 주면
    #   AI가 처음부터 클린 구간에서만 고른다(사후에 버려서 재료가 마르는 것보다 낫다).
    #   ⓪ 노출 제거를 이미 거쳤다면(cleaned) 영상에 노출이 없으므로 스캔 자체가 불필요.
    nsfw_map = []
    if st.get("cleaned"):
        em.log("⓪ 노출 제거를 거친 클린본 — 추가 스캔 생략")
    elif Path(str(video) + ".clean").is_file():
        # ⚡ 순차 자동 클린(소리→의미→화면 3중 필터) 완주 마커 — 이미 그 필터들로
        # 잘라낸 영상을 NN으로 또 훑을 이유가 없다(중복 스캔 제거, 2시간 ≈ 3분 절약).
        # keep 단위 비주얼 가드는 안전망으로 그대로 돈다(수 초).
        em.log("⚡ 3중 필터 클린 완료 영상 — 노출지도 스캔 생략")
    elif c.get("nsfw_full_scan", True):
        try:
            from server.core import nsfw
            with gpu:   # ffmpeg 디코딩 — GPU 레인과 함께 묶어 과부하 방지
                nsfw_map = nsfw.build_map(
                    video, step=float(c.get("nsfw_scan_step", 2.0)),
                    threshold=float(c.get("nsfw_threshold", nsfw.DEFAULT_THRESHOLD)),
                    cache=str(outdir / f"{code}_노출지도.json"), log=em.log)
            if nsfw_map:
                before = len(segs)
                segs = nsfw.drop_segments(segs, nsfw_map)
                em.log(f"노출 구간 대사 제외: {before}→{len(segs)}줄 (AI는 클린 구간만 봅니다)")
        except ImportError:
            em.log("※ NudeNet 미설치 — 전체 노출 스캔 생략(pip install nudenet)")
        except Exception as e:
            em.log(f"※ 전체 노출 스캔 실패({type(e).__name__}: {e}) — keep 단위 가드만 적용됩니다")
    # ★ 2-pass면 1회차에서 대사 번역을 시키지 않는다 — 어차피 아래에서 정밀 전사본으로
    #   통째 교체되는 러프 번역이라 출력 토큰만 버리는 셈이었다(2026-07-13).
    #   내레이션 배치는 입력 일본어 자막의 시각으로 판단하므로 품질 손실이 없다.
    two_pass = bool(st.get("scan"))
    label = "하이라이트형(알파컷식)" if mode == "highlight" else "요약형(짜집기)"
    what = "압축·내레이션(대사는 2차 정밀본으로)" if two_pass else "압축·번역·내레이션"
    em.step(2, 3, f"AI {label} {what} ({llm} 추론, 보통 1~3분)")
    pf = P.prompt_highlight if mode == "highlight" else P.prompt_manual
    hb = heartbeat(em, f"AI 처리({llm})")
    try:
        plan_segs, story = _reduce_transcript(m, segs, llm, em,
                                              limit=int(c.get("map_reduce_chars", 25000)))
        full_hint = "\n".join(x for x in ((hint or "").strip(), story) if x)
        # 화면 시각정보 — 클린본 프레임을 비전(claude -p)이 읽어 '장면/화면글자' 브리핑을 만들고
        #   프롬프트에 넣어준다. LLM이 오디오 자막만이 아니라 화면 행동·표정·소품까지 알고
        #   대사/내레이션을 쓴다(config visual_brief, 기본 off). 실패는 soft-fail(없이 진행).
        vis_text = ""
        use_visual = bool(c.get("visual_brief", False) if visual_brief is None else visual_brief)
        if use_visual:
            try:
                from server.core import visual as _visual
                vis_text = _visual.build_visual_brief(video, c, em.log)
                if vis_text:
                    # 섹션3 regen_narration이 최종 내레이션에도 화면 근거를 쓰도록 파일로 남긴다
                    try:
                        (outdir / f"{code}_시각브리핑.txt").write_text(vis_text, encoding="utf-8")
                    except OSError:
                        pass
            except Exception as e:
                em.log(f"※ 시각정보 생성 실패({type(e).__name__}: {e}) — 시각정보 없이 진행")
        # 강조·정보 내레이션은 기본 끔 — 색만 바뀔 뿐 등장 이펙트·크기·효과음 연출이 없어
        # 화면만 산만하다(2026-07-13). 연출을 갖춘 뒤 GUI 체크박스로 켠다.
        rich = bool(c.get("nar_rich", False) if nar_rich is None else nar_rich)
        # 상황별 짤 — 에셋 폴더에 **실제로 파일이 있는 태그만** LLM에 알려준다.
        #   (없는 태그를 고르게 하면 아무 짤도 안 나오는데 프롬프트만 길어진다)
        tags = None
        want_cutins = bool(c.get("cutins", False) if cutins is None else cutins)
        if want_cutins:
            try:
                from server.core import assets
                have = [t for t, n in assets.available(c["out_dir"]).items() if n > 0]
                if have:
                    tags = have
                    em.log(f"짤 태그 {len(have)}종 사용 가능: {', '.join(have)}")
                else:
                    em.log("※ 짤 폴더가 비어 있어 짤 삽입을 건너뜁니다 "
                           f"({assets.assets_dir(c['out_dir']) / 'gifs'})")
            except Exception as e:
                em.log(f"※ 짤 태그 조회 실패({e}) — 짤 없이 진행")
        res = P.call_llm(pf(m, plan_segs, target, hint=full_hint, pos=pos, style=style,
                            with_dialogue=not two_pass, nar_rich=rich, cutin_tags=tags,
                            visual=vis_text),
                         llm, em.log)
    finally:
        hb.set()
    keep = P.parse_keep(res.get("keep", []), total=P.video_duration(video))
    if not keep:
        raise RuntimeError("LLM이 keep 구간을 못 골랐습니다(빈 응답 — 헤드리스 거부 가능. 아래 수동 모드 사용).")
    keep = _guard_keep(keep, segs, em.log)
    # ★ 컷 경계를 대사 줄 경계로 스냅 + 앞뒤 패딩 — 말 중간에서 끊기는 것("…했습ㄴ") 방지.
    #   순서가 중요하다: ① 정밀 재전사보다 **앞** (그래야 정밀 전사·대사가 최종 keep과 일치)
    #                  ② 노출 가드보다 **앞** (패딩이 방금 도려낸 노출을 되살리면 안 되므로
    #                     노출 가드가 마지막에 돌아야 한다)
    if c.get("snap_cuts", True) and keep:
        keep = P.snap_keep_to_lines(
            keep, segs, total=P.video_duration(video),
            pad=float(c.get("cut_pad", 0.15)), log=em.log)
    res["keep"] = [[a, b] for a, b in keep]   # 제외 반영된 keep을 plan에 저장(자막 재타이밍 일치)
    # 2-pass: ①이 러프 스캔이었다면 keep 구간만 정밀 재전사 → 대사자막을 정밀본으로 교체.
    # 실패해도 러프 기반 dialogue가 남아 있으니 결과는 항상 나온다(품질만 러프로 후퇴).
    if two_pass:
        fine = []
        try:
            precise_model = st.get("model") or c.get("whisper_model", "large-v3")
            em.log(f"2차 정밀 전사 — keep {len(keep)}구간만 {precise_model}로 재전사")
            with gpu:
                # ★ initial_prompt를 주지 않는다(2026-08-11 A/B 실측). 메타 힌트는 배우 이름으로
                #   끝나는데(`…瀬戸環奈。瀬戸環奈`), whisper가 그 이름을 그대로 받아적어 구간
                #   전체를 이름 한 줄로 뭉갠다 — SNOS-334 같은 keep에서 22줄 → 6줄로 붕괴했고,
                #   그 6줄 중 2줄이 배우 이름이었다. 이름만 남은 keep은 _guard_keep에 '대사 0줄'로
                #   걸려 통째로 빠지기까지 한다(ja16 MIDA-727/735/762가 대사 0줄로 납품된 경로).
                #   1차 러프 스캔은 구간 선정용이라 힌트를 유지하지만, 최종 자막이 되는 이 정밀
                #   전사만큼은 힌트 없이 원문 그대로 받는 편이 정확하다.
                fine = P.transcribe_ranges(video, keep, precise_model, em.log,
                                           lambda fr: em.prog(fr, "정밀 전사"))
            # ★ 노출 안전장치 2차 — 러프 스캔은 신음 구간에 환청 대사를 지어내
            #   _guard_keep(러프 기준)을 뚫을 수 있다. 정밀 전사에서도 대사 0줄인
            #   keep은 노출 장면으로 보고 다시 제외한다(전부 걸리면 원본 유지=검수行).
            keep2 = _guard_keep(keep, fine, em.log)
            if keep2 != keep:
                keep = keep2
                res["keep"] = [[a, b] for a, b in keep]
            fine = [s for s in fine
                    if any(a - 0.05 <= s[0] < b + 0.05 for a, b in keep)]
        except Exception as e:
            em.log(f"※ 정밀 재전사 실패({type(e).__name__}: {e}) — 러프 전사로 대사자막을 만듭니다")
            fine = []
        if not fine:
            # 정밀 전사가 실패/0줄이어도 대사자막은 있어야 한다(1회차가 번역을 안 했으므로).
            # 러프 전사에서 keep 안의 줄만 추려 같은 번역 프롬프트에 태운다 — 품질만 러프로 후퇴.
            fine = [s for s in segs if any(a - 0.05 <= s[0] < b + 0.05 for a, b in keep)]
            if fine:
                em.log(f"러프 전사로 대체: keep 안 {len(fine)}줄로 대사자막 생성")
        if fine:
            try:
                fix = P.call_llm(P.prompt_dialogue_fix(m, fine), llm, em.log)
                dlg = fix.get("dialogue") or []
                if dlg:
                    res["dialogue"] = dlg
                    em.log(f"대사자막 생성(정밀 전사본): {len(dlg)}줄")
                elif not res.get("dialogue"):
                    em.log("⚠ 대사 번역이 0줄입니다 — 대사자막 없이 내레이션만 나갑니다")
            except Exception as e:
                em.log(f"⚠ 대사 번역 실패({type(e).__name__}: {e}) "
                       f"— 대사자막 없이 진행합니다(②를 다시 실행하면 재시도)")
        elif not res.get("dialogue"):
            em.log("⚠ keep 구간에 대사가 없습니다 — 대사자막 없이 내레이션만 나갑니다")
    # ★ 비주얼 노출 가드 — 대사 기반 가드가 못 잡는 '대사하며 노출' 케이스를 화면으로 직접 판정.
    if c.get("nsfw_guard", True) and keep:
        try:
            from server.core import nsfw
            if nsfw_map:
                # 전체 지도가 있으면 '도려내기' — 구간 통째로 버리지 않아 재료 손실이 적다
                keep2 = nsfw.subtract(keep, nsfw_map)
                cut = sum(b - a for a, b in keep) - sum(b - a for a, b in keep2)
                if cut > 0.5:
                    em.log(f"노출 지도로 keep 정리: {len(keep)}→{len(keep2)}구간 "
                           f"(노출 {cut:.0f}초 도려냄)")
                if not keep2:
                    raise RuntimeError("고른 구간이 전부 노출이라 남는 게 없습니다 "
                                       "— 대사 없는 본편형 작품으로 보입니다(수동 모드 권장).")
            else:
                # 전체 스캔이 꺼져 있으면 keep 구간만 샘플 검사(구간 단위 제외)
                keep2 = nsfw.guard_keep_visual(
                    keep, video, em.log,
                    step=float(c.get("nsfw_step", nsfw.DEFAULT_STEP)),
                    threshold=float(c.get("nsfw_threshold", nsfw.DEFAULT_THRESHOLD)))
            if keep2 != keep:
                keep = keep2
                res["keep"] = [[a, b] for a, b in keep]
                # 제외된 구간의 대사/내레이션은 stage_subs의 retime이 keep 기준으로 정리한다
        except ImportError:
            em.log("※ NudeNet 미설치 — 비주얼 노출 가드 생략(pip install nudenet). 대사 가드만 적용됨")
        except RuntimeError:
            raise
        except Exception as e:
            em.log(f"※ 비주얼 노출 가드 실패({type(e).__name__}: {e}) — 대사 가드만 적용됨")
    # ★ 대사 부족 조기 중단 — 대사 없는 본편형(신음 위주) 작품은 keep 재료가 없어
    #   목표 길이를 못 채운다. 그대로 진행하면 target 기준으로 뽑힌 내레이션(수십 초)이
    #   짧은 영상에 뭉개져(retime이 끝점으로 밀어붙임) 쓸 수 없는 결과가 나온다.
    #   여기서 멈춰야 TTS·번인 낭비도 없앤다. 수동 모드에서 구간을 직접 고르면 된다.
    got = sum(b - a for a, b in keep)
    ratio = float(c.get("min_keep_ratio", 0.5))
    if target and got < target * ratio:
        raise RuntimeError(
            f"대사가 부족해 목표 길이를 못 채웁니다 — 고른 구간 {got:.0f}초 / 목표 {target}초 "
            f"(전사 대사 {len(segs)}줄). 대사 없는 본편형 작품으로 보입니다. "
            f"수동 모드에서 구간을 직접 고르거나, 목표 길이를 줄여 다시 시도하세요.")
    # ★ 컷을 먼저 하고 성공한 뒤에 plan.json을 쓴다.
    #   완료 판정이 plan.json 존재로 되므로, 컷 도중 죽으면 plan.json이 없어 재실행된다
    #   (반대 순서면 컷이 실패해도 'AI 완료'로 오판되어 final.mp4 없이 다음 단계로 넘어감).
    final = str(outdir / f"{code}_final.mp4")
    em.step(3, 3, "핵심 구간 컷")
    with gpu:
        P.cut_video(video, keep, final, em.log, lambda fr: em.prog(fr, "컷"))
    # ※ 원본 BGM 제거는 최종 번인 단계(stage_burn)로 이동했다 — 최종본에 실제로 반영되는
    #   지점이 거기이고, produce만 재실행해도 적용되기 때문(remove_bgm 인자는 하위호환용 유지).

    # 새 컷이 만들어졌다 = 이전 컨셉의 음성본/굽기본/TTS 조각은 전부 구버전 → 삭제
    P.invalidate_derived(outdir, code, em.log)
    # 왜 그 구간을 골랐는지 — LLM이 준 근거를 로그에 남긴다(재작업 추적)
    km = res.get("keep_meta") or res.get("picks") or []
    worklog(outdir, code,
            f"② AI {'하이라이트형' if mode == 'highlight' else '요약형'}({llm}, 목표 {target}s) "
            f"— keep {len(keep)}구간 / {sum(b - a for a, b in keep):.0f}s"
            + (f", ★{P.clamp_stars(res.get('stars'))}" if res.get("stars") else ""))
    for it in (km if isinstance(km, list) else [])[:12]:
        if isinstance(it, dict) and it.get("reason"):
            worklog(outdir, code,
                    f"    · {float(it.get('start', 0)):.0f}~{float(it.get('end', 0)):.0f}s "
                    f"[{it.get('beat') or ('hook' + str(it.get('hook', '')))}] {it['reason']}")
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
    # 상황별 짤 — LLM이 준 시간은 '원본 기준'이므로 keep 기준(최종 영상)으로 재타이밍한다.
    #   자막과 같은 retime을 태워야 컷 뒤에도 같은 장면에 붙는다.
    cut_in = res.get("cutins") or []
    if cut_in:
        rows = [(float(x.get("start", 0)), float(x.get("end", 0)) or float(x.get("start", 0)) + 2.5,
                 str(x.get("tag") or "")) for x in cut_in if x.get("tag")]
        rt = P.retime([(a, b, t) for a, b, t in rows], keep, snap=False)
        data = [{"start": round(a, 3), "end": round(b, 3), "tag": t} for a, b, t, *_ in rt]
        (outdir / f"{code}_짤.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        em.log(f"상황 짤 {len(data)}개 (컷 기준으로 시간 재계산)")
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
    # 화자 선별 — voicebox 생성 편차(실측 0.815~0.920)로 문장 하나가 다른 목소리처럼
    # 들리는 것 방지. seed를 바꿔 후보를 만들고 기준 임베딩에 가까운 것을 채택한다.
    # 기준(voice_ref.npy)이 없거나 resemblyzer가 없으면 자동으로 단일 생성으로 돌아간다.
    ncand = int(c.get("tts_candidates", 1) or 1)
    ref_npy = None
    if ncand > 1:
        ref_npy = c.get("voice_ref") or str(Path(__file__).resolve().parent.parent
                                            / "models" / "voice_ref.npy")
        if not Path(ref_npy).is_file():
            em.log(f"※ 화자 기준 임베딩 없음({ref_npy}) — 후보 선별 없이 단일 생성")
            ref_npy = None
        else:
            em.log(f"화자 선별 켜짐: 문장당 후보 {ncand}개")
    clips = []
    total = len(entries) + 1 + (1 if mux else 0)
    for i, (st, en, text) in enumerate(entries, 1):
        em.step(i, total, f"음성 {i}/{len(entries)}: {text[:18]}")
        w = str(clipdir / f"n{i:03d}.wav")
        if ref_npy:
            P.tts_generate_best(base, text, profile, language, w, seed,
                                candidates=ncand, ref_npy=ref_npy,
                                python=c.get("voice_python"), log=em.log)
        else:
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
        # ★ 신작은 로컬 DB에도, 아직 크롤링 전이면 우분투 meta_api에도 없을 수 있다.
        #   그렇다고 배너 하나 때문에 파이프라인 전체를 죽이면 안 된다 — 배너만 건너뛰고
        #   자막·TTS·번인은 그대로 진행한다(⑥ 굽기의 banner_layers가 None을 받아 자막만 굽는다).
        #   나중에 크롤링되면 이 단계만 다시 돌려 배너를 붙일 수 있다.
        em.log(f"⚠ 배너 생략 — {e}")
        em.log("   아직 크롤링되지 않은 신작으로 보입니다. 자막·음성·번인은 그대로 진행합니다.")
        em.log("   나중에 DB에 올라오면 ④ 배너만 다시 실행해 붙이면 됩니다.")
        return {"step": "banner", "code": code, "skipped": True, "reason": str(e)}
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


def stage_burn(c, code, styles, em, source=None, banner=True, parts=None, cutins=None,
               remove_bgm=None, reframe=None):
    """⑥ 굽기(하드섭) — voiced 우선 → final. {code}_final_subbed.mp4 생성.
    banner=True면 프레임·인포카드·워터마크를 같은 인코딩 1패스에서 함께 굽는다.
    parts={'frame','info','wm','subs': bool} 로 구울 요소를 고른다(미리보기 체크 그대로).
    reframe=True면 굽기 전에 1080p 리프레임(위쪽 중앙 200% 확대)을 먼저 한다."""
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
    # ★ 1080p 리프레임 — 굽기 **전에** 한다. 720p에 자막을 굽고 나중에 확대하면 글자가
    #   같이 뭉개지고, 배너 PNG(1920x1080 원본)도 한 번 줄였다 늘리는 꼴이 된다.
    #   먼저 1920x1080으로 만들어 놓으면 자막·배너가 native 해상도로 들어간다.
    #   납품본을 프리미어 1080p 타임라인에 100%로 얹기 위한 규격(ja12 v3~).
    rf_tmp = None
    if bool(c.get("reframe_1080", False) if reframe is None else reframe):
        rf_tmp = outdir / f"{code}_rf1080.mp4"
        P.reframe(str(src), str(rf_tmp), zoom=float(c.get("reframe_zoom", 2.0)),
                  align=c.get("reframe_align", "top"), log=em.log)
        src = rf_tmp
        # 스타일은 720p 캔버스 기준이라 그대로 쓰면 글자가 절반으로 보인다 → 1.5배.
        styles = P.scale_styles(styles, override={**P.STYLE_1080_OVERRIDE,
                                                  **(c.get("sub_styles_1080") or {})})
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
        # ★워터마크 우상단 재배치 — gen_infocard는 좌상단에 그리는데 납품 규격은 우상단이다
        #   (인포카드가 왼쪽에서 뜨므로 워터마크까지 왼쪽이면 시선이 한쪽에 몰린다).
        #   리프레임 납품본에서만 켠다. config wm_topright로 강제 on/off.
        wm_tr = c.get("wm_topright")
        if bl.get("wm") and (rf_tmp is not None if wm_tr is None else wm_tr):
            try:
                bl["wm"] = P.wm_to_topright(bl["wm"], margin=float(c.get("wm_margin", 24)))
            except Exception as e:
                em.log(f"※ 워터마크 우상단 재배치 실패({type(e).__name__}: {e}) — 원래 위치로")
        bl = bl or None
    picked = ([k for k in ("frame", "info", "wm") if bl and k in bl]
              + (["자막"] if want_subs else []))
    em.step(1, 1, "굽기(ffmpeg) — " + (", ".join(picked) or "없음"))
    anim = dict(P.BANNER_ANIM)
    try:   # 인포카드 유지시간(초) — config banner_hold로 조절, 워터마크는 그 직후 등장
        anim["hold"] = float(c.get("banner_hold", anim["hold"]))
        anim["wm_start"] = anim["hold"] + 0.1
    except (TypeError, ValueError):
        pass
    # 상황별 짤 — {code}_짤.json(컷 기준 시각) + 에셋 폴더의 실제 파일을 맞춰 얹는다
    cuts = []
    gjson = outdir / f"{code}_짤.json"
    want_cutins = bool(c.get("cutins", False) if cutins is None else cutins)
    if want_cutins and gjson.is_file():
        try:
            from server.core import assets
            cuts = assets.resolve_cutins(
                c["out_dir"], json.loads(gjson.read_text(encoding="utf-8")), log=em.log)
        except Exception as e:
            em.log(f"※ 짤 준비 실패({type(e).__name__}: {e}) — 짤 없이 굽습니다")

    P.burn_subs(str(src), str(dsrt), str(nsrt), out, styles,
                str(njson) if njson.is_file() else None,
                str(djson) if djson.is_file() else None, em.log,
                banner=bl, banner_anim=anim, subs=want_subs,
                screen_flash=bool(c.get("screen_flash", True)),
                flash_intensity=float(c.get("flash_intensity", 0.14)),
                cutins=cuts, cutin_pos=c.get("cutin_pos", "tr"),
                cutin_scale=float(c.get("cutin_scale", 0.26)))
    if rf_tmp is not None:      # 리프레임 중간물 — 구웠으면 쓸모없다(용량만 차지)
        try:
            rf_tmp.unlink()
        except OSError:
            pass
    # 강조·정보 자막이 뜨는 순간에 효과음 — 등장 애니(쾅/일렁임)와 짝이 되어야 임팩트가 산다
    if c.get("sfx", True) and want_subs and njson.is_file():
        try:
            from server.core import sfx as SFX
            from server.core.subs import STYLE_DEFAULT, STYLE_TAGNAME
            st_all = {**STYLE_DEFAULT, **(styles or {})}
            key = {"Emphasis": "emphasis", "Info": "info", "Drip": "drip"}
            events = []
            for d in json.loads(njson.read_text(encoding="utf-8")):
                tag = STYLE_TAGNAME.get(d.get("style", "기본"), "Narration")
                k = key.get(tag)
                if not k:
                    continue
                name = (st_all.get(k) or {}).get("sfx")
                if name:
                    events.append((float(d["start"]), name))
            if events:
                mixed = str(outdir / f"{code}_final_subbed_sfx.mp4")
                if SFX.mix_events(out, events, mixed, c["out_dir"], log=em.log):
                    shutil.move(mixed, out)
        except Exception as e:
            em.log(f"※ 효과음 삽입 실패({type(e).__name__}: {e}) — 효과음 없이 진행")
    em.file("완성 영상", out)
    # ★ 최후 방어선 — 실제로 나가는 완성본을 전수 검사(0.25s 간격).
    #   keep 가드는 2초 샘플이라 컷 경계에 스치는 노출을 놓칠 수 있다. 여기서 잡는다.
    #   검출되면 _완성/이 아니라 _검수필요/로 보내 업로드 대상에서 자동 격리한다.
    flagged = False
    if c.get("nsfw_final_check", True):
        try:
            from server.core import nsfw
            hits = nsfw.check_final(out, step=float(c.get("nsfw_final_step", 0.25)),
                                    threshold=float(c.get("nsfw_threshold", nsfw.DEFAULT_THRESHOLD)),
                                    log=em.log)
            flagged = bool(hits)
        except ImportError:
            em.log("※ NudeNet 미설치 — 완성본 전수 검사 생략")
        except Exception as e:
            em.log(f"※ 완성본 전수 검사 실패({type(e).__name__}: {e}) — 검사 없이 수거")
    # ★ 자체 검사(self-eval) — 나가는 물건의 컷 경계 팝·정지·무음·자막 커버리지를 기계로 본다.
    #   판정만 하고 고치지 않는다(결함은 리포트로 남기고 사람이 결정).
    if c.get("self_eval", True):
        try:
            from server.core import selfeval
            plan = outdir / f"{code}_plan.json"
            keep = P.parse_keep(json.loads(plan.read_text(encoding="utf-8")).get("keep", [])) \
                if plan.is_file() else []
            ev = selfeval.evaluate(out, keep=keep,
                                   srt_files=[dsrt if dsrt.is_file() else None,
                                              nsrt if nsrt.is_file() else None],
                                   log=em.log, cfg=c)
            (outdir / f"{code}_검사.json").write_text(
                json.dumps(ev, ensure_ascii=False, indent=1), encoding="utf-8")
            if not ev["ok"]:
                em.file("자체 검사 리포트", outdir / f"{code}_검사.json")
                worklog(outdir, code, f"⚠ 자체 검사 결함 {len(ev['issues'])}건 — "
                        + "; ".join(i["detail"] for i in ev["issues"][:3]))
            else:
                worklog(outdir, code, f"✔ 자체 검사 통과 (자막 커버리지 "
                                      f"{ev['sub_coverage'] * 100:.0f}%)")
        except Exception as e:
            em.log(f"※ 자체 검사 실패({type(e).__name__}: {e}) — 건너뜁니다")

    # ★ 원본 BGM 제거 — 최종본 오디오에서 배경음악/현장음을 걷어내 목소리만 남긴다(채널 BGM을
    #   따로 얹을 때 유용). config remove_bgm(또는 인자). demucs 필요(config bgm_python).
    #   실패해도 계속(BGM 있는 채로 수거). 비디오(번인)는 스트림 카피라 재번인 없음.
    if bool(c.get("remove_bgm", False) if remove_bgm is None else remove_bgm):
        try:
            from server.core import bgm
            em.log("원본 BGM 제거 (demucs — 목소리만 남김)")
            bgm.remove_bgm(out, out, log=em.log, python=c.get("bgm_python"),
                           model=c.get("bgm_model") or "htdemucs")
            worklog(outdir, code, "⑥ BGM 제거 — 최종본 배경음악 제거(목소리만)")
        except Exception as e:
            em.log(f"※ BGM 제거 실패({type(e).__name__}: {e}) — 원본 소리 그대로 진행")

    # 완성본 수거함 — 품번 폴더에 흩어진 완성본을 한 곳에 모은다(풀오토 출구).
    # 노출이 검출된 건 _검수필요/ 로 격리 — 사람이 보기 전엔 업로드 폴더에 들어가지 않는다.
    try:
        folder = "_검수필요" if flagged else "_완성"
        dest = Path(c["out_dir"]) / folder / f"{_safe(code)}.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out, dest)
        em.file("완성본 수거" if not flagged else "🚨 검수 필요(노출 검출)", dest)
        if flagged:
            em.log(f"🚨 노출이 검출되어 {folder}/ 로 격리했습니다 — 확인 후 직접 옮기세요")
    except Exception as e:
        em.log(f"※ 완성본 수거 실패({e}) — 완성본은 {out}")
    return {"mode": "burn", "subbed": out, "source": str(src),
            "banner": bool(bl), "parts": picked, "nsfw_flagged": flagged}
