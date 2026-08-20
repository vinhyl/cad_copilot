"""TE5 — feature_locator classification / aggregation golden assertions.

Against the committed ``selftest_*.step`` samples we assert the STABLE
grouping counts produced by collect_features -> group_features ->
detect_patterns -> assign_ids. Because the sample files and the locator logic
are fixed, a change in grouping (e.g. a merge threshold tweak, or a new
surface type) flips these counts and is caught here.

Golden counts below were measured against the committed samples:
  selftest.step       : 1 cylinder, 7 planes        (8 singles, 0 patterns)
  selftest_drill.step  : 1 cylinder, 7 planes        (8 singles, 0 patterns)
  selftest_fuse.step   : 1 cylinder, 7 planes        (8 singles, 0 patterns)
  selftest_chamfer.step: 26 planes                   (26 singles, 0 patterns)
  selftest_scale.step  : 6 planes                    (6 singles, 0 patterns)
  selftest_fillet.step : torus (round) features enumerated (no longer crashes)
"""
from __future__ import annotations

import pytest

import cad_core
import feature_locator as fl


def _locate(path):
    shape = cad_core.read_shape(path)
    feats = fl.collect_features(shape)
    comps = fl.group_features(feats)
    singles, patterns = fl.detect_patterns(comps)
    fl.assign_ids(singles, patterns)
    return singles, patterns


def _by_stype(singles):
    out = {}
    for c in singles:
        out.setdefault(c.stype, 0)
        out[c.stype] += 1
    return out


def test_all_features_have_ids(selftest_step):
    singles, _ = _locate(selftest_step)
    assert singles
    for c in singles:
        assert c.id is not None


def test_selftest_step_golden(selftest_step):
    singles, patterns = _locate(selftest_step)
    st = _by_stype(singles)
    assert patterns == []
    assert st.get("cylinder") == 1
    assert st.get("plane") == 7


def test_selftest_drill_golden(selftest_drill):
    singles, patterns = _locate(selftest_drill)
    st = _by_stype(singles)
    assert patterns == []
    # the drilled through-hole is exactly one cylindrical feature
    assert st.get("cylinder") == 1
    cyl = [c for c in singles if c.stype == "cylinder"]
    assert len(cyl) == 1


def test_selftest_fuse_golden(selftest_fuse):
    singles, patterns = _locate(selftest_fuse)
    st = _by_stype(singles)
    assert patterns == []
    # the fused primitive contributes exactly one cylindrical feature
    assert st.get("cylinder") == 1


def test_selftest_chamfer_golden(selftest_chamfer):
    singles, patterns = _locate(selftest_chamfer)
    st = _by_stype(singles)
    assert patterns == []
    # chamfering all box edges yields many planar (bevel) faces
    assert st.get("plane") == 26


def test_selftest_scale_golden(selftest_scale):
    singles, patterns = _locate(selftest_scale)
    st = _by_stype(singles)
    assert patterns == []
    # a plain scaled box is just its 6 planar faces
    assert st.get("plane") == 6


def test_feature_picker_collect_runs(selftest_step):
    """feature_picker.collect_feature_solids sits on top of the locator;
    ensure it produces a non-empty feature list with ids + solids."""
    import feature_picker
    shape = cad_core.read_shape(selftest_step)
    feats = feature_picker.collect_feature_solids(shape)
    assert feats
    assert all("id" in f and "solid" in f for f in feats)


def test_selftest_fillet_locator(selftest_fillet):
    # Sphere faces no longer crash (gp_Sphere().Position().Axis()). With the
    # R1 analytic-axis center fix, vertex-blend spheres enumerate AND group
    # coaxially with their cylinder (one composite feature) instead of
    # surfacing as a separate sphere entry.
    import cad_core
    import feature_locator as fl
    feats = fl.collect_features(cad_core.read_shape(selftest_fillet))
    assert any(c["stype"] == "sphere" for c in feats)   # enumerated, no crash
    singles, _ = _locate(selftest_fillet)
    assert singles
