"""TE4 — MCP server contract tests (path safety, build123d kill-switch,
parameter validation, batch_convert overwrite protection).

All tools are exercised by calling the registered functions directly (the
@mcp.tool() decorator returns the original function in FastMCP, so the tool
bodies run normally). Covers the Phase-0 security contract:
  * _safe_path white-listing (legal relative path resolved; escape rejected)
  * build123d_model disabled-by-default kill-switch
  * edit_geometry illegal-parameter rejection
  * batch_convert never clobbering the source file
"""
from __future__ import annotations

import json
import os
import shutil

import pytest

import cad_mcp_server


# --------------------------------------------------------------------------
# 1) _safe_path white-listing
# --------------------------------------------------------------------------
def test_safe_path_legal_relative_returns_realpath():
    rp = cad_mcp_server._safe_path("selftest.step")
    assert os.path.isabs(rp)
    assert rp.endswith("selftest.step")
    # realpath lives inside the allowed repo root
    assert rp == os.path.realpath("selftest.step")


def test_safe_path_escape_rejected():
    # anything that climbs out of the allowed dir must be rejected
    with pytest.raises(ValueError):
        cad_mcp_server._safe_path("../etc/passwd")
    with pytest.raises(ValueError):
        cad_mcp_server._safe_path("../../secret.txt")


def test_safe_path_restricted_dir_rejects_escape(monkeypatch, tmp_path):
    # Overriding ALLOWED_DIRS to a scratch dir: a repo file is now outside it.
    monkeypatch.setattr(cad_mcp_server, "ALLOWED_DIRS", [str(tmp_path)])
    with pytest.raises(ValueError):
        cad_mcp_server._safe_path("selftest.step")


# --------------------------------------------------------------------------
# 2) build123d_model kill-switch
# --------------------------------------------------------------------------
def test_build123d_disabled_by_default(tmp_path):
    # Without CAD_MCP_ALLOW_BUILD123D=1 the tool must NOT execute anything and
    # must return a clear disabled message (no file is created).
    assert os.environ.get("CAD_MCP_ALLOW_BUILD123D") != "1"
    out_path = os.path.join(str(tmp_path), "should_not_be_created.step")
    result = cad_mcp_server.build123d_model("result = 1", out_path)
    assert "disabled" in result.lower()
    assert not os.path.exists(out_path)


# --------------------------------------------------------------------------
# 3) edit_geometry illegal-parameter rejection
# --------------------------------------------------------------------------
def test_edit_geometry_fillet_zero_radius_rejected():
    with pytest.raises(ValueError):
        cad_mcp_server.edit_geometry(
            "selftest.step", "out.step", "fillet", radius=0.0)


def test_edit_geometry_chamfer_zero_distance_rejected():
    with pytest.raises(ValueError):
        cad_mcp_server.edit_geometry(
            "selftest.step", "out.step", "chamfer", distance=0.0)


def test_edit_geometry_scale_zero_factor_rejected():
    with pytest.raises(ValueError):
        cad_mcp_server.edit_geometry(
            "selftest.step", "out.step", "scale", factor=0.0)


def test_edit_geometry_drill_bad_params_rejected():
    with pytest.raises(ValueError):
        cad_mcp_server.edit_geometry(
            "selftest.step", "out.step", "drill", radius=-1.0)
    with pytest.raises(ValueError):
        cad_mcp_server.edit_geometry(
            "selftest.step", "out.step", "drill", radius=3.0, depth=0.0)


def test_edit_geometry_unknown_op_rejected():
    with pytest.raises(ValueError):
        cad_mcp_server.edit_geometry(
            "selftest.step", "out.step", "frobnicate", radius=1.0)


# --------------------------------------------------------------------------
# 4) batch_convert must not overwrite the source file
# --------------------------------------------------------------------------
def test_batch_convert_skips_source_when_in_out_same_dir(tmp_path, sample_paths):
    # Put a sample in the scratch dir and ask batch_convert to write the same
    # dir with the SAME extension -> the source must be SKIPPED, not clobbered.
    monkeypatch_dirs = [str(tmp_path)]
    import cad_mcp_server as srv
    orig = srv.ALLOWED_DIRS
    srv.ALLOWED_DIRS = monkeypatch_dirs  # allow the scratch dir
    try:
        src = os.path.join(str(tmp_path), "sample.step")
        shutil.copy(sample_paths["step"], src)
        res = json.loads(
            cad_mcp_server.batch_convert(str(tmp_path), str(tmp_path), ".step"))
        assert res["skipped"] >= 1
        assert any("sample.step" in f for f in res["skipped_files"])
        # the source is intact (size unchanged, still present)
        assert os.path.exists(src)
        assert os.path.getsize(src) > 0
    finally:
        srv.ALLOWED_DIRS = orig


def test_batch_convert_writes_report(tmp_path, sample_paths):
    monkeypatch_dirs = [str(tmp_path)]
    import cad_mcp_server as srv
    orig = srv.ALLOWED_DIRS
    srv.ALLOWED_DIRS = monkeypatch_dirs
    try:
        src = os.path.join(str(tmp_path), "sample.step")
        shutil.copy(sample_paths["step"], src)
        # output to a DIFFERENT subdir with a different extension -> actually
        # converts (proves the happy path writes a report).
        out_dir = os.path.join(str(tmp_path), "converted")
        res = json.loads(
            cad_mcp_server.batch_convert(str(tmp_path), out_dir, ".iges"))
        assert os.path.exists(res["report"])
        assert res["scanned"] >= 1
    finally:
        srv.ALLOWED_DIRS = orig
