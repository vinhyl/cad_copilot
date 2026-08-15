"""TE8 — numeric / geometric boundary tests.

Exercises cad_core at extreme and degenerate inputs. The contract is "does not
crash (no segfault) and either returns a sane result or raises a CLEAR error"
-- never silently corrupts or hangs.
"""
from __future__ import annotations

import math
import os

import pytest

import cad_core


def test_tiny_box_properties_finite():
    # 0.01 mm box is well below typical part scale but still valid for OCP
    # (sub-~1e-3 mm collapses to zero volume due to OCCT precision).
    s = cad_core.box(0.01, 0.01, 0.01)
    p = cad_core.properties(s)
    assert p["volume"] > 0
    assert math.isfinite(p["volume"])
    assert math.isfinite(p["surface_area"])


def test_huge_box_properties_finite():
    s = cad_core.box(1e5, 1e5, 1e5)
    p = cad_core.properties(s)
    assert p["volume"] > 0
    assert math.isfinite(p["volume"])


def test_degenerate_box_does_not_crash():
    # zero-thickness box is a face, not a solid; must not segfault. Either a
    # clear error (e.g. OCCT Standard_DomainError) or a finite volume is OK.
    try:
        s = cad_core.box(0.0, 1.0, 1.0)
        p = cad_core.properties(s)
        assert math.isfinite(p["volume"])
    except Exception:
        # any clear, raised error (not a crash) satisfies the contract
        pass


def test_fillet_zero_radius_does_not_crash():
    s = cad_core.box(10, 10, 10)
    try:
        out = cad_core.fillet(s, 0.0)
        assert out is not None
    except (ValueError, RuntimeError):
        # a clear "fillet failed" is also acceptable
        pass


def test_chamfer_zero_distance_does_not_crash():
    s = cad_core.box(10, 10, 10)
    try:
        out = cad_core.chamfer(s, 0.0)
        assert out is not None
    except (ValueError, RuntimeError):
        pass


def test_boolean_invalid_op_raises():
    a = cad_core.box(10, 10, 10)
    b = cad_core.cylinder(2, 10)
    with pytest.raises(ValueError):
        cad_core.boolean(a, b, "nonsense")


def test_read_unsupported_extension_raises(tmp_path):
    # An EXISTING file with an unsupported extension must raise ValueError.
    # (A missing file raises FileNotFoundError earlier, which is correct too.)
    bad = os.path.join(str(tmp_path), "fake.xyz")
    with open(bad, "w") as f:
        f.write("not a cad file")
    with pytest.raises(ValueError):
        cad_core.read_shape(bad)


def test_read_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        cad_core.read_shape("definitely_missing.step")


def test_write_shape_refuses_overwrite(tmp_path):
    s = cad_core.box(5, 5, 5)
    out = os.path.join(str(tmp_path), "once.stl")
    cad_core.write_shape(s, out)
    with pytest.raises(FileExistsError):
        cad_core.write_shape(s, out)
