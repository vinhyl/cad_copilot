import './style.css';
import { AssemblyScene } from './scene.js';
import { AssemblyTree } from './tree.js';
import { getToken, setToken, parseAssembly } from './api.js';

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
  onSelect: (id) => scene.highlight(tree.partIdsUnder(id)),
  onToggle: (visMap) => scene.applyVisibility(visMap),
});
scene.onPick((id) => tree.select(id));

// ---- 加载表单 ----
const form = document.getElementById('load-form');
const pathInput = document.getElementById('path-input');
const forceBox = document.getElementById('force');
const loadBtn = document.getElementById('load-btn');
const statusEl = document.getElementById('status');

function status(text, isError = false) {
  statusEl.textContent = text;
  statusEl.className = isError ? 'error' : 'ok';
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const p = pathInput.value.trim();
  if (!p) return;
  loadBtn.disabled = true;
  status('解析中…');
  try {
    const res = await parseAssembly(p, forceBox.checked);
    const count = await scene.load(res.manifest, res.base_url);
    tree.render(res.manifest.root);
    scene.highlight(null);
    status(
      `${res.manifest.source_file} · ${res.manifest.templates.length} 模板 · ` +
      `${count} 实例 · ${res.cache_hit ? '缓存命中' : '新建缓存'}`,
    );
  } catch (err) {
    status(`错误：${err.message}`, true);
  } finally {
    loadBtn.disabled = false;
  }
});
