"""Blender render plugin -- headless offline rendering (Phase D, ADR-0002 D7/R9).

Scope (D7): a still PNG of the current assembly state -- the per-template
glTF geometries placed at their manifest world matrices, framed by an
auto-placed camera, lit by a sun + neutral world.

Isolation (R9): Blender is invoked as an EXTERNAL dependency
(``blender --background --python <script>``) and never bundled -- GPL
contamination is avoided because we only call the executable. The main
service never imports bpy.

Probe & degrade (D7/D5 pattern, same as ODA in cad_drawing / FreeCAD in
cad_fea): env override (CAD_BLENDER_EXE) -> PATH -> well-known install
paths; missing dependency raises :class:`RenderError` (service -> 503 with
install hint, everything else keeps working).

Matrices: manifest node matrices are 3x4 row-major rows of the CAD world
transform (Z-up, mm) -- the same convention the frontend applies to
InstancedMesh (scene.js ``_matrixFromManifest``). The generated Blender
script therefore un-does the glTF Y-up correction Blender's importer
applies (OCCT writes CAD coordinates as-is) before composing the CAD world
matrix; ``correct_gltf_up_axis`` in the spec disables the correction.

Caching (R8/R17): results live in a content-keyed out_dir (sha256 of the
glTF contents + instance matrices + canonical spec); an existing
``result.json`` short-circuits Blender.

Long tasks (R5): synchronous with clamped timeout; job-id + progress +
cancel WebSocket protocol is a documented later increment.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess

DEFAULT_SPEC = {
    "engine": "cycles",        # cycles | workbench | eevee (headless-safe: cycles CPU)
    "samples": 64,             # cycles samples
    "width": 1600,
    "height": 1200,
    "transparent": False,      # film_transparent
    "azimuth_deg": 35.0,       # camera direction around the model
    "elevation_deg": 25.0,
    "distance_factor": 2.2,    # camera distance = bbox radius * factor
    "correct_gltf_up_axis": True,
    "timeout_s": 300.0,        # clamped to [30, 3600]
}

# --- probe paths (D5/D7 pattern) ----------------------------------------
_BLENDER_CANDIDATES = [
    # Windows
    r"C:\Program Files\Blender Foundation\Blender 4.5\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.4\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.1\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.0\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 3.6\blender.exe",
    # Linux
    "/usr/bin/blender",
    "/snap/bin/blender",
    "/opt/blender/blender",
    # macOS
    "/Applications/Blender.app/Contents/MacOS/Blender",
]


class RenderError(RuntimeError):
    """Missing Blender, timeout, or render failure (kind in
    {"missing", "timeout", "failure"})."""

    def __init__(self, message: str, kind: str = "failure",
                 missing: list | None = None):
        super().__init__(message)
        self.kind = kind
        self.missing = missing or []


def probe_blender() -> str | None:
    """Blender executable path, or None (env -> PATH -> candidates)."""
    env = os.environ.get("CAD_BLENDER_EXE")
    if env and os.path.isfile(env):
        return env
    w = shutil.which("blender")
    if w:
        return w
    for p in _BLENDER_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


HINT = ("渲染插件未启用：需要安装 Blender（作为外部依赖 headless 调用，"
        "不随系统分发）。安装后可用环境变量 CAD_BLENDER_EXE 指定路径。"
        "系统其余功能不受影响。")


def render_status() -> dict:
    b = probe_blender()
    return {
        "blender": b,
        "available": b is not None,
        "missing": [] if b else ["Blender"],
        "hint": None if b else HINT,
    }


# --------------------------------------------------------------------------
# Spec handling
# --------------------------------------------------------------------------

def normalize_spec(spec: dict | None) -> dict:
    """Merge with defaults, coerce, validate (boundary validation only)."""
    s = dict(DEFAULT_SPEC)
    s.update(spec or {})
    try:
        s["engine"] = str(s["engine"]).lower()
        if s["engine"] not in ("cycles", "workbench", "eevee"):
            raise ValueError(f"engine must be cycles/workbench/eevee, got {s['engine']}")
        s["samples"] = int(s["samples"])
        s["width"] = int(s["width"])
        s["height"] = int(s["height"])
        s["transparent"] = bool(s["transparent"])
        s["correct_gltf_up_axis"] = bool(s["correct_gltf_up_axis"])
        s["azimuth_deg"] = float(s["azimuth_deg"])
        s["elevation_deg"] = float(s["elevation_deg"])
        s["distance_factor"] = float(s["distance_factor"])
        s["timeout_s"] = min(max(float(s["timeout_s"]), 30.0), 3600.0)
        if not (16 <= s["samples"] <= 4096):
            raise ValueError("samples must be in [16, 4096]")
        if not (64 <= s["width"] <= 8192) or not (64 <= s["height"] <= 8192):
            raise ValueError("width/height must be in [64, 8192]")
        if not (0.5 <= s["distance_factor"] <= 20.0):
            raise ValueError("distance_factor must be in [0.5, 20]")
    except (TypeError, ValueError) as e:
        raise ValueError(f"bad render spec: {e}") from e
    return s


def _canonical_spec(spec: dict | None) -> str:
    return json.dumps(normalize_spec(spec), sort_keys=True)


def render_cache_key(entries: list, spec: dict | None) -> str:
    """sha256(gltf contents + instance matrices + canonical spec)[:16] (R8)."""
    h = hashlib.sha256()
    for e in entries:
        with open(e["gltf"], "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        h.update(json.dumps(e["instances"], sort_keys=True).encode("utf-8"))
    h.update(_canonical_spec(spec).encode("utf-8"))
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------
# Scene assembly (manifest -> render entries)
# --------------------------------------------------------------------------

_IDENTITY_3X4 = [[1.0, 0.0, 0.0, 0.0],
                 [0.0, 1.0, 0.0, 0.0],
                 [0.0, 0.0, 1.0, 0.0]]


def build_render_entries(manifest: dict, resolve_gltf) -> list:
    """Flatten the manifest into per-template render entries.

    ``resolve_gltf(tid) -> absolute gltf path | None`` decides which file
    each template renders from (service: version-resolved, else baseline
    cache). Returns ``[{gltf, name, instances}]`` where instances are the
    3x4 world matrices of every part node of that template. Raises
    RenderError when templates lack renderable geometry.
    """
    groups: dict[str, dict] = {}

    def walk(node):
        if node.get("type") == "part" and node.get("template"):
            g = groups.setdefault(node["template"], {"instances": []})
            g["instances"].append(node.get("matrix") or _IDENTITY_3X4)
        for ch in node.get("children", []):
            walk(ch)

    walk(manifest["root"])
    names = {t["id"]: t.get("name") for t in manifest.get("templates", [])}

    entries, missing = [], []
    for tid in sorted(groups):
        path = resolve_gltf(tid)
        if not path or not os.path.isfile(path):
            missing.append(f"{tid} ({names.get(tid) or '?'})")
            continue
        entries.append({
            "gltf": os.path.abspath(path),
            "name": names.get(tid) or tid,
            "instances": groups[tid]["instances"],
        })
    if missing:
        raise RenderError(
            "以下模板缺少可渲染的 glTF 几何：" + ", ".join(missing))
    if not entries:
        raise RenderError("装配体中没有可渲染的零件实例")
    return entries


# --------------------------------------------------------------------------
# Inner script (runs INSIDE Blender -- bpy is never imported here)
# --------------------------------------------------------------------------

_SCRIPT_TEMPLATE = r'''# Generated by cad_render.py -- run with:
#     blender --background --python <this file>
# Do not edit; regenerate with cad_render.build_blender_script().
import json
import math
import os
import time

PAYLOAD = json.loads(__PAYLOAD__)
ENTRIES = PAYLOAD["entries"]
SPEC = PAYLOAD["spec"]
PNG = PAYLOAD["png"]
RESULT = PAYLOAD["result"]

T0 = time.time()


def flush(ok, **extra):
    payload = {
        "schema_version": 1,
        "ok": bool(ok),
        "engine": None,
        "duration_s": round(time.time() - T0, 1),
        "width": SPEC["width"],
        "height": SPEC["height"],
        "objects": None,
        "png": PNG,
        "error": None,
    }
    payload.update(extra)
    with open(RESULT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


import bpy
from mathutils import Vector, Matrix

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


def fail(msg):
    flush(False, error=msg)
    print("[cad-render] FAILED: {}".format(msg))
    raise SystemExit(1)


# --- import templates + place instances ---------------------------------
meshes = []
for e in ENTRIES:
    before = set(scene.objects)
    try:
        bpy.ops.import_scene.gltf(filepath=e["gltf"])
    except Exception:
        try:
            bpy.ops.preferences.addon_enable(module="io_scene_gltf2")
            bpy.ops.import_scene.gltf(filepath=e["gltf"])
        except Exception as ex:
            fail("gltf import failed: {}: {}".format(e["gltf"], ex))
    new = [o for o in scene.objects if o not in before and o.type == "MESH"]
    if not new:
        fail("gltf contains no mesh objects: {}".format(e["gltf"]))

    if SPEC.get("correct_gltf_up_axis", True):
        # our glTF carries CAD Z-up coordinates; Blender's importer applied
        # the Y-up -> Z-up correction -- undo it before the CAD world matrix
        rx = Matrix.Rotation(-1.57079632679, 4, "X")
    else:
        rx = Matrix.Identity(4)

    def cad_matrix(rows):
        return Matrix((tuple(rows[0]), tuple(rows[1]), tuple(rows[2]),
                       (0.0, 0.0, 0.0, 1.0)))

    orig = {o.name: o.matrix_world.copy() for o in new}
    for i, rows in enumerate(e["instances"]):
        m = cad_matrix(rows) @ rx
        for o in new:
            if i == 0:
                o.matrix_world = m @ orig[o.name]
                meshes.append(o)
            else:
                dup = o.copy()  # shares mesh data (GPU-style instancing)
                dup.matrix_world = m @ orig[o.name]
                scene.collection.objects.link(dup)
                meshes.append(dup)

if not meshes:
    fail("no mesh objects were placed")

# --- camera / light / world ---------------------------------------------
mn = Vector((1e30, 1e30, 1e30))
mx = Vector((-1e30, -1e30, -1e30))
for o in meshes:
    for c in o.bound_box:
        w = o.matrix_world @ Vector(c)
        mn = Vector((min(mn.x, w.x), min(mn.y, w.y), min(mn.z, w.z)))
        mx = Vector((max(mx.x, w.x), max(mx.y, w.y), max(mx.z, w.z)))
center = (mn + mx) * 0.5
radius = max((mx - mn).length / 2.0, 1e-6)

az = math.radians(SPEC["azimuth_deg"])
el = math.radians(SPEC["elevation_deg"])
dist = radius * SPEC["distance_factor"]
cam_loc = center + Vector((math.cos(el) * math.cos(az),
                           math.cos(el) * math.sin(az),
                           math.sin(el))) * dist

cam_data = bpy.data.cameras.new("Cam")
cam = bpy.data.objects.new("Cam", cam_data)
scene.collection.objects.link(cam)
cam.location = cam_loc
cam_data.lens = 50

target = bpy.data.objects.new("Target", None)
target.location = center
scene.collection.objects.link(target)
track = cam.constraints.new(type="TRACK_TO")
track.target = target

sun_data = bpy.data.lights.new("Sun", type="SUN")
sun_data.energy = 3.0
sun = bpy.data.objects.new("Sun", sun_data)
scene.collection.objects.link(sun)
sun.location = center + Vector((0.5, -0.8, 1.2)) * radius * 2.0
sun_track = sun.constraints.new(type="TRACK_TO")
sun_track.target = target

world = bpy.data.worlds.new("World")
scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes.get("Background")
if bg:
    bg.inputs[0].default_value = (0.06, 0.06, 0.08, 1.0)

# --- engine (headless-safe CYCLES CPU default, with fallbacks) -----------
ENGINE_IDS = {
    "cycles": ("CYCLES",),
    "workbench": ("BLENDER_WORKBENCH",),
    "eevee": ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"),
}
engine_used = None
for eng_id in ENGINE_IDS.get(SPEC["engine"], ("CYCLES",)):
    try:
        scene.render.engine = eng_id
        engine_used = eng_id
        break
    except Exception:
        continue
if engine_used is None:
    scene.render.engine = "CYCLES"
    engine_used = "CYCLES"
if engine_used == "CYCLES":
    scene.cycles.device = "CPU"
    scene.cycles.samples = int(SPEC["samples"])

scene.render.resolution_x = int(SPEC["width"])
scene.render.resolution_y = int(SPEC["height"])
scene.render.film_transparent = bool(SPEC["transparent"])
scene.render.filepath = PNG
scene.render.image_settings.file_format = "PNG"

bpy.ops.render.render(write_still=True)
flush(True, engine=engine_used, objects=len(meshes))
print("[cad-render] done")
'''


def build_blender_script(payload: dict) -> str:
    """Render the inner Blender script (payload embedded as JSON)."""
    return _SCRIPT_TEMPLATE.replace(
        "__PAYLOAD__", json.dumps(json.dumps(payload, sort_keys=True)))


def build_command(blender_exe: str, script_path: str) -> list:
    """blender --background --python <script> (R9: external dependency)."""
    return [blender_exe, "--background", "--python", script_path]


def _invoke(blender_exe: str, script_path: str, timeout_s: float):
    """Run the Blender subprocess (seam for tests)."""
    return subprocess.run(
        build_command(blender_exe, script_path),
        capture_output=True, timeout=timeout_s)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def render_scene(entries: list, out_dir: str, spec: dict | None = None,
                 force: bool = False) -> dict:
    """Render the entries to out_dir/render.png, cached (R8/R17).

    Returns the result.json payload (+ "cache_hit"). Raises RenderError for
    missing Blender (kind="missing"), timeout (kind="timeout") and render
    failure (kind="failure").
    """
    status = render_status()
    if not status["available"]:
        raise RenderError(status["hint"] or "渲染插件不可用",
                          kind="missing", missing=status["missing"])

    spec_n = normalize_spec(spec)
    result_path = os.path.join(out_dir, "result.json")
    if not force and os.path.isfile(result_path):
        with open(result_path, encoding="utf-8") as f:
            result = json.load(f)
        result["cache_hit"] = True
        return result

    os.makedirs(out_dir, exist_ok=True)
    png = os.path.join(out_dir, "render.png")
    script_path = os.path.join(out_dir, "render_script.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(build_blender_script({
            "entries": entries,
            "spec": spec_n,
            "png": os.path.abspath(png),
            "result": os.path.abspath(result_path),
        }))

    try:
        _invoke(status["blender"], script_path, spec_n["timeout_s"])
    except subprocess.TimeoutExpired as e:
        raise RenderError(
            f"渲染超时（>{spec_n['timeout_s']:.0f}s），可通过 spec.timeout_s 上调；"
            f"任务已终止。", kind="timeout") from e
    except OSError as e:
        raise RenderError(f"无法启动 Blender 子进程：{e}", kind="failure") from e

    if not os.path.isfile(result_path):
        raise RenderError(
            "Blender 子进程未产出 result.json（渲染未完成或脚本异常退出）",
            kind="failure")
    with open(result_path, encoding="utf-8") as f:
        result = json.load(f)
    if result.get("ok") and not os.path.isfile(png):
        raise RenderError("渲染报告成功但缺少 render.png", kind="failure")
    result["cache_hit"] = False
    return result
