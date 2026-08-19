const TOKEN_KEY = 'cad_service_token';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

export function setToken(t) {
  localStorage.setItem(TOKEN_KEY, t);
}

export async function parseAssembly(inputPath, force = false) {
  const r = await fetch('/api/assembly/parse', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${getToken()}`,
    },
    body: JSON.stringify({ input_path: inputPath, force }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body; // { cache_key, cache_hit, base_url, manifest }
}
