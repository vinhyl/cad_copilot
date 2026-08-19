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

OCP quirks are documented in tests/_assembly_helpers.py (spike learnings);
this module is the production path for the same functionality.
"""
from __future__ import annotations

import hashlib
import json
import os

SCHEMA_VERSION = 1


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
# Cache build (workspace/cache layout from the vision doc)
# --------------------------------------------------------------------------

def build_cache(input_path: str, out_dir: str) -> dict:
    """Parse an assembly STEP and write the frontend cache layout.

    Writes ``tree_structure.json`` + ``gltf_library/tN.gltf(.bin)`` under
    out_dir and returns the manifest dict (with relative gltf paths filled
    in). Existing outputs are overwritten (cache semantics, R8: cache is
    keyed by source sha256 which is stored in the manifest).
    """
    manifest = parse_assembly(input_path)
    shapes = manifest.pop("_shapes")

    lib = os.path.join(out_dir, "gltf_library")
    os.makedirs(lib, exist_ok=True)
    for t in manifest["templates"]:
        gltf = os.path.join(lib, f"{t['id']}.gltf")
        _export_template_gltf(shapes[t["id"]], t["name"], t["color"], gltf)
        t["gltf"] = f"gltf_library/{t['id']}.gltf"

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
