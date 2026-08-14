# CAD 工具链 (cad_tools)

离线 CAD 特征拾取 / 预览工具链。把 STEP/IGES 模型每个特征切成独立 STL 网格，
生成**可点击拾取**的 3D HTML 预览（three.js 已本地化 vendored，离线可用），并提供
MCP server 供其他 agent 调用。

## 环境要求
- Python 3.13（3.12+ 一般也可，但 OCP 轮子按 3.13 验证）
- Windows / macOS / Linux

## 一键部署
```bash
python bootstrap.py
```
脚本会创建 `venv/` 并安装 `requirements.txt` 里的依赖
（cadquery-ocp-novtk / build123d / fastmcp / numpy）。

Windows 与 macOS 上命令**完全相同**；首次运行需联网从 PyPI 拉取 Python 依赖，
而 three.js 已随仓库 vendored，预览本身无需联网。

## 生成拾取预览
```bash
venv/Scripts/python feature_picker.py your_model.step --out-dir previews   # Windows
venv/bin/python        feature_picker.py your_model.step --out-dir previews   # macOS / Linux
```
生成的 `previews/your_model_拾取.html` 与同目录 `vendor/` 一起打开，**断网也能用**。

## 目录结构
- `cad_core.py` — OCP 核心（读 STEP/IGES、属性、包围盒）
- `feature_locator.py` — 曲面枚举 + 分类聚合 + 2D 编号定位图
- `feature_picker.py` — 特征级 STL 切片 → 可点击 3D 预览（three.js 本地化）
- `make_preview.py` — 实体整体预览
- `cad_mcp_server.py` — FastMCP server（8 工具；feature_picker 暂未接入）
- `cad_build.py` — build123d 字体 import-hook（跨平台无害）
- `vendor/` — 本地 three.js（three@0.160.0：`three.module.min.js` + `OrbitControls` + `STLLoader`）
- `selftest*.step` / `selftest.iges` — 示例输入，用于冒烟测试

## 同步更新
本仓库即单一可信源。任意机器上：

```bash
git pull
python bootstrap.py      # 仅当依赖变更时需要重跑；代码更新直接生效
```

即可拿到最新代码与（如有）依赖变更。

## 打包 / 给其他 agent 用
分发时把整个仓库（含 `vendor/`）一起带走即可；缺失 `vendor/` 时 `feature_picker`
首次运行会自动从 CDN 下载缓存。HTML 与同目录 `vendor/` 的相对位置不能拆开。

## 注意
- `venv/`、`previews/`（生成产物）、`*.stl` 等已被 `.gitignore` 排除，不进版本库。
- MCP 接入 `feature_picker`（`pick_features` 工具）是已知缺口，见 `cad_mcp_server.py` 注释。
