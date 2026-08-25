// 编辑会话页（M2：草稿步骤表驱动草稿几何 + 增量干涉自动检查）
//
//  - 顶部会话条：基线 + 步骤数 + 放弃/保存草稿/确认保存
//  - 左栏：步骤列表（声明式，可增删 · 多目标）
//  - 中栏：双视口（基线锁定 + 草稿实时）+ 布局预设 + 预览范围
//  - 右栏：验证轨道（增量干涉 · 差异占位）
//  - 窄屏：底部抽屉

import '../style.css';
import { AssemblyScene } from '../scene.js';
import { installThemeControls } from '../shared/theme.js';
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
  // 装配级编辑：操作域（领域 A 零件编辑 / 领域 B 装配操作）+ 目标实例 + 换件身份
  opDomain: 'part',                 // 'part'（零件编辑）| 'assembly'（装配操作）
  targetInstance: { nodeId: null, templateId: null },  // 3D 点选选中的实例
  structureTarget: { nodeId: null, name: '', type: '' }, // 结构树点选的装配/分组
  replacedMap: {},                  // tid -> {name, source_cache_key}（换件后新零件名）
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
      // 无则照常自动触发一次 preview 把草稿几何 + 干涉刷出来。
      // 即便恢复了精检，若换件身份信息缺失（旧缓存无 replaced），仍补一次
      // preview 刷新新零件名并保证几何/身份与当前步骤一致。
      if (tryRestoreExact()) {
        if (!replacedInfoComplete()) schedulePreview();
      } else {
        schedulePreview();
      }
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
installThemeControls([sceneBaseline, sceneDraft], document.querySelector('.pane-tools'));
// 结构树点选：记录 structureTarget（供解散分组），并同步对应 3D 视口取景。
// treeDraft 额外启用拖拽（dnd）与分组工具条（toolbar）——结构重组只针对草稿侧。
/** 结构树点选：零件 → 高亮+取景+同步编辑区目标（与搜索一致）；装配/分组 → 仅取景。 */
function handleTreePick(tree, id) {
  const rec = id ? tree.nodes.get(id) : null;
  const node = rec?.node;
  state.structureTarget = node
    ? { nodeId: id, name: node.name || id, type: node.type }
    : { nodeId: null, name: '', type: '' };
  if (!id || !node) return;
  if (node.type === 'part') {
    highlightAndFocus([id], `结构树 → 高亮 ${node.name || id}`);
  } else {
    sceneBaseline.fitToIds([id]);
    if (!camSync) sceneDraft.fitToIds([id]);
  }
}
const treeBaseline = new AssemblyTree(document.getElementById('tree-base'), {
  onSelect: (id) => handleTreePick(treeBaseline, id),
  onToggle: () => {},
});
const treeDraft = new AssemblyTree(document.getElementById('tree-draft'), {
  onSelect: (id) => handleTreePick(treeDraft, id),
  onToggle: () => {},
  dnd: { onDrop: (nodeId, parentId) => {
    if (!state.baselineLoaded) return;
    const rec = treeDraft.nodes.get(nodeId);
    const tid = rec?.node?.template || null;
    upsertAssemblyStep(nodeId, tid, 'reparent', { parent_id: parentId },
      `${rec?.node?.name || nodeId}: 调层级 移至「${parentId}」`);
  } },
  toolbar: {
    onGroupCreate: (parentId) => {
      const gid = genGroupId();
      upsertAssemblyStep(gid, null, 'group_create',
        { name: '新分组', parent_id: parentId }, `新建分组「新分组」`);
    },
    onGroupDissolve: (nodeId) => {
      const rec = treeDraft.nodes.get(nodeId);
      upsertAssemblyStep(nodeId, null, 'group_dissolve', {},
        `解散分组「${rec?.node?.name || nodeId}」`);
    },
  },
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
// 结构树浮层开关：基线与草稿两视口的树同步显隐
const treeToggleBtn = document.getElementById('tree-toggle');
if (treeToggleBtn) {
  const toggleTree = () => {
    ['tree-base', 'tree-draft'].forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.classList.remove('hidden');
      el.classList.toggle('show');
    });
    treeToggleBtn.classList.toggle('active');
  };
  treeToggleBtn.addEventListener('click', toggleTree);
}

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

/** 前端兜底标题（后端 draft_step_title 的镜像，move/replace 用节点名）。 */
function stepFallbackTitle(s) {
  const p = s.params || {};
  if (s.operation === 'move') {
    const name = stepNodeName(s.node_id, s.template_id);
    return `${name}: move Δ(${p.dx ?? 0},${p.dy ?? 0},${p.dz ?? 0}) mm`;
  }
  if (s.operation === 'replace') {
    const name = stepNodeName(s.node_id, s.template_id);
    return `${name}: 换件 来源 ${String(p.source_cache_key || '?').slice(0, 6)}/${p.source_template_id || '?'}`;
  }
  if (s.operation === 'reparent') {
    const pid = p.parent_id || '?';
    return `${stepNodeName(s.node_id, s.template_id)}: 层级 移至「${pid}」`;
  }
  if (s.operation === 'group_create') return `新建分组「${p.name || s.node_id || '?'}」`;
  if (s.operation === 'group_dissolve') return `解散分组「${stepNodeName(s.node_id, s.template_id)}」`;
  return `${tplDisplayName(s.template_id, s.template_id)}: ${s.operation}`;
}

/** 装配步骤节点显示名（move/replace 的 node_id 优先，其次换件后模板名）。 */
function stepNodeName(nodeId, templateId) {
  const rec = nodeId ? treeBaseline.nodes.get(nodeId) : null;
  const nodeName = rec?.node?.name;
  if (nodeName) return nodeName;
  return tplDisplayName(templateId, templateId);
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

/** 步骤卡片统一数据：每张卡都按「类型 / 目标 / 详情」三段展示，保证各步骤风格一致。
 * 目标统一为可读名字 + 层级前缀：换件/特征是「类 <名>」，移动是「件 <名>」——
 * 换件改整类、移动改这一个实例，差异是语义使然而非混乱。 */
function stepCardData(s) {
  const p = s.params || {};
  if (s.operation === 'move') {
    return {
      kind: 'move', type: '移动',
      target: `件 ${stepNodeName(s.node_id, s.template_id)}`,
      detail: `Δ(${p.dx ?? 0}, ${p.dy ?? 0}, ${p.dz ?? 0})mm`,
    };
  }
  if (s.operation === 'replace') {
    return {
      kind: 'replace', type: '换件',
      target: `类 ${tplDisplayName(s.template_id, s.template_id)}`,
      detail: `对齐=${p.align || 'base'}`,
    };
  }
  if (s.operation === 'reparent') {
    const pid = p.parent_id || '?';
    const prec = treeBaseline.nodes.get(pid);
    const pname = prec?.node?.name || (pid.startsWith('g') ? `分组 ${pid}` : pid);
    return {
      kind: 'structure', type: '调层级',
      target: `件 ${stepNodeName(s.node_id, s.template_id)}`,
      detail: `移至「${pname}」`,
    };
  }
  if (s.operation === 'group_create') {
    return {
      kind: 'structure', type: '新建分组',
      target: p.name || s.node_id || '?',
      detail: '',
    };
  }
  if (s.operation === 'group_dissolve') {
    return {
      kind: 'structure', type: '解散分组',
      target: stepNodeName(s.node_id, s.template_id),
      detail: '子节点上提一级',
    };
  }
  const opLabel = (FEATURE_OPS[s.operation] || {}).label || s.operation;
  const target = `类 ${tplDisplayName(s.template_id, s.template_id)}`;
  const detail = s.feature_id ? `特征 ${s.feature_id}` : '';
  return { kind: 'feature', type: opLabel, target, detail };
}

/** 步骤分组键：仅同类且（move 用完全相同位移；replace 相同来源/对齐）可合并。
 * 几何/特征步骤是串行累积，不分组。 */
function stepGroupKey(s) {
  const p = s.params || {};
  if (s.operation === 'move') return `move:${p.dx ?? 0}:${p.dy ?? 0}:${p.dz ?? 0}`;
  if (s.operation === 'replace') return `replace:${p.source_cache_key}:${p.source_template_id}:${p.align}`;
  return `${s.operation}:${s.template_id}:${s.feature_id || ''}`;
}

/** 连续同键的 move 归并成一组（一行显示，避免 31 条同名 move 刷屏）。 */
function groupSteps(steps) {
  const runs = [];
  for (const s of steps) {
    const k = stepGroupKey(s);
    const last = runs[runs.length - 1];
    if (last && last.key === k && s.operation === 'move') last.items.push(s);
    else runs.push({ key: k, items: [s] });
  }
  return runs;
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
  let shown = 0;
  for (const run of groupSteps(state.draftSteps)) {
    if (run.items.length === 1) {
      box.appendChild(buildStepRow(run.items[0], shown + 1));
      shown += 1;
      continue;
    }
    // 分组行：连续同位移的多个 move → 统一卡片「类型/目标/详情」
    const p = run.items[0].params || {};
    const row = document.createElement('div');
    row.className = 'step step-assembly step-group';
    const top = document.createElement('div'); top.className = 'step-top';
    const no = document.createElement('span'); no.className = 'no';
    no.textContent = `${shown + 1}–${shown + run.items.length}`;
    const badge = document.createElement('span'); badge.className = 'step-type ty-move'; badge.textContent = '移动';
    const target = document.createElement('span'); target.className = 'step-target'; target.textContent = `${run.items.length} 件`;
    const names = run.items.map((s) => stepNodeName(s.node_id, s.template_id));
    const desc = document.createElement('div'); desc.className = 'step-desc';
    desc.textContent = `Δ(${p.dx ?? 0}, ${p.dy ?? 0}, ${p.dz ?? 0})mm · ${names.join('、')}`;
    const del = document.createElement('button'); del.className = 'del'; del.textContent = '×';
    del.title = '删除这一组步骤';
    del.addEventListener('click', (e) => {
      e.stopPropagation();
      const start = state.draftSteps.indexOf(run.items[0]);
      if (start >= 0) state.draftSteps.splice(start, run.items.length);
      state.dirty = true;
      markFeaStale();
      renderDraftSteps();
      updateSessionHeader();
      schedulePreview();
    });
    top.append(no, badge, target, del);
    row.append(top, desc);
    box.appendChild(row);
    shown += run.items.length;
  }
  // 镜像到窄屏抽屉
  if (box.id === 'steps-list' && $('#drawer-steps')) {
    renderDraftSteps($('#drawer-steps'));
  }
}

/** 渲染单条步骤（未归并）。所有步骤统一卡片：顶行[序号|类型|目标|删除] + 描述行(详情)。 */
function buildStepRow(s, ordinal) {
  const cd = stepCardData(s);
  const row = document.createElement('div'); row.className = 'step';
  const isAssembly = s.node_id && (s.operation === 'move' || s.operation === 'replace'
                                      || s.operation === 'reparent' || s.operation === 'group_dissolve');
  if (isAssembly) row.classList.add('step-assembly');
  const top = document.createElement('div'); top.className = 'step-top';
  const no = document.createElement('span'); no.className = 'no'; no.textContent = `${ordinal}`;
  const badge = document.createElement('span'); badge.className = `step-type ty-${cd.kind}`; badge.textContent = cd.type;
  const target = document.createElement('span'); target.className = 'step-target'; target.textContent = cd.target;
  const del = document.createElement('button'); del.className = 'del'; del.textContent = '×';
  del.title = '删除此步骤';
  del.addEventListener('click', (e) => {
    e.stopPropagation();
    const idx = state.draftSteps.indexOf(s);
    if (idx >= 0) state.draftSteps.splice(idx, 1);
    state.dirty = true;
    markFeaStale();
    renderDraftSteps();
    updateSessionHeader();
    schedulePreview();
  });
  top.append(no, badge, target, del);
  row.append(top);
  if (cd.detail) {
    const desc = document.createElement('div'); desc.className = 'step-desc';
    desc.textContent = cd.detail;
    row.append(desc);
  }
  // 装配步骤：点击卡片定位到该实例（双视口取景）
  if (isAssembly && s.node_id) {
    row.title = '点击定位到该零件';
    row.addEventListener('click', (e) => {
      if (e.target.closest('.del')) return;
      sceneBaseline.fitToIds([s.node_id]);
      if (!camSync) sceneDraft.fitToIds([s.node_id]);
    });
  }
  return row;
}

// ==========================================================================
// M3：操作表单（目标模板 → 粒度 → 特征 → 操作 → 参数 级联）
// 操作契约与后端 apply_template_edit / apply_feature_edit 一一对应
// ==========================================================================

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

// 领域 B · 装配操作（实例级：针对 3D 点选的单个零件实例）。
// replace/move 步骤带 node_id；template_id 为实例解引用（后端 replay 用）。
const ASSEMBLY_OPS = {
  replace: {
    label: '换件（用其它文件零件）', instance: true,
    fields: [
      { key: 'source', label: '来源零件', type: 'source', hint: '从另一已打开文件选中的零件' },
      { key: 'align', label: '对齐方式', type: 'align', def: 'base' },
      { key: 'dx', label: 'X 偏移 (mm)', type: 'num', def: 0 },
      { key: 'dy', label: 'Y 偏移 (mm)', type: 'num', def: 0 },
      { key: 'dz', label: 'Z 偏移 (mm)', type: 'num', def: 0 },
    ],
  },
  move: {
    label: '移动（dx/dy/dz 绝对位移）', instance: true,
    fields: [
      { key: 'dx', label: 'X (mm)', type: 'num', def: 0 },
      { key: 'dy', label: 'Y (mm)', type: 'num', def: 0 },
      { key: 'dz', label: 'Z (mm)', type: 'num', def: 0 },
    ],
  },
  remove: {
    label: '删除零件', instance: true,
    fields: [],
  },
  reparent: {
    label: '调整层级（移到其它装配/分组下）', instance: true, structure: true,
    fields: [
      { key: 'parent_id', label: '新父节点', type: 'parent', hint: '选父装配/分组；也可在结构树直接拖拽' },
    ],
  },
  group_create: {
    label: '新建分组', instance: true, structure: true,
    fields: [
      { key: 'name', label: '分组名', type: 'text', def: '新分组' },
      { key: 'parent_id', label: '放置位置', type: 'parent-base', hint: '在哪个装配下新建空分组' },
    ],
  },
  group_dissolve: {
    label: '解散分组（子节点上提）', instance: true, structure: true,
    fields: [],
  },
};

function setFormHint(text, level = 'info') {
  const el = $('#sf-hint');
  el.className = `sf-hint ${level === 'info' ? '' : level}`.trim();
  el.textContent = text;
}

function currentDomain() {
  return state.opDomain;   // 'part'（零件编辑=定点特征）| 'assembly'（装配操作）
}

/** 零件编辑统一为特征级编辑（已去掉"整件"层）。 */
function currentGranularity() {
  return 'feature';
}

function currentOpDef() {
  const key = $('#sf-operation').value;
  if (currentDomain() === 'assembly') return ASSEMBLY_OPS[key];
  return FEATURE_OPS[key];
}

/** 目标实例的显示名（换件后取新零件名；否则取装配树节点名）。 */
function targetInstanceName() {
  const { nodeId, templateId } = state.targetInstance;
  if (!nodeId && !templateId) return '';
  const rec = nodeId ? treeBaseline.nodes.get(nodeId) : null;
  const nodeName = rec?.node?.name || (nodeId ? nodeId : '');
  const replaced = state.replacedMap[templateId];
  const name = replaced?.name || nodeName || '';
  const clsName = templateId ? tplDisplayName(templateId, templateId) : '';
  const parts = [];
  if (name) parts.push(name);
  if (clsName && clsName !== name) parts.push(`类${clsName}`);
  return parts.join(' · ');
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
  let ops = {};
  if (currentDomain() === 'assembly') {
    ops = ASSEMBLY_OPS;
  } else {
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

/** 从「其它已打开文件」读被选中的零件，作为换件来源（排除当前 cache_key）。 */
async function loadReplaceSources() {
  try {
    const r = await fetch(`/api/selection?all=1`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    });
    if (!r.ok) return [];
    const data = await r.json();
    const items = data?.selections || [];
    return items.filter((s) => s.cache_key && s.cache_key !== state.cacheKey)
      .map((s) => ({ cache_key: s.cache_key, template_id: s.template_id, node_id: s.node_id, name: s.template_name || s.node_name || s.node_id }));
  } catch { return []; }
}

function renderParams() {
  const box = $('#sf-params');
  box.innerHTML = '';
  const def = currentOpDef();
  if (!def) return;
  // 装配操作：来源选择 + 对齐下拉 + 数值偏移
  if (currentDomain() === 'assembly') {
    renderAssemblyParams(box, def);
    return;
  }
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

/** 装配操作域的参数渲染（当前目标实例 + replace 来源/对齐 + move dx/dy/dz）。 */
function renderAssemblyParams(box, def) {
  // 目标实例状态条（只读）
  const target = document.createElement('div');
  target.className = 'sf-target'; target.id = 'sf-p-target';
  const { nodeId } = state.targetInstance;
  if (!nodeId) {
    target.textContent = '⚠ 未选中零件：请先在 3D 视口点击要操作的零件';
    target.classList.add('warn');
  } else {
    target.textContent = `目标：${targetInstanceName()}`;
  }
  box.appendChild(target);

  for (const f of def.fields) {
    if (f.type === 'source') {
      const row = document.createElement('div'); row.className = 'sf-row';
      const lab = document.createElement('label'); lab.textContent = f.label;
      const sel = document.createElement('select'); sel.id = `sf-p-${f.key}`;
      sel.innerHTML = '<option value="">（请先在其它文件选中零件）</option>';
      row.append(lab, sel);
      box.appendChild(row);
      // 异步填充可换来源
      loadReplaceSources().then((srcs) => {
        if (!srcs.length) {
          const o = document.createElement('option');
          o.value = ''; o.textContent = '（未检测到其它文件选中的零件）';
          sel.appendChild(o);
          return;
        }
        srcs.forEach((s) => {
          const o = document.createElement('option');
          o.value = `${s.cache_key}::${s.template_id}::${s.node_id || ''}`;
          o.textContent = `${s.name}（${s.cache_key.slice(0, 6)}…）`;
          sel.appendChild(o);
        });
      });
    } else if (f.type === 'align') {
      const row = document.createElement('div'); row.className = 'sf-row';
      const lab = document.createElement('label'); lab.textContent = f.label;
      const sel = document.createElement('select'); sel.id = `sf-p-${f.key}`;
      [['base', '底面对齐（加高版）'], ['top', '顶面对齐'], ['origin', '原点对齐'], ['center', '中心对齐'], ['seat', '接合面对齐']].forEach(([v, t]) => {
        const o = document.createElement('option'); o.value = v; o.textContent = t;
        sel.appendChild(o);
      });
      sel.value = f.def || 'base';
      row.append(lab, sel);
      box.appendChild(row);
    } else if (f.type === 'num') {
      const row = document.createElement('div'); row.className = 'sf-row';
      const lab = document.createElement('label'); lab.textContent = f.label;
      const input = document.createElement('input');
      input.type = 'number'; input.step = 'any'; input.value = f.def ?? 0;
      input.id = `sf-p-${f.key}`;
      row.append(lab, input);
      box.appendChild(row);
    } else if (f.type === 'text') {
      const row = document.createElement('div'); row.className = 'sf-row';
      const lab = document.createElement('label'); lab.textContent = f.label;
      const input = document.createElement('input');
      input.type = 'text'; input.value = f.def ?? '';
      input.id = `sf-p-${f.key}`;
      row.append(lab, input);
      box.appendChild(row);
    } else if (f.type === 'parent' || f.type === 'parent-base') {
      // 「新父节点」下拉：枚举草稿结构树中的装配节点（自动降级到基线树）。
      // parent       —— reparent：排除目标自身及后代
      // parent-base —— group_create：任意装配（含根）均可作为放置位置
      const row = document.createElement('div'); row.className = 'sf-row';
      const lab = document.createElement('label'); lab.textContent = f.label;
      const sel = document.createElement('select'); sel.id = `sf-p-${f.key}`;
      row.append(lab, sel);
      box.appendChild(row);
      const exclude = f.type === 'parent' ? (state.targetInstance.nodeId || null) : null;
      populateParentOptions(sel, exclude);
    }
  }
}

/**
 * 用当前草稿结构树（未 preview 时回退基线树）填充装配节点下拉。
 * @param excludeId reparent 时排除目标自身及其后代（不能移入自身/后代下）
 */
function populateParentOptions(sel, excludeId) {
  const src = treeDraft.nodes.size ? treeDraft.nodes : treeBaseline.nodes;
  const items = [];
  const isDescOf = (id, anc) => {
    let cur = id;
    while (cur) {
      if (cur === anc) return true;
      cur = (src.get(cur)?.parentId) ?? null;
    }
    return false;
  };
  const rootId = [...src.values()].find((r) => r.parentId == null)?.node?.id;
  for (const [id, rec] of src) {
    if (rec.node.type !== 'assembly') continue;
    if (id === excludeId || (excludeId && isDescOf(id, excludeId))) continue;
    const depth = (() => {
      let d = 0, cur = rec.parentId;
      while (cur) { d++; cur = src.get(cur)?.parentId ?? null; }
      return d;
    })();
    const indent = '　'.repeat(Math.min(depth, 6));
    const isRoot = id === rootId;
    items.push({ id, label: `${indent}${rec.node.name || id}${isRoot ? '（根）' : ''}` });
  }
  sel.innerHTML = '<option value="">（请选择父装配）</option>';
  for (const it of items) {
    const o = document.createElement('option');
    o.value = it.id; o.textContent = it.label;
    sel.appendChild(o);
  }
}

async function loadFeaturesFor(tid) {
  if (state.featuresCache.has(tid)) return state.featuresCache.get(tid);
  const tpl = state.baseline?.manifest?.templates.find((t) => t.id === tid);
  // 换件后的模板：特征直接指向来源 cache（buffer 自洽，避免拷贝冲突/旧缓存）
  const rep = state.replacedMap[tid];
  if (rep?.source_cache_key && rep.source_template_id) {
    const url = `/cache/${rep.source_cache_key}/features/${rep.source_template_id}.json`;
    try {
      const r = await fetch(url);
      const feats = r.ok ? await r.json() : [];
      state.featuresCache.set(tid, Array.isArray(feats) ? feats : []);
    } catch { state.featuresCache.set(tid, []); }
    return state.featuresCache.get(tid);
  }
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
  // 特征行只在「零件编辑」域显示；装配域（实例级操作）不出现目标特征
  row.hidden = currentDomain() === 'assembly';
  if (row.hidden) return;
  sel.innerHTML = '';
  // 占位项：未选特征时 currentFeature()=null，保证"零件级选中"与"特征级选中"互斥、数据不混淆
  const ph = document.createElement('option');
  ph.value = ''; ph.textContent = '（在某特征上点击选择）';
  sel.appendChild(ph);
  const feats = await loadFeaturesFor(state.targetTemplateId);
  if (!feats.length) {
    setFormHint('该模板没有可编辑特征', 'error');
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
  else sel.value = '';   // keep 不可用 → 回到占位（未选特征）
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

/** 更新操作域表单显隐 + 目标行 + 操作列表。
 * 零件编辑=定点特征：模板/特征 行显示；装配操作域：只留目标行。 */
function applyDomainFieldVisibility() {
  const assembly = currentDomain() === 'assembly';
  $('#sf-template-row').hidden = assembly;   // 目标模板：仅零件编辑域
  $('#sf-feature-row').hidden = assembly;     // 目标特征：仅零件编辑域
  $('#sf-target-row').hidden = false;         // 目标行：两域通用（跟随 3D）
}

function renderDomainForm() {
  applyDomainFieldVisibility();
  refreshTargetRow();
  // 域切换后重渲染操作集；特征相关高亮仅在零件编辑域有意义
  renderOperations();
  if (currentDomain() !== 'assembly') syncFeatureHighlight();
}

function refreshTargetRow() {
  const el = $('#sf-target');
  if (el) el.textContent = targetInstanceName() || '— 请先在 3D 视口点击选择一个零件 —';
}

// 操作域分段切换
document.querySelectorAll('#sf-domain button').forEach((b) => {
  b.addEventListener('click', () => {
    document.querySelectorAll('#sf-domain button').forEach((x) => x.classList.remove('active'));
    b.classList.add('active');
    state.opDomain = b.dataset.domain;
    // 装甲装配域时，默认预选 move（不强制要求来源）；进入时目标行刷新
    renderDomainForm();
  });
});

function initStepForm() {
  const tplSel = $('#sf-template');
  tplSel.innerHTML = '';
  for (const t of state.baseline.manifest.templates) {
    const o = document.createElement('option');
    o.value = t.id;
    o.textContent = `${tplDisplayName(t.id, t.name)} (${t.id})`;
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
$('#sf-feature').addEventListener('change', () => {
  renderOperations();
  syncFeatureHighlight();
});
$('#sf-operation').addEventListener('change', () => renderParams());

/** 模板显示名：换件后返回新零件名，否则原模板名。 */
function tplDisplayName(tid, fallback) {
  const rep = state.replacedMap[tid];
  return rep?.name ? rep.name : (fallback || tid);
}

/** 应用换件身份到 draft：记录 replacedMap、给树节点改名/标「已替换」、刷新下拉与目标。
 *  同时把被替换模板的「特征数据/overlay」重指向来源 cache（不拷贝进目标缓存，
 *  避免 buffer 撞名与旧缓存问题）。还必须在本函数内、sceneDraft.load 之前完成。 */
function applyDraftReplaced(res) {
  if (res?.replaced) state.replacedMap = { ...state.replacedMap, ...res.replaced };
  const mani = res?.manifest;
  if (mani?.root && Object.keys(state.replacedMap).length) {
    (function walk(n) {
      if (n && n.type === 'part' && n.template) {
        const rep = state.replacedMap[n.template];
        if (rep?.name) n.name = rep.name;
        if (rep) n.replaced = true;
      }
      (n.children || []).forEach(walk);
    })(mani.root);
    // 特征数据/overlay 重指向来源：manifest.features → 绝对源路径（scene._featureGltf 据此加载）
    if (Array.isArray(mani.templates)) {
      for (const t of mani.templates) {
        const rep = state.replacedMap[t.id];
        if (rep?.source_cache_key && rep.source_template_id) {
          t.features = `/cache/${rep.source_cache_key}/features/${rep.source_template_id}.json`;
        }
      }
    }
    // 基线场景同模板也改指向来源，避免它读到目标缓存里被换件重写过的旧 tN 特征
    if (state.baseline && Array.isArray(state.baseline.manifest?.templates)) {
      for (const t of state.baseline.manifest.templates) {
        const rep = state.replacedMap[t.id];
        if (rep?.source_cache_key && rep.source_template_id) {
          t.features = `/cache/${rep.source_cache_key}/features/${rep.source_template_id}.json`;
        }
      }
    }
  }
  refreshTemplateLabels();
  refreshTargetRow();
  // 换件后该模板特征已重指向来源：清除旧缓存，目标被换件则重载特征列表/操作
  const replacedTids = Object.keys(state.replacedMap).filter(
    (tid) => res?.replaced?.[tid]);
  if (replacedTids.length) {
    replacedTids.forEach((tid) => {
      state.featuresCache.delete(tid);
      sceneBaseline.clearFeatureGltf(tid);
      sceneDraft.clearFeatureGltf(tid);
      // 草稿场景叠加换件对齐平移（overlay 贴到新零件）；基线场景不加（保持未平移参照）
      const off = state.replacedMap[tid]?.offset;
      sceneDraft.setOverlayOffset(tid, off || null);
      sceneBaseline.setOverlayOffset(tid, null);
    });
    if (state.targetTemplateId && replacedTids.includes(state.targetTemplateId)) {
      renderFeatureOptions();
    }
  }
}

/** 刷新模板下拉的显示名（换件后用新零件名），保持当前选中值不变。 */
function refreshTemplateLabels() {
  const sel = $('#sf-template');
  if (!sel) return;
  const cur = sel.value;
  for (const opt of sel.options) {
    const t = state.baseline?.manifest?.templates.find((t) => t.id === opt.value);
    if (t) opt.textContent = `${tplDisplayName(t.id, t.name)} (${t.id})`;
  }
  if (cur) sel.value = cur;
}

/** 换件身份是否已就绪：每个 replace 步骤的模板都有新零件名。 */
function replacedInfoComplete() {
  const tidSet = new Set();
  for (const s of state.draftSteps || []) {
    if (s.operation === 'replace' && s.template_id) tidSet.add(s.template_id);
  }
  if (!tidSet.size) return true;
  return [...tidSet].every((tid) => !!state.replacedMap[tid]?.name);
}

/** 放弃/删除草稿后清除换件身份，树/下拉/目标回到基线名。 */
function resetDraftIdentity() {
  state.replacedMap = {};
  state.targetInstance = { nodeId: null, templateId: null };
  sceneDraft.overlayOffsets.clear();   // 清除换件对齐偏移（overlay 回到未平移）
  if (state.baseline) {
    treeDraft.render(state.baseline.manifest.root);
  }
  refreshTemplateLabels();
  refreshTargetRow();
  renderDraftSteps();
}

$('#btn-add-step').addEventListener('click', () => {
  if (!state.baselineLoaded || !state.targetTemplateId) {
    setFormHint('等待基线加载…', 'error');
    return;
  }
  const opKey = $('#sf-operation').value;
  // 领域 B：装配操作（实例级）
  if (currentDomain() === 'assembly') {
    addAssemblyStep(opKey);
    return;
  }
  // 领域 A：零件编辑 = 定点特征
  const def = FEATURE_OPS[opKey];
  if (!def) { setFormHint('请先选择操作', 'error'); return; }
  const feat = currentFeature();
  if (!feat) {
    setFormHint('请先在 3D 视口点选零件上的一个特征', 'error');
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
  const tplName = tplDisplayName(state.targetTemplateId,
    state.baseline.manifest.templates.find((t) => t.id === state.targetTemplateId)?.name);
  stepSeq++;
  const step = normalizeStep({
    id: `s${Date.now()}_${stepSeq}`,
    template_id: state.targetTemplateId,
    operation: opKey,
    params,
    feature_id: feat.id,
    title: `${tplName} ${feat.id}: ${def.label}`,
  });
  state.draftSteps.push(step);
  state.dirty = true;
  markFeaStale();
  renderDraftSteps();
  updateSessionHeader();
  setFormHint(`已添加：${step.title}`, 'ok');
  schedulePreview();
});

/** 领域 B：生成/覆盖一条装配级步骤（replace / move），作用于选中的实例。 */
function addAssemblyStep(opKey) {
  const def = ASSEMBLY_OPS[opKey];
  if (!def) { setFormHint('请先选择操作', 'error'); return; }
  const readNum = (key) => {
    const el = document.getElementById(`sf-p-${key}`);
    const v = el ? parseFloat(el.value) : 0;
    return Number.isFinite(v) ? v : 0;
  };

  // 结构操作 · 新建分组：无需 3D 点选，只需 分组名 + 放置父装配
  if (opKey === 'group_create') {
    const nameEl = document.getElementById('sf-p-name');
    const parentEl = document.getElementById('sf-p-parent_base');
    const name = (nameEl?.value || '').trim() || '新分组';
    const parentId = parentEl?.value || null;
    if (!parentId) { setFormHint('请选择分组放置位置（父装配）', 'error'); return; }
    const gid = genGroupId();
    upsertAssemblyStep(gid, null, 'group_create', { name, parent_id: parentId },
      `新建分组「${name}」`);
    return;
  }
  // 结构操作 · 解散分组：目标为草稿结构树中点选的分组
  if (opKey === 'group_dissolve') {
    const st = state.structureTarget || {};
    if (!st.nodeId) { setFormHint('请先在草稿结构树点选要解散的分组', 'error'); return; }
    upsertAssemblyStep(st.nodeId, null, 'group_dissolve', {},
      `解散分组「${st.name || st.nodeId}」`);
    return;
  }

  // 以下为实例级操作（move/remove/replace/reparent）：需 3D 选中目标实例
  const { nodeId, templateId } = state.targetInstance;
  if (!nodeId || !templateId) {
    setFormHint('请先在 3D 视口点选目标零件', 'error');
    return;
  }
  // 结构操作 · 调整层级：把 3D 点选的零件移到指定父装配下
  if (opKey === 'reparent') {
    const parentEl = document.getElementById('sf-p-parent_id');
    const parentId = parentEl?.value || null;
    if (!parentId) { setFormHint('请选择新父节点', 'error'); return; }
    upsertAssemblyStep(nodeId, templateId, 'reparent', { parent_id: parentId },
      `${targetInstanceName()}: 调层级 移至「${parentId}」`);
    return;
  }
  if (opKey === 'move') {
    const params = { dx: readNum('dx'), dy: readNum('dy'), dz: readNum('dz') };
    upsertAssemblyStep(nodeId, templateId, 'move', params,
      `${targetInstanceName()}: move Δ(${params.dx},${params.dy},${params.dz}) mm`);
    return;
  }
  if (opKey === 'remove') {
    upsertAssemblyStep(nodeId, templateId, 'remove', {},
      `${targetInstanceName()}: 删除`);
    return;
  }
  // replace
  const srcSel = document.getElementById('sf-p-source');
  const src = srcSel?.value || '';
  if (!src || !src.includes('::')) {
    setFormHint('请先在其它文件选中一个零件作为换件来源', 'error');
    return;
  }
  const [srcKey, srcTid] = src.split('::');
  const params = {
    source_cache_key: srcKey,
    source_template_id: srcTid,
    align: document.getElementById('sf-p-align')?.value || 'base',
    dx: readNum('dx'), dy: readNum('dy'), dz: readNum('dz'),
  };
  upsertAssemblyStep(nodeId, templateId, 'replace', params,
    `${targetInstanceName()}: 换件 ← 来源 ${srcKey.slice(0, 6)}/${srcTid}`);
}

/** 生成不与现有节点冲突的新分组 id（g1/g2…，防与装配节点 n\d+ 撞名）。 */
let groupSeq = 0;
function genGroupId() {
  let g;
  do { g = `g${++groupSeq}`; }
  while (treeDraft.nodes.has(g) || treeBaseline.nodes.has(g)
         || state.draftSteps.some((s) => s.node_id === g));
  return g;
}

/** 同实例装配步骤后写覆盖（replace/move/reparent/group_dissolve 各一条）。 */
function upsertAssemblyStep(nodeId, templateId, operation, params, title) {
  const i = state.draftSteps.findIndex(
    (s) => s.operation === operation && s.node_id === nodeId);
  const base = i >= 0 ? state.draftSteps[i] : null;
  const step = normalizeStep({
    id: base?.id || `s${Date.now()}_${++stepSeq}`,
    template_id: templateId,
    node_id: nodeId,
    operation,
    params,
    title,
  });
  if (base) { state.draftSteps[i] = step; }
  else { state.draftSteps.push(step); }
  state.dirty = true;
  markFeaStale();
  renderDraftSteps();
  updateSessionHeader();
  setFormHint(`已${base ? '更新' : '添加'}：${title}`, 'ok');
  schedulePreview();
}

// ==========================================================================
// 草稿重放 + 增量干涉（自动）
// ==========================================================================
function schedulePreview() {
  // debounce：连续编辑不频繁请求
  if (state.previewTimer) clearTimeout(state.previewTimer);
  state.previewTimer = setTimeout(() => runPreview('bbox'), 250);
}

/** 草稿预览 + 干涉检查（仅提醒，不拦保存）。
 * level='bbox'（默认）：AABB 快速反馈，步骤每次变更自动触发（毫秒级，
 * 拖拽调位不卡）；level='exact'：布尔精检，显式按钮触发，给用户自查。 */
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
    applyDraftReplaced(res);   // 换件身份：replacedMap + 树改名/「已替换」标记
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
      // 服务端返回干涉数据：仅展示，不拦草稿
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
    // 把步骤哈希写入结果自身：恢复/保留精检时用它精确判定是否过期
    const hashed = { ...res, _hash: draftStepsHash(state.draftSteps) };
    sessionStorage.setItem(
      VERIFY_STORE_PREFIX + state.cacheKey,
      JSON.stringify({ hash: hashed._hash, res: hashed }),
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
  applyDraftReplaced(res);   // 恢复换件身份（树改名/「已替换」/下拉）
  // 过期判定：以结果自身携带的步骤哈希为准（精确，不因后续 bbox 误标）
  const curHash = draftStepsHash(state.draftSteps);
  state.verifyStale = !!(res._hash && res._hash !== curHash);
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
    // 继续展示精检列表（逐处处理干涉不丢）；仅按其步骤哈希精确标记过期
    res = state.exactResult;
    state.verifyStale = !!(res._hash && res._hash !== draftStepsHash(state.draftSteps));
} else {
    state.verifyStale = false;
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

// 精确检查按钮：显式触发布尔精检（自动粗筛只做 AABB 提示）
// #verify-reset 由验证面板动态渲染（renderVerifyPane），此处仅静态校验
// #verify-exact；用可选链避免静态顶栏无对应元素时初始化崩溃。
$('#verify-exact')?.addEventListener('click', () => runPreview('exact'));
$('#verify-reset')?.addEventListener('click', () => resetVerify());

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
  resetDraftIdentity();   // 清除换件身份（replacedMap/树标记/下拉）
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
  if (!confirm('确认将草稿步骤全部落为一条版本？')) return;
  const btn = $('#sess-confirm');
  btn.disabled = true;   // 防重复提交（成功窗口内连点 = 落两个版本）
  statusFn('提交中…');
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
    statusFn(`提交失败：${err.message}`, true);
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
// 步骤搜索：按模板id/模板名/零件名查找模板与零件概要，点击在模型上高亮并取景
// ==========================================================================
function searchTemplates(q) {
  if (!q || !state.baseline?.manifest?.templates) return [];
  const lq = q.toLowerCase();
  const out = [];
  for (const t of state.baseline.manifest.templates) {
    const id = String(t.id);
    const name = tplDisplayName(t.id, t.name || id);
    if (id.toLowerCase().includes(lq) || name.toLowerCase().includes(lq)) {
      out.push({ kind: 'template', id: t.id, label: `${name} (${t.id})` });
    }
  }
  return out;
}

function searchParts(q) {
  if (!q) return [];
  const lq = q.toLowerCase();
  const out = [];
  for (const [nid, rec] of treeBaseline.nodes) {
    if (rec.node.type !== 'part') continue;
    const nm = String(rec.node.name || '');
    const tid = rec.node.template;
    const tidRep = tid ? tplDisplayName(tid, tid) : '';
    if (nm.toLowerCase().includes(lq) || String(nid).toLowerCase().includes(lq)
        || (tidRep && tidRep.toLowerCase().includes(lq))) {
      out.push({ kind: 'part', nodeId: nid, templateId: tid, label: nm || nid });
    }
  }
  return out;
}

/**
 * 把一组零件设为当前目标并在双视口高亮+取景，同步编辑区目标。
 * 搜索命中与结构树点选共用此入口，保证两侧行为一致。
 */
function highlightAndFocus(ids, label) {
  if (!ids || !ids.length) return;
  // 先把视图焦点设到目标零件：让「子装配/零件」视图范围按它计算生效
  state.focus = { level: 'part', nodeId: ids[0] };
  updateRangeButtons();
  reapplyViewFilter();          // 应用该焦点的范围可见性（同时清除旧高亮）
  // 同步编辑区「目标」到该零件：让装配目标/零件编辑的模板/特征都跟随
  const tId0 = sceneBaseline.templateOf(ids[0]);
  state.targetInstance = { nodeId: ids[0], templateId: tId0 || null };
  treeBaseline.select(ids[0]);
  treeDraft.select(ids[0]);
  if (tId0) setTargetTemplate(tId0);
  refreshTargetRow();
  renderParams();
  sceneBaseline.highlight(new Set(ids));
  sceneDraft.highlight(new Set(ids));
  // 取景略远一点（留些余量），避免贴得太近
  sceneBaseline.fitToIds(ids, 1.35);
  if (!camSync) sceneDraft.fitToIds(ids, 1.35);
  statusFn(label);
}

/** 渲染搜索结果：模板组 + 零件组；点击高亮并取景。 */
function renderSearch(query) {
  const box = $('#step-search-results');
  if (!box) return;
  box.innerHTML = '';
  const q = (query || '').trim();
  if (!q || !state.baselineLoaded) { box.hidden = true; return; }
  const tmpls = searchTemplates(q);
  const parts = searchParts(q);
  const addHeader = (txt) => {
    const h = document.createElement('div'); h.className = 'src-hdr'; h.textContent = txt;
    box.appendChild(h);
  };
  const addRow = (label, onClick) => {
    const r = document.createElement('div'); r.className = 'src-row'; r.textContent = label;
    r.addEventListener('click', onClick);
    box.appendChild(r);
  };
  if (tmpls.length) {
    addHeader(`模板 ${tmpls.length}`);
    tmpls.forEach((t) => {
      const ids = [...sceneBaseline.instances.keys()].filter(
        (id) => sceneBaseline.templateOf(id) === t.id);
      addRow(`类 ${t.label}`, () => highlightAndFocus(ids, `搜索 → 高亮 ${t.label}`));
    });
  }
  if (parts.length) {
    addHeader(`零件 ${parts.length}（可能包含子/合集匹配）`);
    parts.forEach((p) => {
      addRow(`件 ${p.label}`, () => highlightAndFocus([p.nodeId], `搜索 → 高亮 ${p.label}`));
    });
  }
  // 防止结果列表过多撑爆侧栏
  if (box.children.length > 40) { box.hidden = true; addHeader('结果过多，请更精确输入'); box.hidden = false; }
  box.hidden = !(tmpls.length || parts.length);
}

const stepSearchInput = $('#step-search');
if (stepSearchInput) {
  let srchTimer = null;
  stepSearchInput.addEventListener('input', () => {
    clearTimeout(srchTimer);
    srchTimer = setTimeout(() => renderSearch(stepSearchInput.value), 150);
  });
  stepSearchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { stepSearchInput.value = ''; renderSearch(''); }
  });
}

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

/** 点击空白：清除「选中/目标」，但保留当前视图锚点（focus/range）——
 *  在子装配/零件视图里取消选中不应跳回整装配。 */
function clearSelection() {
  treeBaseline.select(null);
  treeDraft.select(null);
  state.targetInstance = { nodeId: null, templateId: null };
  updateRangeButtons();
  reapplyViewFilter();           // 保留当前范围可见性，仅清除装配高亮
  sceneBaseline.showFeature(null, null);   // 清除特征 overlay
  sceneDraft.showFeature(null, null);
  if (state.moveMode) sceneDraft.disableMove();
  refreshTargetRow();
  renderParams();                // 目标条回到"未选中"提示
  statusFn('已清除选中');
}

async function handlePick(id, ndc, srcScene) {
  if (!id) { clearSelection(); return; }
  treeBaseline.select(id);
  treeDraft.select(id);
  state.focus = { level: 'part', nodeId: id };
  updateRangeButtons();
  if (state.range !== 'root' && state.range !== 'assembly' && state.range !== 'part') {
    state.range = 'root';
  }
  const tid = srcScene?.templateOf?.(id) ?? sceneBaseline.templateOf(id);
  // 目标实例：两域都依据 3D 点选（装配域 replace/move、零件编辑域的目标模板/特征都跟随它）
  state.targetInstance = { nodeId: id, templateId: tid || null };
  if (tid) setTargetTemplate(tid);   // 内部含 reapplyViewFilter（模板未变时提前返回）
  reapplyViewFilter();               // 焦点变化影响范围过滤，统一重应用（幂等）
  // 点选反馈：两域都高亮被点中的零件（reapplyViewFilter 已清除，这里重染）。
  // 这解决"默认零件编辑域点零件无反馈、看似选不了"的问题。
  {
    const sel = new Set([id]);
    sceneBaseline.highlight(sel);
    sceneDraft.highlight(sel);
  }
  refreshTargetRow();   // 目标行跟随（两域常显）
  renderParams();       // 重建 in-params 目标条
  applyDomainFieldVisibility();   // 兜底：装配域点选后也确保模板/特征行保持隐藏
  if (state.moveMode) sceneDraft.enableMove([id]);   // 移动模式跟随点选换目标
  // 一次点击只产生「一个」确定选中，避免零件/特征两层状态与上行数据混淆：
  //  - 装配域：选中=实例（默认选中色高亮）
  //  - 零件域：选中=特征（严格特征优先：pickFeatureAt 命中或最近兜底，恒有特征）
  //  - 零件域该类无任何特征 → 仅设目标、不高亮（极罕见）
  let featId = null;
  if (ndc && currentDomain() === 'part' && srcScene) {
    featId = await srcScene.pickFeatureAt(ndc, id);
  }
  if (currentDomain() === 'assembly') {
    const sel = new Set([id]);
    sceneBaseline.highlight(sel);
    sceneDraft.highlight(sel);
  } else if (featId) {
    sceneBaseline.highlight(null);
    sceneDraft.highlight(null);
    pendingFeature = { templateId: tid, featureId: featId };
    applyPendingFeature();
  } else {
    sceneBaseline.highlight(null);
    sceneDraft.highlight(null);
    sceneBaseline.showFeature(null, null);
    sceneDraft.showFeature(null, null);
    pendingFeature = null;
    const fs = $('#sf-feature'); if (fs) fs.value = '';
    renderOperations();
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
      resetDraftIdentity();   // 清除换件身份
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
