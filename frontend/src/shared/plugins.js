// 插件状态探测 + 角标渲染（首页入口卡片显示插件可用性）
// 不再占侧栏一整块，降级为角标或小面板。

export const PLUGIN_DEFS = [
  { key: 'oda',     name: '图纸转换 ODA',  what: 'DWG → DXF 转换（图纸对照）' },
  { key: 'fea',     name: '力学分析 FEA',  what: 'FreeCAD + CalculiX（静力学）' },
  { key: 'blender', name: '离线渲染 Blender', what: 'Cycles 静止帧渲染' },
];

export async function refreshPlugins(getPlugins) {
  let state = null;
  try {
    state = await getPlugins();
  } catch {
    state = null;
  }
  return state;
}

// 首页入口卡片角标渲染：在入口按钮内打上"可用/未安装"圆点
export function renderPluginIndicator(el, { available, hint }) {
  const dot = el.querySelector('.plug-dot');
  const st = el.querySelector('.plug-state');
  if (!dot || !st) return;
  if (available) {
    dot.classList.add('on');
    st.textContent = '可用';
    st.classList.remove('off');
  } else {
    dot.classList.remove('on');
    st.textContent = hint ? '未安装（提示）' : '未安装';
    st.classList.add('off');
  }
  el.title = available ? '' : hint || '';
}

// 侧栏小面板（共享层渲染器：首页/图纸/编辑都可能引用，当前仅首页放底部小格）
export function renderPluginPanel(listEl, state) {
  listEl.innerHTML = '';
  for (const def of PLUGIN_DEFS) {
    const st = state?.[def.key] || {};
    const row = document.createElement('div');
    row.className = 'plug-row';
    const dot = document.createElement('span');
    dot.className = `plug-dot${st.available ? ' on' : ''}`;
    const name = document.createElement('span');
    name.className = 'plug-name';
    name.textContent = def.name;
    const s = document.createElement('span');
    s.className = `plug-state${st.available ? '' : ' off'}`;
    s.textContent = st.available ? '可用' : '未安装';
    row.append(dot, name, s);
    row.title = st.available
      ? `${def.what}\n${st.path || st.freecad || st.blender || ''}`
      : (st.hint || def.what);
    listEl.appendChild(row);
  }
}
