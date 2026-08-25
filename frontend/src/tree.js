/**
 * 装配树 UI（copilot-vision「选择层级交互原则」骨架）：
 *   - 装配树是主锚点：点哪个节点选哪个层级（零件/子装配体），与 3D 视口双向同步
 *   - 复位分层预留：显隐复选框按节点层级生效（父级隐藏连带后代）
 */
export class AssemblyTree {
  constructor(container, { onSelect, onToggle, dnd, toolbar } = {}) {
    this.container = container;
    this.onSelect = onSelect;   // (nodeId) => void
    this.onToggle = onToggle;   // (visibleByPartId: Map) => void
    // dnd：{ onDrop(nodeId, parentId) } —— 拖拽改变层级（仅草稿树传）。
    // toolbar：{ onGroupCreate(parentId), onGroupDissolve(nodeId) } ——
    // 分组工具条（新建空分组 / 解散分组），缺省则不渲染、保持只读。
    this.dnd = dnd || null;
    this.toolbar = toolbar || null;
    this.nodes = new Map();     // id -> { node, parentId }
    this.visible = new Map();   // id -> bool（节点自身复选框状态）
    this.rows = new Map();      // id -> row element
    this.selectedId = null;
    this._rootId = null;
    this._dragId = null;
  }

  render(root) {
    this.container.innerHTML = '';
    this.nodes.clear();
    this.visible.clear();
    this.rows.clear();
    this.selectedId = null;
    this._rootId = root.id;
    if (this.toolbar) this._buildToolbar();
    this.container.appendChild(this._build(root, null));
    // 事件委托统一处理拖拽（草稿树且启用了 dnd）
    if (this.dnd) this._wireDragHandlers();
    this._updateGroupControls();
  }

  /**
   * 分组工具条：作用于当前选中节点。
   *  - 「＋新建分组」：选中为装配节点时，在其下新建一个空分组
   *  - 「解散分组」：选中为装配节点且非 root 时，解散（子节点上提一级）
   */
  _buildToolbar() {
    const bar = document.createElement('div');
    bar.className = 'tree-toolbar';
    const mk = (id, text, title) => {
      const b = document.createElement('button');
      b.id = id; b.textContent = text; b.title = title; b.type = 'button';
      return b;
    };
    this._btnGroup = mk('tt-btn-group', '＋ 新建分组', '在选中装配节点下新建空分组');
    this._btnDissolve = mk('tt-btn-dissolve', '解散分组', '解散选中分组（子节点上提一级）');
    bar.append(this._btnGroup, this._btnDissolve);
    this.container.appendChild(bar);
    this._btnGroup.addEventListener('click', () => {
      if (this._canGroup()) this.toolbar.onGroupCreate(this.selectedId);
      else this._flashHint('新建分组需选中一个装配节点');
    });
    this._btnDissolve.addEventListener('click', () => {
      if (this._canDissolve()) this.toolbar.onGroupDissolve(this.selectedId);
      else this._flashHint('解散分组需选中一个非根的装配节点');
    });
  }

  _flashHint() { /* 占位：禁用态已在按钮上提示，无需额外动作 */ }

  _canGroup() {
    const rec = this.selectedId ? this.nodes.get(this.selectedId) : null;
    return !!rec && rec.node.type === 'assembly';
  }
  _canDissolve() {
    const rec = this.selectedId ? this.nodes.get(this.selectedId) : null;
    return !!rec && rec.node.type === 'assembly' && this.selectedId !== this._rootId;
  }
  _updateGroupControls() {
    if (!this.toolbar) return;
    for (const [btn, can] of [[this._btnGroup, this._canGroup()],
                              [this._btnDissolve, this._canDissolve()]]) {
      btn.disabled = !can;
      btn.classList.toggle('hint-disabled', !can);
    }
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
    this._updateGroupControls();
  }

  /** idA 是否是 idB 的祖先（沿 parent 链）。 */
  _isAncestor(idA, idB) {
    let cur = idB;
    while (cur) {
      if (cur === idA) return true;
      cur = this.nodes.get(cur)?.parentId ?? null;
    }
    return false;
  }

  /** 一次给多个节点设置 class（仅在目标行存在时）。 */
  _setDropClass(nodeId, className, on) {
    const row = this.rows.get(nodeId);
    if (row) row.classList.toggle(className, on);
  }

  _isLegalDropTarget(targetId) {
    if (!this._dragId || targetId === this._dragId) return false;
    const rec = this.nodes.get(targetId);
    if (!rec || rec.node.type !== 'assembly') return false;   // 只能放装配节点
    if (this._isAncestor(this._dragId, targetId)) return false;  // 不能放自身/后代
    return true;
  }

  _wireDragHandlers() {
    // 只绑定一次：拖拽用事件委托 + 行 dataset.nodeId，多 render 复用即可。
    if (this._dndBound) return;
    this._dndBound = true;
    this.container.addEventListener('dragstart', (e) => {
      const row = e.target.closest('.trow');
      if (!row) return;
      const id = row.dataset.nodeId;
      if (!id) return;
      this._dragId = id;
      row.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      // Firefox 需要设置数据才能触发 dragover
      try { e.dataTransfer.setData('text/plain', id); } catch (_) {}
    });
    this.container.addEventListener('dragover', (e) => {
      const row = e.target.closest('.trow');
      if (!row || !this._dragId) return;
      const id = row.dataset.nodeId;
      if (this._isLegalDropTarget(id)) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        this._setDropClass(this._lastDropId, 'drop-target', false);
        this._setDropClass(id, 'drop-target', true);
        this._lastDropId = id;
      } else {
        this._setDropClass(id, 'drop-invalid', true);
        this._setDropClass(this._lastDropId, 'drop-target', false);
      }
    });
    this.container.addEventListener('dragleave', (e) => {
      const row = e.target.closest('.trow');
      if (row) { this._setDropClass(row.dataset.nodeId, 'drop-target', false);
                 this._setDropClass(row.dataset.nodeId, 'drop-invalid', false); }
    });
    this.container.addEventListener('drop', (e) => {
      const row = e.target.closest('.trow');
      if (!row || !this._dragId) return;
      const targetId = row.dataset.nodeId;
      if (this._isLegalDropTarget(targetId)) {
        e.preventDefault();
        this.dnd.onDrop(this._dragId, targetId);
      }
    });
    this.container.addEventListener('dragend', () => {
      if (this._dragId) this._setDropClass(this._dragId, 'dragging', false);
      for (const id of this.rows.keys()) {
        this._setDropClass(id, 'drop-target', false);
        this._setDropClass(id, 'drop-invalid', false);
      }
      this._dragId = null;
      this._lastDropId = null;
    });
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
    row.dataset.nodeId = node.id;
    if (this.dnd) row.draggable = true;   // 仅草稿树可拖拽调层级
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
