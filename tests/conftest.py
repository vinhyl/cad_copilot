"""Shared pytest fixtures for the cad_tools test-suite (Phase 3 / TE1).

Provides:
  * repository-root resolution + sys.path injection so the top-level modules
    (cad_core, feature_locator, feature_picker, make_preview, cad_mcp_server)
    are importable from inside ``tests/``.
  * path fixtures pointing at the committed ``selftest*.step`` / ``.iges``
    sample files under the repository root.
  * a throwaway output directory fixture (``tmp_out``) so tests never write
    into the source tree.

NOTE on path safety: cad_mcp_server computes ALLOWED_DIRS from the
CAD_MCP_ALLOWED_DIRS env var at import time. We pin it to the repo root here
so the MCP-tool tests can resolve the sample files; tests that need to write
to a scratch dir monkeypatch ALLOWED_DIRS locally (see test_cad_mcp_server).
"""
from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Make sibling helper modules (e.g. ``_compare_helpers``) importable from any
# test module regardless of how pytest inserts sys.path (single-file vs
# directory invocation, or pytest-version differences across CI runners).
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

# Pin the MCP server's allowed directory to the repo root so sample files
# resolve, and so the default "." behaviour does not depend on the cwd.
os.environ["CAD_MCP_ALLOWED_DIRS"] = REPO_ROOT

# Sample files committed at the repository root.
SAMPLES = {
    "step": os.path.join(REPO_ROOT, "selftest.step"),
    "iges": os.path.join(REPO_ROOT, "selftest.iges"),
    "chamfer": os.path.join(REPO_ROOT, "selftest_chamfer.step"),
    "drill": os.path.join(REPO_ROOT, "selftest_drill.step"),
    "fillet": os.path.join(REPO_ROOT, "selftest_fillet.step"),
    "fuse": os.path.join(REPO_ROOT, "selftest_fuse.step"),
    "scale": os.path.join(REPO_ROOT, "selftest_scale.step"),
}


@pytest.fixture
def repo_root() -> str:
    return REPO_ROOT


@pytest.fixture
def sample_paths() -> dict:
    return dict(SAMPLES)


@pytest.fixture
def selftest_step() -> str:
    return SAMPLES["step"]


@pytest.fixture
def selftest_iges() -> str:
    return SAMPLES["iges"]


@pytest.fixture
def selftest_chamfer() -> str:
    return SAMPLES["chamfer"]


@pytest.fixture
def selftest_drill() -> str:
    return SAMPLES["drill"]


@pytest.fixture
def selftest_fillet() -> str:
    return SAMPLES["fillet"]


@pytest.fixture
def selftest_fuse() -> str:
    return SAMPLES["fuse"]


@pytest.fixture
def selftest_scale() -> str:
    return SAMPLES["scale"]


@pytest.fixture(params=["step", "iges"])
def selftest_input(request) -> str:
    """Parametrize a test over both the STEP and IGES samples."""
    return SAMPLES[request.param]


@pytest.fixture
def tmp_out(tmp_path) -> str:
    """A fresh, writable output directory for each test."""
    d = tmp_path / "out"
    d.mkdir()
    return str(d)
