// 图纸对照独立页（从原 modal 提升）
//  - URL 支持：?cacheKey=xxx（装配体缓存键，用于"从首页打开图纸对照"时附带装配体上下文）
//              ?path=<encode 图纸文件>（支持从首页最近图纸/拖放直接进入）
//  - 顶部：返回首页、标题、导入/拖放入口、结果摘要
//  - 主体：左 SVG 视图 + 右语义列表（沿用原 drawing-body 结构）

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

// 导入 + 渲染主体
async function runImport(p) {
  statusFn('导入中…');
  try {
    const res = await importDrawing(p);
    const summary = `${res.source_file}${res.cache_hit ? ' · 缓存命中' : ''} · `
      + `${res.oda_used ? 'ODA 转换' : 'DXF 直读'} · ${res.entity_count} 实体`;
    $('#drawing-msg').textContent = summary;
    statusFn(summary);
    const r = await fetch(`${res.base_url}/view.svg`);
    $('#drawing-view').innerHTML = await r.text();
    const sem = $('#drawing-semantics');
    sem.innerHTML = '';
    res.semantics.forEach((s) => {
      const row = document.createElement('div'); row.className = 'fp-row';
      const kind = document.createElement('span'); kind.className = 'sem-kind';
      kind.textContent = { thread: '螺纹', diameter: '直径', tolerance: '公差', note: '标注' }[s.kind] || s.kind;
      const val = document.createElement('span'); val.className = 'fp-name'; val.textContent = s.text;
      row.append(kind, val);
      sem.append(row);
    });
    pushRecent(p, 'drawing');
  } catch (err) {
    const denied = err.message.includes('outside allowed dirs');
    statusFn(denied ? '路径不在服务可访问目录内。请确认 CAD_SERVICE_ALLOWED_DIRS 设置。' : `错误：${err.message}`, true);
    $('#drawing-msg').textContent = '加载失败';
  }
}

$('#drawing-browse').addEventListener('click', () => $('#drawing-file-input').click());
$('#drawing-file-input').addEventListener('change', async (e) => {
  const f = e.target.files?.[0];
  e.target.value = '';
  if (!f) return;
  statusFn(`上传中… ${f.name}`);
  try {
    const p = await handleUpload(uploadFile, f);
    runImport(p);
  } catch (err) { statusFn(`上传失败：${err.message}`, true); }
});

bindDropOverlay($('#drop-overlay'), async (f) => {
  statusFn(`上传中… ${f.name}`);
  try {
    const p = await handleUpload(uploadFile, f);
    runImport(p);
  } catch (err) { statusFn(`上传失败：${err.message}`, true); }
});

// 由 URL path 参数直接触发
if (urlPathRaw) runImport(decodeURIComponent(urlPathRaw));
else if (bootLoadPath) runImport(bootLoadPath);
else statusFn('浏览或拖入 DXF / DWG 文件以加载（独立图纸对照页）');
// ?load= 已被本页消费（首页导入跳转而来），清除以免返回首页时重复导入；
// ?path=（本页自身参数）同理消费后清除
syncLoadParam(null);
