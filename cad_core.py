"""CAD geometry core built on OCP (the pythonOCC backend shipped with cadquery).

Supported read/write formats:
  - STEP  (.step / .stp)
  - IGES  (.igs  / .iges)
  - STL   (.stl)            -- mesh
  - BREP  (.brep / .brp)    -- native Open CASCADE

HARD LIMITATION: Siemens NX `.prt` is a proprietary binary format. No
open-source library can read it. To bring a `.prt` into this pipeline, first
export it from NX to STEP/IGES/Parasolid(.x_t), then feed that file here.

This module is import-safe: readers/writers are imported lazily per format so
a missing optional binding never breaks the whole import.
"""
from __future__ import annotations

import html
import json
import os
import threading
from OCP.TopoDS import TopoDS_Shape


def html_escape_text(s) -> str:
    """Escape a string for safe embedding in HTML/XML text or attributes.

    Wraps html.escape(quote=True) so the result is safe inside element text and
    double-quoted attributes alike. Every user-derived string (file names,
    feature names, labels) MUST pass through this before HTML interpolation to
    prevent stored XSS via a maliciously named CAD file.
    """
    return html.escape(str(s), quote=True)


def json_for_script(obj) -> str:
    """Serialize obj to JSON that is safe to embed inside a <script> block.

    Beyond json.dumps(ensure_ascii=False) this neutralises the characters that
    can break out of (or break) a <script> context:
      - '</' -> '<\\/'  (prevents a literal </script> from closing the block)
      - '<'  -> '\\u003c', '>' -> '\\u003e', '&' -> '\\u0026'
      - U+2028 / U+2029 -> '\\u2028' / '\\u2029' (illegal in JS string literals)
    NOTE: the '</' replacement MUST run BEFORE the '<' replacement.
    """
    s = json.dumps(obj, ensure_ascii=False)
    s = s.replace("</", "<\\/")
    s = s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    s = s.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return s


# RLock: re-entrant for the SAME thread -- service-layer handlers already
# hold this lock when calling into build_cache -> write_shape, which enters
# the suppression again (self-deadlock with a plain Lock).
_STDOUT_LOCK = threading.RLock()


class _SuppressStdout:
    """Temporarily redirect the C-level stdout (fd 1) to nul.

    Open CASCADE's STEP/IGES reader & writer print progress banners
    (e.g. '****** Statistics ******', 'Step File Name ... Write Done') to
    stdout. Over an MCP stdio transport those bytes corrupt the JSON-RPC
    stream, so we hide fd 1 for the duration of a chatty call. fd 2 (stderr)
    is left untouched so exceptions/tracebacks stay visible. The original fd
    is always restored, even if the wrapped call raises.
    """

    def __enter__(self):
        # Serialise on a process-wide lock: dup2 manipulates the shared fd 1,
        # so concurrent calls from (e.g.) parallel MCP requests would otherwise
        # clobber each other's saved fd and corrupt the JSON-RPC stream.
        _STDOUT_LOCK.acquire()
        self._devnull = os.open(os.devnull, os.O_WRONLY)
        self._saved = os.dup(1)
        os.dup2(self._devnull, 1)
        return self

    def __exit__(self, *exc):
        # try/finally guarantees the lock is released even if the wrapped call
        # (or the fd restore below) raises.
        try:
            os.dup2(self._saved, 1)
            os.close(self._saved)
            os.close(self._devnull)
        finally:
            _STDOUT_LOCK.release()
        return False


def read_shape(path: str) -> TopoDS_Shape:
    """Read a CAD file into a TopoDS_Shape. Raises ValueError on bad format."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No such file: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext in (".step", ".stp"):
        from OCP.STEPControl import STEPControl_Reader
        from OCP.IFSelect import IFSelect_ReturnStatus
        r = STEPControl_Reader()
        with _SuppressStdout():
            status = r.ReadFile(path)
            if status != IFSelect_ReturnStatus.IFSelect_RetDone:
                raise ValueError(
                    f"无法读取 CAD 文件: {path}（文件损坏或格式不支持）")
            if r.TransferRoots() == 0 or r.NbRootsForTransfer() == 0:
                raise ValueError(
                    f"无法读取 CAD 文件: {path}（文件损坏或格式不支持）")
        shape = r.OneShape()
        if shape.IsNull():
            raise ValueError(
                f"无法读取 CAD 文件: {path}（文件损坏或格式不支持）")
        return shape
    if ext in (".igs", ".iges"):
        from OCP.IGESControl import IGESControl_Reader
        from OCP.IFSelect import IFSelect_ReturnStatus
        r = IGESControl_Reader()
        with _SuppressStdout():
            status = r.ReadFile(path)
            if status != IFSelect_ReturnStatus.IFSelect_RetDone:
                raise ValueError(
                    f"无法读取 CAD 文件: {path}（文件损坏或格式不支持）")
            if r.TransferRoots() == 0 or r.NbRootsForTransfer() == 0:
                raise ValueError(
                    f"无法读取 CAD 文件: {path}（文件损坏或格式不支持）")
        shape = r.OneShape()
        if shape.IsNull():
            raise ValueError(
                f"无法读取 CAD 文件: {path}（文件损坏或格式不支持）")
        return shape
    if ext == ".stl":
        from OCP.StlAPI import StlAPI_Reader
        s = TopoDS_Shape()
        if not StlAPI_Reader().Read(s, path):
            raise RuntimeError(f"STL read failed: {path}")
        return s
    if ext in (".brep", ".brp"):
        from OCP.BRep import BRep_Builder
        from OCP.BRepTools import BRepTools
        b = BRep_Builder()
        s = TopoDS_Shape()
        if not BRepTools.Read(s, path, b):
            raise RuntimeError(f"BREP read failed: {path}")
        return s
    raise ValueError(f"Unsupported input format: '{ext}'. "
                     "Supported: .step/.stp/.igs/.iges/.stl/.brep")


def write_shape(shape: TopoDS_Shape, path: str, *, overwrite: bool = False) -> None:
    """Write a TopoDS_Shape to the given output path (format from extension).

    Refuses to overwrite an existing file by default: if `path` already exists
    (and ``overwrite`` is False) this raises FileExistsError to avoid silently
    destroying user data. Callers that intend to regenerate an output pass
    ``overwrite=True`` (e.g. batch_convert reruns).
    """
    ext = os.path.splitext(path)[1].lower()
    out_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    # Safety: never silently clobber an existing output (M9 / T15) unless the
    # caller explicitly opts in with overwrite=True.
    if os.path.exists(path):
        if overwrite:
            os.remove(path)
        else:
            raise FileExistsError(f"输出文件已存在，拒绝覆盖: {path}")
    if ext in (".step", ".stp"):
        from OCP.STEPControl import STEPControl_Writer, STEPControl_StepModelType
        with _SuppressStdout():
            w = STEPControl_Writer()
            w.Transfer(shape, STEPControl_StepModelType.STEPControl_AsIs)
            w.Write(path)
    elif ext in (".igs", ".iges"):
        from OCP.IGESControl import IGESControl_Writer
        with _SuppressStdout():
            w = IGESControl_Writer()
            w.AddShape(shape)
            w.Write(path)
    elif ext == ".stl":
        from OCP.StlAPI import StlAPI_Writer
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        BRepMesh_IncrementalMesh(shape, 0.1)
        if not StlAPI_Writer().Write(shape, path):
            raise RuntimeError(f"STL write failed: {path}")
    elif ext in (".brep", ".brp"):
        from OCP.BRepTools import BRepTools
        BRepTools.Write(shape, path)
    else:
        raise ValueError(f"Unsupported output format: '{ext}'. "
                         "Supported: .step/.stp/.igs/.iges/.stl/.brep")


def mesh_shape(shape, deflection):
    """Mesh `shape` and return binary STL bytes. Uses a unique temp file
    that is always removed afterwards. Used by the preview generators so
    every call (including concurrent ones) is isolated."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".stl", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.StlAPI import StlAPI_Writer
        BRepMesh_IncrementalMesh(shape, deflection)
        w = StlAPI_Writer()
        w.ASCIIMode = False
        if not w.Write(shape, tmp_path):
            raise RuntimeError("STL mesh write failed")
        with open(tmp_path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def properties(shape: TopoDS_Shape) -> dict:
    """Compute geometric + topological properties of a shape."""
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.Bnd import Bnd_Box
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID, TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer

    p = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, p)
    volume = p.Mass()
    cog = p.CentreOfMass()
    BRepGProp.SurfaceProperties_s(shape, p)
    area = p.Mass()

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    try:
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    except TypeError:
        pt_min, pt_max = box.Get()
        xmin, ymin, zmin = pt_min.X(), pt_min.Y(), pt_min.Z()
        xmax, ymax, zmax = pt_max.X(), pt_max.Y(), pt_max.Z()

    def count(top_abs):
        e = TopExp_Explorer(shape, top_abs)
        n = 0
        while e.More():
            n += 1
            e.Next()
        return n

    solids = count(TopAbs_SOLID)
    return {
        "volume": round(volume, 6),
        "surface_area": round(area, 6),
        "center_of_mass": [round(cog.X(), 6), round(cog.Y(), 6), round(cog.Z(), 6)],
        "bounding_box": {
            "min": [xmin, ymin, zmin],
            "max": [xmax, ymax, zmax],
            "size": [round(xmax - xmin, 6), round(ymax - ymin, 6), round(zmax - zmin, 6)],
        },
        "topology": {
            "solids": solids,
            "faces": count(TopAbs_FACE),
            "edges": count(TopAbs_EDGE),
            "vertices": count(TopAbs_VERTEX),
        },
        "is_assembly": solids > 1,
    }


def centroid_of_faces(faces):
    """Mean of all face centroids (mass-centroid of the union).
    Returns a (x, y, z) tuple or None if input is empty."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    sx, sy, sz, n = 0.0, 0.0, 0.0, 0
    for f in faces:
        try:
            p = GProp_GProps()
            BRepGProp.SurfaceProperties_s(f, p)
            c = p.CentreOfMass()
            sx += c.X(); sy += c.Y(); sz += c.Z(); n += 1
        except Exception:
            continue
    if n == 0:
        return None
    return (sx / n, sy / n, sz / n)


def _build_features_lookup(shape: TopoDS_Shape) -> dict:
    """Build id→face lookup. Currently unused placeholder."""
    return {}


# ---------------------------------------------------------------------------
# Modeling operations -- OCP-native. No third-party builder (e.g. build123d)
# is required. NOTE: build123d was evaluated but crashes on import in this
# environment because a corrupted *system* font breaks fontTools; OCP alone
# covers fillet / chamfer / boolean / drilling / scaling / primitives, which
# is enough to match the "geometry modification" part of the FreeCAD route.
# ---------------------------------------------------------------------------


def box(dx: float, dy: float, dz: float) -> "TopoDS_Shape":
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    return BRepPrimAPI_MakeBox(float(dx), float(dy), float(dz)).Shape()


def cylinder(radius: float, height: float, center=(0.0, 0.0, 0.0),
             direction=(0.0, 0.0, 1.0)) -> "TopoDS_Shape":
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Pnt, gp_Dir
    ax = gp_Ax2(gp_Pnt(*center), gp_Dir(*direction))
    return BRepPrimAPI_MakeCylinder(ax, float(radius), float(height)).Shape()


def boolean(a: "TopoDS_Shape", b: "TopoDS_Shape", op: str = "fuse") -> "TopoDS_Shape":
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse, BRepAlgoAPI_Cut, BRepAlgoAPI_Common
    op = (op or "fuse").lower()
    if op == "fuse":
        algo = BRepAlgoAPI_Fuse(a, b)
    elif op == "cut":
        algo = BRepAlgoAPI_Cut(a, b)
    elif op == "common":
        algo = BRepAlgoAPI_Common(a, b)
    else:
        raise ValueError("op must be fuse|cut|common")
    algo.Build()
    if not algo.IsDone():
        raise RuntimeError(f"Boolean {op} failed")
    return algo.Shape()


def fillet(shape: "TopoDS_Shape", radius: float) -> "TopoDS_Shape":
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS
    mk = BRepFilletAPI_MakeFillet(shape)
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    while exp.More():
        mk.Add(float(radius), TopoDS.Edge(exp.Current()))
        exp.Next()
    mk.Build()
    if not mk.IsDone():
        raise RuntimeError("Fillet failed")
    return mk.Shape()


def chamfer(shape: "TopoDS_Shape", distance: float) -> "TopoDS_Shape":
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS
    mk = BRepFilletAPI_MakeChamfer(shape)
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    while exp.More():
        mk.Add(float(distance), TopoDS.Edge(exp.Current()))
        exp.Next()
    mk.Build()
    if not mk.IsDone():
        raise RuntimeError("Chamfer failed")
    return mk.Shape()


def drill_hole(shape: "TopoDS_Shape", position, direction=(0.0, 0.0, 1.0),
               radius: float = 1.0, depth: float = 10.0) -> "TopoDS_Shape":
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Pnt, gp_Dir
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    ax = gp_Ax2(gp_Pnt(*position), gp_Dir(*direction))
    tool = BRepPrimAPI_MakeCylinder(ax, float(radius), float(depth)).Shape()
    cut = BRepAlgoAPI_Cut(shape, tool)
    cut.Build()
    if not cut.IsDone():
        raise RuntimeError("Drill hole failed")
    return cut.Shape()


def scale_shape(shape: "TopoDS_Shape", factor: float,
                center=(0.0, 0.0, 0.0)) -> "TopoDS_Shape":
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Pnt
    t = gp_Trsf()
    t.SetScale(gp_Pnt(*center), float(factor))
    return BRepBuilderAPI_Transform(shape, t).Shape()
