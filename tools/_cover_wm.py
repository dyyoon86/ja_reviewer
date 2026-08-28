# -*- coding: utf-8 -*-
r"""납품본 좌상단에 박힌 **타 사이트 워터마크**를 채널 로고 pill 로 덮는다.

소스에 다른 사이트 워터마크가 박힌 파일이 섞여 들어온다(ja18 SNOS-334, ja20 START-627·SDMM-238).
좌상단이라 1080p 리프레임(crop=960:540:160:0 → 2배)을 거치면 앞글자는 잘려나가고 꼬리
("…COM")만 남는데, 그게 오히려 흰 글씨라 눈에 더 띈다. 재컷으로는 절대 안 없어진다.

덮개는 인트로 인포카드에 이미 들어있는 **딸딸기튜브 코너 로고 pill** 을 그대로 쓴다.
다만 pill 의 불투명 왼쪽 끝이 x=56 이라 워터마크(x=22~156)의 왼쪽 34px 이 새어 나온다.
→ pill 몸통(딸기와 글자 사이 평평한 구간)에 같은 열을 N 번 복제해 **왼쪽으로만 늘린 뒤**
  그만큼 왼쪽으로 민다. 딸기·글자 위치와 오른쪽 끝은 그대로고 왼쪽만 액자 테두리까지 닿는다.
  액자 테두리 폭(x<22)은 알파를 0 으로 비워 원본 핑크 테두리가 그대로 보이게 한다.

★ 인트로(0~5.6s) 구간에는 인포카드가 그린 pill 이 이미 구워져 있다. 덮개가 그 pill 을
  **완전히 덮도록** 오른쪽 끝을 유지하는 게 중요하다 — 폭이 모자라면 두 개가 겹쳐 보인다.
★ 대상은 리프레임까지 끝난 최종본(1920×1080)이다. 인포카드 PNG 도 같은 좌표계라 스케일
  왕복이 없다. 720p 중간산출물에 적용하면 안 된다.

검증은 스스로 한다: 여러 프레임의 하이패스 중앙값(= 모든 프레임에 공통인 정적 오버레이)으로
워터마크 bbox 를 재고, 덮개 알파가 그 픽셀을 전부 가리는지 확인한 뒤에만 인코딩한다.
인코딩 후 같은 스캔을 다시 돌려 사라졌는지 확인한다.

사용: .venv\Scripts\python.exe tools\_cover_wm.py --out "F:\ja_reviewer_out\ja20" ^
          --codes START-627,SDMM-238
      (--dry 면 덮개 PNG 와 검증만, 인코딩 안 함)
"""
import argparse
import io
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server.core.cutter import has_nvenc, _vcodec_args
from server.core.common import video_duration

LOGO_H = 220        # 인포카드 PNG 좌상단에서 코너 로고만 들어있는 행 범위
LOGO_W = 460
TUCK = 14           # 액자 테두리 안쪽으로 밀어 넣을 여유(px)


# ────────────────────────────── 워터마크 탐지 ──────────────────────────────
def _crop_frame(video, t, w, h):
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video),
                        "-frames:v", "1", "-vf", f"crop={w}:{h}:0:0",
                        "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
    if not r.stdout:
        return None
    return np.asarray(Image.open(io.BytesIO(r.stdout)).convert("L"), dtype=np.float32)


def static_overlay(video, w=560, h=260, n=20):
    """프레임마다 하이패스를 뜬 뒤 **중앙값**을 취한다.
    모든 프레임에 공통인 성분(=고정 오버레이)만 남고 움직이는 장면은 상쇄된다."""
    dur = float(video_duration(str(video)))
    a = 8.0 if dur > 20 else 1.0            # 인트로 인포카드 구간은 피한다
    b = max(a + 1.0, dur - 2.0)
    hs = []
    for t in np.linspace(a, b, n):
        g = _crop_frame(video, t, w, h)
        if g is None:
            continue
        bl = np.asarray(Image.fromarray(g.astype(np.uint8)).filter(ImageFilter.GaussianBlur(9)),
                        dtype=np.float32)
        hs.append(g - bl)
    if not hs:
        raise RuntimeError(f"프레임을 못 읽었습니다: {video}")
    return np.median(np.stack(hs), axis=0)


def _own_overlay(frame_png, shape):
    """우리 액자 프레임이 그리는 픽셀(핑크 테두리 + 흰 실선)을 2px 부풀려 돌려준다.

    ★ 프레임 PNG 에는 테두리(x 0~21) 말고도 **반투명 흰 실선**(x 34~35)이 들어 있다.
      이것도 모든 프레임에 공통이라 정적 오버레이 스캔에 잡힌다 — 빼지 않으면
      우리 디자인을 타사 워터마크로 오인해 "덮개가 못 가린다"고 잘못 멈춘다.
    """
    al = np.asarray(Image.open(frame_png).convert("RGBA"))[:, :, 3][:shape[0], :shape[1]]
    own = al > 8
    for dx in (-2, -1, 1, 2):                       # 안티에일리어싱 가장자리까지
        own |= np.roll(own, dx, axis=1)
    for dy in (-2, -1, 1, 2):
        own |= np.roll(own, dy, axis=0)
    return own


def wm_mask(video, frame_png, border, cover_alpha=None, y0=45, y1=140, x1=260):
    """액자 테두리 오른쪽(x>=border)에서 **우리 것이 아닌** 고정 오버레이만 남긴 마스크.

    cover_alpha 를 주면 덮개 pill 도 우리 것으로 친다 — 덮은 **뒤** 재검사할 때는
    pill 자체가 정적 오버레이라 이걸 안 빼면 항상 "아직 남음"으로 나온다.
    """
    m = static_overlay(video)
    keep = np.zeros_like(m, bool)
    keep[y0:y1, border:x1] = True
    own = _own_overlay(frame_png, m.shape)
    if cover_alpha is not None:
        own |= cover_alpha[:m.shape[0], :m.shape[1]] > 8
    return (m > 12) & keep & ~own


# ────────────────────────────── 덮개 PNG ──────────────────────────────
def border_width(frame_png):
    """액자 프레임 PNG 의 왼쪽 불투명 테두리 폭."""
    al = np.asarray(Image.open(frame_png).convert("RGBA"))[:, :, 3]
    row = al[al.shape[0] // 2]
    w = 0
    while w < len(row) and row[w] >= 250:
        w += 1
    return w


def build_cover(info_png, frame_png, out_png):
    """인포카드의 코너 로고 pill 을 왼쪽으로 늘려 액자 테두리까지 닿게 만든다."""
    a = np.asarray(Image.open(info_png).convert("RGBA"))
    logo = a[:LOGO_H, :LOGO_W].copy()
    al = logo[:, :, 3]
    ys, xs = np.nonzero(al >= 250)
    if len(xs) == 0:
        raise RuntimeError(f"코너 로고를 못 찾았습니다: {info_png}")
    pill_l, pill_r, pill_t, pill_b = xs.min(), xs.max(), ys.min(), ys.max()

    border = border_width(frame_png)
    shift = int(pill_l - (border - TUCK))            # 테두리 안쪽까지 밀어 넣는다
    if shift <= 0:
        raise RuntimeError(f"pill 이 이미 테두리에 닿아 있습니다(left={pill_l}, border={border})")

    # 이음매 = 딸기 오른쪽의 '평평한 몸통'(인접 열 차이가 가장 작은 곳)
    lo, hi = pill_l + 60, min(pill_l + 150, pill_r - 40)
    band = logo[pill_t:pill_b].astype(np.int16)
    seam = min(range(lo, hi), key=lambda x: np.abs(band[:, x] - band[:, x + 1]).mean())

    col = logo[:, seam:seam + 1, :]
    wide = np.concatenate([logo[:, :seam], np.repeat(col, shift, axis=1), logo[:, seam:]],
                          axis=1)[:, shift:]         # 늘린 뒤 그만큼 왼쪽으로
    wide[:, :border, 3] = 0                          # 액자 테두리는 원본이 보이게 비운다

    cover = np.zeros_like(a)
    cover[:LOGO_H, :wide.shape[1]] = wide
    Image.fromarray(cover).save(out_png)
    return cover[:, :, 3], dict(pill=(pill_l, pill_t, pill_r, pill_b), border=border,
                                seam=seam, shift=shift)


# ────────────────────────────── 굽기 ──────────────────────────────
def overlay(video, cover_png, out_video, log=print):
    enc = _vcodec_args(has_nvenc())
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(video), "-i", str(cover_png),
           "-filter_complex", "[0][1]overlay=0:0:format=auto[v]",
           "-map", "[v]", "-map", "0:a?"] + enc + ["-c:a", "copy", str(out_video)]
    log("  ffmpeg 덮개 합성" + (" (GPU·NVENC)" if has_nvenc() else " (CPU·libx264)") + " ...")
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser(description="납품본 좌상단 타사 워터마크를 채널 로고로 덮는다")
    ap.add_argument("--out", required=True, help="배치 out_dir (예: F:\\ja_reviewer_out\\ja20)")
    ap.add_argument("--codes", required=True, help="품번 콤마 구분")
    ap.add_argument("--dry", action="store_true", help="덮개 PNG·검증만, 인코딩 안 함")
    ap.add_argument("--check", action="store_true", help="이미 덮은 파일 재검사만")
    args = ap.parse_args()

    out = Path(args.out)
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    done, failed = [], []

    for code in codes:
        print(f"\n═══ {code} ═══", flush=True)
        ic = out / f"_infocard_{code}"
        info_png, frame_png = ic / f"{code}_인포카드.png", ic / f"{code}_프레임.png"
        src = out / "_완성" / f"{code}.mp4"
        if not (info_png.is_file() and frame_png.is_file() and src.is_file()):
            print("  ✗ 인포카드/프레임 PNG 또는 _완성 파일이 없습니다 — 건너뜀")
            failed.append(code)
            continue

        cover_png = ic / f"{code}_코너로고_덮개.png"
        alpha, geo = build_cover(info_png, frame_png, cover_png)
        print(f"  pill {geo['pill']} · 테두리 {geo['border']}px · 이음매 x={geo['seam']} "
              f"· 왼쪽으로 {geo['shift']}px 확장")

        if args.check:                    # 이미 덮은 파일을 재검사만 (인코딩 없음)
            after = wm_mask(src, frame_png, geo["border"], cover_alpha=alpha)
            print(f"  덮개 밖에 남은 고정 오버레이 {after.sum()}px "
                  + ("✓ 워터마크 없음" if after.sum() == 0 else "★ 남아 있음"))
            (done if after.sum() == 0 else failed).append(code)
            continue

        mask = wm_mask(src, frame_png, geo["border"])
        if mask.sum() == 0:
            print("  · 좌상단에 고정 오버레이가 없습니다 — 워터마크 없음으로 보고 건너뜀")
            continue
        ys, xs = np.nonzero(mask)
        print(f"  워터마크 bbox x {xs.min()}~{xs.max()} y {ys.min()}~{ys.max()} ({mask.sum()}px)")

        h, w = mask.shape
        leak = mask & (alpha[:h, :w] < 250)
        if leak.sum():
            ly, lx = np.nonzero(leak)
            print(f"  ✗ 덮개가 {leak.sum()}px 를 못 가립니다 (x {lx.min()}~{lx.max()} "
                  f"y {ly.min()}~{ly.max()}) — 인코딩 중단")
            failed.append(code)
            continue
        print("  ✓ 덮개 알파가 워터마크를 전부 가림")

        if args.dry:
            continue

        bak = src.with_suffix(".mp4.bak_wm")
        if not bak.exists():
            shutil.copy2(src, bak)
        tmp = src.with_name(f"{src.stem}_덮개.part.mp4")   # ★확장자는 .mp4 여야 muxer가 잡힌다
        overlay(src, cover_png, tmp)
        tmp.replace(src)

        after = wm_mask(src, frame_png, geo["border"], cover_alpha=alpha)
        print(f"  재검사: 덮개 밖에 남은 고정 오버레이 {after.sum()}px "
              + ("✓ 워터마크 사라짐" if after.sum() == 0 else "★ 아직 남음 — 확인 필요"))

        for d in sorted((out / "_납품").glob(f"*_{code}_*.mp4")):
            shutil.copy2(src, d)
            print(f"  ▸ 납품본 갱신: {d.name}")
        stages.worklog(out / code, code, "좌상단 타사 워터마크를 채널 로고 덮개로 처리")
        done.append(code)

    print(f"\n완료 {len(done)}건 {done}" + (f" / 실패 {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
