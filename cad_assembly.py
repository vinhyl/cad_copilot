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

import cad_core

SCHEMA_VERSION = 2   # v2: + explode vectors, + parts/*.step, + features/*


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
    """Size-relative deflection, same formula as make_preview/feature_picker
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

def _export_features_gltf(feature_solids: list, gltf_path: str) -> None:
    """Write one named XCAF shape per feature as a textual glTF.

    Node names in the glTF are the feature ids (e.g. "#3", "#7.2", "P1"),
    so the frontend can map metadata entries to overlay meshes 1:1.
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
        BRepMesh_IncrementalMesh(f["solid"], 0.1)
    w = RWGltf_CafWriter(TCollection_AsciiString(gltf_path), False)  # textual
    info = TColStd_IndexedDataMapOfStringString()
    if not w.Perform(doc, info, Message_ProgressRange()):
        raise RuntimeError(f"RWGltf_CafWriter.Perform failed: {gltf_path}")


def _build_template_features(shape, feats_dir: str, tid: str) -> str | None:
    """Compute features for one template; write feats_dir/tid.{json,gltf}.

    Returns the relative metadata path (or None when the shape yields no
    features -- degenerate input guard).
    """
    import feature_picker  # lazy: heavy imports stay behind first use

    solids = feature_picker.collect_feature_solids(shape)
    if not solids:
        return None
    os.makedirs(feats_dir, exist_ok=True)
    meta = [{k: v for k, v in f.items() if k != "solid"} for f in solids]
    with open(os.path.join(feats_dir, f"{tid}.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    _export_features_gltf(solids, os.path.join(feats_dir, f"{tid}.gltf"))
    return f"features/{tid}.json"


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
                            "trsf": t})
        for ch in node.get("children", []):
            walk(ch)

    walk(manifest["root"])
    return out


def check_interference(manifest: dict, template_shapes: dict,
                       edited_template: str | None = None,
                       edited_shape=None, tolerance: float = 0.01) -> list:
    """Boolean interference gate (D8: deterministic check, AI never guesses).

    When edited_template is None: checks ALL instance pairs (static audit).
    Otherwise: checks the edited template's instances (using edited_shape)
    against every other instance, and edited instances against each other.

    Returns a list of {a, b, volume_mm3} for each interfering pair
    (common volume > tolerance). Empty list = no interference.
    Bbox prefilter keeps the O(n²) boolean count near zero for sparse
    assemblies.
    """
    from OCP.TopLoc import TopLoc_Location
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepBndLib import BRepBndLib
    from OCP.Bnd import Bnd_Box
    from OCP.GProp import GProp_GProps
    from OCP.BRepGProp import BRepGProp

    def world_bbox(shape, trsf):
        moved = shape.Moved(TopLoc_Location(trsf))
        box = Bnd_Box()
        box.SetGap(0.0)
        BRepBndLib.Add_s(moved, box)
        try:
            return box.Get()
        except TypeError:
            pmin, pmax = box.Get()
            return (pmin.X(), pmin.Y(), pmin.Z(), pmax.X(), pmax.Y(), pmax.Z())

    def world_shape(shape, trsf):
        return shape.Moved(TopLoc_Location(trsf))

    def volume_of(shape):
        p = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, p)
        return p.Mass()

    insts = _world_instances(manifest, template_shapes)
    # substitute edited shape for the edited template's instances
    if edited_template is not None:
        for i in insts:
            if i["template"] == edited_template:
                i["shape"] = edited_shape
                i["edited"] = True

    # candidate pairs
    pairs = []
    if edited_template is None:
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
            bboxes[i["id"]] = world_bbox(i["shape"], i["trsf"])
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


def template_shapes_from_cache(cache_dir: str, manifest: dict) -> dict:
    """Load B-rep shapes for every template from cache parts/tN.step."""
    shapes = {}
    for t in manifest["templates"]:
        p = os.path.join(cache_dir, "parts", f"{t['id']}.step")
        if os.path.isfile(p):
            shapes[t["id"]] = cad_core.read_shape(p)
    return shapes
