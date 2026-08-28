# 部署场景模拟：agent 自动部署 cad_copilot 并安装 ODA 插件

下面是一台**全新机器**上，部署 agent 按 README「一键部署」流程跑通时的真实对话。
聚焦 ODA 插件那段——其它步骤只给概览，让你看清"用户会收到哪些提示"。

---

## 场景前提

- 目标机：macOS（Apple 芯片），ODA **尚未安装**（DWG 支持缺这一环）。
- 部署方式：用户在 WorkBuddy 里说"帮我部署 cad_copilot"，agent 接手。

---

## 对话稿

**① 部署 Agent**：先跑环境部署。

```
$ python bootstrap.py
[1/2] 创建 venv/ …
[2/2] 安装 requirements.txt（cadquery-ocp-novtk / build123d / fastmcp / numpy）…
✅ 环境就绪：venv/bin/python 可用
```

**② 部署 Agent**：环境装好了。按约定，MCP 接入这一步由我（部署用的 agent）完成——
把 `cad-engine` 写进 WorkBuddy 的 `~/.workbuddy/mcp.json` 了。

```
✅ 已登记 cad-engine（stdio MCP）到 WorkBuddy 配置
⚠️ 还需你做一步：打开 WorkBuddy「连接器管理 / custom connectors」，
   找到 cad-engine，点 **Trust（信任）** 启用，我才能调用那 18 个 CAD 工具。
   （其中 build123d_model 默认禁用，无需处理，实际可调 17 个）
```

**③ 部署 Agent**：现在检查 ODA 插件——DWG 图纸需要它（DXF 不用）。

```
$ venv/bin/python -c "import cad_drawing; print(cad_drawing.probe_oda_converter())"
None
```

> 🔎 真实参考：本机已装时返回
> `/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter`；
> 新机未装返回 `None`。

**④ 部署 Agent（提示 + 询问）**：

```
⚠️ 没检测到 ODA File Converter。
   使用 DWG 图纸需要它（DXF 不需要）。它是 Open Design Alliance 的免费工具，
   官网免注册直链下载：https://www.opendesign.com/guestfiles/oda_file_converter

   要我现在帮你下载并安装吗？（下载 + 安装全自动，你不用手动操作）
   [ 同意安装 ]   [ 先不装，改用 DXF ]
```

**⑤ 你（用户）**：`同意安装`

**⑥ 部署 Agent**：好，开始装（macOS 取 Apple 芯片 arm64 的 .dmg 直链）。

```
↓ 下载 ODAFileConverter_QT6_macOsX_arm64_15.0dll_27.1.dmg …
📦 挂载 dmg …
📂 拷贝 ODAFileConverter.app → /Applications …
✅ 安装完成
```

**⑦ 部署 Agent（重探测确认）**：

```
$ venv/bin/python -c "import cad_drawing; print(cad_drawing.probe_oda_converter())"
/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter

✅ ODA 已就绪（前端插件面板 ODA 转绿点）。DWG 支持开通。
```

**⑧ 部署 Agent**：收尾。

```
✅ cad_copilot 部署完成
   - Web 服务：bash cad_service_ctl.sh start（后台方式；前台直接跑 python 会被回收）
   - 打开页面：bash cad_service_ctl.sh open（默认首页，token 用默认的 cad-guest-2026）
   - MCP：cad-engine 已登记，记得点 Trust 启用
   - ODA：已安装，DWG 图纸可直接用
需要我现在启动服务并给你前端链接吗？
```

---

## 用户视角的关键提示（提炼）

| 时机 | 用户看到的提示 | 需要用户做的 |
|---|---|---|
| MCP 接入后 | "到连接器管理点 **Trust** 启用 cad-engine" | 点一下 Trust |
| ODA 缺失时 | "DWG 需 ODA，免费免注册，要我装吗？[同意] [先不装]" | 选一个（默认建议同意） |
| ODA 装完 | "ODA 已就绪，面板转绿点" | 无需操作 |
| 全程 | 下载/挂载/拷贝进度 + 最终"部署完成"汇总 | 无需操作 |

**用户总共只需动手两处**：点一次 Trust、对 ODA 装不装选一次。其余 agent 全自动。
