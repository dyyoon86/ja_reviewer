// ja_reviewer Phase1 프론트
const $ = (s) => document.querySelector(s);
const vid = $("#vid");
let videoPath = null, duration = 0;
let excludes = [];          // [[start,end], ...]
let pendingIn = null;       // 마킹 중인 시작점

// ── 유틸 ──────────────────────────────────────────────────────────────────
function hhmmss(x){ x=Math.max(0,Math.floor(x));
  const h=String(Math.floor(x/3600)).padStart(2,'0');
  const m=String(Math.floor(x%3600/60)).padStart(2,'0');
  const s=String(x%60).padStart(2,'0'); return `${h}:${m}:${s}`; }
function log(msg, cls){ const el=$("#log");
  el.innerHTML += `\n${cls?`<span class="${cls}">`:''}${msg}${cls?'</span>':''}`;
  el.scrollTop = el.scrollHeight; }

// ── 파일 열기 ─────────────────────────────────────────────────────────────
$("#btnBrowse").onclick = async () => {
  const r = await fetch("/browse", {method:"POST"}).then(r=>r.json()).catch(()=>({}));
  if (r.path){ $("#path").value = r.path; openVideo(r.path); }
  else if (r.error){ log("파일 다이얼로그 실패: "+r.error+" → 경로를 직접 붙여넣으세요","warn"); }
};
$("#btnOpen").onclick = () => { const p=$("#path").value.trim(); if(p) openVideo(p); };

async function openVideo(path){
  try{
    const r = await fetch("/open",{method:"POST",headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path})});
    if(!r.ok){ log("열기 실패: 파일을 찾을 수 없음","warn"); return; }
    const j = await r.json();
    videoPath = j.path; duration = j.duration || 0;
    vid.src = `/video/stream?path=${encodeURIComponent(j.path)}`;
    excludes = []; pendingIn = null; renderEx();
    log(`영상 로드: ${j.name} (${hhmmss(duration)})`, "ok");
    // 품번 자동 추정 (파일명에서 XXX-000 패턴) → 양 탭에 채움
    const mm = j.name.match(/([A-Za-z]{2,6})-?(\d{2,5})/);
    if(mm){ const guess=(mm[1]+"-"+mm[2]).toUpperCase();
      if(!$("#code").value) $("#code").value=guess;
      if(!$("#codeA").value) $("#codeA").value=guess; }
  }catch(e){ log("열기 오류: "+e,"warn"); }
}

// ── 플레이어 ──────────────────────────────────────────────────────────────
function curTime(){ return vid.currentTime || 0; }
$("#btnPlay").onclick = () => vid.paused ? vid.play() : vid.pause();
function seekRel(d){ vid.currentTime = Math.max(0, Math.min(duration||vid.duration, curTime()+d)); }

vid.addEventListener("loadedmetadata", () => { if(!duration) duration = vid.duration; });
vid.addEventListener("timeupdate", () => {
  const d = duration || vid.duration || 1;
  $("#time").textContent = `${hhmmss(curTime())} / ${hhmmss(d)}`;
  $("#cur").style.left = (curTime()/d*100)+"%";
});

// 타임라인 클릭 시킹
$("#tl").onclick = (e) => {
  const rect = e.currentTarget.getBoundingClientRect();
  const frac = (e.clientX-rect.left)/rect.width;
  vid.currentTime = frac*(duration||vid.duration||0);
};

// ── 마킹 ──────────────────────────────────────────────────────────────────
function markIn(){ pendingIn = curTime(); log(`● 구간 시작: ${hhmmss(pendingIn)}`); renderEx(); }
function markOut(){
  if(pendingIn===null){ log("먼저 '구간 시작'([ 또는 I)을 누르세요","warn"); return; }
  const a=pendingIn, b=curTime();
  if(b<=a){ log("끝이 시작보다 뒤여야 합니다","warn"); return; }
  excludes.push([a,b]); pendingIn=null;
  excludes.sort((x,y)=>x[0]-y[0]); renderEx();
  log(`● 삭제 구간 추가: ${hhmmss(a)} ~ ${hhmmss(b)}`, "ok");
}
$("#btnIn").onclick = markIn; $("#btnOut").onclick = markOut;

$("#btnAddText").onclick = () => {
  const txt=$("#exText").value;
  const re=/(\d{1,2}:\d{2}(?::\d{2})?|\d+)\s*[-~]\s*(\d{1,2}:\d{2}(?::\d{2})?|\d+)/g;
  const toSec=(s)=>{ if(!s.includes(':')) return parseFloat(s);
    const p=s.split(':').map(Number); while(p.length<3)p.unshift(0); return p[0]*3600+p[1]*60+p[2]; };
  let m, n=0;
  while((m=re.exec(txt))){ const a=toSec(m[1]), b=toSec(m[2]); if(b>a){excludes.push([a,b]);n++;} }
  if(n){ excludes.sort((x,y)=>x[0]-y[0]); renderEx(); $("#exText").value=""; log(`텍스트로 ${n}개 구간 추가`,"ok"); }
  else log("형식: 12:30-18:00, 45:00-52:00","warn");
};
$("#btnClear").onclick = () => { excludes=[]; pendingIn=null; renderEx(); };

function renderEx(){
  const ul=$("#exList"); ul.innerHTML="";
  excludes.forEach((r,i)=>{
    const li=document.createElement("li");
    li.innerHTML=`<span>${hhmmss(r[0])} ~ ${hhmmss(r[1])} <span class="muted">(삭제)</span></span>`;
    const b=document.createElement("button"); b.textContent="✕";
    b.onclick=()=>{ excludes.splice(i,1); renderEx(); }; li.appendChild(b); ul.appendChild(li);
  });
  // 타임라인 마커
  const tl=$("#tl"); tl.querySelectorAll(".ex,.pend").forEach(e=>e.remove());
  const d=duration||vid.duration||1;
  excludes.forEach(r=>{ const el=document.createElement("div"); el.className="ex";
    el.style.left=(r[0]/d*100)+"%"; el.style.width=((r[1]-r[0])/d*100)+"%"; tl.appendChild(el); });
  if(pendingIn!==null){ const el=document.createElement("div"); el.className="pend";
    el.style.left=(pendingIn/d*100)+"%"; el.style.width="2px"; tl.appendChild(el); }
}

// ── 단축키 ────────────────────────────────────────────────────────────────
document.addEventListener("keydown", (e) => {
  const t=e.target.tagName;
  if(t==="INPUT"||t==="SELECT"||t==="TEXTAREA") return;
  if(e.code==="Space"){ e.preventDefault(); vid.paused?vid.play():vid.pause(); }
  else if(e.key==="["||e.key==="i"||e.key==="I") markIn();
  else if(e.key==="]"||e.key==="o"||e.key==="O") markOut();
  else if(e.key==="ArrowLeft"){ e.preventDefault(); seekRel(-5); }
  else if(e.key==="ArrowRight"){ e.preventDefault(); seekRel(5); }
});

// ── 탭 ────────────────────────────────────────────────────────────────────
document.querySelectorAll(".tabs .t").forEach(tab=>{
  tab.onclick=()=>{
    document.querySelectorAll(".tabs .t").forEach(x=>x.classList.remove("active"));
    document.querySelectorAll(".pane").forEach(x=>x.classList.remove("active"));
    tab.classList.add("active"); $("#pane-"+tab.dataset.pane).classList.add("active");
  };
});

// ── 잡 실행 + SSE ─────────────────────────────────────────────────────────
function needVideo(){ if(!videoPath){ log("영상을 먼저 여세요","warn"); return false; } return true; }
function needCode(){ if(!$("#code").value.trim()){ log("품번을 입력하세요","warn"); return false; } return true; }

// ── 진행 바 / 파일 목록 ──
function setProg(frac, label, cls){
  const f=$("#progFill"); f.style.width=Math.round((frac||0)*100)+"%";
  f.className="prog-fill"+(cls?" "+cls:"");
  if(label!==undefined) $("#progStep").textContent=label;
}
function clearFiles(){ $("#files").innerHTML=""; }
function addFile(tag, path){
  const li=document.createElement("li");
  li.innerHTML=`<span class="tag">✔ ${tag}</span><span class="pth">${path}</span>`;
  $("#files").appendChild(li);
}

function runJob(job, onDone){
  clearFiles(); setProg(0.04, "시작…");
  const es = new EventSource(`/events/${job}`);
  es.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if(m.type==="log") log("  "+m.msg);
    else if(m.type==="step"){ const fr=m.total?m.n/m.total:0; setProg(fr, `${m.n}/${m.total} · ${m.label}`); }
    else if(m.type==="file"){ addFile(m.label, m.path); }
    else if(m.type==="error"){ log("✖ 오류: "+m.msg,"warn"); setProg(1,"오류","err"); es.close(); }
    else if(m.type==="done"){ setProg(1,"완료","done"); es.close(); onDone(m.result); }
  };
}

// ① 잘라내기 — 품번 불필요
$("#btnTrim").onclick = () => {
  if(!needVideo()) return;
  if(!excludes.length){ log("삭제할 구간을 하나 이상 추가하세요","warn"); return; }
  log("── ① 잘라내기 시작 ──");
  fetch("/trim",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    path:videoPath, excludes
  })}).then(r=>r.json()).then(j=>runJob(j.job, (res)=>{
    videoPath = res.video; duration = res.duration || 0;
    vid.src = `/video/stream?path=${encodeURIComponent(res.video)}`;
    excludes = []; pendingIn = null; renderEx();
    log(`✔ 잘라낸 영상 로드: ${res.video} (${hhmmss(duration)}). 품번 넣고 ②를 누르세요.`,"ok");
  }));
};

// ② 리뷰 생성 — 품번 필요
$("#btnReview").onclick = () => {
  if(!needVideo() || !needCode()) return;
  log("── ② 리뷰 생성 시작 ──");
  fetch("/review",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    path:videoPath, code:$("#code").value.trim(),
    target_sec:+$("#target").value, llm:$("#llm").value, model:$("#whisper").value,
    hint:($("#hint")?$("#hint").value.trim():"")
  })}).then(r=>r.json()).then(j=>runJob(j.job, showResult));
};

function needCodeA(){ if(!$("#codeA").value.trim()){ log("품번을 입력하세요","warn"); return false; } return true; }

$("#btnAnalyze").onclick = () => {
  if(!needVideo() || !needCodeA()) return;
  const mode = $("#modeA") ? $("#modeA").value : "summary";
  log(`── 자동 분석 시작 (${mode==="highlight"?"하이라이트형":"요약형"}) ──`);
  fetch("/analyze",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    path:videoPath, code:$("#codeA").value.trim(),
    target_sec:+$("#targetA").value, llm:$("#llmA").value, model:$("#whisperA").value,
    hint:($("#hintA")?$("#hintA").value.trim():""), mode
  })}).then(r=>r.json()).then(j=>runJob(j.job, (res)=>{
    applyPickResult(res.result);
    log("자동 분석 완료 — 프리뷰에서 재생·확인 후 [확정]","ok");
  }));
};

// AI 결과 → JSON 텍스트 + 프리뷰 목록 반영
function applyPickResult(result){
  $("#autoJson").value = JSON.stringify(result, null, 2);
  $("#btnRender").disabled = false;
  $("#btnSavePick").disabled = false;
  buildPickPreview(result);
}

function _esc(s){ return (s||"").replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m])); }
// 구간 [a,b] 안에 들어오는 자막 라인 필터(시작시각 기준)
function _within(list, a, b){
  return (Array.isArray(list)?list:[]).filter(d=> +d.start >= a-0.1 && +d.start < b+0.1)
    .sort((x,y)=>+x.start-+y.start);
}
// 실제 SRT처럼 25자 내외로 조각내 보여주기(공백 경계 우선)
function _chunk25(text, max){
  max = max||25; const t=(text||"").replace(/\s+/g," ").trim();
  if(t.length<=max) return [t];
  const words=t.split(" "), out=[]; let cur="";
  for(const w of words){
    const cand = cur? cur+" "+w : w;
    if(cand.length>max && cur){ out.push(cur); cur=w; }
    else cur=cand;
  }
  if(cur) out.push(cur);
  return out;
}

// 구간 목록 프리뷰 — 각 구간에 한글 대사(화자) + 내레이션(유형)까지 펼침
function buildPickPreview(result){
  const ul = $("#pickPreview"); ul.innerHTML = "";
  let rows = [];
  if(result && Array.isArray(result.picks) && result.picks.length){
    rows = result.picks.map(p=>({a:+p.start, b:+p.end, hook:p.hook, reason:p.reason||""}));
  } else if(result && Array.isArray(result.keep)){
    rows = result.keep.map(k=>({a:+k[0], b:+k[1], hook:null, reason:""}));
  }
  rows.sort((x,y)=> (y.hook||0)-(x.hook||0) || x.a-y.a);
  const dlgs = result.dialogue || [], nars = result.narration || [];
  rows.forEach((r)=>{
    const li=document.createElement("li"); li.className="pk-item";
    // 헤더 줄
    const head=document.createElement("div"); head.className="pk-head";
    head.innerHTML =
      `<span class="pk-t">${hhmmss(r.a)} ~ ${hhmmss(r.b)}</span>`+
      (r.hook!=null?`<span class="pk-hook">★${r.hook}</span>`:``)+
      `<span class="pk-reason">${_esc(r.reason)}</span>`;
    const play=document.createElement("button");
    play.className="pk-play"; play.textContent="▶ 재생";
    play.onclick=()=>seekPlay(r.a, r.b);
    head.appendChild(play);
    li.appendChild(head);
    // 본문: 대사 + 내레이션
    const body=document.createElement("div"); body.className="pk-body";
    _within(dlgs, r.a, r.b).forEach(d=>{
      const sp=(d.speaker==="남")?"남":"여";
      _chunk25(d.ko).forEach((c,ci)=>{
        body.insertAdjacentHTML("beforeend",
          `<div class="pk-line dlg ${sp==="남"?"m":"f"}"><span class="pk-tag">${ci===0?sp:"·"}</span>${_esc(c)}</div>`);
      });
    });
    _within(nars, r.a, r.b).forEach(n=>{
      const st=n.style||"기본";
      _chunk25(n.text).forEach((c,ci)=>{
        body.insertAdjacentHTML("beforeend",
          `<div class="pk-line nar ${st==="강조"?"emph":st==="정보"?"info":""}"><span class="pk-tag">${ci===0?"내레":"·"}</span>${_esc(c)}</div>`);
      });
    });
    if(!body.children.length) body.innerHTML='<div class="pk-line muted">이 구간 자막 없음</div>';
    li.appendChild(body);
    ul.appendChild(li);
  });
  if(!rows.length) ul.innerHTML = '<li class="muted" style="padding:8px">구간이 없습니다.</li>';
}

// 구간 재생: start로 시킹 → 재생 → end에서 정지
let _seekStopAt = null;
function seekPlay(a, b){
  if(!videoPath){ log("영상을 먼저 열어주세요","warn"); return; }
  _seekStopAt = b;
  vid.currentTime = Math.max(0, a);
  vid.play();
}
vid.addEventListener("timeupdate", ()=>{
  if(_seekStopAt!=null && vid.currentTime >= _seekStopAt){ vid.pause(); _seekStopAt=null; }
});

// AI 선정 결과 저장/불러오기
$("#btnSavePick").onclick = () => {
  const code=$("#codeA").value.trim(); if(!code){ log("품번을 입력하세요","warn"); return; }
  let res; try{ res=JSON.parse($("#autoJson").value); }catch(e){ log("JSON 오류: "+e,"warn"); return; }
  fetch("/pick/save",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code, result:res})}).then(r=>r.json())
    .then(j=> log(j.ok?`✔ 저장: ${j.path}`:"저장 실패","ok"));
};
$("#btnLoadPick").onclick = () => {
  const code=$("#codeA").value.trim(); if(!code){ log("품번을 입력하세요","warn"); return; }
  fetch("/pick/load",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code})}).then(r=>r.json()).then(j=>{
      if(!j.ok||!j.result){ log("저장된 결과 없음","warn"); return; }
      applyPickResult(j.result); log("✔ 저장된 AI 선정 결과 불러옴","ok");
    });
};

$("#btnRender").onclick = () => {
  if(!needVideo() || !needCodeA()) return;
  let res; try{ res=JSON.parse($("#autoJson").value); }catch(e){ log("JSON 오류: "+e,"warn"); return; }
  log("── 확정 렌더 ──");
  fetch("/render",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    path:videoPath, code:$("#codeA").value.trim(), result:res
  })}).then(r=>r.json()).then(j=>runJob(j.job, showResult));
};

function showResult(r){
  $("#resultCard").style.display="block";
  $("#result").innerHTML =
    `<div class="ok">✔ 완료${r.final_sec?` (최종 ${Math.round(r.final_sec)}초)`:''}</div>`+
    (r.final?`<div>영상: ${r.final}</div>`:'')+
    (r.srt_dialogue?`<div>대사: ${r.srt_dialogue}</div>`:'')+
    (r.srt_narration?`<div>내레이션: ${r.srt_narration}</div>`:'')+
    (r.summary?`<div class="muted" style="margin-top:6px">요약: ${r.summary}</div>`:'');
  log("✔ 출력 완료","ok");
  // 리뷰 생성 후 자동 음성 생성 (옵션)
  if(r.srt_narration && $("#ttsAuto").checked){
    if(!$("#ttsProfile").value){ log("⚠ 자동 음성: 보이스를 먼저 선택하세요(보이스 목록 → 한국어).","warn"); return; }
    log("→ 자동으로 내레이션 음성 생성 이어갑니다…");
    runTts();
  }
}

// ── 품번 DB(메타 API) 연결/조회 확인 ────────────────────────────────────────
function checkMeta(code){
  code=(code||"").trim();
  if(!code){ log("품번을 입력하세요","warn"); return; }
  log(`품번 ${code} DB 조회 중…`);
  fetch("/meta/"+encodeURIComponent(code)).then(async r=>{
    const j=await r.json().catch(()=>({}));
    if(!r.ok){ log("✖ 메타 조회 실패: "+(j.detail||("HTTP "+r.status))+" — meta_api 연결/품번 확인","warn"); return; }
    log(`✅ DB 연결 OK — ${j.actress||'?'} / ${j.label||'?'} / ${j.meas||''}${j.title?(' / '+j.title):''}`,"ok");
  }).catch(e=>log("✖ 연결 오류: "+e+" (meta_api 주소/네트워크 확인)","warn"));
}
$("#btnMeta").onclick=()=>checkMeta($("#code").value);
$("#btnMetaA").onclick=()=>checkMeta($("#codeA").value);

// ── TTS (voicebox) ──────────────────────────────────────────────────────────
function curCode(){ return ($("#code").value || $("#codeA").value || "").trim(); }

$("#btnProfiles").onclick = () => {
  log("voicebox 보이스 목록 불러오는 중…");
  fetch("/tts/profiles").then(r=>r.json()).then(d=>{
    if(d.detail){ log("✖ "+d.detail,"warn"); return; }
    const sel=$("#ttsProfile"); sel.innerHTML="";
    const list = Array.isArray(d.profiles) ? d.profiles : (d.profiles.profiles||d.profiles.items||[]);
    if(!list || !list.length){ log("보이스가 없습니다. voicebox에서 한국어(Qwen3-TTS) 보이스를 먼저 만드세요.","warn"); return; }
    list.forEach(p=>{
      const id = p.id || p.profile_id || p.name || p;
      const nm = p.name || p.title || id;
      const lang = p.language || p.lang || "";
      const o=document.createElement("option"); o.value=id; o.textContent=nm+(lang?` [${lang}]`:""); sel.appendChild(o);
    });
    log(`보이스 ${list.length}개 로드 ✅ (한국어 보이스 선택)`,"ok");
  }).catch(e=>log("✖ voicebox 연결 실패: "+e+" — voicebox 실행/주소 확인","warn"));
};

$("#btnTtsTest").onclick = () => {
  if(!$("#ttsProfile").value){ log("보이스를 선택하세요(보이스 목록 → 한국어)","warn"); return; }
  log("── 테스트 음성 생성 ──");
  fetch("/tts/test",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    text:$("#ttsTestText").value, profile:$("#ttsProfile").value,
    tts_base:$("#ttsBase").value.trim()||undefined,
    seed:$("#ttsSeed").value!==""?+$("#ttsSeed").value:undefined
  })}).then(r=>r.json()).then(j=>runJob(j.job, (r)=>{
    const a=$("#ttsAudio");
    a.src="/video/stream?path="+encodeURIComponent(r.wav)+"&t="+Date.now();
    a.style.display="block"; a.play().catch(()=>{});
    log("✔ 테스트 음성 재생 ▶ (연결·보이스 정상)","ok");
  }));
};

function runTts(){
  const code=curCode();
  if(!code){ log("품번을 입력하세요(내레이션 SRT 찾기용)","warn"); return; }
  if(!$("#ttsProfile").value){ log("보이스를 선택하세요(보이스 목록 → 한국어)","warn"); return; }
  log("── 내레이션 음성 생성 시작 ──");
  fetch("/tts",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    code, profile:$("#ttsProfile").value, tts_base:$("#ttsBase").value.trim()||undefined,
    seed:$("#ttsSeed").value!==""?+$("#ttsSeed").value:undefined, mux:$("#ttsMux").checked
  })}).then(r=>r.json()).then(j=>runJob(j.job, (r)=>{
    $("#resultCard").style.display="block";
    $("#result").innerHTML = `<div class="ok">✔ 내레이션 음성 ${r.count}개 합성</div>`+
      (r.narration_wav?`<div>WAV: ${r.narration_wav}</div>`:'')+
      (r.voiced?`<div>입힌 영상: ${r.voiced}</div>`:'');
    log("✔ 내레이션 음성 완료","ok");
  }));
}
$("#btnTts").onclick = runTts;

// ── 자막 입히기(하드섭) + 템플릿 ────────────────────────────────────────────
function collectStyle(p){
  return { font:$("#"+p+"Font").value||"Malgun Gothic", size:+$("#"+p+"Size").value||40,
    bold:$("#"+p+"Bold").checked, color:$("#"+p+"Color").value, outline_color:$("#"+p+"OutColor").value,
    outline:parseFloat($("#"+p+"Outline").value)||0, v:$("#"+p+"V").value, h:$("#"+p+"H").value,
    margin:+$("#"+p+"Margin").value||0 };
}
function applyStyle(p, st){
  if(!st) return;
  $("#"+p+"Font").value=st.font||"Malgun Gothic";
  $("#"+p+"Size").value=st.size!=null?st.size:40;
  $("#"+p+"Bold").checked=!!st.bold;
  $("#"+p+"Color").value=(st.color||"#FFFFFF").toLowerCase();
  $("#"+p+"OutColor").value=(st.outline_color||"#000000").toLowerCase();
  $("#"+p+"Outline").value=st.outline!=null?st.outline:2.2;
  $("#"+p+"V").value=st.v||"bottom";
  $("#"+p+"H").value=st.h||"center";
  $("#"+p+"Margin").value=st.margin!=null?st.margin:40;
}
let SUBTPL={};
function loadSubTemplates(pick){
  fetch("/sub/templates").then(r=>r.json()).then(d=>{
    SUBTPL=d||{}; const s=$("#subTpl"); s.innerHTML="";
    Object.keys(SUBTPL).forEach(n=>{const o=document.createElement("option");o.value=n;o.textContent=n;s.appendChild(o);});
    const first=pick||Object.keys(SUBTPL)[0];
    if(first){ s.value=first; applyTpl(SUBTPL[first]); }
  }).catch(()=>{});
}
function applyTpl(t){ if(!t) return; applyStyle("dlg",t.dialogue); applyStyle("dlm",t.dialogue_m); applyStyle("nar",t.narration); applyStyle("emp",t.emphasis); applyStyle("inf",t.info); }
function allStyles(){ return {dialogue:collectStyle("dlg"), dialogue_m:collectStyle("dlm"), narration:collectStyle("nar"), emphasis:collectStyle("emp"), info:collectStyle("inf")}; }
$("#subTpl").onchange=()=>{ applyTpl(SUBTPL[$("#subTpl").value]); };
$("#btnTplSave").onclick=()=>{
  const name=$("#subTplName").value.trim(); if(!name){ log("템플릿 이름을 입력하세요","warn"); return; }
  fetch("/sub/templates",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    name, styles:allStyles()
  })}).then(r=>r.json()).then(()=>{ $("#subTplName").value=""; loadSubTemplates(name); log("✔ 템플릿 저장: "+name,"ok"); });
};
$("#btnBurn").onclick=()=>{
  const code=curCode(); if(!code){ log("품번을 입력하세요","warn"); return; }
  log("── 자막 입히기 시작 ──");
  fetch("/burn",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    code, styles:allStyles()
  })}).then(r=>r.json()).then(j=>runJob(j.job,(r)=>{
    $("#resultCard").style.display="block";
    $("#result").innerHTML=`<div class="ok">✔ 자막 입힌 영상</div><div>${r.subbed}</div>`;
    log("✔ 자막 영상 완료","ok");
  }));
};

// 인코딩 체크 시에만 대상영상 입력줄 표시
if($("#icEncode")) $("#icEncode").onchange=()=>{
  $("#icSrcRow").style.display=$("#icEncode").checked?"":"none";
};

// ⑤ 인포배너 — 품번 → 오버레이 소스(PNG) 생성 (인코딩 없음) + 미리보기
$("#btnInfocard").onclick=()=>{
  const code=($("#icCode").value||curCode()||"").trim();
  if(!code){ log("품번을 입력하세요","warn"); return; }
  const hold=parseFloat($("#icHold").value)||2.0;
  const encode=$("#icEncode") && $("#icEncode").checked;
  const useCur=$("#icUseCur") && $("#icUseCur").checked;
  const source=($("#icSource").value||"").trim() || (useCur && videoPath ? videoPath : "");
  log("── 인포배너 소스 생성 시작 ──"+(encode?(source?` (영상 오버레이 인코딩: ${source})`:" (데모 인코딩)"):" (PNG만·인코딩 없음)"));
  fetch("/infocard",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    code, hold, source, encode
  })}).then(r=>r.json()).then(j=>{
    if(!j.job){ log("✖ 시작 실패: "+(j.detail||JSON.stringify(j)),"warn"); return; }
    runJob(j.job,(r)=>{
      $("#resultCard").style.display="block";
      const m=r.meta||{}, a=r.assets||{};
      const img=p=>`/image?path=${encodeURIComponent(p)}&t=${Date.now()}`;
      let html=
        `<div class="ok">✔ 인포배너 오버레이 소스 (인코딩 없음)</div>`+
        (m.title?`<div class="muted">${m.code} · ${m.actress} · ${m.title}</div>`:'')+
        `<div class="muted" style="margin:6px 0 2px">▼ 미리보기 (앞 2초 / 이후)</div>`+
        `<div style="display:flex;gap:8px;flex-wrap:wrap">`+
          `<img src="${img(r.preview_info)}" style="width:100%;max-width:420px;border-radius:8px">`+
          `<img src="${img(r.preview_wm)}" style="width:100%;max-width:420px;border-radius:8px">`+
        `</div>`+
        `<div class="muted" style="margin:8px 0 2px">▼ 편집 프로그램에 얹을 투명 PNG</div>`+
        `<div>· 프레임(상시): ${a.frame||''}</div>`+
        `<div>· 인포카드(앞 ${hold}초): ${a.info||''}</div>`+
        `<div>· 워터마크(상시): ${a.wm||''}</div>`;
      if(r.out) html+=`<div style="margin-top:6px">영상: ${r.out}</div>`+
        `<video src="/video/stream?path=${encodeURIComponent(r.out)}" controls autoplay muted loop style="width:100%;max-width:640px;border-radius:8px;margin-top:6px;background:#000"></video>`;
      $("#result").innerHTML=html;
      log("✔ 완료 — PNG를 편집 타임라인에 얹으세요(재인코딩 없음)","ok");
    });
  }).catch(e=>log("✖ 오류: "+e,"warn"));
};

// 초기 설정 로드 (양 탭 동기화)
fetch("/config").then(r=>r.json()).then(c=>{
  if(c.llm){ $("#llm").value=c.llm; $("#llmA").value=c.llm; }
  if(c.target_sec){ $("#target").value=c.target_sec; $("#targetA").value=c.target_sec; }
  if(c.whisper_model){ $("#whisper").value=c.whisper_model; $("#whisperA").value=c.whisper_model; }
  if(c.tts_base){ $("#ttsBase").value=c.tts_base; }
}).catch(()=>{});
loadSubTemplates();
