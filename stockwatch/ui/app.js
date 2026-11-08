
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = (v) => Number(v || 0).toLocaleString();
const state = {
  user: null,
  csrf: '',
  workspace: null,
  campaigns: [],
  summary: null,
  schedules: [],
  users: [],
  audit: [],
  events: [],
  google: null,
  page: 'overview',
  selected: 0,
  statusFilter: 'all',
  search: '',
  historyRows: [],
  analytics: null,
  members: [],
  driveHistory: [],
  settingsTab: 'general',
};
let toastTimer;

function showToast(msg, bad=false){
  const t = $('toast');
  t.textContent = String(msg);
  t.style.background = bad ? '#b62b43' : '#101b47';
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.hidden = true, 4500);
}

async function api(url, options={}){
  const opts = {credentials: 'same-origin', ...options};
  opts.headers = {
    'Accept': 'application/json',
    ...(opts.body ? {'Content-Type': 'application/json'} : {}),
    ...((opts.method && opts.method !== 'GET') ? {'X-CSRF-Token': state.csrf} : {}),
    ...(opts.headers || {})
  };
  const res = await fetch(url, opts);
  let data;
  try { data = await res.json(); }
  catch { data = {error: await res.text().catch(() => 'Request failed')}; }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
const post = (url, obj={}) => api(url, {method:'POST', body: JSON.stringify(obj)});
const put = (url, obj={}) => api(url, {method:'PUT', body: JSON.stringify(obj)});
const del = (url) => api(url, {method:'DELETE'});

function sheetUrl(c){ return `https://docs.google.com/spreadsheets/d/${encodeURIComponent(c.sheet_id)}/edit#gid=${encodeURIComponent(c.gid || 0)}`; }
function dateFmt(v){ if(!v) return '—'; try { return new Date(typeof v === 'number' ? v * 1000 : v).toLocaleString(); } catch { return String(v); } }
function relStatus(s){ return String(s || '').toUpperCase(); }
function statusBadge(v){
  const s = relStatus(v);
  if (['OK', 'DONE'].includes(s)) return `<span class="badge-status badge-ok">● In Stock</span>`;
  if (['PARTIAL_OOS', 'ALL_OOS'].includes(s)) return `<span class="badge-status badge-oos">● OOS</span>`;
  if (['ERROR','FAILED','DONE_WITH_ERRORS'].includes(s)) return `<span class="badge-status badge-err">● ${esc(s.replaceAll('_',' '))}</span>`;
  if (['RUNNING','QUEUED'].includes(s)) return `<span class="badge-status badge-pending">● ${esc(s.replaceAll('_',' '))}</span>`;
  return `<span class="badge-status badge-pending">● ${esc(s || 'Not checked')}</span>`;
}
function campaignStateBadge(c){
  if (c.sync_error) return `<span class="chip red">Errors</span>`;
  if (!c.active) return `<span class="chip orange">Paused</span>`;
  return `<span class="chip green">Synced</span>`;
}
function pageHeader(tag, title, subtitle){
  return `<header class="page-header"><div class="eyebrow">${esc(tag)}</div><h1>${esc(title)}</h1><p>${esc(subtitle)}</p></header>`;
}
function button(label, cmd, id='', cls='small-btn'){
  return `<button type="button" class="${cls}" data-cmd="${cmd}" data-id="${esc(id)}">${label}</button>`;
}
function card(inner, cls=''){ return `<section class="card ${cls}">${inner}</section>`; }
function kpi(icon, title, value, chip='', chipCls='green'){
  return `<section class="card kpi-card"><div class="kpi-top"><div class="kpi-title">${esc(title)}</div><div class="kpi-icon">${icon}</div></div><div class="kpi-value">${value}</div>${chip ? `<span class="chip ${chipCls}">${chip}</span>` : ''}</section>`;
}
function emptyState(text){ return `<div class="empty-state">${esc(text)}</div>`; }
function navIcon(name){
  return {overview:'⌂', campaigns:'◫', history:'◷', access:'♧', reports:'▥', settings:'⚙', products:'▦'}[name] || '•';
}

const pages = [
  ['overview', 'Overview'],
  ['campaigns', 'Campaigns'],
  ['history', 'Stock History'],
  ['access', 'Access Control'],
  ['reports', 'Reports'],
  ['settings', 'Settings'],
];

function applyBrand(){
  const w = state.workspace || {};
  $('brandName').textContent = w.brand_name || 'Your organization';
  $('brandIcon').textContent = w.logo_initials || 'SI';
  document.title = `${w.brand_name || 'Stock Intelligence'} · ${pages.find(p => p[0] === state.page)?.[1] || 'Dashboard'}`;
  document.documentElement.style.setProperty('--primary', w.accent || '#4f46e5');
}
function applyTheme(){
  const theme = localStorage.getItem('si_theme') || state.workspace?.theme_default || 'light';
  document.documentElement.setAttribute('data-theme', theme);
  $('themeToggle').textContent = theme === 'dark' ? '☀' : '☾';
}
function renderNav(){
  $('navigation').innerHTML = pages.filter(([id]) => !(id === 'access' && state.user?.role !== 'admin')).map(([id, label]) => `
    <button data-nav="${id}" class="${state.page === id ? 'active' : ''}"><span class="nav-icon">${navIcon(id)}</span><span>${label}</span></button>
  `).join('');
  $('breadcrumb').textContent = (pages.find(p => p[0] === state.page)?.[1] || 'Overview').toUpperCase();
  $('accountText').textContent = `${state.user?.role === 'admin' ? 'Admin' : state.user?.username || 'User'}`;
}

function activeCampaign(){ return state.campaigns.find(c => c.id === state.selected) || state.campaigns[0] || null; }
function filteredCampaigns(){
  let items = [...state.campaigns];
  if (state.statusFilter === 'active') items = items.filter(c => c.active && !c.sync_error);
  if (state.statusFilter === 'paused') items = items.filter(c => !c.active);
  if (state.statusFilter === 'errors') items = items.filter(c => !!c.sync_error);
  if (state.search) {
    const q = state.search.toLowerCase();
    items = items.filter(c => `${c.name} ${c.sheet_id}`.toLowerCase().includes(q));
  }
  return items;
}

async function loadBase(){
  const [campaigns, summary, workspace, google, schedules, events] = await Promise.all([
    api('/api/campaigns'),
    api('/api/summary'),
    api('/api/workspace'),
    api('/api/google/status').catch(() => ({connected:false})),
    api('/api/schedules').catch(() => ({schedules:[]})),
    api('/api/events').catch(() => ({events:[]})),
  ]);
  state.campaigns = campaigns.campaigns || [];
  state.summary = summary;
  state.workspace = workspace;
  state.google = google;
  state.schedules = schedules.schedules || [];
  state.events = events.events || [];
  if (!activeCampaign() && state.campaigns[0]) state.selected = state.campaigns[0].id;
  syncGlobalCampaignSelect();
}

async function refreshPageData(){
  const c = activeCampaign();
  if (state.page === 'campaigns' || state.page === 'history' || state.page === 'products') {
    if (c) {
      state.analytics = await api(`/api/campaigns/${c.id}/analytics`).catch(() => ({summary:{total:0,available:0,oos:0,error:0},daily:[],latest:[]}));
      const hist = await api(`/api/campaigns/${c.id}/history`).catch(() => ({history:[]}));
      state.historyRows = hist.history || [];
    } else {
      state.analytics = {summary:{total:0,available:0,oos:0,error:0},daily:[],latest:[]};
      state.historyRows = [];
    }
  }
  if (state.page === 'campaigns' && state.user.role === 'admin') {
    const [users, members] = await Promise.all([
      api('/api/users').catch(() => ({users:[]})),
      c ? api(`/api/campaigns/${c.id}/members`).catch(() => ({members:[]})) : Promise.resolve({members:[]})
    ]);
    state.users = users.users || [];
    state.members = members.members || [];
  }
  if (state.page === 'access' && state.user.role === 'admin') {
    const [users, audit, members] = await Promise.all([
      api('/api/users').catch(() => ({users:[]})),
      api('/api/audit').catch(() => ({events:[]})),
      c ? api(`/api/campaigns/${c.id}/members`).catch(() => ({members:[]})) : Promise.resolve({members:[]}),
    ]);
    state.users = users.users || [];
    state.audit = audit.events || [];
    state.members = members.members || [];
  }
  if (state.page === 'reports' && c) {
    state.driveHistory = (await api(`/api/campaigns/${c.id}/drive`).catch(() => ({exports:[]}))).exports || [];
  }
}

async function refreshAll(){
  await loadBase();
  await refreshPageData();
  applyBrand();
  applyTheme();
  renderNav();
  render();
}

function syncGlobalCampaignSelect(){
  const el = $('globalCampaign');
  const prev = String(state.selected || '');
  el.innerHTML = '<option value="">All Campaigns</option>' + state.campaigns.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
  el.value = prev && state.campaigns.some(c => String(c.id) === prev) ? prev : '';
}

function renderLineChart(points, color='#6c5cff', suffix=''){
  const width = 600, height = 220, padL = 44, padR = 18, padT = 18, padB = 36;
  if (!points.length) return emptyState('No chart data yet. Run a stock check after syncing your sheet.');
  const values = points.map(p => Number(p.value || 0));
  const max = Math.max(...values, 1);
  const min = 0;
  const stepX = points.length === 1 ? 0 : (width - padL - padR) / (points.length - 1);
  const py = (v) => padT + (height - padT - padB) * (1 - (v - min) / (max - min || 1));
  const px = (i) => padL + stepX * i;
  const path = points.map((p, i) => `${i===0 ? 'M' : 'L'} ${px(i)} ${py(p.value)}`).join(' ');
  const area = `${path} L ${px(points.length-1)} ${height-padB} L ${px(0)} ${height-padB} Z`;
  const yTicks = 4;
  let yHtml = '';
  for (let i = 0; i <= yTicks; i++) {
    const val = Math.round(max * (yTicks - i) / yTicks);
    const y = padT + (height - padT - padB) * i / yTicks;
    yHtml += `<line x1="${padL}" x2="${width-padR}" y1="${y}" y2="${y}" stroke="var(--line)" stroke-dasharray="4 6"/><text x="8" y="${y+4}" font-size="11" fill="var(--muted)">${val}${suffix}</text>`;
  }
  const xHtml = points.map((p, i) => `<text x="${px(i)}" y="${height-10}" text-anchor="middle" font-size="11" fill="var(--muted)">${esc(p.label)}</text>`).join('');
  const dots = points.map((p, i) => `<circle cx="${px(i)}" cy="${py(p.value)}" r="4.5" fill="${color}" stroke="white" stroke-width="2"/>`).join('');
  const last = points[points.length-1];
  return `<div class="chart-box"><svg viewBox="0 0 ${width} ${height}">
    <defs><linearGradient id="lineArea" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="${color}" stop-opacity=".22"/><stop offset="100%" stop-color="${color}" stop-opacity=".02"/></linearGradient></defs>
    ${yHtml}
    <path d="${area}" fill="url(#lineArea)"/>
    <path d="${path}" fill="none" stroke="${color}" stroke-width="3" stroke-linecap="round"/>
    ${dots}
    <g>${xHtml}</g>
    <g><rect x="${px(points.length-1)-16}" y="${py(last.value)-36}" width="44" height="24" rx="8" fill="${color}"/><text x="${px(points.length-1)+6}" y="${py(last.value)-20}" text-anchor="middle" font-size="11" fill="white" font-weight="700">${Math.round(last.value)}${suffix}</text></g>
  </svg></div>`;
}

function renderStackedBars(points){
  const width = 600, height = 240, padL = 36, padR = 12, padT = 16, padB = 40;
  if (!points.length) return emptyState('No stacked bar data yet.');
  const totals = points.map(p => Number(p.total || 0));
  const max = Math.max(...totals, 1);
  const barW = Math.max(28, (width - padL - padR) / points.length - 14);
  const gap = ((width - padL - padR) - barW * points.length) / Math.max(points.length - 1, 1);
  const y = (v) => padT + (height - padT - padB) * (1 - v / max);
  let bars = '';
  points.forEach((p, i) => {
    const x = padL + i * (barW + gap);
    const inStock = Number(p.inStock || 0);
    const oos = Number(p.oos || 0);
    const total = inStock + oos;
    const hTotal = height - padB - y(total);
    const hOos = height - padB - y(oos);
    const hIn = Math.max(hTotal - hOos, 0);
    bars += `<g>
      <text x="${x + barW/2}" y="${y(total)-6}" text-anchor="middle" font-size="11" fill="var(--text)" font-weight="700">${money(total)}</text>
      <rect x="${x}" y="${height-padB-hIn}" width="${barW}" height="${hIn}" fill="#22c55e" rx="8"/>
      <rect x="${x}" y="${height-padB-hIn-hOos}" width="${barW}" height="${hOos}" fill="#f87171" rx="8"/>
      <text x="${x+barW/2}" y="${height-padB-hIn/2}" text-anchor="middle" font-size="10" fill="white" font-weight="700">${money(inStock)}</text>
      <text x="${x+barW/2}" y="${height-padB-hIn-hOos/2+4}" text-anchor="middle" font-size="10" fill="white" font-weight="700">${money(oos)}</text>
      <text x="${x+barW/2}" y="${height-12}" text-anchor="middle" font-size="11" fill="var(--muted)">${esc(p.label)}</text>
    </g>`;
  });
  return `<div class="chart-box"><svg viewBox="0 0 ${width} ${height}">${bars}</svg></div>`;
}

function overviewPage(){
  const counts = state.summary?.counts || {};
  const latest = (state.summary?.latest || []).slice(0, 5);
  const latestSync = state.summary?.last_sync?.[0];
  const events = state.events.slice(0, 4);
  const campaignRows = state.campaigns.slice(0, 5).map(c => `
    <tr>
      <td><strong>${esc(c.name)}</strong></td>
      <td><a href="${sheetUrl(c)}" target="_blank">docs.google.com/...</a></td>
      <td>${money(c.styles || 0)}</td>
      <td>${dateFmt(c.synced_at)}</td>
      <td>${dateFmt(c.checked_at)}</td>
      <td>${campaignStateBadge(c)}</td>
      <td><div class="table-actions">${button('View','view',c.id)} ${button('Sync','sync',c.id)} ${button('Check','check',c.id, 'small-btn primary')}</div></td>
    </tr>
  `).join('');
  const latestRows = latest.map(r => `
    <tr>
      <td><strong>${esc(r.style_id)}</strong></td>
      <td>${statusBadge(r.status)}</td>
      <td>${esc(r.oos_sizes || '—')}</td>
      <td>${esc(r.in_sizes || '—')}</td>
      <td>${dateFmt(r.checked_at_str || r.checked_at)}</td>
    </tr>
  `).join('');
  const selectedAnalytics = state.analytics?.daily || [];
  const points = selectedAnalytics.slice(-7).map((d, i) => ({label: String(d.day || '').slice(5) || `D${i+1}`, value: Number(d.oos || 0)}));
  return `${pageHeader('OVERVIEW', 'Stock monitoring dashboard', 'Real-time stock monitoring powered by Google Sheets. Track styles, detect OOS, and keep your inventory in sync.')}
  <div class="grid kpis">
    ${kpi('◫','Active Campaigns', money(counts.active_campaigns || 0), 'Live count','blue')}
    ${kpi('🏷','Monitored Styles', money(counts.styles || 0), 'From Sheets','blue')}
    ${kpi('🔔','Alerts / OOS', money(counts.oos || 0), counts.oos ? 'OOS styles' : 'No OOS', counts.oos ? 'red' : 'green')}
    ${kpi('⚠','Detection Errors', money(counts.errors || 0), counts.errors ? `+${counts.errors}` : 'No errors', counts.errors ? 'orange' : 'green')}
    ${kpi('⚡','Checks Today', money(counts.checks || 0), 'Latest runs','blue')}
    ${kpi('↻','Sync Health', latestSync ? (latestSync.error ? 'Failed' : 'Synced') : 'Not synced', latestSync ? (latestSync.error ? 'Needs attention' : 'Latest sync') : 'No sync yet', latestSync?.error ? 'orange' : latestSync ? 'green' : 'gray')}
  </div>
  <div class="notice-bar">
    <div class="notice-main"><span class="notice-badge">✓</span><strong>${latestSync ? (latestSync.error ? 'Latest Sync Failed' : 'Latest Sync Success') : 'No Sheet synced yet'}</strong>${latestSync ? `<span>${esc(latestSync.campaign)}</span><span>Completed at ${dateFmt(latestSync.at)}</span>${latestSync.error ? `<span>${esc(latestSync.error)}</span>` : `<span>${money(counts.styles || 0)} styles checked</span><span>${money(counts.oos || 0)} OOS found</span><span>${money(counts.errors || 0)} errors</span>`}` : '<span>Connect your first campaign.</span>'}</div>
    <button class="secondary-btn" data-nav="campaigns">View Details →</button>
  </div>
  <section class="card form-panel section">
    <div class="card-head"><h2>Create Google Sheet campaign</h2><p>Connect once. Employees update the Sheet; the system automatically discovers changes.</p></div>
    <div class="card-body">
      <form id="createCampaignForm" class="form-inline">
        <label>Campaign Name<input name="name" required placeholder="stock checker"></label>
        <label>Google Sheet URL<input name="sheet_url" type="url" required placeholder="https://docs.google.com/spreadsheets/d/..." ></label>
        <label>Frequency<select name="interval_minutes">${[60,120,360,1440].map(v=>`<option value="${v}">${v===60?'1 hour':v===120?'2 hours':v===360?'6 hours':'24 hours'}</option>`).join('')}</select></label>
        <button class="primary-btn" type="submit">＋ Create Campaign</button>
      </form>
    </div>
  </section>
  <div class="grid two section">
    ${card(`<div class="panel-title-row"><h2>Campaigns</h2><button class="link-btn" data-nav="campaigns">View All Campaigns →</button></div><div class="table-wrap"><table class="table"><thead><tr><th>Campaign Name</th><th>Linked Sheet</th><th>Styles</th><th>Last Sync</th><th>Next Run</th><th>Status</th><th>Actions</th></tr></thead><tbody>${campaignRows || `<tr><td colspan="7">${emptyState('No campaigns yet.')}</td></tr>`}</tbody></table></div>`)}
    ${card(`<div class="panel-title-row"><h2>Latest stock results</h2><button class="link-btn" data-nav="history">View Full Results →</button></div><div class="table-wrap"><table class="table"><thead><tr><th>Style ID</th><th>Status</th><th>OOS Sizes</th><th>Available</th><th>Checked At</th></tr></thead><tbody>${latestRows || `<tr><td colspan="5">${emptyState('No results yet')}</td></tr>`}</tbody></table></div>`)}
  </div>
  <div class="grid two section">
    ${card(`<div class="chart-header"><div><h2>OOS Trend</h2><p class="muted">Number of out-of-stock styles over time</p></div><span class="tag">Last 7 Days</span></div>${renderLineChart(points, '#6c5cff')}`)}
    ${card(`<div class="panel-title-row"><div><h2>Recent Activity</h2><p class="muted">Latest syncs and user activity</p></div><button class="link-btn" data-nav="reports">View All Activity →</button></div><div class="timeline">${events.length ? events.slice(0, 5).map(ev => `<div class="timeline-item"><span class="timeline-dot ${String(ev.event || '').includes('error') ? 'red' : 'green'}"></span><div><strong>${esc((ev.event || '').replaceAll('_',' '))}</strong><div class="tiny">Campaign: ${esc(ev.campaign_name || '—')}</div></div><span class="tiny">${dateFmt(ev.at)}</span></div>`).join('') : emptyState('No activity yet')}</div>`)}
  </div>`;
}

function renderCampaignCards(items){
  return `<div class="grid four section">${items.slice(0,4).map(c => card(`
    <div class="panel-title-row"><div><h2>${esc(c.name)}</h2><p class="muted">${c.sync_error ? 'Review sync errors and reconnect the sheet.' : 'Monitor campaign stock levels and OOS changes.'}</p></div>${campaignStateBadge(c)}</div>
    <div class="info-list">
      <div class="info-line"><span>Sheet</span><a href="${sheetUrl(c)}" target="_blank">docs.google.com/spreadsheets...</a></div>
      <div class="info-line"><span>Styles</span><strong>${money(c.styles || 0)}</strong></div>
      <div class="info-line"><span>Manager</span><strong>${esc(c.created_by_name || state.user.username || 'Admin')}</strong></div>
      <div class="info-line"><span>Next Run</span><strong>${c.checked_at ? dateFmt(c.checked_at + (c.interval_minutes || 0) * 60) : '—'}</strong></div>
      <div class="info-line"><span>Last Check</span><strong>${dateFmt(c.checked_at)}</strong></div>
    </div>
    <div class="table-actions" style="margin-top:14px">${button('View','view',c.id)} ${button('Sync','sync',c.id)} ${button('Check Now','check',c.id,'small-btn primary')} ${button(c.active ? 'Pause' : 'Resume','toggle',c.id)}</div>
  `)).join('')}</div>`;
}

function campaignsPage(){
  const items = filteredCampaigns();
  const active = items.filter(c => c.active && !c.sync_error).length;
  const paused = items.filter(c => !c.active).length;
  const errored = items.filter(c => !!c.sync_error).length;
  const c = activeCampaign();
  const summary = state.analytics?.summary || {total:0,available:0,oos:0,error:0};
  const daily = state.analytics?.daily || [];
  const dailyPoints = daily.slice(-7).map((d, i) => ({label: String(d.day || '').slice(5) || `D${i+1}`, value: Number(d.checks || 0)}));
  const recentRuns = state.historyRows.slice(0,5);
  const rows = items.map(c => `
    <tr>
      <td>${state.selected === c.id ? '☑' : '☐'}</td>
      <td><strong>${esc(c.name)}</strong></td>
      <td><a href="${sheetUrl(c)}" target="_blank">docs.google.com/spreadsheets/d/${esc(c.sheet_id).slice(0, 14)}...</a></td>
      <td>${c.interval_minutes >= 1440 ? Math.round(c.interval_minutes/1440) + ' day' : c.interval_minutes >= 60 ? Math.round(c.interval_minutes/60) + ' hour' : c.interval_minutes + ' min'}</td>
      <td>${esc(state.user.username)}</td>
      <td>${money(c.styles || 0)}</td>
      <td>${dateFmt(c.synced_at)}</td>
      <td>${c.checked_at ? dateFmt(c.checked_at + (c.interval_minutes || 0) * 60) : '—'}</td>
      <td>${campaignStateBadge(c)}</td>
      <td><div class="table-actions">${button('▷','select',c.id)} ${button('✎','edit',c.id)} ${button('↻','sync',c.id)} ${button(c.active ? 'Ⅱ' : '▶','toggle',c.id)}</div></td>
    </tr>`).join('');
  const historyRows = recentRuns.map(r => `<tr><td>${dateFmt(r.checked_at)}</td><td>${statusBadge(r.status)}</td><td>${esc(r.style_id || '—')}</td><td>${esc(r.oos_sizes || '—')}</td><td>${esc(r.in_sizes || '—')}</td></tr>`).join('');
  return `${pageHeader('CAMPAIGNS', 'Campaigns', 'Manage Google Sheet monitoring campaigns to track styles, detect OOS, and sync your inventory in real time.')}
  <div class="filter-row"><div class="filter-chips">
      <button class="filter-chip ${state.statusFilter==='all'?'active':''}" data-filter="all">All Campaigns <span class="chip blue">${items.length}</span></button>
      <button class="filter-chip ${state.statusFilter==='active'?'active':''}" data-filter="active">Active <span class="chip green">${active}</span></button>
      <button class="filter-chip ${state.statusFilter==='paused'?'active':''}" data-filter="paused">Paused <span class="chip orange">${paused}</span></button>
      <button class="filter-chip ${state.statusFilter==='errors'?'active':''}" data-filter="errors">Errors <span class="chip red">${errored}</span></button>
    </div>
    <div class="table-actions">${button('⬆ Bulk Import','bulk')} ${button('＋ New Campaign','create','','small-btn primary')}</div>
  </div>
  ${renderCampaignCards(items)}
  ${card(`<div class="panel-title-row"><h2>Campaigns (${money(items.length)})</h2></div><div class="table-wrap"><table class="table"><thead><tr><th></th><th>Campaign</th><th>Sheet Source</th><th>Frequency</th><th>Owner</th><th>Monitored Styles</th><th>Last Sync</th><th>Next Run</th><th>Status</th><th>Actions</th></tr></thead><tbody>${rows || `<tr><td colspan="10">${emptyState('No campaigns found')}</td></tr>`}</tbody></table></div>`,'section')}
  ${c ? `<div class="split two section">
    ${card(`<div class="panel-title-row"><div><h2>${esc(c.name)}</h2><p class="muted">Monitor campaign stock levels and OOS changes across all key styles.</p></div><div class="table-actions">${button('Edit Campaign','edit',c.id)} ${button('Check Now','check',c.id,'small-btn primary')} ${button(c.active ? 'Pause' : 'Resume','toggle',c.id)}</div></div>
      <div class="info-list">
        <div class="info-line"><span>Google Sheet URL</span><a href="${sheetUrl(c)}" target="_blank">${sheetUrl(c)}</a></div>
        <div class="info-line"><span>Frequency</span><strong>${c.interval_minutes >= 60 ? Math.round(c.interval_minutes/60)+' hour' : c.interval_minutes + ' min'}</strong></div>
        <div class="info-line"><span>Status</span>${campaignStateBadge(c)}</div>
        <div class="info-line"><span>Last Sync Result</span><strong>${c.sync_error ? 'Failed — ' + esc(c.sync_error) : `Success — ${money(c.styles || 0)} styles checked`}</strong></div>
        <div class="info-line"><span>Monitored Counts</span><strong>${money(summary.total || c.styles || 0)} styles • ${money(summary.available || 0)} in stock • ${money(summary.oos || 0)} OOS</strong></div>
      </div>`)}
    ${card(`<div class="chart-header"><div><h2>Sync Trend</h2><p class="muted">Number of monitored styles checked over time.</p></div><span class="tag">Last 7 Days</span></div>${renderLineChart(dailyPoints,'#6c5cff')}<div class="metric-pair" style="margin-top:14px"><div class="metric-mini"><span class="tiny">Monitored Styles</span><strong>${money(summary.total || 0)}</strong></div><div class="metric-mini"><span class="tiny">In Stock</span><strong>${money(summary.available || 0)}</strong></div><div class="metric-mini"><span class="tiny">OOS</span><strong>${money(summary.oos || 0)}</strong></div><div class="metric-mini"><span class="tiny">Errors</span><strong>${money(summary.error || 0)}</strong></div></div>`)}
  </div>
  <div class="grid two section">
    ${card(`<div class="panel-title-row"><h2>Recent Run History</h2><button class="link-btn" data-nav="history">View Full History →</button></div><div class="table-wrap"><table class="table"><thead><tr><th>Run Time</th><th>Result</th><th>Checked</th><th>OOS</th><th>Available</th></tr></thead><tbody>${historyRows || `<tr><td colspan="5">${emptyState('No run history yet')}</td></tr>`}</tbody></table></div>`)}
    ${card(`<div class="panel-title-row"><h2>Assigned Members</h2>${state.user.role === 'admin' ? button('Assign User','assignMember',c.id,'small-btn primary') : ''}</div><div class="timeline">${(state.members || []).length ? state.members.map(m => `<div class="timeline-item"><span class="timeline-dot green"></span><div><strong>${esc(m.username)}</strong><div class="tiny">${esc(m.role)}</div></div>${state.user.role==='admin' ? `<button class="small-btn" data-cmd="removeMember" data-id="${m.id}">Remove</button>` : ''}</div>`).join('') : emptyState('No members assigned yet')}</div>`)}
  </div>` : card(emptyState('Create a campaign to start monitoring stock.'))}`;
}

function historyPage(){
  const c = activeCampaign();
  const daily = state.analytics?.daily || [];
  const pctPoints = daily.slice(-8).map((d, i) => {
    const checks = Number(d.checks || 0);
    const oos = Number(d.oos || 0);
    return {label: String(d.day || '').slice(5) || `D${i+1}`, value: checks ? (oos / checks) * 100 : 0};
  });
  const bars = daily.slice(-8).map((d, i) => ({label: String(d.day || '').slice(5) || `D${i+1}`, inStock: Math.max(Number(d.checks || 0) - Number(d.oos || 0), 0), oos: Number(d.oos || 0), total: Number(d.checks || 0)}));
  const rows = state.historyRows.filter(r => (!state.search || String(r.style_id).includes(state.search)) && (!state.statusFilter || state.statusFilter==='all' || r.status===state.statusFilter)).map(r => `
    <tr>
      <td>${dateFmt(r.checked_at)}</td>
      <td>${esc(c?.name || 'Campaign')}</td>
      <td>${esc(r.style_id)}</td>
      <td>${esc(r.style_id)}</td>
      <td>${esc((r.oos_sizes || r.in_sizes || '').split(',')[0] || '—')}</td>
      <td>—</td>
      <td>${statusBadge(r.status)}</td>
      <td>${r.status === 'OK' ? 'In Stock observed' : r.status === 'ERROR' ? 'Detection Error' : 'OOS observed'}</td>
      <td>Google Sheet Sync</td>
      <td>${button('View','detailHistory',r.style_id)}</td>
    </tr>`).join('');
  const recent = state.historyRows.slice(0,5).map(r => `<div class="timeline-item"><span class="timeline-dot ${r.status === 'OK' ? 'green' : r.status === 'ERROR' ? 'gray' : 'red'}"></span><div><strong>${r.status === 'OK' ? 'In Stock' : r.status === 'ERROR' ? 'Error' : 'OOS'}: ${esc(r.style_id)}</strong><div class="tiny">${esc(r.oos_sizes || r.in_sizes || 'No size detail')} • Campaign: ${esc(c?.name || '—')} • Source: Google Sheet Sync</div></div><span class="tiny">${dateFmt(r.checked_at)}</span></div>`).join('');
  const checks = bars.reduce((a,b) => a+b.total,0), oos = bars.reduce((a,b)=>a+b.oos,0), restocks = state.historyRows.filter(r=>r.status==='OK').length, errors = state.historyRows.filter(r=>r.status==='ERROR').length;
  return `${pageHeader('STOCK HISTORY', 'Stock History', 'Historical stock changes by campaign, style, and size. Track OOS transitions, restocks, and sync results over time.')}
  <section class="card section">
    <div class="form-grid four">
      <label>Campaign<select id="historyCampaignSelect">${state.campaigns.map(c => `<option value="${c.id}" ${c.id===state.selected?'selected':''}>${esc(c.name)}</option>`).join('')}</select></label>
      <label>Style ID<input id="historySearch" placeholder="Search style ID (e.g., TL-DB-014)" value="${esc(state.search)}"></label>
      <label>Status<select id="historyStatus"><option value="all">All Statuses</option><option value="OK" ${state.statusFilter==='OK'?'selected':''}>In Stock</option><option value="PARTIAL_OOS" ${state.statusFilter==='PARTIAL_OOS'?'selected':''}>Partial OOS</option><option value="ALL_OOS" ${state.statusFilter==='ALL_OOS'?'selected':''}>All OOS</option><option value="ERROR" ${state.statusFilter==='ERROR'?'selected':''}>Errors</option></select></label>
      <label>Date Range<select><option>Last 7 Days</option><option>Last 30 Days</option></select></label>
    </div>
  </section>
  <div class="grid four section">
    ${kpi('◫','Total Checks', money(checks), 'Observed checks','blue')}
    ${kpi('⚠','OOS Transitions', money(oos), 'Observed OOS','red')}
    ${kpi('↻','In-stock observations', money(restocks), 'In Stock results','green')}
    ${kpi('⚡','Detection errors', money(errors), 'From available results', 'blue')}
  </div>
  <div class="grid two section">
    ${card(`<div class="chart-header"><div><h2>OOS Trend Over Time</h2><p class="muted">Percentage of styles that are out-of-stock (OOS) over time</p></div><div class="tag">Rate (%)</div></div>${renderLineChart(pctPoints,'#6c5cff','%')}`)}
    ${card(`<div class="chart-header"><div><h2>In-Stock vs Out-of-Stock Counts</h2><p class="muted">Number of style-size records by stock status over time</p></div><div class="tag">Daily</div></div><div class="chart-legend"><span><i class="legend-dot" style="background:#22c55e"></i>In Stock</span><span><i class="legend-dot" style="background:#f87171"></i>Out of Stock</span></div>${renderStackedBars(bars)}`)}
  </div>
  <div class="split two section">
    ${card(`<div class="panel-title-row"><div><h2>Historical Stock Records</h2><p class="muted">Detailed history of stock checks and status changes.</p></div><button class="link-btn" data-cmd="historyCsv">Export CSV</button></div><div class="table-wrap"><table class="table"><thead><tr><th>Checked At</th><th>Campaign</th><th>Style ID</th><th>Product</th><th>Size</th><th>Previous Status</th><th>Current Status</th><th>Change Type</th><th>Source</th><th>Details</th></tr></thead><tbody>${rows || `<tr><td colspan="10">${emptyState('No history records yet')}</td></tr>`}</tbody></table></div>`)}
    ${card(`<div class="panel-title-row"><div><h2>Recent status changes</h2><p class="muted">Latest stock status changes with audit trail.</p></div></div><div class="timeline">${recent || emptyState('No recent changes')}</div>`)}
  </div>`;
}

function accessPage(){
  if (state.user.role !== 'admin') return `${pageHeader('ACCESS CONTROL','Access Control','Manage users, roles, permissions, and secure sign-in for your organization.')}${card(emptyState('Only administrators can access user management.'))}`;
  const users = state.users || [];
  const admins = users.filter(u=>u.role==='admin').length;
  const managers = users.filter(u=>u.role==='manager').length;
  const employees = users.filter(u=>u.role==='employee').length;
  const activeUsers = users.filter(u=>u.active).length;
  const rows = users.map(u => `
    <tr>
      <td><strong>${esc(u.username)}</strong></td>
      <td>${esc(u.username)}</td>
      <td><span class="chip ${u.role==='admin'?'blue':u.role==='manager'?'orange':'green'}">${esc(u.role)}</span></td>
      <td>${u.role === 'employee' ? money((state.members || []).filter(m => m.id === u.id).length) + ' Campaigns' : 'All Campaigns'}</td>
      <td>${dateFmt(u.last_login_at)}</td>
      <td>${u.mfa_enabled ? '✓' : '—'}</td>
      <td>${u.active ? '<span class="chip green">Active</span>' : '<span class="chip gray">Inactive</span>'}</td>
      <td><div class="table-actions">${button('Edit','userEdit',u.id)} ${u.active ? button('Disable','userDisable',u.id) : button('Enable','userEnable',u.id)}</div></td>
    </tr>`).join('');
  const audit = state.audit.slice(0,5).map(a => `<div class="timeline-item"><span class="timeline-dot ${String(a.event).includes('disabled') ? 'red' : 'green'}"></span><div><strong>${esc(String(a.event).replaceAll('_',' '))}</strong><div class="tiny">${esc(a.username || 'system')} ${esc(a.detail || '')}</div></div><span class="tiny">${dateFmt(a.at)}</span></div>`).join('');
  return `${pageHeader('ACCESS CONTROL', 'Access Control', 'Manage users, roles, permissions, and secure sign-in for your organization.')}
  <div class="grid four section">
    ${kpi('👥','Total Users', money(users.length), '+20%','green')}
    ${kpi('⚡','Active Sessions', money(activeUsers), '+27%','green')}
    ${kpi('✉','Pending Invites', '0', '+0','gray')}
    ${kpi('🛡','MFA Enabled', money(users.filter(u=>u.mfa_enabled).length), users.length ? Math.round(users.filter(u=>u.mfa_enabled).length * 100 / users.length) + '%' : '0%','green')}
  </div>
  <div class="split two section">
    ${card(`<div class="panel-title-row"><div><h2>User Management</h2><p class="muted">All users, roles, and access to campaigns.</p></div><div class="table-actions">${button('Add User','addUser','','small-btn primary')}</div></div><div class="table-wrap"><table class="table"><thead><tr><th>Name</th><th>Username</th><th>Role</th><th>Assigned Campaigns</th><th>Last Login</th><th>MFA</th><th>Status</th><th>Actions</th></tr></thead><tbody>${rows || `<tr><td colspan="8">${emptyState('No users yet')}</td></tr>`}</tbody></table></div>`)}
    ${card(`<div class="panel-title-row"><div><h2>Create User</h2><p class="muted">Create a secure local account and assign a campaign (no email is sent).</p></div></div>
      <form id="inviteUserForm" class="form-grid two">
        <label>Name<input name="display_name" placeholder="e.g., Jane Lee"></label>
        <label>Username<input name="username" required placeholder="e.g., jane.lee"></label>
        <label>Email<input name="email" placeholder="e.g., jane@example.com"></label>
        <label>Role<select name="role"><option value="employee">Employee</option><option value="manager">Manager</option><option value="admin">Admin</option></select></label>
        <label class="full">Temporary Password<input name="password" required minlength="12" placeholder="12+ character password"></label>
        <label class="full">Assigned Campaign<select name="campaign_id">${state.campaigns.map(c=>`<option value="${c.id}">${esc(c.name)}</option>`).join('')}</select></label>
        <div class="table-actions"><button class="secondary-btn" type="reset">Cancel</button><button class="primary-btn" type="submit">Create User</button></div>
      </form>`)}
  </div>
  <div class="split two section">
    ${card(`<div class="panel-title-row"><div><h2>Roles & Permissions</h2><p class="muted">User roles and their key capabilities.</p></div></div><div class="role-grid"><div class="role-card"><div class="panel-title-row"><strong>Admin</strong><span class="chip blue">Full Access</span></div><ul><li>Full system access & configuration</li><li>Manage users, roles & permissions</li><li>Create, edit & delete campaigns</li><li>Access all reports & stock history</li><li>Manage security settings (MFA, SSO)</li></ul></div><div class="role-card"><div class="panel-title-row"><strong>Manager</strong><span class="chip orange">Campaign Access</span></div><ul><li>Manage assigned campaigns</li><li>View & export campaign reports</li><li>Monitor stock & OOS alerts</li><li>Invite users (Employee only)</li><li>No system-wide security changes</li></ul></div><div class="role-card"><div class="panel-title-row"><strong>Employee</strong><span class="chip green">Limited Access</span></div><ul><li>View assigned campaigns</li><li>Check stock status & alerts</li><li>View campaign reports</li><li>Sync & monitor campaign data</li><li>No user/system settings access</li></ul></div></div>`)}
    ${card(`<div class="panel-title-row"><div><h2>Recent Security Activity</h2><p class="muted">Key account and security events across your organization.</p></div></div><div class="timeline">${audit || emptyState('No audit activity yet')}</div><div class="metric-pair" style="margin-top:14px"><div class="metric-mini"><span class="tiny">Admins</span><strong>${admins}</strong></div><div class="metric-mini"><span class="tiny">Managers</span><strong>${managers}</strong></div><div class="metric-mini"><span class="tiny">Employees</span><strong>${employees}</strong></div><div class="metric-mini"><span class="tiny">Active</span><strong>${activeUsers}</strong></div></div>`)}
  </div>`;
}

function reportsPage(){
  const schedules = state.schedules || [];
  const c = activeCampaign();
  const rows = state.campaigns.map(c => `
    <tr>
      <td><strong>${esc(c.name)} Report</strong></td>
      <td>${esc(c.name)}</td>
      <td><span class="chip blue">Stock</span></td>
      <td>Latest</td>
      <td>XLSX</td>
      <td>${dateFmt(c.checked_at)}</td>
      <td>Local / Google Drive</td>
      <td>${c.last_run_id ? '<span class="chip green">Completed</span>' : '<span class="chip gray">Pending</span>'}</td>
      <td><div class="table-actions">${button('Preview','view',c.id)} ${button('Download','excel',c.id)} ${button('Drive Upload','drive',c.id)}</div></td>
    </tr>`).join('');
  const recent = (state.driveHistory || []).slice(0,5).map(x => `<div class="list-row"><div><strong>${esc(c?.name || 'Campaign')} export</strong><div class="tiny">Run ${esc(x.run_id || '—')}</div></div><div><div>${dateFmt(x.uploaded_at)}</div><a href="${esc(x.url || '#')}" target="_blank">Open</a></div></div>`).join('');
  return `${pageHeader('REPORTS','Reports','Export reports, view report history, and schedule Google Drive delivery for your campaigns and stock insights.')}
  <div class="grid four section">
    ${kpi('📄','Campaign Reports', money(state.campaigns.filter(c=>!!c.last_run_id).length), 'Latest run','blue')}
    ${kpi('🗓','Scheduled Exports', money(schedules.length), 'Saved schedules','blue')}
    ${kpi('⏰','Last Export', c?.checked_at ? dateFmt(c.checked_at) : '—', '', 'gray')}
    ${kpi('☁','Drive Sync Health', state.google?.connected ? 'Connected' : 'Not connected', state.google?.connected ? 'Healthy' : 'Connect Google', state.google?.connected ? 'green' : 'orange')}
  </div>
  ${card(`<div class="panel-title-row"><div><h2>Reports Library</h2><p class="muted">All generated reports with actions including preview, download, and Google Drive delivery.</p></div><div class="table-actions">${button('+ Create Report','excel', c?.id || '', 'small-btn primary')}</div></div><div class="table-wrap"><table class="table"><thead><tr><th>Report Name</th><th>Campaign</th><th>Type</th><th>Period</th><th>Format</th><th>Created At</th><th>Destination</th><th>Status</th><th>Actions</th></tr></thead><tbody>${rows || `<tr><td colspan="9">${emptyState('No reports available')}</td></tr>`}</tbody></table></div>`,'section')}
  <div class="split equal section">
    ${card(`<div class="panel-title-row"><div><h2>Scheduled Report Delivery</h2><p class="muted">Automate report exports and deliver them to your team and Google Drive.</p></div></div><form id="scheduleForm" class="form-grid two"><label>Campaign<select name="campaign_id">${state.campaigns.map(c => `<option value="${c.id}" ${c.id===state.selected?'selected':''}>${esc(c.name)}</option>`).join('')}</select></label><label>Frequency<select name="frequency_hours"><option value="24">Daily</option><option value="168">Weekly</option><option value="720">Monthly</option></select></label><label>Report Type<input value="Stock check XLSX" readonly></label><label>Delivery Destination<input value="Local report / optional Google Drive" readonly></label><label class="switch"><input type="checkbox" name="auto_drive" checked> Auto-upload to Google Drive</label><div class="table-actions"><button class="secondary-btn" type="reset">Cancel</button><button class="primary-btn" type="submit">Save Schedule</button></div></form><div class="padded-list" style="margin-top:18px">${schedules.length ? schedules.map(s => `<div class="list-row"><div><strong>${esc(s.campaign_name)}</strong><div class="tiny">Every ${s.frequency_hours} hours ${s.auto_drive ? '• Drive enabled' : ''}</div></div><div class="table-actions">${button('Delete','removeSchedule',s.id)}</div></div>`).join('') : emptyState('No report schedules yet')}</div>`)}
    ${card(`<div class="panel-title-row"><div><h2>Recent Exports</h2><p class="muted">Latest report exports and their delivery outcomes.</p></div></div><div class="padded-list">${recent || emptyState('No Drive exports yet')}</div>`)}
  </div>`;
}

function settingsPage(){
  const tabs = [['general','General'],['google','Google Sheets'],['drive','Google Drive'],['monitoring','Monitoring'],['security','Security']];
  const w = state.workspace || {};
  let inner = '';
  if (state.settingsTab === 'general') {
    inner = `${card(`<div class="panel-title-row"><div><h2>Brand Settings</h2><p class="muted">Manage your organization identity and workspace appearance.</p></div></div><form id="settingsBrand" class="form-grid two"><label>Organization Name<input name="brand_name" value="${esc(w.brand_name || '')}" required></label><label>Logo Initials<input name="logo_initials" maxlength="3" value="${esc(w.logo_initials || 'SI')}" required></label><label>Brand Accent<input name="accent" type="color" value="${esc(w.accent || '#4f46e5')}"></label><label>Default Theme<select name="theme_default"><option value="light" ${w.theme_default==='light'?'selected':''}>Day Mode</option><option value="dark" ${w.theme_default==='dark'?'selected':''}>Night Mode</option></select></label><div class="table-actions"><button type="submit" class="primary-btn">Save Brand</button></div></form>`)}
      <div class="grid three section">
        ${card(`<h2>Workspace Identity</h2><p class="muted">Brand name shown in the sidebar and login screen.</p><div class="tag">${esc(w.brand_name || 'Your organization')}</div>`)}
        ${card(`<h2>Theme Mode</h2><p class="muted">User can still toggle day/night mode individually.</p><div class="tag">${esc(w.theme_default || 'light')}</div>`)}
        ${card(`<h2>Primary Color</h2><p class="muted">Used for buttons, chips, and highlights.</p><div class="tag">${esc(w.accent || '#4f46e5')}</div>`)}
      </div>`;
  }
  if (state.settingsTab === 'google') {
    inner = `<div class="grid two"><section class="card"><div class="panel-title-row"><div><h2>Google Account</h2><p class="muted">Connect once for access to private Google Sheets and Drive.</p></div>${state.google?.connected ? '<span class="chip green">Connected</span>' : '<span class="chip orange">Not connected</span>'}</div><div class="padded-list"><div class="list-row"><div><strong>Status</strong><div class="tiny">${state.google?.connected ? 'Google account linked successfully.' : 'OAuth credentials required for private Sheets and Drive uploads.'}</div></div><div class="table-actions">${state.google?.connected ? button('Disconnect','disconnectGoogle','','small-btn') : button('Connect Google','connectGoogle','','small-btn primary')}</div></div></div></section><section class="card"><h2>Google Sheets access</h2><p class="muted">Use a published Sheet URL or connect Google OAuth for private Sheets. The linked Sheet must contain <strong>style_id</strong>, <strong>sizes</strong> and optional <strong>active</strong> columns.</p><p><strong>Permission:</strong> The Google account owning the campaign must have read access. For a service account, share the Google Sheet with its client email.</p></section></div>`;
  }
  if (state.settingsTab === 'drive') {
    inner = `<div class="grid two"><section class="card"><div class="panel-title-row"><div><h2>Google Drive</h2><p class="muted">Save reports, exports, and screenshots to Drive.</p></div>${state.google?.connected ? '<span class="chip green">Connected</span>' : '<span class="chip orange">Connect Google</span>'}</div><div class="padded-list"><div class="list-row"><div><strong>Drive Uploads</strong><div class="tiny">For each campaign, open Campaigns → View → Auto-upload and set a Drive folder ID.</div></div><div class="table-actions">${activeCampaign() ? button('Upload Latest Report','drive',activeCampaign().id,'small-btn primary') : ''}</div></div></div></section><section class="card"><h2>Drive Folder Routing</h2><p class="muted">Drive uploads use the folder ID configured per campaign. This is ideal for shared team folders on Railway as well as local testing.</p></section></div>`;
  }
  if (state.settingsTab === 'monitoring') {
    inner = `<div class="grid three"><section class="card"><h2>Monitoring Settings</h2><div class="info-list"><div class="info-line"><span>Default Frequency</span><strong>1 hour</strong></div><div class="info-line"><span>Retry Count</span><strong>3</strong></div><div class="info-line"><span>Browser Mode</span><strong>Headless (Recommended)</strong></div><div class="info-line"><span>Error Handling</span><strong>Retry with exponential backoff</strong></div></div></section><section class="card"><h2>Notification Preferences</h2><div class="info-list"><div class="info-line"><span>Email Alerts</span><strong>Enabled</strong></div><div class="info-line"><span>WhatsApp Alerts</span><strong>Optional</strong></div><div class="info-line"><span>Summary Reports</span><strong>Daily</strong></div></div></section><section class="card"><h2>Security Preferences</h2><div class="info-list"><div class="info-line"><span>Session Timeout</span><strong>30 minutes</strong></div><div class="info-line"><span>Password Policy</span><strong>Strong (12+ chars)</strong></div><div class="info-line"><span>MFA Enforcement</span><strong>Supported</strong></div></div></section></div>`;
  }
  if (state.settingsTab === 'security') {
    inner = `${card(`<div class="panel-title-row"><div><h2>Account Security</h2><p class="muted">Change your password or enable an authenticator.</p></div></div><form id="passwordForm" class="form-grid two"><label>Current Password<input type="password" name="current" required></label><label>New Password (12+ characters)<input type="password" name="new" minlength="12" required></label><div class="table-actions"><button type="submit" class="primary-btn">Change Password</button>${button('Set up Authenticator','beginMfa','','small-btn')}</div></form><div class="helper">User accounts and campaign permissions are controlled by an administrator.</div>`)}
      <div class="grid two section">${card(`<h2>API & Session</h2><div class="info-list"><div class="info-line"><span>Session Timeout</span><strong>8 hours</strong></div><div class="info-line"><span>CSRF Protection</span><strong>Enabled</strong></div><div class="info-line"><span>Audit Trail</span><strong>Enabled</strong></div></div>`)}${card(`<h2>Deployment Notes</h2><p class="muted">For Railway, set persistent volume/data directory, secure environment variables, and Google OAuth redirect URL using your Railway public domain.</p>`)}
      </div>`;
  }
  return `${pageHeader('SETTINGS','Settings','Manage integrations, sync policies, notifications, and security preferences for your stock monitoring.')}
    <div class="settings-tabs">${tabs.map(([id,label]) => `<button class="settings-tab ${state.settingsTab===id?'active':''}" data-settingstab="${id}">${label}</button>`).join('')}</div>
    ${inner}`;
}

function productsPage(){
  const rows = (state.analytics?.latest || []).map(r => `<tr><td>${esc(r.style_id)}</td><td>${statusBadge(r.status)}</td><td>${esc(r.oos_sizes || '—')}</td><td>${esc(r.in_sizes || '—')}</td></tr>`).join('');
  return `${pageHeader('PRODUCTS','All Products','View the latest campaign product results by style.')}${card(`<div class="table-wrap"><table class="table"><thead><tr><th>Style ID</th><th>Status</th><th>OOS Sizes</th><th>Available Sizes</th></tr></thead><tbody>${rows || `<tr><td colspan="4">${emptyState('No product results yet')}</td></tr>`}</tbody></table></div>`,'section')}`;
}

function render(){
  let html = '';
  if (state.page === 'overview') html = overviewPage();
  else if (state.page === 'campaigns') html = campaignsPage();
  else if (state.page === 'history') html = historyPage();
  else if (state.page === 'access') html = accessPage();
  else if (state.page === 'reports') html = reportsPage();
  else if (state.page === 'settings') html = settingsPage();
  else html = productsPage();
  $('content').innerHTML = html;
}

async function navigate(page){
  state.page = page;
  state.search = '';
  if (page === 'settings' && !state.settingsTab) state.settingsTab = 'general';
  await refreshPageData();
  renderNav();
  render();
  window.scrollTo({top:0, behavior:'instant'});
}

async function handleAction(cmd, id=''){
  try {
    if (cmd === 'bulk') return modalBulkCreate();
    if (cmd === 'create') {
      $('globalSearch').value = '';
      return modalCampaignCreate();
    }
    if (cmd === 'select') { state.selected = Number(id); await refreshPageData(); render(); return; }
    if (cmd === 'view') { state.selected = Number(id); await navigate('campaigns'); return; }
    if (cmd === 'sync') {
      const res = await post(`/api/campaigns/${id}/sync`); showToast(`Sheet synced: ${res.styles || 0} styles`); await refreshAll(); return;
    }
    if (cmd === 'check') {
      const res = await post(`/api/campaigns/${id}/run`); showToast(res.run_id ? `Stock check queued: ${res.run_id}` : 'No styles synced yet or another run is active'); await refreshAll(); return;
    }
    if (cmd === 'toggle') {
      const c = state.campaigns.find(x => String(x.id) === String(id));
      await post(`/api/campaigns/${id}/toggle`, {active: !(c?.active)}); showToast(c?.active ? 'Campaign paused' : 'Campaign resumed'); await refreshAll(); return;
    }
    if (cmd === 'edit') return modalCampaignEdit(Number(id));
    if (cmd === 'excel') { window.location.href = `/api/campaigns/${id}/report`; return; }
    if (cmd === 'drive') { const res = await post(`/api/campaigns/${id}/drive`); showToast(res.drive_url ? 'Report uploaded to Google Drive' : 'Drive upload processed'); await refreshAll(); return; }
    if (cmd === 'detailHistory') return modalHistoryDetail(id);
    if (cmd === 'historyCsv') return downloadCSV();
    if (cmd === 'removeSchedule') { if (!confirm('Delete this report schedule?')) return; await del(`/api/schedules/${id}`); showToast('Schedule removed'); await refreshAll(); return; }
    if (cmd === 'addUser') return modalAddUser();
    if (cmd === 'userEdit') return modalUserEdit(Number(id));
    if (cmd === 'userDisable') { await put(`/api/users/${id}`, {role: (state.users.find(u => u.id === Number(id)) || {}).role || 'employee', active: 0}); showToast('User disabled'); await refreshAll(); return; }
    if (cmd === 'userEnable') { await put(`/api/users/${id}`, {role: (state.users.find(u => u.id === Number(id)) || {}).role || 'employee', active: 1}); showToast('User enabled'); await refreshAll(); return; }
    if (cmd === 'assignMember') return modalAssignMember();
    if (cmd === 'removeMember') { if (!confirm('Remove this user from the campaign?')) return; await post(`/api/campaigns/${activeCampaign().id}/membership`, {user_id: Number(id), action:'remove'}); showToast('Member removed'); await refreshAll(); return; }
    if (cmd === 'connectGoogle') { const res = await api('/api/google/start'); window.location.href = res.authorization_url; return; }
    if (cmd === 'disconnectGoogle') { if (!confirm('Disconnect Google account?')) return; await post('/api/google/disconnect'); showToast('Google disconnected'); await refreshAll(); return; }
    if (cmd === 'beginMfa') return beginMfa();
  } catch (err) {
    showToast(err.message, true);
  }
}

function downloadCSV(){
  const rows = [['checked_at','style_id','status','oos_sizes','in_sizes']].concat(state.historyRows.map(r => [dateFmt(r.checked_at), r.style_id, r.status, r.oos_sizes || '', r.in_sizes || '']));
  const blob = new Blob([rows.map(row => row.map(v => `"${String(v).replaceAll('"','""')}"`).join(',')).join('\r\n')], {type:'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'stock_history.csv'; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1200);
}

function openModal(title, description, fieldsHtml, submitLabel, onSubmit){
  $('modalTitle').textContent = title;
  $('modalDescription').textContent = description || '';
  $('modalFields').innerHTML = fieldsHtml;
  $('modalSubmit').textContent = submitLabel || 'Save';
  $('modalForm').onsubmit = async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.target).entries());
    try {
      $('modalSubmit').disabled = true;
      await onSubmit(data);
      $('actionDialog').close();
      await refreshAll();
    } catch (err) {
      showToast(err.message, true);
    } finally { $('modalSubmit').disabled = false; }
  };
  $('actionDialog').showModal();
}


function modalHistoryDetail(id){
  const row = state.historyRows.find(r=>String(r.style_id)===String(id));
  if (!row) return showToast('Result not found',true);
  openModal('Style Result: '+id, 'Read-only result from the saved stock check.',
    `<div class="info-list" style="grid-column:1/-1"><div class="info-line"><span>Status</span><strong>${esc(row.status)}</strong></div><div class="info-line"><span>OOS sizes</span><strong>${esc(row.oos_sizes || '—')}</strong></div><div class="info-line"><span>Available sizes</span><strong>${esc(row.in_sizes || '—')}</strong></div><div class="info-line"><span>Checked at</span><strong>${dateFmt(row.checked_at)}</strong></div></div>`,
    'Close',async()=>{});
}

function modalBulkCreate(){
  openModal('Bulk Create Campaigns', 'One campaign per line: Campaign Name,Google Sheet URL. Do not include commas inside names.',
    `<label style="grid-column:1/-1">Campaigns (one per line)<textarea name="rows" rows="7" required placeholder="Stock checker,https://docs.google.com/spreadsheets/d/.../edit"></textarea></label>`,
    'Import Campaigns', async (d) => {
      const lines = String(d.rows || '').split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
      if (!lines.length || lines.length > 50) throw Error('Import between 1 and 50 campaigns');
      for (const line of lines) {
        const cut = line.indexOf(',');
        if (cut <= 0) throw Error('Expected name,Google Sheet URL: ' + line.slice(0,80));
        await post('/api/campaigns', {name:line.slice(0,cut).trim(), sheet_url:line.slice(cut+1).trim(),interval_minutes:60,mode:'browser'});
      }
      showToast(`${lines.length} campaigns imported`);
    });
}

function modalCampaignCreate(){
  openModal('Create Campaign', 'Connect a Google Sheet and choose how often to monitor it.',
    `<label>Campaign Name<input name="name" required></label>
     <label>Google Sheet URL<input name="sheet_url" type="url" required></label>
     <label>Frequency<select name="interval_minutes"><option value="60">1 hour</option><option value="120">2 hours</option><option value="360">6 hours</option><option value="1440">24 hours</option></select></label>
     <label>Checking Mode<select name="mode"><option value="browser">Browser</option><option value="auto">Auto</option><option value="fast">Fast</option></select></label>`,
    'Create', (d) => post('/api/campaigns', {...d, interval_minutes: Number(d.interval_minutes)}));
}
function modalCampaignEdit(id){
  const c = state.campaigns.find(x => x.id === id); if (!c) return;
  openModal('Edit Campaign', 'Update the campaign details and schedule.',
    `<label>Campaign Name<input name="name" value="${esc(c.name)}" required></label>
     <label>Google Sheet URL<input name="sheet_url" type="url" value="${sheetUrl(c)}" required></label>
     <label>Frequency<select name="interval_minutes"><option value="60" ${c.interval_minutes==60?'selected':''}>1 hour</option><option value="120" ${c.interval_minutes==120?'selected':''}>2 hours</option><option value="360" ${c.interval_minutes==360?'selected':''}>6 hours</option><option value="1440" ${c.interval_minutes==1440?'selected':''}>24 hours</option></select></label>
     <label>Checking Mode<select name="mode"><option value="browser" ${c.mode==='browser'?'selected':''}>Browser</option><option value="auto" ${c.mode==='auto'?'selected':''}>Auto</option><option value="fast" ${c.mode==='fast'?'selected':''}>Fast</option></select></label>`,
    'Save', (d) => put(`/api/campaigns/${id}`, {...d, interval_minutes: Number(d.interval_minutes)}));
}
function modalAddUser(){
  openModal('Add User', 'Create a new user account. Campaign access can be assigned afterwards.',
    `<label>Username<input name="username" required minlength="3"></label>
     <label>Temporary Password<input name="password" type="password" required minlength="12"></label>
     <label>Role<select name="role"><option value="employee">Employee</option><option value="manager">Manager</option><option value="admin">Admin</option></select></label>`,
    'Create User', (d) => post('/api/users', d));
}
function modalUserEdit(id){
  const u = state.users.find(x => x.id === id); if (!u) return;
  openModal('Edit User', 'Update the role or disable access.',
    `<label>Role<select name="role"><option value="employee" ${u.role==='employee'?'selected':''}>Employee</option><option value="manager" ${u.role==='manager'?'selected':''}>Manager</option><option value="admin" ${u.role==='admin'?'selected':''}>Admin</option></select></label>
     <label>Status<select name="active"><option value="1" ${u.active?'selected':''}>Active</option><option value="0" ${!u.active?'selected':''}>Disabled</option></select></label>`,
    'Save', (d) => put(`/api/users/${id}`, {role:d.role, active:Number(d.active)}));
}
function modalAssignMember(){
  const options = state.users.filter(u => u.active).map(u => `<option value="${u.id}">${esc(u.username)} (${esc(u.role)})</option>`).join('');
  if (!options) return showToast('Create an active user first.', true);
  openModal('Assign Member', 'This person will see the selected campaign.', `<label>Employee<select name="user_id">${options}</select></label>`, 'Assign', (d) => post(`/api/campaigns/${activeCampaign().id}/membership`, {user_id:Number(d.user_id), action:'assign'}));
}
async function beginMfa(){
  const pw = prompt('Enter your current password to begin authenticator setup');
  if (!pw) return;
  const res = await post('/api/mfa', {action:'begin', password: pw});
  openModal('Verify Authenticator', `Add this secret to your authenticator app: ${res.secret}`, `<label>6-digit code<input name="code" required pattern="[0-9]{6}" maxlength="6"></label>`, 'Enable MFA', (d) => post('/api/mfa', {action:'verify', code:d.code}));
}

function bindRootEvents(){
  document.addEventListener('click', async (e) => {
    const navBtn = e.target.closest('[data-nav]');
    if (navBtn) return navigate(navBtn.dataset.nav);
    const cmd = e.target.closest('[data-cmd]');
    if (cmd) return handleAction(cmd.dataset.cmd, cmd.dataset.id);
    const setTab = e.target.closest('[data-settingstab]');
    if (setTab) { state.settingsTab = setTab.dataset.settingstab; render(); return; }
    const filterBtn = e.target.closest('[data-filter]');
    if (filterBtn) { state.statusFilter = filterBtn.dataset.filter; render(); return; }
  });

  document.addEventListener('submit', async (e) => {
    const form = e.target;
    if (!['createCampaignForm','brandForm','settingsBrand','passwordForm','scheduleForm','inviteUserForm'].includes(form.id)) return;
    e.preventDefault();
    const data = Object.fromEntries(new FormData(form).entries());
    try {
      if (form.id === 'createCampaignForm') {
        await post('/api/campaigns', {...data, interval_minutes: Number(data.interval_minutes)});
        showToast('Campaign created. Click Sync to load your sheet.');
        form.reset();
      }
      if (form.id === 'brandForm' || form.id === 'settingsBrand') {
        state.workspace = await post('/api/workspace', data);
        if (form.id === 'brandForm') $('onboard').close();
        showToast('Brand settings saved');
      }
      if (form.id === 'passwordForm') {
        await post('/api/password', data); form.reset(); showToast('Password updated');
      }
      if (form.id === 'scheduleForm') {
        await post('/api/schedules', {campaign_id:Number(data.campaign_id), frequency_hours:Number(data.frequency_hours), auto_drive: !!form.querySelector('[name="auto_drive"]').checked});
        showToast('Schedule saved');
      }
      if (form.id === 'inviteUserForm') {
        const newUser = await post('/api/users', {username:data.username, password:data.password, role:data.role});
        if (data.campaign_id) await post(`/api/campaigns/${data.campaign_id}/membership`, {user_id:newUser.id, action:'assign'}).catch(()=>{});
        form.reset(); showToast('User created');
      }
      await refreshAll();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  document.addEventListener('input', (e) => {
    if (e.target.id === 'historySearch') { state.search = e.target.value.trim(); clearTimeout(window.__historyFilterTimer); window.__historyFilterTimer=setTimeout(()=>{ const el=$('historySearch'); const pos=el?.selectionStart||0; render(); $('historySearch')?.focus(); $('historySearch')?.setSelectionRange(pos,pos); },350); }
  });
  document.addEventListener('change', async (e) => {
    if (e.target.id === 'historyCampaignSelect') { state.selected = Number(e.target.value) || state.selected; await refreshPageData(); render(); }
    if (e.target.id === 'historyStatus') { state.statusFilter = e.target.value; render(); }
  });

  $('modalCancel').onclick = () => $('actionDialog').close();
  $('themeToggle').onclick = () => {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    localStorage.setItem('si_theme', next);
    applyTheme();
  };
  $('mobileMenu').onclick = () => $('sidebar').classList.toggle('open');
  $('logoutBtn').onclick = async () => { try { await post('/api/logout'); } catch {} window.location = '/login'; };
  $('globalCampaign').onchange = async (e) => { state.selected = Number(e.target.value) || state.selected; await refreshPageData(); render(); };
  $('globalSearch').onkeydown = async (e) => { if (e.key === 'Enter') { state.search = e.target.value.trim(); state.page = 'campaigns'; await refreshPageData(); renderNav(); render(); } };
  $('accountBtn').onclick = async () => { state.page = 'settings'; state.settingsTab = 'security'; await refreshPageData(); renderNav(); render(); };
  window.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); $('globalSearch').focus(); } });
}

async function initialize(){
  try {
    const me = await api('/api/me');
    state.user = me.user;
    state.csrf = me.csrf;
    if (!state.selected && state.campaigns[0]) state.selected = state.campaigns[0].id;
    await refreshAll();
    if (!state.workspace?.configured && state.user.role === 'admin') $('onboard').showModal();
  } catch (err) {
    showToast('Could not load dashboard: ' + err.message, true);
  }
}

bindRootEvents();
initialize();
setInterval(() => {
  if (document.visibilityState === 'visible' && !document.querySelector('dialog[open]')) refreshAll().catch(() => {});
}, 60000);
