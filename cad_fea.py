"""FEA plugin -- CalculiX static single scenario (Phase D, ADR-0002 D6/D7/R5).

Scope (D7): 静力学单场景 -- fix the lowest face along an axis, press the
highest face, report max displacement / max von Mises. One solid at a time.

Isolation (D6): FreeCAD is NEVER imported into this process. We generate a
script and run it inside a FreeCAD console subprocess (``freecadcmd`` /
``FreeCAD --console``); CalculiX itself is driven by FreeCAD's FEM solver.

Probe & degrade (D7/D5 pattern, same as ODA in cad_drawing):
  * executables resolved from env overrides (CAD_FREECAD_EXE / CAD_CCX_EXE)
    -> PATH -> well-known install paths;
  * missing dependencies raise :class:`FEAError` with a structured
    ``missing`` list -- the service maps that to 503 with an install hint,
    the rest of the system keeps working.

Caching (R8/R17): results live in a content-keyed out_dir supplied by the
caller (sha256 of the STEP content + canonical spec). An existing
``result.json`` short-circuits the subprocess (``cache_hit=True``).

Long tasks (R5): ``run_static`` accepts ``progress`` / ``should_cancel``
hooks -- the service layer wires them into cad_jobs.JobManager so the
frontend gets a job id + phase progress (relayed from the inner script's
result.json flushes) + cooperative cancel (Popen terminate, 5s grace then
kill). The subprocess is also clamped by spec.timeout_s.

NOTE -- the INNER script (the part that runs inside FreeCAD) follows the
documented FreeCAD FEM scripting API but is exercised only once FreeCAD is
installed; it reports per-phase progress into result.json so the first
validation run pinpoints any version-specific API drift. Everything around
it (probe, command build, orchestration, cache, degradation) is unit-tested
without FreeCAD.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time

DEFAULT_SPEC = {
    "axis": "z",                # restraint/press direction
    "force_N": 1000.0,          # total force on the loaded face
    "young_modulus_MPa": 210000.0,   # steel
    "poisson_ratio": 0.3,
    "density_kg_m3": 7850.0,
    "max_element_size_mm": None,     # None = solver default mesh size
    "timeout_s": 600.0,         # subprocess timeout, clamped to [30, 3600]
}

# --- probe paths (D5/D7 pattern) ----------------------------------------
_FREECAD_CANDIDATES = [
    # Windows (console binary preferred over the GUI exe, D6)
    r"C:\Program Files\FreeCAD 1.1\bin\FreeCADcmd.exe",
    r"C:\Program Files\FreeCAD 1.0\bin\FreeCADcmd.exe",
    r"C:\Program Files\FreeCAD 0.21\bin\FreeCADcmd.exe",
    r"C:\Program Files\FreeCAD 0.20.2\bin\FreeCADcmd.exe",
    r"C:\Program Files\FreeCAD 0.20\bin\FreeCADcmd.exe",
    r"C:\Program Files\FreeCAD 1.0\bin\FreeCAD.exe",
    r"C:\Program Files\FreeCAD 0.21\bin\FreeCAD.exe",
    # Linux
    "/usr/bin/freecadcmd",
    "/usr/bin/FreeCADcmd",
    "/usr/bin/freecad",
    "/opt/freecad/bin/freecadcmd",
    "/snap/bin/freecadcmd",
    # macOS
    "/Applications/FreeCAD.app/Contents/MacOS/freecadcmd",
]

_CCX_CANDIDATES = [
    r"C:\Program Files\CalculiX\ccx.exe",
    r"C:\Program Files\CalculiX\bin\ccx.exe",
    "/usr/bin/ccx",
    "/usr/local/bin/ccx",
]


class FEAError(RuntimeError):
    """Missing plugin dependency, timeout, or solver failure.

    ``kind`` in {"missing", "timeout", "failure"}; ``missing`` lists the
    human names of the absent dependencies (service -> 503 + hint).
    """

    def __init__(self, message: str, kind: str = "failure",
                 missing: list | None = None):
        super().__init__(message)
        self.kind = kind
        self.missing = missing or []


# --------------------------------------------------------------------------
# Probe (D7: 可选插件 -- 探测 + 优雅降级)
# --------------------------------------------------------------------------

def probe_freecad() -> str | None:
    """FreeCAD console executable path, or None (env -> PATH -> candidates)."""
    env = os.environ.get("CAD_FREECAD_EXE")
    if env and os.path.isfile(env):
        return env
    for name in ("freecadcmd", "FreeCADcmd", "freecad", "FreeCAD"):
        w = shutil.which(name)
        if w:
            return w
    for p in _FREECAD_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def probe_calculix(freecad_exe: str | None = None) -> str | None:
    """CalculiX (ccx) solver path, or None.

    FreeCAD bundles ccx next to its own binaries, so a sibling lookup of the
    probed FreeCAD executable is checked before the generic candidates.
    """
    env = os.environ.get("CAD_CCX_EXE")
    if env and os.path.isfile(env):
        return env
    w = shutil.which("ccx")
    if w:
        return w
    if freecad_exe:
        d = os.path.dirname(freecad_exe)
        for name in ("ccx.exe", "ccx"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    for p in _CCX_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


HINT = ("FEA 插件未启用：需要 FreeCAD（含 FEM 工作台，headless 子进程调用）"
        "和 CalculiX (ccx) 求解器。安装后可用环境变量 CAD_FREECAD_EXE / "
        "CAD_CCX_EXE 指定路径。系统其余功能不受影响。")


def fea_status() -> dict:
    """Probe both dependencies once (for /api/plugins)."""
    fc = probe_freecad()
    ccx = probe_calculix(fc)
    missing = []
    if fc is None:
        missing.append("FreeCAD")
    if ccx is None:
        missing.append("CalculiX (ccx)")
    return {
        "freecad": fc,
        "calculix": ccx,
        "available": not missing,
        "missing": missing,
        "hint": None if not missing else HINT,
    }


# --------------------------------------------------------------------------
# Spec handling
# --------------------------------------------------------------------------

def normalize_spec(spec: dict | None) -> dict:
    """Merge with defaults, coerce numbers, validate (boudary validation only)."""
    s = dict(DEFAULT_SPEC)
    s.update(spec or {})
    try:
        s["axis"] = str(s["axis"]).lower()
        if s["axis"] not in ("x", "y", "z"):
            raise ValueError(f"axis must be x/y/z, got {s['axis']}")
        s["force_N"] = float(s["force_N"])
        s["young_modulus_MPa"] = float(s["young_modulus_MPa"])
        s["poisson_ratio"] = float(s["poisson_ratio"])
        s["density_kg_m3"] = float(s["density_kg_m3"])
        s["timeout_s"] = min(max(float(s["timeout_s"]), 30.0), 3600.0)
        if s["force_N"] <= 0 or s["young_modulus_MPa"] <= 0 or s["density_kg_m3"] <= 0:
            raise ValueError("force/young modulus/density must be positive")
        if not 0.0 <= s["poisson_ratio"] < 0.5:
            raise ValueError("poisson ratio must be in [0, 0.5)")
        if s["max_element_size_mm"] is not None:
            s["max_element_size_mm"] = float(s["max_element_size_mm"])
            if s["max_element_size_mm"] <= 0:
                raise ValueError("max_element_size_mm must be positive")
    except (TypeError, ValueError) as e:
        raise ValueError(f"bad FEA spec: {e}") from e
    return s


def _canonical_spec(spec: dict | None) -> str:
    return json.dumps(normalize_spec(spec), sort_keys=True)


def fea_cache_key(step_path: str, spec: dict | None) -> str:
    """sha256(STEP content + canonical spec)[:16] (R8: content-keyed)."""
    h = hashlib.sha256()
    with open(step_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    h.update(_canonical_spec(spec).encode("utf-8"))
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------
# Inner script (runs INSIDE FreeCAD -- see module NOTE)
# --------------------------------------------------------------------------

_SCRIPT_TEMPLATE = r'''# Generated by cad_fea.py -- run inside the FreeCAD console:
#     freecadcmd <this file>
# Do not edit; regenerate with cad_fea.build_freecad_script().
import json
import os
import sys
import time

OUT_DIR = json.loads(__OUT_DIR__)
STEP_PATH = json.loads(__STEP_PATH__)
SPEC = json.loads(__SPEC__)
CCX = json.loads(__CCX__)

T0 = time.time()
PHASES = {}
RESULT = os.path.join(OUT_DIR, "result.json")


def flush(**extra):
    payload = {
        "schema_version": 1,
        "ok": False,
        "phases": PHASES,
        "duration_s": round(time.time() - T0, 1),
        "axis": SPEC["axis"],
        "force_N": SPEC["force_N"],
        "max_displacement_mm": None,
        "max_von_mises_MPa": None,
        "mesh_nodes": None,
        "mesh_elements": None,
        "error": None,
    }
    payload.update(extra)
    with open(RESULT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def phase(name, ok, detail=None):
    PHASES[name] = {"ok": bool(ok),
                    "detail": None if detail is None else str(detail)[:400]}
    flush()


def fail(stage, exc):
    phase(stage, False, exc)
    flush(error="{}: {}".format(stage, exc))
    print("[cad-fea] FAILED at {}: {}".format(stage, exc))
    sys.exit(1)


# --- phase: interpreter -------------------------------------------------
try:
    import FreeCAD as App
    import Part
    import Fem
    phase("interpreter", True, ".".join(map(str, App.Version())))
except Exception as e:
    fail("interpreter", e)

doc = App.newDocument("fea_job")

# --- phase: geometry ----------------------------------------------------
try:
    shape = Part.Shape()
    shape.read(STEP_PATH)
    solid = doc.addObject("Part::Feature", "Solid")
    solid.Shape = shape
    doc.recompute()
    phase("geometry", True,
          "solids={} faces={}".format(len(shape.Solids), len(shape.Faces)))
except Exception as e:
    fail("geometry", e)

# --- phase: face selection (deterministic single scenario) --------------
axis = SPEC.get("axis", "z")


def extreme_face(sign):
    best_v, best_i = None, None
    for i, f in enumerate(shape.Faces):
        v = getattr(f.CenterOfMass, axis)
        if best_v is None or (v < best_v if sign < 0 else v > best_v):
            best_v, best_i = v, i
    return best_i + 1  # FreeCAD face names are 1-based


try:
    FIXED_FACE = extreme_face(-1)
    LOAD_FACE = extreme_face(+1)
    phase("faces", True, "fixed=Face{} loaded=Face{} axis={}".format(
        FIXED_FACE, LOAD_FACE, axis))
except Exception as e:
    fail("faces", e)

# --- phase: FEM setup (analysis / solver / material / constraints) -------
try:
    analysis = doc.addObject("Fem::FemAnalysis", "Analysis")
    solver = doc.addObject("Fem::SolverCalculix", "CalculiX")
    analysis.addObject(solver)
    if CCX:
        try:
            solver.Binary = CCX  # help FreeCAD locate the ccx binary
        except Exception:
            pass

    mat = doc.addObject("App::MaterialObjectPython", "MechanicalMaterial")
    try:
        from femobjects.material import Material
        mat.Proxy = Material(mat)
    except Exception:
        pass  # older FreeCAD builds tolerate a missing proxy
    mat.Category = "Solid"
    mat.Material = {
        "Name": "Steel",
        "YoungsModulus": "{} MPa".format(SPEC["young_modulus_MPa"]),
        "PoissonRatio": str(SPEC["poisson_ratio"]),
        "Density": "{} kg/m^3".format(SPEC["density_kg_m3"]),
    }
    analysis.addObject(mat)

    fixed = doc.addObject("Fem::ConstraintFixed", "FemFixed")
    fixed.References = [(solid, "Face{}".format(FIXED_FACE))]
    analysis.addObject(fixed)

    force = doc.addObject("Fem::ConstraintForce", "FemForce")
    force.References = [(solid, "Face{}".format(LOAD_FACE))]
    force.Force = float(SPEC["force_N"])
    force.Reversed = True  # press against the face normal (compression)
    analysis.addObject(force)
    phase("setup", True)
except Exception as e:
    fail("setup", e)

# --- phase: mesh (gmsh, bundled with FreeCAD) ----------------------------
try:
    mesh = doc.addObject("Fem::FemMeshShapeGmsh", "FEMMeshGmsh")
    mesh.Shape = solid
    analysis.addObject(mesh)
    if SPEC.get("max_element_size_mm"):
        try:
            mesh.CharacteristicLength = float(SPEC["max_element_size_mm"])
        except Exception:
            pass
    doc.recompute()
    fm = mesh.FemMesh
    phase("mesh", True, "nodes={} volumes={}".format(
        fm.NodeCount, fm.VolumeCount))
    flush(mesh_nodes=fm.NodeCount, mesh_elements=fm.VolumeCount)
except Exception as e:
    fail("mesh", e)

# --- phase: solve (CalculiX via FreeCAD's femsolver) ---------------------
try:
    solver.WorkingDir = OUT_DIR
    doc.recompute()
    solver.Run()
    phase("solve", True)
except Exception as e:
    fail("solve", e)

# --- phase: post (read .frd results; best effort, validated on install) --
max_disp = None
max_vm = None
try:
    import glob
    frds = sorted(glob.glob(os.path.join(OUT_DIR, "*.frd")))
    if frds:
        Fem.open(frds[0])
        for o in doc.Objects:
            if "Fem::FemResultObject" not in str(getattr(o, "TypeId", "")):
                continue
            try:
                if max_disp is None and o.DisplacementLengths:
                    max_disp = max(o.DisplacementLengths)
            except Exception:
                pass
            for attr in ("vonMisesStresses", "vonMises", "StressValues"):
                vals = getattr(o, attr, None)
                if vals:
                    max_vm = max(vals)
                    break
    phase("post", True,
          "frd={}".format(os.path.basename(frds[0]) if frds else "none"))
except Exception as e:
    phase("post", False, e)

flush(ok=True, max_displacement_mm=max_disp, max_von_mises_MPa=max_vm)
print("[cad-fea] done")
'''


def build_freecad_script(step_path: str, out_dir: str, spec: dict,
                         ccx: str | None) -> str:
    """Render the inner FreeCAD script (paths/spec embedded as JSON)."""
    s = _SCRIPT_TEMPLATE
    for token, value in (
            ("__OUT_DIR__", out_dir),
            ("__STEP_PATH__", step_path),
            ("__SPEC__", spec),
            ("__CCX__", ccx),
    ):
        # double dumps: a valid Python string literal holding valid JSON
        s = s.replace(token, json.dumps(json.dumps(value, sort_keys=True)))
    return s


def build_command(freecad_exe: str, script_path: str) -> list:
    """freecadcmd <script>; the GUI binary needs --console (D6 headless)."""
    if os.path.basename(freecad_exe).lower() in ("freecad.exe", "freecad"):
        return [freecad_exe, "--console", script_path]
    return [freecad_exe, script_path]


_POLL_INTERVAL_S = 0.2
_CANCEL_GRACE_S = 5.0

# inner-script phase -> percent on the job progress bar (R5). The inner
# script rewrites result.json at every phase flush, so watching the file
# gives REAL progress straight from inside FreeCAD.
_PHASE_PERCENT = {"interpreter": 10, "geometry": 20, "faces": 30,
                  "setup": 40, "mesh": 55, "solve": 75, "post": 95}


def _invoke(freecad_exe: str, script_path: str, timeout_s: float,
            should_cancel=None, progress=None, result_path: str | None = None):
    """Run the FreeCAD subprocess (seam for tests; D6: out of process).

    Popen + polling so the caller can CANCEL cooperatively (R5): the loop
    checks ``should_cancel()`` every poll, terminates the process and raises
    FEAError(kind="cancelled"). While the solver runs, phase updates the
    inner script writes into result.json are relayed through ``progress``.
    stdout/stderr go to DEVNULL (result travels via result.json; no pipe
    deadlock when CalculiX gets chatty).
    """
    proc = subprocess.Popen(
        build_command(freecad_exe, script_path),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    start = time.monotonic()
    reported: set[str] = set()
    try:
        while True:
            if proc.poll() is not None:
                return proc.returncode
            if should_cancel is not None and should_cancel():
                _kill(proc)
                raise FEAError("FEA 求解已取消", kind="cancelled")
            if time.monotonic() - start > timeout_s:
                _kill(proc)
                raise FEAError(
                    f"FEA 求解超时（>{timeout_s:.0f}s），可通过 spec.timeout_s "
                    f"上调；任务已终止。", kind="timeout")
            if progress is not None and result_path and \
                    os.path.isfile(result_path):
                _relay_phases(result_path, reported, progress)
            time.sleep(_POLL_INTERVAL_S)
    finally:
        if proc.poll() is None:  # e.g. KeyboardInterrupt on the service
            _kill(proc)


def _kill(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=_CANCEL_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _relay_phases(result_path: str, reported: set, progress) -> None:
    """Forward not-yet-reported inner-script phases to the progress bar."""
    try:
        with open(result_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return  # half-written file mid-flush; next poll retries
    for phase, ok in (data.get("phases") or {}).items():
        if phase not in reported:
            reported.add(phase)
            detail = "失败: " + str(ok.get("detail")) if not ok.get("ok") \
                else ok.get("detail")
            progress(phase, _PHASE_PERCENT.get(phase), detail)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run_static(step_path: str, out_dir: str, spec: dict | None = None,
               force: bool = False, progress=None,
               should_cancel=None) -> dict:
    """Run the single-scenario static analysis, cached under out_dir.

    Returns the result.json payload (+ "cache_hit"). Raises FEAError for
    missing dependencies (kind="missing"), timeout (kind="timeout"),
    user cancellation (kind="cancelled") and solver failure
    (kind="failure").

    R5 hooks: ``progress(phase, percent, detail)`` receives orchestration
    phases plus the inner-script phases relayed from result.json;
    ``should_cancel()`` is polled while the subprocess runs.
    """
    if not os.path.isfile(step_path):
        raise FileNotFoundError(f"no such STEP file: {step_path}")

    if progress:
        progress("probe", 5, "探测 FreeCAD / CalculiX")
    status = fea_status()
    if not status["available"]:
        raise FEAError(status["hint"] or "FEA 插件不可用",
                       kind="missing", missing=status["missing"])

    spec_n = normalize_spec(spec)
    result_path = os.path.join(out_dir, "result.json")
    if not force and os.path.isfile(result_path):
        with open(result_path, encoding="utf-8") as f:
            result = json.load(f)
        result["cache_hit"] = True
        if progress:
            progress("cache", 100, "缓存命中")
        return result

    os.makedirs(out_dir, exist_ok=True)
    if progress:
        progress("prepare", 8, "生成 FreeCAD 求解脚本")
    script_path = os.path.join(out_dir, "fea_script.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(build_freecad_script(
            os.path.abspath(step_path), os.path.abspath(out_dir),
            spec_n, status["calculix"]))

    if progress:
        progress("solve", 12, "FreeCAD 子进程启动")
    try:
        _invoke(status["freecad"], script_path, spec_n["timeout_s"],
                should_cancel=should_cancel, progress=progress,
                result_path=result_path)
    except subprocess.TimeoutExpired as e:   # stubbed _invoke (tests)
        raise FEAError(
            f"FEA 求解超时（>{spec_n['timeout_s']:.0f}s），可通过 spec.timeout_s "
            f"上调；任务已终止。", kind="timeout") from e
    except OSError as e:
        raise FEAError(f"无法启动 FreeCAD 子进程：{e}", kind="failure") from e

    if not os.path.isfile(result_path):
        raise FEAError(
            "FreeCAD 子进程未产出 result.json（求解未完成或脚本异常退出）",
            kind="failure")
    with open(result_path, encoding="utf-8") as f:
        result = json.load(f)
    result["cache_hit"] = False
    return result
