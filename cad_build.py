"""cad_build: build123d modeling layer for the AI Agent, on top of the OCP kernel.

⚠ SECURITY: run_build123d_script executes ARBITRARY Python (a build123d script)
with the full privileges of this process -- that is equivalent to LOCAL CODE
EXECUTION. Only ever call it from a trusted environment. The MCP server gates
it behind the CAD_MCP_ALLOW_BUILD123D=1 flag and runs it inside a timeout-
guarded subprocess. Never expose it to untrusted input.

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
import multiprocessing as _mp
import os
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


# Timeout (seconds) for a single build123d script; override via CAD_BUILD_TIMEOUT.
_BUILD_TIMEOUT = float(os.environ.get("CAD_BUILD_TIMEOUT", "30"))


def _run_build123d_child(src: str, output_path: str, result_q: "_mp.Queue") -> None:
    """Execute the script in an isolated subprocess and push the outcome back.

    Runs in the child process spawned by run_build123d_script. The build123d
    namespace is rebuilt locally (the parent's import is not inherited reliably
    across spawn), the user script is exec'd, the resulting shape is serialised
    and written, and the outcome is sent back as ("ok", json_props) or
    ("error", message).
    """
    try:
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
        props = json.dumps(cad_core.properties(shape), indent=2, ensure_ascii=False)
        result_q.put(("ok", props))
    except Exception as exc:  # noqa: BLE001
        result_q.put(("error", f"{type(exc).__name__}: {exc}"))


def run_build123d_script(src: str, output_path: str) -> str:
    """Execute a build123d modeling script and write the result to output_path.

    SECURITY: executing arbitrary scripts equals local code execution. The full
    build123d API is available and the script MUST assign its final geometry to
    a variable named ``result``. The script runs in an isolated subprocess with
    a hard timeout (CAD_BUILD_TIMEOUT seconds, default 30); on timeout the child
    is terminated and no output is written. The caller's signature is unchanged:
    returns JSON properties of the generated shape, or raises RuntimeError /
    TimeoutError on failure. Only call from a trusted environment.

    Args:
        src: build123d Python source; assign the final shape to `result`
        output_path: destination; format inferred from its extension
    """
    # Spawn so the child starts clean (no leaked fds / global state) and the
    # build123d namespace is rebuilt from scratch inside the child.
    try:
        _mp.set_start_method("spawn", force=True)
    except (RuntimeError, ValueError):
        pass
    q: "_mp.Queue" = _mp.Queue()
    proc = _mp.Process(target=_run_build123d_child, args=(src, output_path, q))
    proc.start()
    proc.join(_BUILD_TIMEOUT)
    if proc.is_alive():
        proc.terminate()
        proc.join()
        raise TimeoutError(
            f"build123d script exceeded timeout ({_BUILD_TIMEOUT}s) "
            f"and was terminated; output not written to {output_path}"
        )
    if q.empty():
        raise RuntimeError(
            "build123d subprocess produced no result (it may have crashed "
            "without a reportable exception)."
        )
    status, payload = q.get()
    if status == "ok":
        return payload
    raise RuntimeError(payload)
