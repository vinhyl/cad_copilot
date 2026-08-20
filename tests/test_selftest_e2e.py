"""TE2 — full-pipeline end-to-end regression (read -> properties -> convert).

Exercises the non-interactive toolchain against the committed samples:
  * read a STEP/IGES, compute properties (volume/area/topology)
  * convert between formats via write_shape
  * enumerate features via the picker library (collect_feature_solids)

These are the same steps selftest.py performs, now pinned as pytest cases so a
regression in read/properties/convert is caught automatically.
(The historical make_preview HTML/STL outlet was retired with the static
previews; the Web frontend renders meshes from the glTF cache instead.)
"""
from __future__ import annotations

import os

import cad_core


def test_read_and_properties(selftest_step):
    s = cad_core.read_shape(selftest_step)
    p = cad_core.properties(s)
    assert p["volume"] > 0
    assert p["surface_area"] > 0
    assert p["topology"]["faces"] > 0
    assert p["topology"]["edges"] > 0
    assert "bounding_box" in p


def test_e2e_iges_roundtrip_volume_preserved(selftest_iges, tmp_out):
    s1 = cad_core.read_shape(selftest_iges)
    v1 = cad_core.properties(s1)["volume"]
    out = os.path.join(tmp_out, "from_iges.step")
    cad_core.write_shape(s1, out)
    s2 = cad_core.read_shape(out)
    v2 = cad_core.properties(s2)["volume"]
    # allow a small tolerance for tessellation/translation round-trip
    assert abs(v1 - v2) <= max(1.0, abs(v1) * 0.05)


def test_e2e_convert_step_to_stl(selftest_step, tmp_out):
    s = cad_core.read_shape(selftest_step)
    out = os.path.join(tmp_out, "converted.stl")
    cad_core.write_shape(s, out)
    assert os.path.exists(out)
    assert os.path.getsize(out) > 0


def test_e2e_pick_features_full_chain(selftest_step):
    """The feature-enumeration library (same one the Web cache export uses)
    runs end-to-end on selftest.step and reports ids for every feature."""
    import feature_picker
    shape = cad_core.read_shape(selftest_step)
    feats = feature_picker.collect_feature_solids(shape)
    assert feats
    # every reported feature carries an id and a solid
    assert all("id" in f and "solid" in f for f in feats)
