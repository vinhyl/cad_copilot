const TOKEN_KEY = 'cad_service_token';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

export function setToken(t) {
  localStorage.setItem(TOKEN_KEY, t);
}

export async function parseAssembly(inputPath, force = false) {
  return _post('/api/assembly/parse', { input_path: inputPath, force });
}

/** Phase C: 编辑（干涉守门 + 原子提交）。409 时抛出带 interferences 的错误。
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
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
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
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

/** D5 图纸导入：DXF 原生 / DWG 经 ODA → 语义 + SVG 缓存。 */
export async function importDrawing(inputPath) {
  return _post('/api/drawing/import', { input_path: inputPath });
}

/** D5/D7 插件探测：ODA / FreeCAD+CalculiX / Blender 可用性。 */
export async function getPlugins() {
  const r = await fetch('/api/plugins', {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

// 可访问目录（UI 引导：输入路径需位于其中之一）
export async function getConfig() {
  const r = await fetch('/api/config', {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
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
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
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
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
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
    const err = new Error(body.error || body.message || `HTTP ${r.status}`);
    err.status = r.status;
    err.payload = body;      // 409 干涉拒绝的结构化数据（R15）
    throw err;
  }
  return body;
}
