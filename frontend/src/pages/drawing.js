// 图纸对照独立页（从原 modal 提升）
//  - URL 支持：?cacheKey=xxx（装配体缓存键，用于"从首页打开图纸对照"时附带装配体上下文）
//              ?path=<encode 图纸文件>（支持从首页最近图纸/拖放直接进入）
//  - 顶部：返回首页、标题、导入/拖放入口、结果摘要
//  - 主体：左交互式 SVG 视图（缩放/平移/适配）+ 右信息面板（概览 + 语义）

import '../style.css';
import { importDrawing, uploadFile, getToken } from '../api.js';
import {
  consumeUrlBoot, ensureToken, bindStatus,
  pushRecent, bindDropOverlay, handleUpload, goHome,
  readScopeFromUrl, syncLoadParam, initErrorTrap,
} from '../shared/utils.js';

const { bootLoadPath } = consumeUrlBoot();
initErrorTrap();
ensureToken();

const $ = (s) => document.querySelector(s);
const statusFn = bindStatus($('#drawing-status'));
const scope = readScopeFromUrl();

const urlParams = new URLSearchParams(location.search);
const urlPathRaw = urlParams.get('path');   // 图纸文件路径（可选）

$('#nav-home').addEventListener('click', goHome);
// 如果 cacheKey 有上下文，顶部放一行"关联装配体"提示
if (scope.cacheKey) {
  const tip = document.createElement('div');
  tip.className = 'ctx-hint';
  tip.textContent = `关联装配缓存：${scope.cacheKey}`;
  $('#drawing-card').prepend(tip);
}

// ---------- 交互视图控制（缩放 / 平移 / 适配 / 复位） ----------
// 采用"容器 SVG 自带 viewBox + preserveAspectRatio=meet"方案：由浏览器按 viewBox
// 统一缩放几何与描边，保证整图等比不放大失配；pan/zoom 通过调整 viewBox 实现。
const canvas = $('#drw-canvas');
const { svg, inner, state } = (() => {
  const inner = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  inner.id = 'drw-g';
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.id = 'drw-svg';
  svg.setAttribute('width', '100%');
  svg.setAttribute('height', '100%');
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  svg.append(inner);
  canvas.append(svg);
  return { svg, inner, state: { init: [0, 0, 1, 1], vb: [0, 0, 1, 1] } };
})();

function applyViewBox() {
  // rAF 合帧：滚轮/拖拽事件频率远高于刷新率，逐帧合批避免 10MB 级
  // SVG 每个事件全量重绘造成卡顿
  if (applyViewBox.raf) return;
  applyViewBox.raf = requestAnimationFrame(() => {
    applyViewBox.raf = 0;
    svg.setAttribute('viewBox', state.vb.map((v) => Number(v.toFixed(4))).join(' '));
    updateZoomPct();
  });
}

// 当前缩放相对"适配全图"的百分比（用于工具栏读数 + 滑动条同步）
function currentZoomPct() {
  if (!state.init[2]) return 100;
  return state.init[2] / state.vb[2] * 100;
}
// 滑动条用对数映射：10% – 5000% 全程好拖（低端精细、高端跨度大）。
// 位置 pos∈[0,1000] ↔ 百分比 pct：pct = 10 * 500^(pos/1000)
const ZOOM_MIN = 10, ZOOM_MAX = 5000;
function sliderToPct(pos) {
  const t = Math.min(1, Math.max(0, pos / 1000));
  return ZOOM_MIN * Math.pow(ZOOM_MAX / ZOOM_MIN, t);
}
function pctToSlider(pct) {
  const p = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, pct));
  return 1000 * Math.log(p / ZOOM_MIN) / Math.log(ZOOM_MAX / ZOOM_MIN);
}
function updateZoomPct() {
  const el = $('#drw-zoom-pct');
  if (!el) return;
  const pct = currentZoomPct();
  el.textContent = `${Math.round(pct)}%`;
  // 反向同步滑动条，保证滚轮 / 按钮 / 双击缩放后滑条也跟着动
  const slider = $('#drw-zoom-slider');
  if (slider && document.activeElement !== slider) {
    slider.value = String(Math.round(pctToSlider(pct)));
  }
}

// 直接设到目标缩放比例（以视图中心为锚），供滑动条使用。
// 基于"当前实际比例→目标比例"算 factor 累积进 ix；拖动期间设 sliderActive
// 抑制中途提交(change 事件才提交)，vb 全程不变，故不会跳变。
function setZoomTo(pct) {
  const target = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, pct));
  const factor = target / currentZoomPct();
  const rect = canvas.getBoundingClientRect();
  sliderActive = true;                 // 拖动期间不提交，change 时才折算
  ix.k *= factor;
  ix.cx = rect.width / 2; ix.cy = rect.height / 2;
  inner.classList.add('pe-none');
  applyLiveTransform();
}

// 视图度量：考虑 preserveAspectRatio=meet 的居中留白，得到内容区实际
// 缩放 s 与偏移，用于屏幕坐标↔世界坐标的精确换算（此前忽略留白，导致
// 图幅与画布比例不一致时缩放锚点偏移、平移速度脱节）
function viewMetrics() {
  const rect = canvas.getBoundingClientRect();
  const [vx, vy, vw, vh] = state.vb;
  const s = Math.min(rect.width / vw, rect.height / vh) || 1;
  const offX = (rect.width - vw * s) / 2;
  const offY = (rect.height - vh * s) / 2;
  return { rect, s, offX, offY, vx, vy, vw, vh };
}
function screenToWorld(sx, sy) {
  const m = viewMetrics();
  const lx = sx - m.rect.left - m.offX;
  const ly = sy - m.rect.top - m.offY;
  return [m.vx + lx / m.s, m.vy + ly / m.s];
}

// 适配窗口：回到初始 viewBox（浏览器自动等比缩放并居中）
function fit() {
  commitTransform(); // 先折算可能未提交的交互变换
  state.vb = state.init.slice();
  applyViewBox();
}

// 以屏幕点 (sx,sy) 为锚缩放 factor 倍（该点世界坐标保持不动）。
// 走"交互期只动 transform"框架：累积进 ix，停手后才折算 viewBox，避免逐次全量重排。
function zoom(factor, sx, sy) {
  const rect = canvas.getBoundingClientRect();
  const ax = sx !== undefined ? sx - rect.left : rect.width / 2;
  const ay = sy !== undefined ? sy - rect.top : rect.height / 2;
  liveZoom(factor, ax, ay);
}

// ---- 交互期性能优化 ----
// 拖拽 / 滚轮 / 缩放按钮 / 滑条期间只更新 CSS transform（GPU 合成，不触发
// 8 万级实体的 SVG 重排版），交互结束（松手 / 停滚 / 停拖滑条）才一次性把累计
// 变换折算进 viewBox。大图纸下手感从"卡顿"变"跟手"。
// 交互进行中给 #drw-g 加 pe-none，跳过整图命中测试（closest/hover 光标等）。
let ix = { tx: 0, ty: 0, k: 1, cx: 0, cy: 0 }; // 屏幕空间累计变换
let commitT = null;
function applyLiveTransform() {
  const { cx, cy, k, tx, ty } = ix;
  svg.style.transformOrigin = '0 0';
  svg.style.transform =
    `translate(${tx}px, ${ty}px) translate(${cx}px, ${cy}px) scale(${k}) translate(${-cx}px, ${-cy}px)`;
}
// 统一防抖提交：停手 140ms 后把累计 transform 折算进 viewBox（含按钮/滚轮）。
// 滑条拖动期间(sliderActive)不提交，交给 change 事件在松手时一次性提交，
// 避免拖动中途插入全量重排导致视觉跳变。
let sliderActive = false;
function scheduleCommit() {
  clearTimeout(commitT);
  if (sliderActive) return;
  commitT = setTimeout(commitTransform, 140);
}
// 交互期累积一次缩放（相对当前 viewBox 的 factor，锚点屏幕坐标 ax/ay）。
// 与拖拽平移共用 ix，复合叠加；期间只动 transform，不重排。
function liveZoom(factor, ax, ay) {
  ix.k *= factor;
  ix.cx = ax; ix.cy = ay;
  inner.classList.add('pe-none'); // 跳过整图命中测试
  applyLiveTransform();
  scheduleCommit();
}
// 把累计的屏幕变换折算回 viewBox（纯平移时 k===1；缩放时以光标为锚）
function commitTransform() {
  clearTimeout(commitT);
  inner.classList.remove('pe-none'); // 恢复命中测试（文字选择 / 悬停光标）
  if (ix.k === 1 && ix.tx === 0 && ix.ty === 0) {
    ix = { tx: 0, ty: 0, k: 1, cx: 0, cy: 0 };
    return;
  }
  const m = viewMetrics();
  const { s, offX, offY } = m;
  const s2 = ix.k * s;
  const wx = state.vb[0] + (ix.cx - offX) / s;
  const wy = state.vb[1] + (ix.cy - offY) / s;
  const vw = state.vb[2] / ix.k, vh = state.vb[3] / ix.k;
  const vx = wx - (ix.cx - offX) / s2 - ix.tx / s2;
  const vy = wy - (ix.cy - offY) / s2 - ix.ty / s2;
  state.vb = [vx, vy, vw, vh];
  ix = { tx: 0, ty: 0, k: 1, cx: 0, cy: 0 };
  svg.style.transform = '';
  applyViewBox();
}

// 框选放大：将屏幕矩形对应的世界区域设为新 viewBox，并修正宽高比以填满视图
function zoomToWorldRect(wr) {
  commitTransform(); // 先折算可能未提交的交互变换
  let [x0, y0, x1, y1] = wr;
  if (x1 < x0) [x0, x1] = [x1, x0];
  if (y1 < y0) [y0, y1] = [y1, y0];
  let ww = (x1 - x0) || 1e-6, wh = (y1 - y0) || 1e-6;
  const m = viewMetrics();
  const canvasAspect = m.rect.width / m.rect.height;
  if (ww / wh > canvasAspect) wh = ww / canvasAspect;
  else ww = wh * canvasAspect;
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  state.vb = [cx - ww / 2, cy - wh / 2, ww, wh];
  applyViewBox();
}

// wheel：以鼠标为锚缩放。交互期只动 CSS transform，停滚 140ms 后提交 viewBox
canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  const factor = Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.01 : 0.0015));
  const rect = canvas.getBoundingClientRect();
  const ax = e.clientX - rect.left, ay = e.clientY - rect.top;
  inner.classList.add('pe-none'); // 缩放期间跳过整图命中测试
  ix.k *= factor;
  ix.cx = ax; ix.cy = ay;
  applyLiveTransform();
  scheduleCommit();
}, { passive: false });

// 交互模式：pan（拖拽平移） / window（拖拽框选放大）
let mode = 'pan';
let drag = null;          // pan 模式下拖拽起点
let winSel = null;        // window 模式下框选起点（屏幕坐标）
let activePointer = null; // 当前捕获的指针 id

// 用 Pointer Events + 指针捕获：拖拽中指针即便移出窗口 / 在窗外松手，
// pointerup 仍会派发到 canvas，避免「松手后卡在 grabbing、鼠标一动就平移」的死状态。
canvas.addEventListener('pointerdown', (e) => {
  if (e.button !== 0) return;
  // 点在文字上时不进入拖拽，让浏览器执行默认的文本选择；
  // 点在空白/几何元素上才平移画布。
  if (e.target.closest?.('text')) return;
  commitTransform(); // 折算可能未提交的上一轮交互（如刚停的滚轮）
  canvas.setPointerCapture?.(e.pointerId);
  activePointer = e.pointerId;
  if (mode === 'window') {
    winSel = { x0: e.clientX, y0: e.clientY, x1: e.clientX, y1: e.clientY };
    drawSelRect(winSel);
  } else {
    drag = { x: e.clientX, y: e.clientY };
    canvas.classList.remove('grab');
    canvas.classList.add('grabbing');
    canvas.classList.add('no-select'); // 拖拽期间临时禁用文字选择，松手后恢复
    inner.classList.add('pe-none');   // 拖拽期间跳过整图命中测试
  }
});
canvas.addEventListener('pointermove', (e) => {
  if (e.pointerId !== activePointer) return;
  if (winSel) {
    winSel.x1 = e.clientX; winSel.y1 = e.clientY;
    drawSelRect(winSel);
  } else if (drag) {
    // 交互期只累积屏幕位移并动 CSS transform，松手时再折算进 viewBox
    ix.tx += e.clientX - drag.x;
    ix.ty += e.clientY - drag.y;
    applyLiveTransform();
    drag.x = e.clientX; drag.y = e.clientY;
  }
});
function endPointer(e) {
  if (e.pointerId !== activePointer && activePointer !== null) return;
  if (winSel) {
    const r = winSel; winSel = null;
    clearSelRect();
    if (Math.abs(r.x1 - r.x0) > 3 || Math.abs(r.y1 - r.y0) > 3) {
      zoomToWorldRect([...screenToWorld(r.x0, r.y0), ...screenToWorld(r.x1, r.y1)]);
    }
    setMode('pan'); // 框选完成后回到平移模式
  }
  if (drag) {
    drag = null; canvas.classList.add('grab'); canvas.classList.remove('grabbing');
    canvas.classList.remove('no-select'); // 恢复文字选择
    commitTransform(); // 把拖拽累计的位移折算进 viewBox
  }
  activePointer = null;
}
canvas.addEventListener('pointerup', endPointer);
canvas.addEventListener('pointercancel', endPointer);

// 双击聚焦：精确点中实体 → 取其实体包围盒放大居中；落在空白处 → 以光标为
// 中心放大 2.5×。保证双击始终有可见响应（此前空白处双击只调用 fit()，在全图
// 状态下等于无操作，表现为"双击没反应"）。
canvas.addEventListener('dblclick', (e) => {
  const el = e.target.closest?.('path,line,circle,ellipse,polyline,polygon,rect,text');
  if (el && inner.contains(el)) {
    let b;
    try { b = el.getBBox(); } catch { b = null; }
    if (b && isFinite(b.width) && isFinite(b.height) && (b.width > 0 || b.height > 0)) {
      const pad = Math.max(b.width, b.height) * 0.15 + 1e-6;
      state.vb = [b.x - pad, b.y - pad, b.width + 2 * pad, b.height + 2 * pad];
      applyViewBox();
      return;
    }
  }
  // 未命中实体：以光标为锚放大（始终可见），便于在大图上快速钻取
  zoom(2.5, e.clientX, e.clientY);
});

// 框选矩形覆盖层（屏幕坐标，相对 #drawing-view）
let selBoxEl = null;
function drawSelRect(r) {
  const rect = canvas.getBoundingClientRect();
  if (!selBoxEl) {
    selBoxEl = document.createElement('div');
    selBoxEl.className = 'drw-selrect';
    canvas.parentElement.append(selBoxEl);
  }
  selBoxEl.style.left = `${Math.min(r.x0, r.x1) - rect.left}px`;
  selBoxEl.style.top = `${Math.min(r.y0, r.y1) - rect.top}px`;
  selBoxEl.style.width = `${Math.abs(r.x1 - r.x0)}px`;
  selBoxEl.style.height = `${Math.abs(r.y1 - r.y0)}px`;
}
function clearSelRect() { if (selBoxEl) { selBoxEl.remove(); selBoxEl = null; } }

function setMode(next) {
  mode = next;
  $('#drw-toolbar').classList.toggle('win', mode === 'window');
  canvas.classList.toggle('crosshair', mode === 'window');
  if (mode !== 'window') clearSelRect();
}

// 工具栏 + 键盘
$('#drw-fit').addEventListener('click', fit);
$('#drw-zoom-in').addEventListener('click', () => zoom(1.5));
$('#drw-zoom-out').addEventListener('click', () => zoom(1 / 1.5));
$('#drw-zoom-window').addEventListener('click', () => setMode(mode === 'window' ? 'pan' : 'window'));
// 缩放滑动条：对数映射，拖动时实时设到目标比例（以视图中心为锚）
$('#drw-zoom-slider').addEventListener('input', (e) => {
  setZoomTo(sliderToPct(Number(e.target.value)));
});
// 松手才折算进 viewBox（拖动期间 sliderActive 抑制中途提交，避免全量重排跳变）
$('#drw-zoom-slider').addEventListener('change', () => {
  sliderActive = false;
  commitTransform();
});
$('#drw-collapse').addEventListener('click', () => $('#drawing-split').classList.toggle('panel-collapsed'));
$('#drw-fullscreen').addEventListener('click', () => {
  const el = $('#drawing-view');
  if (!document.fullscreenElement) el.requestFullscreen?.();
  else document.exitFullscreen?.();
});
window.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (e.ctrlKey || e.metaKey) return;
  if (e.key === '+' || e.key === '=') zoom(1.5);
  else if (e.key === '-' || e.key === '_') zoom(1 / 1.5);
  else if (e.key === 'f' || e.key === 'F') fit();
  else if (e.key === 'w' || e.key === 'W') setMode(mode === 'window' ? 'pan' : 'window');
  else if (e.key === 'Escape' && mode === 'window') setMode('pan');
});

// 加载 SVG 到受控画布（保留 viewBox；描边/字号等根呈现属性带到内层 g）。
// 大图纸 SVG 有 10MB 级：不走 DOMParser 全量解析（此前还要逐节点搬移
// 8 万+ 子节点，直接冻结标签页），改用字符串切片 + 单次 innerHTML。
function mountSvg(svgText) {
  const open = svgText.indexOf('>', svgText.indexOf('<svg'));
  const close = svgText.lastIndexOf('</svg>');
  if (open < 0 || close < 0 || close < open) {
    statusFn('SVG 解析失败', true);
    return;
  }
  const rootAttrs = svgText.slice(svgText.indexOf('<svg'), open);
  const body = svgText.slice(open + 1, close);
  const vb = (rootAttrs.match(/viewBox="([^"]+)"/) || [])[1] || '';
  const nums = vb.trim().split(/\s+/).map(Number);
  state.init = nums.length === 4 && nums[2] > 0 && nums[3] > 0
    ? nums : [0, 0, 1, 1];
  for (const a of ['stroke', 'stroke-width', 'fill', 'font-family']) {
    const m = rootAttrs.match(new RegExp(`${a}="([^"]*)"`));
    if (m) inner.setAttribute(a, m[1]);
    else inner.removeAttribute(a);
  }
  inner.innerHTML = body;
  fit();
}

// ---------- 信息面板（概览 + 语义） ----------
const SEM_KIND_LABEL = { thread: '螺纹', diameter: '直径', tolerance: '公差', note: '标注' };
const ETYPE_LABEL = {
  LINE: '直线', CIRCLE: '圆', ARC: '圆弧', LWPOLYLINE: '多段线',
  ELLIPSE: '椭圆', SPLINE: '样条', POINT: '点', TEXT: '单行文字', MTEXT: '多行文字',
};

function renderOverview(res) {
  const sem = $('#drawing-semantics');
  sem.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'drw-overview';
  const total = res.entity_count ?? 0;
  const etRow = document.createElement('div');
  etRow.className = 'fp-row drw-overview-row';
  const sup = document.createElement('span');
  sup.className = 'sem-kind';
  sup.textContent = '实体';
  const supVal = document.createElement('span');
  supVal.className = 'fp-name';
  const types = res.entity_types || {};
  const desc = Object.keys(types).map((k) => `${ETYPE_LABEL[k] || k}×${types[k]}`).join(' · ');
  supVal.textContent = `${total}${desc ? `（${desc}）` : ''}`;
  etRow.append(sup, supVal);
  wrap.append(etRow);

  if (Array.isArray(res.layers) && res.layers.length) {
    const lgRow = document.createElement('div');
    lgRow.className = 'drw-overview-row';
    const lg = document.createElement('span');
    lg.className = 'sem-kind';
    lg.textContent = '图层';
    const chips = document.createElement('span');
    chips.className = 'drw-layer-chips';
    res.layers.forEach((l) => {
      const chip = document.createElement('span');
      chip.className = 'drw-layer-chip';
      chip.textContent = l;
      chips.append(chip);
    });
    lgRow.append(lg, chips);
    wrap.append(lgRow);
  }
  sem.append(wrap);

  // 大图纸可有数千条语义（全套图纸所有文字标注），全量渲染拖垮信息面板
  const sems = res.semantics || [];
  const MAX_SEM_ROWS = 120;
  sems.slice(0, MAX_SEM_ROWS).forEach((s) => {
    const row = document.createElement('div'); row.className = 'fp-row';
    const kind = document.createElement('span'); kind.className = 'sem-kind';
    kind.textContent = SEM_KIND_LABEL[s.kind] || s.kind;
    const val = document.createElement('span'); val.className = 'fp-name'; val.textContent = s.text;
    row.append(kind, val);
    sem.append(row);
  });
  if (sems.length > MAX_SEM_ROWS) {
    const more = document.createElement('div');
    more.className = 'fp-row sem-more';
    more.textContent = `… 其余 ${sems.length - MAX_SEM_ROWS} 条语义已省略`;
    sem.append(more);
  }
}

// 导入 + 渲染主体
async function runImport(p, force = false) {
  statusFn(force ? '强制重建缓存中…' : '导入中…');
  try {
    const res = await importDrawing(p, force);
    const summary = `${res.source_file}${res.cache_hit ? ' · 缓存命中' : ' · 缓存重建'} `
      + `· ${res.oda_used ? 'ODA 转换' : 'DXF 直读'} · ${res.entity_count} 实体`;
    $('#drawing-msg').textContent = res.cache_hit
      ? `已载入缓存 ${summary}` : `全新解析 ${summary}`;
    statusFn(summary);
    // 缓存破坏：渲染逻辑升级（schema_version 变化）后强制拉取新 SVG，
    // 否则浏览器 HTTP 缓存会一直返回旧渲染结果
    const svgQs = `?v=${res.schema_version}-${(res.source_sha256 || '').slice(0, 12)}`;
    const r = await fetch(`${res.base_url}/view.svg${svgQs}`);
    const text = await r.text();
    mountSvg(text);
    renderOverview(res);
    pushRecent(p, 'drawing');
  } catch (err) {
    const denied = err.message.includes('outside allowed dirs');
    statusFn(denied ? '路径不在服务可访问目录内。请确认 CAD_SERVICE_ALLOWED_DIRS 设置。' : `错误：${err.message}`, true);
    $('#drawing-msg').textContent = '加载失败';
  }
}

const forceDrawing = () => $('#drawing-force').checked;

$('#drawing-browse').addEventListener('click', () => $('#drawing-file-input').click());
$('#drawing-file-input').addEventListener('change', async (e) => {
  const f = e.target.files?.[0];
  e.target.value = '';
  if (!f) return;
  statusFn(`上传中… ${f.name}`);
  try {
    const p = await handleUpload(uploadFile, f);
    runImport(p, forceDrawing());
  } catch (err) { statusFn(`上传失败：${err.message}`, true); }
});

bindDropOverlay($('#drop-overlay'), async (f) => {
  statusFn(`上传中… ${f.name}`);
  try {
    const p = await handleUpload(uploadFile, f);
    runImport(p, forceDrawing());
  } catch (err) { statusFn(`上传失败：${err.message}`, true); }
});

// 由 URL path 参数直接触发
if (urlPathRaw) runImport(decodeURIComponent(urlPathRaw), forceDrawing());
else if (bootLoadPath) runImport(bootLoadPath, forceDrawing());
else statusFn('浏览或拖入 DXF / DWG 文件以加载（独立图纸对照页）');
// ?load= 已被本页消费（首页导入跳转而来），清除以免返回首页时重复导入；
// ?path=（本页自身参数）同理消费后清除
syncLoadParam(null);