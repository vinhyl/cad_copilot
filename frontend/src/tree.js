/**
 * 装配树 UI（copilot-vision「选择层级交互原则」骨架）：
 *   - 装配树是主锚点：点哪个节点选哪个层级（零件/子装配体），与 3D 视口双向同步
 *   - 复位分层预留：显隐复选框按节点层级生效（父级隐藏连带后代）
 */
export class AssemblyTree {
  constructor(container, { onSelect, onToggle }) {
    this.container = container;
    this.onSelect = onSelect;   // (nodeId) => void
    this.onToggle = onToggle;   // (visibleByPartId: Map) => void
    this.nodes = new Map();     // id -> { node, parentId }
    this.visible = new Map();   // id -> bool（节点自身复选框状态）
    this.rows = new Map();      // id -> row element
    this.selectedId = null;
  }

  render(root) {
    this.container.innerHTML = '';
    this.nodes.clear();
    this.visible.clear();
    this.rows.clear();
    this.selectedId = null;
    this.container.appendChild(this._build(root, null));
  }

  /** 树上选中某节点（外部 3D 点选回调进来时同步）。 */
  select(nodeId) {
    if (this.selectedId && this.rows.has(this.selectedId)) {
      this.rows.get(this.selectedId).classList.remove('selected');
    }
    this.selectedId = nodeId;
    const row = this.rows.get(nodeId);
    if (row) {
      row.classList.add('selected');
      // 只滚动树容器自身（scrollIntoView 会连带滚动页面等祖先容器）
      const cRect = this.container.getBoundingClientRect();
      const rRect = row.getBoundingClientRect();
      if (rRect.height > 0) {
        if (rRect.top < cRect.top) this.container.scrollTop -= cRect.top - rRect.top;
        else if (rRect.bottom > cRect.bottom) this.container.scrollTop += rRect.bottom - cRect.bottom;
      }
    }
  }

  /** 某节点下所有 part 实例的 id 集合（高亮作用域）。 */
  partIdsUnder(nodeId) {
    const out = new Set();
    const stack = [nodeId];
    while (stack.length) {
      const id = stack.pop();
      const rec = this.nodes.get(id);
      if (!rec) continue;
      if (rec.node.type === 'part') out.add(id);
      (rec.node.children || []).forEach((c) => stack.push(c.id));
    }
    return out;
  }

  /** 每个零件节点的有效可见性 = 自身 AND 全部祖先。 */
  effectiveVisibility() {
    const eff = new Map();
    for (const [id, rec] of this.nodes) {
      if (rec.node.type !== 'part') continue;
      let v = this.visible.get(id) !== false;
      let pid = rec.parentId;
      while (pid && v) {
        if (this.visible.get(pid) === false) v = false;
        pid = this.nodes.get(pid)?.parentId ?? null;
      }
      eff.set(id, v);
    }
    return eff;
  }

  // ------------------------------------------------------------------

  _build(node, parentId) {
    this.nodes.set(node.id, { node, parentId });
    this.visible.set(node.id, true);

    const wrap = document.createElement('div');
    wrap.className = 'tnode';

    const row = document.createElement('div');
    row.className = 'trow';
    this.rows.set(node.id, row);

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = true;
    cb.title = '显示 / 隐藏';
    cb.addEventListener('click', (e) => e.stopPropagation());
    cb.addEventListener('change', () => {
      this.visible.set(node.id, cb.checked);
      this.onToggle(this.effectiveVisibility());
    });

    const label = document.createElement('span');
    label.className = 'tname';
    label.textContent = node.name;

    if (node.type === 'assembly') {
      const caret = document.createElement('span');
      caret.className = 'caret';
      caret.textContent = '▾';
      const kids = document.createElement('div');
      kids.className = 'tchildren';
      caret.addEventListener('click', (e) => {
        e.stopPropagation();
        const collapsed = kids.classList.toggle('collapsed');
        caret.textContent = collapsed ? '▸' : '▾';
      });
      const badge = document.createElement('span');
      badge.className = 'badge asm';
      badge.textContent = `${(node.children || []).length} 件`;
      row.append(caret, cb, label, badge);
      (node.children || []).forEach((c) => kids.appendChild(this._build(c, node.id)));
      wrap.append(row, kids);
    } else {
      const badge = document.createElement('span');
      badge.className = 'badge part';
      badge.textContent = '零件';
      // 换件后的新零件：节点标「已替换」（node.replaced 由换件身份逻辑标记）
      if (node.replaced) {
        badge.classList.add('replaced');
        badge.textContent = '已替换';
      }
      row.append(cb, label, badge);
      wrap.append(row);
    }

    row.addEventListener('click', () => {
      this.select(node.id);
      this.onSelect(node.id);
    });
    return wrap;
  }
}
