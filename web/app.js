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

// 새 영상 열면 우측 패널(결과·단계배지·AI 입출력)을 초기화 — 이전 영상 정보 잔류 방지
function resetForNewVideo(){
  const rc=$("#resultCard"); if(rc) rc.style.display="none";
  const r=$("#result"); if(r) r.innerHTML="";
  const po=$("#aiPromptOut"); if(po) po.value="";
  const pj=$("#aiPasteJson"); if(pj) pj.value="";
  const aj=$("#autoJson"); if(aj) aj.value="";
  const br=$("#btnRender"); if(br) br.disabled=true;
  setBadge("badgeTranscribe","idle"); setBadge("badgeAi","idle"); setBadge("badgeSubs","idle");
  clearFiles(); setProg(0,"대기 중");
}

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
    resetForNewVideo();
    const mm = j.name.match(/([A-Za-z]{2,6})-?(\d{2,5})/);
    if(mm){ const guess=(mm[1]+"-"+mm[2]).toUpperCase();
      $("#code").value=guess; $("#codeA").value=guess; }
    else { $("#code").value=""; $("#codeA").value=""; }
    refreshSteps();
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

// 섹션 자동 접기 — 어떤 버튼이 속한 접이식 섹션을 접는다(완료 시)
function collapseAcc(sel){
  const b=$(sel); const d=b && b.closest && b.closest("details.acc");
  if(d){ d.open=false; d.scrollIntoView({block:"nearest",behavior:"smooth"}); }
}

function runJob(job, onDone, onErr){
  clearFiles(); setProg(0.04, "시작…");
  const es = new EventSource(`/events/${job}`);
  es.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if(m.type==="log") log("  "+m.msg);
    else if(m.type==="step"){ const fr=m.total?m.n/m.total:0; setProg(fr, `${m.n}/${m.total} · ${m.label}`); }
    else if(m.type==="progress"){ setProg(m.frac, `${m.label||''} ${Math.round((m.frac||0)*100)}%`); }
    else if(m.type==="file"){ addFile(m.label, m.path); }
    else if(m.type==="error"){ log("✖ 오류: "+m.msg,"warn"); setProg(1,"오류","err"); es.close(); if(onErr) onErr(m.msg); }
    else if(m.type==="done"){ setProg(1,"완료","done"); es.close(); if(onDone) onDone(m.result); }
  };
}

// ① 잘라내기 — 품번 불필요 (전용 모달: 진행 바 → 잘라낸 결과만 따로 보기)
let trimResultPath = null, trimResultDur = 0;

function openTrimModal(){
  $("#trimModal").style.display = "flex";
  $("#trimProgress").style.display = "block";
  $("#trimResult").style.display = "none";
  $("#trimTitle").textContent = "선택 구간 잘라내는 중…";
  $("#trimLog").textContent = "";
  trimResultPath = null; trimResultDur = 0;
  setTrimProg(0.04, "준비 중…", "pulse");
}
function closeTrimModal(){
  $("#trimModal").style.display = "none";
  const tv = $("#trimVid"); tv.pause(); tv.removeAttribute("src"); tv.load();
}
function setTrimProg(frac, label, cls){
  const f = $("#trimProgFill");
  f.style.width = Math.round((frac||0)*100)+"%";
  f.className = "prog-fill"+(cls?" "+cls:"");
  if(label!==undefined) $("#trimProgStep").textContent = label;
}
function appendTrimLog(msg){
  const el = $("#trimLog");
  el.textContent += (el.textContent?"\n":"") + msg;
  el.scrollTop = el.scrollHeight;
}
function showTrimResult(res){
  trimResultPath = res.video; trimResultDur = res.duration || 0;
  $("#trimTitle").textContent = "✅ 잘라낸 결과 미리보기";
  $("#trimProgress").style.display = "none";
  $("#trimResult").style.display = "block";
  const tv = $("#trimVid");
  tv.src = `/video/stream?path=${encodeURIComponent(res.video)}&t=${Date.now()}`;
  tv.play().catch(()=>{});
  let extra="";
  if(res.cut_text && res.cut_text.length)
    extra += `<div class="muted" style="margin-top:8px">✂ 삭제: ${res.cut_text.join(", ")}</div>`;
  if(res.keep_text && res.keep_text.length)
    extra += `<div class="muted">남김: ${res.keep_text.join(", ")}</div>`;
  $("#trimMeta").innerHTML =
    `<span class="ok">길이 ${hhmmss(trimResultDur)}</span> · <span class="pth">${res.video}</span>` + extra;
  log(`✔ 잘라내기 완료: ${res.video} (${hhmmss(trimResultDur)})`, "ok");
  if(res.cut_text) log(`  ✂ 삭제 구간: ${res.cut_text.join(", ")}`);
}
function runTrimJob(job){
  const es = new EventSource(`/events/${job}`);
  es.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if(m.type==="log"){ log("  "+m.msg); appendTrimLog(m.msg); }
    else if(m.type==="step"){ setTrimProg(0.06, m.label, "pulse"); }
    else if(m.type==="progress"){
      const fr = 0.06 + 0.92*(m.frac||0);
      setTrimProg(fr, `${m.label||"잘라내는 중"} ${Math.round((m.frac||0)*100)}%`);
    }
    else if(m.type==="file"){ addFile(m.label, m.path); }
    else if(m.type==="error"){
      log("✖ 오류: "+m.msg, "warn"); appendTrimLog("✖ 오류: "+m.msg);
      setTrimProg(1, "오류", "err"); es.close();
    }
    else if(m.type==="done"){ setTrimProg(1, "완료", "done"); es.close(); showTrimResult(m.result); }
  };
  es.onerror = () => { es.close(); };
}

$("#btnTrim").onclick = () => {
  if(!needVideo()) return;
  if(!excludes.length){ log("삭제할 구간을 하나 이상 추가하세요","warn"); return; }
  log("── ① 잘라내기 시작 ──");
  clearFiles();
  openTrimModal();
  fetch("/trim",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    path:videoPath, excludes, code:$("#code").value.trim(),
    precise: !!($("#trimPrecise") && $("#trimPrecise").checked)
  })}).then(r=>r.json()).then(j=>{
    if(!j.job){ appendTrimLog("잘라내기 시작 실패"); setTrimProg(1,"실패","err"); return; }
    runTrimJob(j.job);
  }).catch(e=>{ appendTrimLog("요청 실패: "+e); setTrimProg(1,"실패","err"); });
};

// 결과 모달 액션
$("#trimClose").onclick = closeTrimModal;
$("#trimReopen").onclick = closeTrimModal;
$("#trimModal").addEventListener("click", (e)=>{ if(e.target.id==="trimModal") closeTrimModal(); });
$("#trimUse").onclick = () => {
  if(!trimResultPath) return;
  videoPath = trimResultPath; duration = trimResultDur;
  vid.src = `/video/stream?path=${encodeURIComponent(trimResultPath)}`;
  excludes = []; pendingIn = null; renderEx();
  log(`✔ 잘라낸 영상으로 계속: ${trimResultPath} (${hhmmss(duration)}). 품번 넣고 ②를 누르세요.`,"ok");
  closeTrimModal();
  collapseAcc("#btnTrim");   // ① 완료 → 접기
};

// ── 수동 모드 모달 (프롬프트 만들기 → 붙여넣기) ──
function openManual(){ if(!needCode()) return; $("#manualModal").style.display="flex"; }
function closeManual(){ $("#manualModal").style.display="none"; }
$("#btnManualOpen").onclick = openManual;
$("#manualClose").onclick = closeManual;
$("#manualCancel").onclick = closeManual;
$("#manualModal").addEventListener("click",(e)=>{ if(e.target.id==="manualModal") closeManual(); });

// ② 리뷰 생성(원샷) — 품번 필요
if($("#btnReview")) $("#btnReview").onclick = () => {
  if(!needVideo() || !needCode()) return;
  log("── ② 리뷰 생성 시작 ──");
  fetch("/review",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    path:videoPath, code:$("#code").value.trim(),
    target_sec:+$("#target").value, llm:$("#llm").value, model:$("#whisper").value,
    hint:($("#hint")?$("#hint").value.trim():"")
  })}).then(r=>r.json()).then(j=>runJob(j.job, showResult));
};

// ② 단계별 리뷰 생성 — 각 단계 독립 실행/재실행, 결과는 서버가 파일로 저장
function setBadge(id, state){  // state: done|run|err|idle
  const b=$("#"+id); if(!b) return;
  const sym={done:"✓",run:"…",err:"✗",idle:"—"};
  b.textContent=sym[state]||"—";
  b.className="st-badge"+(state&&state!=="idle"?" "+state:"");
}
let trimAvail = null;
function refreshSteps(code){
  code=(code||$("#code").value||"").trim();
  if(!code){ setBadge("badgeTranscribe","idle"); setBadge("badgeAi","idle"); setBadge("badgeSubs","idle");
    $("#btnUseTrim").style.display="none"; trimAvail=null; return; }
  fetch("/state/"+encodeURIComponent(code)).then(r=>r.json()).then(s=>{
    const st=s.steps||{};
    setBadge("badgeTranscribe", st.transcribe?"done":"idle");
    setBadge("badgeAi", st.ai?"done":"idle");
    setBadge("badgeSubs", st.subs?"done":"idle");
    // 이전에 잘라낸 결과가 있고, 지금 연 영상이 그 trim 자체가 아니면 → 사용 버튼 노출
    const tb=$("#btnUseTrim");
    const isTrim = videoPath && /_trim\.mp4$/i.test(videoPath);
    if(s.trim_exists && s.trim_video && !isTrim){
      trimAvail={path:s.trim_video, dur:s.trim_sec||0};
      tb.textContent=`✂ 이전에 잘라낸 결과 사용 (${hhmmss(s.trim_sec||0)})`;
      tb.style.display="";
    } else { trimAvail=null; tb.style.display="none"; }
  }).catch(()=>{});
}
$("#btnUseTrim").onclick = () => {
  if(!trimAvail) return;
  videoPath=trimAvail.path; duration=trimAvail.dur;
  vid.src=`/video/stream?path=${encodeURIComponent(trimAvail.path)}&t=${Date.now()}`;
  excludes=[]; pendingIn=null; renderEx();
  log(`✔ 이전에 잘라낸 결과 사용: ${trimAvail.path} (${hhmmss(duration)}). 품번 넣고 ① 전사부터 진행하세요.`,"ok");
  $("#btnUseTrim").style.display="none";
};

// ① 전사 — 현재 영상(잘라낸 것) → 일본어 STT 저장
$("#btnStepTranscribe").onclick = () => {
  if(!needVideo() || !needCode()) return;
  const code=$("#code").value.trim();
  log("── ① 전사 시작 ──"); setBadge("badgeTranscribe","run");
  fetch("/step/transcribe",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    path:videoPath, code, model:$("#whisper").value
  })}).then(r=>r.json()).then(j=>runJob(j.job, (res)=>{
    setBadge("badgeTranscribe","done");
    log(`✔ 전사 완료: ${res.count} 세그먼트 → ${res.srt}`,"ok");
    refreshSteps(code);
  }, ()=>setBadge("badgeTranscribe","err")));
};

// ② AI 처리 — 저장된 전사 + 메타 → LLM 압축·번역·내레이션 + 컷
$("#btnStepAi").onclick = () => {
  if(!needCode()) return;
  const code=$("#code").value.trim();
  const mode=$("#mode")?$("#mode").value:"summary";
  log(`── ② AI 처리 시작 (${mode==="highlight"?"하이라이트형·알파컷식":"요약형·짜집기"}) ──`); setBadge("badgeAi","run");
  fetch("/step/ai",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    code, target_sec:+$("#target").value, llm:$("#llm").value,
    mode, hint:($("#hint")?$("#hint").value.trim():""), pos:segPos(), style:narStyle()
  })}).then(r=>r.json()).then(j=>runJob(j.job, (res)=>{
    setBadge("badgeAi","done"); showResult(res);
    log(`✔ AI 처리 완료 (최종 ${Math.round(res.final_sec||0)}초)`,"ok");
    refreshSteps(code);
  }, ()=>setBadge("badgeAi","err")));
};

// ② 수동 모드 — 프롬프트 화면 표시 → 직접 복붙
$("#btnAiPrompt").onclick = () => {
  if(!needCode()) return;
  const code=$("#code").value.trim();
  $("#aiPromptOut").value="프롬프트 생성 중…";
  fetch("/step/ai/prompt",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    code, target_sec:+$("#target").value,
    mode:($("#mode")?$("#mode").value:"summary"), hint:($("#hint")?$("#hint").value.trim():""), pos:segPos(), style:narStyle()
  })}).then(async r=>{
    const j=await r.json().catch(()=>({}));
    if(!r.ok){ $("#aiPromptOut").value=""; log("✖ 프롬프트 생성 실패: "+(j.detail||r.status)+" (① 전사 먼저 / 메타조회 확인)","warn"); return; }
    const ta=$("#aiPromptOut");
    ta.value=j.prompt;
    ta.focus(); ta.select();   // 바로 Ctrl+C 가능
    log(`✔ 전송 프롬프트 표시됨(${j.prompt.length}자). 칸에서 Ctrl+A→Ctrl+C 로 복사해 codex/claude에 붙여넣으세요`,"ok");
  }).catch(e=>{ $("#aiPromptOut").value=""; log("✖ 프롬프트 생성 오류: "+e,"warn"); });
};
$("#btnAiPromptCopy").onclick = async () => {
  const t=$("#aiPromptOut").value;
  if(!t){ log("먼저 ① 프롬프트 만들기를 누르세요","warn"); return; }
  try{ await navigator.clipboard.writeText(t); log("✔ 프롬프트 클립보드 복사됨","ok"); }
  catch(e){ $("#aiPromptOut").focus(); $("#aiPromptOut").select(); log("클립보드 권한 없음 → 칸에서 Ctrl+C 로 복사하세요","warn"); }
};
$("#btnAiManual").onclick = () => {
  if(!needCode()) return;
  const code=$("#code").value.trim();
  const txt=$("#aiPasteJson").value.trim();
  if(!txt){ log("결과 JSON을 먼저 붙여넣으세요","warn"); return; }
  log("── ② 수동 결과 적용 시작 ──"); setBadge("badgeAi","run");
  fetch("/step/ai/manual",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    code, result:txt
  })}).then(r=>r.json()).then(j=>runJob(j.job,(res)=>{
    setBadge("badgeAi","done"); showResult(res); closeManual();
    log(`✔ 수동 결과 적용 완료 (최종 ${Math.round(res.final_sec||0)}초)`,"ok");
    refreshSteps(code);
  }, ()=>setBadge("badgeAi","err")));
};

// ③ 자막 — 저장된 plan.json → 한글 대사/내레이션 SRT
$("#btnStepSubs").onclick = () => {
  if(!needCode()) return;
  const code=$("#code").value.trim();
  log("── ③ 자막 생성 시작 ──"); setBadge("badgeSubs","run");
  fetch("/step/subs",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    code
  })}).then(r=>r.json()).then(j=>runJob(j.job, (res)=>{
    setBadge("badgeSubs","done"); showResult(res);
    log("✔ 자막 생성 완료","ok");
    refreshSteps(code);
  }, ()=>setBadge("badgeSubs","err")));
};

// ③-b 내레이션 재생성 — 컷·대사 유지, 내레이션만 6슬롯 규칙으로 다시 쓰기
$("#btnRegenNar").onclick = () => {
  if(!needCode()) return;
  const code=$("#code").value.trim();
  log("── 🔁 내레이션 재생성 시작 (컷·대사 유지) ──"); setBadge("badgeSubs","run");
  fetch("/regen/narration",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    code
  })}).then(r=>r.json()).then(j=>runJob(j.job, (res)=>{
    setBadge("badgeSubs","done");
    log(`✔ 내레이션 재생성 완료 (${res.count}줄) — 마음에 들면 TTS·굽기를 다시 실행하세요`,"ok");
    refreshSteps(code);
  }, ()=>setBadge("badgeSubs","err")));
};

// ③-c 구간 재선정 — LLM이 keep을 다시 골라 final.mp4 + 자막 전부 재생성
$("#btnReplan").onclick = () => {
  if(!needCode()) return;
  const code=$("#code").value.trim();
  if(!confirm(`[${code}] keep 구간을 다시 골라 final.mp4와 자막을 전부 다시 만듭니다.\n기존 plan.json·final.mp4를 덮어씁니다. 진행할까요?`)) return;
  log("── 🔁 구간 재선정 시작 (plan·final·자막 재생성) ──"); setBadge("badgeAi","run"); setBadge("badgeSubs","run");
  fetch("/regen/plan",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    code, target_sec:+$("#target").value, llm:$("#llm").value
  })}).then(r=>r.json()).then(j=>runJob(j.job, (res)=>{
    setBadge("badgeAi","done"); setBadge("badgeSubs","done"); showResult(res);
    log(`✔ 구간 재선정 완료 (최종 ${Math.round(res.final_sec||0)}초) — TTS·굽기를 다시 실행하세요`,"ok");
    refreshSteps(code);
  }, ()=>{ setBadge("badgeAi","err"); setBadge("badgeSubs","err"); }));
};

// ── LLM(codex/claude) 연결 확인 ──
function setLlmBadge(id, name, st){ // st: ok|fail|run|idle
  const b=$("#"+id); if(!b) return;
  const sym={ok:"✓",fail:"✗",run:"…",idle:"—"};
  b.textContent=`${name} ${sym[st]||"—"}`;
  b.className="st-badge"+(st==="ok"?" done":st==="fail"?" err":st==="run"?" run":"");
}
$("#btnLlmCheck").onclick = () => {
  setLlmBadge("badgeCodex","codex","run"); setLlmBadge("badgeClaude","claude","run");
  $("#llmStatus").textContent="확인 중… (각 CLI에 짧은 프롬프트 왕복, 10~30초)";
  log("── LLM 연결 확인 ──");
  fetch("/llm/check").then(r=>r.json()).then(d=>{
    ["codex","claude"].forEach(n=>{
      const x=d[n]||{}; const id=n==="codex"?"badgeCodex":"badgeClaude";
      setLlmBadge(id, n, x.ok?"ok":"fail");
      log(`  ${n}: ${x.ok?"✓":"✗"} ${x.msg||""}`, x.ok?"ok":"warn");
    });
    $("#llmStatus").textContent="확인 완료 (✓=설치·로그인·응답 정상)";
  }).catch(e=>{
    setLlmBadge("badgeCodex","codex","fail"); setLlmBadge("badgeClaude","claude","fail");
    log("✖ 연결 확인 실패: "+e,"warn");
  });
};

// 품번 바뀌면 진행 상태 자동 갱신
$("#code").addEventListener("change", ()=>refreshSteps());
$("#code").addEventListener("blur", ()=>refreshSteps());

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
  collapseAcc("#btnReview");   // ② 완료(원샷/자막단계/자동확정) → 접기
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
    refreshSteps(code);
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
    seed:$("#ttsSeed").value!==""?+$("#ttsSeed").value:undefined, mux:$("#ttsMux").checked,
    orig_audio:origAudio(), duck_level:duckLevel()
  })}).then(r=>r.json()).then(j=>runJob(j.job, (r)=>{
    $("#resultCard").style.display="block";
    $("#result").innerHTML = `<div class="ok">✔ 내레이션 음성 ${r.count}개 합성</div>`+
      (r.narration_wav?`<div>WAV: ${r.narration_wav}</div>`:'')+
      (r.voiced?`<div>입힌 영상: ${r.voiced}</div>`:'');
    log("✔ 내레이션 음성 완료","ok");
    collapseAcc("#btnTts");   // ③ 완료 → 접기
  }));
}
$("#btnTts").onclick = runTts;

// ── 자막 입히기(하드섭) + 템플릿 ────────────────────────────────────────────
function collectStyle(p){
  return { font:$("#"+p+"Font").value||"Malgun Gothic", size:+$("#"+p+"Size").value||40,
    bold:$("#"+p+"Bold").checked, color:$("#"+p+"Color").value, outline_color:$("#"+p+"OutColor").value,
    outline:parseFloat($("#"+p+"Outline").value)||0, v:$("#"+p+"V").value, h:$("#"+p+"H").value,
    margin:+$("#"+p+"Margin").value||0,
    anim:($("#"+p+"Anim") ? $("#"+p+"Anim").value : "none") };
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
  if($("#"+p+"Anim")) $("#"+p+"Anim").value=st.anim||"none";
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
  const banner=$("#burnBanner") ? $("#burnBanner").checked : true;
  log("── 자막 입히기 시작 ──"+(banner?" (배너·워터마크 동시)":""));
  fetch("/burn",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    code, styles:allStyles(), banner
  })}).then(r=>r.json()).then(j=>runJob(j.job,(r)=>{
    $("#resultCard").style.display="block";
    $("#result").innerHTML=`<div class="ok">✔ 자막${r.banner?"·배너":""} 입힌 영상</div><div>${r.subbed}</div>`;
    log("✔ 자막 영상 완료"+(r.banner?" (배너 포함)":""),"ok");
    collapseAcc("#btnBurn");   // ④ 완료 → 접기
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
  const alpha=$("#icAlpha") && $("#icAlpha").checked;
  const alpha_format=($("#icAlphaFormat") && $("#icAlphaFormat").value) || "qtrle";
  const alpha_duration=parseFloat($("#icAlphaDur") && $("#icAlphaDur").value)||null;
  const fps=parseInt($("#icAlphaFps") && $("#icAlphaFps").value)||30;
  log("── 인포배너 소스 생성 시작 ──"+(encode?(source?` (영상 오버레이 인코딩: ${source})`:" (데모 인코딩)"):" (PNG만·인코딩 없음)")
      +(alpha?` + 투명 오버레이 영상(${alpha_format})`:""));
  fetch("/infocard",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    code, hold, source, encode, alpha, alpha_format, alpha_duration, fps
  })}).then(r=>r.json()).then(j=>{
    if(!j.job){ log("✖ 시작 실패: "+(j.detail||JSON.stringify(j)),"warn"); return; }
    runJob(j.job,(r)=>{
      $("#resultCard").style.display="block";
      const m=r.meta||{}, a=r.assets||{};
      const img=p=>`/image?path=${encodeURIComponent(p)}&t=${Date.now()}`;
      // 산출물 1줄 — 경로 + 다운로드 링크
      const dl=(label,p)=>p?`<div>· ${label}: ${p} `+
        `<a href="/download?path=${encodeURIComponent(p)}" download style="margin-left:4px">⬇ 받기</a></div>`:"";
      let html=
        `<div class="ok">✔ 인포배너 오버레이 소스 (인코딩 없음)</div>`+
        (m.title?`<div class="muted">${m.code} · ${m.actress} · ${m.title}</div>`:'')+
        (r.preview_anim?
          `<div class="muted" style="margin:6px 0 2px">▼ 움직이는 미리보기 (인포카드→워터마크)</div>`+
          `<video src="/video/stream?path=${encodeURIComponent(r.preview_anim)}&t=${Date.now()}" controls autoplay muted loop style="width:100%;max-width:640px;border-radius:8px;background:#000"></video>`
          :'')+
        `<div class="muted" style="margin:6px 0 2px">▼ 스틸 미리보기 (앞 2초 / 이후)</div>`+
        `<div style="display:flex;gap:8px;flex-wrap:wrap">`+
          `<img src="${img(r.preview_info)}" style="width:100%;max-width:420px;border-radius:8px">`+
          `<img src="${img(r.preview_wm)}" style="width:100%;max-width:420px;border-radius:8px">`+
        `</div>`+
        `<div class="muted" style="margin:8px 0 2px">▼ 편집 프로그램에 얹을 투명 PNG</div>`+
        dl("프레임(상시)", a.frame)+
        dl(`인포카드(앞 ${hold}초)`, a.info)+
        dl("워터마크(상시)", a.wm)+
        (a.frame&&a.info&&a.wm?
          `<div style="margin-top:4px"><a href="/download/zip?paths=`+
          encodeURIComponent([a.frame,a.info,a.wm].join("|"))+
          `&name=${encodeURIComponent((m.code||'infocard')+'_투명PNG.zip')}" download>⬇ PNG 3장 한번에 (zip)</a></div>`:"");
      if(r.overlay) html+=
        `<div class="muted" style="margin:10px 0 2px">▼ 가운데 투명 오버레이 영상 — 편집기 <b>상위 트랙</b>에 얹으세요</div>`+
        `<div>· ${r.overlay}</div>`+
        `<div style="margin-top:4px"><a href="/download?path=${encodeURIComponent(r.overlay)}" download>⬇ 오버레이 .mov 다운로드</a></div>`+
        `<div class="muted" style="margin-top:2px">※ 브라우저·폰에서 재생 안 되는 게 정상입니다(편집용 코덱). 프리미어/캡컷에서 여세요.</div>`;
      if(r.out) html+=`<div style="margin-top:6px">영상: ${r.out}</div>`+
        `<video src="/video/stream?path=${encodeURIComponent(r.out)}" controls autoplay muted loop style="width:100%;max-width:640px;border-radius:8px;margin-top:6px;background:#000"></video>`;
      $("#result").innerHTML=html;
      log(r.overlay?"✔ 완료 — 투명 오버레이 .mov를 편집기 상위 트랙에 얹으세요(원본 재인코딩 없음)"
                   :"✔ 완료 — PNG를 편집 타임라인에 얹으세요(재인코딩 없음)","ok");
      collapseAcc("#btnInfocard");   // ⑤ 완료 → 접기
    });
  }).catch(e=>log("✖ 오류: "+e,"warn"));
};

// ── 출력 폴더 설정 ──
function saveOutDir(){
  const v=$("#outDir").value.trim(); if(!v) return;
  fetch("/config",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({out_dir:v})})
    .then(r=>r.json()).then(()=>log(`✔ 출력 폴더 설정: ${v}`,"ok")).catch(()=>{});
}
$("#btnOutDir").onclick = () => {
  fetch("/browse_dir",{method:"POST"}).then(r=>r.json()).then(r=>{
    if(r.path){ $("#outDir").value=r.path; saveOutDir(); }
    else if(r.error){ log("폴더 다이얼로그 실패: "+r.error+" → 경로 직접 입력","warn"); }
  }).catch(()=>{});
};
$("#outDir").addEventListener("change", saveOutDir);

// ── 작업 큐 (병렬 자동 처리) ────────────────────────────────────────────────
const Q_ST = {  // status → [배지문구, css클래스]
  needs_code:["품번?","nc"], queued:["대기","qd"], running:["진행","run"],
  review:["✋검수","rev"], done:["✓완료","done"], error:["✗오류","err"], held:["⏸정지","held"]
};
let qPrev = {};   // id → status (검수대기/오류 전환 알림용)

function segPos(){ return $("#segPos") ? $("#segPos").value : "mid"; }
function narStyle(){ return $("#narStyle") ? $("#narStyle").value : "3min"; }
function origAudio(){ return $("#ttsOrig") ? $("#ttsOrig").value : "duck"; }
function duckLevel(){ return $("#ttsDuck") ? parseFloat($("#ttsDuck").value) : 0.3; }

function qPipeline(){
  return { clean:$("#qpClean")?$("#qpClean").checked:false,
           transcribe:$("#qpTranscribe").checked, ai:$("#qpAi").checked,
           subs:$("#qpSubs").checked, banner:$("#qpBanner").checked,
           tts:$("#qpTts").checked, burn:$("#qpBurn").checked };
}
function qOpts(){
  const o = { model:$("#whisper").value, llm:$("#llm").value,
              target_sec:+$("#target").value, mode:($("#mode")?$("#mode").value:"summary"),
              pos:segPos(), style:narStyle(), orig_audio:origAudio(), duck_level:duckLevel() };
  if($("#qpTts").checked){
    o.tts_profile=$("#ttsProfile").value; o.tts_base=$("#ttsBase").value.trim()||undefined;
    o.tts_seed=$("#ttsSeed").value!==""?+$("#ttsSeed").value:undefined; o.tts_mux=true;
  }
  return o;
}

$("#btnQAdd").onclick = async () => {
  if($("#qpTts").checked && !$("#ttsProfile").value){
    log("⚠ 큐 TTS 자동 진행: 먼저 ③에서 보이스를 선택하세요(보이스 목록 → 한국어)","warn"); return;
  }
  const r = await fetch("/browse_multi",{method:"POST"}).then(r=>r.json()).catch(()=>({paths:[]}));
  if(r.error){ log("파일 다이얼로그 실패: "+r.error,"warn"); return; }
  if(!r.paths || !r.paths.length) return;
  const j = await fetch("/queue/add",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({paths:r.paths, pipeline:qPipeline(), opts:qOpts()})}).then(r=>r.json());
  log(`📋 작업 큐에 ${j.added.length}개 추가 — 수작업하는 동안 자동 처리됩니다`,"ok");
};

function qAction(id, action, extra){
  return fetch("/queue/item/"+id,{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.assign({action}, extra||{}))})
    .then(async r=>{ if(!r.ok){ const e=await r.json().catch(()=>({})); log("✖ "+(e.detail||r.status),"warn"); } });
}

function qOpen(it){
  // 큐 항목 클릭 → 우측 상세 화면에 로드(백그라운드 잡은 계속 돌아감).
  // ★ 자동 모드에선 플레이어·작업 패널이 숨겨져 있어 클릭해도 아무것도 안 보인다
  //   → 검수/이어작업은 수동 모드의 화면이 필요하므로 자동으로 전환한다.
  if(document.body.dataset.mode === "auto"){
    setMode("manual");
    log(`🛠 수동 모드로 전환 — ${it.code||""} 검수/이어작업 화면입니다 (자동으로 돌아가려면 상단 🔮자동)`, "ok");
  }
  // 검수는 '완성에 가까운 것'부터 본다: 자막 번인본 > 음성 입힌 것 > 컷 결과 > 원본
  const pick = it.subbed || it.voiced || it.final || it.video;
  openVideo(pick);
  setTimeout(()=>{  // openVideo의 파일명 자동추정을 큐의 확정 품번으로 덮어씀
    if(it.code){ $("#code").value=it.code; $("#codeA").value=it.code; refreshSteps(it.code); }
  }, 300);
}

function renderWatch(w){
  const st=$("#watchStat"); if(!st) return;
  if(!w){ st.style.display="none"; return; }
  if(w.enabled){
    st.style.display="block";
    st.textContent = `🔮 감시 중: ${w.dir} · 투입 ${w.added}개` + (w.waiting?` · 다운로드 대기 ${w.waiting}개`:"");
  }else if((w.dir||"").trim() && $("#watchOn") && $("#watchOn").checked){
    st.style.display="block";
    st.textContent = "⚠ 감시 폴더가 없거나 접근 불가: " + w.dir;
  }else{
    st.style.display="none";
  }
}

function renderQueue(snap){
  renderWatch(snap.watch);
  const ul=$("#qlist"); if(!ul) return;
  const items=snap.items||[];
  $("#qCount").textContent = items.length ? `${items.filter(i=>i.status==="running").length}▶ / ${items.length}` : "";
  if(snap.lanes && $("#qLaneGpu")!==document.activeElement) $("#qLaneGpu").value=snap.lanes.gpu;
  if(snap.lanes && $("#qLaneAi")!==document.activeElement) $("#qLaneAi").value=snap.lanes.ai;
  if(!items.length){ ul.innerHTML='<li class="qempty muted">비어 있음 — 영상을 추가하면<br>수작업하는 동안 자동 처리됩니다</li>'; qPrev={}; return; }
  ul.innerHTML="";
  items.forEach((it,i)=>{
    // 상태 전환 알림 (검수대기/오류/완료 도달 시 로그로)
    if(qPrev[it.id] && qPrev[it.id]!==it.status){
      if(it.status==="review") log(`✋ [큐] ${it.code} 검수대기 — 좌측 목록에서 클릭해 확인하세요`,"ok");
      if(it.status==="error")  log(`✗ [큐] ${it.code||it.name} 오류: ${it.error||""}`,"warn");
      if(it.status==="done")   log(`✓ [큐] ${it.code} 전체 완료`,"ok");
    }
    qPrev[it.id]=it.status;
    const [btxt,bcls]=Q_ST[it.status]||["?",""];
    const li=document.createElement("li"); li.className="q-item "+bcls;
    const pct=Math.round((it.progress||0)*100);
    // 큐 순서 = 묶음 영상의 편집 순서 → 첫/마지막 꼭지는 전환 문구가 달라진다 (1개뿐이면 단독)
    const coded=items.filter(x=>(x.code||"").trim());
    const ci=coded.indexOf(it), cn=coded.length;
    const posTag = ci<0 ? "" : (cn===1 ? "단독" : (ci===0 ? "먼저" : (ci===cn-1 ? "마지막" : "다음은")));
    li.innerHTML =
      `<div class="q-top"><b class="q-code">${it.code||"(품번?)"}</b>`+
      (posTag?`<span class="q-badge" title="묶음 리뷰에서 이 작품의 전환 문구(큐 순서로 자동 판정)">${ci+1}. ${posTag}</span>`:"")+
      `<span class="q-badge ${bcls}">${btxt}</span></div>`+
      `<div class="q-name" title="${it.video}">${it.name}</div>`+
      (it.status==="running"?`<div class="prog q-prog"><div class="prog-fill" style="width:${pct}%"></div></div>`:"")+
      `<div class="q-stage">${it.stage_label||""}</div>`+
      `<div class="q-actions"></div>`;
    const acts=li.querySelector(".q-actions");
    const mk=(t,title,fn)=>{ const b=document.createElement("button"); b.textContent=t; b.title=title;
      b.onclick=(e)=>{ e.stopPropagation(); fn(); }; acts.appendChild(b); };
    if(it.status==="needs_code")
      mk("품번 입력","품번을 입력해 큐 진행",()=>{ const c=prompt(`품번 입력 (${it.name})`); if(c) qAction(it.id,"set_code",{code:c}); });
    if(it.status!=="running" && i>0)            mk("▲","위로 (영상에서 앞 순서로)",()=>qAction(it.id,"move",{delta:-1}));
    if(it.status!=="running" && i<items.length-1) mk("▼","아래로 (영상에서 뒷 순서로)",()=>qAction(it.id,"move",{delta:1}));
    if(["queued","running"].includes(it.status)) mk("⏸","일시정지(현재 단계 후 중단)",()=>qAction(it.id,"hold"));
    if(["held","error","review","done"].includes(it.status)) mk("▶","이어서/다시 실행(완료 단계는 건너뜀)",()=>qAction(it.id,"resume"));
    if(it.status!=="running") mk("✕","목록에서 제거(파일은 유지)",()=>qAction(it.id,"remove"));
    li.onclick=()=>qOpen(it);
    ul.appendChild(li);
  });
}

function connectQueue(){
  try{
    const es=new EventSource("/queue/events");
    es.onmessage=(ev)=>{ try{ renderQueue(JSON.parse(ev.data)); }catch(_){} };
    es.onerror=()=>{ es.close(); setTimeout(connectQueue, 3000); };  // 서버 재시작 등 → 재연결
  }catch(_){ setTimeout(connectQueue, 3000); }
}
connectQueue();
fetch("/queue").then(r=>r.json()).then(renderQueue).catch(()=>{});

$("#btnQClear").onclick=()=>fetch("/queue/clear_finished",{method:"POST"});

// ── 폴더 감시(풀오토) — config에 저장, 서버 워처가 5초마다 확인 ──
function saveWatch(){
  fetch("/config",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({watch_enabled:$("#watchOn").checked, watch_dir:$("#watchDir").value.trim()})});
  if($("#watchOn").checked)
    log(`🔮 폴더 감시 켜짐: ${$("#watchDir").value.trim()||"(폴더 미지정)"} — 여기 떨어지는 영상은 1분 단독 완성본까지 자동 처리`,"ok");
  else log("폴더 감시 꺼짐");
}
$("#watchOn").onchange=saveWatch;
$("#watchDir").onchange=saveWatch;
fetch("/config").then(r=>r.json()).then(c=>{
  if($("#watchOn")) $("#watchOn").checked=!!c.watch_enabled;
  if($("#watchDir")) $("#watchDir").value=c.watch_dir||"";
}).catch(()=>{});
function saveLanes(){
  fetch("/config",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({queue_gpu:+$("#qLaneGpu").value||1, queue_ai:+$("#qLaneAi").value||2})});
}
$("#qLaneGpu").addEventListener("change",saveLanes);
$("#qLaneAi").addEventListener("change",saveLanes);
$("#btnQCollapse").onclick=()=>{
  const s=$("#qside"); s.classList.toggle("collapsed");
  $("#btnQCollapse").textContent = s.classList.contains("collapsed")?"⟩":"⟨";
};

// 초기 설정 로드 (양 탭 동기화)
fetch("/config").then(r=>r.json()).then(c=>{
  if(c.llm){ $("#llm").value=c.llm; $("#llmA").value=c.llm; }
  if(c.target_sec){ $("#target").value=c.target_sec; $("#targetA").value=c.target_sec; }
  if(c.whisper_model){ $("#whisper").value=c.whisper_model; $("#whisperA").value=c.whisper_model; }
  if(c.tts_base){ $("#ttsBase").value=c.tts_base; }
  if(c.out_dir){ $("#outDir").value=c.out_dir; }
  if(c.queue_gpu){ $("#qLaneGpu").value=c.queue_gpu; }
  if(c.queue_ai){ $("#qLaneAi").value=c.queue_ai; }
}).catch(()=>{});
loadSubTemplates();
refreshSteps();

// ── 렌더 전 미리보기 ────────────────────────────────────────────────────────
// 굽기(burn_subs)와 동일한 타이밍 규칙을 브라우저에서 재현한다.
// 인포카드: 0~0.4s 페이드인(블러→선명) · hold 후 fade 동안 페이드아웃(선명→블러)
// 워터마크: wm_start 부터 fade 동안 페이드인 + 위에서 slide
let pvData=null, pvRaf=0;

const pvClamp=(v,a,b)=>Math.max(a,Math.min(b,v));

function pvApply(t){
  if(!pvData) return;
  const A=pvData.anim, hold=parseFloat($("#pvHold").value)||A.hold, fade=A.fade;
  const showBanner=$("#pvShowBanner").checked, showWm=$("#pvShowWm").checked;
  const showSubs=$("#pvShowSubs").checked;
  const fr=$("#pvFrame"), ic=$("#pvInfo"), wm=$("#pvWm");
  // 레이어는 1920 기준으로 그려졌다 → 표시 크기에 맞춰 blur·slide를 같은 비율로 환산
  const k = ($("#pvWrap").clientWidth || 960) / (pvData.canvas_w || 1920);

  // 프레임 — 상시
  fr.style.opacity = showBanner ? 1 : 0;

  // 인포카드 — 굽기(_banner_filter)와 동일: fade 필터는 알파를 곱한다
  //   선명본 알파 = fadein(0,0.4) × fadeout(hold,fade)
  const a = pvClamp(t/0.4, 0, 1) * (1 - pvClamp((t-hold)/fade, 0, 1));
  //   흐린본 알파 = fadeout(0,fade) 와 fadein(hold,fade) 중 큰 값 → 체감 블러량
  const blurW = Math.max(1 - pvClamp(t/fade, 0, 1), pvClamp((t-hold)/fade, 0, 1));
  ic.style.opacity = showBanner ? a : 0;
  ic.style.filter  = `blur(${(blurW * A.blur * k).toFixed(2)}px)`;

  // 워터마크 — 페이드인 + 위에서 슬라이드(굽기의 y 오프셋과 동일 픽셀량)
  const wa = pvClamp((t-A.wm_start)/fade, 0, 1);
  wm.style.opacity = showWm ? wa : 0;
  wm.style.transform = `translateY(${(-A.wm_slide * (1-wa) * k).toFixed(2)}px)`;

  // 자막 — 현재 시각에 걸린 것만
  const box=$("#pvSubs");
  box.style.display = showSubs ? "block" : "none";
  if(showSubs){
    const cur=(pvData.subs||[]).filter(s=>t>=s.start && t<=s.end);
    box.innerHTML = cur.map(s=>pvSubHtml(s)).join("");
  }
  $("#pvClock").textContent = t.toFixed(2)+"s";
}

// 미리보기 자막 스타일 — ④에서 편집 중인 값을 우선 쓴다(저장 전에도 바로 확인).
// UI가 아직 안 채워졌으면 서버가 준 값으로 폴백.
function pvStyle(key){
  try{
    const live=allStyles();
    if(live && live[key] && live[key].font) return live[key];
  }catch(e){}
  return (pvData.styles||{})[key] || {};
}

function pvSubHtml(s){
  const st=pvStyle(s.style);
  const W=$("#pvWrap").clientWidth||960, k=W/1920;   // 1080p 기준 → 표시 배율
  const size=Math.max(10,(st.size||38)*k), mg=(st.margin||40)*k;
  const ol=Math.max(1,(st.outline||2)*k);
  const col=st.color||"#fff", oc=st.outline_color||"#000";
  const vpos = st.v==="top" ? `top:${mg}px` : st.v==="middle" ? `top:50%;transform:translateY(-50%)` : `bottom:${mg}px`;
  const hpos = st.h==="right" ? `right:${mg}px;text-align:right`
             : st.h==="left"  ? `left:${mg}px;text-align:left`
             : `left:0;right:0;text-align:center`;
  const shadow=`-${ol}px -${ol}px 0 ${oc}, ${ol}px -${ol}px 0 ${oc}, -${ol}px ${ol}px 0 ${oc}, ${ol}px ${ol}px 0 ${oc}`;
  // 설정한 폰트를 그대로 쓴다(하드코딩 금지). 그 폰트가 없는 OS에서는 뒤 후보로 폴백.
  // style="..." 안에 들어가므로 큰따옴표를 쓰면 속성이 끊긴다 → 작은따옴표로 감싼다.
  const fam=`'${(st.font||'Malgun Gothic').replace(/['"\\]/g,'')}','Malgun Gothic','Noto Sans KR',sans-serif`;
  return `<div style="position:absolute;${vpos};${hpos};padding:0 ${mg}px;`+
         `font-family:${fam};font-weight:${st.bold?700:400};`+
         `font-size:${size}px;line-height:1.25;color:${col};text-shadow:${shadow};`+
         `white-space:pre-wrap">${(s.text||"").replace(/</g,"&lt;")}</div>`;
}

function pvLoop(){
  const v=$("#pvVideo");
  if(v && !v.paused && !v.ended) pvApply(v.currentTime);
  pvRaf=requestAnimationFrame(pvLoop);
}

$("#btnPreview").onclick=()=>{
  const code=($("#pvCode").value||curCode()||"").trim();
  if(!code){ log("품번을 입력하세요","warn"); return; }
  log("── 미리보기 준비 중(배너 레이어 없으면 자동 생성) ──");
  fetch(`/preview/data?code=${encodeURIComponent(code)}`).then(r=>r.json()).then(j=>{
    if(j.detail){ log("✖ "+j.detail,"warn"); return; }
    pvData=j;
    const L=j.layers||{};
    const img=p=>`/image?path=${encodeURIComponent(p)}&t=${Date.now()}`;
    const set=(el,p)=>{ if(p){ el.src=img(p); el.style.display="block"; } else el.style.display="none"; };
    set($("#pvFrame"),L.frame); set($("#pvInfo"),L.info); set($("#pvWm"),L.wm);
    if(L.error) log("※ 배너 레이어 생성 실패: "+L.error,"warn");
    $("#pvVideo").src=`/video/stream?path=${encodeURIComponent(j.video)}`;
    $("#pvStage").style.display="block";
    log(`✔ 미리보기 — ${j.video} (${(j.duration||0).toFixed(1)}s, 자막 ${(j.subs||[]).length}줄)`,"ok");
    if(!pvRaf) pvLoop();
  }).catch(e=>log("✖ 오류: "+e,"warn"));
};

// 스크럽·일시정지 상태에서도 즉시 반영
["seeking","seeked","timeupdate","pause","loadedmetadata"].forEach(ev=>{
  const v=document.getElementById("pvVideo");
  if(v) v.addEventListener(ev,()=>pvApply(v.currentTime));
});
["pvShowBanner","pvShowWm","pvShowSubs","pvHold"].forEach(id=>{
  const el=document.getElementById(id);
  if(el) el.addEventListener("input",()=>{ const v=$("#pvVideo"); if(v) pvApply(v.currentTime); });
});

// ④ 자막 스타일(폰트·크기·색·외곽선·위치)을 만지면 미리보기에 즉시 반영
// select/checkbox는 input이 안 뜨는 브라우저가 있어 change도 함께 건다.
["dlg","dlm","nar","emp","inf"].forEach(p=>{
  ["Font","Size","Bold","Color","OutColor","Outline","V","H","Margin","Anim"].forEach(f=>{
    const el=document.getElementById(p+f);
    if(!el) return;
    const repaint=()=>{
      const v=$("#pvVideo");
      if(v && pvData && $("#pvStage").style.display!=="none") pvApply(v.currentTime);
    };
    el.addEventListener("input", repaint);
    el.addEventListener("change", repaint);
  });
});

// 미리보기에서 체크한 요소만 굽기 — 배너=프레임+인포카드, 워터마크, 자막
$("#btnPvRender").onclick=()=>{
  const code=($("#pvCode").value||curCode()||"").trim();
  if(!code){ log("품번을 입력하세요","warn"); return; }
  const b=$("#pvShowBanner").checked, w=$("#pvShowWm").checked, s=$("#pvShowSubs").checked;
  if(!b && !w && !s){ log("구울 요소가 없습니다 — 하나 이상 체크하세요","warn"); return; }
  const parts={frame:b, info:b, wm:w, subs:s};
  const picked=[b?"배너":null, w?"워터마크":null, s?"자막":null].filter(Boolean).join(", ");
  log(`── 렌더 시작 (${picked}) ──`);
  fetch("/burn",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify({
    code, styles:allStyles(), banner:(b||w), parts
  })}).then(r=>r.json()).then(j=>{
    if(!j.job){ log("✖ 시작 실패: "+(j.detail||JSON.stringify(j)),"warn"); return; }
    runJob(j.job,(r)=>{
      $("#resultCard").style.display="block";
      $("#result").innerHTML=`<div class="ok">✔ 렌더 완료 (${(r.parts||[]).join(", ")||picked})</div>`+
        `<div>${r.subbed}</div>`;
      log("✔ 렌더 완료 — "+r.subbed,"ok");
    });
  }).catch(e=>log("✖ 오류: "+e,"warn"));
};

// ── 실행 전 통합 점검 ───────────────────────────────────────────────────────
// 필수 항목이 하나라도 실패하면 파이프라인이 그 단계에서 멈춘다. 선택 항목은 건너뛰면 됨.
$("#btnHealth").onclick=()=>{
  const deep=$("#healthDeep") ? $("#healthDeep").checked : true;
  const ul=$("#healthList");
  ul.innerHTML=`<li><span class="st-badge run">…</span><span class="st-label">점검 중`+
               (deep?` (LLM 응답·chromium 실행 확인 — 수십 초 걸릴 수 있습니다)`:``)+`</span></li>`;
  $("#btnHealth").disabled=true;
  log("── 실행 전 점검 시작 ──");
  fetch(`/health?deep=${deep?"true":"false"}`).then(r=>r.json()).then(j=>{
    ul.innerHTML="";
    Object.entries(j.items).forEach(([k,v])=>{
      const li=document.createElement("li");
      const badge = v.ok ? `<span class="st-badge done">OK</span>`
                         : `<span class="st-badge ${v.required?"warn":""}">${v.required?"필수":"선택"}</span>`;
      li.innerHTML = badge +
        `<span class="st-label"><b>${v.label}</b> `+
        `<span class="muted">${v.msg}</span>`+
        (v.ok?``:`<div class="muted" style="margin-top:2px">→ 막히는 단계: ${v.blocks}</div>`)+
        `</span>`;
      if(!v.ok) li.style.opacity="1";
      ul.appendChild(li);
    });
    if(j.ok){
      log(`✔ 점검 완료 — 필수 항목 모두 정상 (${j.checked}개 검사, 선택 실패 ${j.fail})`,"ok");
    }else{
      log(`✖ 점검 완료 — 필수 항목 ${j.blocking}개 실패. 아래 목록을 확인하세요`,"warn");
    }
  }).catch(e=>{
    ul.innerHTML=`<li><span class="st-badge warn">✖</span><span class="st-label">점검 실패: ${e}</span></li>`;
    log("✖ 점검 실패: "+e,"warn");
  }).finally(()=>{ $("#btnHealth").disabled=false; });
};

// ── 자막 편집 (③ 결과를 고치고 저장) ────────────────────────────────────────
let subsData=null;
function subsRow(kind, it){
  const isN = kind==="nar";
  const sel = isN
    ? `<select data-f="style">${(subsData.styles||["기본","강조","정보"]).map(s=>`<option ${((it.style||"기본")===s)?"selected":""}>${s}</option>`).join("")}</select>`
    : `<select data-f="speaker">${(subsData.speakers||["여","남"]).map(s=>`<option ${((it.speaker||"여")===s)?"selected":""}>${s}</option>`).join("")}</select>`;
  const d=document.createElement("div"); d.className="subs-row";
  d.innerHTML =
    `<input type="number" step="0.1" data-f="start" value="${(+it.start||0).toFixed(1)}" style="width:60px" title="시작초">`+
    `<input type="number" step="0.1" data-f="end" value="${(+it.end||0).toFixed(1)}" style="width:60px" title="끝초">`+
    sel+
    `<input type="text" data-f="text" value="${(it.text||it.ko||"").replace(/"/g,'&quot;')}" style="flex:1" placeholder="자막 텍스트">`+
    `<button class="mini x" title="줄 삭제">✕</button>`;
  d.querySelector("button.x").onclick=()=>d.remove();
  return d;
}
function subsRender(){
  const n=$("#subsNar"), g=$("#subsDlg"); n.innerHTML=""; g.innerHTML="";
  (subsData.narration||[]).forEach(it=>n.appendChild(subsRow("nar",it)));
  (subsData.dialogue||[]).forEach(it=>g.appendChild(subsRow("dlg",it)));
  $("#subsEditor").style.display="block"; $("#btnSubsSave").disabled=false;
}
function subsCollect(box, kind){
  return [...box.querySelectorAll(".subs-row")].map(r=>{
    const g=f=>r.querySelector(`[data-f="${f}"]`);
    const o={start:parseFloat(g("start").value)||0, end:parseFloat(g("end").value)||0, text:g("text").value.trim()};
    if(kind==="nar") o.style=g("style").value; else o.speaker=g("speaker").value;
    return o;
  }).filter(o=>o.text);
}
$("#btnSubsLoad").onclick=()=>{
  const code=($("#code").value||curCode()||$("#pvCode").value||"").trim();
  if(!code){ log("품번을 입력하세요","warn"); return; }
  $("#subsStatus").textContent="불러오는 중…";
  fetch(`/subs/${encodeURIComponent(code)}`).then(r=>r.json()).then(j=>{
    if(j.detail){ $("#subsStatus").textContent=""; log("✖ "+j.detail,"warn"); return; }
    subsData=j; subsRender();
    $("#subsStatus").textContent=`내레이션 ${j.narration.length} · 대사 ${j.dialogue.length}`;
    log(`✔ 자막 불러옴 (${code})`,"ok");
  }).catch(e=>{ $("#subsStatus").textContent=""; log("✖ "+e,"warn"); });
};
$("#btnAddNar") && ($("#btnAddNar").onclick=()=>$("#subsNar").appendChild(subsRow("nar",{start:0,end:2,text:"",style:"기본"})));
$("#btnAddDlg") && ($("#btnAddDlg").onclick=()=>$("#subsDlg").appendChild(subsRow("dlg",{start:0,end:2,text:"",speaker:"여"})));
$("#btnSubsSave").onclick=()=>{
  if(!subsData){ return; }
  const code=subsData.code;
  const payload={dialogue:subsCollect($("#subsDlg"),"dlg"), narration:subsCollect($("#subsNar"),"nar")};
  $("#subsStatus").textContent="저장 중…";
  fetch(`/subs/${encodeURIComponent(code)}`,{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
    .then(r=>r.json()).then(j=>{
      if(j.detail){ log("✖ "+j.detail,"warn"); $("#subsStatus").textContent=""; return; }
      $("#subsStatus").textContent=`저장됨 · 내레이션 ${j.narration} · 대사 ${j.dialogue}`;
      log("✔ 자막 저장 — ⑤ 굽기를 다시 하면 반영됩니다(내레이션 바꿨으면 ④ TTS도 재생성)","ok");
    }).catch(e=>{ $("#subsStatus").textContent=""; log("✖ "+e,"warn"); });
};

// ══ 자동/수동 모드 전환 ═══════════════════════════════════════════════════════
// 자동 = 영상만 던지면 완성본까지(감시폴더/드롭 + 큐). 수동 = 구간마킹·단계별·TTS·굽기.
// body[data-mode]를 CSS가 읽어 .manual-only / .auto-only 를 감춘다.
function setMode(m){
  document.body.dataset.mode = m;
  document.querySelectorAll("#modesw .m").forEach(b=>b.classList.toggle("on", b.dataset.mode===m));
  try{ localStorage.setItem("ja_mode", m); }catch(_){}
}
document.querySelectorAll("#modesw .m").forEach(b=> b.onclick=()=>setMode(b.dataset.mode));
setMode((()=>{ try{ return localStorage.getItem("ja_mode")||"auto"; }catch(_){ return "auto"; } })());

// ── 풀오토 설정(자동 모드 전용) ──────────────────────────────────────────────
// 풀오토 파이프라인 옵션 = watcher._fullauto_opts 가 config에서 읽는 값들과 같은 키.
function faSave(){
  const body = {
    target_sec: +$("#faTarget").value || 60,
    fullauto_mode: $("#faMode").value,
    llm: $("#faLlm").value,
    banner_hold: +$("#faHold").value || 4,
    nsfw_guard: $("#faNsfw").checked,
    two_pass: $("#faTwoPass").checked,
  };
  fetch("/config",{method:"POST",headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(()=>{ const s=$("#faStat"); if(s) s.textContent="✓ 저장됨 — 다음 영상부터 적용됩니다."; });
}
if($("#btnFaSave")) $("#btnFaSave").onclick = faSave;
["#faTarget","#faMode","#faLlm","#faHold","#faNsfw","#faTwoPass"].forEach(sel=>{
  const el=$(sel); if(el) el.addEventListener("change", faSave);
});
fetch("/config").then(r=>r.json()).then(c=>{
  if($("#faTarget") && c.target_sec) $("#faTarget").value = c.target_sec;
  if($("#faMode") && c.fullauto_mode) $("#faMode").value = c.fullauto_mode;
  if($("#faLlm") && c.llm) $("#faLlm").value = c.llm;
  if($("#faHold") && c.banner_hold) $("#faHold").value = c.banner_hold;
  if($("#faNsfw")) $("#faNsfw").checked = c.nsfw_guard !== false;
  if($("#faTwoPass")) $("#faTwoPass").checked = c.two_pass !== false;
}).catch(()=>{});

if($("#btnWatchDir")) $("#btnWatchDir").onclick = () => {
  fetch("/browse_dir",{method:"POST"}).then(r=>r.json()).then(r=>{
    if(r.path){ $("#watchDir").value = r.path; saveWatch(); }
  }).catch(()=>{});
};
if($("#btnOpenDone")) $("#btnOpenDone").onclick = () => {
  fetch("/open_dir",{method:"POST",headers:{'Content-Type':'application/json'},
    body:JSON.stringify({sub:"_완성"})}).catch(()=>{});
};

// ── 드래그&드롭 → 풀오토 큐 투입 ────────────────────────────────────────────
// 브라우저 보안상 File 객체에는 전체 경로가 없다 → 파일명을 감시폴더 규약 대신
// 서버 /queue/add 는 경로가 필요하므로, 드롭은 '경로 텍스트'만 지원한다.
// (탐색기에서 파일을 끌면 대부분 text/plain 또는 text/uri-list 로 경로가 함께 온다)
const dz = $("#dropzone");
if(dz){
  const stop = e => { e.preventDefault(); e.stopPropagation(); };
  ["dragenter","dragover"].forEach(ev=> dz.addEventListener(ev, e=>{ stop(e); dz.classList.add("hot"); }));
  ["dragleave","drop"].forEach(ev=> dz.addEventListener(ev, e=>{ stop(e); dz.classList.remove("hot"); }));
  dz.addEventListener("drop", async e => {
    let paths = [];
    const uri = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain") || "";
    uri.split(/[\r\n]+/).forEach(u=>{
      u = u.trim(); if(!u) return;
      if(u.startsWith("file:///")) u = decodeURIComponent(u.slice(8));
      paths.push(u);
    });
    if(!paths.length && e.dataTransfer.files.length){
      log("⚠ 브라우저가 파일 경로를 넘겨주지 않았습니다 — 아래 '폴더 선택'으로 감시 폴더를 지정하거나, "
          + "경로를 복사해 큐의 '＋ 영상 여러 개 추가'를 쓰세요","warn");
      return;
    }
    if(!paths.length) return;
    const j = await fetch("/queue/add",{method:"POST",headers:{'Content-Type':'application/json'},
      body:JSON.stringify({paths, pipeline:{clean:true,transcribe:true,ai:true,subs:true,banner:true,tts:true,burn:true},
                           opts:{fullauto:true}})}).then(r=>r.json()).catch(()=>({added:[]}));
    if(j.added && j.added.length) log(`🔮 풀오토 큐에 ${j.added.length}개 투입 — 완성본까지 자동 진행`,"ok");
  });
  dz.onclick = () => { if($("#btnQAdd")) $("#btnQAdd").click(); };
}
