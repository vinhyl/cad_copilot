"""Phase A — XCAF assembly round-trip & glTF export golden tests.

Pins the results validated by the 2026-08-19 spikes (_spike_xcaf.py /
_spike_gltf.py, scratch) as permanent regression cases, per ADR-0002:

  * D3 / R2: assembly tree, part names, colors, accumulated world 4x4
    matrices, instance dedup via ReferredShape (Template + Matrix).
  * D3: glTF node hierarchy with local translations, and the mesh-dedup
    post-process (RWGltf emits one mesh per instance).

The assembly STEP is generated on the fly into a tmp dir (no binary fixture
committed to the repo); see ``_assembly_helpers`` for the structure.
"""
from __future__ import annotations

import json
import os

import pytest

from _assembly_helpers import (
    build_assembly_doc,
    export_gltf,
    gltf_dedup,
    read_assembly_tree,
    write_assembly_step,
)


@pytest.fixture(scope="module")
def assembly_step(tmp_path_factory) -> str:
    """Write the generated assembly STEP once for this module."""
    path = str(tmp_path_factory.mktemp("asm") / "pump_head.step")
    doc = build_assembly_doc()
    write_assembly_step(doc, path)
    return path


@pytest.fixture(scope="module")
def tree_rows(assembly_step) -> list:
    return read_assembly_tree(assembly_step)


# --------------------------------------------------------------------------
# STEP round-trip (XCAF)
# --------------------------------------------------------------------------

def test_roundtrip_tree_structure(tree_rows):
    """PumpHead -> [BasePlate, BearingComp -> [Bolt, Bolt]] with depths."""
    structure = [(r["depth"], r["name"]) for r in tree_rows]
    assert structure == [
        (0, "PumpHead"),
        (1, "BasePlate"),
        (1, "BearingComp"),
        (2, "M4x8_Bolt"),
        (2, "M4x8_Bolt"),
    ]


def test_roundtrip_assembly_flags(tree_rows):
    by_name = {r["name"]: r for r in tree_rows}
    assert by_name["PumpHead"]["is_assembly"] is True
    assert by_name["BearingComp"]["is_assembly"] is True
    assert by_name["BasePlate"]["is_assembly"] is False
    bolts = [r for r in tree_rows if r["name"] == "M4x8_Bolt"]
    assert all(b["is_assembly"] is False and b["is_reference"] is True
               for b in bolts)


def test_roundtrip_world_matrices(tree_rows):
    """World matrix = parent translation + local instance translation."""
    bolts = [r for r in tree_rows if r["name"] == "M4x8_Bolt"]
    t = lambda r: [r["matrix"][i][3] for i in range(3)]  # noqa: E731
    assert t(bolts[0]) == [35.0, 5.0, 5.0]   # 30 (BearingComp) + 5
    assert t(bolts[1]) == [45.0, 5.0, 5.0]   # 30 (BearingComp) + 15
    sub = next(r for r in tree_rows if r["name"] == "BearingComp")
    assert t(sub) == [30.0, 0.0, 0.0]
    # rotation part must be identity for pure translations
    ident = [[1.0 if r == c else 0.0 for c in range(3)] for r in range(3)]
    for r in tree_rows:
        assert [row[:3] for row in r["matrix"]] == ident


def test_roundtrip_instance_dedup(tree_rows):
    """Both bolt instances refer to the SAME template (Template + Matrix)."""
    bolts = [r for r in tree_rows if r["name"] == "M4x8_Bolt"]
    assert len(bolts) == 2
    assert bolts[0]["referred"] is not None
    assert bolts[0]["referred"] == bolts[1]["referred"]


def test_roundtrip_color(tree_rows):
    bolts = [r for r in tree_rows if r["name"] == "M4x8_Bolt"]
    assert all(b["color"] == (0.2, 0.4, 0.9) for b in bolts)
    plate = next(r for r in tree_rows if r["name"] == "BasePlate")
    assert plate["color"] is None


# --------------------------------------------------------------------------
# glTF export (D3)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gltf_path(tmp_path_factory) -> str:
    path = str(tmp_path_factory.mktemp("gltf") / "pump_head.gltf")
    doc = build_assembly_doc()
    export_gltf(doc, path)
    return path


def _load(gltf_path):
    with open(gltf_path, encoding="utf-8") as f:
        return json.load(f)


def test_gltf_node_hierarchy(gltf_path):
    g = _load(gltf_path)
    nodes = g["nodes"]
    by_name = {nd["name"]: nd for nd in nodes if "name" in nd}
    assert set(by_name) >= {"PumpHead", "BasePlate", "BearingComp", "M4x8_Bolt"}

    root = by_name["PumpHead"]
    child_names = {nodes[i]["name"] for i in root.get("children", [])}
    assert child_names == {"BasePlate", "BearingComp"}

    sub = by_name["BearingComp"]
    assert sub["translation"] == [30.0, 0.0, 0.0]
    bolt_idx = {nodes[i]["name"] for i in sub.get("children", [])}
    assert bolt_idx == {"M4x8_Bolt"}


def test_gltf_local_translations(gltf_path):
    """glTF uses nested local transforms: world = parent chain product."""
    g = _load(gltf_path)
    bolts = [nd for nd in g["nodes"] if nd.get("name") == "M4x8_Bolt"]
    translations = sorted(nd["translation"] for nd in bolts)
    assert translations == [[5.0, 5.0, 5.0], [15.0, 5.0, 5.0]]


def test_gltf_dedup_same_template_mesh(gltf_path):
    """After dedup, both bolt instances share ONE mesh (RWGltf duplicates)."""
    before = _load(gltf_path)
    bolts = [nd for nd in before["nodes"] if nd.get("name") == "M4x8_Bolt"]
    # writer emits one mesh per instance (documented OCP behaviour)
    assert len({nd["mesh"] for nd in bolts}) == 2

    stats = gltf_dedup(gltf_path)
    assert stats["after"] < stats["before"]

    after = _load(gltf_path)
    bolts = [nd for nd in after["nodes"] if nd.get("name") == "M4x8_Bolt"]
    assert len({nd["mesh"] for nd in bolts}) == 1
    # unique part templates only: BasePlate + M4x8_Bolt
    assert len(after["meshes"]) == 2


def test_gltf_bin_exists(gltf_path):
    bin_file = gltf_path.replace(".gltf", ".bin")
    assert os.path.exists(bin_file) and os.path.getsize(bin_file) > 0
