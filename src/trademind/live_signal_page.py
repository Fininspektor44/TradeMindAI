"""Dependency-free browser page for the TradeMind live signal console."""

from __future__ import annotations


def render_page() -> str:
    return """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradeMind Live Signal Console</title>
<style>
:root{color-scheme:dark;--bg:#07131d;--panel:#0d2231;--line:#1e4358;--text:#e8f5fc;
--muted:#8fb4c7;--green:#33d6a6;--red:#ff6b7a;--amber:#ffc857;--blue:#62b5ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px Arial,sans-serif}
header{display:flex;justify-content:space-between;gap:20px;align-items:center;padding:20px 24px;
border-bottom:1px solid var(--line);position:sticky;top:0;background:rgba(7,19,29,.96);z-index:3}
h1{font-size:24px;margin:0}.sub{color:var(--muted);margin-top:5px}.badges{display:flex;gap:8px;flex-wrap:wrap}
.badge{padding:6px 9px;border:1px solid var(--line);border-radius:999px;color:var(--muted)}
.badge.safe{color:var(--green);border-color:#23684f}.badge.warn{color:var(--amber)}
main{padding:18px 24px 40px}.cards{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px}.card b{font-size:24px;display:block}
.card span{color:var(--muted)}.filters{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:9px;margin:14px 0}
select,input,button{width:100%;background:#091b28;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:9px}
button{cursor:pointer}button:hover{border-color:var(--blue)}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;min-width:1180px;background:#091923}th,td{padding:10px;border-bottom:1px solid #153448;text-align:left;white-space:nowrap}
th{color:var(--muted);font-size:12px;position:sticky;top:77px;background:#0b1d2a;z-index:2}tbody tr{cursor:pointer}
tbody tr:hover{background:#102b3d}.buy{color:var(--green);font-weight:bold}.sell{color:var(--red);font-weight:bold}
.status{font-weight:bold}.status.WIN{color:var(--green)}.status.LOSS{color:var(--red)}.status.NEW{color:var(--blue)}
.status.ACTIVE{color:var(--amber)}.stale{color:var(--amber)}.empty{padding:35px;text-align:center;color:var(--muted)}
.drawer{position:fixed;right:-520px;top:0;width:min(520px,100%);height:100%;background:#0a1d2a;border-left:1px solid var(--line);
z-index:5;transition:right .2s;padding:20px;overflow:auto}.drawer.open{right:0}.drawer h2{margin:5px 0 16px}.drawer-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.field{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px}.field span{display:block;color:var(--muted);font-size:11px;margin-bottom:4px}
.wide{grid-column:1/-1;white-space:pre-wrap}.close{width:auto;float:right;padding:7px 12px}.error{color:var(--red);margin:10px 0}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.filters{grid-template-columns:repeat(2,1fr)}header{align-items:flex-start}.badges{justify-content:flex-end}}
</style>
</head>
<body>
<header><div><h1>TradeMind Live Signal Console</h1><div class="sub">MT5 + Bybit, сигналы и исходы в одной ленте</div></div>
<div class="badges"><span class="badge safe">READ ONLY</span><span class="badge safe">ORDERS OFF</span><span id="connection" class="badge warn">CONNECTING</span><span id="updated" class="badge">—</span></div></header>
<main>
<section class="cards">
<div class="card"><b id="total">0</b><span>всего сигналов</span></div>
<div class="card"><b id="active">0</b><span>новые и активные</span></div>
<div class="card"><b id="wins">0</b><span>WIN</span></div>
<div class="card"><b id="losses">0</b><span>LOSS</span></div>
<div class="card"><b id="stale">0</b><span>устаревшие данные</span></div>
</section>
<section class="filters">
<select id="source"><option value="">Все источники</option><option>MT5</option><option>BYBIT</option></select>
<select id="symbol"><option value="">Все инструменты</option></select>
<select id="action"><option value="">BUY и SELL</option><option>BUY</option><option>SELL</option></select>
<select id="status"><option value="">Все статусы</option><option>NEW</option><option>ACTIVE</option><option>WIN</option><option>LOSS</option><option>TIMEOUT</option><option>CANCELLED</option></select>
<input id="score" type="number" min="0" max="100" value="0" aria-label="Минимальный score" placeholder="Min score">
<button id="refresh">Обновить сейчас</button>
</section>
<div id="error" class="error"></div>
<div class="table-wrap"><table><thead><tr><th>Время UTC</th><th>Источник</th><th>Инструмент</th><th>Направление</th><th>Сценарий</th><th>Entry</th><th>Stop</th><th>Target</th><th>RR</th><th>Score</th><th>Статус</th><th>Данные</th></tr></thead><tbody id="feed"></tbody></table><div id="empty" class="empty" hidden>Сигналов по выбранным фильтрам нет.</div></div>
</main>
<aside id="drawer" class="drawer"><button id="close" class="close">Закрыть</button><h2 id="detail-title">Сигнал</h2><div id="detail" class="drawer-grid"></div></aside>
<script>
const $=id=>document.getElementById(id);let timer=null;
function text(v){return v===null||v===undefined||v===''?'—':String(v)}
function number(v,d=2){const n=Number(v);return Number.isFinite(n)?n.toFixed(d):'—'}
function field(label,value,wide=false){const box=document.createElement('div');box.className='field'+(wide?' wide':'');const a=document.createElement('span');a.textContent=label;const b=document.createElement('div');b.textContent=text(value);box.append(a,b);return box}
function query(){const p=new URLSearchParams({limit:'500'});for(const id of ['source','symbol','action','status']){if($(id).value)p.set(id,$(id).value)}p.set('min_score',$('score').value||'0');return p.toString()}
async function getJson(path){const r=await fetch(path,{cache:'no-store'});const data=await r.json();if(!r.ok)throw new Error(data.error||`HTTP ${r.status}`);return data}
function fillSymbols(signals){const selected=$('symbol').value;const values=[...new Set(signals.map(x=>x.symbol))].sort();$('symbol').replaceChildren(new Option('Все инструменты',''),...values.map(x=>new Option(x,x)));$('symbol').value=values.includes(selected)?selected:''}
function renderRows(signals){const body=$('feed');body.replaceChildren();$('empty').hidden=signals.length>0;for(const s of signals){const tr=document.createElement('tr');const values=[new Date(s.signal_time).toISOString().replace('T',' ').replace('.000Z','Z'),s.source,s.symbol,s.action,s.scenario,number(s.entry_price),number(s.stop_price),number(s.target_price),number(s.rr),s.score,s.status,s.freshness];values.forEach((value,index)=>{const td=document.createElement('td');td.textContent=text(value);if(index===3)td.className=s.action==='BUY'?'buy':'sell';if(index===10)td.className=`status ${s.status}`;if(index===11&&s.stale)td.className='stale';tr.append(td)});tr.addEventListener('click',()=>openDetail(s.event_id));body.append(tr)}}
async function openDetail(id){try{const s=await getJson('/api/signals/'+encodeURIComponent(id));$('detail-title').textContent=`${s.symbol} ${s.action} · ${s.status}`;const d=$('detail');d.replaceChildren(field('Время',s.signal_time),field('Источник',`${s.source} / ${s.pipeline}`),field('Entry',number(s.entry_price)),field('Stop',number(s.stop_price)),field('Target',number(s.target_price)),field('RR',number(s.rr)),field('Score',s.score),field('Результат',number(s.result)),field('MFE',number(s.mfe)),field('MAE',number(s.mae)),field('Сценарий',s.scenario,true),field('Компоненты',(s.components||[]).join(' · '),true),field('Причины',s.reasons,true),field('ID',s.event_id,true));$('drawer').classList.add('open')}catch(e){$('error').textContent=e.message}}
async function refresh(){try{const [summary,payload]=await Promise.all([getJson('/api/summary'),getJson('/api/signals?'+query())]);$('connection').textContent='ONLINE';$('connection').className='badge safe';$('updated').textContent=new Date(payload.loaded_at).toLocaleTimeString();$('total').textContent=summary.total;$('active').textContent=(summary.by_status.NEW||0)+(summary.by_status.ACTIVE||0);$('wins').textContent=summary.by_status.WIN||0;$('losses').textContent=summary.by_status.LOSS||0;$('stale').textContent=summary.stale;fillSymbols(payload.signals);renderRows(payload.signals);$('error').textContent=payload.errors.length?payload.errors.join(' | '):''}catch(e){$('connection').textContent='OFFLINE';$('connection').className='badge warn';$('error').textContent=e.message}}
for(const id of ['source','symbol','action','status','score'])$(id).addEventListener('change',refresh);$('refresh').addEventListener('click',refresh);$('close').addEventListener('click',()=>$('drawer').classList.remove('open'));refresh();timer=setInterval(refresh,5000);
</script>
</body></html>"""
