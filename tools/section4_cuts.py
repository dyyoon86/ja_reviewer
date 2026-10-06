# -*- coding: utf-8 -*-
"""섹션4 인트로 소재 컷 — 납품본에서 짧은 컷·모자이크·정지프레임·얼굴을 미리 뽑아 둔다.

2026-10-07 "인트로가 지루하다" → 텍스트 위 영상이 아니라 **영상 위 텍스트**로 뒤집었다.
씬마다 바탕이 영상이 되니 소재가 많이 필요한데, 납품본 통째(편당 30~150MB)를 그대로 넣고
data-media-start 로 시킹하면 렌더가 느리고 되감기도 불안하다 → 필요한 구간만 잘라
**전 프레임 키프레임(-g 1)** 으로 저장해 어느 시점에서 열어도 그 프레임이 바로 나오게 한다.

소스는 `{code}/{code}_final.mp4`(1280x720, 배너·자막 없음, 눈검사 반영된 클린 컷)다.
`_완성/*.mp4` 는 배너가 구워져 있어 풀프레임으로 쓰면 남의 배너가 뜬다.

컷 위치는 편 길이의 비율로 잡는다(짧은 편도 안전). `_인트로/몽타주.json` {"품번": 초} 가
있으면 1씬 컷(A)의 시작점을 그 값으로 덮어쓴다(야한 순간 회피용, 기존 규약 유지).

산출(assets/cuts/):
  {code}_a.mp4       1씬 풀프레임 몽타주용 0.9s
  {code}_b.mp4       2씬 배경·4씬 카운트다운 플립용 1.2s
  top1_mos{N}.mp4    3씬 1위 모자이크 3단계(거침→고움), 같은 구간을 N=40/22/10 블록으로
  top1_still.jpg     5씬 1위 정지프레임
  {code}_face.jpg    2씬 얼굴 타일(_L_wm.html 의 base64; 마스코트 폴백이면 없음)
"""
import base64
import json
import re
import subprocess
from pathlib import Path

A_LEN, B_LEN, C_LEN = 0.9, 1.2, 4.2
MOSAIC = (40, 22, 10)          # 1920 폭 기준 블록 수 — 클수록 거칠다(40 = 48px 블록)
# ★클린 컷(_final.mp4)은 제작사 로고가 그대로다(우상단 IPPA·MOODYZ, 좌하단 SUPJAV.COM, 상단
#   SODSTAR…). 납품본은 리프레임(960x540@160,0 → 2배)으로 좌우·하단을 잘라내고 남은 모서리를
#   덮개로 가리는데, 인트로엔 덮개가 없다 → 같은 리프레임 + 양쪽 위 모서리 delogo(비네트 아래라
#   번짐이 안 보인다). 첫 렌더(2026-10-07)에서 로고가 그대로 나와 넣었다.
REFRAME = ("crop=960:540:160:0,delogo=x=2:y=2:w=160:h=110,delogo=x=790:y=2:w=166:h=135,"
           "scale=1920:1080:flags=lanczos")
_RE_FACE = re.compile(r"background-image:url\((data:image/(\w+);base64,([^)]*))\)")


def _dur(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _ff(args):
    return subprocess.run(["ffmpeg", "-y", "-v", "error", *args]).returncode == 0


def _cut(src, dst, at, length, vf=REFRAME):
    # -ss 를 -i 앞에: 빠른 시킹. -g 1 로 전 프레임 I-frame — 렌더러가 임의 시점에서 바로 연다.
    return _ff(["-ss", f"{at:.3f}", "-i", str(src), "-t", f"{length:.3f}", "-an",
                "-vf", vf + ",fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "fast",
                "-crf", "17", "-g", "1", "-movflags", "+faststart", str(dst)])


def _face(icdir, dst):
    f = Path(icdir) / "_L_wm.html"
    if not f.is_file():
        return False
    mo = _RE_FACE.search(f.read_text(encoding="utf-8"))
    if not mo or mo.group(2) == "png":      # png = 딸기 마스코트 폴백(얼굴 아님)
        return False
    Path(dst).write_bytes(base64.b64decode(mo.group(3)))
    return True


def prep(out, rows, assets, log=print):
    """rows(순위·품번) → 각 row 에 cut_a/cut_b/face 를, 1위에는 mos/still 을 덧붙여 돌려준다.
    이미 있는 산출물은 소스보다 새로우면 재사용한다."""
    out, assets = Path(out), Path(assets)
    cuts = assets / "cuts"
    cuts.mkdir(parents=True, exist_ok=True)
    mj = out / "_인트로" / "몽타주.json"
    mpick = json.loads(mj.read_text(encoding="utf-8")) if mj.is_file() else {}
    res = []
    for r in rows:
        c = r["code"]
        src = out / c / f"{c}_final.mp4"
        if not src.is_file():
            src = out / "_완성" / f"{c}.mp4"      # 폴백(배너 있음) — 없는 것보단 낫다
        if not src.is_file():
            log(f"  [!] {c}: 클린 컷 없음 — 소재 제외")
            res.append({**r, "cut_a": None, "cut_b": None, "face": None})
            continue
        D = _dur(src)
        ta = max(0.3, min(D * 0.30, D - A_LEN - 0.2))
        if c in mpick and mpick[c] is not None:
            ta = max(0.0, min(float(mpick[c]), D - A_LEN - 0.1))
        tb = max(0.3, min(D * 0.58, D - B_LEN - 0.2))
        row = {**r}
        for tag, at, ln in (("a", ta, A_LEN), ("b", tb, B_LEN)):
            dst = cuts / f"{c}_{tag}.mp4"
            if not (dst.is_file() and dst.stat().st_mtime > src.stat().st_mtime):
                if not _cut(src, dst, at, ln):
                    log(f"  [!] {c}: 컷 {tag} 실패")
                    dst = None
            row[f"cut_{tag}"] = f"assets/cuts/{dst.name}" if dst else None
        fdst = cuts / f"{c}_face.jpg"
        if not fdst.is_file():
            _face(out / f"_infocard_{c}", fdst)
        row["face"] = f"assets/cuts/{fdst.name}" if fdst.is_file() else None
        if r["rank"] == 1:
            tc = max(0.3, min(D * 0.76, D - C_LEN - 0.2))
            mos = []
            for n in MOSAIC:
                dst = cuts / f"top1_mos{n}.mp4"
                bw = 1920 // n
                vf = (f"{REFRAME},scale={1920 // bw}:{1080 // bw}:flags=area,"
                      f"scale=1920:1080:flags=neighbor")
                if not (dst.is_file() and dst.stat().st_mtime > src.stat().st_mtime):
                    if not _cut(src, dst, tc, C_LEN, vf):
                        log(f"  [!] {c}: 모자이크 {n} 실패")
                        continue
                mos.append(f"assets/cuts/{dst.name}")
            row["mos"] = mos
            still = cuts / "top1_still.jpg"
            ts = max(0.3, min(D * 0.9, D - 0.5))
            if not (still.is_file() and still.stat().st_mtime > src.stat().st_mtime):
                _ff(["-ss", f"{ts:.3f}", "-i", str(src), "-frames:v", "1",
                     "-vf", REFRAME, "-q:v", "2", str(still)])
            row["still"] = f"assets/cuts/{still.name}" if still.is_file() else None
        res.append(row)
        log(f"  {c}: A@{ta:.1f}s B@{tb:.1f}s" + (" face" if row["face"] else " (얼굴 없음)")
            + (f" 1위 모자이크 {len(row.get('mos', []))}단" if r["rank"] == 1 else ""))
    return res
