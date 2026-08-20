# Changelog

所有重要变更记录于此文件。格式参考 [Keep a Changelog](https://keepachangelog.com/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 新增 (Added)（文件输入交互：上传 / 拖放 / 最近使用）
- **`POST /api/upload`（显式授权输入通道）**：原始请求体流式落盘
  `workspace/uploads/`，**内容寻址**（流式 SHA-256 → `uploads/<hash>/
  <文件名>`，与 parse 缓存同构）——同一文件重复上传返回**同一路径**
  （`deduplicated: true`，不新增目录，前端最近使用按路径去重自然
  生效）；不同内容落不同 hash 目录（ODA 按整目录转换，DWG 不混放）；
  文件名取 `?name=` 参数，剥目录 + 净化 Windows 禁用字符；扩展名
  白名单（STEP/DXF/DWG）+ 1 GiB 上限（流式检查，超限清理临时文件）；
  uploads 目录自动注入 `allowed_dirs`，返回路径可直接喂给现有 parse /
  drawing import 端点。安全语义：用户亲手把文件交给服务 = 显式授权，
  token 仍守门。
- **前端上传交互**：加载表单"浏览文件…"按钮（原生文件选择器）；全窗口
  拖放（dragenter 计数防抖 + 虚线覆盖层提示）；上传后按扩展名自动路由
  ——`.step/.stp` 进装配加载，`.dxf/.dwg` 打开图纸对照并导入。
- **导入 UI 重构（路径手输移除）**：上传 / 拖放 / 最近使用成为唯一输入
  通道后，移除路径输入框、"加载"按钮与"可访问目录"常驻提示（白名单
  403 兜底文案保留）；"打开文件…"升级为主按钮；图纸对照弹窗补"浏览
  文件…"按钮（与主窗口共用一个文件选择器，按扩展名路由）。
- **修复：上传 DWG/DXF 后侧栏状态停留在"上传中"**——图纸导入原先只
  更新弹窗内 msg，现在 `importDrawingFile` 同时更新侧栏主状态栏（成功
  摘要 / 失败原因），装配加载同样收敛为直调函数 `loadAssembly`。
- **`?load=` URL 参数（agent 驱动预览入口）**：`/app?token=...&load=<
  encodeURIComponent(路径)>` 一次性消费——前端按扩展名自动路由装配/
  图纸，路径仍受服务端 safe_input_path 权威校验。agent 对话流（"预览
  这个 STEP"）由此闭环：shell 启动服务 → 构造可点击链接 → 用户浏览器
  直接渲染。顺带修复 URL 参数清理丢 `?` 的潜在 bug（多参数时拼成
  `/appload=x`）。
- **前端最近使用**：localStorage 记录最近 8 个成功加载的文件（装配/
  图纸徽标区分，长路径 rtl 截断保留文件名可见），点击一键重载（内容
  寻址缓存命中时秒开），可清空。
- **测试（+5）**：上传鉴权 / 字节级落盘往返 + 返回路径通过 parse 端点
  路径围栏 / 非法扩展名 400 / 文件名穿越净化。

### 修复 (Fixed)（ODA 探测与调用）
- **探测漏检新版本**：`probe_oda_converter` 从硬编码 23/24/25 三个精确
  路径改为 glob 任意版本目录（`ODAFileConverter*\ODAFileConverter.exe`，
  多版本取最新），补 `C:\Program Files (x86)\ODA`，新增 `CAD_ODA_EXE`
  环境变量覆盖（与 FreeCAD/Blender 探测约定一致）。
- **DWG 转换必失败**：ODA CLI 参数顺序传错——filter 位传了 `"0"`，
  匹配不到任何文件并弹窗 "no matched files in input folder"。修正为
  `<src> <out> <ver> <type> <recurse> <audit> <filter>` 即
  `... "ACAD2018" "DXF" "0" "0" "*.DWG"`，并加回归测试钉死参数顺序。

### 新增 (Added)（路径白名单 UI 引导）
- **`GET /api/config`**：返回 `allowed_dirs`（token 守门），供前端展示
  可访问目录边界。
- **前端三层引导**：加载表单下常驻"可访问目录"提示（悬停列出全部）；
  提交前即时校验白名单外的路径（不发请求直接提示）；服务端 403 的
  `path outside allowed dirs` 映射为可操作文案（移入目录 / 设置
  `CAD_SERVICE_ALLOWED_DIRS`）。图纸导入入口同样接入。

### 新增 (Added)（R5 任务协议 + 前端插件状态 UI）
- **`cad_jobs.py` JobManager（R5 长任务异步）**：内存任务注册表——
  `submit(kind, fn)` 立即返回 job id，工作线程执行；`ctx.report(phase,
  percent, detail)` 流式进度（percent=None 表示不确定进度）；`ctx.
  should_cancel()` + `ctx.on_terminate(hook)` 协作取消（hook 兜底：卡死
  子进程也能被 terminate）；生命周期 queued → running → done | error |
  cancelled（取消与自然完成的竞态以用户取消为准）；完成历史封顶保留
  最新 50 条（GC）。
- **插件子进程 Popen 化（协作取消 + 真实进度）**：`cad_fea._invoke` /
  `cad_render._invoke` 从 `subprocess.run` 改为 Popen + 0.2s 轮询——
  取消 → terminate（5s 宽限后 kill）；FEA 轮询时**读取内层脚本每个
  阶段 flush 到 result.json 的进度**并按 interpreter/geometry/faces/
  setup/mesh/solve/post 映射为 10–95% 真实进度条；stdout/stderr 走
  DEVNULL（CalculiX 输出不会撑爆管道死锁）。
- **服务层 R5 端点**：`POST /api/fea/static` 与 `POST /api/render` 增加
  `"async": true`（202 + job_id，同步路径保持兼容）；`GET /api/jobs/{id}`
  任务快照（状态/进度/结果/错误）；`GET /api/jobs` 列表；`POST
  /api/jobs/{jid}/cancel` 幂等取消；同步路径补 `cancelled → 409` 映射。
- **前端插件状态面板（D5/D7 探测可视化）**：侧栏底部常驻"插件状态"
  （ODA / FEA / Blender 绿点=可用、灰点=未安装，悬停显示路径或安装
  提示，↻ 重新探测）；工具栏新增"力学""渲染"按钮（插件不可用时前置
  拦截并显示 hint）。
- **前端任务卡片（进度 + 取消 + 结果）**：右下角悬浮卡片——阶段名
  （中文映射）+ 百分比/不确定动画进度条 + detail + 取消按钮；FEA 完成
  显示最大位移 / 最大 von Mises / 网格规模；渲染完成弹出结果窗口
  （PNG 走同源静态服务）；轮询 800ms，单任务追踪。
- **测试（+18，全套 187 绿）**：JobManager 生命周期/进度/协作取消/
  终止钩子/取消竞态/GC；真实子进程取消与阶段进度中继（sys.executable
  模拟 FreeCAD/Blender）；异步端点契约（202/job 轮询到 done、协作取消
  到 cancelled、错误捕获、鉴权与 404）。

### 新增 (Added)（Phase D 插件框架：FEA / Blender 渲染，代码先行不装依赖）
- **`cad_fea.py` FEA 插件框架（D6/D7）**：CalculiX 静力学单场景（轴向固定
  底面 + 顶面受压），FreeCAD **headless 子进程隔离**（主进程永不 import
  FreeCAD，GUI 二进制自动加 `--console`）；探测链 env（`CAD_FREECAD_EXE` /
  `CAD_CCX_EXE`）→ PATH → 常见安装路径（ccx 优先找 FreeCAD 同目录捆绑版）；
  缺依赖抛结构化 `FEAError`（missing/timeout/failure 三类）；生成脚本按
  interpreter/geometry/faces/setup/mesh/solve/post **七阶段汇报进度**到
  result.json；spec 规范化（力学参数 + 网格尺寸 + 超时钳制 [30,3600]s）；
  结果缓存键 = STEP 内容 + 规范化 spec 的 sha256（R8）。
- **`cad_render.py` Blender 渲染插件框架（D7/R9）**：装配体静止帧渲染——
  按模板导入 glTF（导入一次 + linked 复制实例化），应用 manifest 3×4 世界
  矩阵（与前端 scene.js 同约定），**glTF Y-up 修正**可开关（OCCT 写的是
  CAD Z-up 坐标，Blender 导入器会施加 Y-up→Z-up 旋转需抵消）；自动相机
  取景（方位角/仰角/距离系数）+ 太阳光 + 中性环境光；引擎 cycles(CPU
  headless 安全)/workbench/eevee 带版本 ID 回退；Blender **仅作外部依赖
  调用**（`--background --python`，不捆绑二进制规避 GPL 传染）；
  渲染缓存键 = glTF 内容 + 实例矩阵 + spec 的 sha256（R8）。
- **服务层插件端点（D5/D7 探测 + 优雅降级统一入口）**：`GET /api/plugins`
  汇报 ODA / FreeCAD+ccx / Blender 探测结果（缺什么、装哪、其余功能不受
  影响）；`POST /api/fea/static`（版本链解析模板 STEP → 求解，503 缺插件 /
  504 超时 / 400 校验）；`POST /api/render`（版本链解析 gltf → 建渲染条目
  → 出图，`png_url` 回传）；`/fea/` `/render/` 静态服务（路径逃逸防护）。
  长任务 R5 job/progress 协议仍为后续增量（当前同步 + 钳制超时）。
- **测试（57 个新增，全套 169 绿）**：两插件的外层框架全量覆盖——探测/env
  覆盖、spec 规范化与拒绝、缓存键、生成脚本 compile + 载荷嵌入（含引号
  路径健壮性）、命令构造（console/gui 二进制）、编排/缓存命中跳过子进程/
  force 重算/超时映射/缺产物失败；服务端点契约（鉴权、503/504/404/400
  映射、真实 manifest 条目构建、PNG 静态服务、路径穿越拒绝）。内层
  FreeCAD/bpy 脚本待装依赖后首轮验证（阶段标记 + 部分结果已就绪）。

### 新增 (Added)（Phase C+/D：定点编辑 / DFM 体检 / DXF 校准 / evals runner）
- **定点特征编辑（R1 落地）**：`apply_feature_edit`（hole_resize / boss_remove——
  从特征元数据构造定向布尔，"点哪个特征改哪个特征"）；`match_features`
  **指纹匹配**（硬键=type+轴+位置容差，半径仅评分——扩径正是要追踪的变化）；
  edit API 支持 `feature_id`，语义 changelog（`孔 #1.2: R1.0 -> R1.6`）；
  编辑后特征缓存重导出并**保持 id 稳定**；前端特征面板孔特征带「扩径」。
- **feature_locator 轴心修正（R1 测试期发现）**：回转面特征 center 原用拓扑
  顶点平均，圆柱面顶点只在 seam 上导致中心偏移 ~2mm → 定点编辑偏心切。
  改用解析轴位置（`Axis().Location()` + 轴向中点）；球面圆角现与同轴圆柱
  正确聚合为复合特征（分组键本不含 stype）。
- **一键体检（模块七）**：`dfm_audit_features` 确定性规则（小孔 R<0.5 /
  深孔 L/D>10 / 平行孔薄壁提示）+ `audit_assembly`（干涉 + DFM 合并报告）；
  `GET /api/assembly/audit` + MCP 第 11 个工具 `audit_assembly`；前端
  「体检」按钮 + 报告弹层。
- **图纸导入与语义校准（D5/模块六）**：`cad_drawing.py`——DXF 直读
  （ezdxf 显式入 requirements）/ DWG 经 ODA（常见路径探测 + 缺失时明确
  降级提示）；**语义提取**（螺纹 M10x1.5 / 直径 Ø8 / 公差 H7/g6 +
  TEXT/MTEXT/DIMENSION）；轻依赖 SVG 渲染（LINE/CIRCLE/ARC/LWPOLYLINE/TEXT，
  实体数上限防病态文件，零 PIL）；`POST /api/drawing/import` + `/drawings/`
  静态 + 前端「图纸」弹层（SVG 对照 + 语义列表）。
- **evals runner（D9 断言层）**：`evals/run_evals.py`——黄金轨迹本身是
  确定性工具序列，断言层**先于 LLM 全自动化**（回放轨迹 + 几何断言）；
  新增 2 条 team 真实指令（E003 钻孔体积断言 / E004 干涉拒绝断言），
  runner ALL PASS；LLM 对比层（实际轨迹 vs 黄金）后续接入。

### 新增 (Added)（Phase C：AI 修改闭环 + 版本管理）
- **`cad_versions.py` 增量版本仓库（D10）**：v0 基线 + v1..vN 链式增量（每版本只存
  被改模板的 step/gltf）；**原子提交**（临时目录 + rename，R6）+ manifest 原子写
  + 启动清理 `.tmp_*`；**回滚 = 指针切换**，历史版本文件永不重写；解析链按版本序
  取最新模板文件，否则回退基线 `parts/tN.step`。
- **干涉守门（D8）**：`cad_assembly.check_interference()`——世界矩阵展平实例
  （节点 matrix 已是世界矩阵，不重复乘父链）→ bbox 预筛 → BRepAlgoAPI_Common
  逐对布尔，报告穿透体积 mm³；编辑场景只查被改模板实例 vs 其余 + 实例间。
- **服务层写路径**：`POST /api/assembly/edit`（编辑 → 守门 → 原子提交，409 结构化
  拒绝含零件对/体积/保持版本，R15）、`GET /api/versions`、`POST /api/versions/checkout`、
  `/versions/` 静态路由；parse 缓存命中校验 schema_version（R7，旧 schema 自动重建）。
- **MCP 第 10 个工具 `check_interference`**：全实例对干涉审计（agent 可调的
  确定性守门）；`build_cache` 新增 `parts/tN.step` B-rep 导出（编辑几何源），
  SCHEMA_VERSION 1→2。
- **前端**：编辑面板（钻孔/倒角/圆角/缩放 + 参数表单，干涉拒绝红色结构化消息）
  + 版本面板（v0..vN 列表、当前标记、一键切换/回滚）+ 版本视图 manifest 切换
  （模板 gltf 绝对路径指向 `/versions/`）。
- **修复（测试期发现）**：`_STDOUT_LOCK` 改 RLock——service 层持锁调
  build_cache→write_shape 时 `_SuppressStdout` 重入自死锁；`cad_service` 缺
  `import cad_core`；`_world_instances` 对已是世界矩阵的节点矩阵重复乘父链
  （螺栓 bbox 33→63 双重累积）。
- **E2E（真实浏览器）验证通过**：v1 提交（螺栓钻孔，三角形 176→304 真实变化）；
  scale×2.5 触发 2 处干涉拒绝（94.248 mm³×2，版本不前移）；v0/v1 往返切换
  几何正确还原。
- **测试**：新增 `tests/test_cad_edit_flow.py`（14 用例：版本链/回滚指针/临时清理/
  干涉检出与放行/编辑流/坏参数/未知 id/schema 失效重建/定点扩径/指纹匹配/
  DFM 规则/audit 端点）与 `tests/test_cad_drawing.py`（5 用例：语义提取/
  SVG 渲染/幂等缓存/坏输入/ODA 降级/实体上限），全量 **112/112**；evals runner
  2 条真实指令 ALL PASS。

### 新增 (Added)（Phase B）
- **多层级爆炸图（ADR-0002 D3 / 模块二）**：`cad_assembly.parse_assembly` 为每个
  非根节点计算**相对 explode 向量**（子树质心 − 父质心方向，同心兜底沿父包围盒
  最长轴交替展开；幅值 clamp 到 [0.3, 0.8]×父尺寸）；前端滑条驱动，向量沿祖先
  链累积实现分层爆炸（父动子随 + 子自身再展开）。
- **特征拾取 API 化（模块四前置）**：`feature_picker.collect_feature_solids()`
  公共 API（build() 重构复用，无跨模块私有调用）；`build_cache` 每模板导出
  `features/tN.json`（分类元数据：孔/凸台/平面/圆角…含半径/轴向/长度）+
  `features/tN.gltf`（每特征一个命名节点）；前端特征面板点条目 → 3D 橙色高亮
  overlay（跟随实例矩阵，含爆炸偏移），模板 glTF 按 promise 缓存（失败可重试）。
- **视图操作集补全（copilot-vision 模块二）**：隔离（只显选中子树）、X 光
  （半透明鬼影）、Z 轴剖切面（clipping plane + 滑条）、临时拖拽移动
  （TransformControls，选装配体整组移动）、复位移动、相机书签（localStorage）、
  一键复位视图。
- **E2E 验证修复 3 个 bug**：默认视野误用模板局部包围盒（实例世界矩阵修正）；
  隔离只设 keep 未设其余隐藏；TransformControls attach 目标未入场景图。
  另修特征 glTF 重复请求竞态 + 窄视口 fit 距离按纵横比自适应。

### 新增 (Added)（Phase A）
- **`frontend/`（Phase A 前端 SPA 骨架，ADR-0002 D3 / 模块二）**：Vite + three.js
  （0.160.0，与 vendor 一致）+ 原生 JS。装配 STEP → Template+Matrix 实例化渲染
  （每唯一零件一份 glTF 几何共享 InstancedMesh，实例矩阵取 manifest 世界 4×4）；
  装配树 UI 按「选择层级交互原则」骨架实现（树↔视口双向选择联动、按节点显隐、
  子装配折叠）；`dist/` 构建产物**随仓库提交**（离线运行零 Node 依赖，Node 18+
  仅前端开发需要）。cad_service 新增 `/app` 同源静态服务（防穿越 + SPA fallback +
  跨平台 MIME 显式映射）。
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
- **`tests/test_cad_assembly.py`（18 用例）+ `tests/test_cad_service.py`（19 用例）**：
  manifest 结构/矩阵/模板去重/R2 平铺兜底/缓存布局/MCP 契约（含路径逃逸拒绝）；
  Phase B 增：爆炸向量存在性与方向语义/平铺无爆炸/features 元数据与命名 glTF
  节点一一对应/分类标签。服务层 token 鉴权/SHA 键控幂等缓存/防穿越静态服务/
  WS 协议/`/app` SPA 服务。全量 **92/92 通过**。

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

工程保障团队对 `cad_copilot` 的全链路审查（Phase 0–4）修复收口基线。

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
