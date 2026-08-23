# AGENTS.md — CAD Copilot 操作手册（面向 Agent / LLM）

本仓库是一个本地 AI CAD Copilot：Web 服务（`cad_service.py`，端口 8764）承载
3D 装配视口 + 图纸对照 + 编辑会话，浏览器内的点选（零件/特征）是**用户与 agent
之间的语义通道**——agent 据此精确知道要处理哪个对象。

## 打开 / 处理图纸或 STEP 的标准流程（必须走这条）

1. **探活**：`bash cad_service_ctl.sh status`
   （等价：`curl -H "Authorization: Bearer cad-local-dev-2026" http://127.0.0.1:8764/api/config`
   返回 `200` 即在线）
2. **不在线则启动**：`bash cad_service_ctl.sh start`
   ⚠️ **必须用「后台任务」方式执行**（工具里的 `run_in_background`）。脚本内部
   `exec python cad_service.py`，该后台任务即服务进程本身；用普通命令前台跑会被
   命令结束杀掉。
3. **打开页面**：`bash cad_service_ctl.sh open`
   （macOS 调系统默认浏览器打开；非 macOS 或失败则把下方 URL 给用户）

## 固定地址（token 永远是 `cad-local-dev-2026`）

- 图纸对照：`http://127.0.0.1:8764/app/drawing.html?token=cad-local-dev-2026`
- 装配预览：`http://127.0.0.1:8764/app/?token=cad-local-dev-2026&load=<encodeURIComponent(绝对路径)>`
  或 `?cacheKey=<键>`

## 禁止（会导致服务起不来 / token 错配）

- ❌ 直接 `python cad_service.py`（前台阻塞或被回收）→ 用 `cad_service_ctl.sh start`
- ❌ 为"打开图纸"启动 Vite 开发服务器（`npm run dev` / 5173）→ 8764 生产链接已自带前端
- ❌ 假设无 token 或用"启动时随机打印的 token"拼 URL

## 生命周期

服务随对话会话存活（足够一次交互），长时间不用被回收属正常；用户再次要处理时
重跑第 1–3 步即可。**无需开机自启 / 永久常驻。**

（若用户确要永久常驻：在真实终端 `launchctl load ~/Library/LaunchAgents/com.cadcopilot.service.plist`
或双击 `start_cad_service.command`。）

## 其它约定

- 几何能力也可经 MCP（`cad_mcp_server.py`，18 工具）调用；**落版本永远留给用户在 Web UI 点确认**。
- 读用户选中上下文用 `get_user_selection`（与 Web 视口拾取同源 id）。
- DWG 需 ODA File Converter（部署阶段探测/安装，见 README「ODA 插件」）。
- 详细端点/工具表见 `README.md`。
