# AGENTS.md — CAD Copilot 操作手册（面向 Agent / LLM）

本仓库是一个本地 AI CAD Copilot：Web 服务（`cad_service.py`，端口 8764）承载
3D 装配视口 + 图纸对照 + 编辑会话，浏览器内的点选（零件/特征）是**用户与 agent
之间的语义通道**——agent 据此精确知道要处理哪个对象。

## 使用者分类（重要：默认普通用户模式）

agent 面对两类人，规则是**默认当作纯普通用户，只在显式开发诉求时才进入开发模式**。

- **普通用户（默认）**：他们只是使用产品。只回复"如何操作/处理零件/看效果"等使用口径，
  用大白话；**永远不要主动**聊开发话题——git/commit/push/PR/构建(npm/vite)/代码/测试/
  MCP/服务内部/版本链/"dist 重建"等一律不主动引入。不要在回复里出现这些字眼。
- **开发模式（仅显式触发）**：只有消息明显是开发诉求（让我提交、改代码、重构、建构建、
  跑测试、讲实现细节等）或明确说"我是开发/这是开发需求"时，才回到开发视角（提交行为、
  代码引用、构建说明等）。

判据看**当条消息意图**，不看历史。拿不准时按普通用户口径回复。若产品要硬性隔离终端用户
（访客入口、独立部署），agent 应读取会话/入口标记并按普通用户口径兜底，不依赖用户措辞。

### 双 token 入口（方案 B，配合使用者分类）

服务同时认两个 token，**默认一律走 guest（普通用户操作通道）**，只有显式进入开发模式
才切到 dev：

- **guest `cad-guest-2026`**：默认操作通道。所有打开页面 / 自调 API / MCP 调用默认用它。
  只做使用口径。
- **dev `cad-local-dev-2026`**：开发通道。仅当当条消息明显是开发诉求（提交/改代码/构建/
  跑测试）或明确说"开发模式"时才切到它。

打开页面、探活、start 脚本均默认用 guest；需要与用户确认"这是开发操作"前不要用 dev。

### 按 token 判定 mode（权威，不看措辞）

token 是**入口身份的硬标记，优先于用户措辞**。agent 每轮先确认自己用的是哪个 token / 当前
入口 mode，再决定口径；用户嘴里的措辞（哪怕说"提交/改代码"）**不能**把 guest 入口升级成开发：

1. **怎么读 mode**：
   - 本次会话/URL 用的 access token：`?token=…`、`Authorization: Bearer …`、或
     MCP 的 `CAD_SERVICE_GUEST_TOKEN`/`CAD_SERVICE_TOKEN`；
   - 服务侧判定：`curl -H "Authorization: Bearer <当前token>" http://127.0.0.1:8764/api/config`
     响应体 `"mode":"guest"` 或 `"mode":"dev"`（取自己实际调用的 token 为准）。
2. **mode=guest（默认，绝大多数情况）**：纯使用口径。不动开发话题，不提交/不引用代码，
   **即使**用户用词像开发（"提交""改代码""构建"），也按普通用户兜底——引导用产品、
   或提醒需切换到开发入口，绝不直接执行开发动作。
3. **mode=dev**：只有显式进入开发模式才出现，agent 才谈开发、执行开发动作（提交/构建等）。
4. **切换规则**：guest↔dev 由谁进哪个入口决定。agent 不擅自改 token；用户要开发动作但当前
   token 是 guest 时，说明需从 dev 入口（或明说"开发模式"）再操作。

## 启动服务 / 打开页面的标准流程（必须走这条）

1. **探活**：`bash cad_service_ctl.sh status`
   （等价：`curl -H "Authorization: Bearer cad-guest-2026" http://127.0.0.1:8764/api/config`
   返回 `200` 即在线；响应体含 `"mode":"guest"|"dev"` 可判定当前入口）
2. **不在线则启动**：`bash cad_service_ctl.sh start`
   ⚠️ **必须用「后台任务」方式执行**（工具里的 `run_in_background`）。脚本内部
   `exec <venv/python> cad_service.py`（venv 解释器按 OS 自动选择），该后台任务即服务进程本身；用普通命令前台跑会被
   命令结束杀掉。
3. **打开页面**：`bash cad_service_ctl.sh open`
   （默认打开「首页」`index.html`；自动调系统默认浏览器打开；若打开失败则把下方 URL 给用户）

## 固定地址（默认 token 为 guest `cad-guest-2026`，dev 需显式）

- **首页（默认落地页）**：`http://127.0.0.1:8764/app/index.html?token=cad-guest-2026`
  普通「启动服务」即进这里（文件列表 / 装配预览入口）。
- **启动并打开某个具体文件**：在首页地址后追加
  `&load=<encodeURIComponent(绝对路径)>` 或 `?cacheKey=<键>`，例如
  `http://127.0.0.1:8764/app/index.html?token=cad-guest-2026&load=<encodeURIComponent(绝对路径)>`。
  这是「启动服务并直接打开某份图纸 / 装配」时才用的方式，**不是默认**。
- **图纸对照（二级视图）**：`http://127.0.0.1:8764/app/drawing.html?token=cad-guest-2026`
- **编辑会话（二级视图）**：`http://127.0.0.1:8764/app/edit.html?token=cad-guest-2026`
- **报告中心（二级视图）**：`http://127.0.0.1:8764/app/report.html?token=cad-guest-2026`

## 禁止（会导致服务起不来 / token 错配）

- ❌ 直接 `python cad_service.py`（前台阻塞或被回收）→ 用 `cad_service_ctl.sh start`
- ❌ 为"打开图纸"启动 Vite 开发服务器（`npm run dev` / 5173）→ 8764 生产链接已自带前端
- ❌ 假设无 token 或用"启动时随机打印的 token"拼 URL

## 生命周期

服务随对话会话存活（足够一次交互），长时间不用被回收属正常；用户再次要处理时
重跑第 1–3 步即可。**无需开机自启 / 永久常驻。**

（若用户确要永久常驻：macOS 在真实终端 `launchctl load ~/Library/LaunchAgents/com.cadcopilot.service.plist`
或双击 `start_cad_service.command`；Windows 可建计划任务 / 开机脚本，或会话内直接 `bash cad_service_ctl.sh start`。
主动停止用 `bash cad_service_ctl.sh stop`（跨平台，自动按 OS 选 `lsof` / `netstat`+`taskkill`）。）

## 前端 dist 必须与 src 同步（硬性规则）

`frontend/dist` 是随仓库提交的成品，`cad_service.py` 直接 serve 它（运行期与
`node_modules` 完全解耦）。它**必须**由 `frontend/src` 经 `npm run build` 重建而来，
**绝不**手改 dist、也绝不提交"和 src 对不上的 dist"。

- 提交前端改动前：`cd frontend && npm ci && npm run build`，确认
  `git diff --quiet HEAD -- frontend/dist` 为空（无新增/改动/删除的 chunk）后再 `git add`。
- 一致性校验脚本：`scripts/check-dist-sync.sh`（npm ci + build + 比对 dist 与 HEAD）。
  - CI 的 `frontend-dist` job 对每次 push/PR 都跑，dist 不同步直接红。
  - 本机预装 hook：`.githooks/pre-commit`（`git config core.hooksPath .githooks` 启用）；
    frontend 源码变动时自动拦截"未重建的 dist"。
- 根因教训：曾提交过与 src 对不上的 dist（dist 重做 DOM、src 仍按旧 id 找元素），
  导致编辑页功能静默失效。详见历史反面案例（提交 1820116）。

## 其它约定

- 几何能力也可经 MCP（`cad_mcp_server.py`，18 工具）调用；**落版本永远留给用户在 Web UI 点确认**。
- 读用户选中上下文用 `get_user_selection`（与 Web 视口拾取同源 id）。
- DWG 需 ODA File Converter（部署阶段探测/安装，见 README「ODA 插件」）。
- 详细端点/工具表见 `README.md`。
