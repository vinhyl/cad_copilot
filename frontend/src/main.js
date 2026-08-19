import './style.css';
import { AssemblyScene } from './scene.js';
import { AssemblyTree } from './tree.js';
import {
  getToken, setToken, parseAssembly, editAssembly, listVersions,
  checkoutVersion,
} from './api.js';

// ---- token 引导：URL ?token= 一次性注入 localStorage（本地单用户工具约定）----
const params = new URLSearchParams(location.search);
if (params.get('token')) {
  setToken(params.get('token'));
  params.delete('token');
  history.replaceState(null, '', `${location.pathname}${params}`);
}
if (!getToken()) {
  const t = window.prompt('服务 token（cad_service 启动时打印）：');
  if (t) setToken(t);
}

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

// ---- 编辑面板（Phase C：写操作 → 干涉守门 → 原子版本提交） ----
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
    // R15: 结构化拒绝（干涉）显式呈现
    if (err.status === 409 && err.payload?.interferences) {
      const rows = err.payload.interferences
        .map((h) => `${h.a.name} ↔ ${h.b.name}（${h.volume_mm3} mm³）`).join('；');
      epMsg.className = 'ep-msg-error';
      epMsg.textContent = `⛔ 干涉守门拒绝：${rows}。几何保持 ${err.payload.version} 不变。`;
    } else {
      epMsg.className = 'ep-msg-error';
      epMsg.textContent = `错误：${err.message}`;
    }
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

// ---- 加载表单 ----
const form = $('#load-form');
const pathInput = $('#path-input');
const forceBox = $('#force');
const loadBtn = $('#load-btn');
const statusEl = $('#status');

function status(text, isError = false) {
  statusEl.textContent = text;
  statusEl.className = isError ? 'error' : 'ok';
}

let lastManifest = null;
let lastBaseUrl = null;
let lastCacheKey = null;

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const p = pathInput.value.trim();
  if (!p) return;
  loadBtn.disabled = true;
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
  } catch (err) {
    status(`错误：${err.message}`, true);
  } finally {
    loadBtn.disabled = false;
  }
});
