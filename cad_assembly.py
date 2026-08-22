"""Assembly parsing & Template+Matrix export (Phase A, ADR-0002 D3).

Turns an assembly STEP into the decoupled data model the Web frontend
consumes (see docs/architecture/copilot-vision.md 模块二 / D3):

    manifest = {
      schema_version: 1                  # R7 版本化语义中枢起点
      source_file, source_sha256,        # R8 缓存失效键
      units: "mm",                       # R3 内部一律 mm
      root: <tree node>,                 # 装配树（前端装配树 UI）
      templates: [...]                   # 去重零件模板（D3 Template）
    }

    tree node = {
      id, name, type: "assembly"|"part", template: <template id (part only)>,
      matrix: 3x4 世界矩阵（沿父链累积）, children: [...]
    }

    template = { id, name, color, gltf: <relpath (导出后)> }

Writing a cache dir (``build_cache``) produces exactly the layout from the
vision doc's ``workspace/cache``:

    out_dir/
    ├── tree_structure.json      # manifest 本体
    └── gltf_library/            # 每个唯一模板一份 glTF（天然去重：
        ├── t0.gltf + t0.bin     #   实例共享模板，不复制几何）
        └── t1.gltf + t1.bin

Degenerate inputs (R2 兜底): a flat single-solid STEP becomes a one-part
tree; a multi-root STEP (no assembly structure) gets a synthetic root named
after the file. Unnamed parts get deterministic fallback names (Part_1...).

Per-template STEP export (for D10 version management) is a future increment;
this module currently exports glTF only.

Phase B additions:
  * explosion vectors -- every non-root node gets ``explode`` [dx,dy,dz]:
    direction = child subtree centroid - parent centroid (fallback: parent
    bbox longest axis, alternating sign), magnitude >= 0.3 * parent size.
    Frontend accumulates along the ancestor chain and scales by the slider
    ratio (multi-level explosion, copilot-vision 模块二).
  * per-template features -- ``features/tN.json`` (metadata list from
    feature_locator classification via feature_picker.collect_feature_solids)
    + ``features/tN.gltf`` (one named node per feature id) for feature-level
    picking overlays in the Web frontend (Phase B 拾取 API 化).

OCP quirks are documented in tests/_assembly_helpers.py (spike learnings);
this module is the production path for the same functionality.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading

import cad_core

SCHEMA_VERSION = 4   # v4: 模板/节点名 GBK 乱码还原（复刻 OCCT UTF-8 解码器，
                     #     基于源 STEP 原始字节建 乱码->正确名 映射）
                     # v3: features glTF 网格偏差与模板导出对齐（overlay 共面）
                     # v2: + explode vectors, + parts/*.step, + features/*


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _deflection_for(shape) -> float:
    """Size-relative deflection, same formula as feature_picker's retired
    mesh path / the historical make_preview
    (audit M2/A3: max(min(maxdim/800, 0.5), 1e-5))."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    try:
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    except TypeError:
        pmin, pmax = box.Get()
        xmin, ymin, zmin = pmin.X(), pmin.Y(), pmin.Z()
        xmax, ymax, zmax = pmax.X(), pmax.Y(), pmax.Z()
    size = [xmax - xmin, ymax - ymin, zmax - zmin]
    maxdim = max(size) or 1.0
    return max(min(maxdim / 800.0, 0.5), 1e-5)


def _label_name(label):
    """Label name, or None for missing/OCCT-junk names (R2 fallback).

    Flat STEP files carry translator-generated labels like
    "Open CASCADE STEP translator 7.9 1" -- treat those as unnamed so parts
    get deterministic fallback names instead.
    """
    from OCP.TDataStd import TDataStd_Name
    from OCP.TCollection import TCollection_ExtendedString
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        s = TCollection_ExtendedString(attr.Get()).ToExtString()
        if s and not s.startswith("Open CASCADE"):
            return s
    return None


# --------------------------------------------------------------------------
# GBK 乱码还原：复刻 OCCT 读取 STEP 字符串的 UTF-8 解码（NCollection_UtfIterator）
# --------------------------------------------------------------------------
# SolidWorks 导出 STEP 时把中文名按 Windows 代码页（GBK）写入文件；OCCT 读取器
# 默认按 UTF-8 解码（Resource_FormatType_UTF8 -> TCollection_ExtendedString(bytes,
# isMultiByte=true)），两套编码错位产生乱码。
#
# 解码器（NCollection_UtfIterator::readUTF8，旧版 6 字节 UTF-8）：
#   k = UTF8_BYTES_MINUS_ONE[b0]         # 额外字节数（0..5）
#   逐个字节 <<6 累加后减去 offsetsFromUTF8[k] -> 符号码点
#   任一符号码点 > 0x10FFFF 时 ConvertToUnicode 返回 false -> 整串回退逐字节 Latin-1
#
# 此外 STEP 读取器把字符串字面量（含首尾撇号 "'...'"）整体交给解码器，之后跳过
# 开头撇号并按长度-1 裁掉末字符；若末尾多字节序列吞掉了撇号，被裁掉的是真实名字
# 的末字符（如 "平头刺针" 末尾 EB 被裁）。
#
# 据此对每条 GBK 名字可正向算出 OCCT 最终输出的乱码串，构建 乱码->正确名 映射表，
# 在模板/树节点名读取后直接替换（对任何 GBK STEP 均有效，不依赖上游导出设置）。
UTF8_BYTES_MINUS_ONE = [0] * 192 + [1] * 32 + [2] * 16 + [3] * 8 + [4] * 4 + [5] * 4
OFFSETS_FROM_UTF8 = (0x00000000, 0x00003080, 0x000E2080,
                     0x03C82080, 0xFA082080, 0x82082080)
UTF32_MAX_LEGAL = 0x10FFFF


def _occt_decode(raw: bytes) -> str | None:
    """精确复刻 OCCT readUTF8；整串任一符号非法返回 None（应整体回退 Latin-1）。"""
    out = []
    i, n = 0, len(raw)
    while i < n:
        b = raw[i]
        k = UTF8_BYTES_MINUS_ONE[b]
        # readUTF8 无条件读 k+1 字节（不越界保护）；末尾不足时以 0x00 补齐模拟 NUL
        seg = raw[i:i + k + 1]
        if len(seg) < k + 1:
            seg = seg + b"\x00" * (k + 1 - len(seg))
        code = 0
        for byte in seg:
            code = ((code << 6) + byte) & 0xFFFFFFFF
        code = (code - OFFSETS_FROM_UTF8[k]) & 0xFFFFFFFF
        if code > UTF32_MAX_LEGAL:
            return None
        if code == 0:
            break  # 迭代器遇 NUL 符号终止（尾随截断序列被丢弃）
        out.append(chr(code))
        i += k + 1
    return "".join(out)


def _occt_name(raw: bytes) -> str:
    """OCCT 读取该字符串字面量后得到的最终名字（模拟 cleanText 链路）。"""
    full = b"'" + raw + b"'"
    dec = _occt_decode(full)
    if dec is None:
        return "".join(chr(x) for x in full)[1:-1]
    return dec[1:-1]


def build_mojibake_fixmap(step_path: str) -> dict:
    """扫描 STEP 全部引号字符串，返回 {乱码名: 正确GBK名} 映射（仅含非 ASCII 名）。"""
    data = open(step_path, "rb").read()
    fixmap = {}
    for raw in set(re.findall(rb"'((?:[^']|'')*)'", data)):
        unesc = raw.replace(b"''", b"'")
        try:
            correct = unesc.decode("gbk")
        except (UnicodeDecodeError, ValueError):
            continue
        if correct.isascii():
            continue
        moji = _occt_name(unesc)
        if moji not in fixmap:
            fixmap[moji] = correct
    return fixmap


def _label_entry(label) -> str:
    from OCP.TDF import TDF_Tool
    from OCP.TCollection import TCollection_AsciiString
    s = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, s)
    return s.ToCString()


def _new_xcaf_doc():
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.TCollection import TCollection_ExtendedString
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    app.NewDocument(TCollection_ExtendedString("XmlOcaf"), doc)
    return doc


def _matrix_rows(trsf) -> list:
    return [[round(trsf.Value(r, c), 6) for c in range(1, 5)] for r in range(1, 4)]


def _template_metrics(shape):
    """(centroid xyz, bbox min xyz, bbox max xyz, volume) of a template shape."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    p = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, p)
    c = p.CentreOfMass()
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    try:
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    except TypeError:
        pmin, pmax = box.Get()
        xmin, ymin, zmin = pmin.X(), pmin.Y(), pmin.Z()
        xmax, ymax, zmax = pmax.X(), pmax.Y(), pmax.Z()
    return ((c.X(), c.Y(), c.Z()), (xmin, ymin, zmin), (xmax, ymax, zmax),
            p.Mass() or 1.0)


def _apply_matrix(m: list, pt: tuple) -> tuple:
    """Apply a 3x4 row-major matrix to a point."""
    return tuple(m[i][0] * pt[0] + m[i][1] * pt[1] + m[i][2] * pt[2] + m[i][3]
                 for i in range(3))


def _union_bbox(a, b):
    if a is None:
        return b
    return (tuple(min(x, y) for x, y in zip(a[0], b[0])),
            tuple(max(x, y) for x, y in zip(a[1], b[1])))


def _compute_explosion(root: dict, templates: dict) -> None:
    """Attach relative explode vectors to every non-root node (in place).

    Bottom-up pass computes each node's volume-weighted world centroid and
    world bbox; top-down pass sets child.explode = dir * mag where
    dir = child_center - parent_center (fallback: parent bbox longest axis,
    alternating sign by child index) and mag is clamped to
    [0.3, 0.8] * parent_maxdim.
    """
    tinfo = templates  # tid -> {centroid, bbox, volume}

    def bottom_up(node):
        """Returns (world_center, world_bbox, subtree_volume)."""
        if node["type"] == "part":
            t = tinfo[node["template"]]
            c = _apply_matrix(node["matrix"], t["centroid"])
            bmin = _apply_matrix(node["matrix"], t["bbox"][0])
            bmax = _apply_matrix(node["matrix"], t["bbox"][1])
            bbox = (tuple(min(a, b) for a, b in zip(bmin, bmax)),
                    tuple(max(a, b) for a, b in zip(bmin, bmax)))
            node["_center"] = c
            node["_bbox"] = bbox
            return c, bbox, t["volume"]
        center = (0.0, 0.0, 0.0)
        bbox = None
        vol = 0.0
        for ch in node["children"]:
            c, b, v = bottom_up(ch)
            center = tuple(x + y * v for x, y in zip(center, c))
            bbox = _union_bbox(bbox, b)
            vol += v
        if vol <= 0:
            vol = 1.0
        center = tuple(x / vol for x in center)
        node["_center"] = center
        node["_bbox"] = bbox
        return center, bbox, vol

    bottom_up(root)

    def top_down(node):
        kids = node.get("children", [])
        if not kids:
            return
        span = (tuple(hi - lo for lo, hi in zip(node["_bbox"][0], node["_bbox"][1]))
                if node["_bbox"] else (1.0, 1.0, 1.0))
        maxdim = max(span) or 1.0
        axis_idx = span.index(max(span))
        for i, ch in enumerate(kids):
            v = tuple(a - b for a, b in zip(ch["_center"], node["_center"]))
            dist = (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5
            if dist < 1e-6 * maxdim:
                # concentric children: spread along the parent's longest axis
                sign = 1.0 if i % 2 == 0 else -1.0
                d = [0.0, 0.0, 0.0]
                d[axis_idx] = sign
                mag = 0.5 * maxdim
            else:
                d = [x / dist for x in v]
                mag = min(max(dist, 0.3 * maxdim), 0.8 * maxdim)
            ch["explode"] = [round(x * mag, 4) for x in d]
            top_down(ch)

    top_down(root)

    def strip(node):
        node.pop("_center", None)
        node.pop("_bbox", None)
        for ch in node.get("children", []):
            strip(ch)

    strip(root)


# --------------------------------------------------------------------------
# Parse: STEP -> manifest (no file side effects)
# --------------------------------------------------------------------------

def parse_assembly(input_path: str) -> dict:
    """Parse an assembly STEP into the Template+Matrix manifest (dict).

    Raises ValueError/RuntimeError on unreadable files (same contract as
    cad_core.read_shape). Pure parse -- no files written.
    """
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
    from OCP.TDF import TDF_Label, TDF_LabelSequence
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.Quantity import Quantity_Color
    from OCP.gp import gp_Trsf

    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"No such file: {input_path}")
    ext = os.path.splitext(input_path)[1].lower()
    if ext not in (".step", ".stp"):
        raise ValueError(
            f"Unsupported assembly input format: '{ext}'. Supported: .step/.stp")

    doc = _new_xcaf_doc()
    rdr = STEPCAFControl_Reader()
    rdr.SetNameMode(True)
    rdr.SetColorMode(True)
    if rdr.ReadFile(input_path) != 1:
        raise ValueError(f"无法读取 CAD 文件: {input_path}（文件损坏或格式不支持）")
    if not rdr.Transfer(doc):
        raise ValueError(f"无法读取 CAD 文件: {input_path}（文件损坏或格式不支持）")

    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    templates = []            # [{id, name, color, entry, shape}]
    template_by_entry = {}
    nodes = []
    fallback_counter = [0]

    def get_template(ref_label, ref_entry):
        """Dedup by referred label: same template -> same id (D3)."""
        if ref_entry in template_by_entry:
            return template_by_entry[ref_entry]
        name = _label_name(ref_label)
        color = None
        qc = Quantity_Color()
        if ct.GetColor_s(ref_label, XCAFDoc_ColorType.XCAFDoc_ColorSurf, qc):
            color = [round(qc.Red(), 3), round(qc.Green(), 3), round(qc.Blue(), 3)]
        tid = f"t{len(templates)}"
        templates.append({
            "id": tid,
            "name": name,          # may be None -> fallback later
            "color": color,
            "entry": ref_entry,
            "shape": st.GetShape_s(ref_label),
        })
        template_by_entry[ref_entry] = tid
        return tid

    def fallback_name():
        fallback_counter[0] += 1
        return f"Part_{fallback_counter[0]}"

    def walk(label, depth, acc_trsf, parent_children):
        is_ref = st.IsReference_s(label)
        ref = None
        if is_ref:
            ref = TDF_Label()
            st.GetReferredShape_s(label, ref)
        if st.IsComponent_s(label):
            loc = st.GetLocation_s(label)
            acc_trsf = acc_trsf.Multiplied(loc.Transformation())
        src = ref if ref is not None else label

        own = _label_name(label)
        tpl_name = _label_name(src) if ref is not None else own
        # component labels carry OCCT reference markers "=>[...]" (spike
        # finding): prefer a real own name, else the template name
        name = (own if (own and not own.startswith("=>")) else tpl_name) or None

        is_asm = st.IsAssembly_s(src)
        node = {
            "id": f"n{len(nodes)}",
            "name": name,
            "type": "assembly" if is_asm else "part",
            "matrix": _matrix_rows(acc_trsf),
            "children": [],
        }
        nodes.append(node)
        parent_children.append(node)

        if is_asm:
            comps = TDF_LabelSequence()
            st.GetComponents_s(src, comps)
            for i in range(1, comps.Length() + 1):
                walk(comps.Value(i), depth + 1, acc_trsf, node["children"])
        else:
            entry = _label_entry(src)
            node["template"] = get_template(src, entry)

    roots = TDF_LabelSequence()
    st.GetFreeShapes(roots)
    tree_children = []
    for i in range(1, roots.Length() + 1):
        walk(roots.Value(i), 0, gp_Trsf(), tree_children)

    # GBK 乱码还原：SolidWorks 按 Windows 代码页写中文名，OCCT 按 UTF-8 误读产生
    # 乱码；用源文件原始字节建 乱码->正确名 映射，替换模板与树节点名。
    fixmap = build_mojibake_fixmap(input_path)
    if fixmap:
        for t in templates:
            fixed = fixmap.get(t["name"])
            if fixed:
                t["name"] = fixed
        for n in nodes:
            fixed = fixmap.get(n["name"])
            if fixed:
                n["name"] = fixed

    # R2 兜底：无名零件给确定性回退名
    for t in templates:
        if not t["name"]:
            t["name"] = fallback_name()
    for n in nodes:
        if not n["name"]:
            n["name"] = templates[
                int(n["template"][1:])]["name"] if n.get("template") else fallback_name()

    root = _synthetic_root(tree_children, input_path)

    # Phase B: multi-level explosion vectors (relative, per non-root node)
    tinfo = {}
    for t in templates:
        c, bmin, bmax, vol = _template_metrics(t["shape"])
        tinfo[t["id"]] = {"centroid": c, "bbox": (bmin, bmax), "volume": vol}
    _compute_explosion(root, tinfo)

    return {
        "schema_version": SCHEMA_VERSION,
        "source_file": os.path.basename(input_path),
        "source_sha256": _sha256_file(input_path),
        "units": "mm",
        "root": root,
        "templates": [{k: t[k] for k in ("id", "name", "color")} for t in templates],
        "_shapes": {t["id"]: t["shape"] for t in templates},  # internal, stripped on dump
    }


def _synthetic_root(children: list, input_path: str) -> dict:
    """1 root -> use it directly; 0/N roots -> synthetic file-named root (R2)."""
    if len(children) == 1:
        return children[0]
    stem = os.path.splitext(os.path.basename(input_path))[0]
    return {
        "id": "n_root",
        "name": stem,
        "type": "assembly",
        "matrix": _matrix_rows(__import__("OCP.gp", fromlist=["gp_Trsf"]).gp_Trsf()),
        "children": children,
    }


# --------------------------------------------------------------------------
# Export: per-template glTF (natural dedup: one file per unique template)
# --------------------------------------------------------------------------

def _export_template_gltf(shape, name, color, gltf_path: str) -> None:
    """Write ONE shape as a single-mesh textual glTF (+ .bin alongside).

    Per-template export sidesteps the RWGltf no-dedup quirk entirely: each
    unique part is written once and instances share it via the manifest.
    """
    from OCP.TDataStd import TDataStd_Name
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.RWGltf import RWGltf_CafWriter
    from OCP.TColStd import TColStd_IndexedDataMapOfStringString
    from OCP.Message import Message_ProgressRange
    from OCP.TCollection import TCollection_AsciiString

    doc = _new_xcaf_doc()
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    lbl = st.AddShape(shape, False)
    TDataStd_Name.Set_s(lbl, TCollection_ExtendedString(name))
    if color is not None:
        ct.SetColor(lbl, Quantity_Color(color[0], color[1], color[2], Quantity_TOC_RGB),
                    XCAFDoc_ColorType.XCAFDoc_ColorSurf)

    BRepMesh_IncrementalMesh(shape, _deflection_for(shape))
    w = RWGltf_CafWriter(TCollection_AsciiString(gltf_path), False)  # textual
    info = TColStd_IndexedDataMapOfStringString()
    if not w.Perform(doc, info, Message_ProgressRange()):
        raise RuntimeError(f"RWGltf_CafWriter.Perform failed: {gltf_path}")


# --------------------------------------------------------------------------
# Per-template feature export (Phase B 拾取 API 化)
# --------------------------------------------------------------------------

def _export_features_gltf(feature_solids: list, gltf_path: str,
                          deflection: float) -> None:
    """Write one named XCAF shape per feature as a textual glTF.

    Node names in the glTF are the feature ids (e.g. "#3", "#7.2", "P1"),
    so the frontend can map metadata entries to overlay meshes 1:1.

    ``deflection`` MUST equal the template glTF export's mesh deflection:
    feature compounds reference the SAME faces (shared TShapes), so an
    equal-deflection BRepMesh call reuses the triangulation already stored
    on those faces and the overlay lands exactly coplanar with the part
    surface (a finer/coarser value re-tessellates → overlay depth straddles
    the part surface → striped gaps under depth testing).
    """
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.TDataStd import TDataStd_Name
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.RWGltf import RWGltf_CafWriter
    from OCP.TColStd import TColStd_IndexedDataMapOfStringString
    from OCP.Message import Message_ProgressRange
    from OCP.TCollection import TCollection_AsciiString

    doc = _new_xcaf_doc()
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    for f in feature_solids:
        lbl = st.AddShape(f["solid"], False)
        TDataStd_Name.Set_s(lbl, TCollection_ExtendedString(f["id"]))
        BRepMesh_IncrementalMesh(f["solid"], deflection)
    w = RWGltf_CafWriter(TCollection_AsciiString(gltf_path), False)  # textual
    info = TColStd_IndexedDataMapOfStringString()
    if not w.Perform(doc, info, Message_ProgressRange()):
        raise RuntimeError(f"RWGltf_CafWriter.Perform failed: {gltf_path}")


def _compute_features(shape):
    """Feature solids + metadata for one shape (lazy import)."""
    import feature_picker  # lazy: heavy imports stay behind first use
    solids = feature_picker.collect_feature_solids(shape)
    meta = [{k: v for k, v in f.items() if k != "solid"} for f in solids]
    return solids, meta


def _write_template_features(solids: list, meta: list,
                             feats_dir: str, tid: str,
                             deflection: float) -> str:
    """Write feats_dir/tid.{json,gltf}; returns the relative metadata path."""
    os.makedirs(feats_dir, exist_ok=True)
    with open(os.path.join(feats_dir, f"{tid}.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    _export_features_gltf(solids, os.path.join(feats_dir, f"{tid}.gltf"),
                          deflection)
    return f"features/{tid}.json"


def _build_template_features(shape, feats_dir: str, tid: str) -> str | None:
    """Compute features for one template; write feats_dir/tid.{json,gltf}.

    Returns the relative metadata path (or None when the shape yields no
    features -- degenerate input guard).
    """
    solids, meta = _compute_features(shape)
    if not solids:
        return None
    # 与 _export_template_gltf 同偏差：见 _export_features_gltf docstring
    return _write_template_features(solids, meta, feats_dir, tid,
                                     _deflection_for(shape))


def refresh_template_features(new_shape, cache_dir: str, tid: str) -> list:
    """Re-export a template's features after an edit, keeping feature ids
    STABLE via fingerprint matching (R1). Returns the new feature list
    (with old ids restored where matched)."""
    old_json = os.path.join(cache_dir, "features", f"{tid}.json")
    old_feats = []
    if os.path.isfile(old_json):
        with open(old_json, "r", encoding="utf-8") as f:
            old_feats = json.load(f)

    solids, meta = _compute_features(new_shape)
    if meta:
        restable_feature_ids(old_feats, meta)
        # 与编辑后模板 glTF 重导出（_export_template_gltf）同偏差，保持共面
        _write_template_features(solids, meta,
                                 os.path.join(cache_dir, "features"), tid,
                                 _deflection_for(new_shape))
    return meta


# --------------------------------------------------------------------------
# Cache build (workspace/cache layout from the vision doc)
# --------------------------------------------------------------------------

def build_cache(input_path: str, out_dir: str) -> dict:
    """Parse an assembly STEP and write the frontend cache layout.

    Writes under out_dir (existing outputs are overwritten -- cache
    semantics, R8: cache is keyed by the source sha256 stored in the
    manifest):
      tree_structure.json          manifest (tree + explode + templates)
      gltf_library/tN.gltf(.bin)   one glTF per unique part template
      features/tN.json + tN.gltf   per-template feature metadata + meshes
    """
    manifest = parse_assembly(input_path)
    shapes = manifest.pop("_shapes")

    lib = os.path.join(out_dir, "gltf_library")
    os.makedirs(lib, exist_ok=True)
    parts_dir = os.path.join(out_dir, "parts")
    os.makedirs(parts_dir, exist_ok=True)
    feats_dir = os.path.join(out_dir, "features")
    for t in manifest["templates"]:
        gltf = os.path.join(lib, f"{t['id']}.gltf")
        _export_template_gltf(shapes[t["id"]], t["name"], t["color"], gltf)
        t["gltf"] = f"gltf_library/{t['id']}.gltf"
        # B-rep export for Phase C edits (D10: 版本管理需要可编辑几何)
        cad_core.write_shape(shapes[t["id"]],
                             os.path.join(parts_dir, f"{t['id']}.step"),
                             overwrite=True)
        try:
            t["features"] = _build_template_features(
                shapes[t["id"]], feats_dir, t["id"])
        except Exception:  # noqa: BLE001 -- feature export is best-effort
            t["features"] = None

    tree_path = os.path.join(out_dir, "tree_structure.json")
    with open(tree_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def load_cache(out_dir: str) -> dict:
    """Load a previously built cache manifest (tree_structure.json)."""
    tree_path = os.path.join(out_dir, "tree_structure.json")
    if not os.path.isfile(tree_path):
        raise FileNotFoundError(f"No assembly cache in: {out_dir}")
    with open(tree_path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Phase C: template edits + interference gate (D8 / D10)
# --------------------------------------------------------------------------

def apply_template_edit(shape, operation: str, params: dict):
    """Dispatch a validated template edit on a B-rep shape (geometry track
    of D10's dual-track modification; metadata track is a later increment).

    operation:
      - drill  : cylindrical hole; params radius, depth, position [x,y,z],
                 direction [x,y,z] (template-LOCAL coordinates)
      - fillet : round ALL edges; params radius
      - chamfer: bevel ALL edges; params distance
      - scale  : uniform scale about origin; params factor

    NOTE: fillet/chamfer/scale remain whole-template operations (R1: OCCT
    has no persistent topology naming); targeted feature edits are the
    Phase C follow-up on top of feature ids.
    """
    op = (operation or "").lower()
    if op == "drill":
        radius = float(params.get("radius", 0))
        depth = float(params.get("depth", 0))
        if radius <= 0 or depth <= 0:
            raise ValueError("drill needs radius > 0 and depth > 0")
        pos = [float(x) for x in params.get("position", [0, 0, 0])]
        d = [float(x) for x in params.get("direction", [0, 0, 1])]
        return cad_core.drill_hole(shape, pos, d, radius, depth)
    if op == "fillet":
        radius = float(params.get("radius", 0))
        if radius <= 0:
            raise ValueError("fillet needs radius > 0")
        return cad_core.fillet(shape, radius)
    if op == "chamfer":
        distance = float(params.get("distance", 0))
        if distance <= 0:
            raise ValueError("chamfer needs distance > 0")
        return cad_core.chamfer(shape, distance)
    if op == "scale":
        factor = float(params.get("factor", 0))
        if factor <= 0:
            raise ValueError("scale needs factor > 0")
        return cad_core.scale_shape(shape, factor)
    raise ValueError("operation must be drill|fillet|chamfer|scale")


# --------------------------------------------------------------------------
# Phase C+: targeted feature edits + cross-version fingerprinting (R1)
# --------------------------------------------------------------------------

_AXIS_VEC = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}


def apply_feature_edit(shape, feature: dict, operation: str, params: dict):
    """Edit ONE feature in place (R1: "点哪个特征改哪个特征").

    The feature dict comes from features/tN.json (feature_picker metadata):
    {id, type, axis, radii, extent, location/center}. Supported operations
    construct a targeted boolean from the feature's own geometry:

      - hole_resize : enlarge a hole. params radius (new radius, must be
          >= current). Cuts a cylinder along the feature axis through the
          full feature extent (+20% overshoot each way so the new diameter
          fully sweeps the old hole).
      - boss_remove : cut a boss/protrusion off. Radius = max feature
          radius * 1.05, along the axis over the extent (+overshoot).

    Shrinking a hole or re-adding a boss would require ADDING material --
    not supported (B-rep boolean fuse of a plug is a later increment).
    """
    op = (operation or "").lower()
    ftype = (feature.get("type") or "").lower()
    axis = feature.get("axis")
    radii = feature.get("radii") or []
    extent = float(feature.get("extent") or 0)
    loc = feature.get("center") or feature.get("location") or [0.0, 0.0, 0.0]
    if axis not in _AXIS_VEC:
        raise ValueError(f"feature {feature.get('id')} has no canonical axis")
    if not radii:
        raise ValueError(f"feature {feature.get('id')} has no radius data")

    if op == "hole_resize":
        if ftype not in ("hole", "cylinder", "cone", "sphere"):
            raise ValueError("hole_resize applies to hole-like features")
        new_r = float(params.get("radius", 0))
        cur_r = max(radii)
        if new_r <= 0:
            raise ValueError("hole_resize needs radius > 0")
        if new_r < cur_r - 1e-9:
            raise ValueError(
                f"hole_resize cannot shrink (current R{cur_r} -> R{new_r}); "
                "material cannot be re-added")
        # cut along the feature axis, overshooting both ends so the new
        # diameter fully sweeps the original hole
        d = _AXIS_VEC[axis]
        depth = extent * 1.4 + 2.0 * new_r + 2.0
        start = [loc[i] - d[i] * (depth * 0.2) for i in range(3)]
        return cad_core.drill_hole(shape, start, list(d), new_r, depth)

    if op == "boss_remove":
        if ftype not in ("boss", "cylinder", "cone", "sphere"):
            raise ValueError("boss_remove applies to boss-like features")
        r = max(radii) * 1.05 + 0.01
        d = _AXIS_VEC[axis]
        depth = extent * 1.4 + 2.0 * r + 2.0
        start = [loc[i] - d[i] * (depth * 0.2) for i in range(3)]
        return cad_core.drill_hole(shape, start, list(d), r, depth)

    raise ValueError("operation must be hole_resize|boss_remove")


def match_features(old_feats: list, new_feats: list,
                   pos_tol: float = 1.0, radius_tol: float = 0.2) -> dict:
    """Fingerprint-match feature lists across an edit (R1).

    OCCT has no persistent topology naming, so feature ids are re-enumerated
    after every edit. HARD key: (label/type, axis) equality + center
    distance < pos_tol. Radius delta is NOT a hard veto -- the whole point
    is tracking a feature THROUGH a radius change (hole_resize) -- it only
    scores the pairing so concentric siblings resolve deterministically.
    Greedy best-score-first match.

    Returns {"matched": [{old_id, new_id, old_radii, new_radii}],
             "added": [new_id...], "removed": [old_id...]}.
    """
    def family(f):
        """Classification family: composite re-grouping (e.g. 孔 -> 复合沉孔)
        must NOT break identity -- the type field (hole/boss/...) is stable
        across edits, labels are not."""
        return f.get("type") or f.get("label")

    scored = []
    for oi, o in enumerate(old_feats):
        for ni, n in enumerate(new_feats):
            if family(o) != family(n) or o.get("axis") != n.get("axis"):
                continue
            oc, nc = o.get("center"), n.get("center")
            if not oc or not nc:
                continue
            dist = sum((a - b) ** 2 for a, b in zip(oc, nc)) ** 0.5
            if dist > pos_tol:
                continue
            orr, nrr = o.get("radii") or [], n.get("radii") or []
            rdelta = (abs(max(orr) - max(nrr)) if orr and nrr else 1.0)
            scored.append((dist + 0.5 * rdelta, oi, ni))
    scored.sort()

    old_ids = [f.get("id") for f in old_feats]
    new_ids = [f.get("id") for f in new_feats]
    used_o, used_n = set(), set()
    matched = []
    for _, oi, ni in scored:
        if oi in used_o or ni in used_n:
            continue
        used_o.add(oi)
        used_n.add(ni)
        matched.append({
            "old_id": old_feats[oi].get("id"), "new_id": new_feats[ni].get("id"),
            "old_radii": old_feats[oi].get("radii"),
            "new_radii": new_feats[ni].get("radii"),
        })
    return {
        "matched": matched,
        "added": [nid for i, nid in enumerate(new_ids) if i not in used_n],
        "removed": [oid for i, oid in enumerate(old_ids) if i not in used_o],
    }


def restable_feature_ids(old_feats: list, new_feats: list) -> list:
    """Re-key new_feats with the OLD ids where fingerprints match (stable
    feature identity across versions, D10/R1). Mutates + returns new_feats."""
    m = match_features(old_feats, new_feats)
    mapping = {x["new_id"]: x["old_id"] for x in m["matched"]}
    for f in new_feats:
        if f.get("id") in mapping:
            f["id"] = mapping[f["id"]]
    return new_feats


def _world_instances(manifest: dict, template_shapes: dict) -> list:
    """Flatten the tree into part instances with world matrices + shapes.

    NOTE: manifest node matrices are ALREADY accumulated world transforms
    (parse_assembly walks the parent chain); do not multiply the parent in
    again.
    """
    out = []

    def walk(node):
        from OCP.gp import gp_Trsf
        m = node.get("matrix")
        t = gp_Trsf()
        if m:
            t.SetValues(m[0][0], m[0][1], m[0][2], m[0][3],
                        m[1][0], m[1][1], m[1][2], m[1][3],
                        m[2][0], m[2][1], m[2][2], m[2][3])
        if node["type"] == "part":
            shp = template_shapes.get(node["template"])
            if shp is not None:
                out.append({"id": node["id"], "name": node["name"],
                            "template": node["template"], "shape": shp,
                            "trsf": t, "matrix": m})
        for ch in node.get("children", []):
            walk(ch)

    walk(manifest["root"])
    return out


def check_interference(manifest: dict, template_shapes: dict,
                       edited_template: str | None = None,
                       edited_shape=None, tolerance: float = 0.01,
                       edited_templates: dict | None = None,
                       moved_ids: set | None = None,
                       mode: str = "exact") -> list:
    """Boolean interference gate (D8: deterministic check, AI never guesses).

    Substitution (in priority order):
      1. ``edited_templates`` (dict tid -> shape): multi-template draft mode
         (M2). All listed templates' instances are replaced at once.
      2. ``edited_template`` + ``edited_shape``: single-template legacy
         mode (Phase C edit path).

    ``moved_ids`` (M6.5): set of node_ids whose placement changed (draft
    move steps). The manifest passed in must already carry the moved
    matrices; these instances join the "edited" candidate set so move-only
    drafts get the same incremental check as shape edits.

    ``mode``: 'exact' = boolean common volume (default, the real gate);
    'bbox' = AABB overlap only (millisecond fast check for interactive
    drag previews; hits carry ``potential: True`` and no volume -- exact
    verdict is deferred to confirm-time / explicit recheck).

    When no substitution/move is provided: checks ALL instance pairs
    (static audit).  Otherwise: candidate pairs are
      (edited instance, other instance) + (edited instance, edited instance)
    where "edited" = instance of any substituted template OR a moved
    instance. Bbox prefilter keeps the O(n²) boolean count near zero for
    sparse assemblies.

    Returns a list of {a, b, volume_mm3} for each interfering pair
    (common volume > tolerance); bbox mode returns {a, b, potential} pairs.
    Empty list = no interference.
    """
    from OCP.TopLoc import TopLoc_Location
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.GProp import GProp_GProps
    from OCP.BRepGProp import BRepGProp

    # 本地 bbox 走模块级缓存（_local_bbox_cached）：同一模板的 79 个
    # 实例形状相同 → 只算一次 BRepBndLib；world bbox = 8 角点 × 实例
    # 矩阵纯 Python 数学。此前每实例每次都跑 BRepBndLib（~5s）。

    def local_bbox(shape):
        return _local_bbox_cached(shape)

    def world_bbox(shape, matrix):
        """本地 bbox 8 角点 × 实例矩阵（纯 Python，零 SWIG 调用）。

        角点是「点」：必须应用平移分量。此前用 gp_Vec.Transformed——
        向量变换不含平移（数学正确但类型用错），所有实例的世界 bbox
        都坍缩在模板本地位置，位移/装配位置全部丢失。
        matrix: manifest 节点 3x4 list（[[a,b,c,d],[e,f,g,h],[i,j,k,l]]）。
        """
        x0, y0, z0, x1, y1, z1 = local_bbox(shape)
        if not matrix:
            return (x0, y0, z0, x1, y1, z1)
        r0, r1, r2 = matrix
        xs, ys, zs = [], [], []
        for x in (x0, x1):
            for y in (y0, y1):
                for z in (z0, z1):
                    xs.append(r0[0]*x + r0[1]*y + r0[2]*z + r0[3])
                    ys.append(r1[0]*x + r1[1]*y + r1[2]*z + r1[3])
                    zs.append(r2[0]*x + r2[1]*y + r2[2]*z + r2[3])
        return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

    def world_shape(shape, trsf):
        return shape.Moved(TopLoc_Location(trsf))

    def volume_of(shape):
        p = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, p)
        return p.Mass()

    insts = _world_instances(manifest, template_shapes)

    # substitute edited shapes for the edited templates' instances
    edited_set = set()
    if edited_templates:
        for i in insts:
            if i["template"] in edited_templates:
                i["shape"] = edited_templates[i["template"]]
                i["edited"] = True
                edited_set.add(i["template"])
    elif edited_template is not None:
        for i in insts:
            if i["template"] == edited_template:
                i["shape"] = edited_shape
                i["edited"] = True
                edited_set.add(edited_template)
    if moved_ids:
        for i in insts:
            if i["id"] in moved_ids:
                i["edited"] = True
        # 防御：moved_ids 全没命中时 any(edited)=False 会退化成全量
        # O(n²) 检查（大装配 5 分钟级）。上游 apply_moves 已校验，
        # 这里兜底直接调用方（如直接调 check_interference 的测试）。
        if not any(i.get("edited") for i in insts):
            raise ValueError(
                f"moved_ids 未命中任何实例: {sorted(moved_ids)}")

    # candidate pairs
    pairs = []
    if not any(i.get("edited") for i in insts):
        pairs = [(i, j) for k, i in enumerate(insts)
                 for j in insts[k + 1:]]
    else:
        edited = [i for i in insts if i.get("edited")]
        others = [i for i in insts if not i.get("edited")]
        pairs = [(i, j) for i in edited for j in others]
        pairs += [(edited[k], j) for k in range(len(edited))
                  for j in edited[k + 1:]]

    # cache world bboxes for prefilter
    bboxes = {}

    def bbox_of(i):
        if i["id"] not in bboxes:
            bboxes[i["id"]] = world_bbox(i["shape"], i.get("matrix"))
        return bboxes[i["id"]]

    def overlap(a, b):
        return (a[0] < b[3] and b[0] < a[3] and
                a[1] < b[4] and b[1] < a[4] and
                a[2] < b[5] and b[2] < a[5])

    hits = []
    for i, j in pairs:
        bi, bj = bbox_of(i), bbox_of(j)
        if not overlap(bi, bj):
            continue
        if mode == "bbox":
            # AABB 级快速反馈：重叠即「可能碰撞」，不做布尔（精确
            # 判定延后到显式重检/确认保存的 exact 守门）
            hits.append({"a": {"id": i["id"], "name": i["name"]},
                         "b": {"id": j["id"], "name": j["name"]},
                         "potential": True})
            continue
        common = BRepAlgoAPI_Common(world_shape(i["shape"], i["trsf"]),
                                    world_shape(j["shape"], j["trsf"]))
        common.Build()
        if not common.IsDone():
            continue
        v = volume_of(common.Shape())
        if v > tolerance:
            hits.append({"a": {"id": i["id"], "name": i["name"]},
                         "b": {"id": j["id"], "name": j["name"]},
                         "volume_mm3": round(v, 3)})
    return hits


# ==========================================================================
# M2: 草稿步骤表（声明式 · 多目标 · 可增删）
# 模型：草稿 = 基线 step 出发按顺序串行应用编辑，不落版本。
# ==========================================================================
DRAFT_SCHEMA_VERSION = 1


def replay_draft_shapes(cache_dir: str, manifest: dict, store,
                         steps: list) -> dict:
    """Replay the draft's ordered step list over the baseline geometry.

    Returns ``{template_id: shape}`` for every template touched by the
    draft (only the FINAL shape per template -- the chain collapses to
    one shape per template regardless of how many steps touched it).

    * Geometrically additive: each step's new shape becomes the input to
      the next step on the same template (serial replay).
    * ``move`` steps are instance-level (node_id addressing) and change no
      geometry -- skipped here; see ``moves_from_steps``/``apply_moves``.
    * Reads baseline geometry via ``store.resolve_step(tid, baseline_step=...)``
      so version-aware baseline (current version at session lock time) is
      respected.
    """
    touched = {}
    for s in steps:
        tid = s.get("template_id")
        op = s.get("operation")
        params = s.get("params") or {}
        feature_id = s.get("feature_id")
        if not op:
            raise ValueError(f"step missing operation: {s}")
        if op.lower() == "move":
            continue   # 实例级位移：manifest 侧处理（moves_from_steps）
        if not tid:
            raise ValueError(f"step missing template_id: {s}")

        # source = current replayed shape if already touched, else baseline
        if tid in touched:
            shape = touched[tid]
        else:
            step_path = store.resolve_step(
                tid, baseline_step=os.path.join(cache_dir, "parts", f"{tid}.step"))
            shape = cad_core.read_shape(step_path)

        if feature_id is not None:
            feats_json = os.path.join(cache_dir, "features", f"{tid}.json")
            if not os.path.isfile(feats_json):
                raise ValueError(f"no feature data for {tid}")
            with open(feats_json, encoding="utf-8") as f:
                feats = json.load(f)
            feat = next((x for x in feats if x.get("id") == feature_id), None)
            if feat is None:
                raise ValueError(f"unknown feature_id: {feature_id}")
            shape = apply_feature_edit(shape, feat, op, params)
        else:
            shape = apply_template_edit(shape, op, params)
        touched[tid] = shape
    return touched


# --------------------------------------------------------------------------
# M6.5: 草稿内位置调整（move 步骤 · 实例级）
# move 是第一个实例级操作：几何步骤用 template_id 寻址（改一个模板 =
# 所有实例同步变），而位置调整针对"这个位置的这一件"，用 node_id 寻址。
# --------------------------------------------------------------------------

def _find_node(manifest: dict, node_id):
    stack = [manifest["root"]]
    while stack:
        n = stack.pop()
        if n.get("id") == node_id:
            return n
        stack.extend(n.get("children", []))
    return None


def moves_from_steps(steps: list) -> dict:
    """{node_id: [dx, dy, dz]} —— 每个节点取最后一条 move 步骤的总位移
    （相对基线，绝对语义：后写覆盖，与几何步骤的"最终形状"语义一致）。
    前端拖拽时替换同节点的 move 步骤，这里天然收敛为一条。"""
    out: dict[str, list] = {}
    for s in steps:
        if (s.get("operation") or "").lower() != "move":
            continue
        nid = s.get("node_id")
        if not nid:
            raise ValueError(f"move step missing node_id: {s}")
        p = s.get("params") or {}
        out[nid] = [float(p.get("dx", 0)), float(p.get("dy", 0)),
                    float(p.get("dz", 0))]
    return out


def apply_moves(manifest: dict, moves: dict) -> dict:
    """Deep-copy manifest，把 {node_id: [dx,dy,dz]} 世界系位移应用到
    part 节点累积世界矩阵的平移列（manifest 矩阵已是世界系，直接加）。

    未知 node_id / 非零件节点直接 ValueError：此前静默跳过会让
    check_interference 的增量候选集为空，退化成全量 O(n²) 布尔检查
    （62 模板实测 5 分钟），且草稿 manifest 实际没有移动任何东西。"""
    import copy
    out = copy.deepcopy(manifest)
    applied = set()
    stack = [out["root"]]
    while stack:
        n = stack.pop()
        d = moves.get(n.get("id"))
        if d is not None:
            if n.get("type") != "part":
                raise ValueError(
                    f"move 目标不是零件节点: {n.get('id')}"
                    f"（type={n.get('type')}，move 仅支持 part）")
            applied.add(n["id"])
            m = n.get("matrix")
            if not m:
                m = n["matrix"] = [[1.0, 0.0, 0.0, 0.0],
                                   [0.0, 1.0, 0.0, 0.0],
                                   [0.0, 0.0, 1.0, 0.0]]
            m[0][3] += d[0]
            m[1][3] += d[1]
            m[2][3] += d[2]
        stack.extend(n.get("children", []))
    unknown = set(moves) - applied
    if unknown:
        raise ValueError(
            f"move 步骤引用了不存在的 node_id: {sorted(unknown)}"
            f"（node_id 是装配树节点 id 如 'n2'，不是 STEP 名 'NAUO66'）")
    return out


def draft_step_title(step: dict, manifest: dict) -> str:
    """Deterministic human-readable title for a draft step (used by the
    frontend and the eventual batch changelog)."""
    op = step.get("operation", "?")
    params = step.get("params") or {}
    if op.lower() == "move":
        nid = step.get("node_id", "?")
        node = _find_node(manifest, nid)
        name = node.get("name", nid) if node else nid
        return (f"{name}: move Δ({params.get('dx', 0)},"
                f"{params.get('dy', 0)},{params.get('dz', 0)}) mm")
    tid = step.get("template_id", "?")
    tpl = next((t for t in manifest.get("templates", []) if t["id"] == tid), None)
    tpl_name = tpl.get("name", tid) if tpl else tid
    op = step.get("operation", "?")
    params = step.get("params") or {}
    fid = step.get("feature_id")
    if fid:
        return f"{tpl_name} {fid}: {op} " + ", ".join(f"{k}={v}" for k, v in params.items())
    return f"{tpl_name}: {op} " + ", ".join(f"{k}={v}" for k, v in params.items())


def template_instance_counts(manifest: dict) -> dict:
    """{template_id: 实例数}（遍历装配树统计 part 节点）。"""
    counts: dict[str, int] = {}
    stack = [manifest["root"]]
    while stack:
        n = stack.pop()
        if n.get("type") == "part" and n.get("template"):
            counts[n["template"]] = counts.get(n["template"], 0) + 1
        stack.extend(n.get("children", []))
    return counts


def draft_diff(manifest: dict, baseline_shapes: dict,
               edited_shapes: dict) -> dict:
    """基线 vs 草稿的几何差异摘要（M5，确定性数值，D8）。

    每个被编辑模板：体积/表面积 基线值、草稿值、增量；总计按实例数
    加权（模板去重模型下，改一个模板 = 其全部实例同步变化）。
    """
    counts = template_instance_counts(manifest)
    per_template = []
    tot_base_v = tot_draft_v = tot_base_a = tot_draft_a = 0.0
    for tid, new_shape in sorted(edited_shapes.items()):
        tpl = next((t for t in manifest.get("templates", [])
                    if t["id"] == tid), None)
        base_shape = baseline_shapes.get(tid)
        n = counts.get(tid, 1)
        base_p = cad_core.properties(base_shape) if base_shape is not None else {}
        new_p = cad_core.properties(new_shape)
        bv, dv = base_p.get("volume", 0.0), new_p.get("volume", 0.0)
        ba, da = base_p.get("surface_area", 0.0), new_p.get("surface_area", 0.0)
        tot_base_v += bv * n; tot_draft_v += dv * n
        tot_base_a += ba * n; tot_draft_a += da * n
        per_template.append({
            "template_id": tid,
            "name": tpl.get("name", tid) if tpl else tid,
            "instances": n,
            "baseline_volume_mm3": round(bv, 3),
            "draft_volume_mm3": round(dv, 3),
            "delta_volume_mm3": round(dv - bv, 3),
            "volume_pct": round((dv - bv) / bv * 100, 2) if bv else None,
            "baseline_area_mm2": round(ba, 3),
            "draft_area_mm2": round(da, 3),
            "delta_area_mm2": round(da - ba, 3),
        })
    return {
        "per_template": per_template,
        "totals": {
            "baseline_volume_mm3": round(tot_base_v, 3),
            "draft_volume_mm3": round(tot_draft_v, 3),
            "delta_volume_mm3": round(tot_draft_v - tot_base_v, 3),
            "volume_pct": round((tot_draft_v - tot_base_v) / tot_base_v * 100, 2)
                          if tot_base_v else None,
            "baseline_area_mm2": round(tot_base_a, 3),
            "draft_area_mm2": round(tot_draft_a, 3),
            "delta_area_mm2": round(tot_draft_a - tot_base_a, 3),
        },
    }


# --------------------------------------------------------------------------
# 模板形状进程级缓存：preview/confirm 每次都要全模板形状做干涉检查，
# STEP 导入是大头（62 模板 ~7s）。按 (path, mtime, size) 缓存后热路径
# 归零，move 级琐碎编辑的 preview 从 ~13s 降到 ~1s。
# 缓存 shape 只在只读路径共享（OCP 编辑操作均返回新 shape 不改输入）；
# 文件变化（版本 commit 覆盖）由 mtime 失效。
# --------------------------------------------------------------------------
_SHAPE_CACHE: dict = {}           # path -> ((mtime, size), shape)
_SHAPE_CACHE_LOCK = threading.Lock()
_SHAPE_CACHE_MAX = 256


def _read_cached_shape(path: str):
    st = os.stat(path)
    key = (st.st_mtime, st.st_size)
    with _SHAPE_CACHE_LOCK:
        hit = _SHAPE_CACHE.get(path)
        if hit and hit[0] == key:
            return hit[1]
    shape = cad_core.read_shape(path)
    with _SHAPE_CACHE_LOCK:
        if len(_SHAPE_CACHE) >= _SHAPE_CACHE_MAX:
            _SHAPE_CACHE.pop(next(iter(_SHAPE_CACHE)))   # FIFO 淘汰
        _SHAPE_CACHE[path] = (key, shape)
    return shape


# 本地 bbox 缓存（shape 作 key 并持有引用）：check_interference 里
# world bbox = 本地 bbox 8 角点 × 实例矩阵（纯数学）。BRepBndLib 只对
# 每个不同 shape 跑一次；同一模板的多个实例共享。
_LOCAL_BBOX_CACHE: dict = {}
_LOCAL_BBOX_LOCK = threading.Lock()
_LOCAL_BBOX_MAX = 256


def _local_bbox_cached(shape):
    with _LOCAL_BBOX_LOCK:
        b = _LOCAL_BBOX_CACHE.get(shape)
        if b is not None:
            return b
    from OCP.BRepBndLib import BRepBndLib
    from OCP.Bnd import Bnd_Box
    box = Bnd_Box()
    box.SetGap(0.0)
    BRepBndLib.Add_s(shape, box)
    try:
        b = box.Get()
    except TypeError:
        pmin, pmax = box.Get()
        b = (pmin.X(), pmin.Y(), pmin.Z(), pmax.X(), pmax.Y(), pmax.Z())
    with _LOCAL_BBOX_LOCK:
        if len(_LOCAL_BBOX_CACHE) >= _LOCAL_BBOX_MAX:
            _LOCAL_BBOX_CACHE.pop(next(iter(_LOCAL_BBOX_CACHE)))
        _LOCAL_BBOX_CACHE[shape] = b
    return b


def template_shapes_from_cache(cache_dir: str, manifest: dict) -> dict:
    """Load B-rep shapes for every template from cache parts/tN.step.

    走 _read_cached_shape（按 path+mtime 进程级缓存）：STEP 导入是
    OCP 重活（62 模板 ~7s），而两次 preview 之间未编辑模板的几何
    根本没变。缓存的 shape 是只读共享对象——check_interference
    等只读路径直接用；replay_draft_shapes 的可编辑基线另行直读，
    防止未来出现原地修改几何的操作污染缓存。"""
    shapes = {}
    for t in manifest["templates"]:
        p = os.path.join(cache_dir, "parts", f"{t['id']}.step")
        if os.path.isfile(p):
            shapes[t["id"]] = _read_cached_shape(p)
    return shapes


# --------------------------------------------------------------------------
# Phase C+: DFM audit rules (模块七 一键体检, deterministic)
# --------------------------------------------------------------------------

# (rule_id, severity, description) -- thresholds in mm, CNC 普通刀具口径
DFM_RULES = {
    "small_hole": {
        "severity": "warning",
        "min_radius": 0.5,
        "desc": "孔径过小（R<{min_radius}mm）：小于常用最小钻头，需微钻/EDM"},
    "deep_hole": {
        "severity": "warning",
        "ratio": 10.0,
        "desc": "深径比 > {ratio}:1（深孔排屑困难，需深孔钻/分步）"},
    "thin_wall_hint": {
        "severity": "info",
        "min_gap": 1.0,
        "desc": "平行孔间距 < {min_gap}mm×孔径和：孔间壁厚过薄风险"},
}


def dfm_audit_features(features: list) -> list:
    """Run deterministic DFM rules over ONE template's feature metadata.

    Returns a list of {rule, severity, feature_id, detail} findings.
    Rules are evaluated on the classified feature list (holes/bosses with
    radii/extent) -- no numeric estimation, ever (D8).
    """
    findings = []
    holes = [f for f in features if f.get("type") == "hole" and f.get("radii")]
    for f in holes:
        r = max(f["radii"])
        if r < DFM_RULES["small_hole"]["min_radius"]:
            findings.append({
                "rule": "small_hole", "severity": "warning",
                "feature_id": f.get("id"),
                "detail": f"R{r} < {DFM_RULES['small_hole']['min_radius']}mm "
                          f"({f.get('label')} {f.get('id')})"})
        dia = 2.0 * r
        if dia > 0 and (f.get("extent") or 0) / dia > DFM_RULES["deep_hole"]["ratio"]:
            findings.append({
                "rule": "deep_hole", "severity": "warning",
                "feature_id": f.get("id"),
                "detail": f"L/D = {f['extent'] / dia:.1f} > "
                          f"{DFM_RULES['deep_hole']['ratio']}:1 "
                          f"({f.get('label')} {f.get('id')} L{f['extent']})"})

    # parallel holes with thin web between them (same axis, close centers)
    min_gap = DFM_RULES["thin_wall_hint"]["min_gap"]
    for i in range(len(holes)):
        for j in range(i + 1, len(holes)):
            a, b = holes[i], holes[j]
            if a.get("axis") != b.get("axis") or not a.get("center") or not b.get("center"):
                continue
            dist = sum((x - y) ** 2 for x, y in zip(a["center"], b["center"])) ** 0.5
            web = dist - max(a["radii"]) - max(b["radii"])
            if 0 < web < min_gap:
                findings.append({
                    "rule": "thin_wall_hint", "severity": "info",
                    "feature_id": f"{a.get('id')}+{b.get('id')}",
                    "detail": f"两孔间壁厚 {web:.2f}mm < {min_gap}mm"})
    return findings


def audit_assembly(cache_dir: str, manifest: dict) -> dict:
    """一键体检（模块七导入角色）：干涉 + DFM 全量审计。

    Combines the boolean interference audit (all instance pairs) with the
    per-template DFM rule scan over cached feature metadata. Deterministic
    throughout (D8).
    """
    shapes = template_shapes_from_cache(cache_dir, manifest)
    interferences = check_interference(manifest, shapes)
    dfm = []
    for t in manifest["templates"]:
        if not t.get("features"):
            continue
        fp = os.path.join(cache_dir, t["features"])
        if not os.path.isfile(fp):
            continue
        with open(fp, encoding="utf-8") as f:
            feats = json.load(f)
        for finding in dfm_audit_features(feats):
            finding["part"] = t["name"]
            dfm.append(finding)
    return {
        "interference_count": len(interferences),
        "interferences": interferences,
        "dfm_count": len(dfm),
        "dfm": dfm,
    }
