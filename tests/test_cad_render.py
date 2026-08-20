"""Phase D -- cad_render plugin framework tests (no Blender required).

Same strategy as test_cad_fea: the outer framework (probe, spec, entries
building, script generation, command, orchestration, cache, degradation)
is fully covered; the inner bpy script only needs to compile and carry the
right structure (validated on first Blender install).
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

import cad_render


# --------------------------------------------------------------------------
# Probe (D7: env override -> PATH -> well-known paths)
# --------------------------------------------------------------------------

def test_probe_blender_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "blender.exe"
    fake.write_bytes(b"stub")
    monkeypatch.setenv("CAD_BLENDER_EXE", str(fake))
    assert cad_render.probe_blender() == str(fake)


def _status(available, blender=None):
    return {"blender": blender, "available": available,
            "missing": [] if available else ["Blender"],
            "hint": None if available else cad_render.HINT}


def test_render_status_reports_missing(monkeypatch):
    monkeypatch.setattr(cad_render, "probe_blender", lambda: None)
    st = cad_render.render_status()
    assert st["available"] is False
    assert st["missing"] == ["Blender"]
    assert "CAD_BLENDER_EXE" in st["hint"]


def test_render_status_available(tmp_path, monkeypatch):
    fake = tmp_path / "blender.exe"
    fake.write_bytes(b"")
    monkeypatch.setattr(cad_render, "probe_blender", lambda: str(fake))
    st = cad_render.render_status()
    assert st["available"] is True and st["missing"] == []


# --------------------------------------------------------------------------
# Spec normalization
# --------------------------------------------------------------------------

def test_normalize_spec_defaults_and_clamping():
    s = cad_render.normalize_spec(None)
    assert s["engine"] == "cycles"
    assert s["samples"] == 64
    assert s["width"] == 1600 and s["height"] == 1200
    assert cad_render.normalize_spec({"timeout_s": 5})["timeout_s"] == 30.0
    assert cad_render.normalize_spec({"timeout_s": 99999})["timeout_s"] == 3600.0


@pytest.mark.parametrize("bad", [
    {"engine": "lux"},
    {"samples": 4},
    {"samples": 99999},
    {"width": 10},
    {"height": 10000},
    {"distance_factor": 0.1},
])
def test_normalize_spec_rejects_bad(bad):
    with pytest.raises(ValueError):
        cad_render.normalize_spec(bad)


# --------------------------------------------------------------------------
# Render entries (manifest -> template gltf + world matrices)
# --------------------------------------------------------------------------

def _manifest():
    return {
        "root": {
            "id": "n0", "name": "asm", "type": "assembly",
            "children": [
                {"id": "n1", "type": "part", "template": "t0",
                 "matrix": [[1, 0, 0, 10], [0, 1, 0, 20], [0, 0, 1, 30]],
                 "children": []},
                {"id": "n2", "type": "part", "template": "t1",
                 "matrix": None, "children": []},   # -> identity fallback
                {"id": "n3", "type": "assembly", "children": [
                    {"id": "n4", "type": "part", "template": "t0",
                     "matrix": [[1, 0, 0, -5], [0, 1, 0, 0], [0, 0, 1, 0]],
                     "children": []}]},
            ],
        },
        "templates": [{"id": "t0", "name": "Plate", "color": [0.5, 0.5, 0.5]},
                      {"id": "t1", "name": "Bolt", "color": [0.2, 0.2, 0.2]}],
    }


def test_build_render_entries_groups_instances(tmp_path):
    for tid in ("t0", "t1"):
        (tmp_path / f"{tid}.gltf").write_text("{}", encoding="utf-8")
    entries = cad_render.build_render_entries(
        _manifest(), lambda tid: str(tmp_path / f"{tid}.gltf"))
    assert len(entries) == 2
    t0, t1 = entries
    assert t0["name"] == "Plate" and t0["gltf"].endswith("t0.gltf")
    # t0 is instanced twice (n1 + nested n4); matrices preserved verbatim
    assert t0["instances"] == [
        [[1, 0, 0, 10], [0, 1, 0, 20], [0, 0, 1, 30]],
        [[1, 0, 0, -5], [0, 1, 0, 0], [0, 0, 1, 0]],
    ]
    # node without matrix falls back to identity
    assert t1["instances"] == [cad_render._IDENTITY_3X4]


def test_build_render_entries_missing_gltf_raises(tmp_path):
    with pytest.raises(cad_render.RenderError) as ei:
        cad_render.build_render_entries(_manifest(), lambda tid: None)
    assert "t0" in str(ei.value)


def test_build_render_entries_empty_manifest_raises():
    manifest = {"root": {"id": "n0", "type": "assembly", "children": []},
                "templates": []}
    with pytest.raises(cad_render.RenderError):
        cad_render.build_render_entries(manifest, lambda tid: None)


# --------------------------------------------------------------------------
# Cache key (R8: content-keyed)
# --------------------------------------------------------------------------

def test_render_cache_key_depends_on_content_matrices_spec(tmp_path):
    g = tmp_path / "t0.gltf"
    g.write_text("{}", encoding="utf-8")
    entries = [{"gltf": str(g), "name": "T",
                "instances": [cad_render._IDENTITY_3X4]}]
    k1 = cad_render.render_cache_key(entries, None)
    assert k1 == cad_render.render_cache_key(entries, None)
    assert k1 != cad_render.render_cache_key(entries, {"samples": 32})
    assert k1 != cad_render.render_cache_key(
        [{"gltf": str(g), "name": "T",
          "instances": [[[2, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]]]}], None)
    g.write_text('{"changed": true}', encoding="utf-8")
    assert cad_render.render_cache_key(entries, None) != k1
    assert len(k1) == 16


# --------------------------------------------------------------------------
# Generated script + command construction
# --------------------------------------------------------------------------

def test_build_blender_script_compiles_and_carries_payload():
    payload = {"entries": [{"gltf": "/tmp/t0.gltf", "name": "Plate",
                            "instances": [cad_render._IDENTITY_3X4]}],
               "spec": cad_render.normalize_spec({"samples": 32}),
               "png": "/tmp/out/render.png",
               "result": "/tmp/out/result.json"}
    script = cad_render.build_blender_script(payload)
    compile(script, "render_script.py", "exec")   # valid Python
    assert "import bpy" in script
    assert "bpy.ops.import_scene.gltf" in script
    # spec/paths embedded (double dumps -> escaped quotes in the literal)
    assert '\\"samples\\": 32' in script
    assert '\\"/tmp/out/render.png\\"' in script
    assert "correct_gltf_up_axis" in script         # CAD Z-up correction
    assert "CYCLES" in script
    # never imports our modules (R9: external dependency only)
    assert "import cad_core" not in script
    assert "import cad_render" not in script


def test_build_command_headless():
    assert cad_render.build_command("/usr/bin/blender", "s.py") == \
        ["/usr/bin/blender", "--background", "--python", "s.py"]


# --------------------------------------------------------------------------
# Orchestration: degradation / subprocess / cache / timeout
# --------------------------------------------------------------------------

def test_render_scene_degrades_without_blender(tmp_path, monkeypatch):
    monkeypatch.setattr(cad_render, "render_status", lambda: _status(False))
    entries = [{"gltf": "x.gltf", "name": "T", "instances": []}]
    with pytest.raises(cad_render.RenderError) as ei:
        cad_render.render_scene(entries, str(tmp_path / "r"))
    assert ei.value.kind == "missing"
    assert ei.value.missing == ["Blender"]


def _available(monkeypatch):
    monkeypatch.setattr(cad_render, "render_status",
                        lambda: _status(True, "fake-blender"))


def test_render_scene_orchestrates_subprocess(tmp_path, monkeypatch):
    _available(monkeypatch)
    entries = [{"gltf": "x.gltf", "name": "T",
                "instances": [cad_render._IDENTITY_3X4]}]
    calls = []

    def fake_invoke(exe, script_path, timeout_s, **kwargs):
        calls.append((exe, script_path, timeout_s))
        with open(script_path, encoding="utf-8") as f:
            compile(f.read(), "render_script.py", "exec")
        out_dir = os.path.dirname(script_path)
        with open(os.path.join(out_dir, "render.png"), "wb") as f:
            f.write(b"\x89PNG stub")
        with open(os.path.join(out_dir, "result.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"schema_version": 1, "ok": True, "engine": "CYCLES",
                       "objects": 1, "png": os.path.join(out_dir, "render.png"),
                       "duration_s": 1.2}, f)
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(cad_render, "_invoke", fake_invoke)
    result = cad_render.render_scene(entries, str(tmp_path / "r"))
    assert result["cache_hit"] is False
    assert result["ok"] is True and result["engine"] == "CYCLES"
    assert calls and calls[0][0] == "fake-blender"


def test_render_scene_cache_hit_skips_subprocess(tmp_path, monkeypatch):
    _available(monkeypatch)
    out = tmp_path / "r"
    out.mkdir()
    (out / "render.png").write_bytes(b"\x89PNG")
    (out / "result.json").write_text(
        json.dumps({"ok": True, "engine": "CYCLES"}), encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("subprocess must not run on cache hit")

    monkeypatch.setattr(cad_render, "_invoke", boom)
    result = cad_render.render_scene([{"gltf": "x", "name": "T",
                                       "instances": []}], str(out))
    assert result["cache_hit"] is True


def test_render_scene_timeout_kind(tmp_path, monkeypatch):
    _available(monkeypatch)

    def timeout(exe, script_path, timeout_s, **kwargs):
        raise subprocess.TimeoutExpired(cmd="blender", timeout=timeout_s)

    monkeypatch.setattr(cad_render, "_invoke", timeout)
    with pytest.raises(cad_render.RenderError) as ei:
        cad_render.render_scene([{"gltf": "x", "name": "T",
                                  "instances": []}], str(tmp_path / "r"))
    assert ei.value.kind == "timeout"


def test_render_scene_failure_when_png_missing(tmp_path, monkeypatch):
    _available(monkeypatch)

    def fake_invoke(exe, script_path, timeout_s, **kwargs):
        out_dir = os.path.dirname(script_path)
        with open(os.path.join(out_dir, "result.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"ok": True, "engine": "CYCLES"}, f)  # png never written
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(cad_render, "_invoke", fake_invoke)
    with pytest.raises(cad_render.RenderError) as ei:
        cad_render.render_scene([{"gltf": "x", "name": "T",
                                  "instances": []}], str(tmp_path / "r"))
    assert ei.value.kind == "failure"


def test_render_scene_failure_without_result(tmp_path, monkeypatch):
    _available(monkeypatch)
    monkeypatch.setattr(cad_render, "_invoke",
                        lambda *a, **k: subprocess.CompletedProcess([], 1))
    with pytest.raises(cad_render.RenderError) as ei:
        cad_render.render_scene([{"gltf": "x", "name": "T",
                                  "instances": []}], str(tmp_path / "r"))
    assert ei.value.kind == "failure"


# --------------------------------------------------------------------------
# R5: real-subprocess cancel (Popen polling loop)
# --------------------------------------------------------------------------

def test_invoke_cancel_terminates_real_subprocess(tmp_path, monkeypatch):
    """The polling loop must terminate a live Blender subprocess on cancel."""
    import sys
    import time as _time
    monkeypatch.setattr(cad_render, "render_status",
                        lambda: _status(True, sys.executable))
    # python.exe can't take blender flags: plain [exe, script]
    monkeypatch.setattr(cad_render, "build_command",
                        lambda exe, script: [exe, script])
    monkeypatch.setattr(cad_render, "build_blender_script",
                        lambda payload: "import time\ntime.sleep(30)\n")
    entries = [{"gltf": "x", "name": "T", "instances": []}]
    t0 = _time.monotonic()
    with pytest.raises(cad_render.RenderError) as ei:
        cad_render.render_scene(
            entries, str(tmp_path / "r"),
            should_cancel=lambda: _time.monotonic() - t0 > 0.5)
    assert ei.value.kind == "cancelled"
    assert _time.monotonic() - t0 < 15   # terminated, not waited out
