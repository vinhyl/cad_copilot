"""TE2 — full-pipeline end-to-end regression (read -> properties -> preview).

Exercises the whole non-interactive toolchain against the committed samples:
  * read a STEP/IGES, compute properties (volume/area/topology)
  * convert between formats via write_shape
  * generate a self-contained preview (make_preview -> STL + HTML)

These are the same steps selftest.py performs, now pinned as pytest cases so a
regression in read/properties/mesh/preview is caught automatically.
"""
from __future__ import annotations

import os

import cad_core
import make_preview


def test_read_and_properties(selftest_step):
    s = cad_core.read_shape(selftest_step)
    p = cad_core.properties(s)
    assert p["volume"] > 0
    assert p["surface_area"] > 0
    assert p["topology"]["faces"] > 0
    assert p["topology"]["edges"] > 0
    assert "bounding_box" in p


def test_e2e_preview_writes_html_and_stl(selftest_step, tmp_out):
    res = make_preview.make_preview(selftest_step, out_dir=tmp_out)
    assert os.path.exists(res["html"])
    assert os.path.exists(res["stl"])
    assert os.path.getsize(res["html"]) > 0
    assert os.path.getsize(res["stl"]) > 0
    # the HTML references the embedded base64 STL and escaped file name
    html_text = open(res["html"], encoding="utf-8").read()
    assert "__B64__" not in html_text          # placeholder must be replaced
    assert "STLLoader" in html_text


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


def test_e2e_pick_features_full_chain(selftest_step, tmp_out):
    """Bonus: the interactive picker also runs end-to-end on selftest.step.

    Skipped (not failed) if the offline three.js vendor fails verification, so
    a vendoring hiccup does not masquerade as a logic regression.
    """
    import pytest
    import feature_picker
    try:
        res = feature_picker.make_picker(selftest_step, out_dir=tmp_out)
    except RuntimeError as e:
        if "vendor" in str(e).lower():
            pytest.skip("offline three.js vendor verification unavailable")
        raise
    assert os.path.exists(res["html"])
    assert res["feature_count"] >= 0
    # every reported feature carries an id
    assert all("id" in f for f in res["features"])
