"""Phase A — cad_service (starlette) contract tests.

Covers the security contract (token auth, path confinement, traversal-proof
static serving), the sha-keyed idempotent cache (R8/R17), and the WebSocket
protocol skeleton (D2).
"""
from __future__ import annotations

import json
import os

import pytest
from starlette.testclient import TestClient

import cad_service
from _assembly_helpers import build_assembly_doc, write_assembly_step

TOKEN = "test-token-123"


@pytest.fixture(scope="module")
def assembly_step(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("svc") / "pump_head.step"
    write_assembly_step(build_assembly_doc(), str(path))
    return str(path)


@pytest.fixture()
def client(tmp_path) -> TestClient:
    """App with: fixed token, inputs confined to a scratch dir, isolated
    workspace (per-test so cache-hit tests don't interfere)."""
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    app = cad_service.create_app(
        token=TOKEN,
        allowed_dirs=[str(inputs)],
        workspace=str(tmp_path / "workspace"))
    return TestClient(app)


def _move_step(assembly_step, inputs_dir) -> str:
    """Copy the module-scoped STEP into this test's allowed inputs dir."""
    dst = os.path.join(inputs_dir, os.path.basename(assembly_step))
    with open(assembly_step, "rb") as src, open(dst, "wb") as out:
        out.write(src.read())
    return dst


def _hdr(tok=TOKEN):
    return {"Authorization": f"Bearer {tok}"}


# --------------------------------------------------------------------------
# Health & auth
# --------------------------------------------------------------------------

def test_health_no_auth_needed(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_parse_requires_token(client, assembly_step, tmp_path):
    r = client.post("/api/assembly/parse",
                    json={"input_path": assembly_step})
    assert r.status_code == 401


def test_parse_rejects_wrong_token(client, assembly_step):
    r = client.post("/api/assembly/parse",
                    json={"input_path": assembly_step},
                    headers=_hdr("wrong"))
    assert r.status_code == 401


# --------------------------------------------------------------------------
# Parse + sha-keyed idempotent cache (R8/R17)
# --------------------------------------------------------------------------

def _parse_ok(client, inputs_dir, assembly_step):
    src = _move_step(assembly_step, inputs_dir)
    r = client.post("/api/assembly/parse",
                    json={"input_path": src}, headers=_hdr())
    assert r.status_code == 200, r.text
    return src, r.json()


def test_parse_builds_cache_and_returns_manifest(client, assembly_step, tmp_path):
    inputs = str(tmp_path / "inputs")
    src, body = _parse_ok(client, inputs, assembly_step)
    assert body["cache_hit"] is False
    assert body["cache_key"] and len(body["cache_key"]) == 16
    assert body["base_url"] == f"/cache/{body['cache_key']}"
    m = body["manifest"]
    assert m["root"]["name"] == "PumpHead"
    assert m["source_sha256"].startswith(body["cache_key"])


def test_parse_same_content_hits_cache(client, assembly_step, tmp_path):
    inputs = str(tmp_path / "inputs")
    src, first = _parse_ok(client, inputs, assembly_step)
    r = client.post("/api/assembly/parse",
                    json={"input_path": src}, headers=_hdr())
    second = r.json()
    assert second["cache_hit"] is True
    assert second["cache_key"] == first["cache_key"]
    assert second["manifest"] == first["manifest"]


def test_parse_force_rebuild(client, assembly_step, tmp_path):
    inputs = str(tmp_path / "inputs")
    src, _ = _parse_ok(client, inputs, assembly_step)
    r = client.post("/api/assembly/parse",
                    json={"input_path": src, "force": True}, headers=_hdr())
    assert r.json()["cache_hit"] is False


# --------------------------------------------------------------------------
# Path safety
# --------------------------------------------------------------------------

def test_parse_input_outside_allowed_dirs_rejected(client, assembly_step):
    r = client.post("/api/assembly/parse",
                    json={"input_path": assembly_step}, headers=_hdr())
    assert r.status_code == 403


def test_parse_missing_field_rejected(client):
    r = client.post("/api/assembly/parse", json={}, headers=_hdr())
    assert r.status_code == 400


def test_static_cache_serves_gltf(client, assembly_step, tmp_path):
    inputs = str(tmp_path / "inputs")
    _, body = _parse_ok(client, inputs, assembly_step)
    url = f"{body['base_url']}/gltf_library/t0.gltf"
    r = client.get(url)
    assert r.status_code == 200
    g = json.loads(r.content)
    assert len(g["meshes"]) >= 1


def test_static_cache_traversal_rejected(client):
    r = client.get("/cache/../../etc/passwd")
    assert r.status_code in (403, 404)


def test_static_cache_missing_file_404(client, assembly_step, tmp_path):
    inputs = str(tmp_path / "inputs")
    _, body = _parse_ok(client, inputs, assembly_step)
    r = client.get(f"{body['base_url']}/gltf_library/nope.gltf")
    assert r.status_code == 404


# --------------------------------------------------------------------------
# WebSocket protocol skeleton (D2)
# --------------------------------------------------------------------------

def test_ws_rejects_bad_token(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?token=wrong") as ws:
            ws.receive_json()


def test_ws_ping(client):
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_json({"action": "ping"})
        assert ws.receive_json() == {"ok": True, "action": "ping"}


def test_ws_parse(client, assembly_step, tmp_path):
    inputs = str(tmp_path / "inputs")
    src = _move_step(assembly_step, inputs)
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_json({"action": "parse", "input_path": src})
        resp = ws.receive_json()
    assert resp["ok"] is True
    assert resp["manifest"]["root"]["name"] == "PumpHead"


def test_ws_unknown_action(client):
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        ws.send_json({"action": "bogus"})
        resp = ws.receive_json()
    assert resp["ok"] is False
