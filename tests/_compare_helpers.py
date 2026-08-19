"""Reusable geometry-comparison helpers (Phase 3 / TE9).

These pure helpers were extracted (and de-hardcoded) from the development-time
debug scripts that were deleted during the diagnostics cleanup:

  * ``compare_stl_hole.py``   -> :func:`read_stl_vertices`,
                                 :func:`stl_radius_histogram`
  * ``make_section_compare.py`` -> :func:`bore_circles`
  * ``make_xy_compare.py``    -> :func:`list_cylinders`  (SVG rendering dropped)

They contain no rendering code and no machine-specific hard-coded paths, so
they can be reused by regression tests that need to compare two CAD outputs
(e.g. before/after an edit) without re-implementing vertex / cylinder
enumeration each time. The original debug scripts are removed; this module is
the canonical home for that logic.
"""
from __future__ import annotations

import math
import os
import struct
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import cad_core  # noqa: E402

from OCP.TopExp import TopExp_Explorer  # noqa: E402
from OCP.TopAbs import TopAbs_FACE  # noqa: E402
from OCP.TopoDS import TopoDS  # noqa: E402
from OCP.BRepAdaptor import BRepAdaptor_Surface  # noqa: E402
from OCP.GeomAbs import GeomAbs_Cylinder  # noqa: E402
from OCP.Bnd import Bnd_Box  # noqa: E402
from OCP.BRepBndLib import BRepBndLib  # noqa: E402


def read_stl_vertices(path: str, max_abs_x=None):
    """Return a list of ``(y, z)`` vertex pairs parsed from an ASCII STL.

    If ``max_abs_x`` is given, only vertices with ``|x| <= max_abs_x`` are
    kept (a thin slice through the model). Mirrors compare_stl_hole.py.
    """
    out = []
    with open(path, "r", errors="replace") as f:
        for line in f:
            if line.strip().startswith("vertex"):
                _, x, y, z = line.split()
                x, y, z = float(x), float(y), float(z)
                if max_abs_x is None or abs(x) <= max_abs_x:
                    out.append((y, z))
    return out


def stl_radius_histogram(verts, axis_y=0.0, axis_z=0.0, buckets=160,
                         lo=0.0, hi=8.0):
    """Bin vertex distances from ``(axis_y, axis_z)`` into a radius histogram.

    Returns ``(counts, lo, hi)``. Useful to confirm that, after an edit, a
    bore of a given radius still dominates the histogram.
    """
    cnt = [0] * buckets
    for y, z in verts:
        d = math.hypot(y - axis_y, z - axis_z)
        if lo <= d <= hi:
            idx = int((d - lo) / (hi - lo) * buckets)
            idx = min(buckets - 1, max(0, idx))
            cnt[idx] += 1
    return cnt, lo, hi


def bore_circles(step_path: str, section_y=0.0, section_z=0.0):
    """Return every X-dominant cylindrical (bore) face as ``(radius, (y, z))``.

    The circle is reported centred on the given section axis
    ``(section_y, section_z)``. Mirrors make_section_compare.py (the original
    hard-coded 14.5/0.0 section centre is now a parameter).
    """
    shape = cad_core.read_shape(step_path)
    out = []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        f = TopoDS.Face_s(exp.Current())
        a = BRepAdaptor_Surface(f)
        if a.GetType() == GeomAbs_Cylinder:
            cyl = a.Cylinder()
            r = cyl.Radius()
            d = cyl.Axis().Direction()
            if abs(d.X()) > abs(d.Y()) and abs(d.X()) > abs(d.Z()) and r >= 1.0:
                out.append((r, (section_y, section_z)))
        exp.Next()
    return out


def list_cylinders(path: str):
    """Enumerate every cylindrical face as a dict ``{r, ax_loc, dir, bbox}``.

    Mirrors make_xy_compare.py (the SVG-rendering half was dropped — this is
    the pure geometry enumeration that tests actually reuse).
    """
    s = cad_core.read_shape(path)
    rows = []
    exp = TopExp_Explorer(s, TopAbs_FACE)
    while exp.More():
        f = TopoDS.Face_s(exp.Current())
        ad = BRepAdaptor_Surface(f)
        if ad.GetType() == GeomAbs_Cylinder:
            cyl = ad.Cylinder()
            r = cyl.Radius()
            loc = cyl.Axis().Location()
            d = cyl.Axis().Direction()
            bb = Bnd_Box()
            BRepBndLib.Add_s(f, bb)
            x1, y1, z1, x2, y2, z2 = bb.Get()
            rows.append({
                "r": r,
                "ax_loc": (loc.X(), loc.Y(), loc.Z()),
                "dir": (d.X(), d.Y(), d.Z()),
                "bbox": (x1, y1, z1, x2, y2, z2),
            })
        exp.Next()
    return rows


def count_binary_stl_triangles(path: str) -> int:
    """Count triangles in a binary STL file (80-byte header + uint32 count)."""
    with open(path, "rb") as f:
        header = f.read(84)
    if len(header) < 84:
        raise ValueError(f"not a valid binary STL (too short): {path}")
    return struct.unpack("<I", header[80:84])[0]
