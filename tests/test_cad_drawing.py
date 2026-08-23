"""Phase D — drawing import (DXF native / DWG via ODA) & semantic
calibration tests (ADR-0002 D5 / 模块六).

DXF fixtures are generated with ezdxf itself (threads/diameters/tolerances
callouts + geometry); DWG path is covered by the ODA-missing degradation
case (no converter installed in CI).
"""
from __future__ import annotations

import os
import re

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
    msp.add_text("DWG NO.001", dxfattribs={"height": 5.0}).set_placement((70, 65))
    path = tmp_path_factory.mktemp("dxf") / "pump_head.dxf"
    doc.saveas(str(path))
    return str(path)


def test_import_dxf_semantics_and_svg(drawing_dxf, tmp_path):
    out = str(tmp_path / "dwg_cache")
    res = cad_drawing.import_drawing(drawing_dxf, out)

    assert res["schema_version"] == cad_drawing.DRAWING_SCHEMA_VERSION
    assert res["source_file"] == "pump_head.dxf"
    assert len(res["source_sha256"]) == 64
    assert res["oda_used"] is False

    kinds = {(s["kind"], str(s["value"])) for s in res["semantics"]}
    assert ("thread", "M10x1.5") in kinds
    assert ("diameter", "8.0") in kinds
    assert ("tolerance", "H7/g6") in kinds
    assert any(k == "note" for k, _ in kinds)          # DWG NO.001 -> note
    assert not any(v == "总装图" for k, v in kinds)    # v11：图名噪声被过滤

    # SVG written and contains the geometry（合并路径架构：描边实体在
    # <path d="M..."> 里，圆是独立元素，文字是独立元素）
    svg = open(os.path.join(out, "view.svg"), encoding="utf-8").read()
    assert svg.startswith("<svg")
    assert 'd="M' in svg and "<circle" in svg
    assert "M10x1.5" in svg                            # text rendered
    # Y-flip: sheet top edge (y=60) renders as negative in the flipped view
    assert "L100 -60" in svg                            # polyline top edge
    assert 'y="-65"' in svg                             # 总装图 text at y=65

    # drawing.json persisted
    import json
    j = json.load(open(os.path.join(out, "drawing.json"), encoding="utf-8"))
    assert j["semantics"] == res["semantics"]


def test_import_idempotent_cache(drawing_dxf, tmp_path):
    out = str(tmp_path / "dwg_cache")
    r1 = cad_drawing.import_drawing(drawing_dxf, out)
    r2 = cad_drawing.import_drawing(drawing_dxf, out)
    assert r1 == r2                                    # same source content


def test_part_name_classification(tmp_path):
    """v10：语义侧栏"零件"分组——从杂项文字里滤出零件名，剔除
    图框栏位 / 技术要求 / 视图名 / 标准件规格。"""
    doctexts = [
        "齿圈",                    # 定制件后缀，纯中文 → 零件名
        "咖啡机顶盖组件",           # 定制件后缀"组件" → 零件名
        "多齿调档座C4-1-1",         # 定制件后缀"座" + 规格 → 零件名
        "M8X20盘头内六角螺丝",       # 标准件 + M 规格 → 非零件
        "总装图",                  # 视图/图名 → 非零件
        "批准",                    # 图框栏位 → 非零件
        "技术要求：去毛刺",          # 技术要求 → 非零件
        "V1",                      # 版本 → 非零件
        "Ø1.5*7密封圈",            # 规格+标准件 → 非零件
    ]
    kinds = {}
    for t in doctexts:
        kinds[t] = cad_drawing.is_part_name(cad_drawing.clean_entity_text(t))

    for t in ["齿圈", "咖啡机顶盖组件", "多齿调档座C4-1-1"]:
        assert kinds[t] is True, f"应判为零件名: {t}"
    for t in ["M8X20盘头内六角螺丝", "总装图", "批准", "技术要求：去毛刺", "V1", "Ø1.5*7密封圈"]:
        assert kinds[t] is False, f"应滤除: {t}"


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
    # 合并路径架构：每条 LINE 是描边 <path> d 里的一个子路径（一个 M 命令）
    m = re.search(r'<path vector-effect="non-scaling-stroke" d="([^"]*)"', svg)
    assert m and m.group(1).count("M") == 100


def test_lwpolyline_with_bulge_emits_arc():
    """Bulge segments render as SVG arcs (rounded polyline accuracy)."""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    # (x, y, start_w, end_w, bulge) -- bulge 0.6 on segment (10,0)->(10,10)
    msp.add_lwpolyline([(0, 0), (10, 0), (10, 10, 0, 0, 0.6), (0, 10)], close=True)
    svg = cad_drawing.dxf_to_svg(doc)
    assert re.search(r"A\d+\.\d+", svg)      # SVG arc command present
    assert "Z" in svg                        # closed polyline


def test_svg_merged_path_architecture(tmp_path):
    """v5 合并路径架构：描边实体合成 1 个 path、圆独立元素、无逐元素
    data-* 属性（10 万级实体图纸的 DOM 节点/字节减负）。"""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((0, 0), (1, 0), dxfattribs={"layer": "轮廓"})
    msp.add_line((2, 0), (3, 0))
    msp.add_circle((5, 5), radius=2, dxfattribs={"layer": "中心线"})
    svg = cad_drawing.dxf_to_svg(doc)
    m = re.search(r'<path vector-effect="non-scaling-stroke" d="([^"]*)"', svg)
    assert m is not None                      # 全部描边实体 = 1 个 path
    assert m.group(1).count("M") == 2         # 两条 LINE 两个子路径
    assert svg.count("<circle") == 1
    assert "data-etype" not in svg            # 字节瘦身：不再逐元素带属性


def test_insert_and_dimension_expansion(tmp_path):
    """v5：INSERT 块引用与 DIMENSION 标注展开渲染——大装配图纸的标注
    几何（尺寸线/箭头 SOLID/数值文本）由此进入视图。"""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    # 块：一条对角线，插入两次（一次带平移）
    blk = doc.blocks.new("GEOM")
    blk.add_line((0, 0), (10, 10))
    msp.add_blockref("GEOM", insert=(0, 0))
    msp.add_blockref("GEOM", insert=(100, 0))
    # 线性标注（render() 生成标注几何块：尺寸线/箭头 SOLID/数值文本/
    # Defpoints 层定义点）
    msp.add_linear_dim(base=(0, 20), p1=(0, 0), p2=(30, 0)).render()
    svg = cad_drawing.dxf_to_svg(doc)
    m = re.search(r'<path vector-effect="non-scaling-stroke" d="([^"]*)"', svg)
    assert m and m.group(1).count("M") >= 4   # 2 块引用线 + 标注尺寸线等
    assert "100" in m.group(1)                # 平移后的块引用几何
    assert "<text" in svg                      # 标注数值文本已渲染
    assert svg.count("<path") >= 2             # 描边 path + 箭头 SOLID 填充 path


def test_outlier_entities_do_not_break_viewbox():
    """v9：离群实体（挪出图幅的旧标注）**完整渲染**（完整性优先，绝不
    删除内容），但默认取景框用鲁棒范围（中位数 ± k·MAD）收敛到主体，
    viewBox 不被离群实体撑爆。"""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    # 主体：密集小几何
    for i in range(80):
        msp.add_line((i, 0), (i, 5))
    # 离群实体：x=100000 远方的废弃标注（完整渲染，但不撑爆取景框）
    msp.add_line((100000, 0), (100010, 0))
    msp.add_text("废", dxfattribs={"height": 3}).set_placement((100005, 1))
    svg = cad_drawing.dxf_to_svg(doc)
    assert "100000" in svg                     # v9：离群实体仍完整渲染
    assert "废" in svg                         # 文字同样完整渲染
    vb = [float(v) for v in
          re.search(r'viewBox="([^"]+)"', svg).group(1).split()]
    assert vb[2] < 200                         # 取景框宽度按主体 ~80 收敛


def test_hatch_renders_as_filled_path(tmp_path):
    """v5：HATCH 剖面线渲染为半透明填充路径（evenodd 处理嵌套岛）。"""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    hatch = msp.add_hatch()
    hatch.paths.add_polyline_path(
        [(0, 0), (10, 0), (10, 10), (0, 10)], is_closed=True)
    svg = cad_drawing.dxf_to_svg(doc)
    assert 'fill-opacity="0.25"' in svg
    assert 'fill-rule="evenodd"' in svg
    assert re.search(r'<path fill="#dfe3ea"[^>]*d="M0 -?0L10 -?0L10 -10L0 -10Z', svg)


def test_import_drawing_summary_fields(drawing_dxf, tmp_path):
    """drawing.json exposes entity_types breakdown and ordered layer list."""
    out = str(tmp_path / "sum")
    res = cad_drawing.import_drawing(drawing_dxf, out)
    assert isinstance(res["entity_types"], dict)
    assert res["entity_types"]["LINE"] == 2
    assert res["entity_types"]["LWPOLYLINE"] == 1
    assert res["layers"] == ["0"]            # fixture uses default layer only
