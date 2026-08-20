#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feature enumeration library (serves cad_service feature picking).

Enumerates a shape's CAD features — holes, bosses, fillets, bolt patterns —
as (metadata, solid) pairs. Consumed by cad_assembly's per-template feature
export (features/<tid>.json + .gltf), which drives the Web viewport's
click-to-highlight feature picking.

Builds on feature_locator's enumeration/grouping. The historical static-HTML
preview outlet (vendored three.js + offline picker page) was retired when the
Web frontend took over interactive picking.

PUBLIC API:
    collect_feature_solids(shape) -> list of feature dicts (see docstring)
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cad_core  # noqa: E402
import feature_locator as fl  # noqa: E402

from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.TopoDS import TopoDS_Compound  # noqa: E402


def _compound_of(faces):
    comp = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(comp)
    for f in faces:
        b.Add(comp, f)
    return comp


def _group_faces_by_canonical_radius(c):
    """Split a composite feature's faces into rings keyed by the canonical
    radii already computed by feature detection (c['radii']), instead of
    re-deriving per-face cylinder radii (which mis-fits some faces, e.g. a
    large sleeve). Each face is assigned to the nearest canonical radius by
    its vertex-based mid-radius. Guarantees displayed ring radii == c['radii']."""
    radii = sorted(c.radii, reverse=True)
    groups = {r: [] for r in radii}
    A = c.axis
    for f in c.faces:
        pts = fl.vertices_of(f)
        rad, _ = fl.radial_of(pts, A)
        rmid = (min(rad) + max(rad)) / 2.0
        best = min(radii, key=lambda cr: abs(cr - rmid))
        groups[best].append(f)
    # keep only non-empty rings (a canonical radius with no nearby face is
    # not a real, pickable ring and would yield an empty STL compound)
    return [(r, groups[r]) for r in radii if groups[r]]


def collect_feature_solids(shape):
    """Enumerate a shape's features as (metadata, solid) pairs — PUBLIC API.

    Same classification/grouping as the retired preview outlet, but returns
    the per-feature TopoDS_Compound solid so other modules (e.g.
    cad_assembly's features glTF export) can consume feature geometry
    without going through base64 STL (H7: no cross-module private calls).

    Returns a list of dicts: {id, gid, ring, type, composite, axis, radii,
    extent, location, center, color, label, count?, pitch?, solid}.
    """
    feats_all = fl.collect_features(shape)
    comps = fl.group_features(feats_all)
    singles, patterns = fl.detect_patterns(comps)
    fl.assign_ids(singles, patterns)

    out = []
    for c in singles:
        if c.axis is None:
            kind = "surface"
        elif c.stype == "torus":
            kind = "fillet"
        else:
            kind = fl.classify(shape, c.loc3, c.axis, c.extent)
        color = fl.feature_color(c.stype, kind)
        label = fl.feature_label(c.stype, kind, c.composite)
        dtype = c.stype if kind == "surface" else kind
        base_fid = f"#{c.id}" if isinstance(c.id, int) else str(c.id)
        if c.composite and c.stype in ("cylinder", "cone", "sphere", "torus"):
            for k, (r, rfaces) in enumerate(_group_faces_by_canonical_radius(c), 1):
                ring_loc = cad_core.centroid_of_faces(rfaces) or c.loc3
                out.append({"id": f"{base_fid}.{k}", "gid": c.id, "ring": k,
                            "type": dtype, "composite": True, "axis": c.axis,
                            "radii": [round(r, 4)],
                            "extent": round(c.extent, 3),
                            "location": [round(x, 3) for x in ring_loc],
                            "center": [round(x, 3) for x in ring_loc],
                            "color": color, "label": label,
                            "solid": _compound_of(rfaces)})
        else:
            out.append({"id": base_fid, "gid": c.id, "ring": 0,
                        "type": dtype, "composite": c.composite,
                        "axis": c.axis,
                        "radii": [round(r, 4) for r in c.radii],
                        "extent": round(c.extent, 3),
                        "location": [round(x, 3) for x in c.loc3],
                        "center": [round(x, 3) for x in c.loc3],
                        "color": color, "label": label,
                        "solid": _compound_of(c.faces)})
    for p in patterns:
        out.append({"id": p["id"], "gid": p["id"], "ring": 0,
                    "type": "bolt_pattern", "axis": p["axis"],
                    "radii": [round(p["radius"], 4)],
                    "extent": round(p["extent"], 3),
                    "center": [round(x, 3) for x in p["center"]],
                    "count": p["count"], "pitch": round(p["pitch"], 3),
                    "color": "#8a6d3b", "label": "螺栓孔组",
                    "solid": _compound_of(p["faces"])})
    return out
