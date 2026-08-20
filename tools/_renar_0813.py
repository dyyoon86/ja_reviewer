# -*- coding: utf-8 -*-
"""ja18 재컷(0813) 후 내레이션 재배치 — 문장은 그대로, 자리만 사람이 잡는다.

stage_subs 의 retime 은 컷 밖으로 나간 줄을 '끝에서부터 밀어 넣기'로 처리한다. 이번처럼
한 덩어리를 크게 들어내면 여러 줄이 0초에 겹쳐 쌓여 못 쓴다(SNOS-353 3줄이 0.0~0.1s).
그래서 대사 빈틈을 실측해 아래 표에 직접 박았다. 규칙:
  · 인포카드(배너) 구간에 걸리는 줄은 리번인이 버리므로 7.7s 이후에만 놓는다
  · 각 줄은 자기 TTS 클립 길이만큼 대사 빈틈 안에 들어가야 한다(압축 회피)
  · 잘려나간 장면을 가리키는 줄은 뺀다 — 남은 화면과 말이 어긋나는 게 제일 나쁘다
  · 뺀 줄이 있으면 n001.. 순번이 어긋나므로 wav 를 새 순서로 다시 깐다

사용: .venv\\Scripts\\python.exe tools\\_renar_0813.py --out <out_dir>
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# 품번: [(문장 앞부분, 시작초), ...]  — 여기 없는 줄은 뺀다(=삭제 장면을 가리키는 줄)
# ── 2차(노출 기준 강화) 재컷분
PLACE2 = {
    "SNOS-309": [
        ("여섯 번째 작품은", 8.0), ("서른 중반에도", 11.0), ("정전된 사무실에", 23.8),
        ("데리러 온다는", 35.5), ("돌아가자던", 64.3), ("전개는 느린", 77.5),
        # "남자가 뒤에서 안는데" = 잘라낸 백허그 장면을 가리키는 줄 → 뺀다
    ],
}

PLACE1 = {
    "IPZZ-932": [
        ("네 번째 작품은", 46.1), ("스물여섯의", 58.6), ("야근 끝에", 61.2),
        ("신세 좀", 64.0), ("잘 마시네", 83.9), ("완벽하다는", 86.3),
        ("그럼 이 대화는", 88.8), ("더 마시자는", 129.4), ("화제는 사장님", 132.4),
        ("뒷심이 붙는", 135.0),
    ],
    "SNOS-321": [
        ("일곱 번째 작품은", 24.9), ("학생들이 노리는", 39.4), ("이번엔 쪽지로", 64.7),
        # "표정이 굳어지는 선생님" = 잘라낸 체육복 장면을 가리키는 줄 → 뺀다
    ],
    # 재컷과 무관 — 인포카드(배너) 구간에 걸려 리번인이 버리던 인트로 2줄을 뒤로 옮긴다
    # (배포본에서 "여덟 번째 작품은…" 자막이 아예 안 나오던 편)
    "SNOS-334": [
        ("여덟 번째 작품은", 33.5), ("상사가 얼룩", 36.5), ("요란하진 않아도", 57.8),
    ],
    "SNOS-353": [
        ("아홉 번째 작품은", 16.5), ("S1 넘버", 23.9), ("부탁 하나가", 33.2),
        ("먼저 조르는", 52.6), ("그리고 이 대화", 55.6), ("엄한 선생과", 65.0),
        ("소년은 결국", 78.1), ("한집에 산다는", 89.2), ("초반은 늘어지지만", 93.4),
        # "화난 게 아니라고 달래는데" = 잘라낸 밀착 장면을 가리키는 줄 → 뺀다
    ],
}

# 실행 대상 — 기본은 2차분, --pass1 이면 1차분 표를 쓴다
PLACE = PLACE2


def wav_dur(p: Path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


def srt_ts(t):
    h = int(t // 3600); m = int(t % 3600 // 60); s = int(t % 60)
    return f"{h:02d}:{m:02d}:{s:02d},{int(round((t - int(t)) * 1000)):03d}"


def main():
    ap = argparse.ArgumentParser(description="재컷 후 내레이션 수동 재배치")
    ap.add_argument("codes", nargs="*", help="생략 시 표 전체")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--pass1", action="store_true", help="1차 배치표(PLACE1)로 실행")
    args = ap.parse_args()
    globals()["PLACE"] = PLACE1 if args.pass1 else PLACE2
    out = Path(args.out)

    want = {c.upper() for c in args.codes}
    for code, rows in PLACE.items():
        if want and code not in want:
            continue
        d = out / code
        nj = d / f"{code}_내레이션.json"
        cur = json.loads(nj.read_text(encoding="utf-8"))
        # ★ n001.. 순번은 **TTS를 뽑던 그때의 srt 순서**다. 재컷 후 json 은 retime 이
        #   여러 줄을 0.0s 로 밀어 넣어 정렬이 뒤바뀌므로(353: 2·3번이 서로 바뀐다)
        #   반드시 그때의 srt 백업에서 순서를 가져온다.
        bak = d / f"{code}_내레이션.srt.bak_0813"
        if bak.is_file():
            order = [b.split("\n")[2].strip()
                     for b in bak.read_text(encoding="utf-8-sig").strip().split("\n\n")
                     if len(b.split("\n")) >= 3]
            by_text = {x["text"].strip(): x for x in cur}
            missing = [t for t in order if t not in by_text]
            if missing or len(order) != len(cur):
                sys.exit(f"✘ {code}: 백업 srt와 내레이션 json 이 안 맞는다 — 수동 확인 필요")
            cur = [by_text[t] for t in order]
        else:
            cur.sort(key=lambda x: float(x["start"]))
        tts = d / f"{code}_tts"
        durs = {i: wav_dur(tts / f"n{i + 1:03d}.wav") for i in range(len(cur))}
        print(f"\n{'=' * 66}\n{code}  (기존 {len(cur)}줄 → {len(rows)}줄)")

        picked = []
        for prefix, start in rows:
            hit = [i for i, x in enumerate(cur) if x["text"].startswith(prefix)]
            if len(hit) != 1:
                sys.exit(f"✘ {code}: '{prefix}' 매칭 {len(hit)}건 — 표를 고칠 것")
            i = hit[0]
            picked.append((i, start, durs[i], cur[i]))
        dropped = [x["text"] for i, x in enumerate(cur)
                   if i not in {p[0] for p in picked}]

        prev_end = 0.0
        for i, start, dur, item in picked:
            end = start + dur
            mark = " ⚠겹침" if start < prev_end else ""
            print(f"  n{i + 1:03d} {start:6.1f}~{end:6.1f} ({dur:.1f}s){mark}  {item['text'][:34]}")
            prev_end = end
        for t in dropped:
            print(f"  ✘ 뺌: {t}")
        if args.dry:
            continue

        # ── 1) srt / json 다시 쓰기 (시간순 = 새 n001.. 순서)
        srt, njs = [], []
        for k, (i, start, dur, item) in enumerate(picked, 1):
            end = start + dur
            srt += [str(k), f"{srt_ts(start)} --> {srt_ts(end)}", item["text"], ""]
            njs.append({"start": round(start, 2), "end": round(end, 2),
                        "text": item["text"], "style": item.get("style", "기본")})
        (d / f"{code}_내레이션.srt").write_text("\n".join(srt), encoding="utf-8-sig")
        nj.write_text(json.dumps(njs, ensure_ascii=False, indent=1), encoding="utf-8")

        # ── 2) TTS 클립을 새 순서로 다시 깐다(뺀 줄이 있으면 순번이 밀린다)
        old_idx = [p[0] for p in picked]
        if old_idx != list(range(len(picked))):
            bak = tts.with_name(tts.name + "_bak0813")
            if not bak.exists():
                shutil.copytree(tts, bak)
            tmp = tts.with_name(tts.name + "_new")
            tmp.mkdir(exist_ok=True)
            for k, i in enumerate(old_idx, 1):
                shutil.copy2(tts / f"n{i + 1:03d}.wav", tmp / f"n{k:03d}.wav")
            for p in tts.glob("n*.wav"):
                p.unlink()
            for p in tmp.glob("n*.wav"):
                shutil.move(str(p), tts / p.name)
            tmp.rmdir()
            print(f"  ※ TTS 클립 재배열: {[i + 1 for i in old_idx]} → n001~n{len(old_idx):03d}"
                  f" (원본은 {bak.name})")
        else:
            print(f"  ※ 순서 그대로 — n001~n{len(picked):03d} 사용, 나머지 클립은 무시")

    print("\n완료 — 이제 _reburn_1080_ja18.py 를 --allow-stale-tts 로 돌린다")


if __name__ == "__main__":
    main()
