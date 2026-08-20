#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CAD feature detection library (serves feature_picker / cad_assembly).

Scans a STEP/IGES/STL/BREP solid, enumerates every analytic feature
(holes, bores, bosses, fillets, freeform regions), then:

  * merges coaxial nested cylinders into ONE composite feature
    (e.g. a counterbore shows as Ø12 / Ø6, not two overlapping entries)
  * detects bolt-circle / cluster patterns and reports them as ONE group
    ("螺栓孔组 ×N") instead of N separate features
  * auto-classifies each feature as 孔 (void) vs 凸台 (boss)
  * identifies threads against their host bores

Turns an ambiguous "enlarge the small hole" into an unambiguous
"enlarge feature #N". Pure OCP (no FreeCAD needed).

The historical 2D numbered-badge SVG/HTML outlet was retired (measured
poor in practice); the Web viewport's feature panel + click highlight is
the interactive surface now. This module is a library — no CLI.
"""
from __future__ import annotations

import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cad_core  # noqa: E402

from OCP.TopExp import TopExp_Explorer  # noqa: E402
from OCP.TopAbs import TopAbs_FACE, TopAbs_VERTEX, TopAbs_IN, TopAbs_OUT  # noqa: E402
from OCP.TopoDS import TopoDS  # noqa: E402
from OCP.BRepAdaptor import BRepAdaptor_Surface  # noqa: E402
from OCP.GeomAbs import (GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Torus,  # noqa: E402
                         GeomAbs_Sphere, GeomAbs_Plane)
from OCP.BRep import BRep_Tool  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402
from OCP.BRepClass3d import BRepClass3d_SolidClassifier  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Any  # noqa: E402


# --------------------------------------------------------------------------
# explicit feature model
# --------------------------------------------------------------------------
@dataclass
class Feature:
    """A grouped CAD feature (an analytic revolved face group, a plane region,
    or a freeform region) produced by ``group_features``.

    Mirrors the dict previously returned by group_features; the render path and
    feature_picker now read typed attributes instead of a fragile ``[...]``
    dict contract (H7)."""
    stype: str
    axis: str | None
    loc: tuple
    radii: list
    radius: float
    extent: float
    loc3: tuple
    composite: bool
    faces: list
    id: Any = None
    kind: str | None = None
    normal: Any = None
    offset: float | None = None


FEATURE_TYPE_REGISTRY = {
    "cylinder": {"color_hole": "#e63946", "color_boss": "#2a7d3b", "color_default": "#8a8f98",
                 "label_hole": "孔", "label_boss": "凸台"},
    "cone":     {"color_hole": "#e63946", "color_boss": "#2a7d3b", "color_default": "#8a8f98",
                 "label_hole": "锥孔", "label_boss": "锥台"},
    "torus":    {"color": "#e08600", "kind": "fillet", "label": "圆角"},
    "sphere":   {"color_hole": "#e63946", "color_boss": "#2a7d3b", "color_default": "#8a8f98",
                 "label_hole": "球凹", "label_boss": "球凸"},
    "plane":    {"color": "#8a8f98", "kind": "surface", "label": "平面"},
    "freeform": {"color": "#8a8f98", "kind": "surface", "label": "自由曲面"},
    "bolt_pattern": {"color": "#8a6d3b", "label": "螺栓孔组"},
}


def feature_color(stype, kind=None):
    r = FEATURE_TYPE_REGISTRY.get(stype, {})
    if "color" in r:
        return r["color"]
    if kind == "hole":
        return r.get("color_hole", "#8a8f98")
    if kind == "boss":
        return r.get("color_boss", "#8a8f98")
    return r.get("color_default", "#8a8f98")


def feature_label(stype, kind=None, composite=False):
    r = FEATURE_TYPE_REGISTRY.get(stype, {})
    if "label" in r:
        base = r["label"]
    else:
        base = r.get("label_hole" if kind == "hole" else "label_boss", "特征")
    if composite:
        base = "复合沉孔" if kind == "hole" else "复合凸台"
    return base


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------
def vertices_of(shape):
    pts = []
    exp = TopExp_Explorer(shape, TopAbs_VERTEX)
    while exp.More():
        v = TopoDS.Vertex_s(exp.Current())
        p = BRep_Tool.Pnt_s(v)
        pts.append((p.X(), p.Y(), p.Z()))
        exp.Next()
    return pts


def part_bbox(shape):
    xs, ys, zs = [], [], []
    for x, y, z in vertices_of(shape):
        xs.append(x); ys.append(y); zs.append(z)
    if not xs:
        # Degenerate / empty model (no vertices): callers must guard this and
        # produce a sparse but valid result instead of crashing on min([]).
        return None
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def _canon_axis(d):
    ax = max(abs(d.X()), abs(d.Y()), abs(d.Z()))
    if ax == abs(d.X()):
        return "X"
    if ax == abs(d.Y()):
        return "Y"
    return "Z"


def classify(shape, loc3, axis, extent):
    """Return 'hole' if the cylinder axis-center midpoint lies OUTSIDE the
    solid (i.e. it is a cavity), 'boss' if INSIDE the material."""
    mid = [loc3[0], loc3[1], loc3[2]]
    off = extent * 0.25 if extent > 0 else 0.0
    idx = {"X": 0, "Y": 1, "Z": 2}[axis]
    mid[idx] += off
    sc = BRepClass3d_SolidClassifier(shape, gp_Pnt(*mid), 1e-4)
    st = sc.State()
    return "boss" if st == TopAbs_IN else "hole"


def collect_features(shape):
    """Enumerate EVERY boundary face and tag it by surface type so the whole
    solid structure is represented (not just cylinders).

    Analytic revolved faces (cylinder/cone/torus/sphere) get an axis + a
    perp-radius range derived from their vertices; planes get a normal +
    offset; everything else (bspline / freeform) is kept as a generic
    surface region. Returns a flat list; group_features() then aggregates
    them into features by category."""
    out = []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        f = TopoDS.Face_s(exp.Current())
        ad = BRepAdaptor_Surface(f)
        gtype = ad.GetType()
        pts = vertices_of(f)
        if not pts:
            exp.Next()
            continue
        if gtype in (GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Torus, GeomAbs_Sphere):
            if gtype == GeomAbs_Cylinder:
                ax = ad.Cylinder().Axis(); stype = "cylinder"
            elif gtype == GeomAbs_Cone:
                ax = ad.Cone().Axis(); stype = "cone"
            elif gtype == GeomAbs_Torus:
                ax = ad.Torus().Axis(); stype = "torus"
            else:
                ax = ad.Sphere().Position().Axis(); stype = "sphere"
            d = ax.Direction()
            axis = _canon_axis(d)
            if axis == "X":
                perp = [(p[1], p[2]) for p in pts]; axial = [p[0] for p in pts]
            elif axis == "Y":
                perp = [(p[0], p[2]) for p in pts]; axial = [p[1] for p in pts]
            else:
                perp = [(p[0], p[1]) for p in pts]; axial = [p[2] for p in pts]
            rs = [math.hypot(qx, qy) for qx, qy in perp]
            rmin, rmax = min(rs), max(rs)
            # R1 correctness: revolved faces have seam-only topology vertices
            # (e.g. a cylinder face's vertices sit on the seam), so the vertex
            # average is offset from the true axis. Use the ANALYTIC axis
            # position for the center; axial mid from the vertex extent.
            loc_ax = ax.Location()
            axial_mid = (min(axial) + max(axial)) / 2.0
            if axis == "X":
                cx, cy = loc_ax.Y(), loc_ax.Z()
                loc3 = (axial_mid, loc_ax.Y(), loc_ax.Z())
            elif axis == "Y":
                cx, cy = loc_ax.X(), loc_ax.Z()
                loc3 = (loc_ax.X(), axial_mid, loc_ax.Z())
            else:
                cx, cy = loc_ax.X(), loc_ax.Y()
                loc3 = (loc_ax.X(), loc_ax.Y(), axial_mid)
            radii = sorted({round(rmax, 4), round(rmin, 4)}, reverse=True)
            out.append({"stype": stype, "axis": axis,
                        "loc": (cx, cy), "extent": max(axial) - min(axial),
                        "loc3": loc3, "face": f, "radii": radii})
        elif gtype == GeomAbs_Plane:
            pl = ad.Plane()
            n = pl.Axis().Direction()
            p0 = pts[0]
            off = p0[0] * n.X() + p0[1] * n.Y() + p0[2] * n.Z()
            loc3 = (sum(p[0] for p in pts) / len(pts),
                    sum(p[1] for p in pts) / len(pts),
                    sum(p[2] for p in pts) / len(pts))
            out.append({"stype": "plane", "axis": None,
                        "normal": (n.X(), n.Y(), n.Z()), "offset": off,
                        "loc": (loc3[0], loc3[1]), "extent": 0.0,
                        "loc3": loc3, "face": f, "radii": []})
        else:
            # bspline / surface-of-revolution-extrusion / offset / other
            loc3 = (sum(p[0] for p in pts) / len(pts),
                    sum(p[1] for p in pts) / len(pts),
                    sum(p[2] for p in pts) / len(pts))
            out.append({"stype": "freeform", "axis": None,
                        "loc": (loc3[0], loc3[1]), "extent": 0.0,
                        "loc3": loc3, "face": f, "radii": []})
        exp.Next()
    return out


def group_features(feats, loc_tol=0.5, plane_tol=0.5):
    """Aggregate enumerated faces into features BY CATEGORY:

      * analytic revolved faces (cylinder/cone/torus/sphere) sharing the
        same axis + (binned) perpendicular location are merged into one
        feature -- coaxial holes / bosses / fillets collapse here.
      * planes sharing normal + offset (coplanar) merge into one flat region.
      * freeform faces stay individual (no parameterisation possible).
    Returns a flat list of Feature objects (each carries its `faces`)."""
    buckets = {}
    for c in feats:
        if c["axis"] is not None:
            key = ("A", c["axis"], round(c["loc"][0] / loc_tol),
                   round(c["loc"][1] / loc_tol))
        elif c["stype"] == "plane":
            nb = (round(c["normal"][0] / 0.1), round(c["normal"][1] / 0.1),
                  round(c["normal"][2] / 0.1))
            key = ("P", nb, round(c["offset"] / plane_tol))
        else:
            key = ("F", id(c))
        buckets.setdefault(key, []).append(c)
    comps = []
    for key, items in buckets.items():
        st = items[0]["stype"]
        if st in ("cylinder", "cone", "torus", "sphere"):
            axis = items[0]["axis"]
            loc = (sum(i["loc"][0] for i in items) / len(items),
                   sum(i["loc"][1] for i in items) / len(items))
            loc3 = items[0]["loc3"]
            radii = sorted({round(r, 4) for i in items for r in i["radii"] if r > 0},
                           reverse=True)
            extent = max(i["extent"] for i in items)
            composite = (st in ("cylinder", "cone", "torus", "sphere") and len(radii) > 1)
            comps.append(Feature(
                stype=st, axis=axis, loc=loc, radii=radii,
                radius=radii[0] if radii else 0.0, extent=extent,
                loc3=loc3, composite=composite,
                faces=[i["face"] for i in items]))
        elif st == "plane":
            comps.append(Feature(
                stype="plane", axis=None, loc=items[0]["loc"],
                radii=[], radius=0.0, extent=0.0,
                loc3=items[0]["loc3"], composite=False,
                faces=[i["face"] for i in items],
                normal=items[0]["normal"], offset=items[0]["offset"]))
        else:
            comps.append(Feature(
                stype="freeform", axis=None, loc=items[0]["loc"],
                radii=[], radius=0.0, extent=0.0,
                loc3=items[0]["loc3"], composite=False,
                faces=[items[0]["face"]]))
    _cat = {"cylinder": 0, "cone": 1, "torus": 2, "sphere": 3,
            "plane": 4, "freeform": 5}
    comps.sort(key=lambda c: (_cat.get(c.stype, 9), -c.radius,
                               c.loc[0], c.loc[1]))
    return comps


def assign_ids(singles, patterns):
    """Stamp stable, category-prefixed ids:

      * bolt-circle pattern groups -> P1, P2, ...
      * analytic revolved features (holes/bosses/cones/fillets/spheres) -> #1, #2, ...
      * planar regions   -> L1, L2, ...
      * freeform regions -> S1, S2, ...
    Mutates the Feature objects / pattern dicts in place."""
    for i, p in enumerate(patterns, 1):
        p["id"] = f"P{i}"
    an = [c for c in singles if c.axis is not None and c.stype != "torus"]
    to = [c for c in singles if c.stype == "torus"]
    pl = [c for c in singles if c.stype == "plane"]
    fr = [c for c in singles if c.stype == "freeform"]
    for j, c in enumerate(an, 1):
        c.id = j
    for j, c in enumerate(to, 1):
        c.id = f"F{j}"
    for j, c in enumerate(pl, 1):
        c.id = f"L{j}"
    for j, c in enumerate(fr, 1):
        c.id = f"S{j}"


def detect_patterns(comps, same_r=0.05, same_ext=0.3, ring_tol=0.12, min_n=3):
    """Detect bolt-circle / cluster patterns: >=3 features sharing the same
    axis + primary radius + axial length, arranged roughly on a common
    circle centered at their centroid."""
    buckets = {}
    singles, patterns = [], []
    for c in comps:
        if c.axis is None:
            singles.append(c)
            continue
        r = c.radii[0]
        key = (c.axis, round(r / same_r), round(c.extent / same_ext))
        buckets.setdefault(key, []).append(c)
    for key, items in buckets.items():
        if len(items) < min_n:
            singles.extend(items)
            continue
        cx = sum(i.loc[0] for i in items) / len(items)
        cy = sum(i.loc[1] for i in items) / len(items)
        dists = [math.hypot(i.loc[0] - cx, i.loc[1] - cy) for i in items]
        mean_d = sum(dists) / len(dists)
        if mean_d < 1e-6:
            singles.extend(items)
            continue
        std = math.sqrt(sum((d - mean_d) ** 2 for d in dists) / len(dists))
        if std / mean_d < ring_tol:
            patterns.append({"axis": items[0].axis, "center": (cx, cy),
                             "pitch": mean_d, "radius": items[0].radii[0],
                             "count": len(items),
                             "holes": [(i.loc[0], i.loc[1]) for i in items],
                             "extent": items[0].extent,
                             "faces": [f for i in items for f in i.faces]})
        else:
            singles.extend(items)
    return singles, patterns


def choose_axis(feats):
    from collections import Counter

    def _ax(f):
        # singles are Feature objects; patterns are still plain dicts.
        return f.axis if isinstance(f, Feature) else f.get("axis")

    cnt = Counter(_ax(f) for f in feats if _ax(f) is not None)
    if not cnt:
        return "X"
    best = max(cnt.items(), key=lambda kv: (kv[1], {"X": 3, "Y": 2, "Z": 1}[kv[0]]))
    return best[0]


# --------------------------------------------------------------------------
# thread (freeform -> helical) recognition
# --------------------------------------------------------------------------
def radial_of(pts, A):
    """Return (radii, axial_coords) for points relative to axis A."""
    if A == "X":
        return [math.hypot(p[1], p[2]) for p in pts], [p[0] for p in pts]
    if A == "Y":
        return [math.hypot(p[0], p[2]) for p in pts], [p[1] for p in pts]
    return [math.hypot(p[0], p[1]) for p in pts], [p[2] for p in pts]


def _fit_axis_for_freeform(pts):
    """A freeform face that is a thread segment wraps a single axis: the
    radius (distance orthogonal to that axis) varies only by the thread
    tooth height, while the other two axes span the diameter. So the wrap
    axis is the one minimising the orthogonal-radius range."""
    best = None
    for A in ("X", "Y", "Z"):
        rad, axc = radial_of(pts, A)
        tooth = max(rad) - min(rad)
        if best is None or tooth < best[1]:
            best = (A, tooth, (min(axc), max(axc)))
    return best


def _fit_pitch(pts, A):
    """Best-effort lead/pitch from the periodic radius variation."""
    rad, axc = radial_of(pts, A)
    try:
        import numpy as np
        ts = np.asarray(axc, float)
        rs = np.asarray(rad, float)
        order = np.argsort(ts)
        ts, rs = ts[order], rs[order]
        if ts[-1] - ts[0] < 1e-6:
            return {}
        n = max(80, int((ts[-1] - ts[0]) / 0.04))
        tu = np.linspace(ts[0], ts[-1], n)
        ru = np.interp(tu, ts, rs)
        d2 = np.diff(np.sign(np.diff(ru)))
        pk = np.where(d2 < 0)[0] + 1
        if len(pk) >= 2:
            gaps = np.diff(tu[pk])
            pitch = float(np.median(gaps))
            if 0.25 < pitch < 12.0 and np.std(gaps) / pitch < 0.6:
                return {"pitch": round(pitch, 3)}
    except Exception:
        pass
    return {}


def _fit_handedness(pts, A):
    """Right/left handedness from the sign of d(theta)/d(axial)."""
    if A == "Y":
        ang = [math.atan2(p[2], p[0]) for p in pts]
    elif A == "X":
        ang = [math.atan2(p[2], p[1]) for p in pts]
    else:
        ang = [math.atan2(p[1], p[0]) for p in pts]
    _, axc = radial_of(pts, A)
    order = sorted(range(len(axc)), key=lambda i: axc[i])
    prev_t, prev_a, acc, n = None, None, 0.0, 0
    for i in order:
        a = ang[i]
        t = axc[i]
        if prev_t is not None and t - prev_t > 1e-6:
            d = a - prev_a
            while d > math.pi:
                d -= 2 * math.pi
            while d < -math.pi:
                d += 2 * math.pi
            if abs(d) > 1e-4:
                acc += d
                n += 1
        prev_t, prev_a = t, a
    if n >= 3 and abs(acc) > 0.3:
        return "RH" if acc > 0 else "LH"
    return "?"


def _cyl_axial_range(c):
    idx = {"X": 0, "Y": 1, "Z": 2}[c["axis"]]
    m = c["loc3"][idx]
    half = c["extent"] / 2.0
    return (m - half, m + half)


def _find_host_cyl(cyls, A, major, root, amin, amax, rad_tol=3.0, ax_tol=8.0):
    """Attach a thread to the bore/boss whose wall it is cut into.

    A thread sits on the wall of a cylindrical bore (or on a cylindrical
    boss): its crest/root band hugs the bore radius, the bore must be long
    enough to actually carry the thread, and the thread must overlap the
    bore axially. Return the best matching cylinder id, or None when no
    bore qualifies (=> the thread is a standalone thread feature TH#).

    Hard constraints (the old match was radius-only and happily attached a
    13.7 mm thread to a 0.03 mm paper-thin disc):
      * bore axial extent  >= 0.5 * thread_length   (exclude thin discs)
      * axial overlap      >= 0.35 * thread_length  (thread really sits there)
    """
    length = amax - amin
    if length <= 0:
        return None
    env = 1.5                       # thread tooth envelope tolerance (mm)
    best, best_score, best_id = None, 1e9, None
    for c in cyls:
        if c["axis"] != A or not c["radii"]:
            continue
        cr = c["radii"]
        cmin, cmax = min(cr), max(cr)
        # radial: bore wall must lie within the thread tooth envelope
        # (root .. major), so the thread visibly rides on that wall.
        radial_ok = (cmin - env <= major <= cmax + env) or \
                    (cmin - env <= root <= cmax + env) or \
                    (abs((cmin + cmax) / 2 - major) < rad_tol)
        if not radial_ok:
            continue
        ca, cb = _cyl_axial_range(c)
        host_len = cb - ca
        if host_len < 0.5 * length:
            continue
        ov = min(cb, amax) - max(ca, amin)
        if ov <= 0 or ov < 0.35 * length:
            continue
        radial_err = min(abs(cmax - major), abs(cmin - major),
                         abs(cmax - root), abs(cmin - root))
        score = radial_err + 0.15 * max(0.0, length - ov)
        # tie-break: smaller id wins (stable, intuitive)
        if score < best_score or (abs(score - best_score) < 1e-9 and c["id"] < best_id):
            best_score, best, best_id = score, c["id"], c["id"]
    return best


# identify_threads() is WIP / NOT yet integrated into main() or
# feature_picker.build() -- it is dead code until the thread-recognition
# render path is wired up. Leave it untouched from the render path; the
# internal dict access below assumes the pre-Feature raw feature dicts and
# is only meaningful once threads are actually integrated. Do NOT attempt
# the T13 O(n^2) radial-binning optimisation here: that is wasted effort on
# code that is not on any live path (and would risk golden-test behaviour).
def identify_threads(singles, cyls, axial_gap=2.0, pos_tol=3.0, rad_tol=2.0):
    """Cluster freeform faces into helical (thread) features.

    WIP / NOT YET INTEGRATED into main() or feature_picker.build() -- see the
    module-level note above. Known characteristic: the clustering is O(n^2)
    in the number of freeform faces (the pairwise radius/axial overlap loops
    below). That is acceptable for a future thread pass but is intentionally
    left un-optimised while the function is dead code.

    Returns (threads, host_map) where each thread is a dict with axis,
    major/min radius, length, pitch/handedness (best-effort) and the list
    of source faces; host_map maps a thread to the id of the cylinder it
    is attached to (or None for a standalone thread)."""
    ff = [c for c in singles if c["stype"] == "freeform"]
    items = []
    for c in ff:
        pts = vertices_of(c["faces"][0])
        if len(pts) < 4:
            continue
        fit = _fit_axis_for_freeform(pts)
        A, _, (amin, amax) = fit
        rad, _ = radial_of(pts, A)
        pcx = sum(p[0] for p in pts) / len(pts)
        pcy = sum(p[1] for p in pts) / len(pts)
        pcz = sum(p[2] for p in pts) / len(pts)
        items.append({"c": c, "axis": A, "amin": amin, "amax": amax,
                      "pc": (pcx, pcy, pcz),
                      "rmin": min(rad), "rmax": max(rad), "pts": pts})
    # cluster by axis, then merge into helical threads in two stages:
    #   1) radial connectivity  -- faces whose radius band is close belong
    #      to the same bore (robust to tessellation splitting one thread
    #      into several BRep faces);
    #   2) axial overlap (transitive closure) within each radial component
    #      -- faces that share an axial span belong to the same thread.
    # This avoids the failure of a naive amin-sorted greedy merge, where
    # alternating faces of different bores split one thread apart.
    by_axis = {}
    for it in items:
        by_axis.setdefault(it["axis"], []).append(it)
    clusters = []

    def _uf(n):
        parent = list(range(n))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        return parent, find, union

    for A, its in by_axis.items():
        n = len(its)
        if n == 0:
            continue
        # stage 1: radial connectivity only (candidate same-bore faces)
        parent, find, union = _uf(n)
        for i in range(n):
            mi = (its[i]["rmin"] + its[i]["rmax"]) / 2
            for j in range(i + 1, n):
                mj = (its[j]["rmin"] + its[j]["rmax"]) / 2
                if abs(mi - mj) < 4.0:
                    union(i, j)
        rad_comps = {}
        for i in range(n):
            rad_comps.setdefault(find(i), []).append(i)
        # stage 2: within each radial component, merge by axial OVERLAP only
        # (independent UF, so a radial band spanning two separate axial
        # locations -- e.g. two distinct bores of the same diameter -- is
        # kept as separate threads).
        for comp in rad_comps.values():
            m = len(comp)
            if m == 1:
                clusters.append([its[comp[0]]])
                continue
            p2, f2, u2 = _uf(m)
            for a in range(m):
                for b in range(a + 1, m):
                    ii, jj = comp[a], comp[b]
                    if its[ii]["amin"] <= its[jj]["amax"] and \
                            its[jj]["amin"] <= its[ii]["amax"]:
                        u2(a, b)
            sub = {}
            for a in range(m):
                sub.setdefault(f2(a), []).append(its[comp[a]])
            clusters.extend(sub.values())

    threads, host_map = [], {}
    for cl in clusters:
        allpts = [p for it in cl for p in it["pts"]]
        A = cl[0]["axis"]
        rad, _ = radial_of(allpts, A)
        major, root = max(rad), min(rad)
        amin = min(it["amin"] for it in cl)
        amax = max(it["amax"] for it in cl)
        length = amax - amin
        tooth = major - root
        if length > 1.5 and 0.15 < tooth < 6.0:
            th = {"axis": A, "major": round(major, 3), "root": round(root, 3),
                  "length": round(length, 3), "faces": [it["c"]["faces"][0] for it in cl],
                  "pts": allpts, "amin": amin, "amax": amax}
            th.update(_fit_pitch(allpts, A))
            th["hand"] = _fit_handedness(allpts, A)
            host = _find_host_cyl(cyls, A, major, root, amin, amax)
            th["host"] = host
            threads.append(th)
            host_map[id(th)] = host
            # mark the source freeform faces as consumed so they are no
            # longer listed as raw freeform features -- the thread now
            # represents them (attached to a host cylinder or as TH#).
            # NOTE: `_consumed` is only meaningful once threads are wired
            # into the render path; it is currently dead code.
            for it in cl:
                it["c"]["_consumed"] = True
    # assign TH# ids to standalone threads
    n = 0
    for th in threads:
        if th["host"] is None:
            n += 1
            th["id"] = f"TH{n}"
        else:
            th["id"] = None
    return threads, host_map
