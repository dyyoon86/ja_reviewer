# -*- coding: utf-8 -*-
r"""납품본(1920×1080) **양쪽 위 모서리**의 타사 워터마크를 가린다.

ja22 실측: SUPJAV 워터마크가 장면마다 자리를 옮긴다. 2배 리프레임(상단 정렬) 뒤에는
  · 좌상단에 꼬리 ".COM"  (x 22~155, y 50~120)
  · 우상단 카드 뒤에 머리 "SUP" (x 1765~1898, y 50~115)
가 장면에 따라 번갈아 보인다. `_cover_wm.py` 는 **모든 프레임에 고정된** 오버레이만
중앙값으로 찾으므로 이렇게 움직이는 워터마크는 "없음"으로 건너뛴다. 모서리 글씨 탐지도
시도했지만 우리 카드 글씨·액자 실선에 똑같이 반응해 쓸 수 없었다 → **12편 모두 덮는다**
(덮개가 불필요하게 들어가도 해가 없고, 빠뜨리면 흰 글씨가 그대로 나간다).

  · 좌상단(전 구간): `_cover_wm.build_cover` 의 채널 로고 pill(액자 테두리까지 늘린 것)
  · 우상단(워터마크 카드가 뜬 뒤): 카드 PNG 를 **한 번 더** 얹어 반투명(≈85%)을 ≈98% 로
      만들고, 카드 오른쪽 끝~액자 테두리 틈을 같은 색으로 메운다. 카드 뒤로 비치던 "SUP" 이
      사라지고 카드가 테두리까지 이어진 모양이 된다(불투명 패치를 따로 붙이면 색이 달라 튄다).
  · 우상단(인트로 인포카드 구간, 카드 없음): 좁은 영역만 delogo 로 뭉갠다(약 5초).

★재번인하면 덮개가 날아간다 — 번인 뒤에 돌릴 것. 원본은 `.mp4.bak_corner`, 완료 마커는
  `{code}/{code}.corner`(재실행 시 건너뜀, --redo 로 강제).

사용:
  .venv\Scripts\python.exe tools\_cover_corners.py --out F:\ja_reviewer_out\ja22
  .venv\Scripts\python.exe tools\_cover_corners.py --out ... --codes FNS-256,FNS-257 [--dry] [--redo]
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401
from server import stages
from server.core.cutter import has_nvenc, _vcodec_args
from _cover_wm import build_cover, border_width

INTRO_DELOGO = (1742, 47, 154, 150)  # x, y, w, h — 인트로 동안 "SUP"·제작사 로고 자리


def gap_patch(wm_png, frame_png, out_png):
    """카드 오른쪽 끝(둥근 모서리 포함)~액자 안쪽 테두리를 카드와 같은 색으로 메운 RGBA."""
    wm = np.asarray(Image.open(wm_png).convert("RGBA"))
    H, W = wm.shape[:2]
    ys, xs = np.nonzero(wm[:, :, 3] > 40)
    if not len(xs):
        raise RuntimeError(f"워터마크 카드를 못 찾았습니다: {wm_png}")
    cx1, cy0, cy1 = xs.max(), ys.min(), ys.max()
    sample = wm[cy0 + 12:cy0 + 60, cx1 - 150:cx1 - 20].reshape(-1, 4)
    rgb = np.median(sample[:, :3], axis=0).astype(np.uint8)
    a = float(np.median(sample[:, 3])) / 255.0
    a2 = int(round((1 - (1 - a) ** 2) * 255))          # 카드를 두 겹 얹었을 때와 같은 농도
    x_end = W - border_width(frame_png)
    patch = np.zeros_like(wm)
    # ★카드 두 겹(≈95%)으로는 흰 "SUP" 이 여전히 비쳤다(ja22 FNS-256 실측). "SUP" 자리
    #   (x 1765~)에는 한 겹을 더 얹어 ≈99.5% 로 만들고, 왼쪽 60px 은 0→230 으로 번지게 해
    #   카드 안에서 농도 경계가 보이지 않게 한다. 카드 끝~테두리 틈은 두 겹 농도로 메운다.
    #   세로는 "SUP" 높이(~y128)까지만 — 그 아래 카드 셋째 줄(발매일·3사이즈·키)이
    #   오른쪽 끝까지 이어져 있어 덮으면 글자가 먹힌다.
    x_sup, y_sup = 1705, min(cy1, 128)
    patch[cy0:y_sup, x_sup:x_end, :3] = rgb
    patch[cy0:y_sup, x_sup:x_sup + 60, 3] = np.linspace(0, 250, 60).astype(np.uint8)
    patch[cy0:y_sup, x_sup + 60:x_end, 3] = 250
    patch[y_sup:cy1 + 1, cx1 - 28:x_end, :3] = rgb
    patch[cy0:cy1 + 1, cx1 - 28:x_end, 3] = np.maximum(
        patch[cy0:cy1 + 1, cx1 - 28:x_end, 3], a2)
    Image.fromarray(patch).save(out_png)
    return dict(card_right=int(cx1), top=int(cy0), bottom=int(cy1), alpha=a, alpha2=a2)


def burn(src, left_png, wm_png, patch_png, t_card, out):
    x, y, w, h = INTRO_DELOGO
    fc = (f"[0:v]delogo=x={x}:y={y}:w={w}:h={h}:enable='lt(t,{t_card})'[d];"
          f"[d][1:v]overlay=0:0:format=auto[l];"
          f"[l][2:v]overlay=0:0:format=auto:enable='gte(t,{t_card})'[c];"
          f"[c][3:v]overlay=0:0:format=auto:enable='gte(t,{t_card})'[v]")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-i", str(left_png), "-i", str(wm_png), "-i", str(patch_png),
           "-filter_complex", fc, "-map", "[v]", "-map", "0:a?"] + \
        _vcodec_args(has_nvenc()) + ["-c:a", "copy", str(out)]
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser(description="납품본 양쪽 위 모서리 타사 워터마크 덮기")
    ap.add_argument("--out", required=True)
    ap.add_argument("--codes", default="", help="품번 콤마 구분(생략 시 _완성 전체)")
    ap.add_argument("--dry", action="store_true", help="덮개 PNG 만 만들고 인코딩 안 함")
    ap.add_argument("--redo", action="store_true", help="마커가 있어도 다시(백업본 기준)")
    args = ap.parse_args()

    cfg = _common.load_cfg()
    # 워터마크 카드는 인포카드 hold 끝에 페이드인한다(subs.banner: hold → fade 0.5s)
    t_card = float(cfg.get("banner_hold", 5.0)) + 0.6
    out = Path(args.out)
    done_dir = out / "_완성"
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or \
        sorted(p.stem for p in done_dir.glob("*.mp4"))
    done, skipped, failed = [], [], []
    for code in codes:
        src = done_dir / f"{code}.mp4"
        ic = out / f"_infocard_{code}"
        info_png, frame_png = ic / f"{code}_인포카드.png", ic / f"{code}_프레임.png"
        wm_png = ic / f"{code}_워터마크_tr.png"
        marker = out / code / f"{code}.corner"
        print(f"\n═══ {code} ═══", flush=True)
        if not all(p.is_file() for p in (src, info_png, frame_png, wm_png)):
            print("  ✗ _완성 파일 또는 인포카드/프레임/워터마크 PNG 없음")
            failed.append(code)
            continue
        bak = src.with_suffix(".mp4.bak_corner")
        if marker.is_file() and not args.redo:
            print("  · 이미 덮음(마커) — 건너뜀")
            skipped.append(code)
            continue
        left_png = ic / f"{code}_코너로고_덮개.png"
        _alpha, geo = build_cover(info_png, frame_png, left_png)
        patch_png = ic / f"{code}_우상단_틈.png"
        g2 = gap_patch(wm_png, frame_png, patch_png)
        print(f"  좌: pill {geo['shift']}px 확장 · 우: 카드 끝 x={g2['card_right']} "
              f"알파 {g2['alpha']:.2f}→{g2['alpha2'] / 255:.2f} · 카드 등장 {t_card:.1f}s")
        if args.dry:
            continue
        if not bak.exists():
            shutil.copy2(src, bak)
        base = bak if args.redo else src
        tmp = src.with_name(f"{src.stem}_모서리.part.mp4")
        burn(base, left_png, wm_png, patch_png, t_card, tmp)
        tmp.replace(src)
        marker.write_text("covered", encoding="utf-8")
        stages.worklog(out / code, code, "양쪽 위 모서리 타사 워터마크 덮개(_cover_corners)")
        done.append(code)

    print(f"\n덮음 {len(done)} {done} / 건너뜀 {len(skipped)}"
          + (f" / 실패 {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
