"""Phase D -- cad_fea plugin framework tests (no FreeCAD required).

The OUTER framework is fully covered: probe & env override, spec
normalization, cache keying, generated-script validity, command
construction, orchestration, timeout mapping and graceful degradation
(D7). The INNER FreeCAD script is validated on first install (see the
cad_fea module NOTE); here we only assert it compiles and carries the
phase markers + embedded payload.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

import cad_fea


# --------------------------------------------------------------------------
# Probe (D7: env override -> PATH -> well-known paths)
# --------------------------------------------------------------------------

def test_probe_freecad_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "freecadcmd.exe"
    fake.write_bytes(b"stub")
    monkeypatch.setenv("CAD_FREECAD_EXE", str(fake))
    assert cad_fea.probe_freecad() == str(fake)


def test_probe_calculix_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "ccx.exe"
    fake.write_bytes(b"stub")
    monkeypatch.setenv("CAD_CCX_EXE", str(fake))
    assert cad_fea.probe_calculix() == str(fake)


def test_probe_calculix_finds_freecad_sibling(tmp_path, monkeypatch):
    """FreeCAD bundles ccx next to its binaries (sibling lookup)."""
    fc = tmp_path / "bin" / "freecadcmd.exe"
    fc.parent.mkdir()
    fc.write_bytes(b"stub")
    ccx = tmp_path / "bin" / "ccx.exe"
    ccx.write_bytes(b"stub")
    monkeypatch.delenv("CAD_CCX_EXE", raising=False)
    monkeypatch.setattr(cad_fea.shutil, "which", lambda name: None)
    assert cad_fea.probe_calculix(str(fc)) == str(ccx)


def _status(available, fc=None, ccx=None):
    missing = []
    if fc is None:
        missing.append("FreeCAD")
    if ccx is None:
        missing.append("CalculiX (ccx)")
    return {"freecad": fc, "calculix": ccx, "available": available,
            "missing": missing, "hint": None if available else cad_fea.HINT}


def test_fea_status_reports_missing(monkeypatch):
    monkeypatch.setattr(cad_fea, "probe_freecad", lambda: None)
    monkeypatch.setattr(cad_fea, "probe_calculix", lambda fc=None: None)
    st = cad_fea.fea_status()
    assert st["available"] is False
    assert st["missing"] == ["FreeCAD", "CalculiX (ccx)"]
    assert "CAD_FREECAD_EXE" in st["hint"]


def test_fea_status_available(tmp_path, monkeypatch):
    fc, ccx = tmp_path / "freecadcmd.exe", tmp_path / "ccx.exe"
    fc.write_bytes(b"")
    ccx.write_bytes(b"")
    monkeypatch.setattr(cad_fea, "probe_freecad", lambda: str(fc))
    monkeypatch.setattr(cad_fea, "probe_calculix", lambda fc=None: str(ccx))
    st = cad_fea.fea_status()
    assert st["available"] is True and st["missing"] == [] and st["hint"] is None


# --------------------------------------------------------------------------
# Spec normalization (boundary validation only)
# --------------------------------------------------------------------------

def test_normalize_spec_defaults_and_clamping():
    s = cad_fea.normalize_spec(None)
    assert s["axis"] == "z"
    assert s["force_N"] == 1000.0
    assert s["timeout_s"] == 600.0
    assert cad_fea.normalize_spec({"timeout_s": 5})["timeout_s"] == 30.0
    assert cad_fea.normalize_spec({"timeout_s": 99999})["timeout_s"] == 3600.0


@pytest.mark.parametrize("bad", [
    {"axis": "w"},
    {"force_N": -1},
    {"young_modulus_MPa": 0},
    {"poisson_ratio": 0.7},
    {"density_kg_m3": -1},
    {"max_element_size_mm": 0},
])
def test_normalize_spec_rejects_bad(bad):
    with pytest.raises(ValueError):
        cad_fea.normalize_spec(bad)


# --------------------------------------------------------------------------
# Cache key (R8: content-keyed)
# --------------------------------------------------------------------------

def test_cache_key_depends_on_content_and_spec(tmp_path):
    a = tmp_path / "a.step"
    a.write_bytes(b"AAA")
    k1 = cad_fea.fea_cache_key(str(a), None)
    assert k1 == cad_fea.fea_cache_key(str(a), None)
    assert k1 != cad_fea.fea_cache_key(str(a), {"force_N": 2000})
    a.write_bytes(b"BBB")
    assert cad_fea.fea_cache_key(str(a), None) != k1
    assert len(k1) == 16


# --------------------------------------------------------------------------
# Generated script + command construction
# --------------------------------------------------------------------------

def test_build_freecad_script_compiles_and_carries_payload():
    spec = cad_fea.normalize_spec({"force_N": 42.0})
    script = cad_fea.build_freecad_script(
        "/tmp/x.step", "/tmp/out", spec, "/usr/bin/ccx")
    compile(script, "fea_script.py", "exec")   # valid Python
    assert "import FreeCAD as App" in script
    # spec/paths embedded (double dumps -> escaped quotes in the literal)
    assert '\\"force_N\\": 42.0' in script
    assert '\\"/tmp/x.step\\"' in script
    assert '\\"/usr/bin/ccx\\"' in script
    for ph in ("interpreter", "geometry", "faces", "setup", "mesh",
               "solve", "post"):
        assert f'phase("{ph}"' in script         # progress markers


def test_build_freecad_script_survives_odd_paths():
    """Paths with quotes must not break the generated literal (double
    json.dumps keeps it a valid Python string literal)."""
    weird = os.path.join("o'dd", "x.step")
    script = cad_fea.build_freecad_script(
        weird, "out", cad_fea.normalize_spec(None), None)
    compile(script, "fea_script.py", "exec")
    assert "o'dd" in script


def test_build_command_console_vs_gui():
    assert cad_fea.build_command(r"C:\f\bin\FreeCADcmd.exe", "s.py") == \
        [r"C:\f\bin\FreeCADcmd.exe", "s.py"]
    assert cad_fea.build_command(r"C:\f\bin\FreeCAD.exe", "s.py") == \
        [r"C:\f\bin\FreeCAD.exe", "--console", "s.py"]   # D6 headless
    assert cad_fea.build_command("/usr/bin/freecadcmd", "s.py") == \
        ["/usr/bin/freecadcmd", "s.py"]


# --------------------------------------------------------------------------
# Orchestration: degradation / subprocess / cache / timeout
# --------------------------------------------------------------------------

def test_run_static_degrades_without_deps(tmp_path, monkeypatch, selftest_step):
    monkeypatch.setattr(cad_fea, "fea_status",
                        lambda: _status(False))
    with pytest.raises(cad_fea.FEAError) as ei:
        cad_fea.run_static(selftest_step, str(tmp_path / "fea"))
    assert ei.value.kind == "missing"
    assert ei.value.missing == ["FreeCAD", "CalculiX (ccx)"]


def test_run_static_missing_step(tmp_path, monkeypatch):
    monkeypatch.setattr(cad_fea, "fea_status", lambda: _status(True, "fc", "ccx"))
    with pytest.raises(FileNotFoundError):
        cad_fea.run_static(str(tmp_path / "nope.step"), str(tmp_path / "fea"))


def _available(monkeypatch):
    monkeypatch.setattr(cad_fea, "fea_status",
                        lambda: _status(True, "fake-fc", "fake-ccx"))


def test_run_static_orchestrates_subprocess(tmp_path, monkeypatch, selftest_step):
    _available(monkeypatch)
    calls = []
    payload = {"schema_version": 1, "ok": True,
               "phases": {"solve": {"ok": True}},
               "max_displacement_mm": 0.01, "max_von_mises_MPa": 12.5}

    def fake_invoke(exe, script_path, timeout_s):
        calls.append((exe, script_path, timeout_s))
        # the generated script must exist and be valid Python
        with open(script_path, encoding="utf-8") as f:
            compile(f.read(), "fea_script.py", "exec")
        with open(os.path.join(os.path.dirname(script_path), "result.json"),
                  "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(cad_fea, "_invoke", fake_invoke)
    result = cad_fea.run_static(selftest_step, str(tmp_path / "fea"),
                                {"force_N": 500})
    assert result["cache_hit"] is False
    assert result["ok"] is True
    assert result["max_von_mises_MPa"] == 12.5
    assert calls and calls[0][0] == "fake-fc"
    assert calls[0][2] == cad_fea.normalize_spec({"force_N": 500})["timeout_s"]


def test_run_static_cache_hit_skips_subprocess(tmp_path, monkeypatch,
                                               selftest_step):
    _available(monkeypatch)
    out = tmp_path / "fea"
    out.mkdir()
    (out / "result.json").write_text(
        json.dumps({"ok": True, "max_displacement_mm": 0.5}), encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("subprocess must not run on cache hit")

    monkeypatch.setattr(cad_fea, "_invoke", boom)
    result = cad_fea.run_static(selftest_step, str(out))
    assert result["cache_hit"] is True
    assert result["max_displacement_mm"] == 0.5


def test_run_static_force_recomputes(tmp_path, monkeypatch, selftest_step):
    _available(monkeypatch)
    out = tmp_path / "fea"
    out.mkdir()
    (out / "result.json").write_text(
        json.dumps({"ok": True, "stale": True}), encoding="utf-8")

    def fake_invoke(exe, script_path, timeout_s):
        with open(os.path.join(os.path.dirname(script_path), "result.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"ok": True, "stale": False}, f)
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(cad_fea, "_invoke", fake_invoke)
    result = cad_fea.run_static(selftest_step, str(out), force=True)
    assert result["cache_hit"] is False and result["stale"] is False


def test_run_static_timeout_kind(tmp_path, monkeypatch, selftest_step):
    _available(monkeypatch)

    def timeout(exe, script_path, timeout_s):
        raise subprocess.TimeoutExpired(cmd="freecadcmd", timeout=timeout_s)

    monkeypatch.setattr(cad_fea, "_invoke", timeout)
    with pytest.raises(cad_fea.FEAError) as ei:
        cad_fea.run_static(selftest_step, str(tmp_path / "fea"))
    assert ei.value.kind == "timeout"


def test_run_static_failure_without_result(tmp_path, monkeypatch, selftest_step):
    _available(monkeypatch)
    monkeypatch.setattr(cad_fea, "_invoke",
                        lambda *a: subprocess.CompletedProcess([], 1))
    with pytest.raises(cad_fea.FEAError) as ei:
        cad_fea.run_static(selftest_step, str(tmp_path / "fea"))
    assert ei.value.kind == "failure"
