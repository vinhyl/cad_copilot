"""Drawing (DXF/DWG) import & semantic calibration (Phase D, ADR-0002 D5/模块六).

Pipeline (ezdxf first, ODA-gated DWG):

    DWG --[ODA File Converter, probed]--> DXF --[ezdxf]--> {
        semantics: threads / diameters / tolerances extracted from TEXT,
                   MTEXT and DIMENSION measurement strings,
        svg:        minimal renderer (LINE/CIRCLE/ARC/LWPOLYLINE/TEXT),
    } cached under  workspace/drawings/<sha16>/  (R8 key, R17 idempotent)

DXF is read with ezdxf (already in the venv as a transitive dep; explicit
in requirements). DWG is NOT read natively -- ODA File Converter is probed
at well-known install paths and invoked as a subprocess (D5: probe +
graceful degradation + cached conversion; R9: never bundled).

The minimal SVG renderer exists because the ezdxf drawing add-on requires
PIL, which this project deliberately does not add (轻依赖). It covers the
entity types that matter for a 2D 对照 view.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import ezdxf

# --- ODA File Converter probe paths (D5) -------------------------------
_ODA_CANDIDATES = [
    r"C:\Program Files\ODA\ODAFileConverter 25\ODAFileConverter.exe",
    r"C:\Program Files\ODA\ODAFileConverter 24\ODAFileConverter.exe",
    r"C:\Program Files\ODA\ODAFileConverter 23\ODAFileConverter.exe",
    "/usr/bin/ODAFileConverter",
    "/usr/local/bin/ODAFileConverter",
    "/opt/ODAFileConverter/ODAFileConverter",
]

# --- semantic patterns (模块六: threads / diameters / tolerances) --------
_RE_THREAD = re.compile(r"\bM(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\b")
_RE_DIAMETER = re.compile(r"[ØøΦφ⌀]\s*(\d+(?:\.\d+)?)")
_RE_TOLERANCE = re.compile(r"\b([A-Z][0-9])\s*/\s*([a-z][0-9])\b")


class DrawingError(RuntimeError):
    """Raised for unsupported input or missing ODA converter (D5)."""


def probe_oda_converter() -> str | None:
    """Return the ODA File Converter executable path, or None."""
    for p in _ODA_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _dwg_to_dxf(dwg_path: str, out_dir: str) -> str:
    """Convert a DWG to DXF via ODA File Converter (D5: probe + degrade)."""
    oda = probe_oda_converter()
    if oda is None:
        raise DrawingError(
            "DWG 输入需要 ODA File Converter（未在常见安装路径找到）。"
            "请安装 ODA（免费）或将图纸另存为 DXF。DXF 输入无需任何外部工具。")
    os.makedirs(out_dir, exist_ok=True)
    # ODA CLI: ODAFileConverter <src_dir> <out_dir> <version> <type> <recurse>
    #   version: ACAD9..ACAD2018; type: DXF/DWG/DXB; recurse 0/1
    src_dir = os.path.dirname(os.path.abspath(dwg_path)) or "."
    r = subprocess.run(
        [oda, src_dir, out_dir, "ACAD2018", "DXF", "0", "*", "0"],
        capture_output=True, timeout=120)
    base = os.path.splitext(os.path.basename(dwg_path))[0]
    dxf = os.path.join(out_dir, base + ".dxf")
    if r.returncode != 0 or not os.path.isfile(dxf):
        raise DrawingError(f"ODA 转换失败（exit {r.returncode}）：{dwg_path}")
    return dxf


# --------------------------------------------------------------------------
# Semantic extraction (模块六 语义真理)
# --------------------------------------------------------------------------

def extract_semantics(doc) -> list:
    """Extract thread/diameter/tolerance callouts from TEXT/MTEXT/DIMENSION.

    Returns [{kind, value, text, entity, position}] -- deterministic text
    parsing, no guessing. `kind` in {thread, diameter, tolerance, note}.
    """
    out = []
    msp = doc.modelspace()

    def push(kind, value, text, pos):
        out.append({"kind": kind, "value": value, "text": text,
                    "position": [round(c, 3) for c in pos] if pos else None})

    for e in msp.query("TEXT MTEXT"):
        text = (e.dxf.get("text", "") or "").strip()
        if not text:
            continue
        pos = None
        try:
            p = e.dxf.get("insert", None)
            pos = (p[0], p[1], 0) if p else None
        except Exception:  # noqa: BLE001
            pass
        for m in _RE_THREAD.finditer(text):
            push("thread", f"M{m.group(1)}x{m.group(2)}", text, pos)
        for m in _RE_DIAMETER.finditer(text):
            push("diameter", float(m.group(1)), text, pos)
        for m in _RE_TOLERANCE.finditer(text):
            push("tolerance", f"{m.group(1)}/{m.group(2)}", text, pos)
        if not (_RE_THREAD.search(text) or _RE_DIAMETER.search(text)
                or _RE_TOLERANCE.search(text)):
            push("note", text, text, pos)

    for e in msp.query("DIMENSION"):
        mtext = (e.dxf.get("text", "") or "").strip()
        if mtext and mtext != "<>":
            pos = None
            try:
                p = e.dxf.get("def_point", None)
                pos = (p[0], p[1], 0) if p else None
            except Exception:  # noqa: BLE001
                pass
            for m in _RE_THREAD.finditer(mtext):
                push("thread", f"M{m.group(1)}x{m.group(2)}", mtext, pos)
            for m in _RE_DIAMETER.finditer(mtext):
                push("diameter", float(m.group(1)), mtext, pos)
    return out


# --------------------------------------------------------------------------
# Minimal DXF -> SVG renderer (轻依赖: no PIL / matplotlib)
# --------------------------------------------------------------------------

def dxf_to_svg(doc, max_entities: int = 20000) -> str:
    """Render modelspace LINE/CIRCLE/ARC/LWPOLYLINE/TEXT to an SVG string.

    Flattens at most max_entities primitives (audit M4-style guard for
    pathological files). Output uses a Y-flipped user coordinate system so
    the SVG displays like a drawing sheet.
    """
    msp = doc.modelspace()
    parts = []
    count = 0

    def skip():
        nonlocal count
        count += 1
        return count > max_entities

    # first pass: bounds
    xs, ys = [], []
    for e in msp:
        t = e.dxftype()
        try:
            if t == "LINE":
                xs += [e.dxf.start.x, e.dxf.end.x]
                ys += [e.dxf.start.y, e.dxf.end.y]
            elif t == "CIRCLE":
                c = e.dxf.center
                xs += [c.x - e.dxf.radius, c.x + e.dxf.radius]
                ys += [c.y - e.dxf.radius, c.y + e.dxf.radius]
            elif t in ("TEXT", "MTEXT"):
                p = e.dxf.get("insert", None)
                if p is not None:
                    xs.append(p[0]); ys.append(p[1])
            elif t == "LWPOLYLINE":
                for p in e.get_points():
                    xs.append(p[0]); ys.append(p[1])
        except Exception:  # noqa: BLE001
            continue
    if not xs:
        raise DrawingError("图纸上没有可渲染的二维实体")
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    w = max(maxx - minx, 1.0)
    h = max(maxy - miny, 1.0)
    pad = 0.05
    vb = f"{minx - w * pad} {-maxy - h * pad} {w * (1 + 2 * pad)} {h * (1 + 2 * pad)}"

    for e in msp:
        t = e.dxftype()
        try:
            if t == "LINE" and not skip():
                s, en = e.dxf.start, e.dxf.end
                parts.append(
                    f'<line x1="{s.x:.3f}" y1="{-s.y:.3f}" '
                    f'x2="{en.x:.3f}" y2="{-en.y:.3f}"/>')
            elif t == "CIRCLE" and not skip():
                c = e.dxf.center
                parts.append(
                    f'<circle cx="{c.x:.3f}" cy="{-c.y:.3f}" r="{e.dxf.radius:.3f}"/>')
            elif t == "ARC" and not skip():
                import math
                c, r = e.dxf.center, e.dxf.radius
                a0, a1 = math.radians(e.dxf.start_angle), math.radians(e.dxf.end_angle)
                x0, y0 = c.x + r * math.cos(a0), -(c.y + r * math.sin(a0))
                x1, y1 = c.x + r * math.cos(a1), -(c.y + r * math.sin(a1))
                large = 1 if (a1 - a0) % (2 * math.pi) > math.pi else 0
                parts.append(
                    f'<path d="M{x0:.3f} {y0:.3f} A{r:.3f} {r:.3f} 0 '
                    f'{large} 1 {x1:.3f} {y1:.3f}" fill="none"/>')
            elif t == "LWPOLYLINE" and not skip():
                pts = [(p[0], -p[1]) for p in e.get_points()]
                if len(pts) >= 2:
                    d = "M" + " L".join(f"{x:.3f} {y:.3f}" for x, y in pts)
                    if e.closed:
                        d += " Z"
                    parts.append(f'<path d="{d}" fill="none"/>')
            elif t in ("TEXT", "MTEXT") and not skip():
                text = (e.dxf.get("text", "") or "").replace("&", "&amp;") \
                    .replace("<", "&lt;").replace(">", "&gt;")
                p = e.dxf.get("insert", None)
                hgt = e.dxf.get("height", 1.0) if t == "TEXT" else 1.0
                if p is not None and text:
                    parts.append(
                        f'<text x="{p[0]:.3f}" y="{-p[1]:.3f}" '
                        f'font-size="{hgt:.3f}">{text}</text>')
        except Exception:  # noqa: BLE001
            continue

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" '
        f'stroke="#dfe3ea" stroke-width="{max(w, h) * 0.0015:.4f}" '
        f'fill="none" font-family="monospace">'
        + "".join(parts) + "</svg>")


# --------------------------------------------------------------------------
# Import pipeline (cached, R8/R17)
# --------------------------------------------------------------------------

def import_drawing(input_path: str, out_dir: str) -> dict:
    """Import DXF (native) or DWG (via ODA) -> semantics + SVG, cached.

    Writes out_dir/{drawing.json, view.svg} (cache semantics; the drawing
    cache key is the SOURCE file sha256, stored in the result) and returns
    {"schema_version", "source_file", "source_sha256", "oda_used",
     "semantics", "entity_count"}.
    """
    import hashlib
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"No such file: {input_path}")
    ext = os.path.splitext(input_path)[1].lower()
    if ext not in (".dxf", ".dwg"):
        raise ValueError(f"unsupported drawing format: {ext} (DXF/DWG only)")

    sha = hashlib.sha256()
    with open(input_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            sha.update(chunk)
    key = sha.hexdigest()

    os.makedirs(out_dir, exist_ok=True)
    dxf_path = input_path if ext == ".dxf" else None
    oda_used = False
    if dxf_path is None:
        dxf_path = _dwg_to_dxf(input_path, out_dir)   # cached dwg_converted.dxf
        oda_used = True

    try:
        from ezdxf import recover
        doc, _ = recover.readfile(dxf_path)
    except ImportError:
        try:
            doc = ezdxf.readfile(dxf_path)
        except Exception as e:  # noqa: BLE001
            raise DrawingError(f"无法读取图纸 {input_path}: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise DrawingError(f"无法读取图纸 {input_path}: {e}") from e

    semantics = extract_semantics(doc)
    svg = dxf_to_svg(doc)
    with open(os.path.join(out_dir, "view.svg"), "w", encoding="utf-8") as f:
        f.write(svg)

    result = {
        "schema_version": 1,
        "source_file": os.path.basename(input_path),
        "source_sha256": key,
        "oda_used": oda_used,
        "semantics": semantics,
        "entity_count": len(doc.modelspace()),
    }
    with open(os.path.join(out_dir, "drawing.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result
