"""TE6 — slice / mesh geometry assertions.

Confirms that meshing a shape yields a non-empty STL with a positive triangle
count, via both the public write_shape path and cad_core.mesh_shape (the
shared mesh helper that feature_picker now delegates to).
Reuses the extracted comparison helper (tests._compare_helpers) for the
triangle count.
"""
from __future__ import annotations

import os
import struct
import sys

# Make sibling helper modules importable regardless of how pytest assembles
# sys.path (import mode, pytest-cov active, or runner/version differences).
# Conftest injects the same dir, but we do it here too so the import never
# depends on conftest load order or coverage-plugin sys.path timing.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cad_core
from _compare_helpers import count_binary_stl_triangles


def test_write_shape_produces_nonempty_stl(selftest_step, tmp_out):
    s = cad_core.read_shape(selftest_step)
    out = os.path.join(tmp_out, "mesh.stl")
    cad_core.write_shape(s, out)
    assert os.path.exists(out)
    size = os.path.getsize(out)
    assert size > 0
    # binary STL: 80-byte header + uint32 triangle count + 50 bytes/triangle
    assert size >= 84
    n = count_binary_stl_triangles(out)
    assert n > 0


def test_mesh_bytes_nonempty_with_triangles(selftest_step):
    s = cad_core.read_shape(selftest_step)
    b = cad_core.mesh_shape(s, 0.5)
    assert len(b) > 0
    n = struct.unpack("<I", b[80:84])[0]
    assert n > 0


def test_mesh_triangle_count_scales_with_deflection(selftest_step):
    """A finer deflection must produce at least as many triangles as a coarse
    one (monotonic-ish: smaller deflection -> denser mesh)."""
    s = cad_core.read_shape(selftest_step)
    coarse = cad_core.mesh_shape(s, 1.0)
    fine = cad_core.mesh_shape(s, 0.05)
    nc = struct.unpack("<I", coarse[80:84])[0]
    nf = struct.unpack("<I", fine[80:84])[0]
    assert nf >= nc > 0


def test_stl_from_drilled_hole(selftest_drill, tmp_out):
    s = cad_core.read_shape(selftest_drill)
    out = os.path.join(tmp_out, "drill.stl")
    cad_core.write_shape(s, out)
    n = count_binary_stl_triangles(out)
    assert n > 0
