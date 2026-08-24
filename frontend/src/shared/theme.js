// 模型配色控件：在视口 toolbar 注入"配色"按钮组，写入 localStorage 跨页面记忆。
// 作用于 AssemblyScene.setModelColor（模型表面色 + 背景 + 平滑渐变环境 + 强度/曝光）。
// 模型色用低亮度/低饱和色，缓和白色高光在亮角度的刺眼过曝。

const THEME_LABELS = {
  neutral: '原色',
  slate:   '月灰',
  steel:   '钢蓝',
  sand:    '暖沙',
  olive:   '墨绿',
  ivory:   '米白',
};

export function installThemeControls(scenes, mountEl) {
  if (!mountEl || !scenes || !scenes.length) return;
  const saved = localStorage.getItem('cad-model-color') || 'neutral';

  const group = document.createElement('div');
  group.className = 'tb-group theme-group';
  group.title = '模型配色：切换模型表面颜色（低亮度色缓和过曝刺眼）';
  const label = document.createElement('span');
  label.className = 'tb-label';
  label.textContent = '配色';
  group.appendChild(label);

  const apply = (name) => {
    scenes.forEach((s) => { if (s && s.setModelColor) s.setModelColor(name); });
    try { localStorage.setItem('cad-model-color', name); } catch (e) { /* 隐私模式忽略 */ }
    group.querySelectorAll('.theme-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.theme === name));
  };

  Object.entries(THEME_LABELS).forEach(([name, text]) => {
    const btn = document.createElement('button');
    btn.className = 'theme-btn' + (name === saved ? ' active' : '');
    btn.dataset.theme = name;
    btn.textContent = text;
    btn.addEventListener('click', () => apply(name));
    group.appendChild(btn);
  });

  mountEl.appendChild(group);
  apply(saved);   // 应用已记忆配色（覆盖 scene 构造时的默认 neutral）
}
