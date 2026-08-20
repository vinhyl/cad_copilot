# CAD Copilot (cad_copilot)

> 版本：**v0.1.0** — 变更记录见 [CHANGELOG.md](CHANGELOG.md)

离线 CAD 特征拾取 / 预览工具链。把 STEP/IGES 模型每个特征切成独立 STL 网格，
生成**可点击拾取**的 3D HTML 预览（three.js 已本地化 vendored，离线可用），并提供
MCP server 供其他 agent 调用。

## 环境要求
- Python 3.13（3.12+ 一般也可，但 OCP 轮子按 3.13 验证；`cadquery-ocp-novtk` / `build123d` 已确认有 cp313 wheel 可用）
- Windows / macOS / Linux
- 首次部署需联网从 PyPI 拉取 Python 依赖（`bootstrap.py` 处理）；three.js 已随仓库 vendored，预览本身无需联网

## 一键部署
```bash
python bootstrap.py
```
脚本会创建 `venv/` 并安装 `requirements.txt` 里的依赖
（cadquery-ocp-novtk / build123d / fastmcp / numpy）。

Windows 与 macOS 上命令**完全相同**；仅首次运行需联网。

`bootstrap.py` 只负责环境（venv + 依赖）。**部署阶段还需由 agent 把 `cad-engine`
接入所用客户端**（见下文「MCP Server 使用」的连接片段）；登记后需在客户端点
**Trust** 启用，agent 才能调用那 10 个 CAD 工具（`build123d_model` 默认禁用）。

### 部署阶段：ODA 插件（DWG 支持，默认启用）

ODA File Converter 是**默认启用**的插件——DWG 图纸经它转换后导入（DXF 无需它）。
部署阶段，agent 应先探测 ODA 是否已就位：

```bash
venv/bin/python -c "import cad_drawing; print(cad_drawing.probe_oda_converter())"
```

返回路径即已安装；`None` 表示缺失。

若缺失，agent **告知用户**：使用 DWG 图纸需安装 ODA File Converter（Open Design Alliance
提供的免费工具，官网**免注册直链下载**），下载页：
https://www.opendesign.com/guestfiles/oda_file_converter 。用户同意后，agent **全自动完成
下载与安装**（无需用户手动操作）：

- **macOS**：从下载页取对应架构（Apple 芯片 arm64 / Intel x64）的 `.dmg` 直链，下载后挂载
  并拷贝 `ODAFileConverter*.app` 到 `/Applications`（或取 `.pkg` 直链安装）；
- **Linux**：取 RPM / DEB / AppImage 直链，按发行版安装；
- **Windows**：取 x64 安装包直链，运行安装程序。

安装后重新运行上面的探测命令确认返回路径（前端插件面板 ODA 转绿点）。

**面向非技术用户（小白引导）**：用大白话说明，不要丢命令给用户。例如：
"DWG 是 AutoCAD 的图纸格式，需要装一个免费小工具才能打开；你不用自己下载，
点头我就帮你装好，装完就能看 DWG 了。"用户只需回"装"或"先不装"，下载/安装
全程由 agent 执行，不在对话里暴露终端操作。

## 快速开始：生成拾取预览
```bash
venv/Scripts/python feature_picker.py your_model.step --out-dir previews   # Windows
venv/bin/python        feature_picker.py your_model.step --out-dir previews   # macOS / Linux
```
生成的 `previews/your_model_拾取.html` 与同目录 `vendor/` 一起打开，**断网也能用**。
更完整的命令行参数见下文「命令行速查」。

## MCP Server 使用

`cad_mcp_server.py` 是一个基于 FastMCP 的 stdio MCP server，把上面的几何能力暴露给
WorkBuddy 等 agent 调用。它当前提供 **11 个工具**（详见下表），其中 `build123d_model`
出于安全考虑**默认禁用**。

### 启动（stdio）
```bash
venv/Scripts/python cad_mcp_server.py    # Windows
venv/bin/python        cad_mcp_server.py    # macOS / Linux
```
默认从标准输入/输出以 JSON-RPC 通信，由 MCP 客户端（如 WorkBuddy）拉起，无需手动常驻。

### 接入（由部署用的 agent 完成）

`cad_mcp_server.py` 是标准 MCP stdio server，任何支持 MCP 的客户端都能用，
**server 代码零改动**。需要接入时，部署用的 agent 把下面这段 `cad-engine` 条目写进
**它自己**的客户端配置即可（各客户端路径/格式不同，位置见下）。推荐用绝对路径，
并把可访问目录约束在工程根目录：

```json
{
  "mcpServers": {
    "cad-engine": {
      "command": "/abs/path/to/cad_copilot/venv/bin/python",
      "args": ["/abs/path/to/cad_copilot/cad_mcp_server.py"],
      "env": { "CAD_MCP_ALLOWED_DIRS": "/abs/path/to/cad_copilot" },
      "disabled": false
    }
  }
}
```

> Windows 把 `command` 改成 `venv\\Scripts\\python.exe`。各客户端配置位置示例：
> WorkBuddy `~/.workbuddy/mcp.json`、Claude Desktop
> `~/Library/Application Support/Claude/claude_desktop_config.json`、Cursor
> `~/.cursor/mcp.json`、VS Code `.vscode/mcp.json`（键名/结构不同，需按该客户端格式适配）。
> 写入后仍需在客户端点 **Trust** 启用。

### 11 个工具一览
| 工具 | 说明 | 默认状态 |
|------|------|---------|
| `convert_file` | STEP/IGES/STL/BREP 格式互转 | ✅ 启用 |
| `extract_properties` | 体积 / 面积 / 包围盒 / 质心 / 拓扑 / 是否装配体 | ✅ 启用 |
| `batch_convert` | 批量转换目录下所有支持文件，并写出 JSON 报告 | ✅ 启用 |
| `create_primitive` | 生成基本体：box / cylinder | ✅ 启用 |
| `edit_geometry` | 几何编辑：fillet / chamfer / scale / drill | ✅ 启用 |
| `boolean_parts` | 布尔运算：fuse / cut / common | ✅ 启用 |
| `pick_features` | 生成可点击的 3D 特征拾取预览（离线 HTML + vendor） | ✅ 启用 |
| `parse_assembly` | 装配体 STEP → 装配树 + 4×4 矩阵 + 去重零件模板（glTF 缓存，Web 前端 Template+Matrix 数据源） | ✅ 启用 |
| `check_interference` | 装配体全实例对布尔干涉审计（确定性守门，D8） | ✅ 启用 |
| `audit_assembly` | 一键体检：干涉 + DFM 规则（小孔/深孔/薄壁，模块七） | ✅ 启用 |
| `build123d_model` | 运行 build123d 建模脚本（**执行任意代码 = 本地代码执行**） | ⛔ **默认禁用**（设 `CAD_MCP_ALLOW_BUILD123D=1` 才启用） |

> 安全提示：`build123d_model` 会以 MCP server 进程的完整权限执行传入的任意 Python
> 脚本，等于本地代码执行。它被默认禁用，且每次调用都强制走超时隔离子进程。
> **切勿**在不可信、共享或多租户环境中启用。其余 10 个工具的路径都被限制在
> `CAD_MCP_ALLOWED_DIRS` 白名单内（默认 `.`，即 server 工作目录）。

> 数据隐私备注：Agent 调用本 server 时，工具返回值（特征元数据 / 物性 / 文件清单 /
> 修改参数）与整个会话历史会进入所接 LLM 的上下文，且**跨调用累积**（一次会话可拼出
> 零件的完整语义画像）。团队现阶段**批准使用云端 LLM**，覆盖日常 Agent 使用与后续
> 评测飞轮。复评触发点：Phase C 自然语言修改上线前 / 对外分发前 / 更换模型供应商。
> 完整分析见 [ADR-0002 风险登记 R10](docs/decisions/0002-web-copilot-expansion.md)。

## 本地 Web 服务（Phase A 骨架）

`cad_service.py` 是面向 Web 前端的本地服务层（starlette，随 fastmcp 附带，
**零新增依赖**），与 MCP server 共享同一套工具库（ADR-0002 D2 双 transport）。

```bash
venv/Scripts/python cad_service.py     # http://127.0.0.1:8764（token 启动时打印）
```

| 端点 | 说明 |
|---|---|
| `GET /health` | 健康检查（免鉴权，仅本机） |
| `GET /api/config` | 启动配置（allowed_dirs，前端路径引导用） |
| `POST /api/upload?name=` | **显式授权输入通道**：原始请求体 → 内容寻址落盘 `uploads/<sha256>/`（同内容去重回路径） |
| `POST /api/assembly/parse` | 装配 STEP → manifest + 写缓存（Bearer token） |
| `POST /api/assembly/edit` | **写操作**：模板编辑 → 干涉守门 → 原子版本提交（409 = 干涉拒绝） |
| `GET /api/assembly/audit?cache_key=` | 一键体检：干涉 + DFM 规则（确定性计算） |
| `POST /api/drawing/import` | 图纸导入：DXF 直读 / DWG 经 ODA 转换 + 语义提取 |
| `GET /api/versions?cache_key=` | 版本列表（v0 基线 + v1..vN） |
| `POST /api/versions/checkout` | 版本切换/回滚（指针切换，文件永不重写） |
| `GET /api/plugins` | 插件探测（ODA / FreeCAD+ccx / Blender，可用性 + 缺失提示） |
| `POST /api/fea/static` | FEA 静力学（`async:true` 走 job 模式，202 + job_id） |
| `POST /api/render` | Blender 离线渲染（`async:true` 走 job 模式，202 + job_id） |
| `GET /api/jobs[?kind=]` | R5 任务列表 |
| `GET /api/jobs/{id}` | 任务状态轮询（phase / percent / detail） |
| `POST /api/jobs/{id}/cancel` | 任务取消（协作式：terminate 钩子 + 幂等） |
| `GET /cache/{key}/...` | 静态服务缓存产物（tree_structure.json / gltf_library，防目录穿越） |
| `GET /versions/{key}/...` | 静态服务版本几何（防目录穿越） |
| `GET /drawings/{key}/...` | 静态服务图纸 SVG（防目录穿越） |
| `GET /app/` | Web 前端 SPA（`frontend/dist` 构建产物，见下节） |
| `WS /ws?token=...` | JSON 协议骨架（ping / parse）；长任务进度已由 R5 HTTP job 协议承接（上表 /api/jobs），WS 留作后续推送通道 |

要点：缓存目录按源文件 **SHA-256 前 16 位** 命名——同内容重复导入直接命中
（`cache_hit: true`），内容变化自动换键（R8/R17）；几何操作全局串行锁（R4）；
仅绑 127.0.0.1、token 鉴权、无 CORS（延续约束一）。
配置：`CAD_SERVICE_TOKEN` / `CAD_SERVICE_ALLOWED_DIRS` / `CAD_SERVICE_WORKSPACE` /
`CAD_SERVICE_HOST` / `CAD_SERVICE_PORT` / `CAD_SERVICE_FRONTEND_DIR`。

## Web 前端（Phase A/B）

`frontend/` 为 Vite + three.js（版本与 `vendor/` 一致：three@0.160.0）+ 原生 JS 的
SPA：加载装配 STEP → **Template+Matrix 实例化渲染**（每个唯一零件一份 glTF
几何，全部实例共享 `InstancedMesh`，矩阵来自 manifest 的累积世界 4×4）+ 装配树 UI
（选择层级双向联动：树上点选高亮 3D 实例 / 视口点选定位树上节点；按节点显隐）。

**输入交互**（路径手输已移除，三条显式通道 + agent 入口）：

- **「打开文件…」/ 拖放**：任意位置文件经 `POST /api/upload` 显式授权进入——
  内容寻址落盘 `uploads/<sha256>/<文件名>`（重复上传去重回路径），前端按扩展名
  自动路由（STEP → 装配加载；DXF/DWG → 图纸对照导入）
- **最近使用**：localStorage 记录最近 8 个成功加载文件，点击重载（内容寻址缓存
  命中秒开），与 uploads 路径去重联动
- **agent 预览入口**：`/app?token=...&load=<encodeURIComponent(路径)>` 一次性
  URL 参数——agent（内置浏览器 navigate 或生成链接）驱动加载，路径仍受服务端
  `safe_input_path` 权威校验
- 白名单外的路径（绕过前置校验时）：403 + 可操作文案（移入目录 / 设
  `CAD_SERVICE_ALLOWED_DIRS`），`GET /api/config` 供前端展示可访问目录

**插件状态面板（D5/D7 探测可视化）**：侧栏底部常驻 ODA / FEA / Blender 探测
结果（绿点=可用、灰点=未安装，悬停显示路径或安装指引），↻ 即时重探测；
缺依赖时对应功能给出明确降级提示，其余功能不受影响。

**R5 任务卡片**：FEA / 渲染按钮触发异步 job（202 + job_id），右下角浮卡显示
阶段中文映射 + 进度条（FEA 中继内层 FreeCAD 七阶段真实进度；渲染为不确定
进度），支持取消（协作式终止）；FEA 完成出结果摘要，渲染完成弹大图。

**Phase B 视图操作集**（视图状态 ≠ 数据状态，全部不落盘）：

| 操作 | 入口 | 说明 |
|---|---|---|
| 多层级爆炸 | 工具栏滑条 | 后端算好的相对 explode 向量沿祖先链累积 × 比例 |
| 特征拾取 | 选中零件 → 侧栏特征面板 | 点特征条目 → 3D 橙色高亮 overlay（cache `features/*.json|gltf`） |
| 隔离 / X 光 / 剖切面 | 工具栏 | 只显选中子树 / 半透明鬼影 / Z 轴 clipping plane |
| 临时拖拽移动 | 选中 →「移动」 | TransformControls gizmo，选装配体整组移动；「复位移动」还原 |
| 相机书签 | 「存视角 / 回视角」 | localStorage，不进版本树 |
| 复位视图 | 一键 | 爆炸/显隐/临时移动/相机全部还原 |

**Phase C 编辑闭环**（选中零件 → 侧栏编辑面板）：

- 整模板操作：钻孔（位置/半径/深度）、倒角、圆角、缩放
- **定点特征编辑（R1）**：特征面板的孔特征带「扩径」按钮——只改选中的那个孔
  （从特征元数据构造定向布尔）；编辑后特征缓存经**指纹匹配**保持 id 稳定
  （跨版本认出"同一个孔"，changelog 语义化如 `孔 #1.2: R1.0 -> R1.6`）
- 流程：**编辑 → 干涉守门（BRepAlgoAPI_Common，逐实例布尔 + bbox 预筛）→ 原子版本提交**
  （R6 临时目录 + rename）；干涉时 409 结构化拒绝（涉及零件对 + 穿透体积 mm³），
  几何保持当前版本不变（R15）
- 版本面板：v0 基线 + v1..vN 链式增量（每版本只存被改模板的 step/gltf），
  切换/回滚 = 指针移动，历史版本文件永不重写（D10）

**一键体检（模块七）**：工具栏「体检」→ 干涉全实例对审计 + DFM 规则
（小孔 R<0.5 / 深孔 L/D>10 / 平行孔薄壁提示），全部确定性计算（D8）。

**图纸对照（D5）**：工具栏「图纸」→ DXF 直读 / DWG 经 ODA（探测降级，缺失时
明确提示装 ODA 或转存 DXF）；**语义提取**（螺纹 M10x1.5 / 直径 Ø8 / 公差 H7/g6，
模块六语义真理）+ 轻依赖 SVG 渲染（LINE/CIRCLE/ARC/LWPOLYLINE/TEXT，零 PIL 依赖）。

- **使用（零 Node 依赖）**：`venv/Scripts/python cad_service.py` 后浏览器打开
  `http://127.0.0.1:8764/app/`，token 支持 `?token=...` 一次性注入。构建产物
  `frontend/dist/` **随仓库提交**，与 `vendor/` 同哲学——分发时带走仓库即可离线运行。
- **开发（需 Node 18+）**：`cd frontend && npm install`，之后 `npm run dev`
  （5173 端口，API/WS 代理到 8764，改源码热更新）；发布前 `npm run build` 并提交
  新的 `dist/`。
- token 经 localStorage 保存；`/cache/` 静态文件不设 token（GLTFLoader 无法附带
  请求头）——仅本机监听 + 无 CORS 下可接受（ADR-0002 延续约束一的本地化取舍）。

### 验证连接（mcp_test.py）
开发期可用 `mcp_test.py` 端到端验证 server（它会拉起 server 并真跑几个工具）：
```bash
venv/Scripts/python mcp_test.py    # Windows
```
> 注意：`mcp_test.py` 是开发用冒烟脚本，已被 `.gitignore` 忽略，且当前**硬编码了本机绝对路径**
>（已知局限，见 CHANGELOG / 审计 L6），并非可移植的生产测试。正式的回归测试请用 pytest（见「开发与测试」）。

## 命令行速查

三个脚本都通过仓库内的 `venv/` 运行。Windows 用 `venv/Scripts/python`，macOS/Linux 用 `venv/bin/python`。

### `feature_picker.py` — 生成可点击 3D 拾取预览
```bash
python feature_picker.py <input> [--out-dir DIR]
```
- `input`（必填，位置参数）：源文件，支持 `.step/.stp/.igs/.iges/.stl/.brep`
- `--out-dir DIR`（可选，默认 `previews`）：HTML 输出目录
- 产出：`<基名>_拾取.html`（与同目录 `vendor/` 一起可离线打开）

示例：
```bash
python feature_picker.py selftest.step --out-dir previews
# -> previews/selftest_拾取.html
```

### `feature_locator.py` — 生成 2D 特征编号定位图
```bash
python feature_locator.py <input> [--axis auto] [--out-dir DIR]
```
- `input`（必填，位置参数）：源文件，支持 `.step/.stp/.igs/.iges/.stl/.brep`
- `--axis {auto|x|y|z}`（可选，默认 `auto`）：2D 定位图的投影轴；`auto` 按模型主方向自动选
- `--out-dir DIR`（可选，默认 `previews`）：HTML 输出目录
- 产出：`<基名>_定位图.html`

示例：
```bash
python feature_locator.py selftest.step --axis auto --out-dir previews
# -> previews/selftest_定位图.html
```

### `make_preview.py` — 生成实体整体预览
```bash
python make_preview.py <input>
```
- `input`（必填，位置参数，仅通过 `sys.argv[1]` 传入，无 argparse 子选项）：源文件
- 产出：`previews/<基名>_preview.html` 与 `previews/<基名>.stl`

示例：
```bash
python make_preview.py selftest.step
# -> previews/selftest_preview.html + previews/selftest.stl
```

## 输出产物与目录约定

- `feature_picker.py` 产出 `<基名>_拾取.html`；`feature_locator.py` 产出 `<基名>_定位图.html`；
  `make_preview.py` 产出 `<基名>_preview.html`（外加一个 `<基名>.stl` 网格）。
- **HTML 与同目录 `vendor/` 的相对位置不能拆开**：生成时 three.js 会被复制到输出目录的
  `vendor/` 下，HTML 用相对路径 `./vendor/` 引用它。移动/分发时务必把 HTML 与同级 `vendor/`
  一起带走，否则离线预览无法加载 3D 库。
- `vendor/` 随仓库**提交（git tracked）**，是从 CDN 固定版本（three@0.160.0）vendored 进来的，
  **运行时不再从 CDN 下载**；`feature_picker` 启动时会用 SHA-256 校验 vendor 文件完整性。
  （见 [vendor/README.md](vendor/README.md)）
- 默认输出目录 `previews/` 属于**生成产物**，已被 `.gitignore` 忽略，不进版本库。

典型输出目录树（以 `previews/` 为例）：
```
previews/
├── your_model_拾取.html      # feature_picker 产出（可点击 3D）
├── your_model_定位图.html     # feature_locator 产出（2D 编号）
├── your_model_preview.html   # make_preview 产出（整体）
├── your_model.stl            # make_preview 产出的网格
└── vendor/                   # 复制过来的 three.js（与 HTML 同级，不可拆分）
    ├── three.module.min.js
    ├── jsm/controls/OrbitControls.js
    └── jsm/loaders/STLLoader.js
```

## 目录结构
- `cad_core.py` — OCP 核心（读 STEP/IGES、属性、包围盒、mesh/deflection 单一实现）
- `cad_assembly.py` — 装配体解析（Phase A）：STEP → Template+Matrix manifest（装配树 / 世界矩阵 / 去重模板 / glTF 缓存布局，ADR-0002 D3）
- `cad_service.py` — 本地 Web 服务层（Phase A 骨架，starlette）：HTTP + WS 双通道、token 鉴权、SHA 键控幂等缓存（ADR-0002 D2 / R4 / R8 / R17）
- `feature_locator.py` — 曲面枚举 + 分类聚合 + 2D 编号定位图（Feature 模型 + 类型注册表）
- `feature_picker.py` — 特征级 STL 切片 → 可点击 3D 预览（three.js 本地化 + SHA-256 校验）
- `make_preview.py` — 实体整体预览
- `cad_mcp_server.py` — FastMCP server（**11 工具**；`pick_features` / `parse_assembly` / `check_interference` / `audit_assembly` 已接入；`build123d_model` 默认禁用）
- `cad_versions.py` — 增量版本仓库（Phase C）：原子提交（R6）/ 解析链 / 指针回滚（D10）
- `cad_drawing.py` — 图纸导入与语义校准（Phase D，D5/模块六）：DXF 直读 / DWG 经 ODA（探测降级）+ 螺纹/直径/公差提取 + 轻依赖 SVG 渲染
- `evals/` — 评测基准集（D9）：指令三层标注 + `run_evals.py`（黄金轨迹回放 + 几何断言全自动化；LLM 对比层后续接入）
- `cad_build.py` — build123d 字体 import-hook（跨平台无害，修复损坏系统字体导致 import 崩溃）
- `vendor/` — 本地 three.js（three@0.160.0：`three.module.min.js` + `OrbitControls` + `STLLoader`）；见 [vendor/README.md](vendor/README.md)
- `frontend/` — Web 前端 SPA（Vite + three.js，Phase A 骨架）；`dist/` 构建产物随仓库提交（离线运行无需 Node）
- `tests/` — pytest 测试套件（Phase 3 建立；Phase A/B/C/D 增装配/服务层/编辑流/图纸用例，当前 **112 个用例全绿**）
- `pytest.ini` — pytest 配置（含 `--cov` 覆盖率）
- `docs/architecture/copilot-vision.md` — Web Copilot 系统设想（原根目录 `设想.txt` 迁入；文首附与 ADR-0002 的差异摘要）
- `docs/decisions/0001-ocp-vs-freecad-base.md` — ADR-0001：OCP 轻量底座 vs FreeCAD 方案对比（原 `comparison.md`）
- `docs/decisions/0002-web-copilot-expansion.md` — ADR-0002：Web Copilot 扩展决策（D1–D10 + 延续约束 + 风险登记 R1–R17）
- `CHANGELOG.md` — 版本变更记录（基线 v0.1.0）
- `selftest*.step` / `selftest.iges` — 示例输入，用于冒烟测试

## 同步更新
本仓库即单一可信源。任意机器上：

```bash
git pull
python bootstrap.py      # 仅当依赖变更时需要重跑；代码更新直接生效
```

即可拿到最新代码与（如有）依赖变更。

## 开发与测试

### 冒烟测试
直接用示例输入跑脚本即可快速验证：
```bash
python feature_picker.py selftest.step --out-dir previews
python feature_locator.py selftest.step --axis auto --out-dir previews
python make_preview.py selftest.step
```
CI 还会调用 `pick_features` MCP 工具并对 `feature_count > 0` 与 HTML 存在性做断言。

### pytest 回归套件
```bash
venv/Scripts/python -m pytest tests/ -q    # Windows
venv/bin/python        -m pytest tests/ -q    # macOS / Linux
```
- `pytest.ini` 已配置覆盖率：`--cov=cad_core --cov=feature_locator --cov=feature_picker --cov=make_preview --cov=cad_mcp_server`。
- 测试依赖 `pytest` / `pytest-cov` **不在**运行时 `requirements.txt` 中，需在 venv 里单独 `pip install pytest pytest-cov`。
- CI 为 **3 平台矩阵**（ubuntu / macOS / windows，Python 3.13，环境变量 `PYTHONUTF8=1`），执行 bootstrap → 冒烟 → pytest。

### 新增 MCP 工具规范：docstring 即 agent 文档
每个用 `@mcp.tool` 装饰的工具，其 **docstring 会直接作为工具描述展示给 WorkBuddy / agent**。
因此写新工具时，docstring 就是面向 agent 的契约文档，必须写清楚：
- 工具做什么、参数含义（含默认值/取值范围）、返回什么；
- 任何安全影响（如 `build123d_model` 标注「执行任意代码 = 本地代码执行、默认禁用」）；
- 路径限制（所有用户路径被限制在 `CAD_MCP_ALLOWED_DIRS` 白名单内）。
不要依赖代码外的隐藏说明——agent 只能看到 docstring。

## 故障排查

### OCP / cadquery 轮子安装失败
- 先确认 Python 版本：`python --version` 应为 **3.13**（已验证 `cadquery-ocp-novtk` / `build123d`
  的 **cp313 wheel 存在**）；用其他版本可能报 `no matching distribution`。
- 首次运行需要联网从 PyPI 拉取依赖，由 `bootstrap.py` 创建 `venv/` 并安装；
  之后代码更新通常无需重跑，除非 `requirements.txt` 变了。
- 若公司网络有 PyPI 镜像/代理限制，先在能联网的环境装好再拷贝 `venv/`。

### build123d 因损坏系统字体导入崩溃
- 现象：启用 build123d 时（仅 `build123d_model` 工具且设了 `CAD_MCP_ALLOW_BUILD123D=1` 才会触发
  build123d import）报 `TTLibError: Not a TrueType or OpenType font (bad sfntVersion)`，
  整个 import 中断。本机 culprit 是 `C:/Windows/Fonts/mstmc.ttf` 等损坏字体——build123d 的
  `text.py` 在 import 时扫描系统字体目录并对每个字体调 `TTFont(path)`。
- 解决：`cad_build.py` 已内置 **import-hook**（`_Build123dTextFinder`）拦截 `build123d.text`，
  把 `FontManager.register_font` 包一层，加载失败的字体直接跳过。**普通建模不需要字体即可正常
  import，无需手动处理。** 仅在确实需要 build123d 文本建模时才可能触及字体路径。
- 其它大部分工具（convert / extract / pick 等）不 import build123d，不受此影响。

### Windows 中文文件名 / 路径编码（UnicodeEncodeError）
- 代码已统一用 **utf-8** 读写（`open(..., encoding="utf-8")`），且 HTML 文件名/标题都做了
  `html.escape` 转义；跨平台测试（含中文文件名）在 3 OS 上验证通过。
- 若 Windows 控制台打印中文报 `UnicodeEncodeError`，把终端设为 UTF-8，或设置环境变量
  `PYTHONUTF8=1`（CI 已默认设置）。**切勿**用 GBK 去解码中文路径/文件名——本仓库代码不使用 GBK。

## 打包 / 给其他 agent 用
分发时把整个仓库（含 `vendor/`）一起带走即可；缺失 `vendor/` 时历史版本会尝试从 CDN 下载，
但**当前版本要求 vendor 已随仓库提交**（运行时 SHA-256 校验，缺文件即报错，不会静默联网）。
HTML 与同目录 `vendor/` 的相对位置不能拆开。

## 注意
- `venv/`、`previews/`（生成产物）、`*.stl`、`*.html` 等已被 `.gitignore` 排除，不进版本库。
- `build123d_model` 工具出于安全默认禁用（见上文「MCP Server 使用」）；`pick_features` 已接入，不再是缺口。
