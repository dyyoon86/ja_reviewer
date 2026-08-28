# -*- coding: utf-8 -*-
r"""모자이크(행위 장면) 검출 probe — NudeNet 이 못 보는 사각을 메운다.

★NudeNet 은 '부위 노출'을 본다. 모자이크가 덮인 부위는 EXPOSED 로 안 잡히고,
  그래서 "전수검사 노출 0" 을 받고도 행위 장면이 그대로 납품된다(ja20 01회 실측).

원리: 모자이크는 KxK 블록 안이 평탄하고 **블록 경계에만** 계단이 선다. 즉 화소 미분의
에너지가 K 주기의 특정 위상에 몰린다. 후보 K(6~20)마다 위상별 에너지를 재서
"한 위상이 나머지보다 얼마나 튀는가"(peak ratio)를 점수로 쓴다. 가로·세로가 **같은 K**
에서 동시에 튀어야 모자이크로 본다(줄무늬 옷·블라인드 같은 1방향 패턴 오탐 제거).

타일 단위로 훑어 위치까지 잡는다 — 화면 일부만 모자이크인 게 보통이라 전체 평균으로는 묻힌다.

사용:
  .venv\Scripts\python.exe tools\_probe_mosaic.py <영상> --times 34,36,10,20
  .venv\Scripts\python.exe tools\_probe_mosaic.py <영상> --scan --step 2
"""
import argparse
import io
import subprocess
import sys

import numpy as np
from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")

KS = range(6, 21)          # 후보 블록 크기(px)
TILE = 96                  # 타일 한 변
STRIDE = 64                # 타일 이동 폭
MIN_STD = 6.0              # 평탄한 배경(하늘·흰벽)은 판단 불가라 제외
PEAK = 2.0                 # 위상 피크가 평균의 몇 배 이상이어야 격자로 보는가
FLAT = 0.30                # 블록내 표준편차 / 타일 표준편차 — 이하면 '내부가 뭉개짐'
MIN_TILES = 2              # 이 개수 이상 타일이 걸려야 프레임을 모자이크로 판정


def frame(video, t, w=0):
    # ★원본 해상도 그대로 본다. 납품본은 crop 뒤 2배 업스케일이라 모자이크 격자도 2배로
    #   커져 있는데, 여기서 되돌려 줄이면 격자가 뭉개져 검출이 안 된다(실측: 960 으로
    #   줄이면 정답 구간을 통째로 놓쳤다).
    vf = ["-vf", f"scale={w}:-1"] if w else []
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video),
                        "-frames:v", "1"] + vf +
                       ["-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
    if not r.stdout:
        return None
    return np.asarray(Image.open(io.BytesIO(r.stdout)).convert("L"), dtype=np.float32)


def _phase_peak(d, axis):
    """미분 d 의 축방향 위상 에너지에서 (최적 K, peak비율, 피크 위상)."""
    prof = d.mean(axis=1 - axis)          # 축을 따라가는 1차원 에너지 프로파일
    best = (0, 0.0, 0)
    for K in KS:
        n = (len(prof) // K) * K
        if n < K * 3:
            continue
        e = prof[:n].reshape(-1, K).mean(axis=0)     # 위상별 평균 에너지
        m = e.mean()
        if m <= 1e-6:
            continue
        ratio = e.max() / m
        if ratio > best[1]:
            best = (K, ratio, int(e.argmax()))
    return best


def _flatness(tile, K, px, py):
    """격자에 맞춰 KxK 블록으로 자르고 '블록 안이 얼마나 평탄한가'를 잰다.

    ★이게 핵심 판별자다. 주기성(위상 피크)만 보면 **h264 매크로블록**(8/16px, 게다가
      납품본은 2배 업스케일이라 16/32px)이 똑같이 걸려서 전 프레임이 양성으로 나온다.
      진짜 모자이크는 블록 내부가 단일 색으로 뭉개져 within-block 표준편차가 거의 0 인데,
      압축 블록킹은 내부 텍스처가 살아 있다.
    반환: (블록내 표준편차 중앙값) / (타일 전체 표준편차). 낮을수록 모자이크.
    """
    sub = tile[py:, px:]
    nh, nw = (sub.shape[0] // K) * K, (sub.shape[1] // K) * K
    if nh < K * 2 or nw < K * 2:
        return 1.0
    b = sub[:nh, :nw].reshape(nh // K, K, nw // K, K).transpose(0, 2, 1, 3)
    flat_b = b.reshape(-1, K * K)
    within = flat_b.std(axis=1)
    between = flat_b.mean(axis=1).std()      # 블록 '사이'의 구조
    # ★분모를 타일 전체 std 로 잡으면 안 된다 — 날아간 흰 창문/벽이 절반이면 블록 대부분이
    #   평탄해 어떤 프레임이든 0 이 나온다(실측: 정상 대화 프레임도 0.000).
    #   블록 스케일의 구조(between)가 충분할 때만, 그 대비 within 이 얼마나 작은지를 본다.
    if between < 8.0:
        return 1.0
    return float(np.median(within) / between)


def mosaic_tiles(g):
    """모자이크로 판정된 타일 목록 [(x, y, K, flatness)]."""
    h, w = g.shape
    dx = np.abs(np.diff(g, axis=1))
    dy = np.abs(np.diff(g, axis=0))
    out = []
    for y in range(0, h - TILE, STRIDE):
        for x in range(0, w - TILE, STRIDE):
            tile = g[y:y + TILE, x:x + TILE]
            if tile.std() < MIN_STD:              # 평탄한 배경(하늘·흰벽)은 판단 불가
                continue
            kx, rx, phx = _phase_peak(dx[y:y + TILE, x:x + TILE - 1], axis=1)
            ky, ry, phy = _phase_peak(dy[y:y + TILE - 1, x:x + TILE], axis=0)
            if not (kx and kx == ky and rx >= PEAK and ry >= PEAK):
                continue
            f = _flatness(tile, kx, (phx + 1) % kx, (phy + 1) % kx)
            if f <= FLAT:
                out.append((x, y, kx, round(f, 3)))
    return out


def score(video, t):
    g = frame(video, t)
    if g is None:
        return None, []
    tiles = mosaic_tiles(g)
    return (len(tiles) >= MIN_TILES), tiles


def duration(video):
    o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    return float(o.stdout.strip())


def main():
    ap = argparse.ArgumentParser(description="모자이크 검출 probe")
    ap.add_argument("video")
    ap.add_argument("--times", help="검사할 시각(초) 쉼표 구분")
    ap.add_argument("--scan", action="store_true", help="전체를 --step 간격으로 훑는다")
    ap.add_argument("--step", type=float, default=2.0)
    args = ap.parse_args()

    if args.times:
        ts = [float(x) for x in args.times.split(",") if x.strip()]
    else:
        ts = list(np.arange(0.0, duration(args.video), args.step))

    hits = []
    for t in ts:
        ok, tiles = score(args.video, t)
        if ok is None:
            print(f"{t:7.1f}s  (프레임 없음)")
            continue
        mark = "★모자이크" if ok else "        ·"
        flat = min(tiles, key=lambda z: z[3])[3] if tiles else 1.0
        print(f"{t:7.1f}s  {mark}  타일 {len(tiles):3d}  최저평탄도 {flat:.3f}")
        if ok:
            hits.append(t)
    if args.scan:
        print(f"\n모자이크 프레임 {len(hits)}/{len(ts)}")
        if hits:
            # 연속 구간으로 묶어 출력
            runs, s, p = [], hits[0], hits[0]
            for t in hits[1:]:
                if t - p <= args.step * 1.5:
                    p = t
                else:
                    runs.append((s, p)); s = p = t
            runs.append((s, p))
            for a, b in runs:
                print(f"  {a:7.1f}s ~ {b:7.1f}s  ({b - a + args.step:.0f}s)")


if __name__ == "__main__":
    main()
