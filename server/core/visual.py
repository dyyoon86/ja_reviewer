# -*- coding: utf-8 -*-
"""화면 시각정보(visual grounding) — 클린본에서 키프레임을 뽑아 claude -p(비전)로
'[초] 장면설명 / 화면글자' 브리핑 텍스트를 만든다. 이 텍스트를 ②AI 프롬프트에 넣어주면
LLM이 오디오 전사만이 아니라 '화면에서 뭐가 벌어지는지'까지 알고 대사·내레이션을 쓴다.

전제: 클린본(노출 제거된 영상)만 대상으로 한다 — 외부 LLM에 노골 프레임을 보내지 않는다.
실패는 전부 soft-fail(빈 문자열 반환) — 시각정보 없이 기존대로 진행한다.
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path

from server import pipeline as P
from server.core.llm import run_claude


def _frame_times(dur, step, cap):
    """0..dur 를 step초 간격으로. cap 초과 시 균등 스트라이드로 cap개까지 줄인다."""
    times = []
    t = 0.0
    while t < dur:
        times.append(round(t, 2))
        t += step
    if len(times) > cap and cap > 0:
        stride = len(times) / cap
        times = [times[int(i * stride)] for i in range(cap)]
    return times


def _extract(video, times, outdir, wh=640):
    """프레임을 JPEG 로 뽑는다(비전 토큰은 픽셀 수로 정해지므로 PNG→JPEG 는 전송량만 1/5)."""
    frames = []
    for i, t in enumerate(times):
        dst = outdir / f"f_{i:03d}_{t:07.1f}s.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
             "-vf", f"scale={wh}:-1", "-q:v", "4", str(dst), "-loglevel", "error"],
            capture_output=True)
        if dst.is_file():
            frames.append((t, dst))
    return frames


def _caption(frames, frame_dir, model, log, effort="low"):
    """claude -p (비전) 한 콜·한 턴으로 프레임 전부 캡션 — 이미지를 stream-json 으로 인라인 전달.

    ★2026-10-12 토큰 실측(ja26 15편): 옛 방식(`--allowedTools Read` + 경로 나열)은 모델이 프레임을
      Read 도구로 한 장씩 읽어 편당 60~100턴이 되고, 턴마다 앞선 이미지 전부를 다시 읽는다 →
      편당 캐시쓰기 170K·캐시읽기 500K·출력 13K, 15편에 파이프라인 LLM 비용의 47%.
      인라인은 한 턴(프레임당 ~350토큰, 60장 ≈ 22K 입력)으로 끝난다. 격리 플래그도 함께 적용."""
    import base64
    head = (
        "아래 이미지들은 '정사장면을 이미 제거한' 리뷰용 영상에서 시간순으로 뽑은 프레임이다. "
        "각 이미지 바로 앞에 `[초s]` 시각 표기가 있다.\n"
        "각 프레임을 정확히 `[초s] 장면설명 / 화면글자` 형식으로 한 줄씩만, 주어진 순서대로 전부 답하라"
        "(설명·머리말 금지, 건너뛰기 금지).\n"
        "장면설명: **맨 앞에 화면 속 인물 구성**을 `여성1`·`여성1남성1`·`남성1`·`인물없음` 처럼 적고, "
        "이어서 자세·행동·감정·장소를 사실 그대로 짧게. 화면글자(OCR) 없으면 '없음'.\n"
        "(컷 선정이 여배우 화면 위주로 이뤄지므로 남성만 보이는 프레임을 정확히 구분하는 것이 중요하다.)"
    )
    content = [{"type": "text", "text": head}]
    for t, path in frames:
        content.append({"type": "text", "text": f"[{t:.0f}s]"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.b64encode(path.read_bytes()).decode("ascii")}})
    try:
        out, _ = run_claude(content=content, model=model, effort=effort, log=log,
                            tag="visual_caption", timeout=900)
    except Exception as e:
        log(f"※ 시각 캡션 호출 실패({type(e).__name__}: {e}) — 시각정보 없이 진행")
        return ""
    out = out.strip()
    # 캡션 줄만 남긴다([..s] 로 시작하는 줄)
    keep = [ln for ln in out.splitlines() if re.match(r"\s*\[?\d+\s*s", ln)]
    return "\n".join(keep) if keep else out


def _overview(caps, model, log, effort="low"):
    """프레임 캡션 전체 → '작품이 무엇인지' 3줄 판정.

    ★2026-08-12 추가. 캡션은 6초마다 프레임을 **하나씩 독립적으로** 설명하기 때문에
      조각은 다 잡히는데 전체가 뭔지는 아무도 말하지 않는다. DSOD-001이 그랬다 —
      '트레이닝복 무리', "'08'번 여자", '붉은 옷 감시자 총 든 채', '타이머 29:22'가
      전부 캡션에 있었는데도 **오징어게임 패러디라는 말이 한 번도 안 나왔고**, 그래서
      내레이션이 가장 큰 후킹 포인트를 통째로 놓쳤다("들이닥친 남자들이 게임을 걸고").
      캡션을 다시 한 번 통으로 읽혀 설정·패러디·장르를 명시적으로 뽑아 앞에 붙인다.
    """
    if not caps.strip():
        return ""
    prompt = "\n".join([
        "아래는 리뷰용 영상에서 6초 간격으로 뽑은 프레임 캡션이다(정사장면은 이미 제거됨).",
        "전체를 읽고 **정확히 3줄**로만 답하라. 머리말·설명 금지.",
        "설정: (장소·인물 관계·상황을 한 문장)",
        "패러디: (널리 알려진 영화·드라마·게임의 패러디나 오마주가 명백하면 그 작품명. "
        "예: 초록 트레이닝복+번호+빨간 감시자+카운트다운 → 오징어게임. 아니면 '없음')",
        "장르: (예: 오피스 드라마 / 스릴러 / 학원물 / 인터뷰)",
        "", caps,
    ])
    try:
        out, _ = run_claude(prompt, model=model, effort=effort, log=log,
                            tag="visual_overview", timeout=300)
    except Exception as e:
        log(f"※ 시각 요약 실패({type(e).__name__}: {e}) — 캡션만 사용")
        return ""
    out = out.strip()
    keep = [ln.strip() for ln in out.splitlines()
            if re.match(r"\s*(설정|패러디|장르)\s*:", ln)]
    return "\n".join(keep)


def build_visual_brief(video, cfg, log=print):
    """클린본 → 시각 브리핑 텍스트. 실패 시 '' 반환(soft-fail)."""
    video = Path(video)
    if not video.is_file():
        log("※ 시각정보: 영상 경로 없음 — 생략")
        return ""
    step = float(cfg.get("visual_step", 6.0))
    cap = int(cfg.get("visual_cap", 60))
    model = cfg.get("visual_model", "sonnet")
    effort = cfg.get("visual_effort", "low")   # 캡션은 묘사 작업 — 사고 토큰이 필요 없다
    try:
        dur = P.video_duration(video)
    except Exception as e:
        log(f"※ 시각정보: 길이 측정 실패({e}) — 생략")
        return ""
    times = _frame_times(dur, step, cap)
    if not times:
        return ""
    tmp = Path(tempfile.mkdtemp(prefix="visbrief_"))
    try:
        log(f"화면 시각정보: {len(times)}프레임 추출 → claude -p({model}) 캡션 중…")
        frames = _extract(video, times, tmp)
        if not frames:
            log("※ 시각정보: 프레임 추출 0 — 생략")
            return ""
        brief = _caption(frames, tmp, model, log, effort)
        if brief and cfg.get("visual_overview", True):
            ov = _overview(brief, model, log, effort)
            if ov:
                log("작품 판정: " + " / ".join(ov.splitlines()))
                brief = ov + "\n\n" + brief     # 전체 판정을 맨 앞에
        if brief:
            log(f"화면 시각정보 확보: {len(brief.splitlines())}줄")
        return brief
    finally:
        for f in tmp.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            tmp.rmdir()
        except OSError:
            pass
