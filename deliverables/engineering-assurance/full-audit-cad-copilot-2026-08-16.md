# cad_copilot 全面工程审查报告

**日期**：2026-08-16
**工作流**：工作流 1（全面代码审查）+ 架构评估 + 测试覆盖评估 + 文档评估
**参与成员**：Cody（代码审查师）/ Archi（系统架构师）/ Tessa（测试专家）/ Docu（技术文档师）
**审查对象**：`C:\Users\vince\Documents\ai projects\cad_copilot`（离线 CAD 特征拾取/预览工具链，Python 3.13 + OCP/build123d + FastMCP + three.js）

---

## 📌 TL;DR（执行摘要）

- 整体结论：**核心设计与代码质量良好（架构无环 DAG、OCP 封装合理、算法选型专业），但存在 2 项严重安全问题（任意代码执行面、HTML 注入 XSS）必须在对外暴露前修复**；另有 1 项会导致数据丢失的高危缺陷（batch_convert 覆盖源文件）与 1 个已失效的 MCP 工具（export_preview 生成的 HTML 无法加载 3D 库）。
- 严重度分布：🔴严重 2 项 / 🟠高 7 项 / 🟡中 11 项 / 🟢低 6 项
- 测试与文档：无正式单元/集成测试（仅 CI 冒烟矩阵）；README 与代码事实漂移（工具数 8→9、pick_features 已接入仍标注"缺口"），且缺少 MCP 启动/连接文档。
- 阻塞 / 非阻塞：**2 项严重问题（RCE、XSS）为阻塞项，建议先行修复再对外分发 MCP server**；其余为排期改进项。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| 整体评级 | 🔴 不通过（对外分发前需先修复 2 项严重安全问题；代码质量本身为"良"） |
| 阻塞项数量 | 2（🔴 RCE 面治理、🔴 HTML 注入 XSS） |
| 数据丢失风险 | 1 项（batch_convert 同目录同扩展名转换覆盖源文件） |
| 失效功能 | 1 项（MCP export_preview 生成的 HTML 引用了 three.js r148+ 已移除的路径） |
| 关键行动项 | 10 条（P0 × 4 / P1 × 4 / P2 × 2） |
| 建议下一步 | 按 P0 清单修复 → 建立 pytest 骨架（Tessa P0 测试四项）→ README/文档对齐 → 清理调试产物 |

---

## 🔍 审查发现（按严重度排序，含跨成员去重合并）

### 🔴 严重（2）

| # | 严重度 | 类别 | 文件:行 | 问题描述 | 建议修复 | 来源 |
|---|--------|------|---------|---------|---------|------|
| S1 | 🔴严重 | 安全 | cad_build.py:137-138；cad_mcp_server.py:31-210 | `run_build123d_script` 对 agent 传入脚本直接 `exec()`，且 MCP 全部工具无路径沙箱/白名单 → **拥有该 server 的 agent 即拥有宿主机完全 RCE 与任意文件读写能力**（脚本可 `os.remove`/读任意文件） | build123d_model 增加显式高危确认或禁用开关；降权子进程 + 目录白名单；至少在文档中声明"等于本地代码执行" | Cody / Archi |
| S2 | 🔴严重 | 安全 | feature_picker.py:631-634；make_preview.py:144-150；feature_locator.py:811,828 | 文件名 `base`/`src_name` 未转义直接拼入 HTML title/`<b>`；`__FEATS__` 用 `json.dumps(ensure_ascii=False)` 原样嵌入 `<script>`（不转义 `</script>`）→ **打开恶意命名 CAD 文件（macOS/Linux 文件名可含 `<script>`）即触发 XSS** | 统一 `html.escape()` 处理文件名；`</script`/U+2028/U+2029 转义后再嵌入脚本 | Cody / Tessa |

### 🟠 高（7）

| # | 严重度 | 类别 | 文件:行 | 问题描述 | 建议修复 | 来源 |
|---|--------|------|---------|---------|---------|------|
| H1 | 🟠高 | 安全 | cad_mcp_server.py:32-210 | 全部 9 工具无路径校验/规范化：可写任意路径；`batch_convert` 可扫描任意目录，report.json 泄露目录文件清单（目录枚举） | base-dir 白名单 + `os.path.realpath` 前缀校验 | Cody |
| H2 | 🟠高 | 正确性 | cad_mcp_server.py:82 | `batch_convert` 当 `input_dir==output_dir` 且 out_ext 与源同扩展名时 `out==fp`，**直接覆盖源文件（数据丢失）** | realpath 相同则跳过，或强制输出到子目录 | Cody |
| H3 | 🟠高 | 正确性 | cad_core.py:197-199 | `export_preview` 的 `_VIEWER_HTML` 引用 `three@0.160.0/examples/js/*`（r148 起已从 npm 移除）且走 CDN → **生成的 HTML 无法加载 3D 库，且非离线**；该工具仍暴露在 MCP | 删除 export_preview，或改复用 make_preview 的 importmap+base64/vendored 方案；消除双预览生成器并存（legacy vs modern 路径） | Cody / Archi |
| H4 | 🟠高 | 可维护性 | cad_mcp_server.py:24 | 顶层 `import cad_build` 使整个 server 启动硬依赖 build123d（import 即扫描字体）；build123d 缺失/损坏时 9 个工具全部不可用 | 仿照 pick_features 改为在 build123d_model 内 lazy import | Cody |
| H5 | 🟠高 | 正确性 | cad_core.py:33-43 | `_SuppressStdout` 用 `dup2` 重定向**进程级** fd1，无锁 → 并发请求互相覆盖恢复栈，fd 永久错乱、JSON-RPC 流损坏 | 加 threading.Lock 串行化，或改用 `contextlib.redirect_stdout` | Cody |
| H6 | 🟠高 | 安全 | feature_picker.py:594-613 | `_ensure_vendor` 运行时从 CDN 下载三份 JS，**无 SHA 校验/SRI** → CDN 被攻陷或中间人即向 vendor/ 植入恶意 JS 并在浏览器执行；且"离线"工具首次调用会静默联网 | 固定 vendor 文件入库 + 校验和；下载失败显式报错而非静默 | Cody / Archi |
| H7 | 🟠高 | 可维护性 | feature_picker.py → feature_locator.py | feature_picker 调 feature_locator **私有函数**（`_vertices_of/_radial_of/_classify`）并依赖隐式 dict 契约（faces/radii/loc3/composite），无 dataclass/TypedDict，改键即静默破裂 | 抽显式 Feature 数据模型；私有助手转公开或上移公共层 | Archi |

### 🟡 中（11）

| # | 严重度 | 类别 | 文件:行 | 问题描述 | 建议修复 | 来源 |
|---|--------|------|---------|---------|---------|------|
| M1 | 🟡中 | 正确性 | cad_core.py:51-64 | STEP/IGES 未检查 `ReadFile`/`TransferRoots` 返回值，损坏文件返回空 shape → 下游 `box.Get()` 抛不明异常 | 检查状态码，空 shape 时 raise 明确错误 | Cody |
| M2 | 🟡中 | 正确性 | cad_core.py:103 | write_shape STL 用固定 deflection=0.1：微特征(<0.1)不细分、巨模型网格爆炸；与 feature_picker/make_preview 的尺寸相对 deflection 不一致 | 收敛为单一 `mesh_shape()` 实现，统一 `max(min(maxdim/800, 0.5), 1e-5)` 并传参 | Cody / Archi |
| M3 | 🟡中 | 正确性 | feature_picker.py:53-64,120 | `_mesh_bytes` 复用单一 `_frag_tmp.stl`：并发 pick_features 互相覆盖；临时文件永不清理 | `tempfile.NamedTemporaryFile` / 临时目录 + finally 清理 | Cody / Archi |
| M4 | 🟡中 | 性能 | feature_picker.py:122-181 | 每特征独立 mesh+STL+base64 嵌入 HTML：百级特征 → 数十 MB HTML，浏览器 atob/渲染卡死；内存峰值 ≈ 1.33×ΣSTL | 单特征 mesh 合并共享缓存；特征数设上限/分批；考虑二进制 glb | Cody |
| M5 | 🟡中 | 性能 | feature_locator.py:470-504 | identify_threads 两阶段 union-find 均为 O(n²) 逐对比较，复杂曲面（面上万）退化明显 | 按轴向/半径分箱粗筛后再局部精比 | Cody |
| M6 | 🟡中 | 可维护性 | feature_locator.py:419-539 | `identify_threads`（螺纹识别）仅在 `_diag*.py` 调试脚本调用，**未接入 main()/feature_picker.build，属生产死代码** | 接入主线或标注 WIP | Cody |
| M7 | 🟡中 | 正确性 | feature_locator.py:61-65,891 | `_part_bbox` 对空/无顶点模型 `min(xs)` 抛 ValueError；空模型全链路无统一兜底 | 空列表返回 None，调用方给出"模型为空"提示 | Cody |
| M8 | 🟡中 | 可维护性 | feature_locator.py:857 | `setHl` else 分支 `setAttribute('stroke-width', 当前值)` 是 no-op，hover 后描边永久停留在 3.4 | 记录原始 stroke-width 并还原 | Cody |
| M9 | 🟡中 | 正确性 | cad_core.py:234-235；make_preview.py:137-138；feature_picker.py:637 | 输出静默覆盖：转换 `part.step` 会覆盖同目录 `part.stl`；`_拾取.html`/`_preview.html` 同名互相覆盖 | 输出前检查存在性并报错/生成唯一名 | Cody |
| M10 | 🟡中 | 文档 | README.md；cad_mcp_server.py 模块 docstring | **文档漂移**：README 写"8 工具、pick_features 暂未接入"，实际已接入（git log 3e43b72）且共 9 工具（漏列 build123d_model）→ agent 会误判能力 | README 与代码事实对齐；补 MCP 启动/连接文档（mcp.json 示例、工具一览表、mcp_test.py 验证方式） | Docu / Cody / Archi |
| M11 | 🟡中 | 测试 | 全仓库 | 无 tests/ 目录、无 pytest/unittest、无覆盖率门槛；14 个临时诊断脚本（_diag*._verify_*.py 等）散落仓库，有价值断言未沉淀 | 建立 pytest 骨架 + P0 测试四项（见测试覆盖评估）；收敛/清理诊断脚本 | Tessa |

### 🟢 低（6）

| # | 严重度 | 类别 | 文件:行 | 问题描述 | 建议修复 | 来源 |
|---|--------|------|---------|---------|---------|------|
| L1 | 🟢低 | 正确性 | cad_mcp_server.py:100-123,127-155 | create_primitive/edit_geometry 未校验参数为正（dx=-5 / radius=0 直接传 OCP） | 显式参数校验 + 友好错误 | Cody |
| L2 | 🟢低 | 可维护性 | cad_build.py:66-85 | 字体 import-hook 靠字符串 marker 匹配 build123d 源码，上游改一行即静默失效 | 安装时自检，patch 未生效则告警 | Cody |
| L3 | 🟢低 | 可维护性 | 仓库根 | `_diag*.py`、`_mesh_tmp.stl`(3.4MB)、`_loc_run.txt` 等调试产物入库污染 | 清理并补 .gitignore | Cody / Tessa / Archi / Docu |
| L4 | 🟢低 | 可维护性 | feature_locator.py | 新增特征类型需改 6+ 处（collect/group/assign_ids/build_html/typestr/picker），内聚度低 | 特征类型注册表（type→渲染/命名/颜色/命令） | Archi |
| L5 | 🟢低 | 文档 | vendor/ | vendored three.js 无版本标记/来源说明；HTML 产物与 vendor/ 相对位置不可拆未文档化 | vendor 内补 README 说明版本来源 | Docu |
| L6 | 🟢低 | 正确性 | mcp_test.py | E2E 脚本硬编码本机 Windows 绝对路径，不可移植 | 参数化路径；入库 pytest 化 | Archi / Tessa |

---

## 🏗️ 架构影响评估（Archi 产出）

**架构总览**：以 cad_core（OCP 内核）为唯一底座，feature_locator→feature_picker 逐层复用，cad_mcp_server 作薄 MCP 适配层，无环 DAG，依赖方向清晰。

**优点**：① 依赖无环、cad_core 不反向依赖上层；② 格式 I/O 收敛在 read_shape/write_shape 两处分派；③ MCP 层薄封装、pick_features 惰性 import 降低启动成本；④ requirements 精确锁版 + CI 三平台矩阵；⑤ fd 级 stdout 抑制解决了 OCC banner 污染 MCP stdio JSON-RPC 的隐患。

**评级：基本清晰**。优先改进（按 ROI）：① 统一预览生成路径、删 legacy 双实现；② Feature 显式数据模型、停止跨模块调私有函数；③ 收敛 mesh/deflection 单一实现；④ README 与代码事实对齐；⑤ MCP exec 加信任边界。

---

## 🧪 测试覆盖评估（Tessa 产出）

**现状**：仅 CI 冒烟矩阵（3 OS：bootstrap + feature_picker 冒烟 + pick_features 断言）与 selftest*.step/iges 样例输入；**无 tests/ 目录、无 pytest、无覆盖率门槛**；14 个临时诊断脚本散落（全部 gitignored，未沉淀回归价值）。

**高风险未覆盖模块排序**：① feature_locator.py（921 行，分类/聚合/螺纹识别逻辑最复杂）；② feature_picker.py（STL 切片正确性无断言，且 HTML 注入点需单测兜底）；③ cad_mcp_server.py（对外 agent 契约，错误路径零覆盖）；④ make_preview.py（escape 与 deflection 边界）；⑤ cad_core.py（仅间接覆盖）。

**最小可行测试计划（P0 四项，约 1-2 天工作量可提升至"中等"）**：

| # | 优先级 | 测试项 | 方式 | 覆盖风险 |
|---|--------|--------|------|---------|
| 1 | P0 | pytest 骨架 + selftest.step 全链路回归（read→properties→preview） | 集成 | 基本可用性 |
| 2 | P0 | HTML 生成 XSS 转义（含 `<script>`/`"` 文件名与特征名） | 单元 | 注入/脚本逃逸 |
| 3 | P0 | MCP 工具正反用例（缺失文件/非法 ext/空参/非法 op） | 集成 | agent 调用失败 |
| 4 | P0 | feature_locator 分类/聚合黄金断言（基于 selftest_*.step 计数与分组） | 单元 | 几何误分类 |
| 5 | P1 | STL 切片几何正确性（网格非空、三角数>0、体积容差） | 集成 | 空网格/错误切片 |
| 6 | P1 | 中文文件名/路径全链路跨 3 OS | 集成 | UnicodeEncodeError |
| 7 | P1 | 数值边界（极小/极大尺寸、退化几何、deflection 除零） | 单元 | 崩溃/死循环 |
| 8 | P2 | 14 个临时诊断脚本收敛（删除或迁移断言入 tests/） | 治理 | 维护成本 |
| 9 | P2 | selftest.py 入库 pytest 化；核心模块行覆盖率 ≥60% | 冒烟/CI | 回归入口丢失 |

---

## 📝 文档评估（Docu 产出）

**现状**：README + 模块 docstring 质量良好（LICENSE、requirements 锁版、CI 配置均完善），但存在 2 项 P0 缺口。

**P0 缺口**：① README 过期（工具数 8→9、pick_features 接入状态错误）；② 无 MCP server 启动/连接文档（mcp.json 示例、stdio 启动、mcp_test.py 验证均缺失，外部 agent 无法接入）。
**P1**：HTML 预览文件结构与 vendor/ 相对位置无说明；vendored three.js 无版本标记。
**P2**：feature_locator/make_preview CLI 用法未入 README；无 CHANGELOG；comparison.md 归属不清。

**成熟度评级：L2（基本可用、存在关键缺口）**。

---

## ✅ 行动清单（按优先级排序）

| # | 行动 | 负责角色 | 紧急度 | 预期完成 |
|---|------|---------|--------|---------|
| 1 | MCP 全部工具加路径白名单 + build123d_model 高危确认/沙箱化（消除 RCE 面） | 开发 | P0 | 对外分发前 |
| 2 | 统一 HTML 转义：文件名 html.escape + 脚本段 `</script>`/U+2028/U+2029 防护（覆盖 feature_picker/make_preview/feature_locator 三处） | 开发 | P0 | 对外分发前 |
| 3 | 修复 batch_convert 同目录覆盖源文件（realpath 相等则跳过）；下线或改造失效的 export_preview | 开发 | P0 | 1-2 天 |
| 4 | README 与代码事实对齐（9 工具、pick_features 已接入）+ 补 MCP 使用文档章节 | 开发/文档 | P0 | 1 天 |
| 5 | cad_mcp_server 顶层 `import cad_build` 改 lazy import，避免单点拖垮整个 server | 开发 | P1 | 1 天 |
| 6 | 建立 pytest 骨架并实施 P0 测试四项（XSS 转义 / MCP 正反用例 / feature_locator 黄金断言 / 全链路回归） | 开发+测试 | P1 | 1-2 天 |
| 7 | `_frag_tmp.stl` 改 tempfile 隔离清理；`_SuppressStdout` 加锁 | 开发 | P1 | 1 天 |
| 8 | 收敛 mesh/deflection 为 cad_core 单一实现，统一口径 | 开发 | P1 | 1 天 |
| 9 | 清理仓库根调试产物（_diag*/_verify*/_mesh_tmp.stl 等）并补 .gitignore；收敛 14 个诊断脚本 | 治理 | P2 | 0.5 天 |
| 10 | 特征类型注册表、identify_threads 接入或标注 WIP、vendor 补版本说明、CHANGELOG | 开发/文档 | P2 | 排期 |

---

## ⚠️ 待完善 / 已知局限

- 本报告为**静态代码审查**，未实际运行 OCP/build123d 环境做动态验证（如 XSS payload 真实触发、batch_convert 覆盖复现）；建议修复后补充动态回归。
- CI 冒烟通过不代表修复后行为正确，P0 测试四项落地前，回归保障仍薄弱。
- `batch_convert` 覆盖源文件与 `export_preview` 失效两项，建议修复前先备份现网/示例 STEP 文件。
- 仓库无 git 提交历史审查（团队未授权审查 git log 全量），文档漂移证据基于成员读码与 README 比对。

---

## 📚 数据来源 & 成员产出索引

- **Cody（代码审查师）原始产出**：20 项发现（2🔴/6🟠/8🟡/4🟢），含逐文件行号定位；整体代码质量评级"良"；结论 Request Changes。
- **Archi（架构师）原始产出**：架构总览 DAG 图 + 8 项风险（1🟠高 exec RCE/4🟡/3🟢）；评级"基本清晰"。
- **Tessa（测试专家）原始产出**：测试资产盘点 7 项 + 高风险模块排序 + 9 项最小测试计划；成熟度评级"基础（Basic）"。
- **Docu（技术文档师）原始产出**：文档资产盘点 9 项 + P0/P1/P2 缺口清单 + README 新增大纲；成熟度评级"L2"。

---

> 本报告由工程保障团队 AI 协作生成（Cody / Archi / Tessa / Docu 独立分析，主理人汇编），关键决策请由人类工程负责人复核。
