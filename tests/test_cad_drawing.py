"""Phase D — drawing import (DXF native / DWG via ODA) & semantic
calibration tests (ADR-0002 D5 / 模块六).

DXF fixtures are generated with ezdxf itself (threads/diameters/tolerances
callouts + geometry); DWG path is covered by the ODA-missing degradation
case (no converter installed in CI).
"""
from __future__ import annotations

import os

import ezdxf
import pytest

import cad_drawing


@pytest.fixture(scope="module")
def drawing_dxf(tmp_path_factory) -> str:
    """A tiny drawing with callouts: M10x1.5 thread, Ø8 hole, H7/g6 fit."""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0))
    msp.add_line((0, 0), (0, 60))
    msp.add_circle((50, 30), radius=8)
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 60), (0, 60)], close=True)
    msp.add_text("M10x1.5", dxfattribs={"height": 3.0}).set_placement((10, 50))
    msp.add_text("Ø8 H7/g6", dxfattribs={"height": 3.0}).set_placement((60, 40))
    msp.add_text("总装图", dxfattribs={"height": 5.0}).set_placement((40, 65))
    path = tmp_path_factory.mktemp("dxf") / "pump_head.dxf"
    doc.saveas(str(path))
    return str(path)


def test_import_dxf_semantics_and_svg(drawing_dxf, tmp_path):
    out = str(tmp_path / "dwg_cache")
    res = cad_drawing.import_drawing(drawing_dxf, out)

    assert res["schema_version"] == 1
    assert res["source_file"] == "pump_head.dxf"
    assert len(res["source_sha256"]) == 64
    assert res["oda_used"] is False

    kinds = {(s["kind"], str(s["value"])) for s in res["semantics"]}
    assert ("thread", "M10x1.5") in kinds
    assert ("diameter", "8.0") in kinds
    assert ("tolerance", "H7/g6") in kinds
    assert any(k == "note" for k, _ in kinds)          # 总装图 -> note

    # SVG written and contains the geometry
    svg = open(os.path.join(out, "view.svg"), encoding="utf-8").read()
    assert svg.startswith("<svg")
    assert "<circle" in svg and "<line" in svg
    assert "M10x1.5" in svg                            # text rendered
    # Y-flip: sheet top edge (y=60) renders as negative in the flipped view
    assert "-60.000" in svg                                  # polyline path
    assert 'y="-65.000"' in svg                              # 总装图 text at y=65

    # drawing.json persisted
    import json
    j = json.load(open(os.path.join(out, "drawing.json"), encoding="utf-8"))
    assert j["semantics"] == res["semantics"]


def test_import_idempotent_cache(drawing_dxf, tmp_path):
    out = str(tmp_path / "dwg_cache")
    r1 = cad_drawing.import_drawing(drawing_dxf, out)
    r2 = cad_drawing.import_drawing(drawing_dxf, out)
    assert r1 == r2                                    # same source content


def test_import_rejects_bad_input(tmp_path):
    with pytest.raises(FileNotFoundError):
        cad_drawing.import_drawing(str(tmp_path / "nope.dxf"), str(tmp_path))
    other = tmp_path / "x.step"
    other.write_bytes(b"x")
    with pytest.raises(ValueError):
        cad_drawing.import_drawing(str(other), str(tmp_path))


def test_dwg_without_oda_degrades_clearly(tmp_path):
    """D5: DWG input with no ODA converter installed -> actionable error."""
    dwg = tmp_path / "fake.dwg"
    dwg.write_bytes(b"not a real dwg")
    if cad_drawing.probe_oda_converter() is None:
        with pytest.raises(cad_drawing.DrawingError, match="ODA"):
            cad_drawing.import_drawing(str(dwg), str(tmp_path / "c"))
    else:
        # converter installed locally: conversion of garbage still fails
        with pytest.raises(cad_drawing.DrawingError):
            cad_drawing.import_drawing(str(dwg), str(tmp_path / "c"))


def test_dwg_to_dxf_cli_arg_order(tmp_path, monkeypatch):
    """Regression: ODA CLI is <src> <out> <ver> <type> <recurse> <audit>
    <filter>. Wrong order put "0" in the filter slot -> ODA pops
    "no matched files in input folder"."""
    dwg = tmp_path / "part.dwg"
    dwg.write_bytes(b"fake")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        # emulate ODA: writes the converted dxf into the output dir
        with open(os.path.join(cmd[2], "part.dxf"), "wb") as f:
            f.write(b"dxf")

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(cad_drawing, "probe_oda_converter",
                        lambda: r"C:\fake\ODAFileConverter.exe")
    monkeypatch.setattr(cad_drawing.subprocess, "run", fake_run)
    dxf = cad_drawing._dwg_to_dxf(str(dwg), str(tmp_path / "out"))
    assert dxf.endswith("part.dxf")
    # oda, src_dir, out_dir, then the five option slots
    assert seen["cmd"][3:] == ["ACAD2018", "DXF", "0", "0", "*.DWG"]


def test_svg_entity_limit(tmp_path):
    """Pathological files (audit M4-style) are truncated, not exploded."""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for i in range(300):
        msp.add_line((i, 0), (i, 1))
    path = tmp_path / "many.dxf"
    doc.saveas(str(path))
    svg = cad_drawing.dxf_to_svg(doc, max_entities=100)
    assert svg.count("<line") == 100
