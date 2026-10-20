"""섹션2 결과를 편별로 한 화면에서 보는 검토 페이지 생성기.

출력: {out}/_검토.html — 브라우저로 열면 된다(로컬 파일, 업로드하지 않는다).
  · 위쪽 탭(←/→ 키)으로 편 전환, 순서는 랭킹(꼴찌 → 1위)
  · 왼쪽: 영상({code}_final.mp4) + 지금 나오는 내레이션/대사를 영상 아래에 표시
  · 오른쪽: [타임라인] 내레이션·대사를 시간순으로 섞은 목록(누르면 그 시점으로 이동)
            [줄거리]  {code}_줄거리.md

사용: python tools/review_sheet.py --src <원본폴더> --out <배치폴더>
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ranklist import find_rank_file, load_rank, match_videos  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def _load_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def collect(out, src):
    items = load_rank(find_rank_file(src))
    pairs, missing, _ = match_videos(items, src)
    eps = []
    for rank, code, _v in reversed(pairs):          # 꼴찌 → 1위 (영상 순서)
        d = out / code
        if not d.is_dir():
            continue
        video = next((f"{code}/{n}" for n in (f"{code}_final.mp4", f"{code}_클린.mp4")
                      if (d / n).is_file()), "")
        plan = _load_json(d / f"{code}_plan.json") or {}
        nar = _load_json(d / f"{code}_내레이션.json") or plan.get("narration") or []
        dia = _load_json(d / f"{code}_대사.json") or []
        story = d / f"{code}_줄거리.md"
        eps.append({
            "rank": rank, "code": code, "video": video,
            "stars": plan.get("stars"),
            "story": story.read_text(encoding="utf-8") if story.is_file() else "",
            "nar": [{"s": n.get("start", 0), "e": n.get("end", 0), "t": n.get("text", ""),
                     "sfx": n.get("sfx", ""), "cut": n.get("cutin", "")} for n in nar],
            "dia": [{"s": x.get("start", 0), "e": x.get("end", 0), "t": x.get("text", ""),
                     "sp": x.get("speaker", "")} for x in dia],
        })
    return eps, missing


HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ 검토</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable.min.css">
<style>
/* 디자인 규칙: docs/DESIGN_meta.md */
:root{--bg:#F1F4F7;--surface:#FFFFFF;--surface-2:#F1F4F7;--line:#DEE3E9;--text:#1C2B33;--text-2:#465A69;
  --text-3:#67788A;--blue:#0064E0;--blue-soft:#E8F1FD;--warn:#F7B928;--danger:#E41E3F;
  --shadow:0 1px 2px rgba(28,43,51,.06),0 4px 16px rgba(28,43,51,.06)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0B1014;--surface:#1C2B33;--surface-2:#25353E;
  --line:#34474F;--text:#F1F4F7;--text-2:#B4C0C8;--text-3:#8595A0;--blue:#4A9BFF;--blue-soft:#14304F;--danger:#FF5C75;
  --shadow:none}}
:root[data-theme="dark"]{--bg:#0B1014;--surface:#1C2B33;--surface-2:#25353E;--line:#34474F;--text:#F1F4F7;
  --text-2:#B4C0C8;--text-3:#8595A0;--blue:#4A9BFF;--blue-soft:#14304F;--danger:#FF5C75;--shadow:none}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.5 "Pretendard Variable","Pretendard",system-ui,sans-serif;letter-spacing:-.01em}
button{font:inherit;letter-spacing:inherit}
header{position:sticky;top:0;z-index:5;background:var(--surface);border-bottom:1px solid var(--line)}
.hd{display:flex;align-items:baseline;gap:12px;padding:16px 24px 8px}
.hd h1{margin:0;font-size:22px;font-weight:700;letter-spacing:-.02em}
.hd .count{color:var(--text-3);font-size:13px;font-weight:500}
.hd .miss{margin-left:auto;color:var(--text-3);font-size:13px}
#tabs{display:flex;gap:8px;overflow-x:auto;padding:4px 24px 16px;scrollbar-width:thin}
#tabs button{flex:none;border:0;border-radius:999px;padding:8px 16px;cursor:pointer;
  background:var(--surface-2);color:var(--text-2);font-size:14px;font-weight:500}
#tabs button b{font-weight:700;color:var(--text);margin-right:6px}
#tabs button:hover{filter:brightness(.97)}
#tabs button.on{background:var(--blue);color:#fff}#tabs button.on b{color:#fff}
main{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:24px;padding:24px;
  max-width:1680px;margin:0 auto}
@media(max-width:960px){main{grid-template-columns:1fr;padding:16px}}
.card{background:var(--surface);border-radius:20px;box-shadow:var(--shadow);padding:16px}
#left{position:sticky;top:124px;align-self:start}
@media(max-width:960px){#left{position:static}}
video{width:100%;background:#000;border-radius:16px;display:block}
#novid{padding:64px 16px;text-align:center;color:var(--text-3);background:var(--surface-2);border-radius:16px}
.cap{min-height:88px;margin-top:12px;padding:12px 16px;border-radius:16px;background:#000;color:#fff;
  display:flex;flex-direction:column;justify-content:center;gap:6px;text-align:center}
.cap .n:not(:empty)::before{content:"내레이션";display:inline-block;margin-right:8px;padding:1px 8px;
  border-radius:999px;background:var(--blue);color:#fff;font-size:12px;font-weight:600;vertical-align:2px}
.cap .n{font-weight:600}.cap .d{color:#E4E6EB}
.meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.meta span{padding:4px 12px;border-radius:999px;background:var(--surface-2);color:var(--text-2);font-size:13px;font-weight:500}
.bar{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.seg{display:inline-flex;padding:4px;border-radius:999px;background:var(--surface-2)}
.seg button{border:0;background:transparent;color:var(--text-2);border-radius:999px;padding:6px 18px;cursor:pointer;
  font-size:14px;font-weight:600}
.seg button.on{background:var(--surface);color:var(--text);box-shadow:0 1px 3px rgba(28,43,51,.12)}
.bar label{margin-left:auto;color:var(--text-2);font-size:13px;font-weight:500;display:flex;gap:6px;align-items:center;cursor:pointer}
.bar input{accent-color:var(--blue);width:16px;height:16px}
#tl{display:flex;flex-direction:column;gap:4px}
.row{display:grid;grid-template-columns:48px 1fr;gap:12px;padding:10px 12px;border-radius:12px;cursor:pointer;
  border:1.5px solid transparent;transition:background .15s}
.row:hover{background:var(--surface-2)}
.row .t{color:var(--text-3);font-size:13px;font-variant-numeric:tabular-nums;padding-top:2px}
.row.n{background:var(--blue-soft);box-shadow:inset 3px 0 0 var(--blue)}
.row.n .x{color:var(--text);font-weight:600}
.row.d .x{color:var(--text-2)}
.row.now{border-color:var(--blue)}
.tags{display:inline-flex;flex-wrap:wrap;gap:4px;margin-left:6px;vertical-align:1px}
.tag{font-size:12px;line-height:18px;padding:0 8px;border-radius:999px;background:var(--surface);
  color:var(--text-3);font-weight:500;border:1px solid var(--line)}
.row.d .tag{background:var(--surface-2);border-color:transparent}
.tag.warn{background:var(--warn);border-color:var(--warn);color:#1C2B33}
#story{color:var(--text)}
#story h1{font-size:20px;letter-spacing:-.02em}#story h2{font-size:16px;margin-top:24px}
#story table{border-collapse:collapse;width:100%}
#story td,#story th{border-bottom:1px solid var(--line);padding:8px;text-align:left;vertical-align:top}
#story blockquote{color:var(--text-2);margin:0;padding:8px 16px;border-radius:12px;background:var(--surface-2)}
#story details{margin:8px 0;color:var(--text-2)}#story li{margin:4px 0}
</style></head><body>
<header>
  <div class="hd"><h1 id="title"></h1><span class="count" id="count"></span><span class="miss" id="miss"></span></div>
  <nav id="tabs"></nav>
</header>
<main>
  <section id="left" class="card">
    <div id="vbox"></div>
    <div class="cap"><div class="n" id="capN"></div><div class="d" id="capD"></div></div>
    <div class="meta" id="meta"></div>
  </section>
  <section class="card">
    <div class="bar">
      <div class="seg"><button data-v="tl" class="on">타임라인</button><button data-v="story">줄거리</button></div>
      <label><input type="checkbox" id="onlyN">내레이션만</label>
    </div>
    <div id="tl"></div><div id="story" hidden></div>
  </section>
</main>
<script>
const EPS = __DATA__;
const MISSING = __MISSING__;
const TITLE = __TITLEJS__;
let cur = 0, rows = [], vid = null;
const $ = s => document.querySelector(s);
const fmt = s => { s = Math.max(0, s); return Math.floor(s/60) + ":" + String(Math.floor(s%60)).padStart(2,"0"); };
const esc = s => String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

function tabs(){
  const t = $("#tabs"); t.innerHTML = "";
  EPS.forEach((e,i) => {
    const b = document.createElement("button");
    b.innerHTML = `<b>${e.rank}위</b>${esc(e.code)}`; b.className = i===cur ? "on" : "";
    b.onclick = () => show(i); t.appendChild(b);
  });
  $("#miss").textContent = MISSING.length ? "빠짐 " + MISSING.join(", ") : "";
}
function show(i){
  cur = i; const e = EPS[i]; tabs();
  try { localStorage.setItem("ep", e.code); } catch(_){}
  $("#vbox").innerHTML = e.video ? `<video controls preload="metadata" src="${encodeURI(e.video)}"></video>`
                                 : `<div id="novid">영상 없음</div>`;
  vid = $("#vbox video");
  if (vid) vid.ontimeupdate = tick;
  const nc = e.nar.reduce((a,n)=>a+n.t.length,0);
  $("#meta").innerHTML = `<span>내레이션 ${e.nar.length}줄 · ${nc}자</span><span>대사 ${e.dia.length}줄</span>`
    + (e.stars ? `<span>별점 ${e.stars}</span>` : "");
  const all = e.nar.map(n=>({...n,k:"n"})).concat(e.dia.map(d=>({...d,k:"d"}))).sort((a,b)=>a.s-b.s || (a.k==="n"?-1:1));
  const only = $("#onlyN").checked;
  $("#tl").innerHTML = "";
  rows = [];
  all.forEach(r => {
    if (only && r.k==="d") return;
    const el = document.createElement("div"); el.className = "row " + r.k;
    let extra = "";
    if (r.k==="n"){
      const sec = r.e - r.s, cps = sec>0 ? r.t.replace(/\s/g,"").length/sec : 0;
      if (r.sfx) extra += `<span class="tag">효과음 ${esc(r.sfx)}</span>`;
      if (r.cut) extra += `<span class="tag">짤 ${esc(r.cut)}</span>`;
      extra += `<span class="tag ${cps>7?"warn":""}">${sec.toFixed(1)}초 · ${cps.toFixed(1)}자/초</span>`;
    } else if (r.sp) extra = `<span class="tag">${esc(r.sp)}</span>`;
    el.innerHTML = `<span class="t">${fmt(r.s)}</span><span class="x">${esc(r.t)}${extra ? `<span class="tags">${extra}</span>` : ""}</span>`;
    el.onclick = () => { if (vid){ vid.currentTime = r.s + 0.01; vid.play(); } };
    $("#tl").appendChild(el); rows.push({r, el});
  });
  $("#story").innerHTML = e.story ? (window.marked ? marked.parse(e.story) : `<pre>${esc(e.story)}</pre>`)
                                  : "<p>줄거리 파일 없음</p>";
  $("#capN").textContent = ""; $("#capD").textContent = "";
  window.scrollTo(0,0);
}
function tick(){
  const t = vid.currentTime, e = EPS[cur];
  const n = e.nar.find(x => t>=x.s && t<x.e), d = e.dia.find(x => t>=x.s && t<x.e);
  $("#capN").textContent = n ? n.t : ""; $("#capD").textContent = d ? (d.sp ? d.sp+": " : "") + d.t : "";
  let last = null;
  rows.forEach(({r,el}) => { const on = t>=r.s && t<r.e; el.classList.toggle("now", on); if (on) last = el; });
  if (last && !$("#tl").hidden){
    const b = last.getBoundingClientRect();
    if (b.top < 60 || b.bottom > innerHeight) last.scrollIntoView({block:"center"});
  }
}
document.querySelectorAll(".seg button").forEach(b => b.onclick = () => {
  document.querySelectorAll(".seg button").forEach(x => x.classList.toggle("on", x===b));
  $("#tl").hidden = b.dataset.v !== "tl"; $("#story").hidden = b.dataset.v !== "story";
});
$("#onlyN").onchange = () => show(cur);
addEventListener("keydown", ev => {
  if (ev.target.tagName === "INPUT") return;
  if (ev.key === "ArrowRight" && cur < EPS.length-1) show(cur+1);
  if (ev.key === "ArrowLeft" && cur > 0) show(cur-1);
});
let start = 0;
try { const c = localStorage.getItem("ep"); const k = EPS.findIndex(e=>e.code===c); if (k>=0) start = k; } catch(_){}
document.title = TITLE + " 검토"; $("#title").textContent = TITLE; $("#count").textContent = EPS.length + "편";
if (EPS.length) show(start);
</script></body></html>"""


def build(out, src):
    out, src = Path(out), Path(src)
    eps, missing = collect(out, src)
    dump = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    html = (HTML.replace("__TITLE__", out.name)
                .replace("__DATA__", dump(eps))
                .replace("__MISSING__", dump(missing))
                .replace("__TITLEJS__", dump(out.name)))
    dst = out / "_검토.html"
    dst.write_text(html, encoding="utf-8")
    return dst, eps


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    dst, eps = build(a.out, a.src)
    print(f"검토 페이지 {dst} — {len(eps)}편")


if __name__ == "__main__":
    main()
