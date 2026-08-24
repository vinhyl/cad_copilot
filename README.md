# CAD Copilot (cad_copilot)

> 版本：**v0.1.0** — 变更记录见 [CHANGELOG.md](CHANGELOG.md)

本地 AI CAD Copilot 工具链：`cad_service.py`（Web 服务：3D 装配视口 + 特征拾取 +
编辑会话（草稿模式）+ 版本管理 + 报告中心 + 图纸对照 + FEA/渲染任务），配套
MCP server 供 agent 以 stdio 调用同一套几何能力（双 transport，共享库层与缓存）。
交互界面集中在 Web 前端（`/app` 多页应用：首页 / 编辑 / 图纸 / 报告）；agent 经
会话工具组读写草稿与用户选中上下文，**确认落版本永远留给用户**。

## 🤖 给 Agent / LLM 的操作须知（必读，优先于下面各节）

本服务是**用户与 agent 之间的语义通道**：用户在浏览器点选零件/特征 → agent 精确
知道要处理哪个对象。凡是"打开 / 预览 / 处理图纸或 STEP"，**必须按下述流程**，
不要走其它方式（否则服务起不来或 token 错配，用户无感知）。

1. **先探活**：`bash cad_service_ctl.sh status`
   （等价 `curl -H "Authorization: Bearer cad-local-dev-2026" http://127.0.0.1:8764/api/config`
   返回 `200` 即在线）
2. **不在线则启动**：`bash cad_service_ctl.sh start`
   ⚠️ **必须用「后台任务」方式执行**（工具里的 run_in_background）。脚本内部
   `exec python cad_service.py`，该后台任务即服务进程本身；用普通命令前台跑会被
   命令结束杀掉。
3. **打开页面**：`bash cad_service_ctl.sh open`
   （自动调系统默认浏览器打开；若打开失败则把下方 URL 给用户）
4. **固定地址**（token 固定为 `cad-local-dev-2026`，**不要**依赖"启动时随机打印的 token"）：
   - 图纸对照：`http://127.0.0.1:8764/app/drawing.html?token=cad-local-dev-2026`
   - 装配预览：`http://127.0.0.1:8764/app/?token=cad-local-dev-2026&load=<encodeURIComponent(绝对路径)>`
     或 `?cacheKey=<键>`

**禁止**（这些都会导致服务不可用 / token 错配）：
- ❌ 直接 `python cad_service.py`（前台阻塞或被回收）→ 统一用 `cad_service_ctl.sh start`
- ❌ 为"打开图纸"去启动 Vite 开发服务器（`npm run dev` / 5173）→ 生产链接 8764 已自带前端，无需 Node
- ❌ 假设无 token 或用随机 token 拼 URL

**生命周期**：服务随对话会话存活（足够一次交互），长时间不用被回收属正常；用户再次
要处理时重跑第 1–3 步即可。无需开机自启 / 永久常驻。

## 环境要求
- Python 3.13（3.12+ 一般也可，但 OCP 轮子按 3.13 验证；`cadquery-ocp-novtk` / `build123d` 已确认有 cp313 wheel 可用）
- Windows / macOS / Linux
- 首次部署需联网从 PyPI 拉取 Python 依赖（`bootstrap.py` 处理）；运行期全部离线

## 一键部署
```bash
python bootstrap.py
```
脚本会创建 `venv/` 并安装 `requirements.txt` 里的依赖
（cadquery-ocp-novtk / build123d / fastmcp / numpy）。

Windows 与 macOS 上命令**完全相同**；仅首次运行需联网。

`bootstrap.py` 只负责环境（venv + 依赖）。**部署阶段还需由 agent 把 `cad-engine`
接入所用客户端**（见下文「MCP Server 使用」的连接片段）；登记后需在客户端点
**Trust** 启用，agent 才能调用那 17 个 CAD 工具（`build123d_model` 默认禁用）。

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

## 快速开始：启动 Web 服务

> **Agent / LLM 请直接看上方「🤖 给 Agent / LLM 的操作须知」**，用 `cad_service_ctl.sh`
> 探活 / 启动 / 打开 / 停止，不要手动 `python cad_service.py`。
>
> `cad_service_ctl.sh` 已**跨平台**（macOS / Linux / Windows-GitBash 通用）：仓库根由脚本自身位置自动推导（不写死任何绝对路径），venv 解释器、打开浏览器、停止命令均按 OS 自动选择——同一份脚本在任意设备 / 任意路径下都能直接用。

人工启动（固定 token `cad-local-dev-2026`，无需看启动日志）：
```bash
venv/Scripts/python cad_service.py   # Windows
venv/bin/python        cad_service.py   # macOS / Linux
```
浏览器打开 `http://127.0.0.1:8764/app?token=cad-local-dev-2026`——上传/拖入
STEP 即得 3D 装配视口 + 特征拾取 + 编辑会话（草稿模式）。详见下文「本地 Web
服务」与「Web 前端」两节。

## MCP Server 使用

`cad_mcp_server.py` 是一个基于 FastMCP 的 stdio MCP server，把上面的几何能力暴露给
WorkBuddy 等 agent 调用。它当前提供 **18 个工具**（详见下表）：11 个几何工具 +
7 个会话协作工具（读写草稿 / 读用户选中 / 会话发现 / 版本切换 / 生成报告），
其中 `build123d_model` 出于安全考虑**默认禁用**。

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

### 18 个工具一览
| 工具 | 说明 | 默认状态 |
|------|------|---------|
| `convert_file` | STEP/IGES/STL/BREP 格式互转 | ✅ 启用 |
| `extract_properties` | 体积 / 面积 / 包围盒 / 质心 / 拓扑 / 是否装配体 | ✅ 启用 |
| `batch_convert` | 批量转换目录下所有支持文件，并写出 JSON 报告 | ✅ 启用 |
| `create_primitive` | 生成基本体：box / cylinder | ✅ 启用 |
| `edit_geometry` | 几何编辑：fillet / chamfer / scale / drill | ✅ 启用 |
| `boolean_parts` | 布尔运算：fuse / cut / common | ✅ 启用 |
| `pick_features` | 特征结构化枚举（稳定 id #N / #N.k / P#，与 Web 视口拾取同源；不写文件） | ✅ 启用 |
| `parse_assembly` | 装配体 STEP → 装配树 + 4×4 矩阵 + 去重零件模板（glTF 缓存，Web 前端 Template+Matrix 数据源） | ✅ 启用 |
| `check_interference` | 装配体全实例对布尔干涉审计（确定性计算，D8；仅提醒不拦保存） | ✅ 启用 |
| `audit_assembly` | 一键体检：干涉 + DFM 规则（小孔/深孔/薄壁，模块七） | ✅ 启用 |
| `list_sessions` | 会话发现：服务端最近打开的装配体（cacheKey / 时间 / 草稿步骤数），跨浏览器同步 | ✅ 启用 |
| `get_user_selection` | 读用户当前选中（零件 / 特征 + 所在页面 / 标签页，多窗口区分）——"这个零件厚度加 1mm"式对话的上下文来源 | ✅ 启用 |
| `read_draft` | 读当前草稿步骤表（声明式、多目标、可增删） | ✅ 启用 |
| `preview_draft` | 触发草稿重放预览（草稿几何 + 增量干涉结果） | ✅ 启用 |
| `edit_draft` | **只写草稿不落版本**：追加 / 替换 / 删除草稿步骤；落版本由用户在 Web UI 点「确认保存」 | ✅ 启用 |
| `checkout_version` | 版本切换 / 回滚（指针切换，历史文件永不重写） | ✅ 启用 |
| `generate_report` | 生成装配体快照报告（体检 + 统计 + 版本历史） | ✅ 启用 |
| `build123d_model` | 运行 build123d 建模脚本（**执行任意代码 = 本地代码执行**） | ⛔ **默认禁用**（设 `CAD_MCP_ALLOW_BUILD123D=1` 才启用） |

> 安全提示：`build123d_model` 会以 MCP server 进程的完整权限执行传入的任意 Python
> 脚本，等于本地代码执行。它被默认禁用，且每次调用都强制走超时隔离子进程。
> **切勿**在不可信、共享或多租户环境中启用。其余 17 个工具的路径都被限制在
> `CAD_MCP_ALLOWED_DIRS` 白名单内（默认 `.`，即 server 工作目录）。

> 数据隐私备注：Agent 调用本 server 时，工具返回值（特征元数据 / 物性 / 文件清单 /
> 修改参数）与整个会话历史会进入所接 LLM 的上下文，且**跨调用累积**（一次会话可拼出
> 零件的完整语义画像）。团队现阶段**批准使用云端 LLM**，覆盖日常 Agent 使用与后续
> 评测飞轮。复评触发点：Phase C 自然语言修改上线前 / 对外分发前 / 更换模型供应商。
> 完整分析见 [ADR-0002 风险登记 R10](docs/decisions/0002-web-copilot-expansion.md)。

## 本地 Web 服务

`cad_service.py` 是面向 Web 前端的本地服务层（starlette，随 fastmcp 附带，
**零新增依赖**），与 MCP server 共享同一套工具库（ADR-0002 D2 双 transport）。

```bash
venv/Scripts/python cad_service.py     # http://127.0.0.1:8764，固定 token cad-local-dev-2026
```
> Agent 请用仓库根的 `cad_service_ctl.sh start`（后台任务方式）拉起，不要手动跑这条命令。

| 端点 | 说明 |
|---|---|
| `GET /health` | 健康检查（免鉴权，仅本机） |
| `GET /api/config` | 启动配置（allowed_dirs，前端路径引导用） |
| `POST /api/upload?name=` | **显式授权输入通道**：原始请求体 → 内容寻址落盘 `uploads/<sha256>/`（同内容去重回路径） |
| `POST /api/assembly/parse` | 装配 STEP → manifest + 写缓存（Bearer token） |
| `GET /api/assembly/view?cache_key=` | cacheKey 直载已缓存装配体（不读源文件、不重 parse；回首页 / 最近列表 / URL 引导通道） |
| `POST /api/assembly/edit` | **写操作**：模板编辑 → 原子版本提交（干涉仅前端提醒，不拦截） |
| `GET /api/assembly/audit?cache_key=` | 一键体检：干涉 + DFM 规则（确定性计算） |
| `GET /api/drafts?cache_key=` | 草稿读取（单槽位：声明式步骤表） |
| `POST /api/drafts/save` | 手动保存草稿（单槽位覆盖） |
| `DELETE /api/drafts?cache_key=` | 放弃草稿（清空步骤与预览产物） |
| `POST /api/drafts/preview` | 草稿重放 → 草稿 manifest + 增量干涉（`level=bbox` 默认 AABB 粗筛毫秒级 / `exact` 布尔精检） |
| `POST /api/drafts/confirm` | **确认保存**：全部草稿步骤原子落为一个版本（干涉仅前端提醒，不拦截） |
| `POST /api/drafts/fea-compare` | FEA 双跑对比：基线 vs 草稿目标模板静力学（R5 job 模式） |
| `POST /api/reports/generate` | 生成快照报告（体检 + 统计 + 版本历史聚合） |
| `GET /api/reports[?cache_key=]` / `GET /api/reports/get` | 报告列表 / 单份报告读取 |
| `GET /api/sessions` | 服务端最近会话列表（前端「最近使用」数据源，跨浏览器同步） |
| `POST` / `GET /api/selection` | 用户选中上行 / 读取（零件 / 特征 + page + tab，多窗口区分；agent 经 `get_user_selection` 消费） |
| `POST` / `GET /api/logs/client` | 前端错误上报（error / unhandledrejection + 堆栈）/ 查询 |
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
| `GET /drafts/{key}/...` | 静态服务草稿预览 glTF（防目录穿越） |
| `GET /app/` | Web 前端多页应用（`frontend/dist` 构建产物，见下节） |
| `WS /ws?token=...` | JSON 协议（ping / parse）+ **事件广播**：`draft_saved` / `draft_deleted` / `version_changed` / `selection_changed` / `report_added`——agent 内置浏览器与系统浏览器双开时状态互通 |

要点：缓存目录按源文件 **SHA-256 前 16 位** 命名——同内容重复导入直接命中
（`cache_hit: true`），内容变化自动换键（R8/R17）；几何操作全局串行锁（R4），
但 OCP 重活已下放线程池——长任务（报告生成 / 布尔精检）执行期间服务保持响应
（health / 静态文件 / WS 不冻结）；模板形状按 (path, mtime) 进程级缓存（未编辑
几何不重读 STEP）；仅绑 127.0.0.1、token 鉴权、无 CORS（延续约束一）。
服务日志：`workspace/logs/service.log`（访问日志 + SLOW 标记 + 异常堆栈，
2 MB × 3 轮转）；前端错误经 `/api/logs/client` 汇入同一日志体系。
配置：`CAD_SERVICE_TOKEN` / `CAD_SERVICE_ALLOWED_DIRS` / `CAD_SERVICE_WORKSPACE` /
`CAD_SERVICE_HOST` / `CAD_SERVICE_PORT` / `CAD_SERVICE_FRONTEND_DIR`。

## Web 前端（多页 MPA）

`frontend/` 为 Vite **多入口 MPA** + three.js（three@0.163.0，npm 锁定版本）+
原生 JS，四个页面按职责拆分（页面间经 URL 参数传递 cacheKey / scope）：

| 页面 | 入口 | 职责 |
|---|---|---|
| 首页 | `/app/`（index.html） | 装配预览（**Template+Matrix 实例化渲染**：每唯一零件一份 glTF、实例共享 `InstancedMesh`；装配树双向联动）、选取、**上下文动作区**（按选中层级：装配体 / 子装配体 / 零件动态切换操作）、体检 / 力学 / 渲染 / 图纸 / 编辑入口 |
| 编辑会话 | `/app/edit.html` | 草稿模式编辑闭环（见下） |
| 图纸对照 | `/app/drawing.html` | DXF / DWG 语义对照独立页 |
| 报告中心 | `/app/report.html` | 快照报告浏览（体检 + 统计 + 版本历史） |

代码分层：`pages/`（home / edit / drawing / report 页逻辑）+ `shared/`
（utils / jobs / plugins / api / scene / tree）。

**输入交互**（路径手输已移除，三条显式通道 + agent 入口）：

- **「打开文件…」/ 拖放**：任意位置文件经 `POST /api/upload` 显式授权进入——
  内容寻址落盘 `uploads/<sha256>/<文件名>`（重复上传去重回路径），前端按扩展名
  自动路由（STEP → 装配加载；DXF/DWG → 图纸对照导入）
- **最近使用**：**服务端会话列表**（`GET /api/sessions`，跨浏览器同步）——
  点击经 `GET /api/assembly/view` 按 cacheKey 直载（源文件移动 / 删除后仍可
  打开，缓存命中秒开）
- **agent 预览入口**：`/app?token=...&load=<encodeURIComponent(路径)>` 或
  `?cacheKey=<键>`——agent（内置浏览器 navigate 或生成链接）驱动加载；`?token=`
  / `?load=` / `?cacheKey=` **保留在地址栏**（agent 内置浏览器"用系统浏览器打开"
  后免重新输入 token），路径仍受服务端 `safe_input_path` 权威校验
- 白名单外的路径（绕过前置校验时）：403 + 可操作文案（移入目录 / 设
  `CAD_SERVICE_ALLOWED_DIRS`），`GET /api/config` 供前端展示可访问目录

**插件状态面板（D5/D7 探测可视化）**：侧栏底部常驻 ODA / FEA / Blender 探测
结果（绿点=可用、灰点=未安装，悬停显示路径或安装指引），↻ 即时重探测；
缺依赖时对应功能给出明确降级提示，其余功能不受影响。

**R5 任务卡片**：FEA / 渲染按钮触发异步 job（202 + job_id），右下角浮卡显示
阶段中文映射 + 进度条（FEA 中继内层 FreeCAD 七阶段真实进度；渲染为不确定
进度），支持取消（协作式终止）；FEA 完成出结果摘要，渲染完成弹大图。

**视图操作集**（视图状态 ≠ 数据状态，全部不落盘；首页与编辑页共用 scene.js）：

| 操作 | 入口 | 说明 |
|---|---|---|
| 多层级爆炸 | 工具栏 / 观察工具条滑条 | 后端算好的相对 explode 向量沿祖先链累积 × 比例 |
| 特征拾取 | 选中零件 → 侧栏特征面板 | 点特征条目 → 3D 橙色高亮 overlay（cache `features/*.json|gltf`） |
| 隔离 / X 光 / 剖切面 | 工具栏 / 观察工具条 | 只显选中子树 / 半透明鬼影 / 三轴（X/Y/Z）clipping plane |
| 临时拖拽移动 | 选中 →「移动」 | TransformControls gizmo，选装配体整组移动；「复位移动」还原 |
| 相机书签 | 「存视角 / 回视角」 | localStorage，不进版本树 |
| 复位视图 | 一键 | 爆炸/显隐/临时移动/相机全部还原 |
| 视角锁定光照 | 默认（无需操作） | 环境光照随相机旋转锁死屏幕（顶亮底暗相对屏幕固定）；金属材质哑光化 + 平滑渐变环境 + NeutralToneMapping，任意角度无过曝、无顶亮底暗、无灰 |
| 模型配色 | 工具栏「配色」 | 原色 / 月灰 / 钢蓝 / 暖沙 / 墨绿 / 米白——低亮度表面色，缓和纯白面刺眼；只改模型色、背景恒定深色；localStorage 跨页记忆、即时生效 |

**编辑会话页（草稿模式编辑闭环）**：

- **双视口**：基线锁定（永远不动，对比基准）vs 草稿实时（重放当前步骤表）；
  **相机联动**可锁/解锁（开启即对齐）；观察工具条（爆炸 / 三轴剖切 / 鬼影 /
  视角书签）两视口同步生效
- **声明式草稿步骤表**（左栏）：多目标、可增删；每步变更自动触发草稿重放 +
  干涉检查——**分级**：默认 AABB 粗筛（毫秒级，黄色「可能碰撞」卡片），
  「精确检查」按钮显式布尔精检（红色卡片带穿透体积 mm³ + 耗时秒数）；
  **干涉仅作提醒，不拦保存**（自动粗筛 + 手动精检供自查）
- **操作域二段化**（目标统一由 3D 点选驱动）：
  - **装配操作**（实例级）：**换件 replace**（来源从其它已打开文件选中的零件读取，
    `align` + dx/dy/dz 偏移）、**移动 move**（表单 dx/dy/dz 绝对位移，后写覆盖）
    与 **删除 remove**（整件实例移除，其模板若被其它实例共享则只删这一件）；
    也可开「移动零件」在草稿视口**拖拽**生成 move 步骤（实例级世界系位移）。
  - **零件编辑 = 定点特征**：点零件任意处即命中/就近选中特征（扩孔 / 去凸台），
    目标=该类，配目标模板 / 目标特征。
- **换件身份**：换件后装配树节点**改名 + 「已替换」标记**；被替换模板的特征
  数据/overlay **重指向来源 cache** 并对齐平移贴合新零件；精检/拾取沿用同一矩阵。
- **点空白取消选中**：清除选中但保留视图焦点（子装配/零件视图不跳回整装配）。
- **步骤历史 UI**：步骤卡统一「类型徽标 + 目标 + 详情」；**连续同位移 move 自动
  折叠**为一行；左栏**搜索框**按模板/零件查找并高亮定位、同步编辑区目标；左栏
  布局 = 搜索 → 添加编辑步骤 →（草稿步骤说明 + 历史）。
- **预览范围**：整装配 / 子装配 / 零件 三档 visibility lens 切换（不换页），
  切换**自动取景**到目标包围盒；目标模板高亮；双视口点选联动
- **验证轨道**（右栏）：增量干涉 + FEA 基线 vs 草稿双跑对比（最大位移 /
  von Mises 应力，R5 job 进度）；步骤变更后已有结果标记过期。
  **点击每条干涉结果**在模型上高亮对应的两个零件（A 洋红 / B 青强对比色，
  双视口同染并自动取景）；**精检结果跨刷新保留**（sessionStorage，同标签页
  F5 自动恢复；步序有改动仍保留但标「已过期」），「重置」按钮才丢弃并退回
  自动粗筛——便于逐处处理多处干涉时列表不丢失
- **导航语义**：回首页（保留装配体，view 直载）/ 放弃草稿（步骤清空回基线）/
  保存草稿（单槽位）/ 确认修改（全部步骤原子落为一个版本，回首页）
- **协作边界**：agent 经 MCP 只写草稿（`edit_draft`），「确认保存」按钮永远
  留给用户
- **直接落版本路径**（Phase C 兼容保留）：首页选中零件 → 编辑面板 →
  原子版本提交（干涉仅前端提醒，不拦截）；
  版本面板 v0 基线 + v1..vN 链式增量（每版本只存被改模板的 step/gltf +
  实例 moves + remove 记录），切换/回滚 = 指针移动，历史版本文件永不重写（D10）

**一键体检（模块七）**：工具栏「体检」→ 干涉全实例对审计 + DFM 规则
（小孔 R<0.5 / 深孔 L/D>10 / 平行孔薄壁提示），全部确定性计算（D8）。

**图纸对照（D5）**：独立页（drawing.html，首页「图纸」入口跳入）→ DXF 直读 /
DWG 经 ODA（探测降级，缺失时明确提示装 ODA 或转存 DXF）；**语义提取**（螺纹
M10x1.5 / 直径 Ø8 / 公差 H7/g6，模块六语义真理）+ **零件名语义侧栏**——确定性
规则滤掉图框栏位 / 技术要求 / 视图名 / 标准件规格等噪声，剩下纯零件名并去重，
"其它标注"再筛一层无意义噪声（日期 / 页码 / 比例 / 版本等），支持模糊搜索与
点击定位；+ 轻依赖 SVG 渲染（LINE/CIRCLE/ARC/LWPOLYLINE/TEXT，零 PIL 依赖）。

- **使用（零 Node 依赖）**：服务起来后浏览器打开
  `http://127.0.0.1:8764/app/drawing.html?token=cad-local-dev-2026`（图纸对照页）。
  Agent 用 `bash cad_service_ctl.sh open` 或 `status` / `start` 管理。构建产物
  `frontend/dist/` **随仓库提交**——分发时带走仓库即可离线运行。
- **开发（需 Node 18+）**：`cd frontend && npm install`，之后 `npm run dev`
  （5173 端口，API/WS 代理到 8764，改源码热更新）；发布前 `npm run build` 并提交
  新的 `dist/`。
  > 注：5173 仅用于改前端源码热更新；**agent 与用户"打开图纸"一律用 8764 生产链接**，
  > 无需 Node。
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

历史版本有三个"生成静态 HTML 预览"的 CLI（`feature_picker.py` / `feature_locator.py` /
`make_preview.py`）。**Web 前端落地后已全部退役**——交互式 3D 拾取、特征面板、整体
预览都由 `cad_service.py` 的 `/app` 视口实时承担（agent 侧经 `?load=` URL 直达）。
`feature_locator.py` 与 `feature_picker.py` 保留为**纯特征识别/枚举库**（cad_service
特征缓存的上游），不再是 CLI。

## 输出产物与目录约定

所有运行期产物集中在 `workspace/`（内容寻址缓存，详见
[docs/architecture/copilot-vision.md](docs/architecture/copilot-vision.md) 的
「工作区目录架构」节）：

```
workspace/
├── uploads/<sha256>/<文件名>   # 上传通道落盘（内容寻址去重）
├── cache/<sha16>/              # 装配解析缓存（tree_structure.json + gltf_library + parts + features）
├── drawings/<sha16>/           # 图纸缓存（ODA 转换 DXF + 渲染 SVG）
├── fea/ render/                # FEA / Blender 任务产物
├── versions/<sha16>/           # 增量版本历史（v0 基线 + v1..vN，含实例 moves）
├── drafts/<sha16>/             # 草稿（步骤表 + 预览 glTF，单槽位）
├── reports/<sha16>/            # 快照报告（体检 + 统计 + 版本历史）
├── selection/                  # 会话与选中状态（最近会话列表 / 用户选中上行）
└── logs/service.log            # 服务日志（访问 + SLOW + 异常堆栈，2 MB × 3 轮转）
```

`workspace/` 属于**生成产物**，已被 `.gitignore` 忽略，不进版本库。

## 目录结构
- `cad_core.py` — OCP 核心（读 STEP/IGES、属性、包围盒、mesh/deflection 单一实现）
- `cad_assembly.py` — 装配体解析（Phase A）：STEP → Template+Matrix manifest（装配树 / 世界矩阵 / 去重模板 / glTF 缓存布局，ADR-0002 D3）
- `cad_service.py` — 本地 Web 服务层（Phase A 骨架，starlette）：HTTP + WS 双通道、token 鉴权、SHA 键控幂等缓存（ADR-0002 D2 / R4 / R8 / R17）
- `feature_locator.py` — 特征识别库：曲面枚举 + 分类聚合 + 模式识别（Feature 模型 + 类型注册表）
- `feature_picker.py` — 特征枚举库：`collect_feature_solids`（cad_service 特征缓存的上游，Web 视口拾取数据源）
- `cad_mcp_server.py` — FastMCP server（**18 工具**：11 几何 + 7 会话协作——读写草稿 / 读用户选中 / 会话发现 / 版本切换 / 报告；`build123d_model` 默认禁用）
- `cad_versions.py` — 增量版本仓库（Phase C）：原子提交（R6）/ 解析链 / 指针回滚（D10）
- `cad_drawing.py` — 图纸导入与语义校准（Phase D，D5/模块六）：DXF 直读 / DWG 经 ODA（探测降级）+ 螺纹/直径/公差提取 + 轻依赖 SVG 渲染
- `cad_jobs.py` — R5 任务管理器：job 状态机 / 进度 / 协作式取消（FEA / 渲染长任务）
- `cad_fea.py` / `cad_render.py` — FEA（FreeCAD+CalculiX）与渲染（Blender）子进程插件（探测降级 + 缓存 + 进度中继）
- `evals/` — 评测基准集（D9）：指令三层标注 + `run_evals.py`（黄金轨迹回放 + 几何断言全自动化；LLM 对比层后续接入）
- `cad_build.py` — build123d 字体 import-hook（跨平台无害，修复损坏系统字体导致 import 崩溃）
- `frontend/` — Web 前端多页 MPA（Vite + three.js：首页 / 编辑 / 图纸 / 报告四入口，`pages/` + `shared/` 分层）；`dist/` 构建产物随仓库提交（离线运行无需 Node）
- `tests/` — pytest 测试套件（Phase 3 建立；Phase A/B/C/D 增装配/服务层/编辑流/图纸用例，当前 **192 个用例全绿**）
- `pytest.ini` — pytest 配置（含 `--cov` 覆盖率）
- `docs/architecture/copilot-vision.md` — Web Copilot 系统设想（原根目录 `设想.txt` 迁入；文首附与 ADR-0002 的差异摘要）
- `docs/decisions/0001-ocp-vs-freecad-base.md` — ADR-0001：OCP 轻量底座 vs FreeCAD 方案对比（原 `comparison.md`）
- `docs/decisions/0002-web-copilot-expansion.md` — ADR-0002：Web Copilot 扩展决策（D1–D10 + 延续约束 + 风险登记 R1–R17）
- `CHANGELOG.md` — 版本变更记录（基线 v0.1.0）
- `selftest*.step` / `selftest.iges` — 示例输入（`selftest.py` 可再生），用于冒烟测试

## 同步更新
本仓库即单一可信源。任意机器上：

```bash
git pull
python bootstrap.py      # 仅当依赖变更时需要重跑；代码更新直接生效
```

即可拿到最新代码与（如有）依赖变更。

## 开发与测试

### 冒烟测试
```bash
python selftest.py       # OCP 工具链端到端（建模 → 读写 → 属性 → 转换）
```
CI 还会调用 `pick_features` MCP 工具并对 `feature_count > 0` 与特征 id 完整性做断言。

### pytest 回归套件
```bash
venv/Scripts/python -m pytest tests/ -q    # Windows
venv/bin/python        -m pytest tests/ -q    # macOS / Linux
```
- `pytest.ini` 已配置覆盖率：`--cov=cad_core --cov=feature_locator --cov=feature_picker --cov=cad_mcp_server --cov=cad_assembly --cov=cad_service`。
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
- 代码已统一用 **utf-8** 读写（`open(..., encoding="utf-8")`）；跨平台测试（含中文文件名）
  在 3 OS 上验证通过（上传通道 / 图纸 SVG 文本均按 utf-8 处理）。
- 若 Windows 控制台打印中文报 `UnicodeEncodeError`，把终端设为 UTF-8，或设置环境变量
  `PYTHONUTF8=1`（CI 已默认设置）。**切勿**用 GBK 去解码中文路径/文件名——本仓库代码不使用 GBK。

## 打包 / 给其他 agent 用
分发时把整个仓库一起带走即可（`frontend/dist/` 随仓库提交，无需 Node；运行期全部离线）。

## 注意
- `venv/`、`workspace/`（生成产物）等已被 `.gitignore` 排除，不进版本库。
- `build123d_model` 工具出于安全默认禁用（见上文「MCP Server 使用」）；`pick_features` 已接入，不再是缺口。
