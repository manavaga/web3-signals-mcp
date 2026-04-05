# api/dashboard.py
"""Dashboard — inline HTML/JS single-page app."""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Web3 Signals v2</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0a0b0f;--surface:#12131a;--border:#2a2b35;--text:#e0e0e0;--dim:#6b7280;
--green:#22c55e;--red:#ef4444;--yellow:#eab308;--cyan:#06b6d4;--purple:#a855f7}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.mono{font-family:'SF Mono',Consolas,'Courier New',monospace}
header{display:flex;align-items:center;justify-content:space-between;padding:1rem 1.5rem;border-bottom:1px solid var(--border);background:var(--surface)}
header .title{display:flex;align-items:center;gap:.6rem;font-size:1.25rem;font-weight:700}
header .dot{width:10px;height:10px;border-radius:50%;background:var(--green);display:inline-block}
header .dot.offline{background:var(--red)}
header .meta{display:flex;align-items:center;gap:1rem;font-size:.8rem;color:var(--dim)}
header .meta button{background:var(--surface);border:1px solid var(--border);color:var(--text);padding:.35rem .75rem;border-radius:6px;cursor:pointer;font-size:.8rem}
header .meta button:hover{border-color:var(--cyan)}
nav{display:flex;gap:0;border-bottom:1px solid var(--border);background:var(--surface)}
nav button{background:none;border:none;border-bottom:2px solid transparent;color:var(--dim);padding:.75rem 1.25rem;cursor:pointer;font-size:.85rem;font-weight:500}
nav button.active{color:var(--cyan);border-bottom-color:var(--cyan)}
nav button:hover{color:var(--text)}
main{padding:1.5rem;max-width:1400px;margin:0 auto}
.hidden{display:none!important}
.summary-bar{display:flex;flex-wrap:wrap;gap:.75rem;margin-bottom:1.25rem}
.summary-item{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:.6rem 1rem;font-size:.8rem;display:flex;flex-direction:column;gap:.15rem}
.summary-item .label{color:var(--dim);font-size:.7rem;text-transform:uppercase;letter-spacing:.5px}
.summary-item .value{font-weight:700;font-size:1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:1rem}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:1rem;position:relative}
.card-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:.6rem}
.card-header .asset{font-size:1.1rem;font-weight:700}
.card-header .score{font-size:1.6rem;font-weight:800}
.badge{font-size:.65rem;padding:.2rem .5rem;border-radius:4px;font-weight:600;text-transform:uppercase;letter-spacing:.3px}
.badge-buy{background:rgba(34,197,94,.15);color:var(--green)}
.badge-sell{background:rgba(239,68,68,.15);color:var(--red)}
.badge-neutral{background:rgba(234,179,8,.15);color:var(--yellow)}
.badge-abstain{background:rgba(234,179,8,.1);color:var(--yellow);border:1px solid rgba(234,179,8,.25)}
.dim-bars{margin:.5rem 0}
.dim-row{display:flex;align-items:center;gap:.5rem;margin-bottom:.35rem;font-size:.75rem}
.dim-row .dim-name{width:75px;color:var(--dim);text-transform:capitalize}
.dim-row .bar-bg{flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden}
.dim-row .bar-fill{height:100%;border-radius:3px;transition:width .3s}
.dim-row .dim-val{width:35px;text-align:right;font-weight:600}
.dim-row .dim-wt{width:30px;text-align:right;color:var(--dim);font-size:.65rem}
.detail-text{font-size:.65rem;color:var(--dim);margin-top:.15rem;line-height:1.3;word-break:break-all}
.targets{margin-top:.6rem;padding:.5rem;background:rgba(6,182,212,.05);border:1px solid rgba(6,182,212,.15);border-radius:6px;font-size:.75rem}
.targets .tgt-title{font-weight:600;color:var(--cyan);margin-bottom:.3rem;font-size:.7rem}
.targets .tgt-grid{display:grid;grid-template-columns:1fr 1fr;gap:.2rem .75rem}
.targets .tgt-item{display:flex;justify-content:space-between}
.targets .tgt-label{color:var(--dim)}
.targets .tgt-val{font-weight:600}
.momentum{font-size:.7rem;color:var(--dim);margin-top:.4rem}
.section{margin-bottom:1.5rem}
.section h3{font-size:1rem;font-weight:600;margin-bottom:.75rem;color:var(--text)}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:.75rem}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:1rem;text-align:center}
.stat-card .stat-val{font-size:1.5rem;font-weight:800;margin:.3rem 0}
.stat-card .stat-label{font-size:.75rem;color:var(--dim);text-transform:uppercase}
.table{width:100%;border-collapse:collapse;font-size:.8rem}
.table th,.table td{padding:.5rem .75rem;text-align:left;border-bottom:1px solid var(--border)}
.table th{color:var(--dim);font-weight:500;text-transform:uppercase;font-size:.7rem}
.bar-chart{display:flex;flex-direction:column;gap:.3rem}
.bar-chart-row{display:flex;align-items:center;gap:.5rem;font-size:.8rem}
.bar-chart-row .bc-label{width:80px;text-align:right;color:var(--dim);font-size:.7rem;flex-shrink:0}
.bar-chart-row .bc-bar{flex:1;height:18px;background:var(--border);border-radius:3px;overflow:hidden}
.bar-chart-row .bc-fill{height:100%;background:var(--cyan);border-radius:3px;display:flex;align-items:center;padding-left:4px;font-size:.65rem;font-weight:600;color:var(--bg);min-width:fit-content}
.bar-chart-row .bc-val{width:50px;font-weight:600;font-size:.75rem}
.loading-msg,.error-msg,.empty-msg{text-align:center;padding:2rem;color:var(--dim);font-size:.9rem}
.error-msg{color:var(--red)}
.ic-bar{display:flex;align-items:center;gap:.5rem;margin-bottom:.4rem;font-size:.8rem}
.ic-bar .ic-name{width:90px;color:var(--dim)}
.ic-bar .ic-track{flex:1;height:12px;background:var(--border);border-radius:6px;overflow:hidden;position:relative}
.ic-bar .ic-fill{height:100%;border-radius:6px;position:absolute;top:0}
.ic-bar .ic-val{width:50px;text-align:right;font-weight:600}
</style>
</head>
<body>
<header>
  <div class="title"><span class="dot" id="statusDot"></span> Web3 Signals v2</div>
  <div class="meta">
    <span id="lastUpdated">--</span>
    <button onclick="refreshAll()">Refresh</button>
  </div>
</header>
<nav id="tabs">
  <button class="active" data-tab="signals">Signals</button>
  <button data-tab="performance">Performance</button>
  <button data-tab="analytics">Analytics</button>
  <button data-tab="agents">AI Agents</button>
  <button data-tab="health">Signal Health</button>
</nav>
<main>
  <div id="tab-signals"></div>
  <div id="tab-performance" class="hidden"></div>
  <div id="tab-analytics" class="hidden"></div>
  <div id="tab-agents" class="hidden"></div>
  <div id="tab-health" class="hidden"></div>
</main>
<script>
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
const fmt = (n,d=2) => n==null?'--':Number(n).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtInt = n => n==null?'--':Number(n).toLocaleString();
const pct = n => n==null?'--':(n*100).toFixed(1)+'%';
const scoreColor = (s,dir) => dir==='bullish'?'var(--green)':dir==='bearish'?'var(--red)':'var(--yellow)';
const barColor = s => s>=60?'var(--green)':s>=40?'var(--yellow)':'var(--red)';
const labelBadge = l => {if(!l)return'';const c=l.includes('BUY')?'badge-buy':l.includes('SELL')?'badge-sell':'badge-neutral';return`<span class="badge ${c}">${l}</span>`};

let activeTab='signals';
document.querySelectorAll('#tabs button').forEach(b=>{
  b.addEventListener('click',()=>{
    document.querySelectorAll('#tabs button').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    activeTab=b.dataset.tab;
    ['signals','performance','analytics','agents','health'].forEach(t=>{
      document.getElementById('tab-'+t).classList.toggle('hidden',t!==activeTab);
    });
    loadTab(activeTab);
  });
});

async function fetchJSON(url){
  const r=await fetch(url);
  if(!r.ok) throw new Error(r.status+' '+r.statusText);
  return r.json();
}

function loadTab(tab){
  const loaders={signals:loadSignals,performance:loadPerformance,analytics:loadAnalytics,agents:loadAgents,health:loadHealth};
  if(loaders[tab])loaders[tab]();
}

async function refreshAll(){
  try{const d=await fetchJSON('/health');$('#statusDot').className=d.status==='healthy'?'dot':'dot offline'}catch{$('#statusDot').className='dot offline'}
  $('#lastUpdated').textContent='Updated: '+new Date().toLocaleTimeString();
  loadTab(activeTab);
}

async function loadSignals(){
  const el=$('#tab-signals');
  el.innerHTML='<div class="loading-msg">Loading signals...</div>';
  try{
    const d=await fetchJSON('/api/signal');
    const sigs=d.signals||{};
    const entries=Object.entries(sigs);
    const bullish=entries.filter(([,s])=>s.direction==='bullish'&&!s.abstained);
    const bearish=entries.filter(([,s])=>s.direction==='bearish'&&!s.abstained);
    const abstained=entries.filter(([,s])=>s.abstained);
    const topBuy=bullish.length?bullish.sort((a,b)=>b[1].composite-a[1].composite)[0]:null;
    const topSell=bearish.length?bearish.sort((a,b)=>a[1].composite-b[1].composite)[0]:null;
    const fgVal=entries.length?entries[0][1]?.regime?.fg_value:null;
    let html=`<div class="summary-bar">
      <div class="summary-item"><span class="label">Regime</span><span class="value">${d.regime||'--'}</span></div>
      <div class="summary-item"><span class="label">Fear & Greed</span><span class="value mono" style="color:${fgVal!=null?(fgVal<25?'var(--red)':fgVal<50?'var(--yellow)':'var(--green)'):'var(--dim)'}">${fgVal!=null?fgVal:'--'}</span></div>
      <div class="summary-item"><span class="label">Assets</span><span class="value">${entries.length}</span></div>
      <div class="summary-item"><span class="label">Directional</span><span class="value">${bullish.length+bearish.length}</span></div>
      <div class="summary-item"><span class="label">Abstained</span><span class="value" style="color:var(--yellow)">${abstained.length}</span></div>
      <div class="summary-item"><span class="label">Top Buy</span><span class="value" style="color:var(--green)">${topBuy?topBuy[0]+' '+fmt(topBuy[1].composite):'--'}</span></div>
      <div class="summary-item"><span class="label">Top Sell</span><span class="value" style="color:var(--red)">${topSell?topSell[0]+' '+fmt(topSell[1].composite):'--'}</span></div>
    </div><div class="grid">`;
    const sorted=entries.sort((a,b)=>Math.abs(b[1].composite-50)-Math.abs(a[1].composite-50));
    for(const [asset,s] of sorted){
      const dims=s.dimensions||{};
      const wts=s.weights_used||{};
      let dimHtml='';
      for(const [dn,dv] of Object.entries(dims)){
        const sc=dv.score!=null?dv.score:50;
        const wt=wts[dn];
        dimHtml+=`<div class="dim-row"><span class="dim-name">${dn}</span>
          <div class="bar-bg"><div class="bar-fill" style="width:${sc}%;background:${barColor(sc)}"></div></div>
          <span class="dim-val mono">${fmt(sc,1)}</span>
          <span class="dim-wt">${wt!=null?fmt(wt):''}</span></div>
          <div class="detail-text">${dv.detail||''}</div>`;
      }
      let tgtHtml='';
      if(s.targets){
        const t=s.targets;
        tgtHtml=`<div class="targets"><div class="tgt-title">Targets (${t.timeframe_hours||48}h)</div><div class="tgt-grid">
          <div class="tgt-item"><span class="tgt-label">Entry</span><span class="tgt-val mono">$${fmtInt(t.entry_price)}</span></div>
          <div class="tgt-item"><span class="tgt-label">Target</span><span class="tgt-val mono" style="color:var(--green)">$${fmtInt(t.target_price)}</span></div>
          <div class="tgt-item"><span class="tgt-label">Stop</span><span class="tgt-val mono" style="color:var(--red)">$${fmtInt(t.stop_loss)}</span></div>
          <div class="tgt-item"><span class="tgt-label">R:R</span><span class="tgt-val mono">${fmt(t.risk_reward_ratio)}</span></div>
          <div class="tgt-item"><span class="tgt-label">Move</span><span class="tgt-val mono">${t.predicted_move_pct!=null?fmt(t.predicted_move_pct,1)+'%':'--'}</span></div>
          <div class="tgt-item"><span class="tgt-label">Confidence</span><span class="tgt-val">${t.confidence||'--'}</span></div>
        </div></div>`;
      }
      const dir=s.direction||'neutral';
      const arrow=dir==='bullish'?'&#9650;':dir==='bearish'?'&#9660;':'&#9644;';
      html+=`<div class="card">
        <div class="card-header">
          <div><span class="asset">${asset}</span> <span style="color:${scoreColor(s.composite,dir)}">${arrow}</span>
          ${s.abstained?'<span class="badge badge-abstain">ABSTAIN</span>':''}</div>
          <div style="text-align:right"><div class="score mono" style="color:${scoreColor(s.composite,dir)}">${fmt(s.composite)}</div>
          ${labelBadge(s.label)}</div>
        </div>
        <div class="dim-bars">${dimHtml}</div>
        <div class="momentum">Momentum: <strong>${s.momentum||'--'}</strong></div>
        ${tgtHtml}
      </div>`;
    }
    html+='</div>';
    el.innerHTML=html;
  }catch(e){el.innerHTML=`<div class="error-msg">Error loading signals: ${e.message}</div>`}
}

async function loadPerformance(){
  const el=$('#tab-performance');
  el.innerHTML='<div class="loading-msg">Loading performance...</div>';
  try{
    const d=await fetchJSON('/performance');
    if(!d||(!d.overall&&!d.total_evaluated)){el.innerHTML='<div class="empty-msg">No performance data yet.</div>';return}
    const ov=d.overall||d;
    let html=`<div class="section"><h3>Overall Accuracy</h3><div class="stat-grid">
      <div class="stat-card"><div class="stat-label">Total Evaluated</div><div class="stat-val mono">${fmtInt(d.total_evaluated||ov.total_evaluated||0)}</div></div>
      <div class="stat-card"><div class="stat-label">Gradient Accuracy</div><div class="stat-val mono" style="color:var(--cyan)">${d.gradient_accuracy!=null?fmt(d.gradient_accuracy,1)+'%':ov.gradient_accuracy!=null?fmt(ov.gradient_accuracy,1)+'%':'--'}</div></div>`;
    if(d.by_window){
      for(const [w,v] of Object.entries(d.by_window)){
        const acc=v.accuracy!=null?fmt(v.accuracy,1)+'%':v.gradient!=null?fmt(v.gradient,1)+'%':'--';
        html+=`<div class="stat-card"><div class="stat-label">${w} Window</div><div class="stat-val mono">${acc}</div><div style="font-size:.7rem;color:var(--dim)">${fmtInt(v.count||v.total||0)} signals</div></div>`;
      }
    }
    html+=`</div></div>`;
    if(d.by_direction){
      html+=`<div class="section"><h3>By Direction</h3><div class="stat-grid">`;
      for(const [dir,v] of Object.entries(d.by_direction)){
        const acc=v.accuracy!=null?fmt(v.accuracy,1)+'%':'--';
        const clr=dir==='bullish'?'var(--green)':dir==='bearish'?'var(--red)':'var(--yellow)';
        html+=`<div class="stat-card"><div class="stat-label">${dir}</div><div class="stat-val mono" style="color:${clr}">${acc}</div></div>`;
      }
      html+=`</div></div>`;
    }
    if(d.by_asset){
      html+=`<div class="section"><h3>By Asset</h3><table class="table"><tr><th>Asset</th><th>Accuracy</th><th>Count</th></tr>`;
      for(const [a,v] of Object.entries(d.by_asset)){
        const acc=v.accuracy!=null?fmt(v.accuracy,1)+'%':'--';
        html+=`<tr><td>${a}</td><td class="mono">${acc}</td><td class="mono">${fmtInt(v.count||0)}</td></tr>`;
      }
      html+=`</table></div>`;
    }
    el.innerHTML=html;
  }catch(e){el.innerHTML=`<div class="error-msg">Error: ${e.message}</div>`}
}

async function loadAnalytics(){
  const el=$('#tab-analytics');
  el.innerHTML='<div class="loading-msg">Loading analytics...</div>';
  try{
    const [an,x402]=await Promise.all([fetchJSON('/analytics'),fetchJSON('/analytics/x402').catch(()=>null)]);
    let html=`<div class="section"><h3>Request Summary</h3><div class="stat-grid">
      <div class="stat-card"><div class="stat-label">Total Requests</div><div class="stat-val mono">${fmtInt(an.total_requests||0)}</div></div>
      <div class="stat-card"><div class="stat-label">Unique Clients</div><div class="stat-val mono">${fmtInt(an.unique_clients||0)}</div></div>
      <div class="stat-card"><div class="stat-label">Avg Response</div><div class="stat-val mono">${an.avg_response_ms!=null?fmt(an.avg_response_ms,0)+'ms':'--'}</div></div>
    </div></div>`;
    if(an.requests_per_day&&Object.keys(an.requests_per_day).length){
      const rpd=an.requests_per_day;
      const maxV=Math.max(...Object.values(rpd),1);
      html+=`<div class="section"><h3>Requests Per Day</h3><div class="bar-chart">`;
      for(const [day,cnt] of Object.entries(rpd).slice(-14)){
        const w=Math.max((cnt/maxV)*100,2);
        const lbl=day.slice(5);
        html+=`<div class="bar-chart-row"><span class="bc-label">${lbl}</span><div class="bc-bar"><div class="bc-fill" style="width:${w}%">${cnt}</div></div><span class="bc-val">${fmtInt(cnt)}</span></div>`;
      }
      html+=`</div></div>`;
    }
    if(an.by_endpoint){
      html+=`<div class="section"><h3>By Endpoint</h3><table class="table"><tr><th>Endpoint</th><th>Count</th></tr>`;
      for(const [ep,cnt] of Object.entries(an.by_endpoint).sort((a,b)=>b[1]-a[1])){
        html+=`<tr><td>${ep}</td><td class="mono">${fmtInt(cnt)}</td></tr>`;
      }
      html+=`</table></div>`;
    }
    if(an.by_source){
      html+=`<div class="section"><h3>By Source</h3><div class="stat-grid">`;
      for(const [src,cnt] of Object.entries(an.by_source)){
        html+=`<div class="stat-card"><div class="stat-label">${src}</div><div class="stat-val mono">${fmtInt(cnt)}</div></div>`;
      }
      html+=`</div></div>`;
    }
    if(an.client_types||an.by_client_type){
      const ct=an.client_types||an.by_client_type||{};
      html+=`<div class="section"><h3>Client Types</h3><table class="table"><tr><th>Type</th><th>Count</th></tr>`;
      for(const [t,c] of Object.entries(ct).sort((a,b)=>b[1]-a[1])){
        html+=`<tr><td>${t}</td><td class="mono">${fmtInt(c)}</td></tr>`;
      }
      html+=`</table></div>`;
    }
    if(x402){
      html+=`<div class="section"><h3>x402 Payments</h3><div class="stat-grid">
        <div class="stat-card"><div class="stat-label">Paid Calls</div><div class="stat-val mono" style="color:var(--purple)">${fmtInt(x402.paid_calls||0)}</div></div>
        <div class="stat-card"><div class="stat-label">Revenue (USDC)</div><div class="stat-val mono" style="color:var(--green)">${fmt(x402.revenue||0,4)}</div></div>
        <div class="stat-card"><div class="stat-label">Conversion</div><div class="stat-val mono">${x402.conversion_rate!=null?pct(x402.conversion_rate):'--'}</div></div>
      </div></div>`;
    }
    el.innerHTML=html;
  }catch(e){el.innerHTML=`<div class="error-msg">Error: ${e.message}</div>`}
}

async function loadAgents(){
  const el=$('#tab-agents');
  el.innerHTML='<div class="loading-msg">Loading agent data...</div>';
  try{
    const [ag,er]=await Promise.all([fetchJSON('/analytics/agents').catch(()=>null),fetchJSON('/analytics/errors').catch(()=>null)]);
    let html='';
    if(ag){
      const total=Object.values(ag.by_agent||ag).reduce((s,v)=>s+(typeof v==='number'?v:0),0);
      const agents=ag.by_agent||ag;
      const aiShare=ag.ai_share!=null?pct(ag.ai_share):(total>0?'--':'0%');
      html+=`<div class="section"><h3>AI Agent Calls</h3><div class="stat-grid">
        <div class="stat-card"><div class="stat-label">Total AI Calls</div><div class="stat-val mono" style="color:var(--purple)">${fmtInt(total)}</div></div>
        <div class="stat-card"><div class="stat-label">AI Share</div><div class="stat-val mono">${aiShare}</div></div>
      </div>`;
      if(Object.keys(agents).length){
        const maxA=Math.max(...Object.values(agents).filter(v=>typeof v==='number'),1);
        html+=`<div style="margin-top:1rem" class="bar-chart">`;
        for(const [name,cnt] of Object.entries(agents).filter(([,v])=>typeof v==='number').sort((a,b)=>b[1]-a[1])){
          const w=Math.max((cnt/maxA)*100,3);
          html+=`<div class="bar-chart-row"><span class="bc-label">${name}</span><div class="bc-bar"><div class="bc-fill" style="width:${w}%;background:var(--purple)">${cnt}</div></div><span class="bc-val">${fmtInt(cnt)}</span></div>`;
        }
        html+=`</div>`;
      }
      html+=`</div>`;
    }else{html+=`<div class="empty-msg">No agent data available.</div>`}
    if(er){
      html+=`<div class="section"><h3>Error Summary</h3>`;
      if(er.total_errors!=null){
        html+=`<div class="stat-grid"><div class="stat-card"><div class="stat-label">Total Errors</div><div class="stat-val mono" style="color:var(--red)">${fmtInt(er.total_errors)}</div></div>
        <div class="stat-card"><div class="stat-label">Error Rate</div><div class="stat-val mono">${er.error_rate!=null?pct(er.error_rate):'--'}</div></div></div>`;
      }
      const errs=er.by_type||er.errors||er;
      if(typeof errs==='object'&&Object.keys(errs).length>2){
        html+=`<table class="table" style="margin-top:.75rem"><tr><th>Error</th><th>Count</th></tr>`;
        for(const [t,c] of Object.entries(errs).filter(([k])=>!['total_errors','error_rate'].includes(k))){
          if(typeof c==='number') html+=`<tr><td>${t}</td><td class="mono">${fmtInt(c)}</td></tr>`;
        }
        html+=`</table>`;
      }
      html+=`</div>`;
    }
    el.innerHTML=html||'<div class="empty-msg">No data available.</div>';
  }catch(e){el.innerHTML=`<div class="error-msg">Error: ${e.message}</div>`}
}

async function loadHealth(){
  const el=$('#tab-health');
  el.innerHTML='<div class="loading-msg">Loading health data...</div>';
  try{
    const [ic,h]=await Promise.all([fetchJSON('/analytics/ic').catch(()=>null),fetchJSON('/health').catch(()=>null)]);
    let html='';
    if(ic){
      html+=`<div class="section"><h3>Information Coefficient (IC) per Dimension</h3>`;
      const dims=ic.ic_per_dimension||ic.dimensions||ic;
      if(typeof dims==='object'){
        for(const [name,val] of Object.entries(dims)){
          if(typeof val!=='number')continue;
          const absVal=Math.abs(val);
          const clr=val>0?'var(--green)':'var(--red)';
          const w=Math.min(absVal*500,100);
          html+=`<div class="ic-bar"><span class="ic-name">${name}</span>
            <div class="ic-track"><div class="ic-fill" style="width:${w}%;background:${clr};left:${val<0?100-w+'%':'0'}"></div></div>
            <span class="ic-val mono" style="color:${clr}">${val>=0?'+':''}${fmt(val,3)}</span></div>`;
        }
      }
      html+=`</div>`;
      const pw=ic.proposed_weights||ic.shadow_weights;
      if(pw&&typeof pw==='object'){
        html+=`<div class="section"><h3>Proposed Weights (Shadow Optimizer)</h3><table class="table"><tr><th>Dimension</th><th>Weight</th></tr>`;
        for(const [d,w] of Object.entries(pw)){
          html+=`<tr><td>${d}</td><td class="mono">${fmt(w,3)}</td></tr>`;
        }
        html+=`</table></div>`;
      }
      if(ic.observation_count!=null){
        html+=`<div class="stat-grid"><div class="stat-card"><div class="stat-label">Observations</div><div class="stat-val mono">${fmtInt(ic.observation_count)}</div></div></div>`;
      }
    }else{html+=`<div class="empty-msg">No IC data available.</div>`}
    if(h){
      const upH=Math.floor((h.uptime_seconds||0)/3600);
      const upM=Math.floor(((h.uptime_seconds||0)%3600)/60);
      html+=`<div class="section" style="margin-top:1.5rem"><h3>System Health</h3><div class="stat-grid">
        <div class="stat-card"><div class="stat-label">Status</div><div class="stat-val" style="color:${h.status==='healthy'?'var(--green)':'var(--red)'}">${h.status||'--'}</div></div>
        <div class="stat-card"><div class="stat-label">Uptime</div><div class="stat-val mono">${upH}h ${upM}m</div></div>
        <div class="stat-card"><div class="stat-label">Enabled Assets</div><div class="stat-val mono">${h.enabled_assets||'--'}</div></div>
      </div></div>`;
    }
    el.innerHTML=html||'<div class="empty-msg">No health data available.</div>';
  }catch(e){el.innerHTML=`<div class="error-msg">Error: ${e.message}</div>`}
}

refreshAll();
setInterval(refreshAll,300000);
</script>
</body>
</html>"""
