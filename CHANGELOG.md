# Changelog

所有重要变更记录于此文件。格式参考 [Keep a Changelog](https://keepachangelog.com/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 新增 (Added)
- **`cad_service.py`（Phase A 服务层骨架，ADR-0002 D2）**：starlette 本地服务
  （零新增依赖），HTTP + WebSocket 双通道共享 MCP 同一工具库；仅绑 127.0.0.1 +
  token 鉴权 + 防穿越静态缓存服务（延续约束一）；缓存目录按源 SHA-256 前 16 位
  命名——同内容重复导入幂等命中（R8/R17）；几何操作全局串行锁（R4）。
  端点：`/health`、`POST /api/assembly/parse`、`GET /cache/{key}/...`、
  `WS /ws`（ping/parse 协议骨架，任务队列与进度事件为后续增量）。
- **`cad_assembly.py`（Phase A 生产模块，ADR-0002 D3）**：装配体 STEP →
  Template+Matrix manifest——命名装配树（含累积世界 4×4 矩阵）+ 按 ReferredShape
  去重的零件模板表；`build_cache()` 写出前端缓存布局（`tree_structure.json` +
  `gltf_library/`，每唯一模板一份 glTF，天然规避 RWGltf 不去重问题）。
  manifest 携带 `schema_version`（R7）/ `source_sha256`（R8 缓存键）/ `units: mm`
  （R3）；R2 兜底：平铺单实体 → 单零件树（过滤 OCCT 翻译器垃圾名，回退 Part_N），
  多根 STEP → 合成根节点。
- **MCP 新工具 `parse_assembly`（第 9 个）**：暴露上述能力给 agent，
  docstring 即契约；路径走 `CAD_MCP_ALLOWED_DIRS` 白名单。
- **`tests/test_cad_assembly.py`（13 用例）+ `tests/test_cad_service.py`（15 用例）**：
  manifest 结构/矩阵/模板去重/R2 平铺兜底/缓存布局/MCP 契约（含路径逃逸拒绝）；
  服务层 token 鉴权/SHA 键控幂等缓存/防穿越静态服务/WS 协议。全量 **83/83 通过**。

### 工程验证 (Engineering)
- **Phase A 去风险 spike 全部通过**（XCAF + glTF，2026-08-19）：
  - **Spike 1 XCAF 往返 7/7**：STEPCAFControl 写/读 2 层命名装配，
    层级、零件名、颜色、**累积世界 4×4 矩阵**、同规格零件多实例与
    ReferredShape 去重全部成立——R2/D3 的 OCP 能力确认。
  - **Spike 2 glTF 导出 10/10**：RWGltf 节点层级/命名/局部平移正确；
    **发现 RWGltf 不自动去重同模板 mesh**（每实例一份几何），
    JSON 后处理去重方案已验证（同模板实例指向同一 mesh）——Phase A
    实现注记。
- **新增 `tests/_assembly_helpers.py` + `tests/test_xcaf_assembly.py`**：
  spike 结论固化为可回归的黄金断言（9 个用例：树结构/命名/矩阵/去重/
  颜色/glTF 节点与去重后处理），装配 STEP 测试时生成进临时目录
  （不提交二进制样例）。**全量 55/55 通过**。
- **新增 `evals/` 基准集骨架**（ADR-0002 D9）：指令收集模板
  （三层标注：意图/黄金轨迹/几何断言）+ 状态流转（draft → annotated →
  verified）+ 2 条示例条目；真实指令随日常开发积累。

### 文档 (Docs)
- **新增 ADR-0002**《Web-based AI CAD Copilot 系统扩展决策》：D1–D10 十项决策
  （四阶段路线 / 双 transport 分层 / Template+Matrix 解耦 / 2D 对照降级 / DWG 默认开启 /
  FreeCAD 子进程隔离 / 插件化 / AI 确定性边界 / 评测飞轮 / 图模一致性）、
  两项延续约束、以及风险登记 **R1–R17**（R1–R12 为 2026-08-19 全面复审新增：
  含 R1 特征身份稳定性 / 拓扑命名问题、R11 多用户模式已决策为单机单用户、
  R10 数据隐私阶段性决策——现阶段批准云端 LLM，README 增数据隐私备注；
  R13–R17 为运行时流程核对新增：断线重连 / 同机多窗口写独占 /
  写失败结构化呈现 / workspace 生命周期 / 重复导入幂等）。
- **copilot-vision.md 模块二扩充**（Phase B 范围）：新增**视图操作集**
  （隐藏/隔离/X光/临时拖拽/自定义爆炸/剖切面/相机书签，含"视图状态 ≠ 数据状态"
  边界原则与拖拽双含义区分）与**选择层级交互原则**（装配树主锚点 / 视口点选
  消歧升档 / 状态可见性 / 复位分层）。
- **文档结构整理**：ADR 编号化（`comparison.md` → `0001-ocp-vs-freecad-base.md`）；
  根目录《Web-based AI CAD Copilot 系统扩展设想.txt》迁入
  `docs/architecture/copilot-vision.md` 并**首次纳入版本库**（文首附与 ADR-0002 的
  差异摘要，被修订段落加注）；`.workbuddy/` 加入 `.gitignore`。

## [v0.1.1] — 2026-08-19（CI 修复，未重打 tag）

### 修复 (Fixes)
- **vendor 在 Windows CI 误报 tampered**：`.gitattributes` 将 3 个 vendored three.js
  文件标记为 `-binary`，禁止 `core.autocrlf`/`eol` 把 `.js` 转换成 CRLF（否则
  SHA-256 不匹配、离线预览拒绝运行）。已确认仓库内 blob 已为 LF 存储，无需重新入库。
- **`tests/test_feature_picker_stl.py` 在 CI 上 `ModuleNotFoundError: _compare_helpers`（三平台全挂）**：
  **真实根因**——`tests/_compare_helpers.py` 从未被提交进仓库：`.gitignore` 的 `_*`
  规则把它误当作「dev scratch」忽略了（`git check-ignore` 命中 `.gitignore:20:_*`）。
  CI 是全新 clone，文件根本不存在，任何 `sys.path` 注入都无法解决。
  **修复**——`.gitignore` 收窄为 `/_*`（仅忽略根目录下划线前缀的临时文件），
  并正式提交 `tests/_compare_helpers.py`。
  **回退误诊**——此前 `conftest.py` 的 `TESTS_DIR` 注入（`be630ef`）与测试模块顶部的
  `sys.path.insert`（`88ccf3f`）均为误诊产物，已回退为 `2349362` 原状；
  决定性验证：原始代码 + helper 文件存在 + `pytest tests/ -q`（coverage 生效）
  = 46 passed。

## [v0.1.0] — 2026-08-16

工程保障团队对 `cad_tools` 的全链路审查（Phase 0–4）修复收口基线。

### 安全 (Security)
- **RCE 面治理**：`build123d_model` 默认禁用，仅当 `CAD_MCP_ALLOW_BUILD123D=1` 时启用；
  启用后以超时隔离子进程运行任意 build123d 脚本（等于本地代码执行，已在 docstring 明确标注）。
- **XSS 修复**：HTML 生成统一 `html.escape` 转义文件名/特征名，并对 `</script>`、
  U+2028 / U+2029 做额外防护，杜绝恶意命名文件触发脚本注入。
- **路径白名单**：所有 MCP 工具的用户路径被限制在 `CAD_MCP_ALLOWED_DIRS`（默认 `.`），
  越界路径（如 `../`）被拒绝；`batch_convert` 源/目标同路径同扩展名时跳过，避免覆盖源文件。
- **vendor 固定入库 + SHA-256 校验**：three.js 不再运行时从 CDN 下载，缺失/被篡改即报错。

### 正确性 (Correctness)
- 坏文件读取失败返回清晰错误（不再静默空 shape）。
- 输出覆盖保护：同名输出被阻止或加唯一后缀。
- `create_primitive` / `edit_geometry` 正参数显式校验。
- `_SuppressStdout` 加线程锁，避免并发请求污染 JSON-RPC 流。
- build123d 因损坏系统字体导入崩溃的问题由 `cad_build.py` import-hook 兜底跳过。

### 架构 (Architecture)
- 抽出显式 `Feature` 数据模型，feature_picker ↔ feature_locator 停止跨模块调私有函数。
- 收敛 `cad_core.mesh_shape()` 单一实现，统一 deflection 公式。
- 特征类型注册表（type → 渲染/命名/颜色/命令），新增特征类型只改一处。

### 测试 (Tests)
- 建立 `tests/` + `pytest.ini`（含 `--cov`），CI 3 平台矩阵（ubuntu / macOS / windows，Python 3.13）。
- 全量 **46 个用例通过**（XSS 转义 / MCP 正反用例 / feature_locator 黄金断言 / 全链路回归等）。

### 文档 (Docs)
- 对齐 README 与代码事实：8 工具、pick_features 已接入、build123d_model 默认禁用。
- 新增 MCP Server 使用、命令行速查、输出产物与目录约定、开发与测试、故障排查章节。
- 新增 `vendor/README.md`（版本/许可/SHA-256）、本 `CHANGELOG.md`、归档 `docs/decisions/comparison.md`。

### 已知局限（非阻塞）
- `mcp_test.py` 为开发用冒烟脚本，已被 `.gitignore` 忽略且硬编码本机绝对路径（审计 L6），
  未做参数化（价值低），建议以 pytest 套件为准。
