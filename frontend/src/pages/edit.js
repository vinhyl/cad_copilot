// 编辑会话页（M2：草稿步骤表驱动草稿几何 + 增量干涉自动检查）
//
//  - 顶部会话条：基线 + 步骤数 + 放弃/保存草稿/确认保存
//  - 左栏：步骤列表（声明式，可增删 · 多目标）
//  - 中栏：双视口（基线锁定 + 草稿实时）+ 布局预设 + 预览范围
//  - 右栏：验证轨道（增量干涉 · 差异占位）
//  - 窄屏：底部抽屉

import '../style.css';
import { AssemblyScene } from '../scene.js';
import { AssemblyTree } from '../tree.js';
import {
  getToken, setToken,
  listVersions, checkoutVersion,
  loadDraft, saveDraft, deleteDraft, previewDraft, confirmDraft,
  startFeaCompare, getJob, cancelJob, getPlugins, postSelection,
} from '../api.js';
import {
  consumeUrlBoot, ensureToken, bindStatus,
  goHome, goDrawing,
  readScopeFromUrl,
  LAYOUT_PRESETS, loadLayoutPreset, saveLayoutPreset,
  defaultLayoutForWidth,
  loadSidebarCollapsed, saveSidebarCollapsed,
  saveDraftIndexEntry, deleteDraftIndexEntry,
  getTabId, initWs, initErrorTrap,
} from '../shared/utils.js';

const { bootLoadPath } = consumeUrlBoot();
initErrorTrap();
ensureToken();

const $ = (s) => document.querySelector(s);
const scope = readScopeFromUrl();
const statusFn = bindStatus($('#sess-status'));
const state = {
  cacheKey: scope.cacheKey,
  level: scope.level,
  nodeId: scope.nodeId,
  templateId: scope.templateId,
  baseline: null,           // { version, manifest, baseUrl }
  baselineLoaded: false,
  draftSteps: [],           // 声明式步骤表：[{id, template_id, operation, params, feature_id?}]
  dirty: false,             // 步骤变更未保存（回首页/关页守卫）
  layout: loadLayoutPreset(),
  previewInFlight: false,
  previewTimer: null,
  lastPreview: null,         // { manifest, interferences, check_level, edited_templates }
  checkLevel: null,          // 当前检查级别：'bbox'（粗筛）| 'exact'（精检）
  // M3：视口焦点（点选更新）+ 预览范围 + 操作表单目标
  focus: { level: scope.level || 'root', nodeId: scope.nodeId || null },
  range: 'root',
  targetTemplateId: scope.templateId || null,
  featuresCache: new Map(),  // tid -> 特征列表（来自 /cache/<ck>/features/tN.json）
  // M5：FEA 对比
  feaAvailable: null,        // 插件探测结果（null = 未探测）
  feaJob: null,              // { id, timer }
  feaResult: null,           // 最近一次对比结果
  feaStale: false,           // 步骤变更后已有结果过期
  // M6.5：移动模式（仅草稿视口）
  moveMode: false,
  // 精检结果跨刷新保留（逐处处理干涉时不因步骤改动即丢失）
  exactResult: null,   // 最近一次精检结果（保持显示，直到手动重置/重新精检）
  verifyStale: false,  // 步骤改动后，已存精检结果对未来几何已过期
};

// ==========================================================================
// 基线加载：进入会话即锁基线
// ==========================================================================
async function loadBaseline() {
  if (!state.cacheKey) {
    $('#sess-status').textContent = '未接收到装配体，请回首页打开文件后再进入编辑';
    return;
  }
  statusFn('加载基线版本…');
  let manifest = null, baseUrl = null, curVersion = 'v0';
  try {
    const vers = await listVersions(state.cacheKey);
    curVersion = vers?.current || 'v0';
    const ck = await checkoutVersion(state.cacheKey, curVersion);
    manifest = ck.manifest;
    // checkout 不返回 base_url：模板 gltf 为版本绝对路径或 cache 相对路径，
    // 相对路径一律相对 /cache/<ck>（模板未改过版本时）
    baseUrl = ck.base_url || `/cache/${state.cacheKey}`;
    if (!manifest) throw new Error('checkout 返回内容中没有 manifest');
  } catch (err) {
    statusFn(`加载失败：${err.message}`, true);
    return;
  }
  state.baseline = { manifest, baseUrl, version: curVersion };
  await sceneBaseline.load(manifest, baseUrl);
  treeBaseline.render(manifest.root);
  state.baselineLoaded = true;
  // 草稿场景：基线作为初值，后续 preview 会刷新为草稿几何
  await sceneDraft.load(manifest, baseUrl);
  treeDraft.render(manifest.root);
  initStepForm();
  updateRangeButtons();
  reapplyViewFilter();
  updateSessionHeader();

  // 加载草稿（单槽位）
  try {
    const d = await loadDraft(state.cacheKey);
    if (d && !d.empty && Array.isArray(d.steps) && d.steps.length) {
      state.draftSteps = d.steps.map(normalizeStep);
      state.dirty = false;   // 与服务端一致
      statusFn(`已恢复草稿（${state.draftSteps.length} 步，基线 ${d.baseline_version || curVersion}）`);
      renderDraftSteps();
      // 优先恢复上次精检结果（步序未变）；有则跳过本次自动 bbox 预览，
      // 无则照常自动触发一次 preview 把草稿几何 + 干涉刷出来
      if (!tryRestoreExact()) schedulePreview();
    } else {
      renderDraftSteps();
      statusFn(`已锁定基线 ${curVersion}：${manifest.source_file}`);
    }
  } catch (err) {
    renderDraftSteps();
    statusFn(`草稿加载失败（忽略）：${err.message}`);
  }
}

// ==========================================================================
// 双视口：基线 + 草稿各一个场景
// 相机联动见下方 M6.5 syncCameras（带视角同步开关 + 回声守卫）
// ==========================================================================
const sceneBaseline = new AssemblyScene(document.getElementById('vp-base'));
const sceneDraft    = new AssemblyScene(document.getElementById('vp-draft'));
const treeBaseline = new AssemblyTree(document.getElementById('tree-base'), {
  onSelect: () => {}, onToggle: () => {},
});
const treeDraft    = new AssemblyTree(document.getElementById('tree-draft'), {
  onSelect: () => {}, onToggle: () => {},
});

// ==========================================================================
// 布局预设（四种任意宽度可选）
// ==========================================================================
const wsEl = $('#edit-ws');
function applyLayout(preset) {
  wsEl.classList.remove('layout-split-h','layout-split-v','layout-ab-switch','layout-overlay');
  wsEl.classList.add(`layout-${preset}`);
  if (preset === 'ab-switch') wsEl.dataset.activePane = 'draft';
  document.querySelectorAll('.layout-btn').forEach((b) => {
    b.classList.toggle('active', b.dataset.layout === preset);
  });
  state.layout = preset;
  saveLayoutPreset(preset);
}
document.querySelectorAll('.layout-btn').forEach((b) => {
  b.addEventListener('click', () => applyLayout(b.dataset.layout));
});
const abToggle = document.getElementById('ab-toggle');
if (abToggle) {
  abToggle.addEventListener('click', () => {
    const cur = wsEl.dataset.activePane || 'draft';
    const next = cur === 'draft' ? 'base' : 'draft';
    wsEl.dataset.activePane = next;
    abToggle.textContent = next === 'draft'
      ? 'A/B：当前为草稿，切到基线'
      : 'A/B：当前为基线，切到草稿';
  });
}

// 侧栏折叠：中栏「侧栏」按钮常驻（rail-toggle 在左栏头部，折叠后随
// 侧栏一起 display:none，曾导致折叠后无入口展开且状态已持久化 → 死局）
function applySidebarCollapsed(collapsed) {
  wsEl.classList.toggle('rail-collapsed', collapsed);
  saveSidebarCollapsed(collapsed);
  document.getElementById('rail-toggle-2')?.classList.toggle('active', !collapsed);
}
applySidebarCollapsed(loadSidebarCollapsed());
document.getElementById('rail-toggle')?.addEventListener('click', () => {
  applySidebarCollapsed(!wsEl.classList.contains('rail-collapsed'));
});
document.getElementById('rail-toggle-2')?.addEventListener('click', () => {
  applySidebarCollapsed(!wsEl.classList.contains('rail-collapsed'));
});

// 窄屏底部抽屉
document.querySelectorAll('.drawer-tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.drawer-tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    const target = tab.dataset.drawer;
    document.querySelectorAll('.drawer-pane').forEach((p) => p.classList.remove('active'));
    const pane = document.querySelector(`.drawer-pane[data-drawer="${target}"]`);
    if (pane) {
      pane.classList.add('active');
      // 步骤抽屉：把当前步骤列表镜像到抽屉里
      if (target === 'steps') renderDraftSteps($('#drawer-steps'));
      if (target === 'verify') renderVerifyPane($('#drawer-verify'));
    }
  });
});

// 窄屏：操作表单移入抽屉（左栏整体隐藏）；宽屏：回到左栏
const narrowMq = window.matchMedia('(max-width: 900px)');
const stepFormHome = $('#step-form').parentElement;
function placeFormByWidth() {
  const form = $('#step-form');
  const slot = $('#drawer-steps-form');
  if (!slot) return;
  if (narrowMq.matches && form.parentElement !== slot) slot.appendChild(form);
  else if (!narrowMq.matches && form.parentElement !== stepFormHome) stepFormHome.appendChild(form);
}
narrowMq.addEventListener?.('change', placeFormByWidth);
placeFormByWidth();

// ==========================================================================
// 会话条
// ==========================================================================
function updateSessionHeader() {
  const t = $('#sess-meta');
  const v = state.baseline?.version || '—';
  const n = state.draftSteps.length;
  const src = state.baseline?.manifest?.source_file || state.cacheKey || '—';
  const tpl = state.templateId ? ` · 目标模板 ${state.templateId}` : '';
  t.textContent = `基线 ${v} · 草稿 ${n} 步 · ${src}${tpl}`;
}

// ==========================================================================
// 步骤表操作
// ==========================================================================
let stepSeq = 0;

function normalizeStep(s) {
  // 确保字段齐全（草稿文件可能缺字段）
  return {
    id: s.id || `s${Date.now()}_${++stepSeq}`,
    template_id: s.template_id || s.tpl || null,
    operation: s.operation || s.op,
    params: s.params || {},
    feature_id: s.feature_id ?? null,
    node_id: s.node_id ?? null,   // M6.5 move 步骤：实例级寻址
    title: s.title || '',
  };
}

/** 前端兜底标题（后端 draft_step_title 的镜像，move 用节点名）。 */
function stepFallbackTitle(s) {
  if (s.operation === 'move') {
    const p = s.params || {};
    const rec = treeBaseline.nodes.get(s.node_id);
    const name = rec?.node?.name || s.node_id;
    return `${name}: move Δ(${p.dx ?? 0},${p.dy ?? 0},${p.dz ?? 0}) mm`;
  }
  return `${s.template_id}: ${s.operation}`;
}

/** M6.5：把草稿视口的一次拖拽位移折算进步骤表。
 * 语义：每节点一条 move 步骤，params = 相对基线的总位移（后写覆盖，
 * 与后端 moves_from_steps 一致）。temp 视觉位移由 preview 重放的
 * manifest 矩阵接管，故此处必须清零 temp 避免双重偏移。 */
function commitTempMovesToSteps() {
  const temps = sceneDraft.getTempMoves();
  sceneDraft.resetTempMoves();
  const touched = [];
  for (const [nodeId, d] of Object.entries(temps)) {
    const prev = state.draftSteps.find(
      (s) => s.operation === 'move' && s.node_id === nodeId);
    const base = prev ? prev.params : { dx: 0, dy: 0, dz: 0 };
    const total = {
      dx: +(base.dx + d[0]).toFixed(3),
      dy: +(base.dy + d[1]).toFixed(3),
      dz: +(base.dz + d[2]).toFixed(3),
    };
    const tid = sceneDraft.templateOf(nodeId);
    if (prev) {
      prev.params = total;
      prev.title = '';
    } else {
      state.draftSteps.push({
        id: `s${Date.now()}_${++stepSeq}`,
        template_id: tid,
        node_id: nodeId,
        operation: 'move',
        params: total,
        feature_id: null,
        title: '',
      });
    }
    touched.push(nodeId);
  }
  if (touched.length) {
    state.dirty = true;
    markFeaStale();
    renderDraftSteps();
    schedulePreview();
    statusFn(`位置调整：${touched.length} 件（已入步骤表，触发干涉重检）`);
  }
}

function renderDraftSteps(targetBox) {
  const box = targetBox || $('#steps-list');
  box.innerHTML = '';
  if (!targetBox) refreshFeaPanel();   // 步骤数变化 → FEA 按钮可用性/过期标记
  if (!state.draftSteps.length) {
    const ph = document.createElement('div');
    ph.className = 'empty';
    ph.textContent = '暂无草稿步骤：从下方操作表单选择目标与操作生成';
    box.appendChild(ph);
    return;
  }
  state.draftSteps.forEach((s, i) => {
    const row = document.createElement('div'); row.className = 'step';
    const no = document.createElement('span'); no.className = 'no'; no.textContent = `${i + 1}`;
    const tt = document.createElement('span'); tt.className = 'step-title';
    tt.textContent = s.title || stepFallbackTitle(s);
    const meta = document.createElement('span'); meta.className = 'step-meta';
    meta.textContent = s.operation === 'move'
      ? `${s.node_id} · move`
      : `${s.template_id} · ${s.operation}`;
    const del = document.createElement('button'); del.className = 'del'; del.textContent = '×';
    del.title = '删除此步骤';
    del.addEventListener('click', () => {
      state.draftSteps.splice(i, 1);
      state.dirty = true;
      markFeaStale();
      renderDraftSteps();
      updateSessionHeader();
      schedulePreview();
    });
    row.append(no, tt, meta, del);
    box.appendChild(row);
  });
  // 镜像到窄屏抽屉
  if (box.id === 'steps-list' && $('#drawer-steps')) {
    renderDraftSteps($('#drawer-steps'));
  }
}

// ==========================================================================
// M3：操作表单（目标模板 → 粒度 → 特征 → 操作 → 参数 级联）
// 操作契约与后端 apply_template_edit / apply_feature_edit 一一对应
// ==========================================================================

const TEMPLATE_OPS = {
  drill: {
    label: '钻孔', fields: [
      { key: 'radius', label: '半径 R (mm)', type: 'number', def: 1.5, min: 0.05, step: 0.05 },
      { key: 'depth', label: '深度 (mm)', type: 'number', def: 5, min: 0.05, step: 0.05 },
      { key: 'position', label: '位置 x,y,z（模板局部）', type: 'vec3', def: '0,0,0' },
      { key: 'direction', label: '方向 x,y,z', type: 'vec3', def: '0,0,1' },
    ],
  },
  fillet: {
    label: '倒圆（全部边）', fields: [
      { key: 'radius', label: '圆角半径 R (mm)', type: 'number', def: 0.5, min: 0.05, step: 0.05 },
    ],
  },
  chamfer: {
    label: '倒角（全部边）', fields: [
      { key: 'distance', label: '倒角距离 (mm)', type: 'number', def: 0.5, min: 0.05, step: 0.05 },
    ],
  },
  scale: {
    label: '整体缩放', fields: [
      { key: 'factor', label: '缩放系数', type: 'number', def: 1.1, min: 0.01, step: 0.01 },
    ],
  },
};

const FEATURE_OPS = {
  hole_resize: {
    label: '扩孔', fields: [
      { key: 'radius', label: '新半径 R (mm)', type: 'number', def: 0, min: 0.05, step: 0.05 },
    ],
    types: ['hole', 'cylinder', 'cone', 'sphere'],
  },
  boss_remove: {
    label: '去凸台', fields: [],
    types: ['boss', 'cylinder', 'cone', 'sphere'],
  },
};

function setFormHint(text, level = 'info') {
  const el = $('#sf-hint');
  el.className = `sf-hint ${level === 'info' ? '' : level}`.trim();
  el.textContent = text;
}

function currentGranularity() {
  return $('#sf-granularity').value;   // 'template' | 'feature'
}

function currentOpDef() {
  const key = $('#sf-operation').value;
  if (currentGranularity() === 'feature') return FEATURE_OPS[key];
  return TEMPLATE_OPS[key];
}

function currentFeatures() {
  return state.featuresCache.get(state.targetTemplateId) || [];
}

function currentFeature() {
  const fid = $('#sf-feature').value;
  if (!fid) return null;
  return currentFeatures().find((f) => f.id === fid) || null;
}

/** 按所选特征类型过滤可用操作（hole 类可扩孔，boss 类可去除，柱/锥/球均可）。 */
function featureOpsFor(feat) {
  if (!feat) return {};
  return Object.fromEntries(
    Object.entries(FEATURE_OPS).filter(([, def]) => def.types.includes((feat.type || '').toLowerCase())));
}

function renderOperations() {
  const sel = $('#sf-operation');
  const key = sel.value;   // 尽量保持当前选择
  sel.innerHTML = '';
  let ops = TEMPLATE_OPS;
  if (currentGranularity() === 'feature') {
    const feat = currentFeature();
    ops = feat ? featureOpsFor(feat) : {};
    if (feat && !Object.keys(ops).length) {
      setFormHint('该特征类型不支持定点编辑', 'error');
    }
  }
  for (const [k, def] of Object.entries(ops)) {
    const o = document.createElement('option');
    o.value = k; o.textContent = def.label;
    sel.appendChild(o);
  }
  if (ops[key]) sel.value = key;
  renderParams();
}

function renderParams() {
  const box = $('#sf-params');
  box.innerHTML = '';
  const def = currentOpDef();
  if (!def) return;
  const feat = currentGranularity() === 'feature' ? currentFeature() : null;
  for (const f of def.fields) {
    const row = document.createElement('div'); row.className = 'sf-row';
    const lab = document.createElement('label'); lab.textContent = f.label;
    lab.htmlFor = `sf-p-${f.key}`;
    let input;
    if (f.type === 'vec3') {
      input = document.createElement('input');
      input.type = 'text'; input.value = f.def;
      input.placeholder = 'x,y,z';
    } else {
      input = document.createElement('input');
      input.type = 'number';
      input.min = f.min; input.step = f.step;
      // hole_resize：新半径默认 = 特征当前半径 + 0.5（不可缩孔，直接给合法初值）
      let v = f.def;
      if (feat && f.key === 'radius' && feat.radii?.length) {
        v = Math.round((Math.max(...feat.radii) + 0.5) * 100) / 100;
      }
      input.value = v;
    }
    input.id = `sf-p-${f.key}`;
    row.append(lab, input);
    box.appendChild(row);
  }
  // 特征上下文提示（当前半径等）
  if (feat) {
    const r = (feat.radii || []).map((x) => `R${x}`).join('/');
    setFormHint(`${feat.label} ${feat.id} · ${feat.type} · ${r} · 深 ${feat.extent ?? '—'}`);
  } else {
    setFormHint('');
  }
}

async function loadFeaturesFor(tid) {
  if (state.featuresCache.has(tid)) return state.featuresCache.get(tid);
  const tpl = state.baseline?.manifest?.templates.find((t) => t.id === tid);
  if (!tpl?.features) {
    state.featuresCache.set(tid, []);
    return [];
  }
  try {
    const r = await fetch(`/cache/${state.cacheKey}/${tpl.features}`);
    const feats = r.ok ? await r.json() : [];
    state.featuresCache.set(tid, Array.isArray(feats) ? feats : []);
  } catch {
    state.featuresCache.set(tid, []);
  }
  return state.featuresCache.get(tid);
}

async function renderFeatureOptions() {
  const row = $('#sf-feature-row');
  const sel = $('#sf-feature');
  const keep = sel.value;
  row.hidden = currentGranularity() !== 'feature';
  if (row.hidden) return;
  sel.innerHTML = '';
  const feats = await loadFeaturesFor(state.targetTemplateId);
  if (!feats.length) {
    const o = document.createElement('option');
    o.value = ''; o.textContent = '（该模板无特征数据）';
    sel.appendChild(o);
    setFormHint('该模板没有可编辑特征，可切换为整模板粒度', 'error');
    return;
  }
  feats.forEach((f) => {
    const o = document.createElement('option');
    o.value = f.id;
    const r = (f.radii || []).map((x) => `R${x}`).join('/');
    o.textContent = `${f.label} ${f.id}${r ? ` (${r})` : ''}`;
    sel.appendChild(o);
  });
  if (feats.some((f) => f.id === keep)) sel.value = keep;
  applyPendingFeature();   // pick 竞态兜底：列表就绪后应用特征级拾取结果
  syncFeatureHighlight();  // keep 恢复 / pending 应用后同步 3D 高亮
  // 预载特征 glTF（双视口）：点击模型面 → 特征级拾取联动即点即中
  sceneBaseline.preloadFeaturesByTemplate(state.targetTemplateId);
  sceneDraft.preloadFeaturesByTemplate(state.targetTemplateId);
  renderOperations();
}

/** 特征级拾取的竞态兜底：pickFeatureAt（等 glTF）与特征列表重载（等 JSON）
 *  并行，谁先完成不定；pick 结果暂存 pending，列表就绪后由此应用。 */
let pendingFeature = null;   // {templateId, featureId}
function applyPendingFeature() {
  const sel = $('#sf-feature');
  const p = pendingFeature;
  if (!p || !sel) return;
  if (p.templateId !== state.targetTemplateId) return;         // 模板又变了，丢弃
  if (![...sel.options].some((o) => o.value === p.featureId)) return;  // 列表未就绪
  if (sel.value === p.featureId) { pendingFeature = null; return; }
  sel.value = p.featureId;
  pendingFeature = null;
  sel.dispatchEvent(new Event('change'));   // → 操作项刷新 + 3D 高亮
}

function setTargetTemplate(tid) {
  if (!tid || tid === state.targetTemplateId) return;
  state.targetTemplateId = tid;
  $('#sf-template').value = tid;
  if (currentGranularity() === 'feature') {
    renderFeatureOptions();       // 异步刷新特征列表（完成后重渲染操作）
  } else {
    renderOperations();
  }
  reapplyViewFilter();            // 范围过滤跟随目标模板重应用（零件保持默认色）
  syncFeatureHighlight();         // 模板已换：特征高亮随 currentFeature 为空自动清除
}

/** 特征粒度下把选中特征在双视口 3D 高亮（首页特征面板同款 overlay）。
 * anchor 取目标模板首个实例——特征 glTF 是模板级几何，任意实例等价；
 * 模板粒度 / 未选特征 → 清除。preview 重载后 overlay 随 featureState 保留。 */
async function syncFeatureHighlight() {
  const feat = currentGranularity() === 'feature' ? currentFeature() : null;
  let anchorId = null;
  if (feat && state.targetTemplateId) {
    for (const id of sceneBaseline.instances.keys()) {
      if (sceneBaseline.templateOf(id) === state.targetTemplateId) { anchorId = id; break; }
    }
  }
  if (feat && anchorId) {
    await sceneBaseline.showFeature(anchorId, feat.id);
    await sceneDraft.showFeature(anchorId, feat.id);
  } else {
    sceneBaseline.showFeature(null, null);
    sceneDraft.showFeature(null, null);
  }
}

function initStepForm() {
  const tplSel = $('#sf-template');
  tplSel.innerHTML = '';
  for (const t of state.baseline.manifest.templates) {
    const o = document.createElement('option');
    o.value = t.id;
    o.textContent = `${t.name} (${t.id})`;
    tplSel.appendChild(o);
  }
  // 初始目标：URL scope 带入的模板优先
  const initial = state.baseline.manifest.templates.some((t) => t.id === state.targetTemplateId)
    ? state.targetTemplateId
    : state.baseline.manifest.templates[0]?.id || null;
  state.targetTemplateId = initial;
  if (initial) tplSel.value = initial;
  renderOperations();
}

$('#sf-template').addEventListener('change', (e) => setTargetTemplate(e.target.value));
$('#sf-granularity').addEventListener('change', () => {
  // 切到特征粒度：先异步载特征列表，载完由 renderFeatureOptions 内部渲染操作；
  // 切回整模板粒度：直接重渲染操作
  if (currentGranularity() === 'feature') renderFeatureOptions();
  else renderOperations();
  syncFeatureHighlight();
});
$('#sf-feature').addEventListener('change', () => {
  renderOperations();
  syncFeatureHighlight();
});
$('#sf-operation').addEventListener('change', () => renderParams());

$('#btn-add-step').addEventListener('click', () => {
  if (!state.baselineLoaded || !state.targetTemplateId) {
    setFormHint('等待基线加载…', 'error');
    return;
  }
  const granularity = currentGranularity();
  const opKey = $('#sf-operation').value;
  const def = granularity === 'feature' ? FEATURE_OPS[opKey] : TEMPLATE_OPS[opKey];
  if (!def) { setFormHint('请先选择操作', 'error'); return; }
  const feat = granularity === 'feature' ? currentFeature() : null;
  if (granularity === 'feature' && !feat) {
    setFormHint('请先选择目标特征', 'error');
    return;
  }
  // 参数收集与校验
  const params = {};
  for (const f of def.fields) {
    const el = document.getElementById(`sf-p-${f.key}`);
    if (!el) continue;
    if (f.type === 'vec3') {
      const v = el.value.split(',').map((s) => parseFloat(s.trim()));
      if (v.length !== 3 || v.some((x) => !isFinite(x))) {
        setFormHint(`${f.label} 需为 x,y,z 三个数字`, 'error');
        return;
      }
      params[f.key] = v;
    } else {
      const v = parseFloat(el.value);
      if (!isFinite(v) || v <= 0) {
        setFormHint(`${f.label} 需为正数`, 'error');
        return;
      }
      params[f.key] = v;
    }
  }
  const tplName = state.baseline.manifest.templates
    .find((t) => t.id === state.targetTemplateId)?.name || state.targetTemplateId;
  stepSeq++;
  const step = normalizeStep({
    id: `s${Date.now()}_${stepSeq}`,
    template_id: state.targetTemplateId,
    operation: opKey,
    params,
    ...(feat ? { feature_id: feat.id } : {}),
    title: feat
      ? `${tplName} ${feat.id}: ${def.label}`
      : `${tplName}: ${def.label}`,
  });
  state.draftSteps.push(step);
  state.dirty = true;
  markFeaStale();
  renderDraftSteps();
  updateSessionHeader();
  setFormHint(`已添加：${step.title}`, 'ok');
  schedulePreview();
});

// ==========================================================================
// 草稿重放 + 增量干涉（自动）
// ==========================================================================
function schedulePreview() {
  // debounce：连续编辑不频繁请求
  if (state.previewTimer) clearTimeout(state.previewTimer);
  state.previewTimer = setTimeout(() => runPreview('bbox'), 250);
}

/** 草稿预览 + 干涉检查。
 * level='bbox'（默认）：AABB 快速反馈，步骤每次变更自动触发（毫秒级，
 * 拖拽调位不卡）；level='exact'：布尔精检，显式按钮触发，确认保存
 * 的后端守门也始终 exact——快速反馈只做提示，落版本前的判定不降级。 */
async function runPreview(level = 'bbox') {
  if (!state.cacheKey || !state.baselineLoaded) return;
  if (state.previewInFlight) {
    // 上一次还在跑：等它完再触发一次
    state.previewTimer = setTimeout(() => runPreview(level), 300);
    return;
  }
  state.previewInFlight = true;
  state.checkLevel = level;
  const label = level === 'exact' ? '精确检查' : '快速检查';
  setVerifyHint(`${label}中…`, 'info');
  // 耗时计数：exact 大装配体布尔精检可能数十秒，
  // 静态文案会被当成假死；显示已耗时让用户知道仍在跑
  const t0 = Date.now();
  const tick = setInterval(() => {
    if (!state.previewInFlight) { clearInterval(tick); return; }
    setVerifyHint(`${label}中… ${Math.round((Date.now() - t0) / 1000)}s`, 'info');
  }, 1000);
  const stepsAtRequest = state.draftSteps;   // 竞态守卫：放弃草稿会替换数组引用
  try {
    const res = await previewDraft(state.cacheKey, stepsAtRequest, level);
    if (stepsAtRequest !== state.draftSteps) return;   // 期间被放弃/替换：丢弃过期响应
    state.lastPreview = res;
    if (res?.manifest) {
      // 草稿场景刷新到草稿几何（实例重建后需重应用范围过滤 + 目标高亮）。
      // 被编辑模板是 /drafts/ 绝对路径，未编辑模板是 cache 相对路径 → base_url
      await sceneDraft.load(res.manifest, res.base_url || '');
      treeDraft.render(res.manifest.root);
      reapplyViewFilter();
      reapplyObservation();   // 爆炸/鬼影/剖切跨重建保持
      // 移动模式下重挂 gizmo：实例已带 move 后矩阵，代理中心随之更新
      if (state.moveMode && state.focus.nodeId) {
        sceneDraft.enableMove([state.focus.nodeId]);
      }
    }
    renderVerify(res);
  } catch (err) {
    const payload = err.payload || {};
    if (payload.interferences) {
      // 409：草稿几何产生干涉（preview 也走守门）
      renderVerify(payload);
      setVerifyHint(`草稿产生 ${payload.interferences.length} 处干涉`, 'error');
    } else {
      setVerifyHint(`检查失败：${err.message}`, 'error');
    }
  } finally {
    clearInterval(tick);
    state.previewInFlight = false;
    state.previewTimer = null;
  }
}

/** 点击干涉结果：在模型上高亮对应的两个零件（双视口同染），并取景到两者。
 *  h: 干涉项 {a:{id,name}, b:{id,name}, volume_mm3?} */
function focusInterference(h) {
  const a = h?.a?.id, b = h?.b?.id;
  if (!a || !b || !state.baselineLoaded) return;
  const ids = [a, b];
  sceneBaseline.highlightPair(a, b);   // 洋红/青强对比，两个零件更直观
  sceneDraft.highlightPair(a, b);
  sceneBaseline.fitToIds(ids);
  if (!camSync) sceneDraft.fitToIds(ids);   // 相机联动时基线取景已带过去
  statusFn(`已高亮 ${h.a?.name || a} ↔ ${h.b?.name || b}`);
}

// ==========================================================================
// 精确检查结果跨刷新保留：只保留「步序未变时的精检结果」，一步骤有改动即失效
// （存 sessionStorage，按 cache_key 分槽；刷新同标签页保留，关标签页即清）
// ==========================================================================
const VERIFY_STORE_PREFIX = 'cad-verify:';

/** 对步骤表内容做轻量哈希（忽略 id/title 等展示字段，只算会改变几何的部分）。 */
function draftStepsHash(steps) {
  const s = JSON.stringify((steps || []).map((st) => ({
    template_id: st.template_id,
    operation: st.operation,
    params: st.params,
    feature_id: st.feature_id,
    node_id: st.node_id,
  })));
  let h = 5381;
  for (let i = 0; i < s.length; i++) { h = ((h << 5) + h) + s.charCodeAt(i); h |= 0; }
  return `v1_${Math.abs(h).toString(36)}`;
}

/** 保存精检结果（仅 exact；含 manifest 以便刷新后重建草稿几何，含步序哈希
 *  用于判定恢复后是否已过期）。步骤改动也不会清除——逐处处理干涉时列表保持。 */
function saveExactResult(res) {
  if (res?.check_level !== 'exact' || !state.cacheKey) return;
  try {
    sessionStorage.setItem(
      VERIFY_STORE_PREFIX + state.cacheKey,
      JSON.stringify({ hash: draftStepsHash(state.draftSteps), res }),
    );
  } catch (_) { /* 存储满/禁用：静默忽略，回退为不保留 */ }
}

/** 清除当前精检结果（含本地存储），由「重置」与放弃草稿/删除草稿触发。 */
function clearExactResult() {
  state.exactResult = null;
  state.verifyStale = false;
  try { sessionStorage.removeItem(VERIFY_STORE_PREFIX + state.cacheKey); } catch (_) {}
}

/** 手动重置：丢弃已存精检列表，回到当前几何的自动粗筛。 */
function resetVerify() {
  clearExactResult();
  schedulePreview();
  statusFn('已重置检查结果，回到自动粗筛');
}

/** 尝试恢复上次精检结果。成功（返回 true）时已重建草稿几何并刷新验证栏，
 *  调用方应跳过本次自动 bbox 预览；失败则返回 false。步序若有改动仍恢复，
 *  但标记为已过期（由用户「重置」或重新精检决定）。 */
function tryRestoreExact() {
  if (!state.cacheKey || !state.baselineLoaded) return false;
  let entry = null;
  try {
    const raw = sessionStorage.getItem(VERIFY_STORE_PREFIX + state.cacheKey);
    if (raw) entry = JSON.parse(raw);
  } catch (_) { /* 解析失败按不存在处理 */ }
  const res = entry?.res;
  if (res?.check_level !== 'exact') return false;
  state.lastPreview = res;
  state.exactResult = res;
  if (entry.hash !== draftStepsHash(state.draftSteps)) {
    state.verifyStale = true;   // 步序已变：结果对未来几何过期，但先不丢
  } else {
    state.verifyStale = false;
  }
  const restorePane = () => {
    renderVerify(res);
    const n = (res.interferences || []).length;
    setVerifyHint(state.verifyStale
      ? `已恢复精检结果（${n} 处干涉 · 步序已变，已过期）`
      : (n ? `已恢复精检结果（${n} 处干涉）` : '已恢复精检结果（无干涉）'),
      state.verifyStale ? 'warn' : (n ? 'error' : 'ok'));
  };
  if (res.manifest) {
    sceneDraft.load(res.manifest, res.base_url || '').then(() => {
      treeDraft.render(res.manifest.root);
      reapplyViewFilter();
      reapplyObservation();
      if (state.moveMode && state.focus.nodeId) sceneDraft.enableMove([state.focus.nodeId]);
      restorePane();
    }).catch(() => restorePane());
  } else {
    restorePane();
  }
  return true;
}

function renderVerify(res) {
  // 本次是否精检结果
  const incomingExact = res?.check_level === 'exact'
    || (res?.interferences || []).some((h) => h.volume_mm3 !== undefined);
  if (incomingExact) {
    // 精检结果：成为当前权威列表并落存储（刷新后可恢复）
    state.exactResult = res;
    state.verifyStale = false;
    saveExactResult(res);
  } else if (state.exactResult?.check_level === 'exact') {
    // bbox 粗筛（步骤改动后的自动预览）到来时，已有精检结果 → 不覆盖丢弃，
    // 继续展示精检列表（逐处处理干涉不丢），仅标记过期；几何已由调用方先行刷新
    state.verifyStale = true;
    res = state.exactResult;
  }
  const hits = res?.interferences || [];
  const edited = res?.edited_templates || [];
  const isExact = res?.check_level === 'exact'
    || hits.some((h) => h.volume_mm3 !== undefined);
  // 干涉卡片
  const interferenceBox = $('#verify-interferences');
  interferenceBox.innerHTML = '';
  if (!hits.length) {
    const ok = document.createElement('div'); ok.className = 'verify-ok';
    const scope = edited.length ? `（已编辑 ${edited.length} 模板）` : '';
    ok.textContent = isExact
      ? (state.verifyStale
        ? `✓ 精检无干涉（已过期）${scope}`
        : `✓ 精检无干涉${scope}`)
      : `✓ 快速粗筛无碰撞${scope}`;
    interferenceBox.appendChild(ok);
    setVerifyHint(isExact
      ? (state.verifyStale ? '精检无干涉（步序已变，已过期）' : '精检无干涉')
      : '粗筛无碰撞（AABB 级）', isExact ? (state.verifyStale ? 'warn' : 'ok') : 'ok');
  } else if (isExact) {
    setVerifyHint(state.verifyStale
      ? `${hits.length} 处干涉（精检 · 步序已变，已过期）`
      : `${hits.length} 处干涉（布尔精检）`,
      state.verifyStale ? 'warn' : 'error');
    hits.forEach((h) => {
      const r = document.createElement('div'); r.className = 'verify-item err';
      r.textContent = `${h.a.name} ↔ ${h.b.name}：穿透 ${h.volume_mm3} mm³`;
      r.addEventListener('click', () => focusInterference(h));   // 点击高亮对应零件
      interferenceBox.appendChild(r);
    });
  } else {
    // bbox 级：重叠即「可能碰撞」，黄色提示，等待精确检查定论
    setVerifyHint(`${hits.length} 处可能碰撞（粗筛，点「精确检查」确认）`, 'warn');
    hits.forEach((h) => {
      const r = document.createElement('div'); r.className = 'verify-item warn';
      r.textContent = `${h.a.name} ↔ ${h.b.name}：可能碰撞`;
      r.addEventListener('click', () => focusInterference(h));   // 点击高亮对应零件
      interferenceBox.appendChild(r);
    });
  }
  // 已编辑模板列表（几何）+ 已移动节点（位置）
  const editedBox = $('#verify-edited');
  editedBox.innerHTML = '';
  const moved = res?.moved_nodes || [];
  const rows = [
    ...edited.map((tid) => ({ text: `${tid} · 几何`, cls: '' })),
    ...moved.map((nid) => {
      const rec = treeBaseline.nodes.get(nid);
      return { text: `${rec?.node?.name || nid} · 位置`, cls: 'moved' };
    }),
  ];
  if (rows.length) {
    rows.forEach((r0) => {
      const r = document.createElement('div'); r.className = 'verify-item';
      if (r0.cls) r.classList.add(r0.cls);
      r.textContent = r0.text;
      editedBox.appendChild(r);
    });
  } else {
    const ph = document.createElement('div'); ph.className = 'placeholder';
    ph.textContent = '尚未编辑任何模板';
    editedBox.appendChild(ph);
  }
  // M5 差异摘要（体积/表面积，随 preview 响应刷新）
  renderDiffSummary(res?.diff);
  // 镜像到窄屏抽屉
  if ($('#drawer-verify')) renderVerifyPane($('#drawer-verify'));
}

function renderVerifyPane(box) {
  if (!box) return;
  box.innerHTML = '';
  const hint = document.createElement('div'); hint.className = 'verify-hint info';
  hint.textContent = $('#verify-hint')?.textContent || '';
  box.appendChild(hint);
  // 优先展示精检结果（与右栏一致：步骤改动后仍保留精检列表）；否则用最近预览
  const src = state.exactResult?.check_level === 'exact' ? state.exactResult : state.lastPreview;
  const hits = src?.interferences || [];
  const isExact = src?.check_level === 'exact'
    || hits.some((h) => h.volume_mm3 !== undefined);
  if (hits.length) {
    hits.forEach((h) => {
      const r = document.createElement('div');
      r.className = `verify-item ${isExact ? 'err' : 'warn'}`;
      r.textContent = `${h.a.name} ↔ ${h.b.name}`;
      r.addEventListener('click', () => focusInterference(h));   // 点击高亮对应零件
      box.appendChild(r);
    });
  } else {
    const ok = document.createElement('div'); ok.className = 'verify-ok';
    ok.textContent = isExact ? '✓ 精检无干涉' : '✓ 粗筛无碰撞';
    box.appendChild(ok);
  }
  const reset = document.createElement('button');
  reset.className = 'rail-btn drawer-reset';
  reset.textContent = '重置检查';
  reset.title = '丢弃当前检查结果，回到自动粗筛';
  reset.addEventListener('click', () => resetVerify());
  box.appendChild(reset);
}

function setVerifyHint(text, level = 'info') {
  const el = $('#verify-hint');
  if (!el) return;
  el.className = `verify-hint ${level}`;
  el.textContent = text;
}
setVerifyHint('等待草稿步骤变更…（自动检查为 AABB 快速粗筛）');

// 精确检查按钮：显式触发布尔精检（自动粗筛只做 AABB 提示；
// 确认保存的后端守门也始终 exact）
$('#verify-exact').addEventListener('click', () => runPreview('exact'));
$('#verify-reset').addEventListener('click', () => resetVerify());

// ==========================================================================
// M5：FEA 基线 vs 草稿双跑对比 + 差异摘要
// ==========================================================================

function fmtSigned(v, unit, digits = 2) {
  if (v == null) return '—';
  const n = +v;
  const sign = n > 0 ? '+' : '';
  return `${sign}${n.toFixed(digits)} ${unit}`;
}

function pctCls(p) {
  if (p == null) return 'flat';
  if (Math.abs(p) < 0.05) return 'flat';
  return p > 0 ? 'up' : 'down';
}

/** 差异摘要：每个编辑模板的体积变化 + 总计（preview 响应附带）。 */
function renderDiffSummary(diff) {
  const box = $('#verify-diff');
  if (!box) return;
  box.innerHTML = '';
  if (!diff || !diff.per_template?.length) {
    const ph = document.createElement('div'); ph.className = 'placeholder';
    const movedOnly = state.lastPreview?.moved_nodes?.length;
    ph.textContent = movedOnly
      ? `仅位置调整（${movedOnly} 件移动）：无几何变化，体积/面积不变`
      : '尚未编辑任何模板';
    box.appendChild(ph);
    return;
  }
  diff.per_template.forEach((t) => {
    const row = document.createElement('div'); row.className = 'diff-row';
    const name = document.createElement('span'); name.className = 'd-name';
    name.textContent = `${t.name} ×${t.instances}`;
    name.title = `基线 ${t.baseline_volume_mm3} mm³ → 草稿 ${t.draft_volume_mm3} mm³`;
    const val = document.createElement('span');
    val.className = `d-val ${pctCls(t.volume_pct)}`;
    val.textContent = `${fmtSigned(t.delta_volume_mm3, 'mm³', 1)} (${fmtSigned(t.volume_pct, '%')})`;
    row.append(name, val);
    box.appendChild(row);
  });
  const tot = diff.totals || {};
  const row = document.createElement('div'); row.className = 'diff-row';
  const name = document.createElement('span'); name.className = 'd-name';
  name.style.fontWeight = '600';
  name.textContent = '装配总计（实例加权）';
  const val = document.createElement('span');
  val.className = `d-val ${pctCls(tot.volume_pct)}`;
  val.textContent = `${fmtSigned(tot.delta_volume_mm3, 'mm³', 1)} (${fmtSigned(tot.volume_pct, '%')})`;
  row.append(name, val);
  box.appendChild(row);
}

/** FEA 对比面板：初始状态（按钮 + 插件可用性）。 */
function renderFeaPanelIdle(hintText) {
  const box = $('#fea-compare');
  if (!box) return;
  box.innerHTML = '';
  const head = document.createElement('div'); head.className = 'fea-compare-head';
  const btn = document.createElement('button'); btn.className = 'fea-run-btn';
  btn.id = 'btn-fea-run';
  const tplName = state.baseline?.manifest?.templates
    .find((t) => t.id === state.targetTemplateId)?.name || state.targetTemplateId || '—';
  btn.textContent = `▶ 对 ${tplName} 双跑对比`;
  btn.addEventListener('click', runFeaCompare);
  head.appendChild(btn);
  const hint = document.createElement('div'); hint.className = 'placeholder';
  hint.textContent = hintText
    || '对目标模板跑基线 vs 草稿静力学，对比最大位移 / von Mises';
  head.appendChild(hint);
  box.appendChild(head);
  if (state.feaAvailable === false) {
    btn.disabled = true;
    hint.textContent = 'FEA 插件不可用：需安装 FreeCAD + CalculiX（重启服务后自动探测）';
  } else if (!state.draftSteps.length) {
    btn.disabled = true;
    hint.textContent = '先添加草稿步骤（对比需草稿编辑目标模板）';
  }
}

/** FEA 结果表：基线 / 草稿 / 变化% 三列。 */
function renderFeaResult(res, stale) {
  const box = $('#fea-compare');
  if (!box) return;
  box.innerHTML = '';
  const head = document.createElement('div'); head.className = 'fea-compare-head';
  const btn = document.createElement('button'); btn.className = 'fea-run-btn';
  const tplName = state.baseline?.manifest?.templates
    .find((t) => t.id === res.template_id)?.name || res.template_id;
  btn.textContent = `▶ 重新对比（${tplName}）`;
  btn.addEventListener('click', runFeaCompare);
  head.appendChild(btn);
  const tag = document.createElement('div'); tag.className = 'placeholder';
  tag.textContent = stale
    ? '⚠ 草稿步骤已变化，以下结果基于旧草稿'
    : `载荷 ${res.spec?.force_N ?? '—'}N · ${res.spec?.axis?.toUpperCase() ?? 'Z'} 轴`;
  head.appendChild(tag);
  box.appendChild(head);

  const table = document.createElement('div'); table.className = 'fea-table';
  const h = (text) => {
    const el = document.createElement('div'); el.className = 'ft-h';
    el.textContent = text; return el;
  };
  const m = (text) => {
    const el = document.createElement('div'); el.className = 'ft-metric';
    el.textContent = text; return el;
  };
  const v = (text, cls) => {
    const el = document.createElement('div');
    el.className = `ft-val ${cls || ''}`;
    el.textContent = text; return el;
  };
  const d = (p) => {
    const el = document.createElement('div');
    el.className = `ft-delta ${pctCls(p)}`;
    el.textContent = p == null ? '—' : `${p > 0 ? '+' : ''}${p}%`;
    return el;
  };
  table.append(
    h('指标'), h('基线'), h('草稿'), h('变化'),
    m('最大位移'),
    v(res.baseline?.max_displacement_mm?.toFixed(4) ?? '—', 'ft-base'),
    v(res.draft?.max_displacement_mm?.toFixed(4) ?? '—', 'ft-draft'),
    d(res.delta?.max_displacement_pct),
    m('最大 von Mises'),
    v(res.baseline?.max_von_mises_MPa?.toFixed(2) ?? '—', 'ft-base'),
    v(res.draft?.max_von_mises_MPa?.toFixed(2) ?? '—', 'ft-draft'),
    d(res.delta?.max_von_mises_pct),
  );
  box.appendChild(table);
  const foot = document.createElement('div'); foot.className = 'placeholder';
  const mesh = res.draft?.mesh_nodes != null
    ? `网格 ${res.draft.mesh_nodes} 节点 / ${res.draft.mesh_elements} 单元 · ` : '';
  foot.textContent = mesh
    + (res.draft?.cache_hit ? '草稿侧缓存命中' : '草稿侧已求解');
  box.appendChild(foot);
}

function markFeaStale() {
  // 草稿步骤变更：已有 FEA 结果标记过期（renderDraftSteps → refreshFeaPanel 展示）
  state.feaStale = true;
}

function refreshFeaPanel() {
  if (state.feaJob) return;   // 任务进行中，不覆盖进度 UI
  if (state.feaResult) renderFeaResult(state.feaResult, state.feaStale);
  else renderFeaPanelIdle();
}

/** 运行 FEA 对比（R5 任务协议：202 + 轮询 + 取消）。 */
async function runFeaCompare() {
  if (!state.cacheKey || !state.targetTemplateId || !state.draftSteps.length) {
    statusFn('FEA 对比需要：目标模板 + 至少一个草稿步骤', true);
    return;
  }
  if (state.feaJob) {
    statusFn('FEA 对比任务进行中…', true);
    return;
  }
  const box = $('#fea-compare');
  box.innerHTML = '';
  const ph = document.createElement('div'); ph.className = 'placeholder';
  ph.textContent = '提交双跑任务…';
  box.appendChild(ph);
  try {
    const started = await startFeaCompare(
      state.cacheKey, state.targetTemplateId, state.draftSteps);
    state.feaJob = { id: started.job_id, timer: null };
    pollFeaJob();
  } catch (err) {
    const payload = err.payload || {};
    if (payload.kind === 'missing') {
      state.feaAvailable = false;
      renderFeaPanelIdle('FEA 插件不可用：需安装 FreeCAD + CalculiX');
      statusFn(`FEA 不可用：${payload.missing?.join('、') || err.message}`, true);
    } else {
      renderFeaPanelIdle();
      statusFn(`FEA 对比提交失败：${err.message}`, true);
    }
  }
}

function pollFeaJob() {
  if (!state.feaJob) return;
  state.feaJob.timer = setInterval(async () => {
    let job;
    try {
      job = await getJob(state.feaJob.id);
    } catch { return; }
    const { status, progress = {}, result } = job;
    const box = $('#fea-compare');
    if (status === 'done') {
      clearInterval(state.feaJob.timer);
      state.feaJob = null;
      state.feaResult = result;
      renderFeaResult(result, false);
      const dp = result?.delta?.max_von_mises_pct;
      statusFn(`FEA 对比完成：von Mises ${dp == null ? '—' : `${dp > 0 ? '+' : ''}${dp}%`}`);
    } else if (status === 'error' || status === 'cancelled') {
      clearInterval(state.feaJob.timer);
      state.feaJob = null;
      const msg = status === 'cancelled' ? '已取消' : (job.error || '失败');
      renderFeaPanelIdle();
      statusFn(`FEA 对比${msg}`, true);
    } else {
      // 进行中：进度条（简版文本）
      if (box && progress.percent != null) {
        box.innerHTML = '';
        const p = document.createElement('div'); p.className = 'placeholder';
        p.textContent = `双跑中… ${Math.round(progress.percent)}%（${progress.detail || progress.phase || ''}）`;
        box.appendChild(p);
        const cancel = document.createElement('button');
        cancel.className = 'fea-run-btn';
        cancel.textContent = '取消';
        cancel.addEventListener('click', async () => {
          try { await cancelJob(state.feaJob.id); } catch {}
        });
        box.appendChild(cancel);
      }
    }
  }, 900);
}

// 插件探测：进入会话即探测 FEA 可用性
getPlugins().then((p) => {
  state.feaAvailable = p?.fea?.available ?? null;
}).catch(() => { state.feaAvailable = null; });
renderFeaPanelIdle();

// ==========================================================================
// 会话条操作：放弃 / 保存草稿 / 确认保存
// ==========================================================================
$('#sess-abandon').addEventListener('click', async () => {
  if (!confirm('放弃草稿？步骤将清空且不会保存。')) return;
  // 草稿视口是否展示着编辑几何：只有上次 preview 实际应用过编辑时才需要
  // 重载回基线；零步骤时视口本就是基线，重载反而冲掉范围过滤与相机取景
  const sceneDiverged = !!(state.lastPreview?.edited_templates?.length);
  state.draftSteps = [];
  state.dirty = false;
  if (state.cacheKey) {
    try { await deleteDraft(state.cacheKey, getTabId()); } catch {}
    deleteDraftIndexEntry(state.cacheKey);
  }
  if (sceneDiverged && state.baseline) {
    await sceneDraft.load(state.baseline.manifest, state.baseline.baseUrl);
    treeDraft.render(state.baseline.manifest.root);
    reapplyViewFilter();   // 恢复范围可见性（load 重建实例后状态归零）
    reapplyObservation();  // 爆炸/鬼影/剖切跨重建保持
    if (state.moveMode && state.focus.nodeId) {
      sceneDraft.enableMove([state.focus.nodeId]);
    }
  }
  state.lastPreview = null;
  state.feaResult = null;
  state.feaStale = false;
  clearExactResult();
  renderDraftSteps();
  renderVerify({ interferences: [], edited_templates: [] });
  updateSessionHeader();
  statusFn('草稿已放弃（留在本页可重新编辑，或返回首页）');
});

$('#sess-save').addEventListener('click', async () => {
  if (!state.cacheKey) return;
  try {
    await saveDraft(state.cacheKey, {
      baselineVersion: state.baseline?.version || 'v0',
      baselineSourceFile: state.baseline?.manifest?.source_file || '',
      steps: state.draftSteps,
      client: getTabId(),   // M6：广播 draft_saved 时带回，本 tab 忽略自己的保存
    });
    state.dirty = false;
    saveDraftIndexEntry(state.cacheKey, {
      baselineVersion: state.baseline?.version || 'v0',
      stepCount: state.draftSteps.length,
      baselineSourceFile: state.baseline?.manifest?.source_file || '',
    });
    statusFn(`草稿已保存（${state.draftSteps.length} 步 · 单槽位），可继续编辑`);
  } catch (err) {
    statusFn(`保存失败：${err.message}`, true);
  }
});

$('#sess-confirm').addEventListener('click', async () => {
  if (!state.draftSteps.length) {
    statusFn('没有可确认的草稿步骤', true); return;
  }
  if (!confirm('确认将草稿步骤全部落为一条版本？干涉守门将再次完整检查。')) return;
  const btn = $('#sess-confirm');
  btn.disabled = true;   // 防重复提交（成功窗口内连点 = 落两个版本）
  statusFn('提交中（重放 + 完整干涉守门）…');
  try {
    const res = await confirmDraft(state.cacheKey, state.draftSteps);
    // 成功：版本已落。立即清内存态（跳转前窗口内不可再提交），回首页
    // 看新版本几何（首页 parse 缓存命中会解析到当前版本指针）。
    state.draftSteps = [];
    state.dirty = false;
    deleteDraftIndexEntry(state.cacheKey);
    renderDraftSteps();
    updateSessionHeader();
    statusFn(`已落版本 ${res.version}：${res.changelog.split('\n')[0]}`);
    setTimeout(() => goHomeKeepAssembly({ flash: `已落版本 ${res.version}` }), 900);
  } catch (err) {
    const payload = err.payload || {};
    if (payload.interferences) {
      setVerifyHint(`守门拒绝：${payload.interferences.length} 处干涉`, 'error');
      renderVerify(payload);
      statusFn(`提交被守门拒绝：${payload.interferences.length} 处干涉（版本未变）`, true);
    } else {
      statusFn(`提交失败：${err.message}`, true);
    }
    btn.disabled = false;   // 失败可重试
  }
});

// ==========================================================================
// 预览范围（整装配 / 子装配 / 零件）+ 目标模板高亮 + 视口点选联动
// 焦点跟随：URL scope 初始化，视口点选零件后更新（反复定位目标的工作流）
// ==========================================================================

/** 当前范围下应保持可见的 part 集合；null = 全部可见。 */
function computeRangeKeep() {
  if (!state.baselineLoaded) return null;
  const { level, nodeId } = state.focus;
  if (state.range === 'part') {
    return level === 'part' && nodeId ? new Set([nodeId]) : null;
  }
  if (state.range === 'assembly') {
    if (level === 'assembly' && nodeId) return treeBaseline.partIdsUnder(nodeId);
    if (level === 'part' && nodeId) {
      // 零件焦点：取其父级子树（含兄弟件，提供局部上下文）
      const parentId = treeBaseline.nodes.get(nodeId)?.parentId;
      if (parentId != null) return treeBaseline.partIdsUnder(parentId);
      return new Set([nodeId]);
    }
  }
  return null;   // root / 无有效焦点
}

/** 范围可见性统一重应用（基线加载后 / 草稿刷新后 / 焦点变化后调用）。
 *  零件保持默认色：编辑页的选中反馈只有特征 overlay 的橙色高亮，
 *  整件染色会把特征级高亮淹没。 */
function reapplyViewFilter() {
  if (!state.baselineLoaded) return;
  const keep = computeRangeKeep();
  const vis = new Map();
  for (const [id, rec] of treeBaseline.nodes) {
    if (rec.node.type === 'part') vis.set(id, keep ? keep.has(id) : true);
  }
  sceneBaseline.applyVisibility(vis);
  sceneDraft.applyVisibility(vis);
  sceneBaseline.highlight(null);
  sceneDraft.highlight(null);
}

/** 范围按钮可用性：焦点层级不足时禁用（root 焦点只有整装配）。 */
function updateRangeButtons() {
  const lvl = state.focus.level;
  document.querySelectorAll('.range-btn').forEach((b) => {
    const r = b.dataset.range;
    const ok = r === 'root'
      || (r === 'assembly' && lvl !== 'root')
      || (r === 'part' && lvl === 'part');
    b.disabled = !ok;
    if (!ok) b.classList.remove('active');
  });
}

function setRange(r) {
  state.range = r;
  document.querySelectorAll('.range-btn').forEach((b) => {
    b.classList.toggle('active', b.dataset.range === r);
  });
  reapplyViewFilter();
  // M6.5：范围切换自动取景（此前切到零件级仍是全景缩放，目标只是小点）
  if (state.baselineLoaded) {
    const keep = r === 'root' ? null : computeRangeKeep();
    const ids = keep && keep.size ? [...keep] : null;
    // 基线视口取景 → 相机联动自动带到草稿视口（解锁时仅基线取景）
    sceneBaseline.fitToIds(ids);
  }
}
document.querySelectorAll('.range-btn').forEach((b) => {
  b.addEventListener('click', () => {
    if (!b.disabled) setRange(b.dataset.range);
  });
});

// ==========================================================================
// M6.5：双视口相机联动（纯视角同步，不触碰场景图）
// ==========================================================================
let camSync = true;
let camSyncing = false;   // setCameraState 回声守卫（change 同步派发）
function syncCameras(from, to) {
  if (!camSync || camSyncing || !state.baselineLoaded) return;
  camSyncing = true;
  try { to.setCameraState(from.getCameraState()); } finally { camSyncing = false; }
}
sceneBaseline.onCameraChange(() => syncCameras(sceneBaseline, sceneDraft));
sceneDraft.onCameraChange(() => syncCameras(sceneDraft, sceneBaseline));
$('#cam-sync').addEventListener('click', (e) => {
  camSync = !camSync;
  e.currentTarget.classList.toggle('active', camSync);
  if (camSync) syncCameras(sceneBaseline, sceneDraft);   // 开启即对齐
});

// ==========================================================================
// M6.5：观察工具条（爆炸 / 三轴剖切 / 鬼影 / 视角书签 —— 两视口同步）
// ==========================================================================
const sectionFlip = { on: false };
function sectionPosWorld(t) {
  const bb = sceneBaseline.bbox;
  if (!bb || bb.isEmpty()) return 0;
  const axis = $('#ob-section-axis').value;
  const k = axis === 'X' ? 0 : axis === 'Y' ? 1 : 2;
  const mn = bb.min.getComponent(k), mx = bb.max.getComponent(k);
  return mn + (mx - mn) * (t / 100);
}
function applySection() {
  const on = $('#ob-section-on').checked;
  const axis = $('#ob-section-axis').value;
  const pos = sectionPosWorld(+$('#ob-section-pos').value);
  sceneBaseline.setSection(on, pos, axis, sectionFlip.on);
  sceneDraft.setSection(on, pos, axis, sectionFlip.on);
}
$('#ob-explode').addEventListener('input', (e) => {
  const t = +e.target.value / 100;
  sceneBaseline.applyExplosion(t);
  sceneDraft.applyExplosion(t);
});
$('#ob-section-on').addEventListener('change', applySection);
$('#ob-section-pos').addEventListener('input', applySection);
$('#ob-section-axis').addEventListener('change', applySection);
$('#ob-section-flip').addEventListener('click', (e) => {
  sectionFlip.on = !sectionFlip.on;
  e.currentTarget.classList.toggle('active', sectionFlip.on);
  applySection();
});
$('#ob-xray').addEventListener('click', (e) => {
  const on = !e.currentTarget.classList.contains('active');
  e.currentTarget.classList.toggle('active', on);
  sceneBaseline.setXray(on);
  sceneDraft.setXray(on);
});

/** preview/放弃等场景重建后重应用观察状态（爆炸系数、鬼影、剖切）：
 * load 会重建网格与材质，不重应用则全部静默丢失。 */
function reapplyObservation() {
  sceneBaseline.applyExplosion(+$('#ob-explode').value / 100);
  sceneDraft.applyExplosion(+$('#ob-explode').value / 100);
  const xr = $('#ob-xray').classList.contains('active');
  sceneBaseline.setXray(xr);
  sceneDraft.setXray(xr);
  applySection();
}

// 视角书签（会话内存，存/取双视口完整视角；解锁状态下两侧独立）
const camBookmarks = new Map();
let curBm = '1';
$('#ob-cam-save').addEventListener('click', () => {
  camBookmarks.set(curBm, {
    base: sceneBaseline.getCameraState(),
    draft: sceneDraft.getCameraState(),
  });
  statusFn(`视角已存入书签 ${curBm}`);
});
document.querySelectorAll('.cam-bm').forEach((b) => {
  b.addEventListener('click', () => {
    curBm = b.dataset.bm;
    const st = camBookmarks.get(curBm);
    if (!st) { statusFn(`书签 ${curBm} 为空：先调好视角点「存」`); return; }
    sceneBaseline.setCameraState(st.base);
    if (!camSync) sceneDraft.setCameraState(st.draft);
    // 锁定模式下 baseline 的 change 会自动联动草稿视口
  });
});

// ==========================================================================
// M6.5：移动模式（仅草稿视口 —— 基线视口结构性不可动）
// 拖拽 = 临时视觉位移；松手 → 折算为 move 步骤 → preview 重放接管定位
// ==========================================================================
function setMoveMode(on) {
  const btn = $('#move-toggle');
  if (on && state.focus.level !== 'part') {
    statusFn('移动模式：先在视口点选一个零件', true);
    return;
  }
  state.moveMode = on;
  btn.classList.toggle('active', on);
  if (on) {
    sceneDraft.enableMove([state.focus.nodeId]);
    statusFn('移动模式：拖拽草稿视口箭头调整位置，松手生成 move 步骤并重检干涉');
  } else {
    sceneDraft.disableMove();
  }
}
$('#move-toggle').addEventListener('click', () => setMoveMode(!state.moveMode));
sceneDraft.onMoveEnd(() => commitTempMovesToSteps());

// 视口点选：两个视口均可点选定位目标（焦点 + 表单目标 + 高亮联动）
sceneBaseline.onPick((id, ndc) => handlePick(id, ndc, sceneBaseline));
sceneDraft.onPick((id, ndc) => handlePick(id, ndc, sceneDraft));
async function handlePick(id, ndc, srcScene) {
  if (!id) return;
  treeBaseline.select(id);
  treeDraft.select(id);
  state.focus = { level: 'part', nodeId: id };
  updateRangeButtons();
  if (state.range !== 'root' && state.range !== 'assembly' && state.range !== 'part') {
    state.range = 'root';
  }
  const tid = sceneBaseline.templateOf(id);
  if (tid) setTargetTemplate(tid);   // 内部含 reapplyViewFilter（模板未变时提前返回）
  reapplyViewFilter();               // 焦点变化影响范围过滤，统一重应用（幂等）
  if (state.moveMode) sceneDraft.enableMove([id]);   // 移动模式跟随点选换目标
  // 特征粒度：点击模型面 → 联动目标特征列表（特征级拾取）
  let featId = null;
  if (ndc && currentGranularity() === 'feature' && srcScene) {
    featId = await srcScene.pickFeatureAt(ndc, id);
    if (featId) {
      // 列表可能正在随模板切换重载（JSON/gltF 竞态）：pending 兜底
      pendingFeature = { templateId: tid, featureId: featId };
      applyPendingFeature();
    }
  }
  // M6：选中上行——agent 在对话框里说"这个"时能定位到这里点的是谁
  if (state.cacheKey) {
    postSelection({
      cache_key: state.cacheKey,
      node_id: id,
      template_id: tid || null,
      feature_id: featId,
      source_file: state.baseline?.manifest?.source_file || '',
      page: 'edit',
      client: getTabId(),
    }).catch(() => {});
  }
}

// ==========================================================================
// 初始化
// ==========================================================================
applyLayout(state.layout || defaultLayoutForWidth(window.innerWidth));
setRange({ root: 'root', assembly: 'assembly', part: 'part' }[scope.level] || 'root');
updateRangeButtons();

// M6：订阅服务端事件——agent 经 MCP edit_draft 写草稿后，编辑页原地刷新
// （重载草稿 → 重渲染步骤表 → 调度 preview 重放几何 + 增量干涉）。
async function reloadDraftFromServer(fromAgent) {
  if (!state.cacheKey || !state.baselineLoaded) return;
  try {
    const d = await loadDraft(state.cacheKey);
    if (d && !d.empty && Array.isArray(d.steps) && d.steps.length) {
      state.draftSteps = d.steps.map(normalizeStep);
      state.dirty = false;   // 远端写入即已持久化
      renderDraftSteps();
      schedulePreview();
      const from = fromAgent ? '（来自 agent）' : '';
      statusFn(`草稿已更新${from}：${state.draftSteps.length} 步，视口已刷新`);
    } else {
      // 远程草稿被清空/删除：回基线
      state.draftSteps = [];
      state.dirty = false;
      state.feaResult = null;
      state.feaStale = false;
      clearExactResult();
      renderDraftSteps();
      renderVerify({ interferences: [], edited_templates: [] });
      schedulePreview();
      statusFn('草稿已被远程删除，视口已回基线');
    }
  } catch (err) { /* 网络闪断静默：不打断当前编辑 */ }
}

initWs((ev) => {
  if (!state.cacheKey || ev.cache_key !== state.cacheKey) return;
  if (ev.type === 'draft_saved' && ev.client !== getTabId()) {
    reloadDraftFromServer(ev.client === 'mcp-agent');
  } else if (ev.type === 'draft_deleted' && ev.client !== getTabId()) {
    // 自己发起的删除（放弃草稿）已在本地处理，忽略回声
    reloadDraftFromServer(false);
  } else if (ev.type === 'version_changed') {
    statusFn(`版本已切换到 ${ev.version}（本会话仍锁定基线 ${state.baseline?.version}，确认保存前请留意）`);
  }
});

/** 回首页并重载本会话的装配体（当前版本几何）。
 * 走 cacheKey 直载通道（首页 ?cacheKey= → GET /api/assembly/view）：
 * 不读源文件，源文件不在 allowed dirs 或已移动也能恢复预览。
 * （此前用 source_file 走 parse，裸文件名按服务 CWD 解析必失败。） */
function goHomeKeepAssembly(extra) {
  const params = { ...(state.cacheKey ? { cacheKey: state.cacheKey } : {}) };
  if (!state.cacheKey && bootLoadPath) params.load = bootLoadPath;
  goHome({ ...params, ...(extra || {}) });
}
$('#nav-home').addEventListener('click', () => {
  if (state.dirty
      && !confirm('有未保存的草稿步骤，返回首页将丢失这些修改。确定离开？')) return;
  goHomeKeepAssembly();
});
// 刷新/关闭守卫：dirty 时浏览器原生提示（location.href 导航不触发，由上面手动守卫）
window.addEventListener('beforeunload', (e) => {
  if (state.dirty) { e.preventDefault(); e.returnValue = ''; }
});
$('#nav-drawing').addEventListener('click', () => {
  const params = state.cacheKey ? { cacheKey: state.cacheKey } : {};
  goDrawing(params);
});

loadBaseline();
