"""cad_build: build123d modeling layer for the AI Agent, on top of the OCP kernel.

Why this layer exists
--------------------
For an AI Agent doing CAD parsing / editing / parametric modeling, build123d's
declarative API (BuildPart / Box / Hole / selectors like `faces().sort_by(Axis.Z)`)
is dramatically easier and less error-prone than raw OCP calls, and far less prone
to LLM "hallucination" of wrong class names / argument order. We keep OCP (via
cadquery-ocp-novtk) as the geometry kernel underneath.

The one blocker: build123d's `text.py` scans the system font directory at import
time and calls `TTFont(path)` on every .ttf/.otf/.ttc. On this machine a single
corrupt system font (`C:/Windows/Fonts/mstmc.ttf`) raises
`TTLibError: Not a TrueType or OpenType font (bad sfntVersion)` and aborts the
whole import — even though modeling never needs fonts. (Note: .fon and .ttc are
handled fine; only that one bad .ttf is the culprit.)

Fix
---
An import-hook (MetaPathFinder) intercepts `build123d.text` and, right before the
module-level `available_fonts = FontManager().available_fonts`, wraps
`FontManager.register_font` so a font that fails to load is simply skipped.
No edits to site-packages; survives reinstalls of build123d.
"""
from __future__ import annotations

import importlib.abc
import importlib.util
import json
import sys

import cad_core  # noqa: E402  (gives us _SuppressStdout + write/properties)
from OCP.TopoDS import TopoDS_Shape  # noqa: E402


# ---------------------------------------------------------------------------
# Import hook: patch build123d.text font scanning at load time
# ---------------------------------------------------------------------------
class _Build123dTextFinder(importlib.abc.MetaPathFinder):
    def __init__(self):
        self._active = False

    def find_spec(self, fullname, path, target=None):
        if fullname != "build123d.text" or self._active:
            return None
        self._active = True
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            self._active = False
        if spec is None:
            return None
        spec.loader = _PatchedTextLoader(spec.loader)
        return spec


class _PatchedTextLoader(importlib.abc.Loader):
    def __init__(self, orig):
        self._orig = orig

    def create_module(self, spec):
        return self._orig.create_module(spec)

    def exec_module(self, module):
        origin = module.__spec__.origin
        with open(origin, "r", encoding="utf-8") as fh:
            src = fh.read()
        marker = "available_fonts = FontManager().available_fonts"
        if marker in src:
            patch = (
                "# --- build123d font-scan guard (patched at import by cad_build) ---\n"
                "_orig_register_font = FontManager.register_font\n"
                "def _safe_register_font(self, path, override=False, single_stroke=False):\n"
                "    try:\n"
                "        return _orig_register_font(self, path, override, single_stroke)\n"
                "    except Exception:\n"
                "        return []\n"
                "FontManager.register_font = _safe_register_font\n"
                "# --- end patch ---\n"
            )
            src = src.replace(marker, patch + marker, 1)
        module.__name__ = "build123d.text"
        module.__package__ = "build123d"
        code = compile(src, origin, "exec")
        exec(code, module.__dict__)


# Install the hook BEFORE importing build123d.
sys.meta_path.insert(0, _Build123dTextFinder())

# Import build123d now (font scan is guarded; silence any stray OCCT stdout).
with cad_core._SuppressStdout():
    import build123d  # noqa: E402,F401

__all__ = ["run_build123d_script", "to_occt_shape"]


def to_occt_shape(obj, _depth: int = 0) -> TopoDS_Shape:
    """Extract a TopoDS_Shape from a build123d object or pass through a shape.

    Handles the common build123d shapes:
      BuildPart -> .part (Part) -> .wrapped (TopoDS_Shape)
      Part     -> .wrapped (TopoDS_Shape)
    """
    if isinstance(obj, TopoDS_Shape):
        return obj
    for attr in ("wrapped", "part", "shape"):
        cand = getattr(obj, attr, None)
        if cand is None:
            continue
        if callable(cand):
            cand = cand()
        if isinstance(cand, TopoDS_Shape):
            return cand
        # one level of nesting (e.g. BuildPart.part -> Part)
        if _depth < 3 and cand is not None:
            try:
                return to_occt_shape(cand, _depth + 1)
            except TypeError:
                pass
    raise TypeError(
        "build123d `result` is not a recognizable shape. Assign a build123d "
        "Part/BuildPart or a TopoDS_Shape to `result`. Got: "
        f"{type(obj).__name__}"
    )


def run_build123d_script(src: str, output_path: str) -> str:
    """Execute a build123d modeling script and write the result to output_path.

    The script must assign its final geometry to a variable named ``result``
    (a build123d Part/BuildPart, or a raw TopoDS_Shape). The full build123d API
    (BuildPart, Box, Hole, selectors such as `faces().sort_by(Axis.Z)`, etc.) is
    available. Returns JSON properties of the generated shape.
    """
    g = {"__name__": "__cad_build_script__"}
    exec("from build123d import *", g)
    exec(src, g)
    result = g.get("result")
    if result is None:
        raise ValueError(
            "build123d script must assign the final shape to `result` "
            "(e.g. `result = part.part` or `result = p`)."
        )
    shape = to_occt_shape(result)
    cad_core.write_shape(shape, output_path)
    return json.dumps(cad_core.properties(shape), indent=2, ensure_ascii=False)
