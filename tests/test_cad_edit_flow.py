"""Phase C — version store, interference gate, edit flow (ADR-0002 D8/D10).

The interference-rejection scenario scales BasePlate x2 so it overlaps both
bolt instances (world boxes intersect with volume ~80 mm3 each); the accept
scenario drills a hole in the bolt template (removes material, never
interferes).
"""
from __future__ import annotations

import json
import os

import pytest
from starlette.testclient import TestClient

import cad_assembly
import cad_core
import cad_versions
from _assembly_helpers import build_assembly_doc, write_assembly_step

TOKEN = "test-token-123"


# --------------------------------------------------------------------------
# Fixtures: service app + parsed assembly + helpers
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def assembly_step(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("pc") / "pump_head.step"
    write_assembly_step(build_assembly_doc(), str(path))
    return str(path)


@pytest.fixture()
def client(tmp_path, assembly_step):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    dst = inputs / "pump_head.step"
    with open(assembly_step, "rb") as src, open(dst, "wb") as out:
        out.write(src.read())
    app = cad_service_app(workspace=str(tmp_path / "workspace"),
                          inputs=str(inputs))
    tc = TestClient(app)
    tc.step_path = str(dst)      # absolute, inside allowed dirs
    return tc


def cad_service_app(workspace, inputs):
    import cad_service
    return cad_service.create_app(token=TOKEN, allowed_dirs=[inputs],
                                  workspace=workspace)


def _hdr(tok=TOKEN):
    return {"Authorization": f"Bearer {tok}"}


def _parse(client, path=None):
    r = client.post("/api/assembly/parse",
                    json={"input_path": path or client.step_path},
                    headers=_hdr())
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------
# VersionStore unit behaviour
# --------------------------------------------------------------------------

def test_version_store_commit_and_chain(tmp_path):
    store = cad_versions.VersionStore(str(tmp_path), "k1")
    assert store.current == "v0"
    assert store.version_ids() == []

    f1 = tmp_path / "prep1"
    f1.mkdir()
    (f1 / "t1.step").write_bytes(b"a")
    (f1 / "t1.gltf").write_bytes(b"{}")
    rec = store.commit({"t1": {"step": str(f1 / "t1.step"),
                               "gltf": str(f1 / "t1.gltf")}}, "edit t1")
    assert rec["id"] == "v1" and store.current == "v1"

    # resolve chain: t1 -> v1 file; t0 -> baseline fallback
    assert store.resolve_step("t1", baseline_step="base/t0.step").endswith(
        os.path.join("v1", "t1.step"))
    assert store.resolve_step("t0", baseline_step="base/t0.step") == "base/t0.step"

    # second version edits t0 -> chain resolves t0 from v2, t1 still v1
    f2 = tmp_path / "prep2"
    f2.mkdir()
    (f2 / "t0.step").write_bytes(b"b")
    (f2 / "t0.gltf").write_bytes(b"{}")
    store.commit({"t0": {"step": str(f2 / "t0.step"),
                         "gltf": str(f2 / "t0.gltf")}}, "edit t0")
    assert store.current == "v2"
    assert store.resolve_step("t1").endswith(os.path.join("v1", "t1.step"))
    assert store.resolve_step("t0").endswith(os.path.join("v2", "t0.step"))
    # historical view: at v1, t0 is still baseline
    assert store.resolve_step("t0", version="v1",
                              baseline_step="base/t0.step") == "base/t0.step"


def test_version_store_rollback_pointer_only(tmp_path):
    store = cad_versions.VersionStore(str(tmp_path), "k1")
    f = tmp_path / "p"
    f.mkdir()
    (f / "t1.step").write_bytes(b"a")
    (f / "t1.gltf").write_bytes(b"{}")
    store.commit({"t1": {"step": str(f / "t1.step"), "gltf": str(f / "t1.gltf")}},
                 "edit")
    store.checkout("v0")
    assert store.current == "v0"
    assert store.resolve_step("t1", baseline_step="base/t1.step") == "base/t1.step"
    # files never deleted (D10)
    assert os.path.isfile(os.path.join(str(tmp_path), "k1", "v1", "t1.step"))
    with pytest.raises(KeyError):
        store.checkout("v9")


def test_version_store_cleanup_temp(tmp_path):
    store = cad_versions.VersionStore(str(tmp_path), "k1")
    stale = tmp_path / "k1" / ".tmp_v1_123"
    stale.mkdir()
    (stale / "x").write_bytes(b"1")
    assert store.cleanup_temp() == 1
    assert not stale.exists()


# --------------------------------------------------------------------------
# Interference gate (library level)
# --------------------------------------------------------------------------

def test_check_interference_detects_overlap():
    """Two overlapping boxes in one assembly -> exactly one hit pair."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.TopLoc import TopLoc_Location
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    app.NewDocument(TCollection_ExtendedString("XmlOcaf"), doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    a = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    b = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    la, lb = st.AddShape(a, False), st.AddShape(b, False)
    TDataStd_Name.Set_s(la, TCollection_ExtendedString("A"))
    TDataStd_Name.Set_s(lb, TCollection_ExtendedString("B"))
    root = st.NewShape()
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(5.0, 0.0, 0.0))     # 5mm overlap in x
    st.AddComponent(root, la, TopLoc_Location(gp_Trsf()))
    st.AddComponent(root, lb, TopLoc_Location(t))
    st.UpdateAssemblies()

    path = os.path.join(os.path.dirname(__file__), "_overlap_tmp.step")
    try:
        from OCP.STEPCAFControl import STEPCAFControl_Writer
        from OCP.STEPControl import STEPControl_StepModelType
        w = STEPCAFControl_Writer()
        w.SetNameMode(True)
        w.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs)
        w.Write(path)

        manifest = cad_assembly.parse_assembly(path)
        shapes = manifest.pop("_shapes")
        hits = cad_assembly.check_interference(manifest, shapes)
        assert len(hits) == 1
        assert {hits[0]["a"]["name"], hits[0]["b"]["name"]} == {"A", "B"}
        assert abs(hits[0]["volume_mm3"] - 500.0) < 1.0   # 10x10x5
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_check_interference_clean_assembly(assembly_step):
    manifest = cad_assembly.parse_assembly(assembly_step)
    shapes = manifest.pop("_shapes")
    assert cad_assembly.check_interference(manifest, shapes) == []


def test_apply_template_edit_dispatch(assembly_step):
    manifest = cad_assembly.parse_assembly(assembly_step)
    shapes = manifest.pop("_shapes")
    v0 = cad_core.properties(shapes["t1"])["volume"]
    drilled = cad_assembly.apply_template_edit(
        shapes["t1"], "drill",
        {"radius": 1.0, "depth": 4.0, "position": [0, 0, 0]})
    v1 = cad_core.properties(drilled)["volume"]
    assert v1 < v0      # material removed
    with pytest.raises(ValueError):
        cad_assembly.apply_template_edit(shapes["t1"], "drill", {"radius": 0})
    with pytest.raises(ValueError):
        cad_assembly.apply_template_edit(shapes["t1"], "bogus", {})


# --------------------------------------------------------------------------
# Service edit flow (③ 类流程): gate -> commit -> rollback
# --------------------------------------------------------------------------

def test_edit_drill_commits_v1(client):
    body = _parse(client)
    key = body["cache_key"]

    r = client.post("/api/assembly/edit", headers=_hdr(), json={
        "cache_key": key, "template_id": "t1", "operation": "drill",
        "params": {"radius": 1.0, "depth": 4.0, "position": [0, 0, 0],
                   "direction": [0, 0, 1]}})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] is True and res["version"] == "v1"
    assert "drill" in res["changelog"] and "M4x8_Bolt" in res["changelog"]
    # view manifest points the edited template at the version gltf
    t1 = next(t for t in res["manifest"]["templates"] if t["id"] == "t1")
    assert t1["gltf"].startswith("/versions/")
    assert "/v1/t1.gltf" in t1["gltf"]
    # baseline template untouched
    t0 = next(t for t in res["manifest"]["templates"] if t["id"] == "t0")
    assert t0["gltf"] == "gltf_library/t0.gltf"

    # version files exist and are servable
    gltf_url = t1["gltf"]
    r2 = client.get(gltf_url)
    assert r2.status_code == 200 and len(r2.content) > 0

    # second edit stacks v2 on the chain
    r3 = client.post("/api/assembly/edit", headers=_hdr(), json={
        "cache_key": key, "template_id": "t1", "operation": "chamfer",
        "params": {"distance": 0.2}})
    assert r3.status_code == 200, r3.text
    assert r3.json()["version"] == "v2"

    # versions list
    r4 = client.get(f"/api/versions?cache_key={key}", headers=_hdr())
    assert r4.status_code == 200
    lst = r4.json()
    assert lst["current"] == "v2" and [v["id"] for v in lst["versions"]] == ["v1", "v2"]

    # rollback to v1 (pointer only): t1 resolves to v1 file again
    r5 = client.post("/api/versions/checkout", headers=_hdr(),
                     json={"cache_key": key, "version": "v1"})
    assert r5.status_code == 200
    m = r5.json()["manifest"]
    t1 = next(t for t in m["templates"] if t["id"] == "t1")
    assert "/v1/t1.gltf" in t1["gltf"]
    # and back to baseline
    r6 = client.post("/api/versions/checkout", headers=_hdr(),
                     json={"cache_key": key, "version": "v0"})
    m6 = r6.json()["manifest"]
    t1 = next(t for t in m6["templates"] if t["id"] == "t1")
    assert t1["gltf"] == "gltf_library/t1.gltf"


def test_edit_interference_rejected_no_commit(client):
    body = _parse(client)
    key = body["cache_key"]

    # scale BasePlate x2.5 -> plate spans x[0,50] z[0,12.5], overlapping
    # BOTH bolt instances (x[33,37] and x[43,47], z[5,13])
    r = client.post("/api/assembly/edit", headers=_hdr(), json={
        "cache_key": key, "template_id": "t0", "operation": "scale",
        "params": {"factor": 2.5}})
    assert r.status_code == 409, r.text
    res = r.json()
    assert res["ok"] is False and res["error"] == "interference"
    assert res["stage"] == "interference_gate"
    assert len(res["interferences"]) == 2
    assert {hit["b"]["name"] for hit in res["interferences"]} == {"M4x8_Bolt"}
    assert all(hit["volume_mm3"] > 0 for hit in res["interferences"])

    # nothing committed: versions list still empty, current stays v0
    r2 = client.get(f"/api/versions?cache_key={key}", headers=_hdr())
    lst = r2.json()
    assert lst["versions"] == [] and lst["current"] == "v0"


def test_edit_rejects_bad_params_and_unknown_ids(client):
    body = _parse(client)
    key = body["cache_key"]
    r = client.post("/api/assembly/edit", headers=_hdr(), json={
        "cache_key": key, "template_id": "t1", "operation": "drill",
        "params": {"radius": -1, "depth": 5}})
    assert r.status_code == 400
    r2 = client.post("/api/assembly/edit", headers=_hdr(), json={
        "cache_key": key, "template_id": "t9", "operation": "drill",
        "params": {"radius": 1, "depth": 5}})
    assert r2.status_code == 400
    r3 = client.post("/api/assembly/edit", headers=_hdr(), json={
        "cache_key": "deadbeef", "template_id": "t1", "operation": "drill",
        "params": {"radius": 1, "depth": 5}})
    assert r3.status_code == 404


def test_stale_schema_cache_rebuilt(client, tmp_path):
    """R7: a v1-schema manifest on disk must not be served as cache hit."""
    body = _parse(client)
    key = body["cache_key"]
    manifest_path = os.path.join(str(tmp_path), "workspace", "cache", key,
                                 "tree_structure.json")
    assert os.path.isfile(manifest_path)
    m = json.load(open(manifest_path, encoding="utf-8"))
    m["schema_version"] = 1
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(m, f)

    r = client.post("/api/assembly/parse",
                    json={"input_path": client.step_path}, headers=_hdr())
    res = r.json()
    assert res["cache_hit"] is False          # rebuilt at current schema
    assert res["manifest"]["schema_version"] == cad_assembly.SCHEMA_VERSION
