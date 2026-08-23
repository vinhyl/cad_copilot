// 跨页面共享的小工具：状态条、最近使用、拖放上传、URL 工具、插件探测、排版偏好

// ---------- token / config 引导 ----------
const TOKEN_KEY = 'cad_service_token';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

export function setToken(t) {
  localStorage.setItem(TOKEN_KEY, t);
}

// ---------- M6：tab 标识（选中上行的来源标识，多 tab 场景 last-click-wins） ----------
const TAB_KEY = 'cad_tab_id';

export function getTabId() {
  let id = sessionStorage.getItem(TAB_KEY);
  if (!id) {
    id = `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    sessionStorage.setItem(TAB_KEY, id);
  }
  return id;
}

// ---------- M6：/ws 事件订阅（agent 写草稿/落版本/生成报告后原地刷新） ----------
/** 连接 /ws 订阅服务端事件。onEvent 收到形如
 *  {type: 'draft_saved'|'draft_deleted'|'version_changed'|'report_added'|
 *   'selection_changed', cache_key, client, ...} 的对象。
 *  断线自动重连（3s 退避）；每 25s 发 ping 保活。页面卸载时停止。 */
export function initWs(onEvent) {
  const token = getToken();
  if (!token) return () => {};   // 无 token（未引导）时跳过
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`;
  let ws = null;
  let stopped = false;
  let retryTimer = null;
  let pingTimer = null;
  const scheduleReconnect = () => {
    if (!stopped && !retryTimer) {
      retryTimer = setTimeout(() => {
        retryTimer = null;
        connect();
      }, 3000);
    }
  };
  const connect = () => {
    try {
      ws = new WebSocket(url);
    } catch {
      scheduleReconnect();
      return;
    }
    ws.onmessage = (e) => {
      try { onEvent(JSON.parse(e.data)); } catch { /* 忽略坏帧 */ }
    };
    ws.onclose = scheduleReconnect;
    ws.onerror = scheduleReconnect;
  };
  connect();
  pingTimer = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ action: 'ping' }));
    }
  }, 25000);
  return () => {
    stopped = true;
    clearTimeout(retryTimer);
    clearInterval(pingTimer);
    if (ws) { ws.onclose = null; ws.close(); }
  };
}

// ?token= 注入 localStorage；?load= 抽出来交给上层按扩展名路由。
// 参数保留在地址栏不清理（M6 修订）：内置浏览器「用系统浏览器打开」只能
// 复制当前 URL，清理后跳过去必然缺 token；保留后刷新/跳转/agent 重发
// 链接均幂等（token 重复注入无害，load 重新 parse 走缓存命中）。
export function consumeUrlBoot() {
  const params = new URLSearchParams(location.search);
  if (params.get('token')) {
    setToken(params.get('token'));
  }
  const bootLoadPath = params.get('load') || null;
  return { bootLoadPath };
}

/** 把当前加载的文件写回地址栏 ?load=<path>（保留其余参数如 token/focus）。
 * 「用系统浏览器打开」复制当前 URL 即可携带完整状态，另一浏览器落地
 * 自动加载同一文件。path 为 null 时移除该参数（回到空白首页态）。 */
export function syncLoadParam(path) {
  const params = new URLSearchParams(location.search);
  const cur = params.get('load');
  const next = path || '';
  if (cur === next) return;
  if (path) params.set('load', path);
  else params.delete('load');
  if (path) params.delete('cacheKey');   // 两参数互斥：路径与 cacheKey 不同时携带
  const qs = params.toString();
  history.replaceState(null, '', location.pathname + (qs ? `?${qs}` : ''));
}

/** 把当前装配的 cacheKey 写回地址栏 ?cacheKey=（view 直载通道，不依赖
 * 源文件路径）。与 ?load= 互斥：设置一个时移除另一个。 */
export function syncCacheKeyParam(cacheKey) {
  const params = new URLSearchParams(location.search);
  const cur = params.get('cacheKey');
  if (cur === cacheKey) return;
  if (cacheKey) {
    params.set('cacheKey', cacheKey);
    params.delete('load');
  } else {
    params.delete('cacheKey');
  }
  const qs = params.toString();
  history.replaceState(null, '', location.pathname + (qs ? `?${qs}` : ''));
}

// 无 token 时的引导页（替代裸 prompt）。token 与浏览器绑定存储，换浏览器
// （如「用系统浏览器打开」时 URL 参数已丢失）或 token 失效时会出现此页。
export async function ensureToken() {
  if (getToken()) return;
  if (document.getElementById('token-gate')) return;
  const ov = document.createElement('div');
  ov.id = 'token-gate';
  ov.className = 'token-gate';
  const card = document.createElement('div');
  card.className = 'tg-card';

  const title = document.createElement('div');
  title.className = 'tg-title';
  title.textContent = '连接 CAD 服务';

  const desc = document.createElement('div');
  desc.className = 'tg-desc';
  desc.textContent = '此浏览器还没有访问授权（token 按浏览器存储，不跨浏览器共享）。';

  const way1 = document.createElement('div');
  way1.className = 'tg-way';
  way1.innerHTML = '<b>方式一（推荐）</b>：回到 agent 对话框，让它重新打开发布链接，授权会自动完成。';

  const way2 = document.createElement('div');
  way2.className = 'tg-way';
  way2.insertAdjacentHTML('beforeend', '<b>方式二</b>：粘贴服务 token（cad_service 启动时打印）：');
  const row = document.createElement('div');
  row.className = 'tg-input-row';
  const input = document.createElement('input');
  input.id = 'tg-input';
  input.type = 'text';
  input.placeholder = '服务 token';
  input.autocomplete = 'off';
  const btn = document.createElement('button');
  btn.id = 'tg-btn';
  btn.textContent = '连接';
  const connect = () => {
    const t = input.value.trim();
    if (!t) { input.focus(); return; }
    setToken(t);
    location.reload();   // 带 token 重载，页面正常引导
  };
  btn.addEventListener('click', connect);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') connect(); });
  row.append(input, btn);
  way2.appendChild(row);

  card.append(title, desc, way1, way2);
  ov.appendChild(card);
  document.body.appendChild(ov);
  input.focus();
}

// 读/写可访问目录（兜底文案）——失败不阻塞
export async function loadAllowedDirs(getConfig) {
  try {
    return (await getConfig()).allowed_dirs || [];
  } catch {
    return [];
  }
}

export function pathDeniedMsg(allowedDirs) {
  return '路径不在服务可访问目录内。请将文件移入：'
    + (allowedDirs.join('；') || '（未知）')
    + '；或重启服务时设置环境变量 CAD_SERVICE_ALLOWED_DIRS 添加目录（多个用 ; 分隔）';
}

// ---------- 状态条 ----------
export function bindStatus(el) {
  return (text, isError = false) => {
    el.textContent = text;
    el.className = isError ? 'error' : 'ok';
  };
}

// ---------- 最近使用 ----------
const RECENT_KEY = 'cad_recent_files';
const RECENT_MAX = 8;

export function loadRecent() {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY)) || [];
  } catch {
    return [];
  }
}

export function saveRecent(list) {
  localStorage.setItem(RECENT_KEY, JSON.stringify(list));
}

export function pushRecent(path, kind) {
  const list = loadRecent().filter((r) => r.path !== path);
  list.unshift({ path, kind, ts: Date.now() });
  saveRecent(list.slice(0, RECENT_MAX));
}

// ---------- 拖放覆盖层 ----------
export function bindDropOverlay(overlayEl, onFile) {
  let dragDepth = 0;
  // 仅当拖入的是真实文件（系统拖文件进浏览器）才显示覆盖层。
  // 内部元素/文本拖拽（dragenter 不带 Files 类型）一律忽略，避免
  // 拖动画布时误触发"松开以加载文件"提示。
  const isFileDrag = (e) =>
    !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
  window.addEventListener('dragenter', (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    dragDepth += 1;
    overlayEl?.classList.remove('hidden');
  });
  window.addEventListener('dragover', (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
  });
  window.addEventListener('dragleave', () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) overlayEl?.classList.add('hidden');
  });
  window.addEventListener('drop', (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    dragDepth = 0;
    overlayEl?.classList.add('hidden');
    const f = e.dataTransfer?.files?.[0];
    if (f) onFile(f);
  });
}

// ---------- 上传 + 扩展名路由 ----------
export async function handleUpload(uploadFn, file) {
  const res = await uploadFn(file);
  return res.path;
}

export function kindOfPath(p) {
  const ext = p.slice(p.lastIndexOf('.')).toLowerCase();
  if (ext === '.dxf' || ext === '.dwg') return 'drawing';
  return 'assembly';
}

// ---------- URL 生成（页面跳转） ----------
// 约定：Vite build 在 base=/app/ 下，编辑页是 /app/edit.html。开发模式用同源相对路径。
const BASE = (import.meta.env.BASE_URL || '/app/').replace(/\/$/, '');
export const PATHS = {
  home:    `${BASE}/index.html`,
  edit:    `${BASE}/edit.html`,
  drawing: `${BASE}/drawing.html`,
  report:  `${BASE}/report.html`,
};

export function goHome(params) {
  const qs = new URLSearchParams(params || {}).toString();
  location.href = PATHS.home + (qs ? `?${qs}` : '');
}
export function goEdit(params) {
  const qs = new URLSearchParams(params).toString();
  location.href = PATHS.edit + (qs ? `?${qs}` : '');
}
export function goDrawing(params) {
  const qs = new URLSearchParams(params).toString();
  location.href = PATHS.drawing + (qs ? `?${qs}` : '');
}
export function goReport(params) {
  const qs = new URLSearchParams(params).toString();
  location.href = PATHS.report + (qs ? `?${qs}` : '');
}

// URL scope 编解码（编辑页带入首页的选取范围）
export function encodeScope(scope) {
  const p = {};
  for (const k of ['cacheKey', 'level', 'nodeId', 'partIds', 'templateId']) {
    if (scope[k] != null) p[k] = typeof scope[k] === 'string' || typeof scope[k] === 'number'
      ? String(scope[k])
      : JSON.stringify(scope[k]);
  }
  return p;
}

export function readScopeFromUrl() {
  const sp = new URLSearchParams(location.search);
  const scope = {};
  scope.cacheKey = sp.get('cacheKey') || null;
  scope.level = sp.get('level') || null;
  scope.nodeId = sp.get('nodeId') || null;
  scope.templateId = sp.get('templateId') || null;
  try { scope.partIds = sp.get('partIds') ? new Set(JSON.parse(sp.get('partIds'))) : null; } catch {}
  return scope;
}

// ---------- 排版偏好（分屏为主 · 叠影为辅 · 四种预设） ----------
export const LAYOUT_PRESETS = ['split-h', 'split-v', 'ab-switch', 'overlay'];
const LAYOUT_KEY = 'cad_ui_layout_preset';
const LAYOUT_BREAKPOINT_MID = 1280;
const LAYOUT_BREAKPOINT_NARROW = 900;

export function defaultLayoutForWidth(w) {
  if (w >= LAYOUT_BREAKPOINT_MID) return 'split-h';
  if (w >= LAYOUT_BREAKPOINT_NARROW) return 'split-v';
  return 'ab-switch';
}

export function loadLayoutPreset() {
  const v = localStorage.getItem(LAYOUT_KEY);
  if (LAYOUT_PRESETS.includes(v)) return v;
  return defaultLayoutForWidth(window.innerWidth);
}

export function saveLayoutPreset(p) {
  localStorage.setItem(LAYOUT_KEY, p);
}

// 侧栏展开状态
const SIDEBAR_COLLAPSE_KEY = 'cad_ui_sidebar_collapsed';
export function loadSidebarCollapsed() {
  return localStorage.getItem(SIDEBAR_COLLAPSE_KEY) === '1';
}
export function saveSidebarCollapsed(v) {
  localStorage.setItem(SIDEBAR_COLLAPSE_KEY, v ? '1' : '0');
}

// 相机书签
export const CAM_KEY = 'cad_cam_bookmark';

// ---------- 草稿单槽位：存草稿元数据（前端读“继续未完成草稿”提示） ----------
// 草稿文件本体由后端管理：workspace/drafts/<cacheKey>.json（步骤表）。
// 前端在 localStorage 存一个轻量索引，用于首页提示。
const DRAFT_INDEX_KEY = 'cad_draft_index';

export function loadDraftIndex() {
  try {
    return JSON.parse(localStorage.getItem(DRAFT_INDEX_KEY)) || {};
  } catch { return {}; }
}

export function saveDraftIndexEntry(cacheKey, { baselineVersion, stepCount, baselineSourceFile }) {
  const idx = loadDraftIndex();
  idx[cacheKey] = { baselineVersion, stepCount, baselineSourceFile, ts: Date.now() };
  localStorage.setItem(DRAFT_INDEX_KEY, JSON.stringify(idx));
}

export function deleteDraftIndexEntry(cacheKey) {
  const idx = loadDraftIndex();
  delete idx[cacheKey];
  localStorage.setItem(DRAFT_INDEX_KEY, JSON.stringify(idx));
}

export function draftFor(cacheKey) {
  return loadDraftIndex()[cacheKey] || null;
}

// ---------- 全局错误捕获（页面加载不了/瘫痪时的可观测性） ----------
// window error + unhandledrejection 统一上报 POST /api/logs/client，
// 服务端写入 workspace/logs/service.log（WARNING 级，含堆栈）。
// 同时挂 window.__cadErrors 数组，现场可直接在控制台查看。
let _trapArmed = false;
let _lastReport = { msg: '', ts: 0 };

export function initErrorTrap() {
  if (_trapArmed) return;
  _trapArmed = true;
  window.__cadErrors = [];

  const report = (msg, stack) => {
    try {
      window.__cadErrors.push({ ts: new Date().toISOString(), msg, stack });
    } catch { /* 忽略 */ }
    // 节流：同一消息 3 秒内只报一次（渲染循环里连续抛错会刷屏）
    const now = Date.now();
    if (msg === _lastReport.msg && now - _lastReport.ts < 3000) return;
    _lastReport = { msg, ts: now };
    const payload = JSON.stringify({
      page: location.pathname,
      message: String(msg).slice(0, 500),
      stack: String(stack || '').slice(0, 2000),
    });
    // keepalive：页面即将卸载时也能送出；上报本身失败不再上报（防递归）
    fetch('/api/logs/client', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${localStorage.getItem('cad_service_token') || ''}`,
      },
      body: payload,
      keepalive: true,
    }).catch(() => {});
  };

  window.addEventListener('error', (e) => {
    report(e.message || 'unknown error',
      e.error && e.error.stack ? e.error.stack : '');
  });
  window.addEventListener('unhandledrejection', (e) => {
    const r = e.reason;
    report(`unhandled rejection: ${r && r.message ? r.message : r}`,
      r && r.stack ? r.stack : '');
  });
}
