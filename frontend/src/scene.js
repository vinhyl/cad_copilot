import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { TransformControls } from 'three/addons/controls/TransformControls.js';

const BASE_COLOR = new THREE.Color(0xffffff);
const SELECT_COLOR = new THREE.Color(0xff8a3d);
// 干涉对高亮：两个零件用强烈对比色（洋红 / 青），一眼区分是哪两个
const INTERFERENCE_COLOR_A = new THREE.Color(0xff00c8);
const INTERFERENCE_COLOR_B = new THREE.Color(0x00c8ff);
const ZERO_SCALE = new THREE.Matrix4().makeScale(0, 0, 0);

// 显示/模型配色：每套预设 = 模型表面色(modelColor) + 背景(bg) + 平滑渐变环境 + 强度/曝光。
// 关键两点根治"一团灰"与"过曝"：
//   1) 材质改为哑光（metalness≈0.2, roughness≈0.5，见 load()）——金属只反射环境、不吃方向光，
//      正是它让渐变环境反射成均匀灰、且把白金属面在亮角度过曝。哑光后方向光真正给模型上明暗，
//      颜色以漫反射显示，灰与过曝一并消失。
//   2) modelColor 为低亮度/低饱和色（非纯白），观感上缓和白色高光的刺眼；null=保留导出原色。
// 环境仍为平滑渐变（无 RoomEnvironment 硬光斑）且随相机旋转屏幕锁定（见渲染循环）。
const THEMES = {
  // 模型配色：只改模型表面色(modelColor)；背景(bg)统一为固定深色中性、不参与切换，
  // 否则背景随模型色走会让模型与背景同色、模型不显眼。环境渐变保留与各色呼应（仅影响
  // 哑光面的微弱反射，不决定背景），让配色整体协调又不损失对比度。
  neutral: { modelColor: null,       bg: 0x1c1c20, envTop: 0xb9bcc4, envMid: 0x808890, envBot: 0x3c3e46, intensity: 0.55, exposure: 1.0  },
  slate:   { modelColor: 0x9aa0aa,   bg: 0x1c1c20, envTop: 0x9aa0aa, envMid: 0x6a7180, envBot: 0x2a2d34, intensity: 0.5,  exposure: 1.0  },
  steel:   { modelColor: 0x6f7d92,   bg: 0x1c1c20, envTop: 0x8a98ad, envMid: 0x586478, envBot: 0x202632, intensity: 0.5,  exposure: 1.0  },
  sand:    { modelColor: 0xb6a888,   bg: 0x1c1c20, envTop: 0xc2b393, envMid: 0x8a7d5e, envBot: 0x2e281d, intensity: 0.5,  exposure: 1.0  },
  olive:   { modelColor: 0x7d8676,   bg: 0x1c1c20, envTop: 0x9aa08a, envMid: 0x68705c, envBot: 0x23271c, intensity: 0.5,  exposure: 1.0  },
  ivory:   { modelColor: 0xd8d2c4,   bg: 0x1c1c20, envTop: 0xe2ddce, envMid: 0xb0a99a, envBot: 0x33322c, intensity: 0.5,  exposure: 0.95 },
};

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
    // 色调映射：NeutralToneMapping 把亮度>1.0 的高光柔和回退（而非硬削成纯白），
    // 消除金属反射/顶光在部分角度的过曝；同时尽量保留材质本色，适合 CAD 读图。
    this.renderer.toneMapping = THREE.NeutralToneMapping;
    this.renderer.toneMappingExposure = 1.0;
    this.renderer.localClippingEnabled = true;
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x1c1c20);
    this.camera = new THREE.PerspectiveCamera(50, 1, 0.01, 100000);
    this.camera.position.set(60, 40, 60);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.addEventListener('change', () => {
      if (this.cameraChangeCb) this.cameraChangeCb();
    });

    // 光照策略：OCP 导出材质多为金属（metalness≈1），金属只反射环境、不吃方向光漫反射，
    // 这导致(1)渐变环境反射成均匀灰、(2)白金属面在亮角度过曝。加载时（load()）已把材质
    // 改为哑光（metalness≈0.2, roughness≈0.5），方向光因此真正给模型上明暗、颜色以漫反射
    // 显示——灰与过曝一并消失。环境贴图仍随相机旋转屏幕锁定（见渲染循环），提供柔和顶光渐变；
    // 主光/补光为跟随相机的"屏幕顶光/侧补光"，保证任意视角明暗一致、绝不世界顶亮底暗。
    // 模型配色（modelColor/背景/强度/曝光）见 setModelColor 与工具栏"配色"按钮。
    this._envTextures = {};          // 主题环境贴图缓存（按预设名，避免每次切换重建 PMREM）
    this.currentTheme = 'neutral';
    this.currentModelColor = null;   // null = 保留导出原色；否则为预设的模型表面色
    this.setModelColor('neutral');   // 默认：哑光材质 + 平滑渐变环境，无硬光斑、无过曝
    // 主光（key）：每帧相对相机上方 → 模型中心，屏幕锁定的顶光，给模型主明暗
    this.dirLight = new THREE.DirectionalLight(0xffffff, 1.0);
    this.scene.add(this.dirLight);
    this.scene.add(this.dirLight.target);   // target 须入场景图，世界矩阵才会更新
    // 补光（fill）：每帧相对相机侧下方 → 模型中心，柔化主光背侧的死黑
    this.fillLight = new THREE.DirectionalLight(0xffffff, 0.5);
    this.scene.add(this.fillLight);
    this.scene.add(this.fillLight.target);
    // 环境光（均匀，无方向）：兜底填充，避免背光面死黑；不引入顶/底偏置
    this.ambient = new THREE.AmbientLight(0xffffff, 0.35);
    this.scene.add(this.ambient);
    // 环境贴图每帧跟随相机旋转（见渲染循环），使金属反射相对屏幕固定
    this._envEuler = new THREE.Euler();

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
    this.overlayOffsets = new Map();    // templateId -> [dx,dy,dz]（换件后新零件特征的对齐平移，仅草稿场景用）
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
      if (!e.value && this.moveEndCb) this.moveEndCb();   // 松手：草稿 move 步骤入口
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
      // 环境贴图跟随相机旋转：金属反射的"上亮下暗"始终相对屏幕固定，绝不世界顶亮底暗
      this._envEuler.setFromQuaternion(this.camera.quaternion);
      this.scene.environmentRotation.copy(this._envEuler);
      // 主光：相机上方一点 → 模型中心（相对视角的"顶光"，保留正常明暗层次）
      this.dirLight.position.copy(this.camera.position)
        .add(this.camera.up.clone().multiplyScalar(40));
      this.dirLight.target.position.copy(this.controls.target);
      this.dirLight.target.updateMatrixWorld();
      // 补光：相机侧下方一点 → 模型中心，柔化主光背侧的死黑，仍完全跟视角
      this.fillLight.position.copy(this.camera.position)
        .add(this.camera.up.clone().multiplyScalar(-15))
        .add(this._cameraRight().multiplyScalar(20));
      this.fillLight.target.position.copy(this.controls.target);
      this.fillLight.target.updateMatrixWorld();
      this.renderer.render(this.scene, this.camera);
    });
    this._bindPick();
  }

  onPick(cb) { this.pickHandler = cb; }

  /** 应用模型配色：设置模型表面色(modelColor) + 背景 + 平滑渐变环境 + 强度/曝光。
   * modelColor 为 null 时保留各模板导出原色；否则统一染成该低亮度色（缓和过曝刺眼）。
   * 可在加载前后任意时刻调用；预设名记入 currentTheme / currentModelColor。 */
  setModelColor(name) {
    const t = THEMES[name] || THEMES.neutral;
    this.currentTheme = name;
    this.currentModelColor = t.modelColor ?? null;
    this.scene.background = new THREE.Color(t.bg);
    if (!this._envTextures[name]) this._envTextures[name] = this._buildEnvTexture(t);
    this.scene.environment = this._envTextures[name];
    this.scene.environmentIntensity = t.intensity;
    this.renderer.toneMappingExposure = t.exposure;
    this._applyModelColor();
  }

  /** 把当前模型配色染到场景内材质：null=还原导出原色（首次染前会存一份原色备份）。
   * 哑光材质下 material.color 即为所见表面色；与 instanceColor(选中高亮)相乘生效。 */
  _applyModelColor() {
    if (!this.group) return;   // 构造早期 group 尚未创建时（如默认 setModelColor）跳过
    const color = this.currentModelColor;
    for (const child of this.group.children) {
      const mats = Array.isArray(child.material) ? child.material : [child.material];
      mats.forEach((m) => {
        if (!m || !m.color) return;
        if (color == null) {
          if (m.userData._origColor) m.color.copy(m.userData._origColor);
        } else {
          if (!m.userData._origColor) m.userData._origColor = m.color.clone();
          m.color.set(color);
        }
        m.needsUpdate = true;
      });
    }
  }

  /** 由主题生成平滑渐变环境贴图（垂直：顶亮 → 中 → 底暗）。
   * 关键：无 RoomEnvironment 那种硬光面板，金属反射是平滑渐变，任何角度都不会
   * 出现单一过曝亮斑；配合渲染循环的 environmentRotation 随相机旋转即屏幕锁定。 */
  _buildEnvTexture(theme) {
    const c = document.createElement('canvas');
    c.width = 16; c.height = 256;
    const ctx = c.getContext('2d');
    const g = ctx.createLinearGradient(0, 0, 0, 256);
    g.addColorStop(0.0, '#' + new THREE.Color(theme.envTop).getHexString());
    g.addColorStop(0.5, '#' + new THREE.Color(theme.envMid).getHexString());
    g.addColorStop(1.0, '#' + new THREE.Color(theme.envBot).getHexString());
    ctx.fillStyle = g; ctx.fillRect(0, 0, 16, 256);
    const tex = new THREE.CanvasTexture(c);
    tex.mapping = THREE.EquirectangularReflectionMapping;
    tex.colorSpace = THREE.SRGBColorSpace;
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    const env = pmrem.fromEquirectangular(tex).texture;
    tex.dispose(); pmrem.dispose();
    return env;
  }

  async load(manifest, baseUrl) {
    // 重载同一装配（preview 刷新 / 放弃草稿回基线）时保留用户相机，
    // 只有首次加载才自动取景——否则每次刷新都把视角拽回全景
    const prevCam = this.instances.size > 0 ? this.getCameraState() : null;
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
      // 版本视图的 gltf 是绝对路径（/versions/...），基线是相对 cache 的路径
      const url = tpl.gltf.startsWith('/')
        ? tpl.gltf : `${baseUrl}/${tpl.gltf}`;
      const gltf = await loader.loadAsync(url);
      const meshes = [];
      gltf.scene.traverse((o) => { if (o.isMesh) meshes.push(o); });
      const { geometry, material } = this._mergeTemplateMeshes(meshes);
      // 哑光化：金属材质会反射环境成灰、且白面过曝；降到 metalness≈0.2 让方向光真正
      // 给模型上明暗、颜色以漫反射显示。roughness 适度，保留一点高光质感而非全哑。
      material.metalness = 0.2;
      material.roughness = 0.5;

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
    this._applyModelColor();   // 应用当前模型配色到刚构建的材质（新建/重载都生效）

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
    if (prevCam) this.setCameraState(prevCam);
    else this._fitCamera();
    return this.instances.size;
  }

  /** 高亮一组零件实例（partIds 为 null 时清除）。color 可选，缺省用默认选中色。 */
  highlight(partIds, color) {
    const sel = partIds || new Set();
    const c = color || SELECT_COLOR;
    for (const [id, { mesh, index }] of this.instances) {
      mesh.setColorAt(index, sel.has(id) ? c : BASE_COLOR);
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    }
  }

  /** 干涉对高亮：零件 A 洋红、零件 B 青，用强对比色让两个零件更直观。 */
  highlightPair(idA, idB) {
    for (const [id, { mesh, index }] of this.instances) {
      let c = BASE_COLOR;
      if (id === idA) c = INTERFERENCE_COLOR_A;
      else if (id === idB) c = INTERFERENCE_COLOR_B;
      mesh.setColorAt(index, c);
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

  /** 剖切面（M6.5 三轴扩展）：on=false 关闭。
   * axis ∈ 'X'|'Y'|'Z'（默认 Z，向后兼容）；pos 为该轴世界坐标；
   * flip=true 保留另一侧。默认保留轴向低值一侧。 */
  setSection(on, pos, axis = 'Z', flip = false) {
    for (const mesh of this.group.children) {
      const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      mats.forEach((m) => { m.clippingPlanes = on ? [this.sectionPlane] : null; m.needsUpdate = true; });
    }
    if (on && typeof pos === 'number') {
      const v = { X: [1, 0, 0], Y: [0, 1, 0], Z: [0, 0, 1] }[axis] || [0, 0, 1];
      const s = flip ? -1 : 1;
      this.sectionPlane.normal.set(s * v[0], s * v[1], s * v[2]);
      this.sectionPlane.constant = -s * pos;
    }
  }

  /** 相机变更回调（OrbitControls change）：双视口联动的传播通道。 */
  onCameraChange(cb) { this.cameraChangeCb = cb; }

  /** 把相机取景到给定零件集合的包围盒（范围切换自动取景）。
   * ids 为 null 时对整体 bbox 取景（等价首次加载）。
   * pad=>1 让相机离得更远（如搜索定位时想留点余量）。 */
  fitToIds(ids, pad = 1) {
    const box = new THREE.Box3();
    if (!ids) {
      if (!this.bbox.isEmpty()) box.copy(this.bbox);
    } else {
      for (const id of ids) {
        const inst = this.instances.get(id);
        if (!inst || !inst.visible) continue;
        const tb = this.templateBoxes.get(inst.mesh.userData.templateId);
        if (!tb) continue;
        // 与 _updateMatrices 同公式：base × (爆炸 + temp 平移)
        const acc = this.accExplode.get(id) || [0, 0, 0];
        const m = new THREE.Matrix4().makeTranslation(
          acc[0] * this.explodeRatio + inst.temp[0],
          acc[1] * this.explodeRatio + inst.temp[1],
          acc[2] * this.explodeRatio + inst.temp[2],
        ).multiply(inst.base);
        box.union(tb.clone().applyMatrix4(m));
      }
    }
    if (box.isEmpty()) return;
    this._fitCameraTo(box, pad);
  }

  /** 移动结束回调（TransformControls 拖拽松开）：草稿 move 步骤的入口。 */
  onMoveEnd(cb) { this.moveEndCb = cb; }

  /** 当前全部临时位移 {nodeId: [dx,dy,dz]}（temp 非零者）。 */
  getTempMoves() {
    const out = {};
    for (const [id, inst] of this.instances) {
      const [x, y, z] = inst.temp;
      if (x || y || z) out[id] = [x, y, z];
    }
    return out;
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

  /** 清除某模板的特征 glTF 缓存（换件后 overlay 几何/列表已变，强制重载）。 */
  clearFeatureGltf(tid) { this.featureGltfCache.delete(tid); }

  /** 设置某模板特征 overlay 的额外平移（换件对齐偏移；仅草稿场景需要）。 */
  setOverlayOffset(tid, d) {
    if (d) this.overlayOffsets.set(tid, d);
    else this.overlayOffsets.delete(tid);
    this._syncOverlay();
  }

  /** 特征 glTF 模板级缓存（promise；失败清除条目允许重试）。 */
  _featureGltf(tid) {
    let load = this.featureGltfCache.get(tid);
    if (load) return load;
    const tpl = this.manifest.templates.find((t) => t.id === tid);
    if (!tpl?.features) return null;
    load = (async () => {
      const loader = new GLTFLoader();
      const featRel = tpl.features;   // 可能是相对路径(基线)，或被换件重指向的绝对路径
      const urlOf = (rel) => (rel && rel.startsWith('/')) ? rel : `${this.baseUrl}/${rel}`;
      const [gltf, feats] = await Promise.all([
        loader.loadAsync(urlOf(featRel.replace('.json', '.gltf'))),
        fetch(urlOf(featRel))
          .then((r) => (r.ok ? r.json() : []))
          .catch(() => []),
      ]);
      // 固化矩阵：节点不挂渲染场景，pickFeatureAt 直接 raycast 需要 matrixWorld
      gltf.scene.updateMatrixWorld(true);
      // GLTFLoader 命名节点时经 PropertyBinding.sanitizeNodeName 处理
      //（删除 . : / [ ]，空格→下划线，如 "#1.1" → "#11"），与特征 JSON
      // 的 id 不再一致——同步取特征 JSON 建立 sanitize 名 → 原始 id 的
      // 映射，nodes 直接以原始特征 id 为 key（高亮/拾取两侧都还原）。
      // 另注意多 primitive 特征会被展开成 Group(原名) + Mesh(原名_N) 子
      // 节点——按顶层节点收集，拾取沿父链回溯到顶层。
      const sanitize = (s) => s.replace(/\s/g, '_').replace(/[[\].:/]/g, '');
      const origBySan = new Map();   // sanitize 名 -> 原始特征 id
      for (const f of Array.isArray(feats) ? feats : []) {
        origBySan.set(sanitize(f.id), f.id);
      }
      const nodes = new Map();       // 原始特征 id -> Object3D
      for (const child of gltf.scene.children) {
        const orig = origBySan.get(child.name);
        if (orig) nodes.set(orig, child);
      }
      return { nodes, origBySan };
    })();
    load.catch(() => { if (this.featureGltfCache.get(tid) === load) this.featureGltfCache.delete(tid); });
    this.featureGltfCache.set(tid, load);
    return load;
  }

  /** 预载模板特征 glTF（特征粒度激活时调用，为 3D 特征拾取联动做准备）。 */
  preloadFeaturesByTemplate(tid) { this._featureGltf(tid); }

  /** 特征级拾取：在指定零件上拾取点击处的特征 id（特征粒度编辑的
   *  3D → 列表联动）。返回特征 id 或 null（未命中 / glTF 不可用）。 */
  async pickFeatureAt(ndc, partId) {
    if (!ndc || !partId) return null;
    const tid = this.templateOf(partId);
    const load = tid ? this._featureGltf(tid) : null;
    if (!load) return null;
    const { nodes, origBySan } = await load;
    const inst = this.instances.get(partId);
    if (!inst || !nodes.size) return null;
    // 特征 glTF 是模板局部坐标：把世界射线逆变换到局部空间再求交。
    // 换件模板的 overlay 还叠加了对齐偏移（_syncOverlay 里 multiply），这里必须一致，
    // 否则射线与渲染出来的 overlay 错位 → 特征难选中/选不到。
    const m = new THREE.Matrix4();
    inst.mesh.getMatrixAt(inst.index, m);
    const off = this.overlayOffsets.get(tid);
    if (off) m.multiply(new THREE.Matrix4().makeTranslation(off[0], off[1], off[2]));
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(ndc, this.camera);
    raycaster.ray.applyMatrix4(m.clone().invert());
    // 递归检测：多 primitive 特征节点是 Group，非递归永远打不中
    const hits = raycaster.intersectObjects([...nodes.values()], true);
    if (hits.length) {
      // 沿父链回溯到特征顶层节点，再经 sanitize 名映射回原始特征 id
      let o = hits[0].object;
      while (o && !origBySan.has(o.name)) o = o.parent;
      return o ? origBySan.get(o.name) : null;
    }
    // 兜底：没直接命中（点击点落在棱/缝）→ 返回离（变换到局部空间的）射线最近的特征。
    // 保证"点零件 = 选中一个确定特征"（严格特征优先，不产生"零件级"独立态）。
    let best = null, bestD = Infinity;
    const wp = new THREE.Vector3();
    for (const [oid, nd] of nodes) {
      nd.getWorldPosition(wp);
      const d = raycaster.ray.distanceToPoint(wp);
      if (d < bestD) { bestD = d; best = oid; }
    }
    return best;
  }

  /** 特征拾取 overlay：加载模板 features glTF，高亮指定特征。
   *  featureId 为 null 时清除 overlay。返回 false 表示特征不可用。 */
  async showFeature(partId, featureId) {
    if (!partId || featureId == null) { this._clearOverlay(); return true; }
    const tid = this.templateOf(partId);
    if (!tid || !this.manifest.templates.find((t) => t.id === tid)?.features) {
      this._clearOverlay(); return false;
    }
    const cache = await this._featureGltf(tid);
    const src = cache?.nodes.get(featureId);
    if (!src) { this._clearOverlay(); return false; }

    this._clearOverlay();
    // overlay 与零件表面共面（后端同一套三角化导出）：深度完全相等，
    // 靠 polygonOffset 在深度测试中稳定胜出，避免 Z-fighting 条纹。
    const mat = new THREE.MeshStandardMaterial({
      color: new THREE.Color(0xff8a3d), emissive: new THREE.Color(0x662200),
      transparent: true, opacity: 0.9, side: THREE.DoubleSide,
      depthTest: true,
      polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -2,
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

  /** 相机右向（世界坐标），用于补光相对相机的偏移。 */
  _cameraRight() {
    const e = this.camera.matrixWorld.elements;
    return new THREE.Vector3(e[0], e[1], e[2]).normalize();
  }

  _clearOverlay() {
    for (const child of [...this.overlay.children]) {
      this.overlay.remove(child);
      child.traverse?.((o) => { if (o.isMesh) { o.geometry?.dispose?.(); } });
    }
    this.featureState = null;
  }

  /** overlay 跟随实例当前世界矩阵（含爆炸与临时移动）。
 *  换件模板额外叠加对齐平移（overlay 为来源零件局部坐标，需平移贴到新零件上）。 */
  _syncOverlay() {
    if (!this.featureState) return;
    const inst = this.instances.get(this.featureState.instanceId);
    if (!inst) return;
    const mesh = this.overlay.getObjectByName('__feature__');
    if (mesh) {
      const m = new THREE.Matrix4();
      inst.mesh.getMatrixAt(inst.index, m);
      const off = this.overlayOffsets.get(this.featureState.templateId);
      if (off) {
        m.multiply(new THREE.Matrix4().makeTranslation(off[0], off[1], off[2]));
      }
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
    this._fitCameraTo(this.bbox);
  }

  /** 取景到显式包围盒（fitToIds / _fitCamera 共用）。 */
  _fitCameraTo(box, pad = 1) {
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const dir = this.camera.position.clone().sub(this.controls.target).normalize();
    if (dir.lengthSq() < 1e-9) dir.set(1, 0.7, 1).normalize();
    this.controls.target.copy(center);
    // 窄视口（aspect < 1）时水平视野更小：按 aspect 收紧 fit 距离避免侧缘裁剪
    let dist = maxDim * 1.8 * pad / Math.min(1, Math.sqrt(this.camera.aspect || 1));
    // 防止小零件被"适配屏幕"过度放大：相机不放到比整装配对角线的一定比例更近，
    // 保留周围上下文，避免半个螺母充满全屏。
    if (!this.bbox.isEmpty()) {
      const diag = this.bbox.getSize(new THREE.Vector3()).length() || maxDim;
      dist = Math.max(dist, diag * 0.12);
    } else {
      // 整装配 bbox 暂不可用时（如部分几何未载入），按零件自身尺寸兜底，
      // 仍保证相机不贴到能把小件吹满屏的距离。
      dist = Math.max(dist, maxDim * 12);
    }
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
        // ndc 一并传出：特征级拾取（pickFeatureAt）需要点击位置
        if (nodeId) this.pickHandler(nodeId, ndc.clone());
      } else if (this.pickHandler) {
        // 点空白（未命中任何零件）：传出 null，让页面清除当前选中
        this.pickHandler(null, ndc.clone());
      }
    });
  }

  _resize() {
    const w = this.container.clientWidth || 1;
    const h = this.container.clientHeight || 1;
    // setSize 默认把 CSS 尺寸写进 style：dpr>1（视网膜屏）时 canvas
    // 画布为 w*dpr 物理像素、显示尺寸仍为 w。此前传 false 不写 style，
    // canvas 会以 w*dpr 的原始尺寸撑破容器（首页 2x 溢出；编辑页
    // 网格最小内容尺寸反馈放大直至浏览器上限，视口全黑只剩提示）。
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }
}
