# cad_copilot 修复任务拆分计划

**日期**：2026-08-16
**工作流**：工作流 1（全面代码审查）+ 工作流 5（技术债评估）→ 修复计划编排
**参与成员**：Cody（代码修复任务）/ Archi（架构重构任务）/ Tessa（测试建设任务）/ Docu（文档修复任务），主理人汇编去重
**依据**：`full-audit-cad-copilot-2026-08-16.md`（全面审查报告，26 项发现）

---

## 📌 TL;DR（执行摘要）

- 将 26 项审查发现拆解为 **37 个可执行任务**（去重后），编排为 **5 个阶段**：安全加固 → 正确性修复 → 架构收敛 → 测试建设 → 文档与治理。
- 去重合并 5 组重叠项（A1↔T5、A3↔T10、A4↔T1、A5↔T11、A7↔T6），避免重复排期。
- 总工作量预估：**约 45-50 人时**（单人串行约 6-7 个工作日；3 人并行约 2-3 天）。其中 2 项严重安全问题（exec 沙箱、HTML 转义）为**第一阶段阻塞项，建议先于一切交付**。
- 阶段间依赖：Phase1 的 export_preview 去留（T5）与 build123d_model 开关（T1）定稿后，Phase4 文档 D1/D2 才能收口；TE3 XSS 单测依赖 T2 修复；A6 依赖 A2。

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| 任务总数 | 37（T 系列 16 + A 系列去重后 4 + TE 系列 10 + D 系列 9，含 2 项交叉归属调整） |
| 阶段划分 | 5（Phase 0-4 + 验收） |
| 关键路径 | T1/T2/T3/T4（安全）→ T5（预览路径定稿）→ D1→D2（文档收口） |
| 阻塞项 | S1 exec 沙箱（T1/A4）、S2 HTML 转义（T2） |
| 建议下一步 | 先实施 Phase 0（约 1-1.5 人日），随后并行推进 Phase 1/3 |

---

## 🗂️ 任务总览（按阶段）

### Phase 0 — 安全加固（🔴 阻塞项，先行）

| 任务 | 关联发现 | 文件:行 | 具体改动 | 依赖 | 验证方式 | 工作量 | 来源 |
|------|---------|---------|---------|------|---------|--------|------|
| T1/A4 | S1 RCE | cad_build.py:137-138；cad_mcp_server.py | exec 前校验脚本路径∈白名单；build123d_model 加高危开关（默认关闭/显式确认）+ 执行超时；文档声明"等于本地代码执行" | 无 | 白名单外脚本被拒；开关关时工具返回明确错误 | 2-4h | Cody/Archi |
| T2 | S2 XSS | feature_picker.py:631-634；make_preview.py:144-150；feature_locator.py:811,828 | 统一 html.escape + 额外转义 `</script>`/U+2028/U+2029；抽公共转义函数 | 无 | 含 `</script>` 文件名回归测试，浏览器无注入 | 3h | Cody |
| T3 | H1 路径无校验 | cad_mcp_server.py:32-210 | 新增 base_dir 白名单，所有路径 realpath 后前缀校验；batch_convert 目录枚举同规则 | 无 | 白名单外路径全部被拒 | 3h | Cody |
| T4 | H2 覆盖源文件 | cad_mcp_server.py:82 | batch_convert realpath(input)==realpath(output) 且同 ext 时跳过/强制子目录 | T3 | 同目录同 ext 转换不覆盖原文件 | 1h | Cody |
| T8 | H6 CDN 无校验 | feature_picker.py:594-613 | vendor JS 固定入库（去 CDN 下载），启动校验 SHA-256，失败显式报错 | 无 | 断网可用；篡改文件被拒 | 2h | Cody/Archi(A8) |

### Phase 1 — 正确性修复

| 任务 | 关联发现 | 文件:行 | 具体改动 | 依赖 | 验证方式 | 工作量 | 来源 |
|------|---------|---------|---------|------|---------|--------|------|
| T5/A1 | H3 export_preview 失效 | cad_core.py:197-199 | export_preview 改调 make_preview 现代路径，删除 legacy `_VIEWER_HTML`（**决定 D1 文档定稿**） | Phase 0 完成 | 调用 export_preview 产出有效离线 HTML | 1.5h | Cody/Archi |
| T6/A7 | H4 顶层依赖 | cad_mcp_server.py:24 | 移除顶层 `import cad_build`，build123d_model 内 lazy import；import-hook 显式 activate()/上下文管理器 | 无 | 未装 build123d 时其余工具正常 | 0.5-1h | Cody/Archi |
| T7 | H5 fd 并发 | cad_core.py:33-43 | `_SuppressStdout` 加 module 级 threading.Lock，dup2 进入/退出全程持锁 | 无 | 并发 10 线程导出无 fd 串扰 | 1h | Cody |
| T9 | M1 坏文件静默 | cad_core.py:51-64 | 检查 ReadFile/TransferRoots 返回状态码，空 shape 时 raise 带文件名/原因 | 无 | 坏 STEP 报清晰错误 | 1h | Cody |
| T11/A5 | M3 临时文件 | feature_picker.py:53-64,120 | tempfile.mkstemp 唯一临时文件 + try/finally 清理 | 无 | 多次调用无残留 | 1h | Cody/Archi |
| T15 | M9 输出覆盖 | cad_core.py:234-235；make_preview.py:137-138；feature_picker.py:637 | 输出前检查存在性，已存在则报错/加唯一后缀 | 无 | 同名输出被阻止 | 0.5h | Cody |
| T16a | L1 参数校验 | cad_mcp_server.py:100-155 | create_primitive/edit_geometry 正数参数显式校验 + 友好错误 | 无 | 非法参数被拒 | 1h | Cody |

### Phase 2 — 架构收敛

| 任务 | 关联发现 | 文件:行 | 具体改动 | 依赖 | 验证方式 | 工作量 | 来源 |
|------|---------|---------|---------|------|---------|--------|------|
| A2 | H7 隐式契约 | feature_picker ↔ feature_locator | 抽 `dataclass Feature`，locator 开放公开 API 返回显式模型（替代私有函数 + dict 契约） | 无 | 类型检查通过；行为不变 | 中(0.5-1d) | Archi |
| A3/T10 | M2 deflection 不一 | cad_core.py:103 | `cad_core.mesh_shape()` 单一实现统一 deflection 公式（maxdim/800） | 无 | STL 网格密度一致 | 0.5-1h | Archi/Cody |
| A6 | L4 扩展性低 | feature_locator.py 全链路 | 特征类型注册表（type→渲染/命名/颜色/命令），替代 6+ 处硬编码分支 | A2 | 新增特征类型只改注册表 1 处 | 中(0.5-1d) | Archi |
| T13 | M5 O(n²) | feature_locator.py:470-504 | identify_threads 按坐标分箱粗筛再精比 | 无 | 大模型耗时下降且结果不变 | 2h | Cody |
| T14 | M6/M7/M8 | feature_locator.py:419-539,61-65,891,857 | identify_threads 接入主线或标 WIP；空模型返回 None；setHl 还原原始 stroke-width | T13 | 空模型/高亮/死代码检查通过 | 2h | Cody |

### Phase 3 — 测试建设（可与 Phase 0-2 并行推进）

| 任务 | 具体内容 | 前置依赖 | 验证标准 | 工作量 | 来源 |
|------|---------|---------|---------|--------|------|
| TE1 | pytest 骨架：tests/ + pytest.ini(含cov) + conftest（selftest 样例/路径夹具） | 无 | 收集 0 错误、覆盖率报告可生成 | 0.5d | Tessa |
| TE2 | test_selftest_e2e.py 全链路回归（read→properties→preview） | 无 | 3 OS CI 通过 | 0.5d | Tessa |
| TE3 | test_feature_picker_html.py：XSS 转义（`<script>`、引号/&、特征名注入） | **T2 完成后**（否则测试必失败） | 全部转义用例通过 | 0.5d | Tessa |
| TE4 | test_cad_mcp_server.py：MCP 工具正反用例（缺失文件/非法ext/空参/非法op/正常拾取） | Phase 0 路径白名单定稿后校准错误预期 | 错误路径均符合契约返回 | 0.5d | Tessa |
| TE5 | test_feature_locator.py：基于 selftest_*.step 的孔/螺纹计数与分组黄金断言 | 无 | 计数分组与人工标注一致 | 1d | Tessa |
| TE6 | test_feature_picker_stl.py：切片几何（非空、三角数>0、体积容差） | 无 | 几何断言通过 | 0.5d | Tessa |
| TE7 | test_i18n_filenames.py：中文文件名拾取/预览/输出跨 3 OS | 无 | 3 OS 通过 | 0.5d | Tessa |
| TE8 | test_numeric_edges.py：极小/极大尺寸、退化几何、deflection 边界 | 无 | 边界不崩或明确报错 | 0.5d | Tessa |
| TE9 | 诊断脚本收敛：删除 10 个（结论已固化进 TE2/TE5）+ 迁移 3 个对比脚本入 tests/ + enlarge_small_hole.py 判定处置 | 无 | git 无残留或已登记 | 0.5d | Tessa |
| TE10 | selftest pytest 化 + CI 覆盖率门槛 ≥60% | TE1-TE9 | coverage≥60% 门槛生效 | 1d | Tessa |

### Phase 4 — 文档与治理

| 任务 | 具体内容 | 前置依赖 | 验收标准 | 工作量 | 来源 |
|------|---------|---------|---------|--------|------|
| D1 | README 改 9 工具、补 pick_features 已接入；cad_mcp_server.py docstring 补 build123d_model | **T5 去留 + T1 开关定稿后** | 全文工具数=9、无"暂未接入"表述 | 0.5d | Docu |
| D2 | README 新增"MCP Server 使用"章节：stdio 启动、mcp.json 示例、9 工具一览表、mcp_test.py 验证 | D1 | 新人按文 10 分钟启动并验证连接 | 1d | Docu |
| D3 | 命令速查（feature_picker/feature_locator/make_preview 参数+示例） | 无 | 每条命令含调用示例与输出样例 | 0.5d | Docu |
| D4 | 输出产物与目录约定（HTML 与 vendor/ 相对位置不可拆、断网依赖） | 无 | 含目录树示例与约束 | 0.5d | Docu |
| D5 | vendor/ 内 three.js 版本标记（版本号、来源、许可） | T8 后可同步 | 文件头或同级注释可查版本 | 0.25d | Docu |
| D6 | 开发与测试章节（selftest、CI、新增工具规范：docstring 即 agent 文档） | 无 | 新工具开发流程有书面规范 | 0.5d | Docu |
| D7 | 故障排查章节（OCP 安装失败、Windows 中文编码） | 无 | 覆盖两项已知故障且可复现 | 0.5d | Docu |
| D8 | CHANGELOG.md 基线 + README 标注版本 | 无 | 版本号与 CHANGELOG 首条对应 | 0.5d | Docu |
| D9 | comparison.md 移 docs/decisions 或删除；README 目录结构同步 | TE9 清理后 | 文档目录与实际仓库一致 | 0.25d | Docu |
| T16b | mcp_test.py 路径参数化入库；清理 `_diag*`/`_mesh_tmp.stl`(3.4MB) 等调试产物 + 补 .gitignore | 无 | 仓库根干净 | 1h | Cody |

### Phase 5 — 验收发布

- 全量回归：`pytest` 全绿 + CI 3 OS 矩阵通过（TE10 覆盖率门槛生效）
- 文档一致性复核：README 工具数 / 目录结构 / MCP 章节与代码事实一致（D1-D9 收口）
- 安全复核：白名单绕过尝试、XSS payload 注入尝试均被拦截
- Go/No-Go 决策报告（工作流 4 模板）后再对外分发 MCP server

---

## 🔗 依赖链总览

```
Phase 0: T1 ─┬─ T3 ─→ T4
             ├─ T2 ──────────────→ TE3 (XSS 单测)
             └─ T8 ──────────────→ D5 (vendor 版本标记)
Phase 1: T5 (预览路径定稿) ───────→ D1 ─→ D2 (MCP 文档收口)
         T1 (开关定稿) ──────────→ D1
Phase 2: A2 ─→ A6 (注册表依赖数据模型)
         T13 ─→ T14 (螺纹优化后处置死代码)
Phase 3: TE1-TE9 基本独立 → TE10 (覆盖率门槛收尾)
Phase 4: D3-D9 可并行，D1→D2 串行
```

## ✅ 行动清单（按优先级排序）

| # | 行动 | 负责角色 | 紧急度 | 预期完成 |
|---|------|---------|--------|---------|
| 1 | Phase 0 安全五项：T1 exec 沙箱 + T2 HTML 转义 + T3/T4 路径白名单与防覆盖 + T8 vendor 固定 | Cody（开发） | P0 | 1-1.5 人日 |
| 2 | Phase 3 并行启动：TE1 pytest 骨架 + TE2 全链路回归 + TE5 黄金断言 | Tessa（测试） | P0 | 与 Phase 0 并行 |
| 3 | T5 export_preview 去留定稿（阻塞 D1/D2 文档） | Cody+Archi | P1 | Phase 0 后 0.5 日 |
| 4 | Phase 1 其余正确性修复（T6/T7/T9/T11/T15/T16a） | Cody | P1 | 1 人日 |
| 5 | Phase 2 架构收敛（A2 Feature 模型 → A3 mesh 收敛 → A6 注册表） | Archi | P1 | 1.5-2.5 人日 |
| 6 | Phase 4 文档（D1-D9，D1 待 T5/T1 定稿） | Docu | P2 | 2-3 人日（D3-D9 可提前并行） |
| 7 | 收尾：TE9 诊断脚本收敛 + TE10 覆盖率门槛 + Phase 5 验收 Go/No-Go | 全员 | P2 | 全链路完成后 |

---

## ⚠️ 待完善 / 已知局限

- **工作量口径**：T/A 系列按小时、TE/D 系列按人日估算，系成员经验值，非工时承诺；A2/A6 需实施时细拆。
- **A4 分级方案**：本计划采用"目录白名单 + 超时 + 默认禁用开关"（低成本高收益）；若安全要求更高，可升级"降权子进程"（未排期，需单独评估）。
- **T5 决策待定**：export_preview 是"改调 make_preview"还是"直接下线"，取决于下游是否有依赖该工具的 agent，需用户确认后 D1 才可定稿。
- **TE3/TE4** 明确依赖代码修复完成，若提前编写会出现预期性失败，建议按依赖链排期。
- 本计划未包含 A2/A6 实施时可能引入的回归成本（Feature 模型改动触及 feature_picker 全链路），建议 A2 落地时同步跑 TE5 黄金断言验证行为不变。

---

## 📚 数据来源 & 成员产出索引

- **Cody（代码审查师）**：T 系列 16 项任务清单（T1-T16，含文件:行号、改动、依赖、验证、工作量；分三批：T1-T4 安全/数据丢失 → T5-T8 → T9-T16）
- **Archi（架构师）**：A 系列 8 项重构清单（A1-A8，含推荐方案与选型理由；A5/A7 标注归代码修复线）
- **Tessa（测试专家）**：TE 系列 10 项测试任务（含 tests/ 目录结构建议 8 个文件、14 个诊断脚本逐一处置结论）
- **Docu（技术文档师）**：D 系列 9 项文档任务（D1→D2 串行依赖标注、合计约 4.5 人日）
- **主理人去重合并**：A1↔T5、A3↔T10、A4↔T1、A5↔T11、A7↔T6（共 5 组合并，防重复排期）

---

> 本报告由工程保障团队 AI 协作生成（Cody / Archi / Tessa / Docu 独立产出，主理人汇编），关键决策请由人类工程负责人复核。
