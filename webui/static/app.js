const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

function setTab(k){
  $$(".tab").forEach(el=>el.classList.remove("active"));
  $$(".tabs button").forEach(el=>el.classList.remove("active"));
  const t = document.getElementById("tab-"+k);
  if(t) t.classList.add("active");
  document.querySelector(`.tabs button[data-tab="${k}"]`)?.classList.add("active");
}
$$(".tabs button").forEach(b=>b.addEventListener("click",()=>setTab(b.dataset.tab)));

let authToken = localStorage.getItem("webui_token")||"";
function authHeaders(){
  const h = {"Content-Type":"application/json"};
  if(authToken) h["Authorization"] = "Bearer "+authToken;
  return h;
}
let statusEtag = "";
let logCursor = 0;
async function api(path, opts={}){
  const headers = {...authHeaders(), ...(opts.headers||{})};
  if(headers["Content-Type"]==="application/json" && opts.body && typeof opts.body==="string"){
    // keep
  }
  // 状态接口附带 ETag，降低空轮询传输
  if(path.startsWith("/api/apps/status") && statusEtag){
    headers["If-None-Match"] = statusEtag;
  }
  const r = await fetch(path, {headers, ...opts});
  if(r.status===304){
    return {notModified:true, status:304};
  }
  if(r.status===401){
    showLogin(true);
    throw new Error("未登录");
  }
  // 更新 ETag
  const et = r.headers.get("ETag");
  if(et && path.startsWith("/api/apps/status")){
    statusEtag = et;
  }
  const t = await r.text();
  try{
    const j = JSON.parse(t);
    if(j && j.cursor !== undefined) logCursor = j.cursor;
    return j;
  }catch{ return {raw:t, status:r.status}; }
}
async function checkAuth(){
  try{
    const j = await api("/api/auth/status");
    if(j.has_account && !j.logged_in){
      showLogin(false);
    } else {
      hideLogin();
      if(j.logged_in) $("#authUser").textContent = j.username;
    }
    return j;
  }catch{
    // 若 401 且未设置账号，后端会 401，但 has_account=false 时应放行；此处忽略
    return {has_account:false};
  }
}
function showLogin(force){
  const o=$("#loginOverlay");
  if(o){ o.style.display="flex"; if(force) $("#loginErr").textContent="会话已过期，请重新登录"; }
}
function hideLogin(){
  const o=$("#loginOverlay");
  if(o) o.style.display="none";
  $("#loginErr").style.display="none";
}
$("#btnLogin")?.addEventListener("click", async ()=>{
  const u=$("#loginUser").value.trim(), p=$("#loginPass").value;
  const err=$("#loginErr");
  err.style.display="none";
  if(!u||!p){ err.textContent="请填写账号密码"; err.style.display="block"; return; }
  const r = await fetch("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:u,password:p})});
  const j = await r.json();
  if(!j.ok){ err.textContent=j.error||"登录失败"; err.style.display="block"; return; }
  authToken=j.token; localStorage.setItem("webui_token", authToken);
  hideLogin(); $("#authUser").textContent=u;
  // 重连 WS 带 token
  if(ws){ try{ws.close();}catch{} }
  connectWS();
  refreshApps(); loadDaily(); loadWool(); loadSeq();
});
$("#btnLogout")?.addEventListener("click", async ()=>{
  await fetch("/api/auth/logout",{method:"POST",headers:authHeaders()});
  localStorage.removeItem("webui_token"); authToken="";
  $("#authUser").textContent=""; showLogin(false);
});

// Apps
let appsCache=[];
async function refreshApps(){
  const j = await api("/api/apps");
  appsCache = j.apps||[];
  const c = $("#appList");
  c.innerHTML = appsCache.map((a,i)=>`
    <div class="item" draggable="true" data-idx="${i}">
      <label style="display:flex;gap:8px;align-items:center;flex:1">
        <input type="checkbox" ${a.enabled?"checked":""} data-app="${a.app_key}"/>
        <b>${a.display_name}</b><small class="muted">${a.app_key}</small>
        ${a.launcher_values?.length?`<select data-choice="${a.app_key}">${a.launcher_values.map(v=>`<option value="${v}" ${v===a.script_choice?"selected":""}>${v}</option>`).join("")}</select>`:""}
      </label>
      <small class="muted">#${a.order}</small>
    </div>
  `).join("");
}
$("#btnRefreshApps").onclick = refreshApps;
$("#btnSavePrefs").onclick = async ()=>{
  const prefs = appsCache.map(a=>{
    const chk = document.querySelector(`input[data-app="${a.app_key}"]`);
    const sel = document.querySelector(`select[data-choice="${a.app_key}"]`);
    return {app_key:a.app_key, enabled: chk?chk.checked:false, script_choice: sel?sel.value:""};
  });
  await api("/api/apps/prefs",{method:"POST",body:JSON.stringify({prefs})});
  alert("已保存");
  refreshApps();
};
$("#btnRun").onclick = async ()=>{
  const force = $("#chkForce").checked;
  const prefs = appsCache.filter(a=> document.querySelector(`input[data-app="${a.app_key}"]`)?.checked)
    .map(a=>({app_key:a.app_key, script_choice: document.querySelector(`select[data-choice="${a.app_key}"]`)?.value||a.script_choice}));
  if(!prefs.length) return alert("请先勾选任务");
  const r = await api("/api/apps/run",{method:"POST",body:JSON.stringify({apps:prefs, forceRerun:force})});
  if(!r.ok) alert(r.error||"启动失败");
};
$("#btnStop").onclick = async ()=>{ await api("/api/apps/stop",{method:"POST"}); };

// Daily
async function loadDaily(){
  const j = await api("/api/daily/progress");
  $("#dailyDate").textContent = j.date_key||"";
  const c = $("#dailyGames");
  c.innerHTML = (j.games||[]).map(g=>`
    <div class="game ${g.is_all_success?"ok":g.has_failed?"fail":""}">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <b>${g.game_name} <small class="muted">${g.game_key}</small></b>
        <span class="tag">${g.is_all_success?"全部完成":g.has_failed?"有失败":"进行中"} ${g.progress_percent}%</span>
      </div>
      <div style="margin-top:8px">
        ${(g.tasks||[]).map(t=>`
          <div class="task">
            <span>${t.task_name} <small class="muted">${t.status}</small></span>
            <span>${t.message||""}${t.verify_image_url?` <a href="${t.verify_image_url}" target="_blank">查看截图</a>`:""}</span>
          </div>
          ${t.verify_image_url?`<div style="margin:6px 0"><img src="${t.verify_image_url}" style="max-width:100%;max-height:170px;object-fit:contain;border:1px solid #dbcdbd;border-radius:8px;background:#f4ece0"/></div>`:""}
        `).join("")}
      </div>
    </div>
  `).join("");
}
$("#btnDailyLoad").onclick = loadDaily;
$("#btnDailyCheck").onclick = async ()=>{
  const sel = appsCache.filter(a=>a.enabled).map(a=>a.app_key); // 复用已启用，也可全量
  await api("/api/daily/check",{method:"POST",body:JSON.stringify({games:[]})});
  loadDaily();
};

// 最近失败记录（失败取证：截图 + 当帧文字）
async function loadFailures(){
  const days = $("#failDays").value || 7;
  const j = await api(`/api/daily/failures?days=${days}&limit=50`);
  const sum = $("#failSummary"), list = $("#failList");
  if (j.error){
    $("#failCount").textContent = "加载失败: " + j.error;
    sum.innerHTML = ""; list.innerHTML = "";
    return;
  }
  const items = j.items || [];
  $("#failCount").textContent = items.length ? `共 ${items.length} 条（按次数看前 5 条）` : "最近无失败记录 ✅";
  sum.innerHTML = (j.summary||[]).slice(0,5).map(s=>`
    <div class="item">
      <div style="display:flex;justify-content:space-between">
        <span><b>${s.flow_key||"?"} / ${s.step||"?"}</b> <small class="muted">${s.has_image?"有截图":"无截图"}</small></span>
        <span class="tag">${s.fail_count} 次</span>
      </div>
      <small class="muted">${String(s.sample_error||"").slice(0,120)}</small>
    </div>`).join("");
  list.innerHTML = items.map(f=>`
    <div class="item">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span><b>${f.flow_key||"?"} / ${f.step||"?"}</b> <small class="muted">${f.game_key||"-"}</small></span>
        <small class="muted">${f.time_text||""}</small>
      </div>
      <div style="color:#b04b44">${String(f.error||"").slice(0,160)}</div>
      ${(f.ocr_texts||[]).length?`<small class="muted">当帧文字: ${(f.ocr_texts||[]).slice(0,6).join(" ｜ ")}</small>`:""}
      ${f.image_url?`<div style="margin:6px 0"><img src="${f.image_url}" style="max-width:100%;max-height:170px;object-fit:contain;border:1px solid #dbcdbd;border-radius:8px;background:#f4ece0"/></div>`:""}
    </div>`).join("");
}
$("#btnFailLoad").onclick = loadFailures;
$("#failDays").onchange = loadFailures;

// Wool
async function loadWool(){
  const j = await api("/api/wool/flows");
  const c = $("#woolList");
  c.innerHTML = (j.flows||[]).map(f=>`
    <div class="item">
      <div><b>${f.name}</b> <small class="muted">${f.key}</small> ${f.exe_ready?"✅":"⚠ 路径未就绪"}</div>
      <label class="chk"><input type="checkbox" ${f.enabled?"checked":""} data-wool="${f.key}"/> 启用</label>
    </div>
  `).join("");
  c.querySelectorAll("input[data-wool]").forEach(ch=>{
    ch.addEventListener("change", async ()=>{
      await api(`/api/wool/flows/${ch.dataset.wool}/toggle`,{method:"POST",body:JSON.stringify({enabled:ch.checked, field:"enabled"})});
    });
  });
}
$("#btnWoolRun").onclick = async ()=>{
  const r = await api("/api/wool/run",{method:"POST"});
  if(!r.ok) alert(r.error||"已有任务运行中");
};

// ClickSeq
async function loadSeq(){
  const j = await api("/api/click-sequences");
  const c = $("#seqList");
  c.innerHTML = (j.sequences||[]).map(s=>`
    <div class="item ${s.protected?"protected":""}">
      <div style="flex:1">
        <div><b>${s.name}</b> ${s.protected?'<span class="tag">🔒 一条龙</span>':''} <small class="muted">${s.steps}步 ${s.created_at||""}</small></div>
        <small class="muted">${s.window||""} ${s.missing?"（缺失可恢复）":""}</small>
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn" onclick="playSeq('${s.name}',false)">回放</button>
        <button class="btn" onclick="playSeq('${s.name}',true)">试跑</button>
        ${s.protected?'<button class="btn" disabled>受保护</button>':`<button class="btn" onclick="delSeq('${s.name}')">删除</button>`}
      </div>
    </div>
  `).join("");
}
window.playSeq = async (name,dry)=>{ await api(`/api/click-sequences/${encodeURIComponent(name)}/play`,{method:"POST",body:JSON.stringify({loop:1,dry})}); alert("已开始 "+name); };
window.delSeq = async (name)=>{
  if(!confirm(`确认删除「${name}」？`)) return;
  const r = await api(`/api/click-sequences/${encodeURIComponent(name)}`,{method:"DELETE"});
  if(!r.ok) alert(r.error); else loadSeq();
};
$("#btnSeqRefresh").onclick = loadSeq;
$("#btnSeqRestore").onclick = async ()=>{
  const r = await api("/api/click-sequences/restore-protected",{method:"POST"});
  alert("已恢复: "+(r.restored||[]).join("、")||"无");
  loadSeq();
};

// Logs WS + poll fallback（增量 + GZip，大幅降低重复传输）
let ws;
let logMode = "all"; // all | login
let wsConnected = false;
function connectWS(){
  try{
    const tok = authToken ? `?token=${encodeURIComponent(authToken)}` : "";
    ws = new WebSocket((location.protocol==="https:"?"wss://":"ws://")+location.host+"/ws/logs"+tok);
    ws.onopen = ()=>{ wsConnected = true; };
    ws.onmessage = e=>{
      if(logMode==="login" && !e.data.includes("[WebUI][登录]") && !e.data.includes("[WebUI][登出]")) return;
      const v=$("#logView"); v.textContent += e.data+"\n"; v.scrollTop=v.scrollHeight;
    };
    ws.onclose = ()=>{ wsConnected = false; setTimeout(connectWS, 3000); };
    ws.onerror = ()=>{ wsConnected = false; };
  }catch{ wsConnected = false; }
}
async function pollLogs(){
  if(document.hidden) return;
  if(wsConnected && logMode==="all") return;
  try{
    let url;
    if(logMode==="login"){
      url = "/api/logs/login?limit=100";
    } else {
      // 增量：已有游标则只拉新日志，否则全量首屏
      url = logCursor ? `/api/logs?limit=100&since=${logCursor}` : "/api/logs?limit=50";
    }
    const j = await api(url);
    if(j.notModified) return;
    const v=$("#logView");
    if(logCursor && j.cursor && j.logs && j.logs.length){
      // 增量追加
      if(j.logs.length) {
        v.textContent += (v.textContent ? "\n" : "") + j.logs.join("\n");
        v.scrollTop=v.scrollHeight;
      }
      return;
    }
    const next = (j.logs||[]).join("\n");
    if(v.textContent !== next){
      v.textContent = next;
      v.scrollTop=v.scrollHeight;
    }
  }catch(e){
  }
}
function setLogMode(m){
  logMode=m;
  $("#btnLogAll").className = m==="all" ? "btn primary" : "btn";
  $("#btnLogLogin").className = m==="login" ? "btn primary" : "btn";
  pollLogs();
}
$("#btnLogAll")?.addEventListener("click", ()=> setLogMode("all"));
$("#btnLogLogin")?.addEventListener("click", ()=> setLogMode("login"));
$("#btnLogRefresh")?.addEventListener("click", pollLogs);
$("#btnLogClear")?.addEventListener("click", ()=>{ $("#logView").textContent=""; });
function fmtElapsed(sec){
  if(!sec || sec<0) return "";
  const m=Math.floor(sec/60), s=sec%60;
  return m? `${m}分${s}秒` : `${s}秒`;
}
function stateIcon(s){
  const m={"pending":"⏳","running":"▶","completed":"✅","failed":"❌","skipped":"⏭️","stopped":"⚠️"};
  return m[s]||"•";
}
let _statusTimer = null;
async function refreshStatus(){
  if(document.hidden) return;
  try{
    const j = await api("/api/apps/status");
    if(j.notModified){
      // 无变化，仅刷新已运行时长
      const elEl=$("#runElapsed");
      if(j.elapsed_sec === undefined){
        // 304 时后端未回 elapsed，需本地自增（约 5s 轮询，避免每秒请求）
      }
      scheduleStatus(j.running ? 3000 : 5000);
      return;
    }
    const b=$("#runBadge");
    b.textContent=j.running?"运行中":"空闲";
    b.className="badge "+(j.running?"running":"");
    const report = (j.last && (j.last.last_report||j.last.report)) || [];
    const warnings = (j.last && (j.last.last_warnings||j.last.warnings)) || [];
    if(report && report.length) $("#lastReport").textContent=[...report, ...warnings.map(w=>"[警告] "+w)].join("\n");

    const card=$("#runStatusCard"), qEl=$("#runQueue"), curEl=$("#runCurrent"), elEl=$("#runElapsed"), srcEl=$("#runSource");
    const hasQueue = j.queue && j.queue.length>0;
    const showCard = j.running || hasQueue;
    card.style.display = showCard ? "block" : "none";
    if(showCard){
      elEl.textContent = j.running && j.elapsed_sec ? `已运行 ${fmtElapsed(j.elapsed_sec)}` : "";
      srcEl.textContent = j.source ? `来源: ${j.source==="gui"?"桌面GUI":j.source==="webui"?"WebUI":j.source}` : "";
      const cur = j.current;
      if(cur && j.running){
        curEl.textContent = `正在执行: ${cur.display_name||cur.app_key} ${cur.script_choice?`(${cur.script_choice})`:""}`;
      } else if(j.running){
        curEl.textContent = "任务执行中…";
      } else {
        curEl.textContent = "队列已就绪/已完成";
      }
      if(hasQueue){
        qEl.innerHTML = j.queue.map(it=>{
          const st = it.state||"pending";
          return `<div class="item" style="padding:6px 8px"><span>${stateIcon(st)} <b>${it.display_name||it.app_key}</b> <small class="muted">${it.app_key}</small> ${it.script_choice?`<small class="muted">(${it.script_choice})</small>`:""}</span><span class="tag">${st}</span></div>`;
        }).join("");
      } else {
        qEl.innerHTML = `<small class="muted">暂无队列</small>`;
      }
    }
    const stopBtn=$("#btnStop");
    if(stopBtn){
      stopBtn.disabled = !j.running;
      stopBtn.title = j.running ? `正在运行${j.current?`: ${j.current.display_name||j.current.app_key}`:""} — 点击停止` : "空闲";
    }
    // 动态间隔：运行中 3s，空闲 5s
    scheduleStatus(j.running ? 3000 : 5000);
  }catch(e){
    scheduleStatus(5000);
  }
}
function scheduleStatus(delay){
  if(_statusTimer) clearTimeout(_statusTimer);
  _statusTimer = setTimeout(refreshStatus, delay);
}
let _logTimer = null;
function scheduleLogs(){
  if(_logTimer) clearTimeout(_logTimer);
  // WS 已连接时 8s 兜底，否则 5s
  const delay = wsConnected ? 8000 : 5000;
  _logTimer = setTimeout(()=>{ pollLogs(); scheduleLogs(); }, delay);
}

// init with auth gate
(async ()=>{
  const st = await checkAuth();
  if(!st.has_account){
    hideLogin();
    $("#loginHint").textContent = "尚未设置账号，WebUI 暂无需登录（请在桌面 GUI 设置账号密码后启用保护）";
  }
  refreshApps(); loadDaily(); loadWool(); loadSeq(); pollLogs(); connectWS(); refreshStatus(); scheduleLogs();
  if(authToken) $("#btnLogout").style.display="inline-block";
  document.addEventListener("visibilitychange", ()=>{
    if(!document.hidden){ refreshStatus(); pollLogs(); }
  });
  setInterval(async()=>{
    if(document.hidden) return;
    try{
      const s = await fetch("/api/auth/status",{headers:authHeaders()}).then(r=>r.json());
      $("#btnLogout").style.display = s.logged_in ? "inline-block":"none";
      if(s.logged_in) $("#authUser").textContent = s.username;
    }catch{}
  }, 10000);
})();
