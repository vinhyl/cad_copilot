"""MCP server exposing CAD geometry capabilities to WorkBuddy.

Tools:
  - convert_file       : STEP/IGES/STL/BREP <-> STEP/IGES/STL/BREP
  - extract_properties : volume, area, bbox, center of mass, topology, assembly
  - export_preview     : emit STL + self-contained HTML viewer
  - batch_convert      : convert every supported file in a folder + JSON report
  - create_primitive   : box / cylinder
  - edit_geometry      : fillet / chamfer / scale / drill
  - boolean_parts      : fuse / cut / common of two solids
  - pick_features      : interactive feature-picking 3D preview (offline HTML + vendor)

Run:  python cad_mcp_server.py   (stdio transport, consumed by WorkBuddy mcp.json)
"""
from __future__ import annotations

import json
import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cad_core  # noqa: E402
import cad_build  # noqa: E402  (loads build123d via font-scan shim)

from fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("cad-engine")


@mcp.tool()
def convert_file(input_path: str, output_path: str) -> str:
    """Convert a CAD file between STEP/IGES/STL/BREP.

    Args:
        input_path: path to source (.step/.stp/.igs/.iges/.stl/.brep)
        output_path: destination path; format inferred from its extension
    Returns: confirmation string with the output path.
    """
    shape = cad_core.read_shape(input_path)
    cad_core.write_shape(shape, output_path)
    return f"Converted {input_path} -> {output_path}"


@mcp.tool()
def extract_properties(input_path: str) -> str:
    """Extract geometric + topological properties of a CAD file as JSON.

    Returns volume, surface area, center of mass, bounding box, topology counts
    (solids/faces/edges/vertices) and whether the shape is an assembly.
    """
    shape = cad_core.read_shape(input_path)
    return json.dumps(cad_core.properties(shape), indent=2, ensure_ascii=False)


@mcp.tool()
def export_preview(input_path: str, out_dir: str = "") -> str:
    """Export a CAD file to a viewable STL + HTML viewer (returns JSON paths)."""
    res = cad_core.export_preview(input_path, out_dir or None)
    return json.dumps(res, ensure_ascii=False)


@mcp.tool()
def batch_convert(input_dir: str, output_dir: str, out_ext: str = ".step") -> str:
    """Convert every supported CAD file in input_dir and write a JSON report.

    Args:
        input_dir: folder to scan
        output_dir: folder for converted outputs + report.json
        out_ext: output extension, one of .step/.stp/.igs/.iges/.stl/.brep
    """
    patterns = ("*.step", "*.stp", "*.igs", "*.iges", "*.stl", "*.brep", "*.brp")
    files = []
    for pat in patterns:
        files.extend(glob.glob(os.path.join(input_dir, pat)))
        files.extend(glob.glob(os.path.join(input_dir, pat.upper())))
    files = sorted(set(files))
    os.makedirs(output_dir, exist_ok=True)
    report = []
    for fp in files:
        base = os.path.splitext(os.path.basename(fp))[0]
        out = os.path.join(output_dir, base + out_ext)
        rec = {"source": fp, "target": out, "status": "ok"}
        try:
            shape = cad_core.read_shape(fp)
            cad_core.write_shape(shape, out)
        except Exception as e:  # noqa: BLE001
            rec["status"] = "error"
            rec["error"] = str(e)
        report.append(rec)
    rep_path = os.path.join(output_dir, "report.json")
    with open(rep_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    ok = sum(1 for r in report if r["status"] == "ok")
    return json.dumps({"scanned": len(report), "ok": ok, "report": rep_path},
                      ensure_ascii=False)


@mcp.tool()
def create_primitive(kind: str, output_path: str, dx: float = 0.0, dy: float = 0.0,
                    dz: float = 0.0, radius: float = 0.0, height: float = 0.0,
                    center: str = "0,0,0", direction: str = "0,0,1") -> str:
    """Create a primitive solid and write it to output_path.

    Args:
        kind: 'box' (needs dx,dy,dz) or 'cylinder' (needs radius,height)
        center / direction: comma-separated triples, e.g. '10,5,0' / '0,0,1'
    """
    kind = (kind or "").lower()
    if kind == "box":
        if not (dx and dy and dz):
            raise ValueError("box needs dx, dy, dz > 0")
        shp = cad_core.box(dx, dy, dz)
    elif kind == "cylinder":
        if not (radius and height):
            raise ValueError("cylinder needs radius, height > 0")
        c = [float(x) for x in center.split(",")]
        d = [float(x) for x in direction.split(",")]
        shp = cad_core.cylinder(radius, height, c, d)
    else:
        raise ValueError("kind must be 'box' or 'cylinder'")
    cad_core.write_shape(shp, output_path)
    return f"Created {kind} -> {output_path}"


@mcp.tool()
def edit_geometry(input_path: str, output_path: str, operation: str,
                  radius: float = 0.0, distance: float = 0.0, factor: float = 1.0,
                  position: str = "0,0,0", direction: str = "0,0,1",
                  depth: float = 10.0) -> str:
    """Apply a modeling operation to a CAD file and write the result.

    operation:
      - fillet  : round all edges, use 'radius'
      - chamfer : bevel all edges, use 'distance'
      - scale   : uniform scale, use 'factor'
      - drill   : subtract a cylindrical hole; use 'position' (x,y,z),
                  'radius', 'depth', 'direction' (x,y,z)
    """
    shp = cad_core.read_shape(input_path)
    op = (operation or "").lower()
    if op == "fillet":
        out = cad_core.fillet(shp, radius)
    elif op == "chamfer":
        out = cad_core.chamfer(shp, distance)
    elif op == "scale":
        out = cad_core.scale_shape(shp, factor)
    elif op == "drill":
        p = [float(x) for x in position.split(",")]
        d = [float(x) for x in direction.split(",")]
        out = cad_core.drill_hole(shp, p, d, radius, depth)
    else:
        raise ValueError("operation must be fillet|chamfer|scale|drill")
    cad_core.write_shape(out, output_path)
    return f"Applied {op} -> {output_path}"


@mcp.tool()
def boolean_parts(input_path: str, with_path: str, op: str, output_path: str) -> str:
    """Combine two solids (op = fuse | cut | common) and write the result."""
    a = cad_core.read_shape(input_path)
    b = cad_core.read_shape(with_path)
    out = cad_core.boolean(a, b, op)
    cad_core.write_shape(out, output_path)
    return f"Boolean {op} -> {output_path}"


@mcp.tool()
def build123d_model(script: str, output_path: str) -> str:
    """Run a build123d modeling script and write the result to output_path.

    This is the preferred tool for parametric modeling and for feature-based
    editing of existing CAD files by an AI Agent: build123d's declarative API
    (BuildPart / Box / Hole / fillet / ...) and its streaming Selector API
    (e.g. `faces().sort_by(Axis.Z)[-1]`, `edges().filter_by(Axis.X)`,
    `faces().group_by(Axis.Z)`) are far less error-prone for an LLM than raw
    OCP calls, and far less prone to class-name / argument-order hallucination.

    The full build123d API is in scope. The script MUST assign the final
    geometry to a variable named `result` -- a build123d Part/BuildPart, or a
    raw TopoDS_Shape. Returns JSON properties (volume, area, bbox, topology) of
    the generated shape.

    Args:
        script: build123d Python code; assign output to `result`
        output_path: destination; format inferred from its extension
    """
    return cad_build.run_build123d_script(script, output_path)


@mcp.tool()
def pick_features(input_path: str, out_dir: str = "") -> str:
    """Generate an interactive, clickable 3D feature-picking preview for a CAD file.

    Unlike export_preview (a static viewer), this emits an HTML page where every
    feature is its own separately clickable mesh: selecting a feature in the list
    (or clicking its surface in 3D) highlights it with an x-ray overlay + orange
    edge outline, shows its properties, and can auto-focus the camera. The page is
    fully offline -- three.js is vendored locally under ./vendor/ next to the HTML.

    Args:
        input_path: path to source (.step/.stp/.igs/.iges/.stl/.brep)
        out_dir: output folder for the HTML + vendored three.js; defaults to
                 ./previews next to this server file
    Returns: JSON with the HTML path, feature count, per-feature metadata, and
             shape properties (volume / faces / edges / bounding-box size).
    """
    import feature_picker  # lazy: only loaded when this tool is actually called
    res = feature_picker.make_picker(input_path, out_dir or None)
    return json.dumps(res, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
