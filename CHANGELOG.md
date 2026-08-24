# Changelog

所有重要变更记录于此文件。格式参考 [Keep a Changelog](https://keepachangelog.com/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 变更 (Changed)（编辑页装配体级编辑：操作域二段化 + 换件身份 + 特征级编辑）
- **操作域二段化**：`[零件编辑]` / `[装配操作]` 两个操作域，目标统一由 3D 点选（`targetInstance`）驱动。
  - **装配操作**（实例级）：新增 **换件 replace**（来源从其它已打开文件选中的零件读取，`/api/selection?all=1` 剔除当前 cache_key；含 `align` + dx/dy/dz 偏移）与 **移动 move**（表单 dx/dy/dz 绝对位移，后写覆盖）。装配域下隐藏「目标模板/目标特征」。
  - **零件编辑 = 定点特征**（去掉"整件"层）：点零件任意位置即命中或就近选中特征（扩孔 / 去凸台），目标=该零件所属类，显示「目标模板 / 目标特征」。
- **换件后新零件身份**：preview 返回 `replaced` 映射（新名 + 来源 cache/模板 + 对齐偏移 `offset`），前端装配树节点**改名 + 「已替换」badge**；被替换模板的**特征数据/overlay 重指向来源 cache**（不拷贝进目标缓存，避免 buffer 撞名）；草稿场景的特征 overlay **叠加对齐平移 `offset`** 贴合新零件，特征拾取用同一矩阵一致。
- **点空白取消选中**：仅清除瞬态选中（目标实例 / 装配高亮 / 特征 overlay / 树选中），保留视图焦点（`focus`/`range`），在子装配/零件视图里取消不跳回整装配。
- 视图范围（整装配 / 子装配 / 零件）与选取/特征/搜索的交互统一。

### 变更 (Changed)（编辑页步骤历史 UI + 搜索 + 布局）
- **步骤卡统一排版**：所有卡片统一为「类型徽标 + 目标 + 详情」结构（换件 `类 <名>`、移动 `件 <名>`、特征 `扩孔/去凸台 + 特征名`），用彩色徽标区分操作类型。
- **连续同位移的 move 自动折叠**为一行（如 `移动 31 件 · Δ(0,15,0)mm`），描述行列出全部节点名；「删除」一键删整组。串行累积的特征步骤不折叠。
- **步骤搜索**：左栏搜索框按模板 id / 模板名 / 零件名查找，结果分「模板 / 零件」组；点击高亮并取景、同步编辑区目标与视图焦点。
- **左栏布局**：搜索 → 添加编辑步骤 →（草稿步骤说明 + 历史）。

### 修复 (Fixed)（编辑页样式与交互）
- 修复 `.sf-row` 的 `display:flex` 覆盖 `hidden` 属性导致行隐藏失效（装配域下「目标模板/目标特征」仍显示）→ 增加 `.sf-row[hidden]{display:none}`。
- 修复取景对小零件过度放大：`_fitCameraTo` 增加最小距离下限（整装配对角线比例，几何未全载入时按零件自身倍数兜底）。

### 修复 (Fixed)（编辑页侧栏超高撑开模型窗口）
- **左右侧栏高度锁在第一屏**：`.edit-ws` 显式 `grid-template-rows: minmax(0,1fr)`
  + `.rail` `min-height:0`，行高不再随侧栏内容（步骤表 / 验证轨道条目多时）自动
  撑大；`.rail-body` 自带 `overflow:auto` 在锁高后生效，超出部分在侧栏内滚动，
  中间的模型视口始终保持在首屏高度内，不再被推向屏外。

### 变更 (Changed)（编辑会话验证轨道：干涉高亮 + 精检结果跨刷新保留 + 手动重置）
- **点击干涉结果高亮对应零件**：`renderVerify` 的每条干涉卡片（精检/粗筛/窄屏
  抽屉）均可点击，`focusInterference` 在双视口用 `AssemblyScene.highlightPair`
  同时高亮该对的两个零件并自动取景；A 用洋红、B 用青，强对比色一眼区分
  是谁撞谁（scene.js 新增 `INTERFERENCE_COLOR_A/B` 常量与 `highlightPair`）。
- **精检结果跨刷新保留**：精确检查结果（含草稿 manifest）存入 `sessionStorage`
  （`cad-verify:<cache_key>`），同标签页 F5 自动恢复；恢复时按步骤表几何哈希
  （`draftStepsHash`，忽略 id/title 展示字段）判定是否过期。
- **步骤改动不再丢结果**：步骤变更触发的自动 AABB 粗筛不再覆盖已存精检列表，
  而是**保留并标记「已过期」**（黄色提示），供用户逐处处理多处干涉时持续对照；
  新增「重置」按钮（右栏 + 抽屉 `resetVerify`）手动丢弃并退回自动粗筛，
  重新「精确检查」以最新几何刷新结果。
- **清理时机**：放弃草稿 / 草稿被远程删除 / 确认落版本时调用 `clearExactResult`
  清除已存精检结果。

### 变更 (Changed)（语义侧栏 v11→v12：零件名噪声再过滤）
- **零件名列表更干净**：`is_part_name` 增加两类过滤——①**多行文本**（两处标注
  叠一起如 `手柄接头\n定位垫片`）不算零件名；②**单行工艺/材料/装配说明**
  （`一次出光滑`、`切口`、`材料：不锈钢`、`（与水仓卡环紧入）`、`逆时方向螺旋`、
  `去毛刺` 等）归入 `_RE_PART_NOISE` 排除。带括号规格的真零件保留
  （`接气阀连接杆（短/长）`）。实测咖啡机全套图：去重零件 130 → 123。
  `is_noise_note` 同步滤掉单行工艺/材料说明（多行技术要求/公差表保留）。
  `DRAWING_SCHEMA_VERSION` 11 → 12。

### 测试 (Tests)（过期渲染断言对齐 v9 策略）
- **4 个图纸渲染测试按 v9 策略修正**：旧断言仍按 v4/v5 行为——① 合并路径正则
  匹配 `<path d=`，而当前描边 path 为 `<path vector-effect="non-scaling-stroke"
  d=`（元素级声明线宽锁定屏幕像素，v6→v7）；② 离群实体"整实体剔除不渲染"
  的断言与 v8→v9「**所有可渲染实体完整渲染**、viewBox 仅作默认取景」的意图
  冲突——`test_defpoints_and_outlier_entities_excluded` 改为
  `test_outlier_entities_do_not_break_viewbox`：断言离群实体与文字**仍完整
  渲染**，但默认取景框按鲁棒范围收敛到主体不被撑爆。全套 **192 用例全绿**。

### 变更 (Changed)（语义侧栏 v10→v11：噪声 note 再过滤 + 模糊搜索 + 定位优化）
- **"其它标注"再筛一层噪声**：`cad_drawing.py` 新增 `is_noise_note`，
  对非零件名的 note 丢弃明显无意义内容——日期（`2026-08-23` / `2026年8月23日`）、
  页码（`第3张` / `共12页`）、缩放比例（`1:2` / `1：1`）、版本号（`V1.2`）、
  纯数字/尺寸残留（`0.05`）、剖视线/视图标记（`A` / `A-A` / `F1` / `A13`）、
  标题栏固定栏位标签（批准/审核/设计/比例/签名…，复用 `_PART_FRAME_WORDS`）、
  单行视图/图名（`底视图` / `坯件图` / `总装图` / `焊接`…；多行技术要求等
  正文保留）。实测咖啡机全套图：note 2428 → 243，part_name 218 保持。
  `DRAWING_SCHEMA_VERSION` 10 → 11 触发旧缓存重建。
- **语义栏模糊搜索**：`drawing.js` 语义面板顶部新增搜索框，实时按子串
  （不区分大小写）过滤"零件/其它标注"两组，分组标题显示匹配数
  （`零件 3 / 130`）；搜索结果仍可点击定位，未搜索时"其它标注"截断
  120 条、搜索时放宽到 400 条。
- **点击定位取景优化**：`locateSemantics` 以加载时固定的全图 viewBox 为
  稳定基准（修复连点累计放大），窗口 = 全图长边 × 0.05（约 10%），
  既能看清零件周边图形又不过贴；修复定位坐标 Y 未翻转导致跳到图外的问题。

### 变更 (Changed)（语义侧栏"零件"分组：v9→v10）
- **语义侧栏新增"零件名称"分类**：装配图纸（如 2.0 咖啡机全套图）文字布局
  与零件名位置不统一，靠纯位置/图层无法区分零件名与其它文字。改为确定性
  规则语义分类：`cad_drawing.py` 新增 `clean_entity_text`（剥 MTEXT 格式码、
  `%%C→Ø`、折行折叠）与 `is_part_name`（图框白名单 / 技术要求 / 视图名 /
  加工工艺 / 纯序号 / 日期 / 版本 / 纯规格尺寸 / 标准件词过滤；保留 ≥2 汉字
  中文名词短语，规格+定制件后缀如"阀芯/齿轮/组件/杆"豁免，`M\d` 常规螺丝
  不当零件）。`extract_semantics` 输出 `kind=part_name`。实测咖啡机全套图
  2685 条文字 → 131 个零件名。
- **前端语义栏分组**：`drawing.js` 把语义拆成"零件"（去重、置顶、绿色高亮、
  点击定位到图纸对应坐标）与"其它标注"（截断到 120 条防 DOM 拖垮）两组，
  新增 `.drw-group-title` / `.drw-part` / `.drw-part-row` 样式。
  `DRAWING_SCHEMA_VERSION` 9 → 10，旧缓存自动重建。

### 修复 (Fixed)（图纸文字渲染：对齐锚点 / 旋转 / \H 高度覆盖）
- **文字渲染此前全部按"左下角起点 + 水平方向"绘制**，四类 DXF 对齐语义
  全部丢失：① TEXT 的 halign/valign（非左对齐时锚点应在 `align_point`，
  尺寸数值普遍是居中对齐——渲染出来整体右移错位、压在尺寸线上）；
  ② MTEXT 的 `attachment_point` 1-9 宫格锚点；③ TEXT/MTEXT 旋转
  （`rotation`/`text_direction`，竖排/斜排标注渲染成水平，与图形交叠
  看似"失真"）；④ MTEXT 内联 `\H<高度>;` 绝对高度覆盖（如
  `\H7.50;D-D` 强调段被基值 5.0 压小）。修复：DXF 锚点语义映射到 SVG
  `text-anchor` + 基线偏移（SVG y 向下：字形顶=基线-0.8h、底=基线+0.2h，
  按底/中/顶对齐换算 dy），旋转经 Y 翻转坐标系取负后输出
  `transform="rotate(...)"`，`\H` 覆盖取整段最大有效高度。实测咖啡机
  4668 文字：4035 个对齐锚点 + 1162 个旋转文字正确渲染，标题栏
  "批准/设计/审核"等居中于表格单元格。`DRAWING_SCHEMA_VERSION` 5 → 6
  触发旧缓存重建（水仓/盖子两张图此前停在 schema 4 中间版本，内联
  格式码 `{\fFangSong|b0|i0;...}` 原样显示为乱码文字，一并修复）。
- **服务白名单回退**：`CAD_SERVICE_ALLOWED_DIRS` 临时扩展到
  `~/Documents/ai`（为直接打开 OneDrive 图纸）已回退——文件打开的
  正常路径是先上传到 `workspace/uploads` 再做后续处理，白名单恢复
  默认（项目目录 + uploads）。

### 变更 (Changed)（图纸渲染 v6→v9 收口：移除"错误修复方向" + 准确性修正）
- **v6→v7（文字/线条放大失真修复）**：文字改实心 `fill` + `stroke:none`；
  线条/路径/圆统一 `vector-effect="non-scaling-stroke"` + 固定 `stroke-width`
  （此前描边随 viewBox 同步放大，放大后线宽翻倍、文字糊成"一坨"）。
- **v7→v8（几何准确性）**：ELLIPSE 按 `start/end_param` 判定开放/闭合
  （2063 个椭圆弧先前被错画成整椭圆）；闭合 SPLINE 按 `closed` 闭合
  （97 个剖面轮廓补回缺失段）；HATCH 容差收紧至 ≤0.5mm；实体上限
  12 万 → 40 万（超限打印告警而非静默截断）。
- **v8→v9（移除"鲁棒过滤丢实体"的错误方向）**：此前为压制离群实体撑爆
  viewBox，用 `_robust_extent`（中位数 ± 8·MAD）**整实体剔除不渲染**——
  这误删了标题栏文本等合法内容（属"错误修复方向"）。现改为**所有可渲染
  实体均完整渲染**，`_robust_extent` 仅用于计算默认取景框（初始缩放到
  内容中心），绝不删除内容；同时修正多段线 BULGE 圆弧方向（先前画到
  错误一侧）、POINT 半径与采样弦高自适应。`DRAWING_SCHEMA_VERSION` 6→9，
  旧缓存经 `?v=` 缓存破坏参数自动重建。

### 修复 (Fixed)（大尺寸图纸渲染：viewBox 撑爆 / DOM 冻结 / 标注缺失）
- **大图纸"特征像被放大、整图缩不小"（viewBox 被离群实体撑爆）**：展开
  INSERT/DIMENSION 后带出被挪出图幅的旧标注/废弃视图（咖啡机图纸实测
  x≈-123 万的 34 个实体 vs 主体 ~7000 宽），min/max 边界被撑大近 200 倍，
  核心几何被压成屏幕上一小块。修复：`_arc_extent` 按 ARC 实际扫过角度算
  紧致包络（大半径小角度构造弧不再用整圆包络 cx±r 撑爆边界）。默认取景
  框改用实体中心点的鲁棒范围（中位数 ± k·MAD，`_robust_extent`）——
  **仅用于初始缩放到内容中心，不删除任何实体**（其"整实体剔除"的旧用法
  已在 v9 移除，见上「v8→v9」）。
- **标注几何/块引用整块缺失（容器实体未展开）**：INSERT / DIMENSION /
  LEADER / MLEADER / TOLERANCE / ACAD_PROXY_ENTITY 原先直接跳过——大型
  装配图纸的尺寸线/箭头/数值文本与块引用内容全在这类容器里。新增
  `_iter_leaf_entities` 迭代器栈：`virtual_entities()` 展开为带变换叶子
  实体（嵌套块引用递归展开、保持文档顺序、免深递归）。
- **DOM 爆炸冻结标签页（合并路径架构）**：逐实体一个 SVG 元素，咖啡机
  85776 实体 → 8.3 万 DOM 节点 / 15.9MB，浏览器直接冻结。改为合并路径：
  全部描边实体合成 1 个 `<path>`、HATCH/SOLID 填充各 1 个 `<path>`、圆
  独立元素、文字独立元素——DOM 降到 **5973 节点**；坐标紧凑格式化
  （小数位按图幅自适应 + 去尾零）。新增 HATCH（半透明填充）与 SOLID
  （实心填充）渲染、SPLINE 按图幅自适应弦高 flattening 展平（此前只画
  控制点折线）、Defpoints 标注定义点过滤（出图本就不打印，渲染只是
  满屏杂点）、实体上限 2 万 → 12 万（大装配图纸不再截断）。
  `DRAWING_SCHEMA_VERSION` 4 → 5 触发旧缓存重建。
- **前端 10MB 级 SVG 加载/交互卡顿**：① `mountSvg` 弃 DOMParser 全量
  解析（还要逐节点搬移 8 万+ 子节点）改字符串切片 + 单次 `innerHTML`；
  ② 缩放/平移 rAF 合帧（滚轮事件频率远高于刷新率，逐事件全量重绘
  10MB SVG 必卡）；③ 语义列表截断 120 条（大图纸数千条标注文字全量
  渲染拖垮信息面板）；④ CAD 发丝线 `vector-effect: non-scaling-stroke`
  （此前描边随 viewBox 同步放大，放大 2 倍线宽翻倍、细节被吞）。
- **渲染升级后浏览器仍显旧图**：SVG 请求 URL 加 `?v=<schema>-<sha12>`
  缓存破坏参数，否则 HTTP 缓存一直返回旧渲染结果（配合「强制重建缓存」
  勾选项——原先勾选后仍提示缓存命中，`force` 参数未接入后端）。
  实测（咖啡机 85776 实体）：加载后 30 帧平均帧间隔 17.6ms，放大后
  线条清晰、无空白无碎片，文字标注可辨。

### 修复 (Fixed)（编辑页特征选取/高亮链路 + 侧栏折叠 + 模板名乱码）
- **模板名/零件名 GBK 乱码**：SolidWorks 导出 STEP 时把中文名按 Windows
  代码页（GBK）裸写进文件，STEP 标准不声明编码；OCCT 读取器默认按 UTF-8
  解码（`Resource_FormatType_UTF8` → `TCollection_ExtendedString(bytes,
  true)`），两套编码错位产生乱码（如 `¶à³Ýµ÷µµ×ùC4-1-1`）。逆向破解
  OCCT 解码器（`NCollection_UtfIterator::readUTF8`，旧版 6 字节 UTF-8：
  `k = UTF8_BYTES_MINUS_ONE[b0]` 逐字节 `<<6` 累加后减 `offsetsFromUTF8[k]`；
  任一符号码点 > 0x10FFFF → `ConvertToUnicode` 返回 false → **整串回退
  逐字节 Latin-1**——这正是"部分名字全乱、部分半乱"的原因）。另复刻 STEP
  读取器 `cleanText` 链路（字符串字面量含首尾撇号整体解码，跳过开头撇号
  并裁掉末字符，故末尾多字节序列吞掉撇号时真实名字末字符被裁，如"平头
  刺针"末字节 EB）。新增 `build_mojibake_fixmap`：扫描源 STEP 全部引号
  字符串，GBK 可解码者正向算出 OCCT 乱码串，建 乱码→正确名 映射表，
  `parse_assembly` 在模板/树节点名读取后直接替换（36/36 全部命中，
  根节点"总装260129"）。对任意 GBK STEP 有效，不依赖上游导出设置。
  `SCHEMA_VERSION` 3 → 4，旧缓存下次 parse 自动重建。
- **特征列表选取无 3D 高亮**：编辑页原先未调用 `showFeature`（首页特征
  面板逻辑未复用），列表选特征只有表单切换无视觉反馈。新增
  `syncFeatureHighlight`：特征选择 / 粒度切换 / 模板切换时在基线+草稿
  双视口同步显示橙色 overlay；切走特征粒度或换模板自动清除。
- **点击模型不联动目标特征列表**：特征级 3D 拾取 `pickFeatureAt` 三处
  根因——① GLTFLoader 经 `PropertyBinding.sanitizeNodeName` 删除
  `.` `:` `/` `[` `]`（`#1.1` → `#11`），与特征 JSON id 失配 → 加载时
  按特征 JSON 建立 sanitize 名 → 原始 id 映射表；② 多 primitive 特征
  被展开为 Group(原名) + Mesh(原名_N)，非递归射线检测永远打不中 → 改
  递归检测并沿父链回溯顶层特征节点；③ 3D 拾取（等 glTF）与特征列表
  （等 JSON）并行竞态 → `pendingFeature` 暂存拾取结果，列表就绪后回放
  （模板已切换则丢弃）。首页特征面板共享 `showFeature` 同步受益。
- **零件整体橙色染色淹没特征高亮**：`reapplyViewFilter` 原先将目标
  模板全部实例染成橙色，与特征 overlay 高亮冲突。去掉整件染色，零件
  保持默认色——编辑页的选中反馈只有特征 overlay 的橙色高亮一层。
- **侧栏折叠丢失模型窗口且无法回退**：折叠时 `grid-template-columns:
  0 1fr 0` 把中栏压进 0 宽轨道（canvas 宽度变 1px）；改单列
  `minmax(0,1fr)` 让中栏占满整行，中栏加常驻展开按钮杜绝"折叠后无法
  恢复"死局；窄屏媒体查询重置为单列布局不受折叠影响。
- **特征高亮呈条状空白/碎片状橙色**：零件模板 glTF 按自适应偏差
  （`maxdim/800`，上限 0.5）网格化，特征 glTF 却固定偏差 0.1——特征
  compound 复用原 shape 的同一批面（共享 TShape），不同偏差使 BRepMesh
  对同一张曲面生成两套三角化，overlay 表面在零件表面前后交替摆动，
  深度测试下呈条状空白，掠射角处偏差放大、碎片化最严重。修复：
  `_export_features_gltf` 增加 `deflection` 参数并与模板导出统一用
  `_deflection_for(shape)`（`_build_template_features` /
  `refresh_template_features` 两条调用链同步传入）——同偏差下增量网格器
  直接复用面上已存三角化，两份 glTF 顶点逐位一致（t1 实测 87 网格 /
  1154 顶点全部精确重合），overlay 与零件表面严格共面；前端 overlay
  材质恢复 `depthTest: true` + `polygonOffset(-1, -2)` 平局裁决。
  `SCHEMA_VERSION` 2 → 3，旧缓存下次 parse 自动重建（咖啡机 62 模板
  已重建验证：正对特征像素扫描实心填充，最长连续段 30、碎片段仅 2）。
- **已知残留（暂缓优化）**：个别部位仍有轻微空白（不影响使用，用户
  确认暂不处理）。候选优化方向：overlay 顶点沿法线微偏移（vertex
  normal offset）替代纯 `polygonOffset`，或按掠射角自适应加大偏移量；
  待用户反馈再定优先级。

### 新增 (Added)（可观测性 + 干涉分级性能）
- **结构化服务日志**：`workspace/logs/service.log`（2 MB × 3 轮转）——请求级
  访问日志（方法/路径/状态/耗时，>1s 标 `SLOW`）；未捕获异常 500 兜底 + 完整
  堆栈入日志（此前端点崩溃直接断连接，前端只见 network error）；parse/edit/
  preview/confirm/report 关键路径失败 `log.exception` 记堆栈。
- **前端错误捕获 `initErrorTrap`**：`error` + `unhandledrejection` 统一上报
  `POST /api/logs/client`（含堆栈，3s 节流防刷屏），四页全接入；
  `window.__cadErrors` 供控制台排查。
- **几何重活线程池下放**：parse/edit/preview/confirm/report/drawing 等 7 个
  端点的 OCP 重活经 `run_in_threadpool` 执行（`_GEOMETRY_LOCK` 仍保证 OCCT
  串行，D2/R4 不变）——事件循环不再阻塞，修复"一次操作期间整服务冻结
  （静态文件/health/WS 全部无响应）即系统瘫痪"的根因。
- **干涉检查分级（交互性能）**：preview 默认 `level=bbox` AABB 粗筛（bbox
  重叠即黄色"可能碰撞"卡片，毫秒级）；验证轨道新增「精确检查」按钮显式
  布尔精检（红色卡片带穿透体积 mm³，hint 显示耗时秒数）；**confirm 守门
  始终 exact 布尔**（D8 确定性不降级）。move 级微调 preview 热路径
  13s → **~5ms**（62 模板咖啡机实测）。
- **模板形状进程级缓存**：STEP 导入按 (path, mtime, size) 缓存（62 模板
  冷 7.4s → 热 0s；版本 commit 覆盖由 mtime 失效，FIFO 256 淘汰）+ 本地
  bbox 缓存（BRepBndLib 每不同 shape 只跑一次；世界 bbox = 8 角点 ×
  实例矩阵纯 Python 数学，替代每实例 OCP 调用 ~5s）。
- **`GET /api/assembly/view?cache_key=`**：cacheKey 直载通道（不读源文件、
  不重 parse）——编辑页回首页、最近列表点击、URL 引导全部切换；源文件已
  移动/删除或不在白名单时已缓存装配体仍可完整预览。`?cacheKey=` 与
  `?load=` 互斥写回地址栏。
- **preview 计时提示**：验证轨道显示「检查中… Ns」动态秒数（大装配精检
数十秒，静态文案会被当成假死）。

### 变更 (Changed)（DWG 看图 UI：交互手感 / 缩放控件 / 工具栏整理）
- **图纸窗口占满视口**：原图纸区纵向被 ~640px 限制、下方大片空白，且拖拽易误
  触发「松开以加载文件」提示、易选中文字。改为 `.page-drawing` 直接
  `display:flex; flex-direction:column; height:100vh`、SVG `width/height:100%`；
  拖放遮罩 `bindDropOverlay` 仅当 `dataTransfer.types` 含 `Files`（真实文件拖入）
  才显示；文字选择仅在拖拽期间临时禁用（`pointerdown` 检测 `closest('text')`，
  点在文字上不进入拖拽、允许浏览器选中）。
- **状态条不再占布局高度**：「浏览或拖入 DXF / DWG 文件以加载」提示条原是
  flex 兄弟元素、未加载时撑成半屏高。改为移入 `#drawing-view` 内部、
  `position:absolute` 浮于左下、不占高度、不阻挡拖拽。
- **光标语义区分**：悬停文字显示 I-beam 竖条（`#drw-g text { cursor:text }`），
  悬停空白/几何显示 CAD 十字线，直观区分「可选文字」与「可拖画布」。
- **缩放控件增强**：新增缩放滑动条（对数映射 10%–5000%，默认 100%，宽度 220px），
  居中夹在「－ / 百分比读数 / ＋」之间；滑条与滚轮/按钮/双击统一走「交互期只动
  CSS transform、停手 140ms 才折算 viewBox」框架，8 万实体图缩放/平移不再逐次
  全量重排。`updateZoomPct` 反向同步滑条位置（滚轮/按钮缩放后滑条跟着跳）。
- **工具栏按逻辑重排**：视图导航（适配 / 框选）、缩放控制（－ / 滑条 / 百分比 /
  ＋）、视图模式（面板 / 全屏）三组，组间分隔线。因默认打开即适配态，「复位」
  与「适配」等价，已移除「复位」按钮（避免冗余困惑）。
- **性能回退教训**：曾给 `#drw-g` 加常驻 `will-change:transform; contain:...` 试图
  提示合成层，实测反而更卡——常驻 `will-change` 强制浏览器把整棵 8 万节点 SVG
  永久留在 GPU 合成层，每次停手折算 viewBox 都要重传/栅格化全图。已移除；保留
  的有效优化为「交互期只动 transform + 交互期 `pointer-events:none` 跳过整图命中
  测试」。滑条跳变也已修（拖动期间抑制中途提交，仅松手一次性折算）。

### 修复 (Fixed)（性能加固过程中发现的正确性缺陷）
- **`world_bbox` 平移分量丢失**：世界包围盒角点误用 `gp_Vec.Transformed`
  （向量变换不含平移——数学正确但类型用错），所有实例的世界 bbox 全部
  坍缩在模板本地位置，bbox 预筛长期失真（方向是"过度送检"，无漏检但
  exact 白跑大量布尔对）。改纯 Python 点变换后动态正确（移动 5mm → 19
  对重叠 / 远离 2000mm → 0 对）。
- **move 步骤坏 node_id 静默退化全量检查**：未知 node_id（如误用 STEP
  标签 `NAUO66` 而非节点 id `n2`）时 `apply_moves` 静默跳过 → 增量候选集
  为空 → `check_interference` 退化全量 O(n²) 布尔（62 模板 ~5 分钟）且
  草稿实际没移动任何东西（错误结果 + 假死双重症状）。现在未知/非零件
  节点 400 快速失败（中文提示区分节点 id 与 STEP 名）。
- **空步骤 preview 触发全量检查**：删光步骤的 preview 之前同样会跑全量
  干涉检查（~5 分钟），现在短路 3ms 返回（草稿=基线）。
- **report_generate `counts` NameError**：报告中心重构引入的回归，日志
  系统上线后当场捕获修复（instances 计数）。
- **零步骤放弃草稿误载整装配体**：仅在草稿视口展示过编辑几何时才重载
  基线几何，且保留相机与范围过滤状态。
- **草稿删除 WS 回声误提示**：删除请求带 `client` 标识，广播事件透传，
  前端忽略自己发起的删除事件（不再弹"草稿已被远程删除"）。
- **编辑页回首页/重新打开失败**：回首页与最近列表原先拿 `manifest.
  source_file`（裸文件名）走 parse——parse 必须读源文件算 hash，源文件
  移动/删除或不在白名单时直接失败（首页空白）。改 `GET /api/assembly/
  view` 按 cacheKey 直载后闭环。

### 新增 (Added)（M6.5：编辑页观察工具 + 草稿内位置调整）
- **双视口相机联动**：基线/草稿两视口视角实时同步（OrbitControls change
  回调互播 + 回声守卫防死循环），「视角同步」按钮锁/解锁，开启即对齐。
- **观察工具条**：爆炸滑杆 / 三轴剖切（X/Y/Z + 位置 + 反向，`setSection`
  向后兼容旧签名）/ 透明鬼影 / 视角书签（1–3 槽）/ 视角同步 / 移动模式
  ——两视口同步生效。
- **范围切换自动取景**：预览范围（整装配/子装配/零件）切换时相机自动
  取景到目标包围盒（`fitToIds`，含爆炸与临时位移；工具条高度变化导致
  的画布裁剪错位以动态位置计算修复）。
- **草稿内位置调整（move 步骤）**：移动模式下草稿视口点选零件 →
  TransformControls 拖拽 → 生成 move 步骤（实例级世界系位移，node_id
  寻址）；同节点重复拖拽替换不累积（"最终位置"语义）；与几何步骤统一
  进草稿步骤表、参与增量干涉检查（移动实例加入候选集）、参与版本落盘
  （commit `moves` 字段 + `resolve_moves` 沿版本链解析，move-only 版本
  不携带模板文件）；基线视口永远不动（对比语义）。

### 新增 (Added)（M6：Agent 通信层——用户↔agent 基于选中上下文协作）
- **选中上行 `POST/GET /api/selection`**：前端把用户点选的零件/特征上报
  服务（含 page 与 tab 标识，支持多窗口并发）；agent 经 MCP
  `get_user_selection` 读取——"这个的厚度加 1mm"式对话的上下文来源。
- **会话发现 `GET /api/sessions`**：服务端最近会话列表，替代 localStorage
  最近文件（跨浏览器同步；`workspace/selection/` 会话状态落盘）。
- **WS 事件广播**：draft_saved / draft_deleted / version_changed /
  selection_changed / report_added——agent 内置浏览器与系统浏览器双开时
  状态互通。
- **MCP 会话工具组 7 个**（总数 11 → 18）：`list_sessions` /
  `get_user_selection` / `read_draft` / `preview_draft` / `edit_draft` /
  `checkout_version` / `generate_report`。
- **协作边界固化**：agent 只写草稿（`edit_draft`），确认落版本
  （`/api/drafts/confirm`）永远留给用户在 Web UI 点按钮。
- **token 与 URL 引导加固**：`?token=` / `?load=` 保留在地址栏（agent
  内置浏览器"用系统浏览器打开"后免重新输入，裸 prompt 改页内引导卡片）；
  401 统一清 token 并提示从 agent 重新打开链接；加载成功后 `?load=`
  写回地址栏供复制/刷新。

### 新增 (Added)（M4/M5：报告中心 + FEA 基线草稿双跑对比）
- **报告中心**：`POST /api/reports/generate`（快照聚合：体检结果 + 统计
  （模板/实例数、体积、表面积）+ 版本历史）/ `GET /api/reports`（列表）/
  `GET /api/reports/get`（单份）；`workspace/reports/<key>/` 落盘；独立
  报告页（report.html）浏览。
- **FEA 双跑对比**：`POST /api/drafts/fea-compare`——对基线与草稿版本的
  目标模板分别跑静力学，对比最大位移 / 最大 von Mises 应力；编辑页右栏
  提交、R5 job 进度跟踪、结果对比展示；步骤变更后已有结果标记过期。

### 新增 (Added)（M1–M3：编辑会话闭环——多页架构 + 草稿模式 + 试改工作流）
- **Vite 多入口 MPA**：前端从单页拆为四页——首页（index.html：预览 /
  选取 / 上下文动作区）、编辑会话（edit.html）、图纸对照（drawing.html）、
  报告（report.html）；页面间经 URL 参数传递 cacheKey/scope；代码分层
  `pages/`（页逻辑）+ `shared/`（utils/jobs/plugins/api/scene/tree）；
  dev server 代理全部后端静态路由（/cache /versions /drawings /drafts
  /fea /render）。
- **草稿模式端点**：`GET /api/drafts`（单槽位读取）/ `POST /api/drafts/
  save` / `DELETE /api/drafts`（放弃）/ `POST /api/drafts/preview`
  （步骤重放 → 草稿 manifest + 增量干涉 + diff）/ `POST /api/drafts/
  confirm`（全部步骤原子落为一个版本）；`GET /drafts/{key}/...` 草稿
  预览 glTF 静态服务（防穿越）。
- **编辑会话页（edit.html）**：进入即锁基线版本；双视口（基线锁定 vs
  草稿实时）；左栏声明式步骤表（多目标、可增删，每步变更自动触发重放 +
  增量干涉，防抖 + 竞态守卫）；右栏验证轨道；预览范围三档 visibility
  lens（整装配/子装配/零件，不换页）+ 目标模板高亮；五级级联操作表单
  （目标模板 → 编辑粒度 → 特征 → 操作 → 参数，特征选项按类型过滤自
  `features/tN.json`）；窄屏底部抽屉 + 侧栏折叠（响应式）。
- **首页上下文动作区**：按选中层级（装配体/子装配体/零件）动态切换可用
  操作（隔离子树 / 进入编辑 / 特征面板 / 体检 / 力学 / 渲染 / 图纸）。

### 移除 (Removed)（静态预览出口清理）
- **退役三个"生成静态 HTML 预览"的 CLI 出口**：`make_preview.py`（整体预览页）、
  `feature_locator.py` 的 2D 定位图出口、`feature_picker.py` 的离线拾取页出口。
  Web 视口（`/app`）落地后，交互式 3D 拾取、特征面板、整体预览均由前端实时
  承担（agent 侧经 `?load=` URL 直达），静态 HTML 出口无存在必要——
  `make_preview.py` 整文件删除（-170 行）。
- **`feature_locator.py` / `feature_picker.py` 保留为纯特征识别/枚举库**：核心
  API（`collect_features` / `group_features` / `detect_patterns` /
  `collect_feature_solids`）原样保留——它们是 cad_service 特征缓存
  （`features/tN.json` + `tN.gltf`）的上游；删除的是 HTML 生成、2D 标签布点
  （`project_edges` / `place_labels` / `build_html`）与 vendor 资产嵌入代码
  （两文件合计 -975 行）。
- **MCP `pick_features` 改纯结构化返回**：不再写 HTML 文件，直接返回特征
  元数据 JSON（稳定 id #N / #N.k / P#，与 Web 视口拾取同源）+ 体积/拓扑/
  包围盒属性摘要。
- **移除 `vendor/` three.js 本地化目录**（-670 KB）：Web 前端改由 npm 锁定
  版本 + `dist/` 构建产物提交承担；`cad_core` 内 vendor SHA-256 校验逻辑
  与 `previews/` 旧产物目录随之删除。
- **CI / 测试适配**：冒烟步骤 `feature_picker.py` CLI 替换为 `selftest.py`
  （OCP 读/属性/转换，样本再生语义：写前删旧文件防覆盖保护误触发）与
  `pick_features` MCP 工具结构化断言；删除 `tests/test_feature_picker_html.py`，
  pytest 覆盖配置同步更新。净变化 **-1315 行**（19 个文件）。

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
- **macOS 探测漏检**：`probe_oda_converter` 原先只搜 Windows 目录与 3 个
  Linux 路径，**完全漏掉 macOS**——已安装的 `/Applications/ODAFileConverter.app/
  Contents/MacOS/ODAFileConverter` 永远扫不到，插件面板显示灰点。新增
  `_ODA_MAC_DIRS`（`/Applications`、`~/Applications`）glob `ODAFileConverter*.app/
  Contents/MacOS/ODAFileConverter`，并在候选直连路径补 macOS 位置。
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
