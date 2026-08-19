"""Phase A — cad_assembly production module tests (ADR-0002 D3/R2/R3/R7/R8).

Covers the Template+Matrix manifest produced from a real assembly STEP
(round-tripped through the XCAF writer, see _assembly_helpers) plus the
degenerate-input fallbacks (R2) and the cache layout the frontend consumes.
"""
from __future__ import annotations

import json
import os

import pytest

import cad_assembly
from _assembly_helpers import build_assembly_doc, write_assembly_step


@pytest.fixture(scope="module")
def assembly_step(tmp_path_factory) -> str:
    path = str(tmp_path_factory.mktemp("asm") / "pump_head.step")
    write_assembly_step(build_assembly_doc(), path)
    return path


@pytest.fixture(scope="module")
def manifest(assembly_step) -> dict:
    return cad_assembly.parse_assembly(assembly_step)


def _flatten(node, depth=0):
    yield (depth, node)
    for child in node["children"]:
        yield from _flatten(child, depth + 1)


# --------------------------------------------------------------------------
# Manifest structure (D3)
# --------------------------------------------------------------------------

def test_manifest_metadata_fields(manifest, assembly_step):
    assert manifest["schema_version"] == 1            # R7
    assert manifest["units"] == "mm"                  # R3
    assert manifest["source_file"] == "pump_head.step"
    assert len(manifest["source_sha256"]) == 64       # R8 cache key
    assert "_shapes" not in manifest or "_shapes" in manifest  # internal ok


def test_tree_structure_and_names(manifest):
    flat = [(d, n["name"], n["type"]) for d, n in _flatten(manifest["root"])]
    assert flat == [
        (0, "PumpHead", "assembly"),
        (1, "BasePlate", "part"),
        (1, "BearingComp", "assembly"),
        (2, "M4x8_Bolt", "part"),
        (2, "M4x8_Bolt", "part"),
    ]


def test_world_matrices_accumulated(manifest):
    flat = [n for _, n in _flatten(manifest["root"])]
    bolts = [n for n in flat if n["name"] == "M4x8_Bolt"]
    t = lambda n: [n["matrix"][i][3] for i in range(3)]  # noqa: E731
    assert t(bolts[0]) == [35.0, 5.0, 5.0]
    assert t(bolts[1]) == [45.0, 5.0, 5.0]


def test_templates_deduped(manifest):
    """2 unique part templates (not 3 instances); both bolts share one."""
    tpl = manifest["templates"]
    assert [t["name"] for t in tpl] == ["BasePlate", "M4x8_Bolt"]
    assert [t["id"] for t in tpl] == ["t0", "t1"]
    bolts = [n for _, n in _flatten(manifest["root"]) if n["name"] == "M4x8_Bolt"]
    assert bolts[0]["template"] == bolts[1]["template"] == "t1"
    colored = next(t for t in tpl if t["name"] == "M4x8_Bolt")
    assert colored["color"] == [0.2, 0.4, 0.9]
    assert next(t for t in tpl if t["name"] == "BasePlate")["color"] is None


def test_flat_step_becomes_single_part_tree(selftest_step):
    """R2 fallback: a single-solid STEP (no assembly) parses to one part."""
    m = cad_assembly.parse_assembly(selftest_step)
    root = m["root"]
    assert root["type"] == "part"
    assert root["children"] == []
    assert len(m["templates"]) == 1
    assert m["templates"][0]["id"] == "t0"
    assert root["template"] == "t0"
    # deterministic fallback name for unnamed flat parts
    assert root["name"] == "Part_1"
    assert m["templates"][0]["name"] == "Part_1"


def test_rejects_non_step_input(tmp_path):
    bad = tmp_path / "x.stl"
    bad.write_bytes(b"solid x")
    with pytest.raises(ValueError):
        cad_assembly.parse_assembly(str(bad))
    with pytest.raises(FileNotFoundError):
        cad_assembly.parse_assembly(str(tmp_path / "missing.step"))


# --------------------------------------------------------------------------
# Cache build (workspace/cache layout)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cache_dir(assembly_step, tmp_path_factory) -> str:
    out = str(tmp_path_factory.mktemp("cache"))
    cad_assembly.build_cache(assembly_step, out)
    return out


def test_cache_layout(cache_dir):
    assert os.path.isfile(os.path.join(cache_dir, "tree_structure.json"))
    for tid in ("t0", "t1"):
        g = os.path.join(cache_dir, "gltf_library", f"{tid}.gltf")
        b = os.path.join(cache_dir, "gltf_library", f"{tid}.bin")
        assert os.path.isfile(g) and os.path.getsize(g) > 0
        assert os.path.isfile(b) and os.path.getsize(b) > 0


def test_cache_manifest_gltf_paths(cache_dir):
    m = cad_assembly.load_cache(cache_dir)
    assert m["templates"][0]["gltf"] == "gltf_library/t0.gltf"
    assert m["templates"][1]["gltf"] == "gltf_library/t1.gltf"
    # glTF is valid JSON with one mesh and named root node
    g = json.load(open(os.path.join(cache_dir, m["templates"][1]["gltf"]),
                       encoding="utf-8"))
    assert len(g["meshes"]) >= 1
    assert any("M4x8_Bolt" in (nd.get("name") or "") for nd in g["nodes"])


def test_load_cache_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        cad_assembly.load_cache(str(tmp_path))


def test_flat_step_cache(selftest_step, tmp_out):
    m = cad_assembly.build_cache(selftest_step, tmp_out)
    assert m["root"]["type"] == "part"
    assert os.path.isfile(os.path.join(tmp_out, "gltf_library", "t0.gltf"))


# --------------------------------------------------------------------------
# MCP tool contract (docstring is the agent-facing contract)
# --------------------------------------------------------------------------

def test_mcp_parse_assembly_no_outdir(assembly_step, monkeypatch):
    import cad_mcp_server
    monkeypatch.setattr(cad_mcp_server, "ALLOWED_DIRS",
                        [os.path.dirname(assembly_step)])
    raw = cad_mcp_server.parse_assembly(assembly_step)
    m = json.loads(raw)
    assert m["schema_version"] == 1
    assert m["root"]["name"] == "PumpHead"
    assert [t["name"] for t in m["templates"]] == ["BasePlate", "M4x8_Bolt"]
    assert "gltf" not in m["templates"][0]   # no out_dir -> no file outputs
    assert "_shapes" not in m                # internal field never leaks to agents


def test_mcp_parse_assembly_writes_cache(assembly_step, tmp_out, monkeypatch):
    import cad_mcp_server
    monkeypatch.setattr(cad_mcp_server, "ALLOWED_DIRS",
                        [os.path.dirname(assembly_step), tmp_out])
    raw = cad_mcp_server.parse_assembly(assembly_step, out_dir=tmp_out)
    m = json.loads(raw)
    assert m["templates"][0]["gltf"] == "gltf_library/t0.gltf"
    assert os.path.isfile(os.path.join(tmp_out, "tree_structure.json"))
    assert os.path.isfile(os.path.join(tmp_out, "gltf_library", "t1.bin"))


def test_mcp_parse_assembly_path_escape_rejected(assembly_step):
    import cad_mcp_server
    with pytest.raises(ValueError):
        cad_mcp_server.parse_assembly(assembly_step, out_dir="../outside")
