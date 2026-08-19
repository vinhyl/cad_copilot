# CAD 工具链 (cad_tools)

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

## 快速开始：生成拾取预览
```bash
venv/Scripts/python feature_picker.py your_model.step --out-dir previews   # Windows
venv/bin/python        feature_picker.py your_model.step --out-dir previews   # macOS / Linux
```
生成的 `previews/your_model_拾取.html` 与同目录 `vendor/` 一起打开，**断网也能用**。
更完整的命令行参数见下文「命令行速查」。

## MCP Server 使用

`cad_mcp_server.py` 是一个基于 FastMCP 的 stdio MCP server，把上面的几何能力暴露给
WorkBuddy 等 agent 调用。它当前提供 **9 个工具**（详见下表），其中 `build123d_model`
出于安全考虑**默认禁用**。

### 启动（stdio）
```bash
venv/Scripts/python cad_mcp_server.py    # Windows
venv/bin/python        cad_mcp_server.py    # macOS / Linux
```
默认从标准输入/输出以 JSON-RPC 通信，由 MCP 客户端（如 WorkBuddy）拉起，无需手动常驻。

### 接入 WorkBuddy（mcp.json 示例）
在 WorkBuddy 的 MCP 配置（`mcp.json`）中加入：
```json
{
  "mcpServers": {
    "cad-engine": {
      "command": "venv/Scripts/python",
      "args": ["cad_mcp_server.py"]
    }
  }
}
```
> macOS / Linux 把 `command` 换成 `venv/bin/python`。路径相对于仓库根目录；若 server 在
> 其他目录，请写绝对路径或先 `cd` 到仓库根。

### 8 个工具一览
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
| `build123d_model` | 运行 build123d 建模脚本（**执行任意代码 = 本地代码执行**） | ⛔ **默认禁用**（设 `CAD_MCP_ALLOW_BUILD123D=1` 才启用） |

> 安全提示：`build123d_model` 会以 MCP server 进程的完整权限执行传入的任意 Python
> 脚本，等于本地代码执行。它被默认禁用，且每次调用都强制走超时隔离子进程。
> **切勿**在不可信、共享或多租户环境中启用。其余 8 个工具的路径都被限制在
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
| `POST /api/assembly/parse` | 装配 STEP → manifest + 写缓存（Bearer token） |
| `GET /cache/{key}/...` | 静态服务缓存产物（tree_structure.json / gltf_library，防目录穿越） |
| `WS /ws?token=...` | JSON 协议骨架（ping / parse；任务队列与进度事件为后续增量） |

要点：缓存目录按源文件 **SHA-256 前 16 位** 命名——同内容重复导入直接命中
（`cache_hit: true`），内容变化自动换键（R8/R17）；几何操作全局串行锁（R4）；
仅绑 127.0.0.1、token 鉴权、无 CORS（延续约束一）。
配置：`CAD_SERVICE_TOKEN` / `CAD_SERVICE_ALLOWED_DIRS` / `CAD_SERVICE_WORKSPACE` /
`CAD_SERVICE_HOST` / `CAD_SERVICE_PORT`。

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
- `cad_mcp_server.py` — FastMCP server（**9 工具**；`pick_features` / `parse_assembly` 已接入；`build123d_model` 默认禁用）
- `cad_build.py` — build123d 字体 import-hook（跨平台无害，修复损坏系统字体导致 import 崩溃）
- `vendor/` — 本地 three.js（three@0.160.0：`three.module.min.js` + `OrbitControls` + `STLLoader`）；见 [vendor/README.md](vendor/README.md)
- `tests/` — pytest 测试套件（Phase 3 建立；Phase A 增装配/服务层用例，当前 **83 个用例全绿**）
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
