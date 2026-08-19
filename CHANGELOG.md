# Changelog

所有重要变更记录于此文件。格式参考 [Keep a Changelog](https://keepachangelog.com/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
