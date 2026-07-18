# -*- coding: utf-8 -*-
"""모음집 최종 생산 배치 — 품번 순서대로 ①내레이션(연속 리뷰) → ②배너 → ③TTS → ④번인.

각 단계 내용:
  ① regen_narration(seq=(i,n)) — 1→n 연속 리뷰 흐름(연결 인트로), AI톤 금지,
     개별 마무리 인사 금지(모음집 맨 끝 멘트는 사람이 별도로).
  ② stage_banner(hold) — 인포배너/프레임/워터마크 PNG. 메타 없으면 배너만 생략하고 진행.
  ③ stage_tts(mux=False) — {code}_내레이션.wav 생성만(영상에 안 섞음, 사람이 조합).
  ④ stage_burn — **대사 자막 + 배너/워터마크만** 번인. 내레이션 자막은 굽지 않도록
     내레이션 srt/json을 잠시 숨겼다가 복원한다(음성·내레이션은 사람이 최종 조합).
     stage_burn이 완성본 전수검사 + 자체검사 + _완성/_검수필요 수거까지 해준다.

사용: .venv\\Scripts\\python.exe tools\\batch_produce.py "C:\\...\\영상폴더" [--meta URL] [--hold 5]
"""
import argparse
import sys
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server import pipeline as P
from server.core.regen import regen_narration
from batch_clean import CliEmitter, guess_code


def ensure_voicebox(base, log=print, boot_wait=420):
    """voicebox 백엔드 생존 확인 — 죽어 있으면 앱을 재기동하고 응답까지 대기.
    (voicebox-server-cuda가 산발적으로 크래시하는 전력: GUI는 살아도 17493이 죽는다)"""
    import subprocess
    import time as _t
    import urllib.request

    def alive():
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/", timeout=3):
                return True
        except Exception:
            return False

    if alive():
        return True
    log("⚠ voicebox 무응답 — 재기동 시도")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Stop-Process -Name voicebox,voicebox-server-cuda -Force -ErrorAction SilentlyContinue; "
                    "Start-Sleep 3; Start-Process 'C:\\Program Files\\Voicebox\\voicebox.exe'"],
                   capture_output=True, timeout=60)
    t0 = _t.time()
    while _t.time() - t0 < boot_wait:
        if alive():
            log(f"voicebox 재기동 완료 ({_t.time() - t0:.0f}s) — 콜드스타트 대기 30s")
            _t.sleep(30)   # 모델 로드 직후 첫 요청이 오래 걸리는 것 완화
            return True
        _t.sleep(5)
    return False


def hide_narration(outdir, code):
    """번인에서 내레이션 자막을 빼기 위한 임시 숨김 — 복원용 목록 반환.
    이전 실행이 중단되며 남긴 .hold 잔재가 있으면 지우고 진행(현재 파일이 최신본)."""
    import os
    moved = []
    for suffix in ("_내레이션.srt", "_내레이션.json"):
        f = outdir / f"{code}{suffix}"
        if f.is_file():
            hidden = f.with_name(f.name + ".hold")
            if hidden.exists():
                hidden.unlink()
            os.replace(f, hidden)
            moved.append((hidden, f))
    return moved


def main():
    ap = argparse.ArgumentParser(description="모음집 최종 생산(내레이션→배너→TTS→대사만 번인)")
    ap.add_argument("folder", help="원본 영상 폴더 (품번 순서 결정용)")
    ap.add_argument("--meta", help="meta_api 주소 오버라이드")
    ap.add_argument("--hold", type=float, default=None, help="배너 유지 초 (기본 config banner_hold)")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    if args.meta:
        cfg["meta_api"] = args.meta
    hold = args.hold if args.hold is not None else float(cfg.get("banner_hold", 5.0))
    codes = sorted({guess_code(v.name) for v in Path(args.folder).glob("*.mp4")} - {""})
    n = len(codes)
    styles = cfg.get("sub_styles") or P.STYLE_DEFAULT
    print(f"대상 {n}개(순서 고정) / meta={cfg['meta_api']} / banner hold={hold}s / "
          f"tts={cfg.get('tts_base')} profile={str(cfg.get('tts_profile'))[:8]}…")

    results = []
    for i, code in enumerate(codes, 1):
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        print(f"\n{'=' * 70}\n({i}/{n}) {code}", flush=True)
        if not (outdir / f"{code}_plan.json").is_file():
            results.append((code, "✘ plan 없음 — 섹션② 먼저"))
            continue
        t0 = time.time()
        step = "내레이션"
        try:
            regen_narration(outdir, cfg["meta_api"], log=em.log, seq=(i, n))

            step = "배너"
            b = stages.stage_banner(cfg, code, em, hold=hold)
            banner_note = "배너 생략" if b.get("skipped") else "배너 OK"

            step = "TTS"
            # 동일 seed 고정 — 클립·작품 간 목소리 톤 편차를 없앤다(voicebox는 seed에 따라 발성이 달라짐)
            # voicebox 백엔드가 산발적으로 죽으므로 시도 전 생존 확인 + 실패 시 재기동 후 1회 재시도
            for attempt in (1, 2, 3):
                if not ensure_voicebox(cfg["tts_base"], em.log):
                    raise RuntimeError("voicebox 재기동 실패 — 수동 확인 필요")
                try:
                    stages.stage_tts(cfg, code, cfg["tts_base"], cfg["tts_profile"],
                                     cfg.get("tts_language", "ko"), cfg.get("tts_seed"), False, em)
                    break
                except Exception as e:
                    if attempt == 3:
                        raise
                    em.log(f"⚠ TTS 실패({e}) — voicebox 상태 점검 후 재시도 {attempt}/2")

            step = "번인"
            # 대사 0줄 작품(무대사 하이라이트)은 내레이션을 숨기면 구울 자막이 없다
            # → 자막 끄고 배너·워터마크만 굽는다
            dsrt = outdir / f"{code}_대사.srt"
            has_dlg = dsrt.is_file() and bool(P.srt_parse(str(dsrt)))
            if not has_dlg:
                em.log("대사 자막 0줄 — 자막 없이 배너·워터마크만 번인")
            moved = hide_narration(outdir, code)
            try:
                stages.stage_burn(cfg, code, styles, em,
                                  parts=None if has_dlg else {"subs": False})
            finally:
                import os
                for hidden, orig in moved:
                    os.replace(hidden, orig)

            el = (time.time() - t0) / 60
            results.append((code, f"✔ 완료 ({banner_note}) {el:.1f}분"))
        except Exception as e:
            results.append((code, f"✘ {step} 실패: {e}"))
            traceback.print_exc()

    print(f"\n{'=' * 70}\n요약")
    for code, note in results:
        print(f"  {code}: {note}")
    fails = sum(1 for _, x in results if x.startswith("✘"))
    print(f"\n완료 {len(results) - fails}/{len(results)}" + (f", 실패 {fails}" if fails else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
