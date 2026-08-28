# -*- coding: utf-8 -*-
"""노출 세그먼트 통째 제거 → 재컷 → 재자막 → 내레이션 재생성 → TTS → 마무리.

ja19 눈검사 결과 "걸리는 장면은 아예 빼라"는 지시에 따른 자동 경로다.
_recut_safe.py 는 사람이 고른 세그먼트 번호를 하드코딩했지만, 이 도구는 **NN 이
직접 화면을 보고** 뺄 세그먼트를 정한다.

★스캔은 반드시 '실제 납품되는 화면'으로 한다.
  납품본은 1080p 리프레임(위쪽 중앙 200% 확대 = crop 960x540@top)이라 하반신이
  프레임 밖으로 날아간다. 원본 전체 프레임으로 판정하면 화면에 나오지도 않는
  장면 때문에 멀쩡한 컷을 버리게 된다. 그래서 여기서는 stage_burn 과 똑같은
  crop 을 걸고 프레임을 뽑아 판정한다(config reframe_1080 이 꺼져 있으면 crop 없음).

판정 규칙(둘 중 하나만 걸려도 그 세그먼트는 통째로 버린다):
  ① 직접 노출  — NSFW_CLASSES 가 threshold 이상으로 1프레임이라도 잡히면
  ② 탈의 지속  — 살노출(ARMPITS/BELLY) 프레임 비율이 skin-ratio 이상이면(정사 구간)
     NudeNet 은 '행위'를 모르고 각도·이불로 가려지면 EXPOSED 가 안 뜬다. 옷을
     벗었다는 신호인 살노출 비율로 그 구멍을 메운다(server/core/nsfw.py 주석 참조).

남는 길이가 --min-total 미만이면 세그먼트 통째 삭제 대신 **걸린 지점 ±pad 만
도려내는 폴백**으로 전환한다(전편이 날아가는 것보다 낫다). 폴백으로 살아남은
조각은 다시 한 번 스캔해 여전히 걸리면 버린다.

사용:
  .venv\\Scripts\\python.exe tools\\_safecut.py --out "C:\\...\\ja19" --dry
  .venv\\Scripts\\python.exe tools\\_safecut.py --out "C:\\...\\ja19" --only PRWF-015
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server import pipeline as P
from server.stages import NullLock
from server.core import nsfw
from server.core.regen import regen_narration
from batch_clean import CliEmitter
from batch_produce import ensure_voicebox
from _finish_ja19 import finish_one


def crop_filter(cfg, video):
    """납품 화면과 동일한 crop 필터(리프레임이 꺼져 있으면 None)."""
    if not bool(cfg.get("reframe_1080", False)):
        return None
    from server.core.common import video_wh
    wh = video_wh(video)
    if not wh:
        return None
    crop, _ = P.reframe_crop(wh[0], wh[1], float(cfg.get("reframe_zoom", 2.0)),
                             cfg.get("reframe_align", "top"))
    return crop


def scan(video, ranges, crop, step, threshold, log=print, strict=False, width=640):
    """구간별 판정. 반환: {구간index: {'hits': [...], 'skin': 비율, 'n': 프레임수}}

    strict=True 면 `nsfw.strict_hits` 규칙(속옷 COVERED, 노출 상의 = BREAST_COVERED
    다중 검출)을 함께 본다 — "속옷 노출까지 배제" 기준용.
    ★width 를 640 으로 줄이면 작은 COVERED 영역을 놓친다. 엄격 모드는 기본 1280 을 쓴다.
    """
    det = nsfw._detector()
    vf = (crop + f",scale={width}:-1") if crop else f"scale={width}:-1"
    out = {}
    with tempfile.TemporaryDirectory() as td:
        for i, (a, b) in enumerate(ranges):
            a, b = float(a), float(b)
            ts = [a + k * step for k in range(max(1, int((b - a) / step)))]
            if ts[-1] < b - 0.2:
                ts.append(max(a, b - 0.2))
            hits, skin, n = [], 0, 0
            for k, t in enumerate(ts):
                f = os.path.join(td, f"s{i:03d}_{k:04d}.jpg")
                try:
                    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}",
                                    "-i", str(video), "-frames:v", "1", "-vf", vf, f],
                                   check=True, timeout=60)
                except Exception:
                    continue
                if not os.path.isfile(f):
                    continue
                n += 1
                try:
                    dets = det.detect(f) or []
                except Exception:
                    continue
                got_skin = False
                for x in dets:
                    cls, sc = x.get("class"), float(x.get("score", 0))
                    if cls in nsfw.NSFW_CLASSES and sc >= threshold:
                        hits.append((round(t, 2), cls, round(sc, 2)))
                    elif cls in nsfw.SKIN_CLASSES and sc >= threshold:
                        got_skin = True
                if strict:
                    for cls, sc in nsfw.strict_hits(dets):
                        hits.append((round(t, 2), cls, sc))
                if got_skin:
                    skin += 1
                os.remove(f)
            out[i] = {"hits": hits, "skin": (skin / n) if n else 0.0, "n": n}
            log(f"   구간{i + 1} {a:.0f}~{b:.0f}s  프레임{n} 노출{len(hits)} 살노출{out[i]['skin'] * 100:.0f}%")
    return out


def pick(keep, rep, skin_ratio):
    """버릴 구간 index 집합 + 사유."""
    bad = {}
    for i, r in rep.items():
        if r["hits"]:
            t, cls, sc = max(r["hits"], key=lambda x: x[2])
            bad[i] = f"노출 {len(r['hits'])}프레임(최고 {cls} {sc} @{t}s)"
        elif r["skin"] >= skin_ratio:
            bad[i] = f"탈의 지속 {r['skin'] * 100:.0f}%"
    return bad


def carve(seg, hits, pad, min_clip):
    """세그먼트에서 걸린 지점 ±pad 만 도려낸 조각들."""
    a, b = float(seg[0]), float(seg[1])
    bads = sorted((max(a, t - pad), min(b, t + pad)) for t, _, _ in hits)
    merged = []
    for s, e in bads:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    out, cur = [], a
    for s, e in merged:
        if s - cur >= min_clip:
            out.append([round(cur, 2), round(s, 2)])
        cur = max(cur, e)
    if b - cur >= min_clip:
        out.append([round(cur, 2), round(b, 2)])
    return out


def snapshot_tts(outdir, code, em):
    """재컷 전에 내레이션 문장·음성 클립을 떠 둔다(재사용용).

    ★invalidate_derived 가 {code}_tts 를 통째로 지운다 — 재컷 직전에 떠 놓지 않으면
      멀쩡한 음성을 버리고 TTS 를 다시 돌게 된다. 반환: {정규화문장: wav경로}
    """
    srt = outdir / f"{code}_내레이션.srt"
    clipdir = outdir / f"{code}_tts"
    if not (srt.is_file() and clipdir.is_dir()):
        return {}
    keep_dir = outdir / f"{code}_tts_prev"
    if keep_dir.is_dir():
        shutil.rmtree(keep_dir, ignore_errors=True)
    shutil.copytree(clipdir, keep_dir)
    out = {}
    for i, (st, en, tx) in enumerate(P.srt_parse(srt), 1):
        w = keep_dir / f"n{i:03d}.wav"
        if w.is_file():
            out[" ".join(str(tx).split())] = str(w)
    em.log(f"기존 내레이션 {len(out)}문장 스냅샷(재사용 대기)")
    return out


def reuse_tts(outdir, code, snap, em):
    """새 내레이션 SRT 순서에 맞춰 스냅샷 클립을 다시 깐다.
    문장이 하나라도 안 맞으면 False — 호출부가 TTS 로 넘어간다."""
    srt = outdir / f"{code}_내레이션.srt"
    entries = P.srt_parse(srt) if srt.is_file() else []
    if not entries or not snap:
        return False
    picked = []
    for st, en, tx in entries:
        w = snap.get(" ".join(str(tx).split()))
        if not w:
            em.log(f"⚠ 재사용 실패 — 새 문장 발견: {str(tx)[:24]}…")
            return False
        picked.append(w)
    clipdir = outdir / f"{code}_tts"
    clipdir.mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(picked, 1):
        shutil.copy2(src, clipdir / f"n{i:03d}.wav")
    em.log(f"✔ 기존 음성 {len(picked)}문장 재사용 — TTS 건너뜀")
    return True


def in_keep(t, keep):
    return any(a <= t <= b for a, b in keep)


def main():
    ap = argparse.ArgumentParser(description="노출 구간 제거 재컷")
    ap.add_argument("--out", help="out_dir 오버라이드")
    ap.add_argument("--only", default="", help="이 품번만(쉼표 구분)")
    ap.add_argument("--skip", default="", help="제외할 품번(쉼표 구분)")
    ap.add_argument("--threshold", type=float, default=0.22, help="NN 임계(기본 0.22 — 기본값보다 빡세게)")
    ap.add_argument("--step", type=float, default=0.5, help="프레임 샘플 간격(초)")
    ap.add_argument("--skin-ratio", type=float, default=0.50, help="탈의 판정 살노출 비율")
    ap.add_argument("--pad", type=float, default=2.0, help="폴백 도려내기 여유(초)")
    ap.add_argument("--min-clip", type=float, default=5.0, help="폴백 조각 최소 길이(초)")
    ap.add_argument("--min-total", type=float, default=35.0, help="이 길이 밑이면 폴백 전환")
    ap.add_argument("--duck", type=float, default=0.3, help="해설 중 원음 볼륨")
    ap.add_argument("--regen-nar", action="store_true",
                    help="내레이션을 새로 쓴다(TTS 전량 재생성). 기본은 기존 문장·음성 재사용")
    ap.add_argument("--carve-first", action="store_true",
                    help="세그먼트 통째 삭제 대신 처음부터 걸린 지점만 도려낸다")
    ap.add_argument("--strict", action="store_true",
                    help="속옷 노출·노출 의상까지 배제(nsfw.strict_hits 규칙 추가)")
    ap.add_argument("--width", type=int, default=0,
                    help="판정 프레임 폭(0=자동: 기본 640, --strict 면 1280)")
    ap.add_argument("--dry", action="store_true", help="판정만 하고 재컷하지 않음")
    args = ap.parse_args()
    W = args.width or (1280 if args.strict else 640)

    cfg = _common.load_cfg()
    if args.out:
        cfg["out_dir"] = args.out
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}
    skip = {c.strip().upper() for c in args.skip.split(",") if c.strip()}

    root = Path(cfg["out_dir"])
    codes = sorted(d.name for d in root.iterdir()
                   if d.is_dir() and not d.name.startswith("_")
                   and (d / f"{d.name}_plan.json").is_file())
    codes = [c for c in codes if c not in skip and (not only or c in only)]
    print(f"대상 {len(codes)}개 / out_dir={root} / 임계 {args.threshold} · 간격 {args.step}s "
          f"· 탈의 {args.skin_ratio * 100:.0f}%" + (" / DRY-RUN" if args.dry else ""))

    results = []
    for i, code in enumerate(codes, 1):
        outdir = stages.work_dir(cfg, code)
        em = CliEmitter(code)
        print(f"\n{'=' * 70}\n({i}/{len(codes)}) {code}", flush=True)
        t0 = time.time()
        try:
            planf = outdir / f"{code}_plan.json"
            plan = json.loads(planf.read_text(encoding="utf-8"))
            keep = [[float(a), float(b)] for a, b in P.parse_keep(plan.get("keep", []))]
            if not keep:
                raise RuntimeError("keep 비어 있음")
            st = stages.load_state(outdir, code)
            clean = st.get("video") or str(outdir / f"{code}_클린.mp4")
            if not os.path.isfile(clean):
                raise RuntimeError(f"클린본 없음: {clean}")

            crop = crop_filter(cfg, clean)
            em.log(f"납품 화면 기준 스캔 — crop={crop or '없음(리프레임 OFF)'}")
            rep = scan(clean, keep, crop, args.step, args.threshold, em.log,
                       strict=args.strict, width=W)
            bad = pick(keep, rep, args.skin_ratio)

            total_old = sum(b - a for a, b in keep)
            new_keep = [seg for j, seg in enumerate(keep) if j not in bad]
            mode = "세그먼트 삭제"
            total_new = sum(b - a for a, b in new_keep)
            if bad and (args.carve_first or total_new < args.min_total):
                # 걸린 지점만 도려낸다.
                # ★기본은 '세그먼트 통째 삭제'인데, 노출이 한두 프레임만 스치는 컷까지
                #   통째로 버려 멀쩡한 대화 수십 초가 같이 날아간다(ja20 03회: 6구간 중
                #   바니수트가 보이는 건 일부인데 얼굴 클로즈업 컷까지 함께 삭제됐다).
                #   --carve-first 면 처음부터 도려내기로 간다 — 판정 오탐이 나도 ±pad 만
                #   잘려 피해가 작다. 도려낸 조각은 어차피 아래에서 재검사한다.
                mode = ("부분 도려내기" if args.carve_first else "부분 도려내기(폴백)")
                new_keep = []
                for j, seg in enumerate(keep):
                    if j not in bad:
                        new_keep.append(seg); continue
                    pieces = carve(seg, rep[j]["hits"], args.pad, args.min_clip)
                    if not pieces:
                        continue
                    # 살아남은 조각 재검사 — 여전히 걸리면 버린다
                    rep2 = scan(clean, pieces, crop, args.step, args.threshold, em.log,
                                strict=args.strict, width=W)
                    bad2 = pick(pieces, rep2, args.skin_ratio)
                    new_keep += [p for k, p in enumerate(pieces) if k not in bad2]
                new_keep.sort()
                total_new = sum(b - a for a, b in new_keep)

            for j, why in sorted(bad.items()):
                em.log(f"🚫 구간{j + 1} {keep[j][0]:.0f}~{keep[j][1]:.0f}s 제거 — {why}")
            em.log(f"keep {len(keep)}→{len(new_keep)}구간, {total_old:.0f}s→{total_new:.0f}s ({mode})")

            if not bad:
                results.append((code, "· 노출 없음 — 재컷 불필요", time.time() - t0)); continue
            if not new_keep:
                results.append((code, "✘ 남는 구간 없음 — 사람이 판단 필요", time.time() - t0)); continue
            if args.dry:
                results.append((code, f"(dry) {len(bad)}구간 제거 예정 → {total_new:.0f}s",
                                time.time() - t0)); continue

            # ── 재컷부터는 되돌릴 수 없다 — plan 백업 후 진행
            bak = outdir / f"{code}_plan.json.presafe"
            if not bak.is_file():
                bak.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
            plan["keep"] = new_keep
            plan["dialogue"] = [d for d in plan.get("dialogue", [])
                                if in_keep(d.get("start", 0), new_keep)]
            plan["narration"] = [n for n in plan.get("narration", [])
                                 if in_keep(n.get("start", 0), new_keep)]
            if not plan["narration"]:
                plan["narration"] = [{"start": new_keep[0][0], "end": new_keep[0][0] + 3,
                                      "text": (plan.get("summary") or "")[:40] or code,
                                      "style": "기본"}]
            planf.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

            snap = {} if args.regen_nar else snapshot_tts(outdir, code, em)
            final = str(outdir / f"{code}_final.mp4")
            em.log("재컷 중…")
            with NullLock():
                P.cut_video(clean, new_keep, final, em.log, lambda fr: None)
            P.invalidate_derived(outdir, code, em.log)
            # 재컷하면 오디오가 새로 나오므로 BGM 제거 마커도 무효다
            marker = outdir / f"{code}_final.nobgm"
            if marker.is_file():
                marker.unlink()

            # 기본 경로: 내레이션을 새로 쓰지 않는다. 잘려나간 구간의 문장은
            # plan 필터에서 이미 빠졌고, 살아남은 문장은 stage_subs 가 새 타임라인으로
            # 재매핑한다 — 문장이 그대로면 음성도 그대로 쓸 수 있다(TTS 0회).
            # --regen-nar 를 주면 길이에 맞춰 슬롯을 다시 계산해 통째로 새로 쓴다.
            stages.stage_subs(cfg, code, em)
            if args.regen_nar:
                regen_narration(outdir, cfg["meta_api"], log=em.log)
            stages.stage_banner(cfg, code, em, hold=float(cfg.get("banner_hold", 5.0)))
            if not args.regen_nar and reuse_tts(outdir, code, snap, em):
                pass
            else:
              for attempt in (1, 2, 3):
                  if not ensure_voicebox(cfg["tts_base"], em.log):
                      raise RuntimeError("voicebox 재기동 실패")
                  try:
                      stages.stage_tts(cfg, code, cfg["tts_base"], cfg["tts_profile"],
                                       cfg.get("tts_language", "ko"), cfg.get("tts_seed"), False, em)
                      break
                  except Exception as e:
                      if attempt == 3:
                          raise
                      em.log(f"⚠ TTS 재시도 {attempt}/2 ({e})")

            sub = finish_one(cfg, code, em, duck=args.duck)
            sz = sub.stat().st_size / 1e6 if sub.is_file() else 0
            results.append((code, f"✔ {len(bad)}구간 제거 → {total_new:.0f}s ({sz:.0f}MB)",
                            time.time() - t0))
        except Exception as e:
            traceback.print_exc()
            results.append((code, f"✘ {e}", time.time() - t0))

    print(f"\n{'=' * 70}\n요약")
    for code, note, el in results:
        print(f"  {code}: {note} ({el / 60:.1f}분)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
