const TOKEN_KEY = 'cad_service_token';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

export function setToken(t) {
  localStorage.setItem(TOKEN_KEY, t);
}

/** M6：401 统一处理。服务重启会换 token（尤其 agent 未固定
 * CAD_SERVICE_TOKEN 时），换到系统浏览器打开的旧 tab 会全线 401——
 * 清掉失效 token 并给出可行动的指引，避免用户面对裸 "HTTP 401"。 */
function _guard401(r) {
  if (r.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    throw new Error('登录已失效（服务重启或 token 变更）：请让 agent 重新打开预览链接');
  }
}

export async function parseAssembly(inputPath, force = false) {
  return _post('/api/assembly/parse', { input_path: inputPath, force });
}

/** 按 cacheKey 直载缓存（GET /api/assembly/view）。不读源文件——
 * 回首页/最近列表/跨浏览器恢复用，源文件移动或删除不影响加载。 */
export async function viewAssembly(cacheKey) {
  const r = await fetch(
    `/api/assembly/view?cache_key=${encodeURIComponent(cacheKey)}`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { _guard401(r); throw new Error(body.error || `HTTP ${r.status}`); }
  return body;
}

/** Phase C: 编辑（原子提交；干涉仅前端提醒）。409 时抛出带 interferences 的错误。
 * featureId 提供时为定点特征编辑（R1），否则整模板编辑。 */
export async function editAssembly(cacheKey, templateId, operation, params, featureId = null) {
  return _post('/api/assembly/edit', {
    cache_key: cacheKey, template_id: templateId,
    operation, params,
    ...(featureId != null ? { feature_id: featureId } : {}),
  });
}

export async function listVersions(cacheKey) {
  const r = await fetch(`/api/versions?cache_key=${encodeURIComponent(cacheKey)}`, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { _guard401(r); throw new Error(body.error || `HTTP ${r.status}`); }
  return body;
}

export async function checkoutVersion(cacheKey, version) {
  return _post('/api/versions/checkout', { cache_key: cacheKey, version });
}

/** 模块七一键体检：干涉 + DFM。 */
export async function auditAssembly(cacheKey) {
  const r = await fetch(
    `/api/assembly/audit?cache_key=${encodeURIComponent(cacheKey)}`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { _guard401(r); throw new Error(body.error || `HTTP ${r.status}`); }
  return body;
}

// ==========================================================================
// M2: 草稿 API（声明式步骤表 · 多目标 · 单槽位 · 增量干涉）
// 步骤表结构：[{id, template_id, operation, params, feature_id?}]
// ==========================================================================

/** 加载草稿（GET /api/drafts?cache_key=ck）。无草稿返回 {empty: true, steps: []}。 */
export async function loadDraft(cacheKey) {
  const r = await fetch(
    `/api/drafts?cache_key=${encodeURIComponent(cacheKey)}`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { _guard401(r); throw new Error(body.error || `HTTP ${r.status}`); }
  return body;
}

/** 单槽位整体覆盖保存草稿（POST /api/drafts/save）。
 * client = 本 tab 标识（M6）：服务端广播 draft_saved 时原样带回，
 * 收到事件的 tab 据此忽略自己发出的保存。 */
export function saveDraft(cacheKey, { baselineVersion, baselineSourceFile, steps, client }) {
  return _post('/api/drafts/save', {
    cache_key: cacheKey,
    baseline_version: baselineVersion,
    baseline_source_file: baselineSourceFile,
    steps,
    ...(client ? { client } : {}),
  });
}

/** 放弃草稿（DELETE /api/drafts?cache_key=ck）。幂等。 */
export async function deleteDraft(cacheKey, client = '') {
  const r = await fetch(
    `/api/drafts?cache_key=${encodeURIComponent(cacheKey)}`
    + `&client=${encodeURIComponent(client)}`, {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${getToken()}` },
    });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { _guard401(r); throw new Error(body.error || `HTTP ${r.status}`); }
  return body;
}

/** 草稿预览（POST /api/drafts/preview）。level='bbox'（默认）：AABB
 * 快速反馈（拖拽级交互）；'exact'：布尔精检（显式触发给用户自查）。 */
export async function previewDraft(cacheKey, steps, level = 'bbox') {
  const r = await _post('/api/drafts/preview', {
    cache_key: cacheKey, steps, level,
  });
  return r;
}

/** 把草稿全部步骤落为一条版本（POST /api/drafts/confirm）。 */
export async function confirmDraft(cacheKey, steps) {
  const r = await fetch('/api/drafts/confirm', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${getToken()}`,
    },
    body: JSON.stringify({ cache_key: cacheKey, steps }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    _guard401(r);
    const err = new Error(body.error || `HTTP ${r.status}`);
    err.status = r.status;
    err.payload = body;   // 409 干涉拒绝的结构化数据
    throw err;
  }
  return body;
}

/** M5：FEA 基线 vs 草稿双跑对比（R5 异步任务，202 + job_id）。
 * FEA 插件缺失抛 503（err.payload.kind === 'missing'）。 */
export async function startFeaCompare(cacheKey, templateId, steps, spec = {}) {
  const r = await fetch('/api/drafts/fea-compare', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${getToken()}`,
    },
    body: JSON.stringify({
      cache_key: cacheKey, template_id: templateId, steps, spec,
    }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    _guard401(r);
    const err = new Error(body.error || `HTTP ${r.status}`);
    err.status = r.status;
    err.payload = body;   // 503 结构化 missing
    throw err;
  }
  return body;
}

// ==========================================================================
// M4: 报告中心 API（快照报告：体检 + 统计 + 版本历史）
// ==========================================================================

/** 生成快照报告并落盘（POST /api/reports/generate），返回完整报告。 */
export function generateReport(cacheKey) {
  return _post('/api/reports/generate', { cache_key: cacheKey });
}

/** 报告列表（GET /api/reports?cache_key=ck），新→旧，只含摘要。 */
export async function listReports(cacheKey) {
  const r = await fetch(
    `/api/reports?cache_key=${encodeURIComponent(cacheKey)}`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { _guard401(r); throw new Error(body.error || `HTTP ${r.status}`); }
  return body;
}

/** 读单份完整报告（GET /api/reports/get）。 */
export async function getReport(cacheKey, reportId) {
  const q = `cache_key=${encodeURIComponent(cacheKey)}`
    + `&report_id=${encodeURIComponent(reportId)}`;
  const r = await fetch(`/api/reports/get?${q}`, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { _guard401(r); throw new Error(body.error || `HTTP ${r.status}`); }
  return body;
}

/** D5 图纸导入：DXF 原生 / DWG 经 ODA → 语义 + SVG 缓存。force=1 强制重建。 */
export async function importDrawing(inputPath, force = false) {
  return _post('/api/drawing/import', { input_path: inputPath, force: !!force });
}

/** D5/D7 插件探测：ODA / FreeCAD+CalculiX / Blender 可用性。 */
export async function getPlugins() {
  const r = await fetch('/api/plugins', {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { _guard401(r); throw new Error(body.error || `HTTP ${r.status}`); }
  return body;
}

// 可访问目录（UI 引导：输入路径需位于其中之一）
export async function getConfig() {
  const r = await fetch('/api/config', {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { _guard401(r); throw new Error(body.error || `HTTP ${r.status}`); }
  return body;
}

/** 上传文件（显式授权通道）：原始体 POST，文件名走 ?name=，返回服务端落盘路径。 */
export async function uploadFile(file) {
  const r = await fetch(`/api/upload?name=${encodeURIComponent(file.name)}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${getToken()}` },
    body: file,
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { _guard401(r); throw new Error(body.error || `HTTP ${r.status}`); }
  return body;
}

/** R5 异步 FEA：202 + {job_id, url}，轮询 getJob。 */
export function startFeaJob(cacheKey, templateId, spec = {}) {
  return _post('/api/fea/static', {
    cache_key: cacheKey, template_id: templateId, spec, async: true,
  });
}

/** R5 异步渲染：202 + {job_id, url}。 */
export function startRenderJob(cacheKey, spec = {}) {
  return _post('/api/render', { cache_key: cacheKey, spec, async: true });
}

/** R5 任务查询：{status, progress, result, error, ...}。 */
export async function getJob(jobId) {
  const r = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { _guard401(r); throw new Error(body.error || `HTTP ${r.status}`); }
  return body;
}

/** R5 协作取消（幂等：对已完成任务返回最终状态）。 */
export function cancelJob(jobId) {
  return _post(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {});
}

async function _post(url, payload) {
  const r = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${getToken()}`,
    },
    body: JSON.stringify(payload),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    _guard401(r);
    const err = new Error(body.error || body.message || `HTTP ${r.status}`);
    err.status = r.status;
    err.payload = body;      // 409 干涉拒绝的结构化数据（R15）
    throw err;
  }
  return body;
}

// ==========================================================================
// M6: agent 通信回路 API（选中上行 + 会话发现）
// ==========================================================================

/** 用户选中上行（POST /api/selection）。fire-and-forget 场景下由调用方
 * 吞错——上行失败不阻塞点选交互。 */
export function postSelection(payload) {
  return _post('/api/selection', payload);
}

/** 读用户选中（GET /api/selection[?cache_key=ck]）。带 cacheKey 返回该
 * 会话的选中，不带返回全部会话中最新的一条。 */
export async function getSelection(cacheKey = '') {
  const q = cacheKey ? `?cache_key=${encodeURIComponent(cacheKey)}` : '';
  const r = await fetch(`/api/selection${q}`, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { _guard401(r); throw new Error(body.error || `HTTP ${r.status}`); }
  return body;
}

/** 活跃会话列表（GET /api/sessions）。 */
export async function listSessions() {
  const r = await fetch('/api/sessions', {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { _guard401(r); throw new Error(body.error || `HTTP ${r.status}`); }
  return body;
}
