"""XCAF assembly fixture helpers for Phase A tests (ADR-0002 D3 / R2).

Builds a deterministic 2-level test assembly with named parts, one colored
part and one duplicated (multi-instanced) part; writes/reads it via STEPCAF;
exports glTF via RWGltf; dedups same-template meshes in the exported JSON.

    PumpHead (assembly)
    ├── BasePlate            (box 20x10x5, at origin)
    └── BearingComp (assembly, translated +30 X)
        ├── M4x8_Bolt #1     (cylinder r2 h8, local +5,+5,+5 -> world 35,5,5)
        └── M4x8_Bolt #2     (cylinder r2 h8, local +15,+5,+5 -> world 45,5,5)

OCP quirks discovered by the 2026-08-19 spikes and encapsulated here:
  * many XCAFDoc_ShapeTool/ColorTool methods are exposed with an ``_s``
    suffix in this wheel (IsAssembly_s, GetComponents_s, GetReferredShape_s,
    GetColor_s, ...) -- check ``dir()`` before assuming plain names.
  * AddComponent takes a TopLoc_Location, not a gp_Trsf.
  * TDF_Label has no EntryToString; use TDF_Tool.Entry_s.
  * Component labels carry OCCT reference-marker names ("=>[entry]"); the
    real part names live on the referred template labels.
  * RWGltf_CafWriter does NOT dedup same-template geometry: each instance
    gets its own mesh. Dedup is a JSON post-process (see gltf_dedup).
  * RWGltf_CafWriter requires shapes to be pre-triangulated.
"""
from __future__ import annotations

import json
import os


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build_assembly_doc():
    """Build the test assembly as a fresh XCAF document (see module docstring)."""
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.TopLoc import TopLoc_Location
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    app.NewDocument(TCollection_ExtendedString("XmlOcaf"), doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    box = BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape()
    bolt = BRepPrimAPI_MakeCylinder(2.0, 8.0).Shape()

    lbl_box = st.AddShape(box, False)
    TDataStd_Name.Set_s(lbl_box, TCollection_ExtendedString("BasePlate"))
    lbl_bolt = st.AddShape(bolt, False)
    TDataStd_Name.Set_s(lbl_bolt, TCollection_ExtendedString("M4x8_Bolt"))
    ct.SetColor(lbl_bolt, Quantity_Color(0.2, 0.4, 0.9, Quantity_TOC_RGB),
                XCAFDoc_ColorType.XCAFDoc_ColorSurf)

    lbl_asm = st.NewShape()
    TDataStd_Name.Set_s(lbl_asm, TCollection_ExtendedString("PumpHead"))
    lbl_sub = st.NewShape()
    TDataStd_Name.Set_s(lbl_sub, TCollection_ExtendedString("BearingComp"))

    def loc(x, y, z):
        t = gp_Trsf()
        t.SetTranslation(gp_Vec(x, y, z))
        return TopLoc_Location(t)

    st.AddComponent(lbl_asm, lbl_box, loc(0.0, 0.0, 0.0))
    st.AddComponent(lbl_asm, lbl_sub, loc(30.0, 0.0, 0.0))
    st.AddComponent(lbl_sub, lbl_bolt, loc(5.0, 5.0, 5.0))
    st.AddComponent(lbl_sub, lbl_bolt, loc(15.0, 5.0, 5.0))
    st.UpdateAssemblies()
    return doc


# --------------------------------------------------------------------------
# STEP write / read
# --------------------------------------------------------------------------

def write_assembly_step(doc, path: str) -> None:
    """Write an XCAF document to STEP (AP214, names + colors enabled)."""
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_StepModelType
    from OCP.Interface import Interface_Static

    Interface_Static.SetCVal_s("write.step.schema", "AP214IS")
    w = STEPCAFControl_Writer()
    w.SetNameMode(True)
    w.SetColorMode(True)
    w.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs)
    status = w.Write(path)
    if status != 1:
        raise RuntimeError(f"STEPCAFControl_Writer.Write failed: {path}")


def read_assembly_tree(path: str) -> list:
    """Read a STEP assembly back and walk the XCAF tree.

    Returns a list of rows, one per visited node (depth-first):
      {depth, name, is_assembly, is_reference, matrix, referred, color, entry}
    ``matrix`` is the accumulated WORLD 4x4 transform (3 rows x 4 cols).
    ``name`` prefers the component's own name unless it is an OCCT
    reference marker ("=>[...]"), in which case the template name is used.
    """
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorType
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDF import TDF_Label, TDF_LabelSequence
    from OCP.TDataStd import TDataStd_Name
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.Quantity import Quantity_Color
    from OCP.gp import gp_Trsf

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    app.NewDocument(TCollection_ExtendedString("XmlOcaf"), doc)

    rdr = STEPCAFControl_Reader()
    rdr.SetNameMode(True)
    rdr.SetColorMode(True)
    if rdr.ReadFile(path) != 1:
        raise RuntimeError(f"STEPCAFControl_Reader.ReadFile failed: {path}")
    if not rdr.Transfer(doc):
        raise RuntimeError(f"STEPCAFControl_Reader.Transfer failed: {path}")

    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    def name_of(lb):
        a = TDataStd_Name()
        if lb.FindAttribute(TDataStd_Name.GetID_s(), a):
            return TCollection_ExtendedString(a.Get()).ToExtString()
        return None

    def entry_of(lb):
        from OCP.TDF import TDF_Tool
        from OCP.TCollection import TCollection_AsciiString
        s = TCollection_AsciiString()
        TDF_Tool.Entry_s(lb, s)
        return s.ToCString()

    rows = []

    def walk(label, depth, acc_trsf):
        is_ref = st.IsReference_s(label)
        ref = None
        if is_ref:
            ref = TDF_Label()
            st.GetReferredShape_s(label, ref)
        if st.IsComponent_s(label):
            loc = st.GetLocation_s(label)
            acc_trsf = acc_trsf.Multiplied(loc.Transformation())
        src = ref if ref is not None else label
        own = name_of(label)
        tpl = name_of(src) if ref is not None else None
        name = tpl if (ref is not None and (own is None or own.startswith("=>"))) \
            else (own or tpl)
        color = None
        qc = Quantity_Color()
        if ct.GetColor_s(src, XCAFDoc_ColorType.XCAFDoc_ColorSurf, qc):
            color = (round(qc.Red(), 3), round(qc.Green(), 3), round(qc.Blue(), 3))
        rows.append({
            "depth": depth, "name": name,
            "is_assembly": st.IsAssembly_s(src), "is_reference": is_ref,
            "matrix": [[round(acc_trsf.Value(r, c), 4) for c in range(1, 5)]
                       for r in range(1, 4)],
            "referred": entry_of(ref) if ref is not None else None,
            "color": color, "entry": entry_of(label),
        })
        # Assembly-ness lives on the referred target, not on the component.
        if st.IsAssembly_s(src):
            comps = TDF_LabelSequence()
            st.GetComponents_s(src, comps)
            for i in range(1, comps.Length() + 1):
                walk(comps.Value(i), depth + 1, acc_trsf)

    roots = TDF_LabelSequence()
    st.GetFreeShapes(roots)
    for i in range(1, roots.Length() + 1):
        walk(roots.Value(i), 0, gp_Trsf())
    return rows


# --------------------------------------------------------------------------
# glTF export
# --------------------------------------------------------------------------

def normalize_component_names(doc) -> int:
    """Copy template names onto component labels (they carry "=>[...]" markers).

    Returns the number of component labels fixed. Components are not part of
    GetShapes(); the tree is walked from the free shapes.
    """
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.TDF import TDF_Label, TDF_LabelSequence
    from OCP.TDataStd import TDataStd_Name
    from OCP.TCollection import TCollection_ExtendedString

    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    def name_of(lb):
        a = TDataStd_Name()
        if lb.FindAttribute(TDataStd_Name.GetID_s(), a):
            return TCollection_ExtendedString(a.Get()).ToExtString()
        return None

    fixed = 0

    def walk(label):
        nonlocal fixed
        if st.IsReference_s(label):
            ref = TDF_Label()
            if st.GetReferredShape_s(label, ref):
                tpl = name_of(ref)
                own = name_of(label)
                if tpl and (own is None or own.startswith("=>")):
                    TDataStd_Name.Set_s(label, TCollection_ExtendedString(tpl))
                    fixed += 1
                walk(ref)
        if st.IsAssembly_s(label):
            comps = TDF_LabelSequence()
            st.GetComponents_s(label, comps)
            for i in range(1, comps.Length() + 1):
                walk(comps.Value(i))

    roots = TDF_LabelSequence()
    st.GetFreeShapes(roots)
    for i in range(1, roots.Length() + 1):
        walk(roots.Value(i))
    return fixed


def export_gltf(doc, path: str, deflection: float = 0.1) -> None:
    """Pre-triangulate all shapes and export the document as textual glTF.

    RWGltf_CafWriter requires pre-computed triangulation (see module docstring).
    """
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.TDF import TDF_LabelSequence
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.RWGltf import RWGltf_CafWriter
    from OCP.TCollection import TCollection_AsciiString
    from OCP.TColStd import TColStd_IndexedDataMapOfStringString
    from OCP.Message import Message_ProgressRange

    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    labels = TDF_LabelSequence()
    st.GetShapes(labels)
    for i in range(1, labels.Length() + 1):
        shape = st.GetShape_s(labels.Value(i))
        if shape is not None and not shape.IsNull():
            BRepMesh_IncrementalMesh(shape, deflection)

    normalize_component_names(doc)
    w = RWGltf_CafWriter(TCollection_AsciiString(path), False)  # textual glTF
    info = TColStd_IndexedDataMapOfStringString()
    if not w.Perform(doc, info, Message_ProgressRange()):
        raise RuntimeError(f"RWGltf_CafWriter.Perform failed: {path}")


def gltf_dedup(path: str) -> dict:
    """Dedup same-template meshes in an exported glTF (in place).

    RWGltf_CafWriter emits one mesh per instance. Instances of the same
    template share the (normalized) node name, so nodes with an identical
    name are re-pointed to the first node's mesh and redundant meshes are
    dropped. Returns stats {before, after, merged_names}.

    NOTE: this name-keyed rule is sufficient for the generated fixture; the
    production pipeline (Phase A) will key instances by the XCAF template
    label, which is unambiguous even when distinct parts share a name.
    Orphaned accessors/bufferViews in the .bin are left in place (dead bytes
    only) -- compacting buffers is a Phase A implementation detail.
    """
    with open(path, "r", encoding="utf-8") as f:
        g = json.load(f)

    nodes = g.get("nodes", [])
    first_mesh_by_name = {}
    redirect = {}  # mesh index -> kept mesh index
    for nd in nodes:
        name = nd.get("name")
        m = nd.get("mesh")
        if name is None or m is None:
            continue
        if name in first_mesh_by_name:
            kept = first_mesh_by_name[name]
            if kept != m:
                redirect[m] = kept
                nd["mesh"] = kept
        else:
            first_mesh_by_name[name] = m

    merged = sorted({k for k in redirect})
    if merged:
        drop = set(merged)
        g["meshes"] = [m for i, m in enumerate(g["meshes"]) if i not in drop]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(g, f, ensure_ascii=False, indent=1)

    return {
        "before": len(g["meshes"]) + len(merged),
        "after": len(g["meshes"]),
        "merged_names": sorted({nd.get("name") for nd in nodes
                                if nd.get("name") in first_mesh_by_name}),
    }
