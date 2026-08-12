# -*- coding: utf-8 -*-
"""워터마크 얼굴 슬롯이 딸기 마스코트로 떨어진 편을 배우 사진으로 갈아끼운다.

**왜 필요한가** — gen_infocard는 작품이 로컬 DB에 없으면 meta_api(우분투)에서 메타를
받아오는데, 사진은 우분투 로컬 파일이라 안 실려온다. 그래서 `actress_ja` 이름으로
로컬 actresses를 한 번 더 조회하는 폴백이 있는데, 이게 두 경우에 빗나간다:

  · **한자 표기 차이** — 사이트마다 다르다(七島舞 vs 七嶋舞, 百田光希 vs 百田光稀).
  · **배우 2명 이상** — `actress_ja`가 "坂道みる, 村上悠華" 같은 합친 문자열이라
    단일 행 조회에 안 걸린다.

그러면 얼굴 자리가 딸기로 나간다(ja18에서 ABF-375·SNOS-334·SNOS-353이 그랬다).

**어떻게 고치나** — 배너를 처음부터 다시 만들려면 meta_api가 살아 있어야 하는데,
우분투가 죽어 있으면 방법이 없다. 대신 `_infocard_{품번}/_L_wm.html` 에 **그때 렌더한
HTML이 통째로 남아 있다**. 거기서 마스코트 배경만 배우 사진으로 바꿔 다시 찍으면
메타 조회 없이 워터마크만 정확히 교체된다(인포카드·프레임은 얼굴이 없어 손댈 것 없다).

바꾼 뒤에는 그 편의 최종본을 다시 구워야 한다(`_reburn_1080_ja18.py {품번}`).

사용: .venv\\Scripts\\python.exe tools\\_fix_face.py --out <out_dir> 품번=사진파일 [...]
  예) _fix_face.py --out C:\\...\\ja18 ABF-375=nanasima_mai.jpg SNOS-334=seto_kanna.jpg
  사진은 파일명만 주면 jav_scrap/images_actress/ 에서 찾는다.
"""
import argparse
import base64
import re
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import _common  # noqa: F401  (repo 루트 sys.path 등록)

IMG_DIR = Path(r"E:\vscode\workspace\jav_scrap\images_actress")

# 마스코트 폴백 배경 → 사진 배경. gen_infocard.html_wm 의 두 분기와 정확히 같은 모양이다.
FALLBACK_RE = re.compile(
    r"background-image:url\(data:image/png;base64,[^)]*\);"
    r"background-size:70%;background-color:#[0-9a-fA-F]{6}")


def patch(outdir: Path, code: str, photo: Path) -> str:
    icdir = outdir / f"_infocard_{code}"
    html_f = icdir / "_L_wm.html"
    if not html_f.is_file():
        return f"✘ {html_f.name} 없음 — 배너를 만든 적이 없는 편이다"
    html = html_f.read_text(encoding="utf-8")
    if not FALLBACK_RE.search(html):
        if "background-size:cover" in html:
            return "· 이미 배우 사진이 들어가 있음 — 건너뜀"
        return "✘ 얼굴 배경 패턴을 못 찾음 — gen_infocard 쪽이 바뀌었는지 확인할 것"

    b64 = base64.b64encode(photo.read_bytes()).decode()
    new = f"background-image:url(data:image/jpeg;base64,{b64});background-size:cover"
    html = FALLBACK_RE.sub(new, html, count=1)
    html_f.write_text(html, encoding="utf-8")

    from playwright.sync_api import sync_playwright
    png = icdir / "L_wm.png"
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
        pg.goto("file://" + str(html_f).replace("\\", "/"))
        pg.wait_for_timeout(1300)
        pg.screenshot(path=str(png), omit_background=True)
        pg.close(); b.close()
    shutil.copyfile(png, icdir / f"{code}_워터마크.png")
    # 우상단 재배치 사본은 리프레임 스크립트가 매번 다시 만든다 — 옛것이 남아 헷갈리지 않게 지운다.
    (icdir / f"{code}_워터마크_tr.png").unlink(missing_ok=True)
    return f"✔ {photo.name} 적용 ({png.stat().st_size // 1024}KB)"


def main():
    ap = argparse.ArgumentParser(description="워터마크 얼굴 교체(메타 조회 없이)")
    ap.add_argument("pairs", nargs="+", metavar="품번=사진",
                    help="예: ABF-375=nanasima_mai.jpg")
    ap.add_argument("--out", required=True, help="out_dir")
    args = ap.parse_args()

    outdir = Path(args.out)
    rows = []
    for pair in args.pairs:
        code, _, name = pair.partition("=")
        code = code.upper()
        if not name:
            rows.append((code, "✘ 사진을 지정하지 않았다(품번=파일명)")); continue
        photo = Path(name)
        if not photo.is_file():
            photo = IMG_DIR / name
        if not photo.is_file():
            rows.append((code, f"✘ 사진 없음: {photo}")); continue
        try:
            rows.append((code, patch(outdir, code, photo)))
        except Exception as e:
            import traceback; traceback.print_exc()
            rows.append((code, f"✘ {e}"))

    print("\n요약")
    for code, note in rows:
        print(f"  {code}: {note}")
    fails = sum(1 for _, n in rows if n.startswith("✘"))
    if fails:
        print(f"\n실패 {fails}건")
        sys.exit(1)
    print("\n다음: 해당 편 최종본을 다시 구울 것 — "
          "python _reburn_1080_ja18.py <품번...> --allow-stale-tts")


if __name__ == "__main__":
    main()
