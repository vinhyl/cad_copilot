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

/** Phase C: 编辑（干涉守门 + 原子提交）。409 时抛出带 interferences 的错误。 */
export async function editAssembly(cacheKey, templateId, operation, params) {
  return _post('/api/assembly/edit', {
    cache_key: cacheKey, template_id: templateId,
    operation, params,
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
