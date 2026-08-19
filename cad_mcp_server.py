"""MCP server exposing CAD geometry capabilities to WorkBuddy.

Tools:
  - convert_file       : STEP/IGES/STL/BREP <-> STEP/IGES/STL/BREP
  - extract_properties : volume, area, bbox, center of mass, topology, assembly
  - batch_convert      : convert every supported file in a folder + JSON report
  - create_primitive   : box / cylinder
  - edit_geometry      : fillet / chamfer / scale / drill
  - boolean_parts      : fuse / cut / common of two solids
  - pick_features      : interactive feature-picking 3D preview (offline HTML + vendor)
  - parse_assembly     : assembly STEP -> tree + 4x4 matrices + dedup part
                         templates (glTF cache for the Web frontend)
  - build123d_model    : run a build123d modeling script (DISABLED by default;
                         requires CAD_MCP_ALLOW_BUILD123D=1 -- local code execution)

Run:  python cad_mcp_server.py   (stdio transport, consumed by WorkBuddy mcp.json)
"""
from __future__ import annotations

import json
import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cad_core  # noqa: E402

from fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("cad-engine")

# ---------------------------------------------------------------------------
# Path safety: every user-supplied path is confined to ALLOWED_DIRS.
# Configure with CAD_MCP_ALLOWED_DIRS (os.pathsep-separated list of absolute
# dirs; default "." = the server's working directory). A path that does not
# resolve inside one of these dirs (e.g. via "../") is rejected.
# ---------------------------------------------------------------------------
ALLOWED_DIRS = [os.path.abspath(p)
                for p in os.environ.get("CAD_MCP_ALLOWED_DIRS", ".").split(os.pathsep)
                if p]


def _safe_path(p: str) -> str:
    """Resolve p to a real path and ensure it lives inside ALLOWED_DIRS.

    Raises ValueError if p escapes the allowed directories. Non-existent paths
    are allowed (so output dirs can be created), since realpath does not require
    the file to already exist.
    """
    rp = os.path.realpath(p)
    if not any(rp == d or rp.startswith(d + os.sep) for d in ALLOWED_DIRS):
        raise ValueError(f"path outside allowed dirs: {p}")
    return rp


@mcp.tool()
def convert_file(input_path: str, output_path: str) -> str:
    """Convert a CAD file between STEP/IGES/STL/BREP.

    Args:
        input_path: path to source (.step/.stp/.igs/.iges/.stl/.brep)
        output_path: destination path; format inferred from its extension
    Returns: confirmation string with the output path.
    """
    shape = cad_core.read_shape(_safe_path(input_path))
    cad_core.write_shape(shape, _safe_path(output_path))
    return f"Converted {input_path} -> {output_path}"


@mcp.tool()
def extract_properties(input_path: str) -> str:
    """Extract geometric + topological properties of a CAD file as JSON.

    Returns volume, surface area, center of mass, bounding box, topology counts
    (solids/faces/edges/vertices) and whether the shape is an assembly.
    """
    shape = cad_core.read_shape(_safe_path(input_path))
    return json.dumps(cad_core.properties(shape), indent=2, ensure_ascii=False)


@mcp.tool()
def batch_convert(input_dir: str, output_dir: str, out_ext: str = ".step") -> str:
    """Convert every supported CAD file in input_dir and write a JSON report.

    Args:
        input_dir: folder to scan
        output_dir: folder for converted outputs + report.json
        out_ext: output extension, one of .step/.stp/.igs/.iges/.stl/.brep
    """
    input_dir = _safe_path(input_dir)
    output_dir = _safe_path(output_dir)
    patterns = ("*.step", "*.stp", "*.igs", "*.iges", "*.stl", "*.brep", "*.brp")
    files = []
    for pat in patterns:
        files.extend(glob.glob(os.path.join(input_dir, pat)))
        files.extend(glob.glob(os.path.join(input_dir, pat.upper())))
    files = sorted(set(files))
    os.makedirs(output_dir, exist_ok=True)
    report = []
    skipped = []
    for fp in files:
        base = os.path.splitext(os.path.basename(fp))[0]
        out = os.path.join(output_dir, base + out_ext)
        # Never overwrite the source file: when input and output resolve to the
        # same path (e.g. input_dir == output_dir with the same extension) skip
        # it instead of clobbering the user's data.
        if os.path.realpath(out) == os.path.realpath(fp):
            skipped.append(fp)
            continue
        rec = {"source": fp, "target": out, "status": "ok"}
        try:
            shape = cad_core.read_shape(fp)
            cad_core.write_shape(shape, out, overwrite=True)
        except Exception as e:  # noqa: BLE001
            rec["status"] = "error"
            rec["error"] = str(e)
        report.append(rec)
    rep_path = os.path.join(output_dir, "report.json")
    with open(rep_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    ok = sum(1 for r in report if r["status"] == "ok")
    return json.dumps({"scanned": len(report), "ok": ok,
                       "skipped": len(skipped), "skipped_files": skipped,
                       "report": rep_path},
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
    cad_core.write_shape(shp, _safe_path(output_path))
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
    shp = cad_core.read_shape(_safe_path(input_path))
    op = (operation or "").lower()
    if op == "fillet":
        if radius <= 0:
            raise ValueError("fillet needs radius > 0")
        out = cad_core.fillet(shp, radius)
    elif op == "chamfer":
        if distance <= 0:
            raise ValueError("chamfer needs distance > 0")
        out = cad_core.chamfer(shp, distance)
    elif op == "scale":
        if factor <= 0:
            raise ValueError("scale needs factor > 0")
        out = cad_core.scale_shape(shp, factor)
    elif op == "drill":
        if radius <= 0:
            raise ValueError("drill needs radius > 0")
        if depth <= 0:
            raise ValueError("drill needs depth > 0")
        p = [float(x) for x in position.split(",")]
        d = [float(x) for x in direction.split(",")]
        out = cad_core.drill_hole(shp, p, d, radius, depth)
    else:
        raise ValueError("operation must be fillet|chamfer|scale|drill")
    cad_core.write_shape(out, _safe_path(output_path))
    return f"Applied {op} -> {output_path}"


@mcp.tool()
def boolean_parts(input_path: str, with_path: str, op: str, output_path: str) -> str:
    """Combine two solids (op = fuse | cut | common) and write the result."""
    a = cad_core.read_shape(_safe_path(input_path))
    b = cad_core.read_shape(_safe_path(with_path))
    out = cad_core.boolean(a, b, op)
    cad_core.write_shape(out, _safe_path(output_path))
    return f"Boolean {op} -> {output_path}"


@mcp.tool()
def build123d_model(script: str, output_path: str) -> str:
    """Run a build123d modeling script and write the result to output_path.

    SECURITY: this executes arbitrary Python (a build123d script) with the full
    privileges of the MCP server process. It is therefore DISABLED by default
    and only runs when the environment variable CAD_MCP_ALLOW_BUILD123D=1 is
    set. Executing arbitrary scripts equals LOCAL CODE EXECUTION -- never
    enable this tool in an untrusted, shared, or multi-tenant environment.

    When enabled, the script runs in an isolated, timeout-guarded subprocess
    (see cad_build.run_build123d_script; CAD_BUILD_TIMEOUT overrides the default
    30s). The build123d API is in scope; the script MUST assign the final
    geometry to a variable named `result`. Returns JSON properties of the
    generated shape.

    Args:
        script: build123d Python code; assign output to `result`
        output_path: destination; format inferred from its extension
    """
    if os.environ.get("CAD_MCP_ALLOW_BUILD123D") != "1":
        return ("build123d_model is disabled by default "
                "(set CAD_MCP_ALLOW_BUILD123D=1 to enable); "
                "executing arbitrary scripts equals local code execution.")
    output_path = _safe_path(output_path)
    import cad_build  # lazy: avoid a hard build123d dependency at import time
    return cad_build.run_build123d_script(script, output_path)


@mcp.tool()
def pick_features(input_path: str, out_dir: str = "") -> str:
    """Generate an interactive, clickable 3D feature-picking preview for a CAD file.

    Emits an HTML page where every feature is its own separately clickable mesh:
    selecting a feature in the list (or clicking its surface in 3D) highlights it
    with an x-ray overlay + orange edge outline, shows its properties, and can
    auto-focus the camera. The page is fully offline -- three.js is vendored
    locally under ./vendor/ next to the HTML.

    Args:
        input_path: path to source (.step/.stp/.igs/.iges/.stl/.brep)
        out_dir: output folder for the HTML + vendored three.js; defaults to
                 ./previews next to this server file
    Returns: JSON with the HTML path, feature count, per-feature metadata, and
             shape properties (volume / faces / edges / bounding-box size).
    """
    import feature_picker  # lazy: only loaded when this tool is actually called
    res = feature_picker.make_picker(_safe_path(input_path),
                                     _safe_path(out_dir) if out_dir else None)
    return json.dumps(res, ensure_ascii=False)


@mcp.tool()
def parse_assembly(input_path: str, out_dir: str = "") -> str:
    """Parse an assembly STEP into a decoupled Template + Matrix manifest.

    Returns JSON with: the assembly tree (named nodes, each carrying its
    accumulated world 4x4 matrix), and a deduplicated part-template list --
    every unique part appears once (same-spec instances share one template),
    with per-template glTF geometry for the Web frontend.

    A flat single-solid STEP is handled too (becomes a one-part tree with a
    fallback name); a multi-root STEP gets a synthetic root named after the
    file. All matrices are in millimetres.

    When out_dir is given, also writes the frontend cache layout:
      out_dir/tree_structure.json     (the manifest itself)
      out_dir/gltf_library/tN.gltf/.bin (one glTF per unique part template)
    Existing cache files are overwritten (cache semantics; the manifest
    carries the source SHA-256 as its cache key).

    Args:
        input_path: source assembly (.step/.stp)
        out_dir: optional cache output folder; defaults to no file output
    Returns: JSON manifest string (tree + templates + metadata).
    """
    import cad_assembly  # lazy: only loaded when this tool is actually called
    if out_dir:
        manifest = cad_assembly.build_cache(
            _safe_path(input_path), _safe_path(out_dir))
    else:
        manifest = cad_assembly.parse_assembly(_safe_path(input_path))
        manifest.pop("_shapes", None)
    return json.dumps(manifest, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
