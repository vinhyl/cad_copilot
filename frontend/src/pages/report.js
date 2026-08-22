// 报告中心独立页（M4）：
//  - 左栏：历史报告列表（时间倒序，含摘要徽标）
//  - 右栏：报告详情（摘要卡 + 版本历史 + 干涉明细 + DFM 明细）
//  - 生成报告：对当前装配体做快照（体检 + 统计 + 版本链），落盘后自动选中
//  - 干涉条目可点击回首页定位零件（?load=源文件&focus=节点 id）
//  URL 约定：report.html?cacheKey=<ck>[&report_id=<id>][&source=<path>]

import '../style.css';
import {
  generateReport, listReports, getReport,
} from '../api.js';
import {
  consumeUrlBoot, ensureToken, bindStatus, goHome, initWs, initErrorTrap,
} from '../shared/utils.js';

const { bootLoadPath } = consumeUrlBoot();
initErrorTrap();
ensureToken();

const $ = (s) => document.querySelector(s);
const statusFn = bindStatus($('#report-status'));

// M6：订阅服务端事件——agent 经 MCP generate_report 生成报告后自动刷新列表
initWs((ev) => {
  if (!cacheKey || ev.cache_key !== cacheKey) return;
  if (ev.type === 'report_added') {
    refreshList(ev.report_id || null);
    statusFn('新报告已生成（远程），列表已刷新');
  }
});

const sp = new URLSearchParams(location.search);
const cacheKey = sp.get('cacheKey');
let sourceFile = sp.get('source') || '';
let currentReportId = sp.get('report_id') || null;

$('#nav-home').addEventListener('click', goHome);

if (!cacheKey) {
  $('#btn-generate').disabled = true;
  statusFn('未指定装配体：请从首页进入报告中心', true);
}

function fmtNum(v, unit) {
  if (v == null) return '—';
  const n = +v;
  if (Math.abs(n) >= 1000) return `${n.toFixed(0)} ${unit}`;
  return `${n.toFixed(2)} ${unit}`;
}

// ---------- 报告列表 ----------
async function refreshList(selectId = null) {
  if (!cacheKey) return;
  const box = $('#report-list');
  box.innerHTML = '';
  try {
    const { reports } = await listReports(cacheKey);
    if (!reports.length) {
      const ph = document.createElement('div');
      ph.className = 'placeholder';
      ph.textContent = '尚无报告：点击右上「生成报告」';
      box.appendChild(ph);
      return;
    }
    reports.forEach((r) => {
      const row = document.createElement('button');
      row.className = 'report-item';
      if (r.report_id === (selectId || currentReportId)) {
        row.classList.add('active');
      }
      const head = document.createElement('div');
      head.className = 'ri-head';
      const t = document.createElement('span');
      t.className = 'ri-time';
      t.textContent = r.created;
      const v = document.createElement('span');
      v.className = 'ri-version';
      v.textContent = r.version || 'v0';
      head.append(t, v);
      const badges = document.createElement('div');
      badges.className = 'ri-badges';
      const s = r.summary || {};
      const mk = (cls, text, title) => {
        const b = document.createElement('span');
        b.className = `badge ${cls}`;
        b.textContent = text;
        b.title = title;
        return b;
      };
      if (s.interferences > 0) badges.appendChild(
        mk('err', `干涉 ${s.interferences}`, '静态干涉'));
      if (s.dfm_warnings > 0) badges.appendChild(
        mk('warn', `DFM ${s.dfm_warnings}`, 'DFM 警告'));
      if (s.dfm_infos > 0) badges.appendChild(
        mk('info', `提示 ${s.dfm_infos}`, 'DFM 提示'));
      if (!badges.children.length) badges.appendChild(mk('ok', '✓ 通过', '无问题'));
      const meta = document.createElement('div');
      meta.className = 'ri-meta';
      meta.textContent = `${s.templates ?? '—'} 模板 · ${s.instances ?? '—'} 实例`;
      row.append(head, badges, meta);
      row.addEventListener('click', () => {
        currentReportId = r.report_id;
        document.querySelectorAll('.report-item').forEach((x) =>
          x.classList.remove('active'));
        row.classList.add('active');
        showReport(r.report_id);
      });
      box.appendChild(row);
    });
    // URL 带 report_id 或默认选最新
    const want = selectId || currentReportId;
    if (want && reports.some((r) => r.report_id === want)) {
      showReport(want);
    } else if (!currentReportId) {
      currentReportId = reports[0].report_id;
      showReport(currentReportId);
    }
  } catch (err) {
    statusFn(`报告列表加载失败：${err.message}`, true);
  }
}

// ---------- 报告详情 ----------
let lastReport = null;

async function showReport(reportId) {
  const detail = $('#report-detail');
  detail.innerHTML = '';
  statusFn('加载报告…');
  try {
    const rep = await getReport(cacheKey, reportId);
    lastReport = rep;
    if (rep.source_file) sourceFile = rep.source_file;
    $('#report-meta').textContent =
      `${rep.source_file || cacheKey} · 版本 ${rep.version}`;
    renderDetail(detail, rep);
    statusFn(`报告 ${rep.report_id} · ${rep.created}`);
  } catch (err) {
    statusFn(`报告读取失败：${err.message}`, true);
  }
}

function renderDetail(detail, rep) {
  const s = rep.summary || {};
  const st = rep.assembly_stats || {};

  // 摘要卡
  const summary = document.createElement('div');
  summary.className = 'rp-card';
  const title = document.createElement('div');
  title.className = 'rp-card-title';
  title.textContent = `快照 ${rep.report_id} · ${rep.created} · 基线 ${rep.version}`;
  summary.appendChild(title);
  const grid = document.createElement('div');
  grid.className = 'rp-stat-grid';
  const stat = (label, value, cls = '') => {
    const cell = document.createElement('div');
    cell.className = `rp-stat ${cls}`.trim();
    const k = document.createElement('div'); k.className = 'rp-stat-k';
    k.textContent = label;
    const v = document.createElement('div'); v.className = 'rp-stat-v';
    v.textContent = value;
    cell.append(k, v);
    return cell;
  };
  grid.appendChild(stat('模板', s.templates ?? '—'));
  grid.appendChild(stat('实例', s.instances ?? '—'));
  grid.appendChild(stat('干涉', s.interferences ?? 0,
    s.interferences > 0 ? 'bad' : 'good'));
  grid.appendChild(stat('DFM 警告', s.dfm_warnings ?? 0,
    s.dfm_warnings > 0 ? 'warn' : 'good'));
  grid.appendChild(stat('DFM 提示', s.dfm_infos ?? 0,
    s.dfm_infos > 0 ? 'warn' : 'good'));
  grid.appendChild(stat('总体积', fmtNum(st.total_volume_mm3, 'mm³')));
  grid.appendChild(stat('总表面积', fmtNum(st.total_area_mm2, 'mm²')));
  summary.appendChild(grid);
  detail.appendChild(summary);

  // 干涉明细
  const interCard = document.createElement('div');
  interCard.className = 'rp-card';
  const iTitle = document.createElement('div');
  iTitle.className = 'rp-card-title';
  iTitle.textContent = `干涉明细${rep.interferences?.length ? `（${rep.interferences.length}）` : ''}`;
  interCard.appendChild(iTitle);
  if (rep.interferences?.length) {
    rep.interferences.forEach((h) => {
      const row = document.createElement('div');
      row.className = 'rp-row err clickable';
      row.title = '点击回首页定位该零件';
      row.textContent = `${h.a.name} ↔ ${h.b.name}：穿透 ${h.volume_mm3} mm³`;
      row.addEventListener('click', () => locatePart(h.a.id));
      interCard.appendChild(row);
    });
  } else {
    const ok = document.createElement('div');
    ok.className = 'rp-ok';
    ok.textContent = '✓ 无静态干涉';
    interCard.appendChild(ok);
  }
  detail.appendChild(interCard);

  // DFM 明细
  const dfmCard = document.createElement('div');
  dfmCard.className = 'rp-card';
  const dTitle = document.createElement('div');
  dTitle.className = 'rp-card-title';
  dTitle.textContent = `DFM 审查${rep.dfm?.length ? `（${rep.dfm.length}）` : ''}`;
  dfmCard.appendChild(dTitle);
  if (rep.dfm?.length) {
    rep.dfm.forEach((d) => {
      const row = document.createElement('div');
      row.className = `rp-row ${d.severity === 'warning' ? 'warn' : 'info'}`;
      row.textContent = `[${d.part}] ${d.detail}`;
      dfmCard.appendChild(row);
    });
  } else {
    const ok = document.createElement('div');
    ok.className = 'rp-ok';
    ok.textContent = '✓ DFM 规则全部通过';
    dfmCard.appendChild(ok);
  }
  detail.appendChild(dfmCard);

  // 版本历史
  const vCard = document.createElement('div');
  vCard.className = 'rp-card';
  const vTitle = document.createElement('div');
  vTitle.className = 'rp-card-title';
  vTitle.textContent = '版本历史';
  vCard.appendChild(vTitle);
  const v0 = document.createElement('div');
  v0.className = `rp-row version${rep.version === 'v0' ? ' current' : ''}`;
  const v0n = document.createElement('span'); v0n.textContent = 'v0';
  const v0d = document.createElement('span');
  v0d.textContent = '基线（原始导入）';
  const v0c = document.createElement('span'); v0c.textContent = '';
  v0.append(v0n, v0d, v0c);
  vCard.appendChild(v0);
  (rep.versions || []).forEach((v) => {
    const row = document.createElement('div');
    row.className = `rp-row version${v.id === rep.version ? ' current' : ''}`;
    const n = document.createElement('span'); n.textContent = v.id;
    const d = document.createElement('span');
    d.textContent = (v.changelog || '').split('\n')[0];
    d.title = v.changelog || '';
    const c = document.createElement('span'); c.textContent = v.created || '';
    row.append(n, d, c);
    vCard.appendChild(row);
  });
  detail.appendChild(vCard);
}

// 干涉条目 → 回首页定位零件（首页支持 ?load= + ?focus=）
function locatePart(nodeId) {
  if (!nodeId) return;
  const params = new URLSearchParams();
  if (sourceFile) params.set('load', sourceFile);
  params.set('focus', nodeId);
  const base = (import.meta.env.BASE_URL || '/app/').replace(/\/$/, '');
  location.href = `${base}/index.html?${params.toString()}`;
}

// ---------- 生成报告 ----------
$('#btn-generate').addEventListener('click', async () => {
  if (!cacheKey) { statusFn('未指定装配体', true); return; }
  $('#btn-generate').disabled = true;
  statusFn('生成中（体检 + 统计 + 版本历史）…');
  try {
    const rep = await generateReport(cacheKey);
    currentReportId = rep.report_id;
    if (rep.source_file) sourceFile = rep.source_file;
    await refreshList(rep.report_id);
    statusFn(`已生成 ${rep.report_id}：干涉 ${rep.summary.interferences} · `
      + `DFM ${rep.summary.dfm_warnings + rep.summary.dfm_infos}`);
  } catch (err) {
    statusFn(`生成失败：${err.message}`, true);
  } finally {
    $('#btn-generate').disabled = false;
  }
});

// ---------- 初始化 ----------
if (cacheKey) refreshList();
// 首页跳转可能带 ?load=（与报告无关，仅透传源文件路径兜底）
if (!sourceFile && bootLoadPath) sourceFile = bootLoadPath;
