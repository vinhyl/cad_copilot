import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';

const BASE_COLOR = new THREE.Color(0xffffff);
const SELECT_COLOR = new THREE.Color(0xff8a3d);
const ZERO_SCALE = new THREE.Matrix4().makeScale(0, 0, 0);

/**
 * 3D 视口：按 manifest 的 Template + Matrix 模型渲染装配体（ADR-0002 D3）。
 *
 * 每个唯一零件模板只加载一份 glTF 几何，所有实例共享同一个
 * InstancedMesh（GPU 实例化）；每个实例的矩阵来自 manifest 中
 * part 节点的累积世界 4x4 矩阵（3 行 x 4 列）。
 *
 * 选择联动（copilot-vision「选择层级交互原则」的骨架）：
 *   - highlight(partIds)：视口高亮一组实例（树点击 -> 3D）
 *   - onPick(nodeId)：视口点选实例回调（3D 点击 -> 树）
 *   - applyVisibility(map)：按零件节点 id 显隐（零缩放矩阵技巧）
 */
export class AssemblyScene {
  constructor(container) {
    this.container = container;
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(window.devicePixelRatio);
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

    this.group = new THREE.Group();
    this.scene.add(this.group);

    // nodeId -> { mesh, index, base }（base = 原始世界矩阵）
    this.instances = new Map();
    this.pickHandler = null;
    this.templateBoxes = new Map(); // templateId -> Box3（用于视口适配）

    this._resize();
    new ResizeObserver(() => this._resize()).observe(container);
    this.renderer.setAnimationLoop(() => {
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    });
    this._bindPick();
  }

  onPick(cb) {
    this.pickHandler = cb;
  }

  async load(manifest, baseUrl) {
    this._clear();
    const loader = new GLTFLoader();

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
        this.instances.set(n.id, { mesh: inst, index: i, base: m });
      });
      inst.instanceMatrix.needsUpdate = true;
      if (inst.instanceColor) inst.instanceColor.needsUpdate = true;

      geometry.computeBoundingBox();
      this.templateBoxes.set(tid, geometry.boundingBox.clone());
      this.group.add(inst);
    }
    this._fitCamera();
    return this.instances.size;
  }

  /** 高亮一组零件实例（partIds 为 null 时清除全部高亮）。 */
  highlight(partIds) {
    const sel = partIds || new Set();
    for (const [id, { mesh, index }] of this.instances) {
      mesh.setColorAt(index, sel.has(id) ? SELECT_COLOR : BASE_COLOR);
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    }
  }

  /** 按零件节点 id 显隐（visibleMap: Map<partId, bool>）。 */
  applyVisibility(visibleMap) {
    for (const [id, { mesh, index, base }] of this.instances) {
      const visible = visibleMap.get(id);
      mesh.setMatrixAt(index, visible === false ? ZERO_SCALE : base);
      mesh.instanceMatrix.needsUpdate = true;
    }
  }

  // ------------------------------------------------------------------
  // internals
  // ------------------------------------------------------------------

  _clear() {
    for (const child of [...this.group.children]) {
      this.group.remove(child);
      child.geometry?.dispose?.();
      const mat = child.material;
      if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
      else mat?.dispose?.();
    }
    this.instances.clear();
    this.templateBoxes.clear();
  }

  /** OCP 模板的 glTF 常为一个 mesh 多个 primitive（每个面组一个）。
   *  GLTFLoader 会展开为多个 Mesh；合并为单一几何体供 InstancedMesh 使用。 */
  _mergeTemplateMeshes(meshes) {
    if (!meshes.length) throw new Error('模板 glTF 中没有网格');
    let geoms = meshes.map((m) => (m.geometry.index ? m.geometry.toNonIndexed() : m.geometry));
    // 只保留所有 primitive 共有的顶点属性，保证可合并
    const sets = geoms.map((g) => new Set(Object.keys(g.attributes)));
    const common = [...sets[0]].filter((a) => sets.every((s) => s.has(a)));
    geoms.forEach((g) => {
      Object.keys(g.attributes).forEach((k) => { if (!common.includes(k)) g.deleteAttribute(k); });
    });
    let merged = geoms.length === 1 ? geoms[0] : mergeGeometries(geoms, false);
    if (!merged) {
      // 属性形状仍不一致时退化为纯位置几何
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
    const box = new THREE.Box3();
    for (const { mesh, base } of this.instances.values()) {
      const tb = this.templateBoxes.get(mesh.userData.templateId);
      if (tb) box.union(tb.clone().applyMatrix4(base));
    }
    if (box.isEmpty()) return;
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const dir = this.camera.position.clone().sub(this.controls.target).normalize();
    if (dir.lengthSq() < 1e-9) dir.set(1, 0.7, 1).normalize();
    this.controls.target.copy(center);
    this.camera.position.copy(center).addScaledVector(dir, maxDim * 1.8);
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
