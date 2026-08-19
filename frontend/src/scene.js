import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { TransformControls } from 'three/addons/controls/TransformControls.js';

const BASE_COLOR = new THREE.Color(0xffffff);
const SELECT_COLOR = new THREE.Color(0xff8a3d);
const ZERO_SCALE = new THREE.Matrix4().makeScale(0, 0, 0);

/**
 * 3D 视口（ADR-0002 D3 + Phase B 视图操作集）。
 *
 * 渲染模型：每个唯一零件模板一份 glTF 几何，实例共享 InstancedMesh；
 * 实例矩阵 = T(累积爆炸 × ratio + 临时移动) × base（base = manifest 世界 4x4）。
 *
 * 视图操作集（视图状态 ≠ 数据状态，全部不落盘）：
 *   applyExplosion(ratio)   多层级爆炸（manifest 相对 explode 向量沿祖先链累积）
 *   applyVisibility(map)    按零件节点显隐（零缩放矩阵技巧）
 *   setXray(on)             透明鬼影模式
 *   setSection(on, z)       Z 轴剖切面（clipping plane）
 *   enableMove(ids)/…       临时拖拽移动（TransformControls，不落盘）
 *   resetTempMoves()        复位全部临时移动（复位分层中的「全部」档）
 *   showFeature(…)          特征拾取 overlay（cache features glTF 按名取节点）
 *   getCameraState/setCameraState  视角书签（持久化由调用方负责）
 */
export class AssemblyScene {
  constructor(container) {
    this.container = container;
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(window.devicePixelRatio);
    this.renderer.localClippingEnabled = true;
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x1c1c20);
    this.camera = new THREE.PerspectiveCamera(50, 1, 0.01, 100000);
    this.camera.position.set(60, 40, 60);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x3a3a44, 1.1));
    const dir = new THREE.DirectionalLight(0xffffff, 1.4);
    dir.position.set(50, 80, 30);
    this.scene.add(dir);
    // OCP 导出的 glTF 材质 metalness/roughness 常为 1.0：无环境贴图时
    // 金属面渲染成暗色。RoomEnvironment 提供中性 PBR 环境光修正此问题。
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

    this.group = new THREE.Group();
    this.scene.add(this.group);
    this.overlay = new THREE.Group();          // 特征拾取 overlay
    this.scene.add(this.overlay);

    // 状态
    this.instances = new Map();   // partNodeId -> {mesh, index, base, temp:[x,y,z], visible}
    this.nodeInfo = new Map();    // nodeId -> {parentId, explode, type, children:[ids]}
    this.accExplode = new Map();  // partNodeId -> [x,y,z]（沿祖先链累积）
    this.explodeRatio = 0;
    this.xray = false;
    this.sectionPlane = new THREE.Plane(new THREE.Vector3(0, 0, -1), 1e9);
    this.bbox = new THREE.Box3();
    this.templateBoxes = new Map();
    this.featureGltfCache = new Map();  // templateId -> {nodes:Map(name->Object3D)}
    this.featureState = null;           // {templateId, instanceId, featureId}
    this.pickHandler = null;

    // 拖拽移动（TransformControls + 代理 Object3D）
    this.moveIds = new Set();       // 当前可移动的 part 节点集合
    this.moveProxy = new THREE.Object3D();
    this.scene.add(this.moveProxy);   // TransformControls 要求 attach 目标在场景图内
    this.moveControls = new TransformControls(this.camera, this.renderer.domElement);
    this.moveControls.setMode('translate');
    this.moveControls.setSize(0.8);
    this.moveControls.attach(this.moveProxy);
    this.moveControls.enabled = false;
    this.moveControls.visible = false;
    this.scene.add(this.moveControls);
    this._lastProxyPos = new THREE.Vector3();
    this.moveControls.addEventListener('dragging-changed', (e) => {
      this.controls.enabled = !e.value;
    });
    this.moveControls.addEventListener('objectChange', () => {
      const delta = this.moveProxy.position.clone().sub(this._lastProxyPos);
      this._lastProxyPos.copy(this.moveProxy.position);
      for (const id of this.moveIds) {
        const inst = this.instances.get(id);
        if (!inst) continue;
        inst.temp = [
          inst.temp[0] + delta.x, inst.temp[1] + delta.y, inst.temp[2] + delta.z];
      }
      this._updateMatrices();
      this._syncOverlay();
    });

    this._resize();
    new ResizeObserver(() => this._resize()).observe(container);
    this.renderer.setAnimationLoop(() => {
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    });
    this._bindPick();
  }

  onPick(cb) { this.pickHandler = cb; }

  async load(manifest, baseUrl) {
    this._clear();
    const loader = new GLTFLoader();
    this.baseUrl = baseUrl;
    this.manifest = manifest;

    // 记录树结构（parent/children/explode），供爆炸累积与子树操作
    const walkInfo = (n, parentId) => {
      this.nodeInfo.set(n.id, {
        parentId, type: n.type, explode: n.explode || null,
        children: (n.children || []).map((c) => c.id),
      });
      (n.children || []).forEach((c) => walkInfo(c, n.id));
    };
    walkInfo(manifest.root, null);

    // 展平装配树 -> part 实例，按模板分组
    const byTemplate = new Map();
    (function walk(n) {
      if (n.type === 'part') {
        if (!byTemplate.has(n.template)) byTemplate.set(n.template, []);
        byTemplate.get(n.template).push(n);
      }
      (n.children || []).forEach(walk);
    })(manifest.root);

    for (const [tid, nodes] of byTemplate) {
      const tpl = manifest.templates.find((t) => t.id === tid);
      const gltf = await loader.loadAsync(`${baseUrl}/${tpl.gltf}`);
      const meshes = [];
      gltf.scene.traverse((o) => { if (o.isMesh) meshes.push(o); });
      const { geometry, material } = this._mergeTemplateMeshes(meshes);

      const inst = new THREE.InstancedMesh(geometry, material, nodes.length);
      inst.userData.templateId = tid;
      inst.userData.nodeIds = new Array(nodes.length);
      nodes.forEach((n, i) => {
        const m = this._matrixFromManifest(n.matrix);
        inst.setMatrixAt(i, m);
        inst.setColorAt(i, BASE_COLOR);
        inst.userData.nodeIds[i] = n.id;
        this.instances.set(n.id, { mesh: inst, index: i, base: m, temp: [0, 0, 0], visible: true });
      });
      inst.instanceMatrix.needsUpdate = true;
      if (inst.instanceColor) inst.instanceColor.needsUpdate = true;

      geometry.computeBoundingBox();
      this.templateBoxes.set(tid, geometry.boundingBox.clone());
      this.group.add(inst);
    }

    // 预计算每实例的累积爆炸向量（自身 + 全部祖先的相对向量之和）
    const accOf = (nodeId) => {
      let acc = [0, 0, 0];
      let id = nodeId;
      while (id != null) {
        const info = this.nodeInfo.get(id);
        if (info?.explode) acc = [
          acc[0] + info.explode[0], acc[1] + info.explode[1], acc[2] + info.explode[2]];
        id = info?.parentId ?? null;
      }
      return acc;
    };
    for (const id of this.instances.keys()) this.accExplode.set(id, accOf(id));

    // 整体包围盒（剖切范围 / 相机适配）：模板局部 bbox × 实例世界矩阵
    this.bbox.makeEmpty();
    for (const [, inst] of this.instances) {
      const tb = this.templateBoxes.get(inst.mesh.userData.templateId);
      if (tb) this.bbox.union(tb.clone().applyMatrix4(inst.base));
    }
    this._fitCamera();
    return this.instances.size;
  }

  /** 高亮一组零件实例（partIds 为 null 时清除）。 */
  highlight(partIds) {
    const sel = partIds || new Set();
    for (const [id, { mesh, index }] of this.instances) {
      mesh.setColorAt(index, sel.has(id) ? SELECT_COLOR : BASE_COLOR);
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    }
  }

  /** 按零件节点 id 显隐（visibleMap: Map<partId, bool>）。 */
  applyVisibility(visibleMap) {
    for (const [id, inst] of this.instances) {
      inst.visible = visibleMap.get(id) !== false;
    }
    this._updateMatrices();
  }

  /** 多层级爆炸：ratio ∈ [0,1]。 */
  applyExplosion(ratio) {
    this.explodeRatio = Math.max(0, Math.min(1, ratio));
    this._updateMatrices();
    this._syncOverlay();
  }

  /** X 光（透明鬼影）模式。 */
  setXray(on) {
    this.xray = !!on;
    for (const mesh of this.group.children) {
      const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      mats.forEach((m) => {
        m.transparent = this.xray;
        m.opacity = this.xray ? 0.35 : 1.0;
        m.depthWrite = !this.xray;
        m.needsUpdate = true;
      });
    }
  }

  /** Z 轴剖切面：on=false 关闭；pos 为世界 Z 高度。 */
  setSection(on, pos) {
    for (const mesh of this.group.children) {
      const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      mats.forEach((m) => { m.clippingPlanes = on ? [this.sectionPlane] : null; m.needsUpdate = true; });
    }
    if (on && typeof pos === 'number') {
      // normal (0,0,-1)：保留 z < pos 一侧
      this.sectionPlane.constant = pos;
    }
  }

  /** 临时拖拽移动：绑定 gizmo 到给定 part 节点集合（选择层级原则——
   *  选装配体则整组移动，选零件则单件移动）。 */
  enableMove(partIds) {
    this.moveIds = new Set(partIds || []);
    if (!this.moveIds.size) { this.disableMove(); return; }
    const center = new THREE.Vector3();
    let n = 0;
    for (const id of this.moveIds) {
      const inst = this.instances.get(id);
      if (!inst) continue;
      const tb = this.templateBoxes.get(inst.mesh.userData.templateId);
      if (tb) center.add(tb.clone().applyMatrix4(inst.base).getCenter(new THREE.Vector3()));
      n++;
    }
    if (!n) { this.disableMove(); return; }
    center.divideScalar(n);
    this.moveProxy.position.copy(center);
    this._lastProxyPos.copy(center);
    this.moveControls.enabled = true;
    this.moveControls.visible = true;
  }

  disableMove() {
    this.moveControls.enabled = false;
    this.moveControls.visible = false;
    this.moveIds.clear();
  }

  /** 复位全部临时移动（视图操作集的复位档位之一）。 */
  resetTempMoves() {
    for (const [, inst] of this.instances) inst.temp = [0, 0, 0];
    this._updateMatrices();
    this._syncOverlay();
  }

  /** 选中零件的模板 id（特征面板数据源）。 */
  templateOf(partId) {
    const inst = this.instances.get(partId);
    return inst ? inst.mesh.userData.templateId : null;
  }

  /** 特征拾取 overlay：加载模板 features glTF，高亮指定特征。
   *  featureId 为 null 时清除 overlay。返回 false 表示特征不可用。 */
  async showFeature(partId, featureId) {
    if (!partId || featureId == null) { this._clearOverlay(); return true; }
    const tid = this.templateOf(partId);
    const tpl = this.manifest.templates.find((t) => t.id === tid);
    if (!tid || !tpl?.features) { this._clearOverlay(); return false; }

    let cache = this.featureGltfCache.get(tid);
    if (!cache) {
      // 缓存加载 promise（非结果）：连点同一特征不会触发重复/被中止的请求；
      // 失败时清除缓存条目，允许下次重试（避免会话内永久失效）
      const load = (async () => {
        const loader = new GLTFLoader();
        const gltf = await loader.loadAsync(
          `${this.baseUrl}/${tpl.features.replace('.json', '.gltf')}`);
        const nodes = new Map();
        gltf.scene.traverse((o) => { if (o.isMesh && o.name) nodes.set(o.name, o); });
        return { nodes };
      })();
      load.catch(() => { if (this.featureGltfCache.get(tid) === load) this.featureGltfCache.delete(tid); });
      this.featureGltfCache.set(tid, load);
      cache = load;
    }
    cache = await cache;
    const src = cache.nodes.get(featureId);
    if (!src) { this._clearOverlay(); return false; }

    this._clearOverlay();
    const mat = new THREE.MeshStandardMaterial({
      color: new THREE.Color(0xff8a3d), emissive: new THREE.Color(0x662200),
      transparent: true, opacity: 0.9, side: THREE.DoubleSide,
    });
    const holder = new THREE.Group();
    holder.name = '__feature__';
    const cloned = src.clone();
    cloned.traverse((o) => { if (o.isMesh) o.material = mat; });
    holder.add(cloned);
    this.overlay.add(holder);
    this.featureState = { templateId: tid, instanceId: partId, featureId };
    this._syncOverlay();
    return true;
  }

  getCameraState() {
    return { pos: this.camera.position.toArray(), target: this.controls.target.toArray() };
  }

  setCameraState(state) {
    if (!state?.pos || !state?.target) return;
    this.camera.position.fromArray(state.pos);
    this.controls.target.fromArray(state.target);
    this.controls.update();
  }

  // ------------------------------------------------------------------
  // internals
  // ------------------------------------------------------------------

  _clearOverlay() {
    for (const child of [...this.overlay.children]) {
      this.overlay.remove(child);
      child.traverse?.((o) => { if (o.isMesh) { o.geometry?.dispose?.(); } });
    }
    this.featureState = null;
  }

  /** overlay 跟随实例当前世界矩阵（含爆炸与临时移动）。 */
  _syncOverlay() {
    if (!this.featureState) return;
    const inst = this.instances.get(this.featureState.instanceId);
    if (!inst) return;
    const mesh = this.overlay.getObjectByName('__feature__');
    if (mesh) {
      const m = new THREE.Matrix4();
      inst.mesh.getMatrixAt(inst.index, m);
      mesh.matrixAutoUpdate = false;
      mesh.matrix.copy(m);
      mesh.matrixWorld.copy(m);
    }
  }

  /** 统一重算实例矩阵：T(累积爆炸 × ratio + temp) × base；不可见 → 零缩放。 */
  _updateMatrices() {
    for (const [id, inst] of this.instances) {
      const acc = this.accExplode.get(id) || [0, 0, 0];
      const dx = acc[0] * this.explodeRatio + inst.temp[0];
      const dy = acc[1] * this.explodeRatio + inst.temp[1];
      const dz = acc[2] * this.explodeRatio + inst.temp[2];
      const m = new THREE.Matrix4().makeTranslation(dx, dy, dz).multiply(inst.base);
      inst.mesh.setMatrixAt(inst.index, inst.visible ? m : ZERO_SCALE);
      inst.mesh.instanceMatrix.needsUpdate = true;
    }
  }

  _clear() {
    this._clearOverlay();
    this.featureGltfCache.clear();
    for (const child of [...this.group.children]) {
      this.group.remove(child);
      child.geometry?.dispose?.();
      const mat = child.material;
      if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
      else mat?.dispose?.();
    }
    this.instances.clear();
    this.nodeInfo.clear();
    this.accExplode.clear();
    this.templateBoxes.clear();
    this.explodeRatio = 0;
    this.moveIds.clear();
  }

  /** OCP 模板的 glTF 常为一个 mesh 多个 primitive（每个面组一个）。
   *  GLTFLoader 会展开为多个 Mesh；合并为单一几何体供 InstancedMesh 使用。 */
  _mergeTemplateMeshes(meshes) {
    if (!meshes.length) throw new Error('模板 glTF 中没有网格');
    let geoms = meshes.map((m) => (m.geometry.index ? m.geometry.toNonIndexed() : m.geometry));
    const sets = geoms.map((g) => new Set(Object.keys(g.attributes)));
    const common = [...sets[0]].filter((a) => sets.every((s) => s.has(a)));
    geoms.forEach((g) => {
      Object.keys(g.attributes).forEach((k) => { if (!common.includes(k)) g.deleteAttribute(k); });
    });
    let merged = geoms.length === 1 ? geoms[0] : mergeGeometries(geoms, false);
    if (!merged) {
      geoms.forEach((g) => {
        Object.keys(g.attributes).forEach((k) => { if (k !== 'position') g.deleteAttribute(k); });
      });
      merged = mergeGeometries(geoms, false);
    }
    if (!merged) throw new Error('模板 primitive 无法合并');
    if (!merged.attributes.normal) merged.computeVertexNormals();
    return { geometry: merged, material: meshes[0].material };
  }

  /** manifest 3x4 矩阵 -> THREE.Matrix4（行主序）。 */
  _matrixFromManifest(rows) {
    const [r0, r1, r2] = rows;
    return new THREE.Matrix4().set(
      r0[0], r0[1], r0[2], r0[3],
      r1[0], r1[1], r1[2], r1[3],
      r2[0], r2[1], r2[2], r2[3],
      0, 0, 0, 1,
    );
  }

  _fitCamera() {
    if (this.bbox.isEmpty()) return;
    const center = this.bbox.getCenter(new THREE.Vector3());
    const size = this.bbox.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const dir = this.camera.position.clone().sub(this.controls.target).normalize();
    if (dir.lengthSq() < 1e-9) dir.set(1, 0.7, 1).normalize();
    this.controls.target.copy(center);
    // 窄视口（aspect < 1）时水平视野更小：按 aspect 收紧 fit 距离避免侧缘裁剪
    const dist = maxDim * 1.8 / Math.min(1, Math.sqrt(this.camera.aspect || 1));
    this.camera.position.copy(center).addScaledVector(dir, dist);
    this.camera.near = Math.max(maxDim / 1000, 0.001);
    this.camera.far = maxDim * 100;
    this.camera.updateProjectionMatrix();
    this.controls.update();
  }

  _bindPick() {
    const el = this.renderer.domElement;
    const raycaster = new THREE.Raycaster();
    let downX = 0, downY = 0;
    el.addEventListener('pointerdown', (e) => { downX = e.clientX; downY = e.clientY; });
    el.addEventListener('pointerup', (e) => {
      if (Math.hypot(e.clientX - downX, e.clientY - downY) > 5) return; // 拖拽视角，不是点击
      if (this.moveControls.dragging) return;                            // gizmo 拖拽
      const rect = el.getBoundingClientRect();
      const ndc = new THREE.Vector2(
        ((e.clientX - rect.left) / rect.width) * 2 - 1,
        -((e.clientY - rect.top) / rect.height) * 2 + 1,
      );
      raycaster.setFromCamera(ndc, this.camera);
      const hits = raycaster.intersectObjects(this.group.children, false);
      if (hits.length && this.pickHandler) {
        const h = hits[0];
        const nodeId = h.object.userData.nodeIds?.[h.instanceId];
        if (nodeId) this.pickHandler(nodeId);
      }
    });
  }

  _resize() {
    const w = this.container.clientWidth || 1;
    const h = this.container.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }
}
