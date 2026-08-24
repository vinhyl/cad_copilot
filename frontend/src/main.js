import './style.css';
import { AssemblyScene } from './scene.js';
import { AssemblyTree } from './tree.js';
import {
  getToken, setToken, parseAssembly, editAssembly, listVersions,
  checkoutVersion, auditAssembly, importDrawing,
  getPlugins, getConfig, uploadFile, startFeaJob, startRenderJob,
  getJob, cancelJob,
} from './api.js';

// ---- URL 一次性参数引导（本地单用户工具约定）----
// ?token= 注入 localStorage；?load=<encodeURIComponent(路径)> 供 agent
// 生成可点击预览链接（文件末尾按扩展名路由装配/图纸）。URLSearchParams
// 已解码一次，这里不再二次 decode——agent 侧须用 encodeURIComponent 编码。
const params = new URLSearchParams(location.search);
let bootLoadPath = null;
let urlDirty = false;
if (params.get('token')) {
  setToken(params.get('token'));
  params.delete('token');
  urlDirty = true;
}
if (params.get('load')) {
  bootLoadPath = params.get('load');
  params.delete('load');
  urlDirty = true;
}
if (urlDirty) {
  const qs = params.toString();
  history.replaceState(null, '', location.pathname + (qs ? `?${qs}` : ''));
}
if (!getToken()) {
  const t = window.prompt('服务 token（cad_service 启动时打印）：');
  if (t) setToken(t);
}

// ---- 服务配置：可访问目录（错误文案用） ----
// 输入已全部走上传/拖放/最近使用（显式授权通道），路径手输不再存在；
// 服务端 safe_input_path 仍是权威校验，这里只为 403 兜底文案提供目录列表。
let allowedDirs = [];

function pathDeniedMsg() {
  return '路径不在服务可访问目录内。请将文件移入：'
    + (allowedDirs.join('；') || '（未知）')
    + '；或重启服务时设置环境变量 CAD_SERVICE_ALLOWED_DIRS 添加目录（多个用 ; 分隔）';
}

async function refreshConfig() {
  try {
    allowedDirs = (await getConfig()).allowed_dirs || [];
  } catch { /* 配置获取失败不阻塞主流程 */ }
}
refreshConfig();

// ---- 组件 ----
const scene = new AssemblyScene(document.getElementById('viewport'));
const tree = new AssemblyTree(document.getElementById('tree'), {
  onSelect: (id) => selectNode(id),
  onToggle: (visMap) => scene.applyVisibility(visMap),
});
scene.onPick((id) => tree.select(id));

// ---- 选择状态（选择层级交互原则：装配树定层级） ----
let selectedId = null;
let moveMode = false;

function selectNode(id) {
  selectedId = id;
  scene.highlight(tree.partIdsUnder(id));
  if (moveMode) {
    scene.enableMove(tree.partIdsUnder(id));   // 选装配体 → 整组移动
  }
  updateFeaturePanel();
  updateEditPanel();
}

// ---- 工具栏 ----
const $ = (s) => document.querySelector(s);
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
  if (!selectedId) { status('先在装配树或视口中选择节点', true); return; }
  const keep = tree.partIdsUnder(selectedId);
  const vis = new Map();
  for (const [id, rec] of tree.nodes) {
    if (rec.node.type === 'part') vis.set(id, keep.has(id));   // 其余全部隐藏
  }
  scene.applyVisibility(vis);
});

btnMove.addEventListener('click', () => {
  if (!selectedId) { status('先在装配树或视口中选择节点', true); return; }
  moveMode = !moveMode;
  btnMove.classList.toggle('active', moveMode);
  if (moveMode) scene.enableMove(tree.partIdsUnder(selectedId));
  else scene.disableMove();
});

$('#btn-reset-moves').addEventListener('click', () => scene.resetTempMoves());

// 剖切面：slider 0-100 映射到整体 bbox 的 Z 范围
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

// 相机书签（localStorage，视图配置不落版本树）
const CAM_KEY = 'cad_cam_bookmark';
$('#btn-cam-save').addEventListener('click', () => {
  localStorage.setItem(CAM_KEY, JSON.stringify(scene.getCameraState()));
  status('视角已保存');
});
$('#btn-cam-restore').addEventListener('click', () => {
  const s = JSON.parse(localStorage.getItem(CAM_KEY) || 'null');
  if (s) scene.setCameraState(s);
  else status('尚无已保存视角', true);
});

// ---- 一键体检（模块七：干涉 + DFM） ----
const auditModal = $('#audit-modal');
const auditBody = $('#audit-body');
$('#audit-close').addEventListener('click', () => auditModal.classList.add('hidden'));
auditModal.addEventListener('click', (e) => {
  if (e.target === auditModal) auditModal.classList.add('hidden');
});
$('#btn-audit').addEventListener('click', async () => {
  if (!lastCacheKey) { status('先加载装配体', true); return; }
  status('体检中（干涉 + DFM 规则）…');
  try {
    const rep = await auditAssembly(lastCacheKey);
    const div = (cls, html) => {
      const el = document.createElement('div');
      el.className = cls;
      el.innerHTML = html;
      return el;
    };
    auditBody.innerHTML = '';
    if (rep.interference_count === 0 && rep.dfm_count === 0) {
      auditBody.appendChild(div('audit-ok', '✓ 未发现问题：无干涉，DFM 规则全部通过'));
    } else {
      if (rep.interference_count > 0) {
        auditBody.appendChild(div('audit-severity error',
          `干涉 ×${rep.interference_count}`));
        rep.interferences.forEach((h) => auditBody.appendChild(
          div('audit-item', `${h.a.name} ↔ ${h.b.name}：穿透 ${h.volume_mm3} mm³`)));
      }
      if (rep.dfm_count > 0) {
        auditBody.appendChild(div('audit-severity warning', `DFM 提示 ×${rep.dfm_count}`));
        rep.dfm.forEach((d) => auditBody.appendChild(
          div('audit-item', `[${d.part}] ${d.detail}`)));
      }
    }
    auditModal.classList.remove('hidden');
    status(`体检完成：干涉 ${rep.interference_count} · DFM ${rep.dfm_count}`);
  } catch (err) {
    status(`体检失败：${err.message}`, true);
  }
});

// ---- 图纸对照（D5：DXF/DWG → 语义 + SVG） ----
const drawingModal = $('#drawing-modal');
$('#drawing-close').addEventListener('click', () => drawingModal.classList.add('hidden'));
drawingModal.addEventListener('click', (e) => {
  if (e.target === drawingModal) drawingModal.classList.add('hidden');
});
$('#btn-drawing').addEventListener('click', () => drawingModal.classList.remove('hidden'));
$('#drawing-browse').addEventListener('click', () => $('#file-input').click());

// 图纸导入主体：上传路由 / 最近使用 / 弹窗浏览共用。
// 同时更新弹窗 msg 与侧栏主状态栏——否则上传触发的导入会让主状态栏
// 停留在"上传中"。
async function importDrawingFile(p) {
  const msg = $('#drawing-msg');
  msg.textContent = '导入中…';
  status('图纸导入中…');
  try {
    const res = await importDrawing(p);
    const summary = `${res.source_file}${res.cache_hit ? ' · 缓存命中' : ''} · `
      + `${res.oda_used ? 'ODA 转换' : 'DXF 直读'} · ${res.entity_count} 实体`;
    msg.textContent = summary;
    status(`图纸：${summary}`);
    // SVG 直插（同源静态服务）
    const r = await fetch(`${res.base_url}/view.svg`);
    $('#drawing-view').innerHTML = await r.text();
    // 语义列表（螺纹/直径/公差 = 模块六语义真理）
    const sem = $('#drawing-semantics');
    sem.innerHTML = '';
    res.semantics.forEach((s) => {
      const row = document.createElement('div');
      row.className = 'fp-row';
      const kind = document.createElement('span');
      kind.className = 'sem-kind';
      kind.textContent = { thread: '螺纹', diameter: '直径', tolerance: '公差', note: '标注' }[s.kind] || s.kind;
      const val = document.createElement('span');
      val.className = 'fp-name';
      val.textContent = s.text;
      sem.append(row);
      row.append(kind, val);
    });
    pushRecent(p, 'drawing');
  } catch (err) {
    const denied = err.message.includes('outside allowed dirs');
    const text = denied ? pathDeniedMsg() : `错误：${err.message}`;
    msg.textContent = text;
    status(text, true);
  }
}

// ---- 插件状态面板（D5/D7 探测 + 优雅降级） ----
const PLUGIN_DEFS = [
  { key: 'oda', name: '图纸转换 ODA', what: 'DWG → DXF 转换（图纸对照）' },
  { key: 'fea', name: '力学分析 FEA', what: 'FreeCAD + CalculiX（静力学）' },
  { key: 'blender', name: '离线渲染 Blender', what: 'Cycles 静止帧渲染' },
];
let pluginsState = null;

async function refreshPlugins() {
  const list = $('#plugin-list');
  try {
    pluginsState = await getPlugins();
  } catch {
    pluginsState = null;
    list.innerHTML = '<div class="plug-row"><span class="plug-state err">探测失败（服务不可达）</span></div>';
    return;
  }
  list.innerHTML = '';
  for (const def of PLUGIN_DEFS) {
    const st = pluginsState[def.key] || {};
    const row = document.createElement('div');
    row.className = 'plug-row';
    const dot = document.createElement('span');
    dot.className = `plug-dot${st.available ? ' on' : ''}`;
    const name = document.createElement('span');
    name.className = 'plug-name';
    name.textContent = def.name;
    const state = document.createElement('span');
    state.className = `plug-state${st.available ? '' : ' off'}`;
    state.textContent = st.available ? '可用' : '未安装';
    row.append(dot, name, state);
    row.title = st.available
      ? `${def.what}\n${st.path || st.freecad || st.blender || ''}`
      : (st.hint || def.what);
    list.appendChild(row);
  }
}
$('#plug-refresh').addEventListener('click', () => {
  refreshPlugins();
  status('插件重新探测中…');
});
refreshPlugins();

// ---- R5 任务卡片：进度 + 取消（FEA / 渲染共用） ----
const jobCard = $('#job-card');
const jobTitle = $('#job-title');
const jobPhase = $('#job-phase');
const jobBar = $('#job-bar');
const jobBarFill = $('#job-bar-fill');
const jobDetail = $('#job-detail');
const jobResult = $('#job-result');
const jobCancelBtn = $('#job-cancel');
let activeJob = null;   // { id, kind, timer }

const PHASE_LABELS = {
  queued: '排队中', probe: '探测插件', prepare: '生成脚本', cache: '缓存命中',
  interpreter: 'FreeCAD 启动', geometry: '载入几何', faces: '选定约束面',
  setup: 'FEM 设置', mesh: '网格划分', solve: '求解中', post: '读取结果',
  render: '渲染中', done: '完成',
};

function renderJobCard(job) {
  const { progress = {}, status } = job;
  const pct = progress.percent;
  jobPhase.textContent = `${PHASE_LABELS[progress.phase] || progress.phase || status}`
    + (typeof pct === 'number' ? ` · ${Math.round(pct)}%` : '');
  if (typeof pct === 'number') {
    jobBar.classList.remove('indeterminate');
    jobBarFill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
  } else {
    jobBar.classList.add('indeterminate');
    jobBarFill.style.width = '';
  }
  jobDetail.textContent = progress.detail || '';
  jobCancelBtn.style.display = (status === 'queued' || status === 'running') ? '' : 'none';
}

function stopJobPolling() {
  if (activeJob?.timer) clearInterval(activeJob.timer);
  activeJob = null;
}

function showJobError(job) {
  jobResult.innerHTML = '';
  const el = document.createElement('div');
  el.className = 'jr-err';
  el.textContent = job.status === 'cancelled'
    ? '已取消（几何与缓存不受影响）' : `失败：${job.error || '未知错误'}`;
  jobResult.appendChild(el);
}

function showFeaResult(res) {
  jobResult.innerHTML = '';
  const fmt = (v, unit) => (v == null ? '—' : `${(+v).toFixed(4)} ${unit}`);
  const mk = (label, val) => {
    const row = document.createElement('div');
    row.className = 'jr-row';
    const k = document.createElement('span');
    k.textContent = label;
    const v = document.createElement('span');
    v.textContent = val;
    row.append(k, v);
    jobResult.appendChild(row);
  };
  mk('最大位移', fmt(res.max_displacement_mm, 'mm'));
  mk('最大 von Mises', fmt(res.max_von_mises_MPa, 'MPa'));
  mk('网格', res.mesh_nodes != null ? `${res.mesh_nodes} 节点 / ${res.mesh_elements} 单元` : '—');
  if (res.cache_hit) mk('缓存', '命中（force 可重算）');
  if (res.duration_s != null) mk('耗时', `${res.duration_s}s`);
}

function showRenderResult(res) {
  jobResult.innerHTML = '';
  const ok = document.createElement('div');
  ok.className = 'jr-ok';
  ok.textContent = `完成：${res.engine || 'renderer'} · ${res.objects ?? '?'} 对象`
    + (res.duration_s != null ? ` · ${res.duration_s}s` : '');
  jobResult.appendChild(ok);
  // 打开渲染结果弹窗（图片走同源静态服务）
  const modal = $('#render-modal');
  $('#render-view').innerHTML = `<img src="${res.png_url}" alt="render" />`;
  $('#render-meta').textContent = res.png_url;
  modal.classList.remove('hidden');
}

const renderModal = $('#render-modal');
$('#render-close').addEventListener('click', () => renderModal.classList.add('hidden'));
renderModal.addEventListener('click', (e) => {
  if (e.target === renderModal) renderModal.classList.add('hidden');
});

async function trackJob(started, kind, label) {
  if (activeJob) {
    status('已有任务在进行（见右下角卡片，可取消后再发起新任务）', true);
    return;
  }
  activeJob = { id: started.job_id, kind, timer: null };
  jobTitle.textContent = label;
  jobResult.innerHTML = '';
  jobDetail.textContent = '';
  jobPhase.textContent = '排队中';
  jobBar.classList.add('indeterminate');
  jobCard.classList.remove('hidden');
  status(`${label}已提交（任务 ${started.job_id.slice(0, 8)}）`);

  const poll = async () => {
    let job;
    try {
      job = await getJob(activeJob.id);
    } catch {
      return;   // 瞬时网络问题：下个周期重试
    }
    renderJobCard(job);
    if (job.status === 'done') {
      stopJobPolling();
      if (kind === 'fea') showFeaResult(job.result);
      else showRenderResult(job.result);
      status(`${label}完成`);
    } else if (job.status === 'error' || job.status === 'cancelled') {
      stopJobPolling();
      showJobError(job);
      status(`${label}${job.status === 'cancelled' ? '已取消' : '失败'}`, true);
    }
  };
  activeJob.timer = setInterval(poll, 800);
  poll();
}

jobCancelBtn.addEventListener('click', async () => {
  if (!activeJob) return;
  jobCancelBtn.disabled = true;
  try {
    await cancelJob(activeJob.id);
    jobPhase.textContent = '正在取消…';
  } catch (err) {
    status(`取消失败：${err.message}`, true);
  } finally {
    jobCancelBtn.disabled = false;
  }
});

// ---- 力学分析（D7 FEA 插件，选中零件 → 模板级静力学场景） ----
$('#btn-fea').addEventListener('click', async () => {
  if (!lastCacheKey) { status('先加载装配体', true); return; }
  if (pluginsState && pluginsState.fea && !pluginsState.fea.available) {
    status(pluginsState.fea.hint || 'FEA 插件未安装', true);
    return;
  }
  const rec = selectedId && tree.nodes.get(selectedId);
  if (!rec || rec.node.type !== 'part') {
    status('先在装配树或视口中选择一个零件（分析对象）', true);
    return;
  }
  const tid = scene.templateOf(selectedId);
  try {
    const started = await startFeaJob(lastCacheKey, tid);
    trackJob(started, 'fea', `力学分析 ${rec.node.name}`);
  } catch (err) {
    status(`提交失败：${err.message}`, true);
  }
});

// ---- 离线渲染（D7 Blender 插件，整个装配体当前状态） ----
$('#btn-render').addEventListener('click', async () => {
  if (!lastCacheKey) { status('先加载装配体', true); return; }
  if (pluginsState && pluginsState.blender && !pluginsState.blender.available) {
    status(pluginsState.blender.hint || 'Blender 插件未安装', true);
    return;
  }
  try {
    const started = await startRenderJob(lastCacheKey);
    trackJob(started, 'render', '离线渲染');
  } catch (err) {
    status(`提交失败：${err.message}`, true);
  }
});

$('#btn-view-reset').addEventListener('click', () => {
  scene.applyExplosion(0);
  explodeSlider.value = 0;
  explodeVal.textContent = '0%';
  scene.setXray(false);
  btnXray.classList.remove('active');
  sectionOn.checked = false;
  applySection();
  moveMode = false;
  btnMove.classList.remove('active');
  scene.disableMove();
  scene.resetTempMoves();
  scene.highlight(null);
  tree.render(lastManifest.root);   // 显隐复选框全部复位
  scene.applyVisibility(tree.effectiveVisibility());
  scene._fitCamera();
  status('视图已复位');
});

// ---- 特征面板（拾取 API 化：cache features JSON + glTF overlay） ----
const fp = $('#feature-panel');
const fpTitle = $('#fp-title');
const fpList = $('#fp-list');
$('#fp-close').addEventListener('click', () => {
  fp.classList.add('hidden');
  scene.showFeature(null, null);
});

function updateFeaturePanel() {
  if (!selectedId) { fp.classList.add('hidden'); return; }
  const partIds = tree.partIdsUnder(selectedId);
  // 特征面板只针对零件节点（装配体选择显示提示）
  if (partIds.size !== 1 || !tree.nodes.get(selectedId)) {
    fp.classList.add('hidden');
    return;
  }
  const node = tree.nodes.get(selectedId).node;
  if (node.type !== 'part') {
    fp.classList.add('hidden');
    return;
  }
  const tid = scene.templateOf(node.id);
  const tpl = lastManifest.templates.find((t) => t.id === tid);
  if (!tpl?.features) { fp.classList.add('hidden'); return; }

  fetch(`${lastBaseUrl}/${tpl.features}`)
    .then((r) => r.json())
    .then((feats) => {
      fpTitle.textContent = `${node.name} · ${feats.length} 特征`;
      fpList.innerHTML = '';
      feats.forEach((f) => {
        const row = document.createElement('div');
        row.className = 'fp-row';
        const dot = document.createElement('span');
        dot.className = 'fp-dot';
        dot.style.background = f.color;
        const name = document.createElement('span');
        name.className = 'fp-name';
        name.textContent = `${f.label} ${f.id}`;
        const dim = document.createElement('span');
        dim.className = 'fp-dim';
        const r = (f.radii || []).map((x) => `R${x}`).join('/');
        dim.textContent = [f.axis, r, f.extent ? `L${f.extent}` : null]
          .filter(Boolean).join(' ');
        row.append(dot, name, dim);

        // 定点特征编辑（R1）：孔类特征支持扩径
        if (f.type === 'hole' && (f.radii || []).length) {
          const btn = document.createElement('button');
          btn.className = 'fp-edit';
          btn.textContent = '扩径';
          btn.title = '定点扩径（只改这个孔）';
          btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const cur = Math.max(...f.radii);
            const input = window.prompt(`扩径到多大半径？（当前 R${cur}，只能扩大）`, (cur + 0.5).toFixed(2));
            if (!input) return;
            const newR = parseFloat(input);
            if (!(newR > cur)) { window.alert('只能扩大（B-rep 无法回填材料）'); return; }
            btn.disabled = true;
            btn.textContent = '…';
            try {
              const res = await editAssembly(
                lastCacheKey, tid, 'hole_resize', { radius: newR }, f.id);
              lastManifest = res.manifest;
              await scene.load(res.manifest, lastBaseUrl);
              tree.render(res.manifest.root);
              scene.highlight(null);
              selectedId = null;
              status(`已提交 ${res.version}：${res.changelog}`);
              refreshVersions();
            } catch (err) {
              window.alert(`错误：${err.message}`);
            } finally {
              btn.disabled = false;
              btn.textContent = '扩径';
            }
          });
          row.appendChild(btn);
        }

        row.addEventListener('click', () => {
          fpList.querySelectorAll('.fp-row').forEach((x) => x.classList.remove('selected'));
          row.classList.add('selected');
          scene.showFeature(node.id, f.id);
        });
        fpList.appendChild(row);
      });
      fp.classList.remove('hidden');
    })
    .catch(() => fp.classList.add('hidden'));
}

// ---- 编辑面板（Phase C：写操作 → 原子版本提交；干涉仅前端提醒） ----
const ep = $('#edit-panel');
const epTitle = $('#ep-title');
const epOp = $('#ep-op');
const epMsg = $('#ep-msg');
const vp = $('#version-panel');
const vpList = $('#vp-list');
let editTarget = null;   // {templateId, partName}

$('#ep-close').addEventListener('click', () => ep.classList.add('hidden'));
$('#vp-refresh').addEventListener('click', refreshVersions);

epOp.addEventListener('change', () => {
  $('#ep-drill-params').classList.toggle('hidden', epOp.value !== 'drill');
  $('#ep-chamfer-params').classList.toggle('hidden', epOp.value !== 'chamfer');
  $('#ep-fillet-params').classList.toggle('hidden', epOp.value !== 'fillet');
  $('#ep-scale-params').classList.toggle('hidden', epOp.value !== 'scale');
});

function updateEditPanel() {
  const rec = selectedId && tree.nodes.get(selectedId);
  if (!rec || rec.node.type !== 'part' || !lastManifest) {
    editTarget = null;
    ep.classList.add('hidden');
    return;
  }
  const tid = scene.templateOf(selectedId);
  editTarget = { templateId: tid, partName: rec.node.name };
  epTitle.textContent = `编辑 ${rec.node.name}（模板 ${tid}）`;
  ep.classList.remove('hidden');
}

function editParams() {
  switch (epOp.value) {
    case 'drill': {
      const pos = $('#ep-pos').value.split(',').map((x) => parseFloat(x.trim()));
      return {
        radius: parseFloat($('#ep-radius').value),
        depth: parseFloat($('#ep-depth').value),
        position: pos.length === 3 ? pos : [0, 0, 0],
      };
    }
    case 'chamfer':
      return { distance: parseFloat($('#ep-distance').value) };
    case 'fillet':
      return { radius: parseFloat($('#ep-fradius').value) };
    case 'scale':
      return { factor: parseFloat($('#ep-factor').value) };
    default:
      return {};
  }
}

$('#ep-run').addEventListener('click', async () => {
  if (!editTarget || !lastCacheKey) return;
  epMsg.className = 'ep-msg-info';
  epMsg.textContent = '修改中（几何 + 干涉检查）…';
  epMsg.classList.remove('hidden');
  try {
    const res = await editAssembly(
      lastCacheKey, editTarget.templateId, epOp.value, editParams());
    // 用版本视图 manifest 重载场景（编辑模板 gltf 指向版本文件）
    lastManifest = res.manifest;
    await scene.load(res.manifest, lastBaseUrl);
    tree.render(res.manifest.root);
    scene.highlight(null);
    selectedId = null;
    ep.classList.add('hidden');
    epMsg.classList.add('hidden');
    status(`已提交 ${res.version}：${res.changelog}`);
    refreshVersions();
  } catch (err) {
      epMsg.className = 'ep-msg-error';
      epMsg.textContent = `错误：${err.message}`;
    epMsg.classList.remove('hidden');
  }
});

// ---- 版本面板 ----
async function refreshVersions() {
  if (!lastCacheKey) return;
  try {
    const res = await listVersions(lastCacheKey);
    vp.classList.remove('hidden');
    vpList.innerHTML = '';
    const mkRow = (id, changelog, created, current) => {
      const row = document.createElement('div');
      row.className = `vp-row${current ? ' current' : ''}`;
      const name = document.createElement('span');
      name.className = 'vp-name';
      name.textContent = `${id}`;
      const desc = document.createElement('span');
      desc.className = 'vp-desc';
      desc.textContent = changelog + (created ? ` · ${created}` : '');
      const btn = document.createElement('button');
      btn.textContent = current ? '当前' : '切换';
      btn.disabled = current;
      btn.addEventListener('click', async () => {
        const r = await checkoutVersion(lastCacheKey, id);
        lastManifest = r.manifest;
        await scene.load(r.manifest, lastBaseUrl);
        tree.render(r.manifest.root);
        scene.highlight(null);
        selectedId = null;
        status(`已切换到 ${id}`);
        refreshVersions();
      });
      row.append(name, desc, btn);
      vpList.appendChild(row);
    };
    mkRow('v0', '基线（原始导入）', '', res.current === 'v0');
    [...res.versions].reverse().forEach((v) =>
      mkRow(v.id, v.changelog, v.created, v.id === res.current));
  } catch {
    vp.classList.add('hidden');
  }
}

// ---- 加载状态与装配加载主体 ----
const forceBox = $('#force');
const browseBtn = $('#btn-browse');
const statusEl = $('#status');

function status(text, isError = false) {
  statusEl.textContent = text;
  statusEl.className = isError ? 'error' : 'ok';
}

let lastManifest = null;
let lastBaseUrl = null;
let lastCacheKey = null;

// 装配加载主体：上传路由 / 最近使用共用（路径手输已移除）。
async function loadAssembly(p) {
  browseBtn.disabled = true;
  status('解析中…');
  try {
    const res = await parseAssembly(p, forceBox.checked);
    lastManifest = res.manifest;
    lastBaseUrl = res.base_url;
    lastCacheKey = res.cache_key;
    const count = await scene.load(res.manifest, res.base_url);
    tree.render(res.manifest.root);
    scene.highlight(null);
    selectedId = null;
    fp.classList.add('hidden');
    ep.classList.add('hidden');
    explodeSlider.value = 0;
    explodeVal.textContent = '0%';
    scene.applyExplosion(0);
    applySection();
    status(
      `${res.manifest.source_file} · ${res.manifest.templates.length} 模板 · ` +
      `${count} 实例 · ${res.cache_hit ? '缓存命中' : '新建缓存'}`,
    );
    refreshVersions();
    pushRecent(p, 'assembly');
  } catch (err) {
    const denied = err.message.includes('outside allowed dirs');
    status(denied ? pathDeniedMsg() : `错误：${err.message}`, true);
  } finally {
    browseBtn.disabled = false;
  }
}

// ---- 文件上传 / 拖放 / 最近使用 ----
// 上传是显式授权通道：用户亲手把文件交给服务（token 仍守门），落盘
// workspace/uploads 后按扩展名路由到装配加载或图纸导入。
const dropOverlay = $('#drop-overlay');
const recentPanel = $('#recent-panel');
const RECENT_KEY = 'cad_recent_files';
const RECENT_MAX = 8;

function loadRecent() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; }
  catch { return []; }
}

function saveRecent(list) {
  localStorage.setItem(RECENT_KEY, JSON.stringify(list));
}

function pushRecent(path, kind) {
  const list = loadRecent().filter((r) => r.path !== path);
  list.unshift({ path, kind, ts: Date.now() });
  saveRecent(list.slice(0, RECENT_MAX));
  renderRecent();
}

function routeByExtension(p) {
  const ext = p.slice(p.lastIndexOf('.')).toLowerCase();
  if (ext === '.dxf' || ext === '.dwg') {
    drawingModal.classList.remove('hidden');
    importDrawingFile(p);
  } else {
    drawingModal.classList.add('hidden');   // 切回装配视图时收起图纸弹窗
    loadAssembly(p);
  }
}

function renderRecent() {
  const list = loadRecent();
  recentPanel.classList.toggle('hidden', !list.length);
  const el = $('#recent-list');
  el.innerHTML = '';
  list.forEach((r) => {
    const row = document.createElement('div');
    row.className = 'recent-row';
    row.title = r.path;
    const badge = document.createElement('span');
    badge.className = `recent-badge ${r.kind}`;
    badge.textContent = r.kind === 'drawing' ? '图纸' : '装配';
    const name = document.createElement('span');
    name.className = 'recent-name';
    name.textContent = r.path;
    row.append(badge, name);
    row.addEventListener('click', () => routeByExtension(r.path));
    el.append(row);
  });
}

$('#recent-clear').addEventListener('click', () => {
  saveRecent([]);
  renderRecent();
});

async function handleUpload(file) {
  status(`上传中… ${file.name}`);
  try {
    const res = await uploadFile(file);
    routeByExtension(res.path);
  } catch (err) {
    status(`上传失败：${err.message}`, true);
  }
}

$('#btn-browse').addEventListener('click', () => $('#file-input').click());
$('#file-input').addEventListener('change', (e) => {
  const f = e.target.files[0];
  e.target.value = '';               // 同一文件可重复选择
  if (f) handleUpload(f);
});

// 全窗口拖放：dragenter 计数防子元素抖动
let dragDepth = 0;
window.addEventListener('dragenter', (e) => {
  e.preventDefault();
  dragDepth += 1;
  dropOverlay.classList.remove('hidden');
});
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) dropOverlay.classList.add('hidden');
});
window.addEventListener('drop', (e) => {
  e.preventDefault();
  dragDepth = 0;
  dropOverlay.classList.add('hidden');
  const f = e.dataTransfer?.files[0];
  if (f) handleUpload(f);
});

renderRecent();

// ---- agent 预览链接消费：?load=<path>（顶部已提取并从 URL 清除）----
// agent 在对话里给出 /app?token=...&load=<encodeURIComponent(路径)>，
// 用户点击即加载；路径仍受服务端 safe_input_path 权威校验。
if (bootLoadPath) routeByExtension(bootLoadPath);
