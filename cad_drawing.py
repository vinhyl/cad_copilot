"""Drawing (DXF/DWG) import & semantic calibration (Phase D, ADR-0002 D5/模块六).

Pipeline (ezdxf first, ODA-gated DWG):

    DWG --[ODA File Converter, probed]--> DXF --[ezdxf]--> {
        semantics: threads / diameters / tolerances extracted from TEXT,
                   MTEXT and DIMENSION measurement strings,
        svg:        minimal renderer (LINE/CIRCLE/ARC/LWPOLYLINE/TEXT),
    } cached under  workspace/drawings/<sha16>/  (R8 key, R17 idempotent)

DXF is read with ezdxf (already in the venv as a transitive dep; explicit
in requirements). DWG is NOT read natively -- ODA File Converter is probed
at well-known install paths and invoked as a subprocess (D5: probe +
graceful degradation + cached conversion; R9: never bundled).

The minimal SVG renderer exists because the ezdxf drawing add-on requires
PIL, which this project deliberately does not add (轻依赖). It covers the
entity types that matter for a 2D 对照 view.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess

import ezdxf

# --- ODA File Converter probe paths (D5) -------------------------------
# Resolution order (mirrors cad_fea / cad_render): env override ->
# well-known install dirs (glob any version, e.g. "ODAFileConverter 27.1.0").
_ODA_WIN_DIRS = [
    r"C:\Program Files\ODA",
    r"C:\Program Files (x86)\ODA",
]
_ODA_MAC_DIRS = [
    "/Applications",
    os.path.expanduser("~/Applications"),
]
_ODA_CANDIDATES = [
    "/usr/bin/ODAFileConverter",
    "/usr/local/bin/ODAFileConverter",
    "/opt/ODAFileConverter/ODAFileConverter",
    "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter",
]

# --- semantic patterns (模块六: threads / diameters / tolerances) --------
_RE_THREAD = re.compile(r"\bM(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\b")
_RE_DIAMETER = re.compile(r"[ØøΦφ⌀]\s*(\d+(?:\.\d+)?)")
_RE_TOLERANCE = re.compile(r"\b([A-Z][0-9])\s*/\s*([a-z][0-9])\b")

# 图纸缓存 schema：渲染逻辑变化（v9：移除"鲁棒过滤丢实体"的错误修复方向
# ——所有可渲染实体完整渲染，viewBox 仅用于默认取景；修正 LWPOLYLINE
# 凸度圆弧 sweep 方向（此前镜像到实体错误一侧）；曲线弦高容差收紧到
# ≤1mm 且椭圆按尺寸自适应加密采样；POINT 标记尺寸封顶避免大图变巨点；
# v8：椭圆弧按起止参数判定开放、闭合样条按 closed 闭合、HATCH 容差 ≤0.5mm、
# 渲染上限 120k→400k；v7：文字实心填充 + non-scaling-stroke 恒定线宽，
# 修复放大后文字糊成"一坨"；v6：TEXT/MTEXT 对齐锚点/旋转/MTEXT \H 覆盖；
# v5：INSERT/DIMENSION 块展开、HATCH/SOLID 渲染、合并路径架构、
# MTEXT 格式码清理、Defpoints 过滤）时递增，服务端据此判定旧缓存需重建。
DRAWING_SCHEMA_VERSION = 9

# 需展开为叶子实体的容器实体：大型装配图纸的标注几何（尺寸线/箭头/数值
# 文本）、块引用内容全在这类容器里，跳过它们 = 图纸缺一大块。
_EXPANDABLE = ("INSERT", "DIMENSION", "LEADER", "TOLERANCE", "MLEADER",
               "ACAD_PROXY_ENTITY")

# Defpoints 是标注定义点（构造点），CAD 出图本就不打印，渲染只会满屏杂点
_DEFPOINT_LAYERS = {"defpoints", "def points", "def point"}


class DrawingError(RuntimeError):
    """Raised for unsupported input or missing ODA converter (D5)."""


def probe_oda_converter() -> str | None:
    """Return the ODA File Converter executable path, or None.

    D5: 默认开启、探测降级。Resolution: CAD_ODA_EXE env -> well-known
    Windows install dirs (any version, newest first) -> unix paths.
    """
    import glob

    env = os.environ.get("CAD_ODA_EXE")
    if env and os.path.isfile(env):
        return env
    for base in _ODA_WIN_DIRS:
        hits = glob.glob(os.path.join(base, "ODAFileConverter*",
                                      "ODAFileConverter.exe"))
        if hits:
            return max(hits)  # newest version sorts last lexically
    for base in _ODA_MAC_DIRS:
        hits = glob.glob(os.path.join(base, "ODAFileConverter*.app",
                                      "Contents", "MacOS", "ODAFileConverter"))
        if hits:
            return max(hits)  # newest version sorts last lexically
    for p in _ODA_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _dwg_to_dxf(dwg_path: str, out_dir: str) -> str:
    """Convert a DWG to DXF via ODA File Converter (D5: probe + degrade)."""
    oda = probe_oda_converter()
    if oda is None:
        raise DrawingError(
            "DWG 输入需要 ODA File Converter（未在常见安装路径找到）。"
            "请安装 ODA（免费）或将图纸另存为 DXF；已安装时可用环境变量 "
            "CAD_ODA_EXE 指定路径。DXF 输入无需任何外部工具。")
    os.makedirs(out_dir, exist_ok=True)
    # ODA CLI: ODAFileConverter <src_dir> <out_dir> <version> <type> <recurse>
    #   <audit> [<filter>]
    #   version: ACAD9..ACAD2018; type: DXF/DWG/DXB; recurse/audit: 0/1
    #   filter MUST be a glob like *.DWG -- a non-glob value (e.g. "0")
    #   matches nothing and ODA pops "no matched files in input folder".
    src_dir = os.path.dirname(os.path.abspath(dwg_path)) or "."
    r = subprocess.run(
        [oda, src_dir, out_dir, "ACAD2018", "DXF", "0", "0", "*.DWG"],
        capture_output=True, timeout=120)
    base = os.path.splitext(os.path.basename(dwg_path))[0]
    dxf = os.path.join(out_dir, base + ".dxf")
    if r.returncode != 0 or not os.path.isfile(dxf):
        raise DrawingError(f"ODA 转换失败（exit {r.returncode}）：{dwg_path}")
    return dxf


# --------------------------------------------------------------------------
# Semantic extraction (模块六 语义真理)
# --------------------------------------------------------------------------

def extract_semantics(doc) -> list:
    """Extract thread/diameter/tolerance callouts from TEXT/MTEXT/DIMENSION.

    Returns [{kind, value, text, entity, position}] -- deterministic text
    parsing, no guessing. `kind` in {thread, diameter, tolerance, note}.
    """
    out = []
    msp = doc.modelspace()

    def push(kind, value, text, pos):
        out.append({"kind": kind, "value": value, "text": text,
                    "position": [round(c, 3) for c in pos] if pos else None})

    for e in msp.query("TEXT MTEXT"):
        text = (e.dxf.get("text", "") or "").strip()
        if not text:
            continue
        pos = None
        try:
            p = e.dxf.get("insert", None)
            pos = (p[0], p[1], 0) if p else None
        except Exception:  # noqa: BLE001
            pass
        for m in _RE_THREAD.finditer(text):
            push("thread", f"M{m.group(1)}x{m.group(2)}", text, pos)
        for m in _RE_DIAMETER.finditer(text):
            push("diameter", float(m.group(1)), text, pos)
        for m in _RE_TOLERANCE.finditer(text):
            push("tolerance", f"{m.group(1)}/{m.group(2)}", text, pos)
        if not (_RE_THREAD.search(text) or _RE_DIAMETER.search(text)
                or _RE_TOLERANCE.search(text)):
            push("note", text, text, pos)

    for e in msp.query("DIMENSION"):
        mtext = (e.dxf.get("text", "") or "").strip()
        if mtext and mtext != "<>":
            pos = None
            try:
                p = e.dxf.get("def_point", None)
                pos = (p[0], p[1], 0) if p else None
            except Exception:  # noqa: BLE001
                pass
            for m in _RE_THREAD.finditer(mtext):
                push("thread", f"M{m.group(1)}x{m.group(2)}", mtext, pos)
            for m in _RE_DIAMETER.finditer(mtext):
                push("diameter", float(m.group(1)), mtext, pos)
    return out


# --------------------------------------------------------------------------
# Minimal DXF -> SVG renderer (轻依赖: no PIL / matplotlib)
# --------------------------------------------------------------------------

def _iter_leaf_entities(entities):
    """Yield leaf render entities, expanding block refs / dimensions in order.

    INSERT / DIMENSION / LEADER / TOLERANCE / MLEADER 等容器通过
    virtual_entities() 展开为带变换的叶子实体（嵌套块引用递归展开）；
    展开失败的容器整体跳过。迭代器栈保持文档顺序、避免深递归。
    """
    stack = [iter(entities)]
    while stack:
        try:
            e = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        t = e.dxftype()
        if t in _EXPANDABLE:
            try:
                stack.append(iter(e.virtual_entities()))
            except Exception:  # noqa: BLE001
                continue
        else:
            yield e


def _is_defpoint(e) -> bool:
    try:
        return (e.dxf.get("layer", "") or "").strip().lower() in _DEFPOINT_LAYERS
    except Exception:  # noqa: BLE001
        return False


def _polyline_pts(e):
    """LWPOLYLINE 顶点 [(x, y, bulge), ...]（DXF 帧，y 向上）。"""
    return e.get_points(format="xyb")


def _ellipse_points(e, sag: float | None = None):
    """Sample an ELLIPSE as a polyline (DXF 帧 y 向上), respecting arc params.

    sag 给定时按弦高容差自适应加密采样（大椭圆也保持平滑），否则退化为
    每整圆 48 段（用于边界估算足够）。
    """
    import ezdxf.math as zm
    try:
        c = e.dxf.center
        major = zm.Vec3(e.dxf.major_axis)
        ratio = float(e.dxf.ratio) if e.dxf.get("ratio") else 1.0
    except Exception:  # noqa: BLE001
        return []
    # unit minor axis = rotate(major, -90deg), length = |major| * ratio
    mj = zm.Vec3(major)
    ml = mj.magnitude
    if ml <= 1e-9:
        return []
    hu = mj / ml                     # major unit
    # minor unit = hu rotated by -90 deg (CCW in DXF; Y-flip keeps conjugation)
    mn = zm.Vec3(-hu.y, hu.x, 0) * ratio
    a0, a1 = 0.0, math.tau
    sp = e.dxf.get("start_param", None)
    ep = e.dxf.get("end_param", None)
    if sp is not None and ep is not None:
        a0, a1 = float(sp), float(ep)
    n = max(2, int(abs(a1 - a0) / (math.tau / 48)) + 1)
    if sag:
        n = max(n, min(2048, int(2 * math.pi * ml / sag) + 1))
    out = []
    for i in range(n + 1):
        t = a0 + (a1 - a0) * i / n
        p = zm.Vec3(c.x, c.y, 0) + hu * math.cos(t) * ml + mn * math.sin(t) * ml
        out.append((p.x, p.y))
    return out


def _spline_points(e, sag: float | None = None):
    """Approximate a SPLINE by a polyline.

    sag=None 时返回控制点（控制多边形包住曲线，供边界计算用）；
    sag 给定时用 ezdxf flattening 按弦高误差展平真实曲线（供渲染）。
    仅含拟合点（fit points）而无控制点的样条回退用拟合点。
    """
    import ezdxf.math as zm
    try:
        if sag is not None and hasattr(e, "flattening"):
            pts = [zm.Vec3(p) for p in e.flattening(sag)]
            if len(pts) >= 2:
                return [(p.x, p.y) for p in pts]
        ctrl = [zm.Vec3(p) for p in e.control_points]
        if len(ctrl) < 2:
            ctrl = [zm.Vec3(p) for p in e.fit_points]
    except Exception:  # noqa: BLE001
        return []
    if len(ctrl) < 2:
        return []
    return [(p.x, p.y) for p in ctrl]


def _pts_d(coords, fmt, closed: bool = False) -> str:
    """顶点序列 -> 合并路径的一个子路径（M/L 折线）。"""
    if len(coords) < 2:
        return ""
    d = "M" + fmt(coords[0][0]) + " " + fmt(coords[0][1])
    for x, y in coords[1:]:
        d += "L" + fmt(x) + " " + fmt(y)
    if closed:
        d += "Z"
    return d


def _polyline_d(pts, fmt, closed) -> str:
    """LWPOLYLINE 顶点（含 bulge 圆弧段）-> 子路径 d（显示帧，Y 已翻转）。"""
    if len(pts) < 2:
        return ""
    segs = ["M" + fmt(pts[0][0]) + " " + fmt(-pts[0][1])]
    for i in range(1, len(pts)):
        p = pts[i]
        prev = pts[i - 1]
        bulge = p[2] if len(p) > 2 else 0.0
        if bulge:
            dx, dy = p[0] - prev[0], p[1] - prev[1]
            d = math.hypot(dx, dy)
            if d > 1e-9:
                theta = 4.0 * math.atan(bulge)
                r = d * (1.0 + bulge * bulge) / (4.0 * abs(bulge))
                large = 1 if abs(math.degrees(theta)) > 180 else 0
                # DXF 正 bulge = 逆时针（y 向上）；Y 翻转到屏幕坐标系后
                # 该弧应取 sweep=1（负 bulge 取 0），否则圆弧会画到实体
                # 错误的另一侧（倒角/圆角等被镜像）。
                sweep = 1 if bulge > 0 else 0
                segs.append("A" + fmt(r) + " " + fmt(r) + " 0 " + str(large)
                            + " " + str(sweep) + " " + fmt(p[0]) + " "
                            + fmt(-p[1]))
                continue
        segs.append("L" + fmt(p[0]) + " " + fmt(-p[1]))
    if closed:
        segs.append("Z")
    return "".join(segs)


def _robust_extent(vals):
    """离群点鲁棒的值域：(lo, hi)。

    展开 INSERT/DIMENSION 后常带出被挪出图幅的旧标注/废弃视图（实测咖啡机
    图纸 x≈-123 万的 34 个实体 vs 主体 ~7000 宽），min/max 会被它们撑爆。
    用中位数 ± k·MAD 过滤；剔除比例过高（>5%，可能是真实的多视图/多图纸
    布局）时回退全量范围。样本量大时抽样降开销。
    """
    import statistics
    if len(vals) < 64:
        return min(vals), max(vals)
    if len(vals) > 200_000:                     # 统计意义不变，省排序开销
        vals = vals[::max(1, len(vals) // 200_000)]
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals)
    if mad <= 0:
        return min(vals), max(vals)
    k = 8.0
    lo, hi = med - k * mad, med + k * mad
    inside = [v for v in vals if lo <= v <= hi]
    if len(inside) < len(vals) * 0.95:
        return min(vals), max(vals)
    return min(inside), max(inside)


_RENDERABLE = ("LINE", "CIRCLE", "ARC", "LWPOLYLINE", "ELLIPSE", "SPLINE",
               "POINT", "TEXT", "MTEXT", "SOLID", "HATCH")


def _entity_extent(e, t):
    """实体紧致 extent (minx, maxx, miny, maxy)；取不到返回 None。"""
    try:
        if t == "LINE":
            s, en = e.dxf.start, e.dxf.end
            return (min(s.x, en.x), max(s.x, en.x),
                    min(s.y, en.y), max(s.y, en.y))
        if t == "CIRCLE":
            c, r = e.dxf.center, e.dxf.radius
            return (c.x - r, c.x + r, c.y - r, c.y + r)
        if t == "ARC":
            return _arc_extent(e)
        if t in ("TEXT", "MTEXT"):
            p = e.dxf.get("insert", None)
            return (p[0], p[0], p[1], p[1]) if p else None
        if t == "LWPOLYLINE":
            pts = _polyline_pts(e)
            if not pts:
                return None
            return (min(p[0] for p in pts), max(p[0] for p in pts),
                    min(p[1] for p in pts), max(p[1] for p in pts))
        if t in ("ELLIPSE", "SPLINE"):
            pts = _ellipse_points(e) if t == "ELLIPSE" else _spline_points(e)
            if not pts:
                return None
            return (min(p[0] for p in pts), max(p[0] for p in pts),
                    min(p[1] for p in pts), max(p[1] for p in pts))
        if t == "POINT":
            p = e.dxf.get("location", None)
            return (p[0], p[0], p[1], p[1]) if p else None
        if t == "SOLID":
            vs = e.vertices()
            if not vs:
                return None
            return (min(v[0] for v in vs), max(v[0] for v in vs),
                    min(v[1] for v in vs), max(v[1] for v in vs))
        if t == "HATCH":
            xs = [v[0] for p in e.paths for v in p.vertices]
            ys = [v[1] for p in e.paths for v in p.vertices]
            if not xs:
                return None
            return (min(xs), max(xs), min(ys), max(ys))
    except Exception:  # noqa: BLE001
        return None
    return None


def _arc_extent(e):
    """ARC 实际扫过角度的紧致包络 (minx, maxx, miny, maxy)。

    边界若用整圆包络 cx±r/cy±r，一条大半径小角度的圆弧（如 R1296 的
    构造弧）会把 viewBox 撑大数倍；按起止角与扫过路径上的轴向极值角
    (0/90/180/270°) 计算真实范围。
    """
    c, r = e.dxf.center, e.dxf.radius
    a0 = math.radians(e.dxf.start_angle) % math.tau
    a1 = math.radians(e.dxf.end_angle) % math.tau
    sweep = (a1 - a0) % math.tau
    angs = [a0, a1] + [k * math.pi / 2 for k in range(4)
                       if (k * math.pi / 2 - a0) % math.tau <= sweep]
    xs = [c.x + r * math.cos(a) for a in angs]
    ys = [c.y + r * math.sin(a) for a in angs]
    return min(xs), max(xs), min(ys), max(ys)


def dxf_to_svg(doc, max_entities: int = 1000000) -> str:
    """Render modelspace 2D entities to an SVG string (merged-path form).

    覆盖 LINE / CIRCLE / ARC / LWPOLYLINE(含 bulge) / ELLIPSE / SPLINE /
    POINT / TEXT / MTEXT / SOLID / HATCH；INSERT / DIMENSION / LEADER 等
    容器实体先经 _iter_leaf_entities 展开再渲染——大装配图纸的标注几何
    与块引用内容由此进入视图。

    输出为合并路径架构：全部描边实体合成 1 个 <path>、剖面线与实心填充
    各 1 个 <path>、圆独立元素、文字独立元素——10 万级实体的图纸 DOM 节点
    从 ~10 万降到 ~1 万，浏览器才挂得动（此前 15.9MB / 8.3 万元素直接
    冻结标签页）。坐标紧凑格式化（定长小数 + 去尾零）压字节。Y 翻转显示。
    **所有可渲染实体均完整渲染**；viewBox 仅用实体中心点的鲁棒范围
    （中位数 ± k·MAD，见 _robust_extent）做默认取景框，不删除任何内容。
    max_entities 为渲染实体上限（病态文件保护，audit M4）。
    """
    from ezdxf.tools.text import plain_mtext

    msp = doc.modelspace()
    leaves = list(_iter_leaf_entities(msp))

    # -- pass A: 收集可渲染实体 + 逐实体 extent -----------------------------
    # Defpoints 定义点在收集阶段即剔除（不进统计也不渲染）
    ents = []  # [(entity, type, extent|None)]
    raw_xs, raw_ys = [], []
    for e in leaves:
        t = e.dxftype()
        if t == "POINT" and _is_defpoint(e):
            continue
        if t not in _RENDERABLE:
            continue
        ext = _entity_extent(e, t)
        ents.append((e, t, ext))
        if ext is not None:
            raw_xs += [ext[0], ext[1]]
            raw_ys += [ext[2], ext[3]]
    if not ents:
        raise DrawingError("图纸上没有可渲染的二维实体")

    # -- 矢量化 HATCH 边界（距离容差按粗略图幅自适应）并回填 extent ---------
    rough = max(max(raw_xs) - min(raw_xs), max(raw_ys) - min(raw_ys), 1.0) \
        if raw_xs else 1.0
    for i, (e, t, ext) in enumerate(ents):
        if t != "HATCH":
            continue
        try:
            e.paths.edge_to_polyline_paths(distance=min(rough / 1000.0, 0.5))
        except Exception:  # noqa: BLE001
            continue
        ents[i] = (e, t, _entity_extent(e, t))

    # -- pass B: viewBox 取景（仅决定默认缩放，不剔除任何实体） ------------
    # 用实体中心点的鲁棒范围（中位数 ± k·MAD）作为默认取景框，避免个别
    # 远离图幅的废弃标注/旧视图把整张图撑到极小；但**所有可渲染实体都
    # 完整渲染**——完整性优先于取景，用户可自由缩放/平移查看全部内容。
    if not ents:
        raise DrawingError("图纸上没有可渲染的二维实体")
    cxs, cys = [], []
    for _, _, ext in ents:
        if ext is not None:
            cxs.append((ext[0] + ext[1]) / 2)
            cys.append((ext[2] + ext[3]) / 2)
    minx, maxx = _robust_extent(cxs)
    miny, maxy = _robust_extent(cys)
    w = max(maxx - minx, 1.0)
    h = max(maxy - miny, 1.0)
    pad = 0.05
    vb = f"{minx - w * pad:.3f} {-maxy - h * pad:.3f} {w * (1 + 2 * pad):.3f} {h * (1 + 2 * pad):.3f}"

    # 坐标精度按图幅自适应：mm 级图幅 2 位小数足够，小图提高精度
    maxdim = max(w, h)
    dec = 2 if maxdim >= 10 else (3 if maxdim >= 1 else 4)

    def fmt(v: float) -> str:
        s = f"{v:.{dec}f}"
        return s.rstrip("0").rstrip(".") if "." in s else s

    # -- render pass: emit（合并路径架构，仅渲染 kept 实体） -----------------
    strokes: list[str] = []      # 描边子路径（LINE/ARC/PLINE/ELLIPSE/SPLINE）
    circles: list[str] = []      # CIRCLE
    hatch_ds: list[str] = []     # HATCH 边界（半透明填充）
    solid_ds: list[str] = []     # SOLID（实心填充）
    texts: list[str] = []
    emitted = 0
    sag = min(maxdim / 4000.0, 1.0)   # 曲线弦高容差：放大后不失真

    for e, t, _ext in ents:
        if emitted >= max_entities:
            import sys
            print(f"[drawing] WARN 渲染实体数达上限 {max_entities}，"
                  f"超出部分未渲染（图纸可能不完整）。", file=sys.stderr)
            break
        try:
            if t == "LINE":
                s, en = e.dxf.start, e.dxf.end
                strokes.append("M" + fmt(s.x) + " " + fmt(-s.y)
                               + "L" + fmt(en.x) + " " + fmt(-en.y))
            elif t == "CIRCLE":
                c = e.dxf.center
                circles.append('<circle cx="' + fmt(c.x) + '" cy="'
                               + fmt(-c.y) + '" r="' + fmt(e.dxf.radius)
                               + '" vector-effect="non-scaling-stroke"/>')
            elif t == "ARC":
                c, r = e.dxf.center, e.dxf.radius
                a0 = math.radians(e.dxf.start_angle)
                a1 = math.radians(e.dxf.end_angle)
                x0, y0 = c.x + r * math.cos(a0), -(c.y + r * math.sin(a0))
                x1, y1 = c.x + r * math.cos(a1), -(c.y + r * math.sin(a1))
                large = 1 if (a1 - a0) % (2 * math.pi) > math.pi else 0
                # DXF ARC 恒为逆时针（y 向上）；已做 Y 翻转 → SVG sweep 取 0
                strokes.append("M" + fmt(x0) + " " + fmt(y0)
                               + "A" + fmt(r) + " " + fmt(r) + " 0 "
                               + str(large) + " 0 " + fmt(x1) + " " + fmt(y1))
            elif t == "LWPOLYLINE":
                d = _polyline_d(_polyline_pts(e), fmt,
                                closed=bool(getattr(e, "closed", False)))
                if d:
                    strokes.append(d)
            elif t == "ELLIPSE":
                # 仅整椭圆才闭合；椭圆弧（start/end_param 不等整圈）必须
                # 开放，否则会把弧错误连成整椭圆、画出不存在的弦。
                sp = e.dxf.get("start_param")
                ep = e.dxf.get("end_param")
                closed_e = (sp is None or ep is None
                            or abs(float(ep) - float(sp) - math.tau) < 1e-3)
                d = _pts_d([(x, -y) for (x, y) in _ellipse_points(e, sag)],
                           fmt, closed=closed_e)
                if d:
                    strokes.append(d)
            elif t == "SPLINE":
                # 按图幅自适应弦高展平真实曲线，而非控制点折线；
                # 闭合样条（剖面轮廓常见）须闭合，否则缺一段连线。
                closed_s = False
                try:
                    closed_s = bool(getattr(e, "closed", False))
                except Exception:  # noqa: BLE001
                    pass
                d = _pts_d([(x, -y) for (x, y) in _spline_points(e, sag=sag)],
                           fmt, closed=closed_s)
                if d:
                    strokes.append(d)
            elif t == "POINT":
                p = e.dxf.get("location", None)
                if p is not None:
                    # 点标记用小尺寸实心圆（封顶，避免大图被 maxdim 线性放大成巨点）
                    r_pt = min(maxdim * 0.0015, 2.0)
                    circles.append('<circle cx="' + fmt(p[0]) + '" cy="'
                                   + fmt(-p[1]) + '" r="' + fmt(r_pt)
                                   + '" fill="#dfe3ea" stroke="none"/>')
            elif t == "SOLID":
                # vertices() 已是正确绘制顺序（DXF SOLID 三/四点）
                d = _pts_d([(v[0], -v[1]) for v in e.vertices()], fmt,
                           closed=True)
                if d:
                    solid_ds.append(d)
            elif t == "HATCH":
                for p in e.paths:
                    d = _pts_d([(v[0], -v[1]) for v in p.vertices], fmt,
                               closed=True)
                    if d:
                        hatch_ds.append(d)
            elif t in ("TEXT", "MTEXT"):
                raw = e.dxf.get("text", "") or ""
                # TEXT 用 height，MTEXT 用 char_height；缺失时按图幅比例兜底
                hgt = (e.dxf.get("height", 0) or 0) if t == "TEXT" \
                    else (e.dxf.get("char_height", 0) or 0)
                if t == "MTEXT":
                    # \H 绝对高度覆盖（如 \H7.50;）：整段取最大有效高度，
                    # 否则强调段字号被基值压小
                    for hm in re.finditer(r"\\H(\d+(?:\.\d+)?);", raw):
                        hv = float(hm.group(1))
                        if hv > hgt:
                            hgt = hv
                    raw = plain_mtext(raw).replace("\n", " ")
                if hgt <= 0:
                    hgt = maxdim * 0.004
                text = raw.replace("&", "&amp;").replace("<", "&lt;") \
                    .replace(">", "&gt;")
                p = e.dxf.get("insert", None)
                if p is not None and text.strip():
                    x, y = p[0], -p[1]
                    # 对齐：DXF 锚点语义 -> SVG text-anchor / 基线偏移。
                    # SVG y 向下，字形顶=基线-0.8h、底=基线+0.2h。
                    anchor = "start"
                    dy = 0.0
                    if t == "TEXT":
                        ha = e.dxf.get("halign", 0) or 0
                        va = e.dxf.get("valign", 0) or 0
                        if ha:  # 非左对齐时锚点在 align_point
                            ap = e.dxf.get("align_point", None)
                            if ap is not None:
                                x, y = ap[0], -ap[1]
                        anchor = ("middle" if ha in (1, 4)
                                  else "end" if ha == 2 else "start")
                        if va == 1:      # 底对齐
                            dy = -0.2 * hgt
                        elif va == 2:    # 垂直居中
                            dy = 0.3 * hgt
                        elif va == 3:    # 顶对齐
                            dy = 0.8 * hgt
                    else:  # MTEXT attachment_point 1..9（列:1左2中0右；行:顶/中/底）
                        apn = e.dxf.get("attachment_point", 1) or 1
                        col = apn % 3
                        anchor = ("middle" if col == 2
                                  else "end" if col == 0 else "start")
                        row = 0 if apn <= 3 else (1 if apn <= 6 else 2)
                        dy = (0.8 if row == 0 else 0.3 if row == 1
                              else -0.2) * hgt
                    y += dy
                    # 文字独立实心样式：显式 fill（浅色实心）+ stroke="none"，
                    # 否则会继承根 <g> 的 fill="none" 变成空心描边字，小图
                    # 尚可，大图放大后轮廓叠成一坨无法辨识。
                    attrs = ['x="' + fmt(x) + '" y="' + fmt(y)
                             + '" font-size="' + fmt(hgt) + '"',
                             'fill="#cdd3df" stroke="none"']
                    if anchor != "start":
                        attrs.append('text-anchor="' + anchor + '"')
                    rot = e.dxf.get("rotation", 0.0) or 0.0
                    if t == "MTEXT":
                        # text_direction 向量优先于 rotation
                        td = e.dxf.get("text_direction", None)
                        if td is not None and (td[0] or td[1]):
                            rot = math.degrees(math.atan2(td[1], td[0]))
                    if abs(rot) > 0.5:  # Y 翻转坐标系的旋转取负
                        attrs.append('transform="rotate(' + fmt(-rot) + ' '
                                     + fmt(x) + ' ' + fmt(y) + ')"')
                    texts.append("<text " + " ".join(attrs) + ">"
                                 + text + "</text>")
        except Exception:  # noqa: BLE001
            continue
        emitted += 1

    # stroke-width 固定为屏幕像素（经 vector-effect 在元素级生效，见下），
    # 不再随图幅 maxdim 线性放大——此前大图线条粗到淹没文字是"放大成一坨"
    # 的主因之一。
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="' + vb
           + '" stroke="#dfe3ea" stroke-width="1.2"'
           + ' fill="none" font-family="monospace">']
    if solid_ds:
        out.append('<path fill="#dfe3ea" fill-opacity="0.85" '
                   'fill-rule="evenodd" stroke="none" d="'
                   + "".join(solid_ds) + '"/>')
    if hatch_ds:
        out.append('<path fill="#dfe3ea" fill-opacity="0.25" '
                   'fill-rule="evenodd" stroke="none" d="'
                   + "".join(hatch_ds) + '"/>')
    if strokes:
        # vector-effect 非继承属性，必须在元素级声明：描边粗细锁定屏幕
        # 像素（1.2px），缩放/放大看图时线宽恒定、不再随图幅变粗淹没文字。
        out.append('<path vector-effect="non-scaling-stroke" d="'
                   + "".join(strokes) + '"/>')
    if circles:
        out.append("".join(circles))
    out.extend(texts)
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------
# Import pipeline (cached, R8/R17)
# --------------------------------------------------------------------------

def import_drawing(input_path: str, out_dir: str) -> dict:
    """Import DXF (native) or DWG (via ODA) -> semantics + SVG, cached.

    Writes out_dir/{drawing.json, view.svg} (cache semantics; the drawing
    cache key is the SOURCE file sha256, stored in the result) and returns
    {"schema_version", "source_file", "source_sha256", "oda_used",
     "semantics", "entity_count"}.
    """
    import hashlib
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"No such file: {input_path}")
    ext = os.path.splitext(input_path)[1].lower()
    if ext not in (".dxf", ".dwg"):
        raise ValueError(f"unsupported drawing format: {ext} (DXF/DWG only)")

    sha = hashlib.sha256()
    with open(input_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            sha.update(chunk)
    key = sha.hexdigest()

    os.makedirs(out_dir, exist_ok=True)
    dxf_path = input_path if ext == ".dxf" else None
    oda_used = False
    if dxf_path is None:
        dxf_path = _dwg_to_dxf(input_path, out_dir)   # cached dwg_converted.dxf
        oda_used = True

    try:
        from ezdxf import recover
        doc, _ = recover.readfile(dxf_path)
    except ImportError:
        try:
            doc = ezdxf.readfile(dxf_path)
        except Exception as e:  # noqa: BLE001
            raise DrawingError(f"无法读取图纸 {input_path}: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise DrawingError(f"无法读取图纸 {input_path}: {e}") from e

    semantics = extract_semantics(doc)
    svg = dxf_to_svg(doc)
    with open(os.path.join(out_dir, "view.svg"), "w", encoding="utf-8") as f:
        f.write(svg)

    entity_types = {}
    layers = []
    for e in doc.modelspace():
        et = e.dxftype()
        entity_types[et] = entity_types.get(et, 0) + 1
        lay = (e.dxf.get("layer", "0") or "0")
        if lay not in layers:
            layers.append(lay)

    result = {
        "schema_version": DRAWING_SCHEMA_VERSION,
        "source_file": os.path.basename(input_path),
        "source_sha256": key,
        "oda_used": oda_used,
        "semantics": semantics,
        "entity_count": len(doc.modelspace()),
        "entity_types": entity_types,
        "layers": layers,
    }
    with open(os.path.join(out_dir, "drawing.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result
