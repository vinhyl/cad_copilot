#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Auto-number CAD feature locator (v2 — decluttered).

Scans a STEP/IGES/STL/BREP solid, enumerates every cylindrical feature
(holes, bores, bosses), then:

  * merges coaxial nested cylinders into ONE composite feature
    (e.g. a counterbore shows as Ø12 / Ø6, not two overlapping circles)
  * detects bolt-circle / cluster patterns and draws them as ONE group
    ("螺栓孔组 ×N") instead of N overlapping dots
  * auto-classifies each feature as 孔 (void) vs 凸台 (boss)
  * places numbered badges with leader lines using a greedy
    collision-avoidance layout, so every number clearly ties to one shape
  * makes the SVG interactive: hover a badge <-> table row cross-highlights

Turns an ambiguous "enlarge the small hole" into an unambiguous
"enlarge feature #N". Pure OCP (no FreeCAD needed).

Usage:
    feature_locator.py <input.step> [--axis auto|x|y|z] [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cad_core  # noqa: E402

from OCP.TopExp import TopExp_Explorer  # noqa: E402
from OCP.TopAbs import TopAbs_FACE, TopAbs_VERTEX, TopAbs_IN, TopAbs_OUT  # noqa: E402
from OCP.TopoDS import TopoDS  # noqa: E402
from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve  # noqa: E402
from OCP.GeomAbs import (GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Torus,  # noqa: E402
                         GeomAbs_Sphere, GeomAbs_Plane)
from OCP.BRep import BRep_Tool  # noqa: E402
from OCP.gp import gp_Dir, gp_Pnt, gp_Ax2  # noqa: E402
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape  # noqa: E402
from OCP.BRepClass3d import BRepClass3d_SolidClassifier  # noqa: E402


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------
def _vertices_of(shape):
    pts = []
    exp = TopExp_Explorer(shape, TopAbs_VERTEX)
    while exp.More():
        v = TopoDS.Vertex_s(exp.Current())
        p = BRep_Tool.Pnt_s(v)
        pts.append((p.X(), p.Y(), p.Z()))
        exp.Next()
    return pts


def _part_bbox(shape):
    xs, ys, zs = [], [], []
    for x, y, z in _vertices_of(shape):
        xs.append(x); ys.append(y); zs.append(z)
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def _canon_axis(d):
    ax = max(abs(d.X()), abs(d.Y()), abs(d.Z()))
    if ax == abs(d.X()):
        return "X"
    if ax == abs(d.Y()):
        return "Y"
    return "Z"


def _classify(shape, loc3, axis, extent):
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
        pts = _vertices_of(f)
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
                ax = ad.Sphere().Axis(); stype = "sphere"
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
            cx = sum(qx for qx, qy in perp) / len(perp)
            cy = sum(qy for qx, qy in perp) / len(perp)
            loc3 = (sum(p[0] for p in pts) / len(pts),
                    sum(p[1] for p in pts) / len(pts),
                    sum(p[2] for p in pts) / len(pts))
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
    Returns a flat list of feature dicts (each carries its `faces`)."""
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
            comps.append({"stype": st, "axis": axis, "loc": loc, "radii": radii,
                          "radius": radii[0] if radii else 0.0, "extent": extent,
                          "loc3": loc3, "composite": composite,
                          "faces": [i["face"] for i in items]})
        elif st == "plane":
            comps.append({"stype": "plane", "axis": None, "loc": items[0]["loc"],
                          "radii": [], "radius": 0.0, "extent": 0.0,
                          "loc3": items[0]["loc3"], "normal": items[0]["normal"],
                          "composite": False, "faces": [i["face"] for i in items]})
        else:
            comps.append({"stype": "freeform", "axis": None, "loc": items[0]["loc"],
                          "radii": [], "radius": 0.0, "extent": 0.0,
                          "loc3": items[0]["loc3"], "composite": False,
                          "faces": [items[0]["face"]]})
    _cat = {"cylinder": 0, "cone": 1, "torus": 2, "sphere": 3,
            "plane": 4, "freeform": 5}
    comps.sort(key=lambda c: (_cat.get(c["stype"], 9), -c["radius"],
                               c["loc"][0], c["loc"][1]))
    return comps


def assign_ids(singles, patterns):
    """Stamp stable, category-prefixed ids:

      * bolt-circle pattern groups -> P1, P2, ...
      * analytic revolved features (holes/bosses/cones/fillets/spheres) -> #1, #2, ...
      * planar regions   -> L1, L2, ...
      * freeform regions -> S1, S2, ...
    Mutates the dicts in place."""
    for i, p in enumerate(patterns, 1):
        p["id"] = f"P{i}"
    an = [c for c in singles if c["axis"] is not None and c["stype"] != "torus"]
    to = [c for c in singles if c["stype"] == "torus"]
    pl = [c for c in singles if c["stype"] == "plane"]
    fr = [c for c in singles if c["stype"] == "freeform"]
    for j, c in enumerate(an, 1):
        c["id"] = j
    for j, c in enumerate(to, 1):
        c["id"] = f"F{j}"
    for j, c in enumerate(pl, 1):
        c["id"] = f"L{j}"
    for j, c in enumerate(fr, 1):
        c["id"] = f"S{j}"


def detect_patterns(comps, same_r=0.05, same_ext=0.3, ring_tol=0.12, min_n=3):
    """Detect bolt-circle / cluster patterns: >=3 features sharing the same
    axis + primary radius + axial length, arranged roughly on a common
    circle centered at their centroid."""
    buckets = {}
    singles, patterns = [], []
    for c in comps:
        if c["axis"] is None:
            singles.append(c)
            continue
        r = c["radii"][0]
        key = (c["axis"], round(r / same_r), round(c["extent"] / same_ext))
        buckets.setdefault(key, []).append(c)
    for key, items in buckets.items():
        if len(items) < min_n:
            singles.extend(items)
            continue
        cx = sum(i["loc"][0] for i in items) / len(items)
        cy = sum(i["loc"][1] for i in items) / len(items)
        dists = [math.hypot(i["loc"][0] - cx, i["loc"][1] - cy) for i in items]
        mean_d = sum(dists) / len(dists)
        if mean_d < 1e-6:
            singles.extend(items)
            continue
        std = math.sqrt(sum((d - mean_d) ** 2 for d in dists) / len(dists))
        if std / mean_d < ring_tol:
            patterns.append({"axis": items[0]["axis"], "center": (cx, cy),
                             "pitch": mean_d, "radius": items[0]["radii"][0],
                             "count": len(items),
                             "holes": [(i["loc"][0], i["loc"][1]) for i in items],
                             "extent": items[0]["extent"],
                             "faces": [f for i in items for f in i["faces"]]})
        else:
            singles.extend(items)
    return singles, patterns


def choose_axis(feats):
    from collections import Counter
    cnt = Counter(f["axis"] for f in feats if f.get("axis") is not None)
    if not cnt:
        return "X"
    best = max(cnt.items(), key=lambda kv: (kv[1], {"X": 3, "Y": 2, "Z": 1}[kv[0]]))
    return best[0]


# --------------------------------------------------------------------------
# thread (freeform -> helical) recognition
# --------------------------------------------------------------------------
def _radial_of(pts, A):
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
        rad, axc = _radial_of(pts, A)
        tooth = max(rad) - min(rad)
        if best is None or tooth < best[1]:
            best = (A, tooth, (min(axc), max(axc)))
    return best


def _fit_pitch(pts, A):
    """Best-effort lead/pitch from the periodic radius variation."""
    rad, axc = _radial_of(pts, A)
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
    _, axc = _radial_of(pts, A)
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


def identify_threads(singles, cyls, axial_gap=2.0, pos_tol=3.0, rad_tol=2.0):
    """Cluster freeform faces into helical (thread) features.

    Returns (threads, host_map) where each thread is a dict with axis,
    major/min radius, length, pitch/handedness (best-effort) and the list
    of source faces; host_map maps a thread to the id of the cylinder it
    is attached to (or None for a standalone thread)."""
    ff = [c for c in singles if c["stype"] == "freeform"]
    items = []
    for c in ff:
        pts = _vertices_of(c["faces"][0])
        if len(pts) < 4:
            continue
        fit = _fit_axis_for_freeform(pts)
        A, _, (amin, amax) = fit
        rad, _ = _radial_of(pts, A)
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
        rad, _ = _radial_of(allpts, A)
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


# --------------------------------------------------------------------------
# projection
# --------------------------------------------------------------------------
def project_edges(shape, axis):
    from OCP.HLRAlgo import HLRAlgo_Projector
    from OCP.TopAbs import TopAbs_EDGE
    axes = {"X": gp_Dir(1, 0, 0), "Y": gp_Dir(0, 1, 0), "Z": gp_Dir(0, 0, 1)}
    algo = HLRBRep_Algo()
    algo.Add(shape)
    algo.Projector(HLRAlgo_Projector(gp_Ax2(gp_Pnt(0, 0, 0), axes[axis])))
    algo.Update()
    hlr = HLRBRep_HLRToShape(algo)
    edges = hlr.VCompound()
    if edges is None:
        return None

    def coords(p):
        if axis == "X":
            return (p.Y(), p.Z())
        if axis == "Y":
            return (p.X(), p.Z())
        return (p.X(), p.Y())

    polylines = []
    exp = TopExp_Explorer(edges, TopAbs_EDGE)
    while exp.More():
        e = TopoDS.Edge_s(exp.Current())
        ad = BRepAdaptor_Curve(e)
        first, last = ad.FirstParameter(), ad.LastParameter()
        n = max(2, int((last - first) * 4))
        line = []
        for i in range(n + 1):
            t = first + (last - first) * i / n
            p = ad.Value(t)
            line.append(coords(p))
        polylines.append(line)
        exp.Next()
    return polylines


# --------------------------------------------------------------------------
# label layout (greedy collision avoidance)
# --------------------------------------------------------------------------
def _rect_overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ox = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    oy = max(0, min(ay + ah, by + bh) - max(ay, by))
    return ox * oy


def place_labels(anchors, W, H, badge_r=14, box_w=30, box_h=22, off=14):
    """anchors: iterable of (cx, cy, fid). Returns list of
    (fid, anchor_x, anchor_y, badge_x, badge_y)."""
    dirs = [(0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)]
    placed, out = [], []
    for cx, cy, fid in anchors:
        best, best_score = None, 1e18
        for dx, dy in dirs:
            bx = cx + dx * (badge_r + off)
            by = cy + dy * (badge_r + off)
            bx = min(max(bx, badge_r + 6), W - badge_r - 6)
            by = min(max(by, badge_r + 6), H - badge_r - 6)
            rect = (bx - box_w / 2, by - box_h / 2, box_w, box_h)
            score = sum(_rect_overlap(rect, pr) for pr in placed)
            if score < best_score:
                best_score, best = score, (bx, by)
        bx, by = best
        placed.append((bx - box_w / 2, by - box_h / 2, box_w, box_h))
        out.append((fid, cx, cy, bx, by))
    return out


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
def build_html(shape, singles, patterns, axis, silhouette, bbox, src_name):
    if axis == "X":
        u_min, u_max = bbox[1], bbox[4]
        v_min, v_max = bbox[2], bbox[5]
        u_lab, v_lab = "Y", "Z"
    elif axis == "Y":
        u_min, u_max = bbox[0], bbox[3]
        v_min, v_max = bbox[2], bbox[5]
        u_lab, v_lab = "X", "Z"
    else:
        u_min, u_max = bbox[0], bbox[3]
        v_min, v_max = bbox[1], bbox[4]
        u_lab, v_lab = "X", "Y"

    margin = 0.14 * max(u_max - u_min, v_max - v_min, 1e-6)
    u0, u1 = u_min - margin, u_max + margin
    v0, v1 = v_min - margin, v_max + margin
    span_u, span_v = u1 - u0, v1 - v0
    W, H = 900, 660
    scale = min(W / span_u, H / span_v)

    def tx(u):
        return (u - u0) * scale + (W - span_u * scale) / 2

    def ty(v):
        return (v1 - v) * scale + (H - span_v * scale) / 2

    # classify single features
    for c in singles:
        if c["axis"] is None:
            c["kind"] = "surface"
        elif c["stype"] == "torus":
            c["kind"] = "fillet"
        else:
            c["kind"] = _classify(shape, c["loc3"], c["axis"], c["extent"])

    svg = []
    svg.append(f'<svg id="loc" viewBox="0 0 {W} {H}" width="100%" '
               f'style="max-width:960px;background:#fff;border:1px solid #e2e2e2;'
               f'border-radius:8px" xmlns="http://www.w3.org/2000/svg">')
    svg.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="#fbfbfd"/>')

    if silhouette:
        for line in silhouette:
            d = "M " + " L ".join(f"{tx(u):.1f},{ty(v):.1f}" for u, v in line)
            svg.append(f'<path d="{d}" fill="none" stroke="#b9bcc9" '
                       f'stroke-width="1.2"/>')
    else:
        svg.append(f'<rect x="{tx(u0):.1f}" y="{ty(v1):.1f}" '
                   f'width="{span_u*scale:.1f}" height="{span_v*scale:.1f}" '
                   f'fill="none" stroke="#ddd" stroke-dasharray="4 4"/>')

    color_map = {}
    # ---- patterns (bolt-circle groups) ----
    pat_anchors = []
    for i, p in enumerate(patterns, 1):
        p["id"] = f"P{i}"
        cx, cy = tx(p["center"][0]), ty(p["center"][1])
        pr = p["pitch"] * scale
        # dashed pitch circle
        svg.append(f'<circle class="feat" data-id="{p["id"]}" cx="{cx:.1f}" '
                   f'cy="{cy:.1f}" r="{pr:.1f}" fill="none" '
                   f'stroke="#8a6d3b" stroke-width="1.3" '
                   f'stroke-dasharray="6 4" opacity="0.9"/>')
        # hole dots
        for hx, hy in p["holes"]:
            svg.append(f'<circle class="feat" data-id="{p["id"]}" cx="{tx(hx):.1f}" '
                       f'cy="{ty(hy):.1f}" r="{p["radius"]*scale:.1f}" '
                       f'fill="#e6394620" stroke="#e63946" stroke-width="1.6"/>')
        color_map[p["id"]] = "#8a6d3b"
        pat_anchors.append((cx, cy - pr, p["id"]))

    # ---- single features ----
    single_anchors = []
    for c in singles:
        # project the feature centroid into the end-view plane
        if axis == "X":
            u, v = c["loc3"][1], c["loc3"][2]
        elif axis == "Y":
            u, v = c["loc3"][0], c["loc3"][2]
        else:
            u, v = c["loc3"][0], c["loc3"][1]
        cx, cy = tx(u), ty(v)
        st = c["stype"]
        if st in ("cylinder", "cone", "sphere"):
            stroke = "#e63946" if c["kind"] == "hole" else "#2a7d3b"
        elif st == "torus":
            stroke = "#e08600"            # fillet / round
        else:
            stroke = "#8a8f98"            # plane / freeform
        end_on = (c["axis"] == axis)
        analytic = st in ("cylinder", "cone", "torus", "sphere")
        if analytic and c["radii"]:
            for r in c["radii"]:
                rr = r * scale
                svg.append(f'<circle class="feat" data-id="{c["id"]}" cx="{cx:.1f}" '
                           f'cy="{cy:.1f}" r="{rr:.1f}" fill="{stroke}14" '
                           f'stroke="{stroke}" stroke-width="2"/>')
            if not end_on:
                svg.append(f'<circle class="feat" data-id="{c["id"]}" cx="{cx:.1f}" '
                           f'cy="{cy:.1f}" r="9" fill="none" stroke="{stroke}" '
                           f'stroke-width="1" stroke-dasharray="3 3"/>')
        else:
            # plane / freeform: square marker (no clean circular projection)
            svg.append(f'<rect class="feat" data-id="{c["id"]}" x="{cx-5:.1f}" '
                       f'y="{cy-5:.1f}" width="10" height="10" fill="{stroke}55" '
                       f'stroke="{stroke}" stroke-width="1.5"/>')
        color_map[c["id"]] = stroke
        # only the primary holes/bosses get numbered badges + leader lines;
        # fillets/planes/freeform/threads stay as hoverable markers
        # (+ full table), otherwise 60+ badges would crowd the end-view.
        if c["stype"] == "cylinder":
            single_anchors.append((cx, cy, c["id"]))

    # ---- labels with leader lines ----
    labels = place_labels(pat_anchors + single_anchors, W, H)
    for fid, ax_, ay_, bx, by in labels:
        # leader line from anchor edge to badge edge
        dx, dy = bx - ax_, by - ay_
        L = math.hypot(dx, dy) or 1
        sx, sy = ax_ + dx / L * 4, ay_ + dy / L * 4
        ex, ey = bx - dx / L * 14, by - dy / L * 14
        btxt = ("#" + str(fid)) if isinstance(fid, int) else str(fid)
        svg.append(f'<line x1="{sx:.1f}" y1="{sy:.1f}" x2="{ex:.1f}" '
                   f'y2="{ey:.1f}" stroke="#444" stroke-width="1"/>')
        # badge
        fill = color_map.get(fid, "#e63946")
        svg.append(f'<circle class="badge" data-id="{fid}" cx="{bx:.1f}" '
                   f'cy="{by:.1f}" r="14" fill="{fill}" stroke="#fff" '
                   f'stroke-width="2" style="cursor:pointer"/>')
        svg.append(f'<text class="badge" data-id="{fid}" x="{bx:.1f}" '
                   f'y="{by+5:.1f}" font-size="15" font-weight="bold" '
                   f'text-anchor="middle" fill="#fff" style="cursor:pointer">'
                   f'{btxt}</text>')

    # scale bar (10 mm)
    bar_len = 10 * scale
    svg.append(f'<line x1="22" y1="{H-26}" x2="{22+bar_len:.1f}" y2="{H-26}" '
               f'stroke="#333" stroke-width="2"/>')
    svg.append(f'<text x="22" y="{H-32}" font-size="11" fill="#333">10 mm</text>')
    svg.append(f'<text x="{W-14}" y="22" font-size="12" fill="#888" '
               f'text-anchor="end">视图方向：沿 {axis} 轴（端视图）</text>')
    svg.append('</svg>')

    # ---- table ----
    def typestr(c):
        if c["stype"] == "torus":
            return "圆角"
        if c["stype"] == "plane":
            return "平面"
        if c["stype"] == "freeform":
            return "自由曲面"
        if c["stype"] == "cone":
            return "锥孔" if c["kind"] == "hole" else "锥台"
        if c["stype"] == "sphere":
            return "球凹" if c["kind"] == "hole" else "球凸"
        base = "孔" if c["kind"] == "hole" else "凸台"
        if c["composite"]:
            base = "复合沉孔" if c["kind"] == "hole" else "复合凸台"
        return base

    def uv_of(c):
        if axis == "X":
            return (c["loc3"][1], c["loc3"][2])
        if axis == "Y":
            return (c["loc3"][0], c["loc3"][2])
        return (c["loc3"][0], c["loc3"][1])

    rows = ""
    for p in patterns:
        rows += (f"<tr data-id=\"{p['id']}\"><td>{p['id']}</td>"
                 f"<td>螺栓孔组</td><td>{p['axis']}</td>"
                 f"<td>{p['radius']:.3f}</td><td>{p['count']} 个</td>"
                 f"<td>阵列分布（节圆 Ø{2*p['pitch']:.2f}）</td></tr>")
    for c in singles:
        cid = c["id"]
        idcell = ("#" + str(cid)) if isinstance(cid, int) else str(cid)
        if c["stype"] == "cylinder" and c["composite"]:
            dia = " / ".join(f"Ø{2*r:.2f}({idcell}.{k})"
                             for k, r in enumerate(c["radii"], 1))
        elif c["radii"]:
            dia = " / ".join(f"Ø{2*r:.2f}" for r in c["radii"])
        else:
            dia = "—"
        axc = c["axis"] if c["axis"] is not None else "—"
        uu, vv = uv_of(c)
        rows += (f"<tr data-id=\"{cid}\"><td>{idcell}</td>"
                 f"<td>{typestr(c)}</td><td>{axc}</td>"
                 f"<td>{dia}</td>"
                 f"<td>{c['extent']:.2f}</td>"
                 f"<td>({uu:.2f}, {vv:.2f})</td></tr>")

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{src_name} · 特征定位图</title>
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;background:#f4f5f7;
margin:0;padding:24px;color:#222}}
h2{{margin:0 0 4px}}.sub{{color:#777;font-size:13px;margin-bottom:16px}}
.wrap{{background:#fff;border:1px solid #e2e2e2;border-radius:10px;
padding:16px;max-width:980px}}
table{{border-collapse:collapse;width:100%;margin-top:18px;font-size:13px}}
th,td{{border:1px solid #e6e6e6;padding:6px 10px;text-align:center}}
th{{background:#f0f2f5}}tr:nth-child(even){{background:#fafbfc}}
tr.hl{{background:#fff3cd!important;outline:2px solid #f0ad4e}}
.badge{{transition:opacity .1s}}.feat{{transition:stroke-width .1s,opacity .1s}}
.feat.dim{{opacity:.25}}.badge.dim{{opacity:.25}}
.hint{{margin-top:14px;font-size:13px;color:#555;line-height:1.7}}
.b{{color:#e63946;font-weight:bold}}
.legend span{{display:inline-block;margin-right:16px;font-size:12px}}
.dot{{display:inline-block;width:11px;height:11px;border-radius:50%;
vertical-align:middle;margin-right:4px}}</style></head><body>
<h2>{src_name} · 特征定位图</h2>
<div class="sub">枚举全部曲面并分类聚合（孔/凸台·圆角·平面·自由曲面）· 投影轴 = {axis} · 纯 OCP 生成</div>
<div class="wrap">
{''.join(svg)}
<div class="legend" style="margin-top:10px">
<span><span class="dot" style="background:#e63946"></span>孔 / 沉孔</span>
<span><span class="dot" style="background:#2a7d3b"></span>凸台（实体柱）</span>
<span><span class="dot" style="background:#e08600"></span>圆角（环面）</span>
<span><span class="dot" style="background:#8a8f98"></span>平面 / 自由曲面</span>
<span><span class="dot" style="background:#8a6d3b"></span>螺栓孔组</span>
<span style="color:#888">悬停编号或表格行可联动高亮</span>
</div>
<table>
<tr><th>编号</th><th>类型</th><th>轴线</th><th>直径</th>
<th>轴向长度/数量</th><th>位置({u_lab},{v_lab}) / 说明</th></tr>
{rows}
</table>
<div class="hint">
用法：告诉我 <span class="b">"把 #{ (next((c['id'] for c in singles if c['kind']=='hole' and c['radius']<2.0), '?')) } 扩大 N 倍"</span>
即可精确指认，无需截图箭头。其它类型用前缀：<b>F</b>=圆角、<b>L</b>=平面、<b>S</b>=自由曲面（如"去掉 F3 圆角"）。<br>
图例：<b>同心圆叠加</b> = 复合沉孔（大圆沉孔、小圆底孔，已合并为同一编号，子圈见 #N.k）；
<b>橙色圆</b> = 圆角（环面，两端半径）；<b>灰色方块</b> = 平面/自由曲面（无直径参数，仅可点选定位）；
<b>虚线圆</b> = 螺栓孔阵列（整体作为一组 P#）；实线细圆 = 端视孔（圆=真实孔径）。
</div></div>
<script>
const svg=document.getElementById('loc');
function setHl(id,on){{
  svg.querySelectorAll('.feat[data-id="'+id+'"]').forEach(e=>{{
    if(on){{e.setAttribute('stroke-width','3.4');e.classList.remove('dim');}}
    else e.setAttribute('stroke-width', e.getAttribute('stroke-width'));
  }});
  svg.querySelectorAll('.badge[data-id="'+id+'"]').forEach(e=>e.classList.toggle('dim',!on));
  document.querySelectorAll('tr[data-id="'+id+'"]').forEach(r=>r.classList.toggle('hl',on));
}}
svg.querySelectorAll('.badge').forEach(b=>{{
  const id=b.getAttribute('data-id');
  b.addEventListener('mouseenter',()=>setHl(id,true));
  b.addEventListener('mouseleave',()=>setHl(id,false));
}});
document.querySelectorAll('tr[data-id]').forEach(r=>{{
  const id=r.getAttribute('data-id');
  r.addEventListener('mouseenter',()=>setHl(id,true));
  r.addEventListener('mouseleave',()=>setHl(id,false));
}});
</script>
</body></html>"""
    return html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--axis", default="auto")
    ap.add_argument("--out-dir", default=os.path.join(HERE, "previews"))
    args = ap.parse_args()

    shape = cad_core.read_shape(args.input)
    feats = collect_features(shape)
    comps = group_features(feats)
    singles, patterns = detect_patterns(comps)
    assign_ids(singles, patterns)
    axis = args.axis if args.axis in ("x", "y", "z") else choose_axis(singles + patterns)
    axis = axis.upper()
    bbox = _part_bbox(shape)
    silhouette = project_edges(shape, axis)
    if silhouette is None:
        print("[warn] HLR silhouette unavailable", file=sys.stderr)

    base = os.path.splitext(os.path.basename(args.input))[0]
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    html_path = os.path.join(out_dir, base + "_定位图.html")
    html = build_html(shape, singles, patterns, axis, silhouette, bbox,
                      os.path.basename(args.input))
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    feats_json = [{"id": c["id"], "type": c["stype"],
                   "composite": c["composite"], "axis": c["axis"],
                   "radii": [round(r, 4) for r in c["radii"]],
                   "extent": round(c["extent"], 3),
                   "location": [round(x, 3) for x in c["loc3"]]} for c in singles]
    feats_json += [{"id": p["id"], "type": "bolt_pattern",
                    "axis": p["axis"], "radius": round(p["radius"], 4),
                    "count": p["count"], "pitch": round(p["pitch"], 3),
                    "center": [round(x, 3) for x in p["center"]]}
                   for p in patterns]
    print(json.dumps({"axis": axis, "single_count": len(singles),
                      "pattern_count": len(patterns), "features": feats_json,
                      "html": html_path}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
