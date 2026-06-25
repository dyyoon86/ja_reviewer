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

function runJob(job, onDone){
  const es = new EventSource(`/events/${job}`);
  es.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if(m.type==="log") log("  "+m.msg);
    else if(m.type==="error"){ log("✖ 오류: "+m.msg,"warn"); es.close(); }
    else if(m.type==="done"){ es.close(); onDone(m.result); }
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
    target_sec:+$("#target").value, llm:$("#llm").value, model:$("#whisper").value
  })}).then(r=>r.json()).then(j=>runJob(j.job, showResult));
};

function needCodeA(){ if(!$("#codeA").value.trim()){ log("품번을 입력하세요","warn"); return false; } return true; }

$("#btnAnalyze").onclick = () => {
  if(!needVideo() || !needCodeA()) return;
  log("── 자동 분석 시작 ──");
  fetch("/analyze",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    path:videoPath, code:$("#codeA").value.trim(),
    target_sec:+$("#targetA").value, llm:$("#llmA").value, model:$("#whisperA").value
  })}).then(r=>r.json()).then(j=>runJob(j.job, (res)=>{
    $("#autoJson").value = JSON.stringify(res.result, null, 2);
    $("#btnRender").disabled = false;
    log("자동 분석 완료 — 결과 확인/수정 후 [확정]","ok");
  }));
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
}

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

$("#btnTts").onclick = () => {
  const code=curCode();
  if(!code){ log("품번을 입력하세요(내레이션 SRT 찾기용)","warn"); return; }
  if(!$("#ttsProfile").value){ log("보이스를 선택하세요(보이스 목록 → 한국어)","warn"); return; }
  log("── 내레이션 음성 생성 시작 ──");
  fetch("/tts",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    code, profile:$("#ttsProfile").value, tts_base:$("#ttsBase").value.trim()||undefined,
    mux:$("#ttsMux").checked
  })}).then(r=>r.json()).then(j=>runJob(j.job, (r)=>{
    $("#resultCard").style.display="block";
    $("#result").innerHTML = `<div class="ok">✔ 내레이션 음성 ${r.count}개 합성</div>`+
      (r.narration_wav?`<div>WAV: ${r.narration_wav}</div>`:'')+
      (r.voiced?`<div>입힌 영상: ${r.voiced}</div>`:'');
    log("✔ 내레이션 음성 완료","ok");
  }));
};

// 초기 설정 로드 (양 탭 동기화)
fetch("/config").then(r=>r.json()).then(c=>{
  if(c.llm){ $("#llm").value=c.llm; $("#llmA").value=c.llm; }
  if(c.target_sec){ $("#target").value=c.target_sec; $("#targetA").value=c.target_sec; }
  if(c.whisper_model){ $("#whisper").value=c.whisper_model; $("#whisperA").value=c.whisper_model; }
  if(c.tts_base){ $("#ttsBase").value=c.tts_base; }
}).catch(()=>{});
