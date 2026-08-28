# 文档过时项审计 — cad_copilot

- **审计日期**：2026-08-28
- **基线提交**：`b99f0dc`（main，与 origin/main 一致）
- **盘点范围**：`README.md`、`AGENTS.md`、`CHANGELOG.md`、`docs/**`、`.github/workflows/ci.yml`、
  `.gitignore`，逐条对照 `cad_service.py` / `cad_mcp_server.py` / `cad_service_ctl.sh` /
  `frontend/vite.config.js` / `pytest` 实际收集结果
- **结论**：共发现 **19 项**待处理，其中 **A 类 5 项会让 agent 直接跑偏**，建议优先修。

---

## 一句话结论

文档主体质量不差，但**「双 token（guest/dev）方案」上线后 README 一直没跟上**，
导致 README 与 AGENTS.md 对同一件事给了两套互相矛盾的指令；另外 CHANGELOG
的「未发布」段落后于最近 4 个提交，以及两处指向已退役/被 gitignore 脚本的死指令。

---

## A 类：会让 agent 跑偏（建议优先）

| # | 位置 | 现状 | 代码事实 | 建议 |
|---|---|---|---|---|
| A1 | `README.md:18, 26-28, 92, 97, 180, 331` | 全文 URL / curl 示例写死 dev token `cad-local-dev-2026`，**全文 0 处提及 guest token** | `cad_service.py:63-64` 定义 `GUEST_TOKEN=cad-guest-2026` / `DEV_TOKEN=cad-local-dev-2026`；`cad_service_ctl.sh:22-25` 默认 URL 与探活**都用 guest** | README 的 agent 须知 / 固定地址 / 快速开始 / 本地服务 / 图纸对照五处统一改为 guest，dev 标注为「仅开发显式切换」 |
| A2 | `README.md:11-37`（agent 须知整节） | 只有「探活 / 启动 / 打开 / 固定地址」四步，**没有 mode 判定** | `/api/config` 返回 `"mode": "guest"\|"dev"`（`cad_service.py:202, 345-354`） | README 补一条：先按自己用的 token 判 mode，再决定口径（与 `AGENTS.md:38-70` 对齐） |
| A3 | `README.md:227-228` | 环境变量只列 `CAD_SERVICE_TOKEN` / `ALLOWED_DIRS` / `WORKSPACE` / `HOST` / `PORT` / `FRONTEND_DIR` | 还有 `CAD_SERVICE_GUEST_TOKEN`（`cad_service.py:197`）未记录 | 补进环境变量清单 |
| A4 | `README.md:414-417`（冒烟测试） | 写「`python selftest.py` — OCP 工具链端到端」 | `selftest.py` 被 `.gitignore:36` 排除，**仓库里不存在**；CI `ci.yml:30-32` 已注释「legacy selftest retired，由 `tests/test_selftest_e2e.py` 覆盖」 | 删掉该命令，改为指向 pytest 套件的 e2e 用例 |
| A5 | `README.md:342-348`（「验证连接（mcp_test.py）」整节） | 一整节教人跑 `mcp_test.py`，并注明「硬编码本机绝对路径」 | `mcp_test.py` 同样被 `.gitignore:37` 排除、**不存在** | 整节删除，或替换为 `fastmcp.Client("cad_mcp_server.py").list_tools()` 这类可复现的验证方式 |

---

## B 类：事实性数字 / 版本过时

| # | 位置 | 现状 | 事实 | 建议 |
|---|---|---|---|---|
| B1 | `README.md:3` | 版本写 **v0.1.0** | `CHANGELOG.md:673` 已有 `[v0.1.1] — 2026-08-19`；`git tag` 只有 v0.1.0（CHANGELOG 自注「未重打 tag」） | README 标 `v0.1.1 (+ 未发布变更)`，或补打 tag 后统一 |
| B2 | `README.md:393` | 「tests/ … 当前 **192 个用例全绿**」 | 实测 `pytest --collect-only` = **214 tests collected** | 更新数字（或直接去掉数字，避免每次漂移） |
| B3 | `docs/deploy_oda_simulation.md:32` | 「点 Trust 后我才能调用那 **17 个** CAD 工具」 | 工具总数 **18**（17 启用 + `build123d_model` 默认禁用） | 改为「18 个工具（其中 1 个默认禁用）」，与 `README.md:104` 口径统一 |
| B4 | `docs/deploy_oda_simulation.md:82` | 收尾提示「Web 服务：`python cad_service.py`」 | 与 `README.md:32` 的**禁止项**直接冲突，且缺 token | 改为 `bash cad_service_ctl.sh start`（后台方式）+ 8764 带 token 链接 |
| B5 | `docs/architecture/copilot-vision.md:7` | 「以 ADR-0002（D1–D10 + 风险登记 **R1–R12**）为准」 | ADR-0002 现为 **R1–R17**（`0002…md:65-66`，R13–R17 为运行时核对新增） | 摘要里的 R1–R12 改成 R1–R17 |

---

## C 类：代码有、文档没写（遗漏）

| # | 位置 | 遗漏内容 | 证据 | 建议 |
|---|---|---|---|---|
| C1 | `README.md:184-218`（端点表） | **缺 `DELETE /api/sessions`**（隐藏会话 /「清空记录」） | `cad_service.py:2036`；功能已在 CHANGELOG「清空记录」段记录 | 端点表补一行 |
| C2 | `README.md:213-216`（静态路由） | **缺 `GET /fea/{key}/...`、`GET /render/{key}/...`** | `cad_service.py:2048-2049`；`vite.config.js` 的 proxy 也代了这两个前缀 | 端点表补两行 |
| C3 | `README.md:379-399`（目录结构） | 缺 `scripts/check-dist-sync.sh`、`.githooks/pre-commit`、`build_viewer_cache.py`、`start_cad_service.command`（后两项目前**只** AGENTS.md 提过，README 0 次） | `scripts/`、`build_viewer_cache.py`、`start_cad_service.command` 均存在 | 目录结构补这 4 项，尤其是 dist 同步校验的三个组件 |
| C4 | `CHANGELOG.md:6`（「未发布」段） | **缺最近 4 个提交**：`b99f0dc` 结构树点选零件高亮并同步编辑区目标、`b4e77f7`/`0dc2b97`/`708bf49` dist 同步校验与 CI 修复、`e2eded9` 双 token 方案 B | `grep "guest\|双 token" CHANGELOG.md` → **0 命中**（CHANGELOG 完全没有 guest 的记录） | 补 3 条：① 双 token 入口 ② dist 同步校验（脚本 + hook + CI job）③ 结构树点选高亮 |

---

## D 类：文档内部互相矛盾

| # | 冲突双方 | 说明 | 建议 |
|---|---|---|---|
| D1 | `CHANGELOG.md:8-10` + `README.md:191,197,289,316` 「干涉仅提醒，不拦保存」 **vs** `ADR-0002:286`「每次版本提交的**守门检查**」、`:361` R15「守门失败返回结构化错误」 | 干涉守门语义已**反转**（`fb0d0b0`），但 ADR-0002 仍是反转前的描述，同一机制两套说法 | 在 ADR-0002 的 D10 / R15 处加「⚠ 已于 2026-08-XX 修订：干涉降级为提醒，不拦保存；守门语义保留给其它写失败（如原子回滚）」，不要回改 CHANGELOG |
| D2 | `README.md:55`「17 个工具」 **vs** `:104/:141/:385`「18 个工具」 | 同一文件两种口径（虽可解释为「17 可用 + 1 禁用」） | 统一成「18 个工具（`build123d_model` 默认禁用，实际可调 17 个）」 |

---

## E 类：仓库卫生（违反自建规则）

| # | 位置 | 问题 | 影响 | 建议 |
|---|---|---|---|---|
| E1 | `frontend/dist/cmp.html` | **未跟踪残留文件**（`git status` 显示 `??`），而 `vite.config.js` 只有 index/edit/drawing/report 四个入口，不产出它 | 违反 `AGENTS.md:72`「dist 必须由 src 经 `npm run build` 重建而来」的硬性规则；CI `frontend-dist` job 跑 `check-dist-sync.sh --strict` 会因多出未跟踪文件变红 | 确认无用后删除该文件 |
| E2 | `.gitignore:36-37` + `README.md:342-417` | `selftest.py` / `mcp_test.py` 已被排除出仓库，README 却仍以「可运行入口」引用 | 干净 checkout 后照文档操作必然 `No such file` | 与 A4/A5 一并处理：删文档引用（保留脚本的 gitignore） |

---

## 处理记录（2026-08-29 执行）

执行前先 `git fetch` 核对同步：本地 `b99f0dc` **落后 origin/main 3 个提交**，
已 `git pull --ff-only` 到 `5e18695` 后**重新核对**，结论 19 项全部成立
（远端只补了 CHANGELOG 一条 `cdda27f` 修复记录，README 的 token 问题一行未动），
且 C4 另需补记 `299a6f6`。

| 项 | 状态 | 处理方式 |
|---|---|---|
| A1 | ✅ 已修 | README 新增「双 token 入口」小节；agent 须知 / 快速开始 / 本地服务 / 图纸对照 / agent 预览入口共 6 处 URL 改 guest；默认落地页按 `299a6f6` 改为 `index.html` |
| A2 | ✅ 已修 | 补「按 token 判定 mode（权威，不看措辞）」+ `/api/config` 的 `mode` 字段读法 |
| A3 | ✅ 已修 | 环境变量补 `CAD_SERVICE_GUEST_TOKEN`，并注明双 token 覆盖关系 |
| A4 | ✅ 已修 | 冒烟改为与 CI 同一条 `pick_features` 断言，并注明 `selftest.py` 已退役、覆盖并入 `tests/test_selftest_e2e.py` |
| A5 | ✅ 已修 | 「验证连接（mcp_test.py）」整节替换为 `fastmcp.Client(...).list_tools()` 验证片段 |
| B1–B5 | ✅ 已修 | 版本 v0.1.1（注明 tag 未重打）；192→214 用例；部署模拟文档 17→18 工具、禁直跑 `cad_service.py`；vision 的 R1–R12→R1–R17 |
| C1–C3 | ✅ 已修 | 端点表补 `DELETE /api/sessions`、`/fea/{key}/...`、`/render/{key}/...`；目录结构补 `cad_service_ctl.sh` / `build_viewer_cache.py` / `start_cad_service.command` / `scripts/check-dist-sync.sh` / `.githooks/pre-commit` |
| C4 | ✅ 已修 | CHANGELOG 补 4 段：双 token 方案 B、dist 同步校验三件套、结构树点选高亮、默认落地页改首页 |
| D1 | ✅ 已修 | ADR-0002 第 7 条加干涉守门修订注记，R15 加「干涉不属此守门」说明（**未回改 CHANGELOG**） |
| D2 | ✅ 已修 | README 与部署模拟文档统一为「18 个工具，其中 1 个默认禁用，实际可调 17 个」 |
| E1 | ✅ 已修 | 删除 `frontend/dist/cmp.html` 未跟踪残留 |

**实测验证**（改完即时核对，确认文档与代码一致）：
```
curl -H "Authorization: Bearer cad-guest-2026"     /api/config -> "mode":"guest"
curl -H "Authorization: Bearer cad-local-dev-2026" /api/config -> "mode":"dev"
/app/index.html?token=cad-guest-2026  -> HTTP 200
/app/drawing.html?token=cad-guest-2026 -> HTTP 200
```

### E1 延伸发现（新，待用户定夺）

删 `dist/cmp.html` 时查明它不是孤立残留，源头在 `frontend/public/`（vite 会原样拷进 dist）：

| 文件 | 大小 | 跟踪状态 | 说明 |
|---|---|---|---|
| `frontend/public/cmp.html` | 644 B | **未跟踪** | DXF 渲染对比调试页，引用下面两个 svg，无人引用它 |
| `frontend/public/ref_zone.svg` | 1.28 MB | 已跟踪 | 官方 ezdxf 渲染基准图 |
| `frontend/public/mine_zone.svg` | 870 KB | 已跟踪 | 本项目渲染对照图 |

两个 svg **与 src 是同步的**（在 public/ 里，build 会重新产出），**不会**让 CI 变红；
但它们合计 **2.1 MB 调试资源随 dist 分发给每个用户**。建议：三者一起从 `public/` 移除
（调试目的已达成），届时 dist 重建即可同步变小。

## 建议执行顺序

1. **A 类（A1–A3）**：README 全面切 guest token + 补 mode 判定 + 补 `CAD_SERVICE_GUEST_TOKEN`——这一步做完，README 与 AGENTS.md 才不构成矛盾指令。
2. **E1**：删 `frontend/dist/cmp.html`，让 CI 的 dist 同步校验恢复绿。
3. **A4/A5 + E2**：清掉两处指向已退役脚本的死指令。
4. **C4**：CHANGELOG 补最近 4 个提交（尤其双 token，这是用户可见的入口语义变化）。
5. **B1–B5 + C1–C3 + D1–D2**：数字、版本、端点表、目录结构、ADR 冲突注记——可一次性批处理。

## 附：本次审计用到的核对命令（可复跑）

```bash
git log --oneline -20 && git status --short
grep -c '^@mcp.tool' cad_mcp_server.py                       # 18
grep -n 'GUEST_TOKEN\|DEV_TOKEN' cad_service.py cad_service_ctl.sh
grep -n '^        Route(' cad_service.py                      # 全量路由对照端点表
venv/bin/python -m pytest tests/ --collect-only -q -o addopts=""   # 214 tests
ls frontend/dist/*.html && grep -n 'input:' frontend/vite.config.js  # 4 入口 vs dist 文件
grep -n 'selftest\|mcp_test' .gitignore .github/workflows/ci.yml
```
