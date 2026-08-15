# CAD 工程文件处理方案对比

> 对比对象
> - **方案 A（当前已搭）**：`cadquery` + `OCP`（Open CASCADE 的 Python 绑定）+ `mcp`，纯 pip 安装，已封装成 WorkBuddy MCP 工具。
> - **方案 B（用户提供）**：`FreeCAD Headless（Python API）` + `build123d` + `Three.js 实时渲染`。

---

## 先讲三个关键事实（决定结论）

1. **两个方案的几何内核完全相同——都是 Open CASCADE（OCCT）。**
   FreeCAD 内置 OCCT；cadquery / build123d 通过 `OCP`（OCCT 的 Python 绑定）调用。
   所以"几何能力上限"两者**一致**，差异只在**上层功能封装**。

2. **"无头" ≠ "免安装"。** 方案 B 说"轻量 Python 运行环境"是误导：
   FreeCAD 仍需**完整安装（GB 级）**，只是不启动 GUI；它的 Python 是 FreeCAD 自带的 `freecadcmd`。
   你**不能在任意 venv 里 `pip install freecad`**（官方没有干净的 pip 包，只有 conda 版）。
   相比之下方案 A 是**真正的纯 pip 轻量底座**（cadquery/OCP 在任意 venv 干净安装，已验证 cp313 wheel 存在）。

3. **`.prt`（Siemens NX）双方都读不了。** 方案 B 的图里只画了 `.step` 入口——作者也默认 NX 专有格式开不了。
   无论 A 还是 B，`.prt` 都必须**先在 NX 里另存为 STEP/IGES/Parasolid(`.x_t`)** 才能进流水线。这点方案 B 并没有解决。

---

## 逐项对比

| 能力 | 方案 A（cadquery/OCP + MCP） | 方案 B（FreeCAD + build123d + Three.js） |
|---|---|---|
| 安装方式 / 体积 | **纯 pip，任意 venv，轻量** | 需装完整 FreeCAD（GB 级），用其自带 Python |
| 几何内核 | Open CASCADE（OCP） | Open CASCADE（FreeCAD 内置）— **同级** |
| 读取 / 转换 step·iges·stl·brep | ✅ | ✅ |
| 参数化建模 / 几何修改（打孔·倒角·布尔·开槽·缩放） | 当前仅"读+转+提取"，**可叠加 build123d 补上（纯 pip）** | ✅ build123d |
| 2D 工程图（TechDraw 出 SVG/PDF） | ❌ | ✅ **FreeCAD 独有** |
| DFM 分析（壁厚 / 干涉 / 质心） | 质心/体积/包围盒可（OCP 自带）；壁厚·干涉需自写 | 同左，需自写（FreeCAD 只给部分质量属性） |
| Web 预览 | ✅ STL + 自包含 three.js HTML（可升级 glTF） | ✅ glTF + Three.js |
| MCP 自然语言接入 WorkBuddy | ✅ **已封装**（convert / extract / preview / batch） | ❌ 需自己再包一层 |
| `.prt`（NX 专有） | ❌（需先 NX 转 STEP） | ❌（需先 NX 转 STEP） |

---

## 结论

- **方案 B 在两点上确实更强，是真实补充**：① 参数化建模/几何修改工作流；② 2D 工程图（TechDraw 出图）——这是 FreeCAD 独有、纯 OCP 方案拿不到的能力。
- **方案 B 的"轻量无头"是误称**：它仍需 GB 级 FreeCAD；且对 `.prt` 同样无解。
- **方案 A 的差异化优势**：真正轻量、纯 pip、可任意部署，且已经封装好 MCP，WorkBuddy 里直接自然语言调用。

### 最优融合路线（推荐）
以方案 A 为**轻量底座 + MCP 接入**，按需叠加：

1. **建模能力（纯 pip，无需 FreeCAD）**：把 cadquery 升级 / 并行为 `build123d`，获得打孔·倒角·布尔·开槽·缩放等参数化建模。
2. **预览升级（纯 pip）**：从 STL 升级为 **glTF**（OCP `RWGltf`）+ Three.js，比 STL 更通用、可带材质。
3. **2D 工程图（仅当需要时才装 FreeCAD）**：用 FreeCAD 的 TechDraw 工作台出 SVG/PDF。这是唯一必须装 FreeCAD 的理由。

> 一句话：**方案 B 的"上限更高"主要体现在"2D 出图 + 现成建模封装"；我的方案赢在"轻量、纯 pip、已接 MCP"。把 build123d 叠上来，两者差距就只剩 TechDraw 一项了。**
