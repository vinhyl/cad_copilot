// 首页：预览 + 选取（M1 目标：仅保留浏览态能力，工作能力通过动作卡片入口跳转）
//  - 预览：爆炸/X光/剖切/视角/复位（全局视图工具，不依赖选择）
//  - 选取：装配树 + 视口点选，层级感知上下文动作区
//  - 入口卡片：进入编辑会话、打开图纸对照、体检·分析报告
//  - 底部小格：插件状态（不再占整块）
//  - 草稿续作提示：如果 cacheKey 有未完成草稿，在信息条上放"继续未完成草稿"按钮

import '../style.css';
import { AssemblyScene } from '../scene.js';
import { AssemblyTree } from '../tree.js';
import { installThemeControls } from '../shared/theme.js';
import {
  getToken, setToken,
  parseAssembly, viewAssembly, listVersions, auditAssembly, importDrawing,
  getPlugins, getConfig, uploadFile, startFeaJob, startRenderJob,
  getJob, cancelJob, postSelection, listSessions, deleteSessions,
} from '../api.js';
import {
  consumeUrlBoot, ensureToken, loadAllowedDirs, pathDeniedMsg,
  bindStatus, pushRecent, bindDropOverlay, handleUpload, kindOfPath,
  goEdit, goDrawing, goReport, encodeScope, CAM_KEY,
  draftFor, getTabId, initWs, syncLoadParam, syncCacheKeyParam,
  initErrorTrap,
} from '../shared/utils.js';
import { setupJobCard } from '../shared/jobs.js';
import { PLUGIN_DEFS, refreshPlugins, renderPluginPanel } from '../shared/plugins.js';

const { bootLoadPath } = consumeUrlBoot();
initErrorTrap();
ensureToken();

const $ = (s) => document.querySelector(s);
const scene = new AssemblyScene(document.getElementById('viewport'));
installThemeControls([scene], document.getElementById('toolbar'));
const tree = new AssemblyTree(document.getElementById('tree'), {
  onSelect: (id) => selectNode(id),
  onToggle: (visMap) => scene.applyVisibility(visMap),
});
// 视口点选 = 完整选择（与在列表条目上点击等价）：树行高亮 + 模型高亮 + 上下文动作区
scene.onPick((id) => {
  tree.select(id);
  selectNode(id);
});

const statusFn = bindStatus($('#status'));

// ?flash= 一次性提示（编辑页确认落版本后跳回时携带），显示后从地址栏清除
let pendingFlash = null;
{
  const sp = new URLSearchParams(location.search);
  const flash = sp.get('flash');
  if (flash) {
    pendingFlash = flash;
    statusFn(flash);
    sp.delete('flash');
    history.replaceState(null, '',
      location.pathname + (sp.toString() ? `?${sp}` : ''));
  }
}
const allowedDirs = [];
loadAllowedDirs(getConfig).then((d) => allowedDirs.push(...d));

// M6：订阅服务端事件——agent 经 MCP 写草稿/落版本/生成报告后提示用户
initWs((ev) => {
  if (ev.cache_key && lastCacheKey && ev.cache_key !== lastCacheKey) return;
  if (ev.type === 'draft_saved') {
    refreshSessions();   // 刷新侧栏草稿徽章（含其他浏览器/agent 写入）
    if (ev.client === getTabId()) return;   // 自己保存的，忽略
    const from = ev.client === 'mcp-agent' ? '（来自 agent）' : '';
    statusFn(`草稿已更新：${ev.step_count} 步${from}，可从上下文动作区继续编辑`);
    renderContext();     // 续作按钮可能出现
  } else if (ev.type === 'draft_deleted') {
    refreshSessions();
    renderContext();     // 续作按钮应消失
  } else if (ev.type === 'version_changed') {
    refreshSessions();   // 版本徽章更新
    statusFn(`版本已切换到 ${ev.version}，重新打开文件可查看新几何`);
  } else if (ev.type === 'report_added') {
    statusFn('新报告已生成，可从「体检·分析报告」入口查看');
  }
});

// 选择状态（选择层级交互原则：装配树定层级）
let selectedId = null;
let moveMode = false;
let lastManifest = null;
let lastBaseUrl = null;
let lastCacheKey = null;
let lastLoadPath = null;   // 本次加载的输入路径（parse 通道才有真实路径；view 直载为 source_file 名）

/** 跳编辑页。编辑页「← 首页」的模型恢复走 cacheKey 直载通道
 * （encodeScope 已带 cacheKey），无需再传 load 路径。 */
function goEditWithLoad(params) {
  goEdit({ ...params });
}

// ---------- 任务卡片（共享） ----------
// 侧栏 tab：装配树 / 插件状态
document.querySelectorAll('.sb-tab').forEach((t) => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.sb-tab').forEach((x) => x.classList.remove('active'));
    t.classList.add('active');
    const tab = t.dataset.tab;
    document.querySelectorAll('.sb-tab-body').forEach((b) => {
      b.classList.toggle('hidden', b.dataset.body !== tab);
    });
  });
});

const jobs = setupJobCard({
  root: $('#job-card'),
  getJob, cancelJob,
  onDone: ({ kind, result }) => {
    if (kind === 'render' && result?.png_url) {
      const m = $('#render-modal');
      $('#render-view').innerHTML = `<img src="${result.png_url}" alt="render" />`;
      $('#render-meta').textContent = result.png_url;
      m.classList.remove('hidden');
    }
  },
  reportFn: (text, isError) => statusFn(text, isError),
});

$('#render-close').addEventListener('click', () => $('#render-modal').classList.add('hidden'));
$('#render-modal').addEventListener('click', (e) => {
  if (e.target.id === 'render-modal') $('#render-modal').classList.add('hidden');
});

// ---------- 选择 → 上下文动作区 ----------
function levelOf(rec) {
  if (!rec) return null;
  // 根节点：root 且没有 parentId
  const isRoot = !tree.nodes.get(rec.node.id)?.parentId;
  if (isRoot) return 'root';
  if (rec.node.type === 'part') return 'part';
  return 'assembly';
}

function templateOfPart(nodeId) {
  return scene.templateOf(nodeId);
}

function partIdsUnder(nodeId) {
  if (!nodeId) return new Set();
  return tree.partIdsUnder(nodeId);
}

function buildScope() {
  if (!selectedId) return null;
  const rec = tree.nodes.get(selectedId);
  const level = levelOf(rec);
  const scope = { cacheKey: lastCacheKey, level, nodeId: selectedId };
  if (level === 'part') {
    scope.templateId = templateOfPart(selectedId);
  }
  const pids = partIdsUnder(selectedId);
  scope.partIds = [...pids];
  return scope;
}

function renderContext() {
  const ctx = $('#ctx-actions');
  ctx.innerHTML = '';
  const rec = selectedId && tree.nodes.get(selectedId);
  const level = levelOf(rec);
  // 没有装配体加载：不显示
  if (!lastManifest) return;
  const hint = (() => {
    if (!selectedId) return '未选择节点：全局体检 · 渲染 · 版本入口';
    return `选中层级：${{root:'整装配体', assembly:'子装配体', part:'零件'}[level] || '节点'}`;
  })();
  const hl = document.createElement('div');
  hl.className = 'ctx-hint';
  hl.textContent = hint;
  ctx.appendChild(hl);

  const row = document.createElement('div');
  row.className = 'ctx-row';

  // 公共：体检（整装配体对全体，子装配体提示范围，零件范围）
  const auditBtn = document.createElement('button');
  auditBtn.className = 'ctx-btn';
  auditBtn.textContent = '体检';
  auditBtn.addEventListener('click', runAudit);
  row.appendChild(auditBtn);

  if (level === 'root' || !selectedId) {
    // 整装配体入口：渲染 · 版本入口
    const renderBtn = document.createElement('button');
    renderBtn.className = 'ctx-btn';
    renderBtn.textContent = '渲染';
    renderBtn.addEventListener('click', runRender);
    row.appendChild(renderBtn);

    const versBtn = document.createElement('button');
    versBtn.className = 'ctx-btn';
    versBtn.textContent = '版本';
    versBtn.addEventListener('click', showVersionsModal);
    row.appendChild(versBtn);

    const drawingBtn = document.createElement('button');
    drawingBtn.className = 'ctx-btn';
    drawingBtn.textContent = '图纸对照…';
    drawingBtn.addEventListener('click', () => {
      const params = lastCacheKey ? { cacheKey: lastCacheKey } : {};
      goDrawing(params);
    });
    row.appendChild(drawingBtn);
  }

  if (level === 'assembly') {
    // 子装配体：隔离
    const isoBtn = document.createElement('button');
    isoBtn.className = 'ctx-btn';
    isoBtn.textContent = '隔离子树';
    isoBtn.addEventListener('click', isolateSelected);
    row.appendChild(isoBtn);
  }

  if (level === 'part') {
    // 零件：进入编辑 / 力学分析 / 特征入口
    const editBtn = document.createElement('button');
    editBtn.className = 'ctx-btn primary';
    editBtn.textContent = '进入编辑…';
    editBtn.addEventListener('click', () => {
      const scope = buildScope();
      goEditWithLoad(encodeScope(scope));
    });
    row.appendChild(editBtn);

    const feaBtn = document.createElement('button');
    feaBtn.className = 'ctx-btn';
    feaBtn.textContent = '力学分析';
    feaBtn.addEventListener('click', () => runFea(selectedId));
    row.appendChild(feaBtn);

    // 特征摘要：只显示"查看特征"按钮，不再整块占侧栏（点击展开临时 modal）
    const featBtn = document.createElement('button');
    featBtn.className = 'ctx-btn';
    featBtn.textContent = '查看特征';
    featBtn.addEventListener('click', showFeatureModalForPart);
    row.appendChild(featBtn);
  }

  // 草稿续作：仅当前 cacheKey 有草稿时显示
  const draft = lastCacheKey ? draftInfoFor(lastCacheKey) : null;
  if (draft) {
    const resumeBtn = document.createElement('button');
    resumeBtn.className = 'ctx-btn resume';
    resumeBtn.textContent = `继续草稿（${draft.stepCount} 步 · 基线 ${draft.baselineVersion || 'v0'}）`;
    resumeBtn.addEventListener('click', () => {
      goEditWithLoad(encodeScope({ cacheKey: lastCacheKey, level: 'root', resume: '1' }));
    });
    row.appendChild(resumeBtn);
  }

  ctx.appendChild(row);
}

function showVersionsModal() {
  if (!lastCacheKey) return;
  listVersions(lastCacheKey).then((res) => {
    const m = $('#versions-modal');
    const list = $('#versions-body');
    list.innerHTML = '';
    const mk = (id, changelog, created, current) => {
      const r = document.createElement('div');
      r.className = `vp-row${current ? ' current' : ''}`;
      const n = document.createElement('span'); n.className = 'vp-name'; n.textContent = `${id}`;
      const d = document.createElement('span'); d.className = 'vp-desc';
      d.textContent = changelog + (created ? ` · ${created}` : '');
      const b = document.createElement('button');
      b.textContent = current ? '当前' : '切换';
      b.disabled = current;
      if (!current) b.addEventListener('click', async () => {
        const r2 = await checkout(lastCacheKey, id);
        lastManifest = r2.manifest;
        await scene.load(r2.manifest, lastBaseUrl);
        tree.render(r2.manifest.root);
        scene.highlight(null);
        selectedId = null;
        renderContext();
        statusFn(`已切换到 ${id}`);
      });
      r.append(n, d, b);
      list.append(r);
    };
    mk('v0', '基线（原始导入）', '', res.current === 'v0');
    [...res.versions].reverse().forEach((v) =>
      mk(v.id, v.changelog, v.created, v.id === res.current));
    m.classList.remove('hidden');
  }).catch((err) => statusFn(`版本获取失败：${err.message}`, true));
}
$('#versions-close').addEventListener('click', () => $('#versions-modal').classList.add('hidden'));
$('#versions-modal').addEventListener('click', (e) => {
  if (e.target.id === 'versions-modal') $('#versions-modal').classList.add('hidden');
});

async function checkout(cacheKey, version) {
  return _post('/api/versions/checkout', { cache_key: cacheKey, version });
}
async function _post(url, payload) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getToken()}` },
    body: JSON.stringify(payload),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

// ---------- 特征 modal（首页浏览，点选查看） ----------
function showFeatureModalForPart() {
  if (!selectedId) return;
  const rec = tree.nodes.get(selectedId);
  if (!rec || rec.node.type !== 'part') return;
  const tid = templateOfPart(selectedId);
  const tpl = lastManifest.templates.find((t) => t.id === tid);
  if (!tpl?.features) { statusFn('该零件没有特征数据', true); return; }
  const m = $('#feature-modal');
  const list = $('#feature-body');
  $('#feature-title').textContent = `${rec.node.name} · 模板 ${tid}`;
  list.innerHTML = '加载中…';
  fetch(`${lastBaseUrl}/${tpl.features}`)
    .then((r) => r.json())
    .then((feats) => {
      list.innerHTML = '';
      feats.forEach((f) => {
        const row = document.createElement('div'); row.className = 'fp-row';
        const dot = document.createElement('span'); dot.className = 'fp-dot'; dot.style.background = f.color;
        const name = document.createElement('span'); name.className = 'fp-name'; name.textContent = `${f.label} ${f.id}`;
        const dim = document.createElement('span'); dim.className = 'fp-dim';
        const r = (f.radii || []).map((x) => `R${x}`).join('/');
        dim.textContent = [f.axis, r, f.extent ? `L${f.extent}` : null].filter(Boolean).join(' ');
        row.append(dot, name, dim);
        row.addEventListener('click', () => {
          scene.showFeature(selectedId, f.id);
          uploadSelection({ nodeId: selectedId,
                            templateId: templateOfPart(selectedId),
                            featureId: f.id });
        });
        list.appendChild(row);
      });
      m.classList.remove('hidden');
    })
    .catch((err) => statusFn(`特征加载失败：${err.message}`, true));
}
$('#feature-close').addEventListener('click', () => {
  $('#feature-modal').classList.add('hidden');
  scene.showFeature(null, null);
});
$('#feature-modal').addEventListener('click', (e) => {
  if (e.target.id === 'feature-modal') $('#feature-modal').classList.add('hidden');
});

// ---------- 体检 modal（首页：报告中心入口，保留原 modal 形态直到 M6 报告中心） ----------
async function runAudit() {
  if (!lastCacheKey) { statusFn('先加载装配体', true); return; }
  statusFn('体检中（干涉 + DFM 规则）…');
  try {
    const rep = await auditAssembly(lastCacheKey);
    const body = $('#audit-body');
    body.innerHTML = '';
    const div = (cls, html) => { const el = document.createElement('div'); el.className = cls; el.innerHTML = html; return el; };
    if (rep.interference_count === 0 && rep.dfm_count === 0) {
      body.appendChild(div('audit-ok', '✓ 未发现问题：无干涉，DFM 规则全部通过'));
    } else {
      if (rep.interference_count > 0) {
        body.appendChild(div('audit-severity error', `干涉 ×${rep.interference_count}`));
        rep.interferences.forEach((h) => body.appendChild(
          div('audit-item', `${h.a.name} ↔ ${h.b.name}：穿透 ${h.volume_mm3} mm³`)));
      }
      if (rep.dfm_count > 0) {
        body.appendChild(div('audit-severity warning', `DFM 提示 ×${rep.dfm_count}`));
        rep.dfm.forEach((d) => body.appendChild(
          div('audit-item', `[${d.part}] ${d.detail}`)));
      }
    }
    $('#audit-modal').classList.remove('hidden');
    statusFn(`体检完成：干涉 ${rep.interference_count} · DFM ${rep.dfm_count}`);
  } catch (err) { statusFn(`体检失败：${err.message}`, true); }
}
$('#audit-close').addEventListener('click', () => $('#audit-modal').classList.add('hidden'));
$('#audit-modal').addEventListener('click', (e) => {
  if (e.target.id === 'audit-modal') $('#audit-modal').classList.add('hidden');
});

// ---------- 渲染（通过任务共享组件） ----------
let pluginsState = null;
refreshPlugins(getPlugins).then((s) => {
  pluginsState = s;
  renderPluginPanel($('#plugin-list'), s);
});
$('#plug-refresh').addEventListener('click', async () => {
  statusFn('插件重新探测中…');
  pluginsState = await refreshPlugins(getPlugins) || null;
  renderPluginPanel($('#plugin-list'), pluginsState);
});

async function runRender() {
  if (!lastCacheKey) { statusFn('先加载装配体', true); return; }
  if (pluginsState?.blender?.available === false) {
    statusFn(pluginsState.blender.hint || 'Blender 插件未安装', true); return;
  }
  try {
    const started = await startRenderJob(lastCacheKey);
    jobs.track(started, 'render', '离线渲染', {
      renderResultFn: (res) => {
        const body = { ok: 1 };  // 渲染结果已在 onDone 开 modal 显示
        return body;
      },
    });
  } catch (err) { statusFn(`提交失败：${err.message}`, true); }
}

async function runFea(partId) {
  if (!lastCacheKey) return;
  if (pluginsState?.fea?.available === false) {
    statusFn(pluginsState.fea.hint || 'FEA 插件未安装', true); return;
  }
  const rec = partId && tree.nodes.get(partId);
  if (!rec || rec.node.type !== 'part') {
    statusFn('先选择一个零件', true); return;
  }
  const tid = templateOfPart(partId);
  try {
    const started = await startFeaJob(lastCacheKey, tid);
    jobs.track(started, 'fea', `力学分析 ${rec.node.name}`, {
      renderResultFn: (res) => {
        const body = { ok: 1 };
        // 结果卡片直接展示在右下任务卡（jobs 自带 result 渲染由 main.js 负责时再迁移）
        // M1 先把结果显示在任务卡 result 区
        const box = $('#job-result');
        const mk = (label, val) => {
          const r = document.createElement('div'); r.className = 'jr-row';
          const k = document.createElement('span'); k.textContent = label;
          const v = document.createElement('span'); v.textContent = val;
          r.append(k, v); box.appendChild(r);
        };
        const fmt = (v, u) => v == null ? '—' : `${(+v).toFixed(4)} ${u}`;
        mk('最大位移', fmt(res.max_displacement_mm, 'mm'));
        mk('最大 von Mises', fmt(res.max_von_mises_MPa, 'MPa'));
        mk('网格', res.mesh_nodes != null ? `${res.mesh_nodes} 节点 / ${res.mesh_elements} 单元` : '—');
        if (res.cache_hit) mk('缓存', '命中');
        if (res.duration_s != null) mk('耗时', `${res.duration_s}s`);
        return body;
      },
    });
  } catch (err) { statusFn(`提交失败：${err.message}`, true); }
}

// ---------- 入口卡片（未选中零件态：编辑入口可点；点了就根据选中 scope 跳转） ----------
$('#entry-edit').addEventListener('click', () => {
  const scope = buildScope();
  if (!scope) {
    if (!lastCacheKey) { statusFn('先加载装配体', true); return; }
    statusFn('选择一个零件后再进入编辑，或选子装配体进入局部编辑', true);
    return;
  }
  if (scope.level === 'root' || scope.level === 'assembly') {
    // 没有零件时，用户想直接进编辑页：我们允许（编辑页内部引导选目标）
    goEditWithLoad(encodeScope(scope));
    return;
  }
  goEditWithLoad(encodeScope(scope));
});
$('#entry-drawing').addEventListener('click', () => {
  const params = lastCacheKey ? { cacheKey: lastCacheKey } : {};
  goDrawing(params);
});
$('#entry-report').addEventListener('click', () => {
  const params = lastCacheKey ? { cacheKey: lastCacheKey } : {};
  goReport(params);
});

// ---------- 装配选择逻辑 ----------
// M6：选中上行（fire-and-forget，失败静默——不阻塞点选交互）。
// agent 经 MCP get_user_selection 读到它，消解"这个零件/这个孔"。
function uploadSelection({ nodeId = null, templateId = null, featureId = null } = {}) {
  if (!lastCacheKey) return;
  postSelection({
    cache_key: lastCacheKey,
    node_id: nodeId,
    template_id: templateId,
    feature_id: featureId,
    source_file: lastManifest?.source_file || '',
    page: 'home',
    client: getTabId(),
  }).catch(() => {});
}

function selectNode(id) {
  selectedId = id;
  scene.highlight(tree.partIdsUnder(id));
  if (moveMode) { scene.enableMove(tree.partIdsUnder(id)); }
  const rec = tree.nodes.get(id);
  uploadSelection({
    nodeId: id,
    templateId: rec?.node?.type === 'part' ? (rec.node.template || null) : null,
  });
  renderContext();
}
function isolateSelected() {
  if (!selectedId) return;
  const keep = tree.partIdsUnder(selectedId);
  const vis = new Map();
  for (const [id, rec] of tree.nodes) {
    if (rec.node.type === 'part') vis.set(id, keep.has(id));
  }
  scene.applyVisibility(vis);
}

// ---------- 全局视图工具（不依赖选择） ----------
const explodeSlider = $('#explode-slider');
const explodeVal = $('#explode-val');
const sectionOn = $('#section-on');
const sectionSlider = $('#section-slider');
const btnXray = $('#btn-xray');
const btnIsolate = $('#btn-isolate');
const btnMove = $('#btn-move');

explodeSlider.addEventListener('input', () => {
  const ratio = explodeSlider.value / 100;
  explodeVal.textContent = `${explodeSlider.value}%`;
  scene.applyExplosion(ratio);
});
btnXray.addEventListener('click', () => {
  const on = !btnXray.classList.contains('active');
  btnXray.classList.toggle('active', on);
  scene.setXray(on);
});
btnIsolate.addEventListener('click', () => {
  if (!selectedId) { statusFn('先在装配树或视口中选择节点', true); return; }
  isolateSelected();
});
btnMove.addEventListener('click', () => {
  if (!selectedId) { statusFn('先在装配树或视口中选择节点', true); return; }
  moveMode = !moveMode;
  btnMove.classList.toggle('active', moveMode);
  if (moveMode) scene.enableMove(tree.partIdsUnder(selectedId));
  else scene.disableMove();
});
$('#btn-reset-moves').addEventListener('click', () => scene.resetTempMoves());
function sectionPos() {
  const b = scene.bbox;
  if (!b || b.isEmpty()) return 0;
  return b.min.z + (b.max.z - b.min.z) * (sectionSlider.value / 100);
}
function applySection() {
  scene.setSection(sectionOn.checked, sectionPos());
}
sectionOn.addEventListener('change', applySection);
sectionSlider.addEventListener('input', applySection);
$('#btn-cam-save').addEventListener('click', () => {
  localStorage.setItem(CAM_KEY, JSON.stringify(scene.getCameraState()));
  statusFn('视角已保存');
});
$('#btn-cam-restore').addEventListener('click', () => {
  const s = JSON.parse(localStorage.getItem(CAM_KEY) || 'null');
  if (s) scene.setCameraState(s);
  else statusFn('尚无已保存视角', true);
});
$('#btn-view-reset').addEventListener('click', () => {
  scene.applyExplosion(0);
  explodeSlider.value = 0; explodeVal.textContent = '0%';
  scene.setXray(false); btnXray.classList.remove('active');
  sectionOn.checked = false; applySection();
  moveMode = false; btnMove.classList.remove('active'); scene.disableMove();
  scene.resetTempMoves();
  scene.highlight(null);
  tree.render(lastManifest.root);
  scene.applyVisibility(tree.effectiveVisibility());
  scene._fitCamera();
  statusFn('视图已复位');
});

// ---------- 装配加载 + 图纸路由 ----------
const forceBox = $('#force');
const browseBtn = $('#btn-browse');

async function loadAssembly(p) {
  browseBtn.disabled = true;
  statusFn('解析中…');
  try {
    const res = await parseAssembly(p, forceBox.checked);
    await renderLoadedAssembly(res, p);
  } catch (err) {
    const denied = err.message.includes('outside allowed dirs');
    statusFn(denied ? pathDeniedMsg(allowedDirs) : `错误：${err.message}`, true);
  } finally {
    browseBtn.disabled = false;
  }
}

/** 按 cacheKey 直载缓存（GET /api/assembly/view，不读源文件）。
 * 回首页（编辑页「← 首页」）与最近列表点击走此通道——源文件移动或
 * 路径不在 allowed dirs 时 parse 必失败，但缓存仍完整可渲染。 */
async function loadAssemblyByKey(cacheKey) {
  browseBtn.disabled = true;
  statusFn('加载缓存…');
  try {
    const res = await viewAssembly(cacheKey);
    await renderLoadedAssembly(res, null);
  } catch (err) {
    statusFn(`错误：${err.message}`, true);
  } finally {
    browseBtn.disabled = false;
  }
}

/** 渲染已取得的装配数据（parse 与 view 共用）。
 * loadPath 非空时写回地址栏 ?load=；为 null（view 直载）时写
 * ?cacheKey=，下次打开仍走缓存通道。 */
async function renderLoadedAssembly(res, loadPath) {
  lastManifest = res.manifest;
  lastBaseUrl = res.base_url;
  lastCacheKey = res.cache_key;
  lastLoadPath = loadPath || res.manifest.source_file || '';
  const count = await scene.load(res.manifest, res.base_url);
  tree.render(res.manifest.root);
  scene.highlight(null);
  selectedId = null;
  explodeSlider.value = 0; explodeVal.textContent = '0%';
  scene.applyExplosion(0);
  applySection();
  // 简要信息条
  const src = res.manifest.source_file;
  const tpls = res.manifest.templates.length;
  const draft = draftInfoFor(lastCacheKey);
  $('#info-summary').innerHTML = '';
  const line = document.createElement('span');
  line.textContent = `${src} · ${tpls} 模板 · ${count} 实例 · ${res.cache_hit ? '缓存命中' : '新建缓存'}`;
  $('#info-summary').appendChild(line);
  statusFn(`${pendingFlash ? `${pendingFlash}；` : ''}已加载：${src} · ${tpls} 模板 · ${count} 实例`);
  pendingFlash = null;
  if (draft?.stepCount > 0) {
    statusFn(`有未完成草稿（${draft.stepCount} 步），可从上下文动作区继续`);
  }
  renderContext();
  if (loadPath) {
    pushRecent(loadPath, 'assembly');
    syncLoadParam(loadPath);
  } else {
    syncCacheKeyParam(res.cache_key);
  }
}

async function importDrawingFile(p) {
  syncLoadParam(p);   // 图纸路径同样写回，跳转后 drawing 页消费完清除
  goDrawing({ path: encodeURIComponent(p) });
}

function routeByExtension(p) {
  const kind = kindOfPath(p);
  if (kind === 'drawing') importDrawingFile(p);
  else loadAssembly(p);
}

// 最近使用（M6 升级：服务端会话列表为主——跨浏览器可见；图纸仍走
// localStorage 历史，因为图纸没有服务端会话）
let sessionsCache = [];   // [{cache_key, source_file, current_version, draft_steps, updated}]

/** 草稿信息：优先服务端会话数据（跨浏览器），localStorage 索引作 fallback。 */
function draftInfoFor(cacheKey) {
  const s = sessionsCache.find((x) => x.cache_key === cacheKey);
  if (s && s.draft_steps > 0) {
    return { stepCount: s.draft_steps, baselineVersion: s.draft_baseline_version };
  }
  return draftFor(cacheKey);   // 会话列表尚未加载/失败时的兜底
}

async function refreshSessions() {
  try {
    const { sessions } = await listSessions();
    sessionsCache = sessions || [];
  } catch { sessionsCache = []; }
  renderRecent();
}

function renderRecent() {
  const listEl = $('#recent-list');
  const panel = $('#recent-panel');
  // 装配行来自服务端（新→旧）；图纸行来自本浏览器历史
  let drawings = [];
  try {
    const all = JSON.parse(localStorage.getItem('cad_recent_files')) || [];
    drawings = all.filter((r) => r.kind === 'drawing');
  } catch {}
  panel.classList.toggle('hidden', !sessionsCache.length && !drawings.length);
  listEl.innerHTML = '';
  sessionsCache.forEach((s) => {
    const row = document.createElement('div'); row.className = 'recent-row';
    row.title = `${s.source_file}（${s.updated}）`;
    const badge = document.createElement('span');
    badge.className = 'recent-badge assembly';
    badge.textContent = '装配';
    const name = document.createElement('span');
    name.className = 'recent-name'; name.textContent = s.source_file;
    row.append(badge, name);
    if (s.draft_steps > 0) {
      const draft = document.createElement('span');
      draft.className = 'recent-badge draft';
      draft.textContent = `草稿 ${s.draft_steps} 步`;
      row.appendChild(draft);
      // 查看草稿效果入口：直达编辑页基线 vs 草稿双视口（编辑页会自动恢复
      // 并 preview 该草稿）。不区分来源——AI 经 MCP 写草稿 / 网页手动存草稿
      // 都走同一判定（draft_steps>0）。
      const preview = document.createElement('button');
      preview.type = 'button';
      preview.className = 'recent-badge cta';
      preview.textContent = '预览草稿';
      preview.title = '查看编辑效果：基线 vs 草稿 双视口';
      preview.addEventListener('click', (e) => {
        e.stopPropagation();   // 阻止行自身的「加载装配」行为
        goEditWithLoad(encodeScope({ cacheKey: s.cache_key, level: 'root' }));
      });
      row.appendChild(preview);
    }
    const ver = document.createElement('span');
    ver.className = 'recent-badge';
    ver.textContent = s.current_version || 'v0';
    row.appendChild(ver);
    row.addEventListener('click', () => {
      // view 直载：源文件可能已移动/删除或不在 allowed dirs，
      // cacheKey 通道不读源文件，缓存仍在即可渲染
      loadAssemblyByKey(s.cache_key);
    });
    listEl.append(row);
  });
  drawings.forEach((r) => {
    const row = document.createElement('div'); row.className = 'recent-row'; row.title = r.path;
    const badge = document.createElement('span');
    badge.className = 'recent-badge drawing';
    badge.textContent = '图纸';
    const name = document.createElement('span');
    name.className = 'recent-name'; name.textContent = r.path;
    row.append(badge, name);
    row.addEventListener('click', () => routeByExtension(r.path));
    listEl.append(row);
  });
}
$('#recent-clear').addEventListener('click', async () => {
  // 图纸历史走本浏览器 localStorage；装配会话在服务端，调接口隐藏
  // （仅记隐藏清单、保留渲染缓存，与 DWG"清指针留文件"语义一致）
  localStorage.setItem('cad_recent_files', JSON.stringify([]));
  try {
    await deleteSessions();   // 不带 cache_key = 隐藏全部装配会话
  } catch (e) {
    console.warn('清空装配会话失败（服务端）：', e);
  }
  await refreshSessions();   // 重拉服务端会话列表并重渲染
});
refreshSessions();

async function handleFile(file) {
  statusFn(`上传中… ${file.name}`);
  try {
    const path = await handleUpload(uploadFile, file);
    routeByExtension(path);
  } catch (err) { statusFn(`上传失败：${err.message}`, true); }
}

$('#btn-browse').addEventListener('click', () => $('#file-input').click());
$('#file-input').addEventListener('change', (e) => {
  const f = e.target.files?.[0];
  e.target.value = '';
  if (f) handleFile(f);
});

bindDropOverlay($('#drop-overlay'), handleFile);

// 焦点回链：?focus=partId 从报告中心回首页定位零件
(function applyFocusOnLoad() {
  const sp = new URLSearchParams(location.search);
  const focus = sp.get('focus');
  if (focus) {
    // 监听首次树渲染完成后选择目标
    const origRender = tree.render.bind(tree);
    tree.render = function(root) {
      origRender(root);
      const tick = setInterval(() => {
        const found = tree.nodes.get(focus);
        if (found) {
          tree.select(focus);
          selectNode(focus);
          clearInterval(tick);
        }
      }, 40);
      setTimeout(() => clearInterval(tick), 2000);
    };
  }
})();

// 首页 URL 引导：优先 ?cacheKey=（view 直载，不依赖源文件），
// 否则 ?load=<path>（parse，需要源文件在 allowed dirs 内）
{
  const sp = new URLSearchParams(location.search);
  const bootCacheKey = sp.get('cacheKey');
  if (bootCacheKey) loadAssemblyByKey(bootCacheKey);
  else if (bootLoadPath) routeByExtension(bootLoadPath);
}
renderContext();
