"""Local CAD Copilot service layer (Phase A, ADR-0002 D2/D3).

Starlette app exposing the tool library to the Web frontend over HTTP +
WebSocket. Built on starlette/uvicorn/websockets that already ship with
fastmcp -- zero new dependencies (轻依赖原则).

Layering (D2): this module is a THIN service layer -- geometry lives in
cad_assembly/cad_core (pure library). The MCP stdio server and this HTTP/WS
service are two transports over the same tool contract.

Security (ADR-0002 延续约束一):
  * binds 127.0.0.1 only (no LAN exposure);
  * token auth on every API route (CAD_SERVICE_TOKEN env, or auto-generated
    and printed at startup);
  * all user-supplied paths confined to CAD_SERVICE_ALLOWED_DIRS (same
    semantics as the MCP server's CAD_MCP_ALLOWED_DIRS);
  * static cache serving is path-traversal-proof (realpath prefix check);
  * no CORS middleware (same-origin frontend only).

Cache layout (R8/R17 idempotency): parse results live under
``<workspace>/cache/<sha256[:16]>/`` -- re-importing the same file content
hits the existing cache (cache_hit=true) instead of rebuilding; changed
content gets a new key automatically.

Concurrency (R4): OCCT geometry ops are not thread-safe, so every geometry
call is serialized on a global lock (same pattern as cad_core's stdout
suppression lock).

Run:
    venv/Scripts/python cad_service.py            # 127.0.0.1:8764
    CAD_SERVICE_TOKEN=... venv/bin/python cad_service.py
"""
from __future__ import annotations

import json
import os
import secrets
import threading

from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute

import cad_assembly
import cad_core
from cad_core import _SuppressStdout

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8764

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FRONTEND_DIR = os.path.join(_REPO_ROOT, "frontend", "dist")

# R4: OCCT is not thread-safe; serialize all geometry calls.
_GEOMETRY_LOCK = threading.Lock()

# Explicit MIME map for the SPA static server (Windows registry mimetypes
# can mislabel .js as text/plain -- CI runs on all 3 platforms).
_MIME = {
    ".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript",
    ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
    ".png": "image/png", ".ico": "image/x-icon", ".map": "application/json",
    ".wasm": "application/wasm",
}


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------

def create_app(token: str | None = None,
               allowed_dirs: list[str] | None = None,
               workspace: str | None = None,
               frontend_dir: str | None = None) -> Starlette:
    """Build the service app.

    Args (None -> read from env -> sensible default):
        token: bearer token guarding the API. Default: CAD_SERVICE_TOKEN env
            or a fresh random token (printed when run as __main__).
        allowed_dirs: dirs user-supplied input paths may live in.
            Default: CAD_SERVICE_ALLOWED_DIRS env or the cwd.
        workspace: root for cache output. Default: CAD_SERVICE_WORKSPACE
            env or ./workspace.
        frontend_dir: built SPA directory served at /app/. Default:
            CAD_SERVICE_FRONTEND_DIR env or ./frontend/dist. Missing dir ->
            /app returns 503 with a hint (run npm run build).
    """
    token = token or os.environ.get("CAD_SERVICE_TOKEN") or secrets.token_urlsafe(24)
    allowed_dirs = allowed_dirs or [
        os.path.abspath(p)
        for p in os.environ.get("CAD_SERVICE_ALLOWED_DIRS", ".").split(os.pathsep)
        if p
    ]
    workspace = os.path.abspath(
        workspace or os.environ.get("CAD_SERVICE_WORKSPACE", "workspace"))
    cache_root = os.path.join(workspace, "cache")
    os.makedirs(cache_root, exist_ok=True)
    frontend_dir = os.path.realpath(
        frontend_dir or os.environ.get("CAD_SERVICE_FRONTEND_DIR",
                                       DEFAULT_FRONTEND_DIR))

    def safe_input_path(p: str) -> str:
        """Resolve p and confine it to allowed_dirs (MCP _safe_path semantics)."""
        rp = os.path.realpath(p)
        if not any(rp == d or rp.startswith(d + os.sep) for d in allowed_dirs):
            raise PermissionError(f"path outside allowed dirs: {p}")
        return rp

    def safe_cache_path(rest: str) -> str:
        """Resolve a /cache/<rest> URL path inside cache_root, no traversal."""
        rp = os.path.realpath(os.path.join(cache_root, rest))
        if not (rp == cache_root or rp.startswith(cache_root + os.sep)):
            raise PermissionError("cache path escape")
        return rp

    versions_root = os.path.join(workspace, "versions")
    os.makedirs(versions_root, exist_ok=True)

    def safe_versions_path(rest: str) -> str:
        """Resolve a /versions/<rest> URL path inside versions_root."""
        rp = os.path.realpath(os.path.join(versions_root, rest))
        if not (rp == versions_root or rp.startswith(versions_root + os.sep)):
            raise PermissionError("versions path escape")
        return rp

    drawings_root = os.path.join(workspace, "drawings")
    os.makedirs(drawings_root, exist_ok=True)

    def safe_drawings_path(rest: str) -> str:
        rp = os.path.realpath(os.path.join(drawings_root, rest))
        if not (rp == drawings_root or rp.startswith(drawings_root + os.sep)):
            raise PermissionError("drawings path escape")
        return rp

    fea_root = os.path.join(workspace, "fea")
    os.makedirs(fea_root, exist_ok=True)

    def safe_fea_path(rest: str) -> str:
        rp = os.path.realpath(os.path.join(fea_root, rest))
        if not (rp == fea_root or rp.startswith(fea_root + os.sep)):
            raise PermissionError("fea path escape")
        return rp

    render_root = os.path.join(workspace, "render")
    os.makedirs(render_root, exist_ok=True)

    def safe_render_path(rest: str) -> str:
        rp = os.path.realpath(os.path.join(render_root, rest))
        if not (rp == render_root or rp.startswith(render_root + os.sep)):
            raise PermissionError("render path escape")
        return rp

    def check_auth(request) -> None:
        supplied = (request.headers.get("authorization", "")
                    .removeprefix("Bearer ").strip()
                    or request.headers.get("x-service-token", ""))
        if not secrets.compare_digest(supplied, token):
            raise PermissionError("invalid or missing token")

    # ----------------------------------------------------------------------
    # Handlers
    # ----------------------------------------------------------------------

    async def health(request):
        return JSONResponse({"status": "ok", "service": "cad-copilot",
                             "schema_version": cad_assembly.SCHEMA_VERSION})

    async def parse(request):
        try:
            check_auth(request)
            body = json.loads(await request.body() or b"{}")
            src = safe_input_path(str(body["input_path"]))
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)

        key = cad_assembly._sha256_file(src)[:16]
        cache_dir = os.path.join(cache_root, key)
        tree_json = os.path.join(cache_dir, "tree_structure.json")
        force = bool(body.get("force"))

        if not force and os.path.isfile(tree_json):
            # R8/R17: same source content -> reuse existing cache node.
            # R7: a manifest written by an older schema (e.g. v1 without
            # parts/*.step) must be rebuilt, not reused.
            manifest = cad_assembly.load_cache(cache_dir)
            if manifest.get("schema_version") == cad_assembly.SCHEMA_VERSION:
                return JSONResponse({"cache_key": key, "cache_hit": True,
                                     "base_url": f"/cache/{key}",
                                     "manifest": manifest})

        try:
            with _GEOMETRY_LOCK, _SuppressStdout():
                manifest = cad_assembly.build_cache(src, cache_dir)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)}, status_code=422)
        return JSONResponse({"cache_key": key, "cache_hit": False,
                             "base_url": f"/cache/{key}", "manifest": manifest})

    async def cache_file(request):
        try:
            fp = safe_cache_path(request.path_params["rest"])
        except PermissionError:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not os.path.isfile(fp):
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(fp)

    # ----------------------------------------------------------------------
    # Phase C: edit (interference-gated) + version management (D10)
    # ----------------------------------------------------------------------

    def _store(cache_key: str):
        import cad_versions
        store = cad_versions.VersionStore(versions_root, cache_key)
        store.cleanup_temp()   # R6: clear crashed-commit leftovers
        return store

    def _view_manifest(cache_key: str) -> dict:
        """Baseline manifest with version-resolved template gltf paths.

        Template gltf becomes an absolute /versions/... URL when a version
        modified it; otherwise the baseline cache-relative path stays.
        """
        manifest = cad_assembly.load_cache(os.path.join(cache_root, cache_key))
        store = _store(cache_key)
        for t in manifest["templates"]:
            vurl = store.resolve_gltf(t["id"])
            if vurl:
                t["gltf"] = vurl
        return manifest

    def _edit_context(cache_key: str):
        cache_dir = os.path.join(cache_root, cache_key)
        if not os.path.isfile(os.path.join(cache_dir, "tree_structure.json")):
            raise KeyError(f"unknown cache_key: {cache_key}")
        manifest = cad_assembly.load_cache(cache_dir)
        return cache_dir, manifest

    async def edit(request):
        """POST /api/assembly/edit -- the write path (③ 类流程):
        apply template edit -> interference gate -> atomic version commit.
        Nothing is committed when the gate rejects (R15: structured error)."""
        try:
            check_auth(request)
            body = json.loads(await request.body() or b"{}")
            cache_key = str(body["cache_key"])
            tid = str(body["template_id"])
            operation = str(body["operation"])
            params = body.get("params") or {}
            feature_id = body.get("feature_id")   # None = whole-template edit
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except (KeyError, ValueError, TypeError) as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)

        try:
            cache_dir, manifest = _edit_context(cache_key)
        except KeyError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

        tpl = next((t for t in manifest["templates"] if t["id"] == tid), None)
        if tpl is None:
            return JSONResponse(
                {"error": f"unknown template_id: {tid}"}, status_code=400)

        store = _store(cache_key)
        tmp_dir = os.path.join(versions_root, cache_key, "_prep")
        try:
            with _GEOMETRY_LOCK, _SuppressStdout():
                # resolve the CURRENT geometry of this template (version
                # chain first, baseline cache second)
                step_path = store.resolve_step(
                    tid, baseline_step=os.path.join(cache_dir, "parts", f"{tid}.step"))
                shape = cad_core.read_shape(step_path)

                if feature_id is not None:
                    # R1 targeted feature edit: locate the feature's own
                    # metadata and edit ONLY it
                    feats_json = os.path.join(cache_dir, "features", f"{tid}.json")
                    if not os.path.isfile(feats_json):
                        return JSONResponse(
                            {"ok": False, "error": f"no feature data for {tid}",
                             "stage": "validation"}, status_code=400)
                    with open(feats_json, encoding="utf-8") as f:
                        feats = json.load(f)
                    feat = next((x for x in feats if x.get("id") == feature_id), None)
                    if feat is None:
                        return JSONResponse(
                            {"ok": False,
                             "error": f"unknown feature_id: {feature_id}",
                             "stage": "validation"}, status_code=400)
                    old_r = max(feat.get("radii") or [0])
                    new_shape = cad_assembly.apply_feature_edit(
                        shape, feat, operation, params)
                    feature_desc = f"{feat.get('label')} {feature_id}"
                    if operation == "hole_resize":
                        changelog = (f"{tpl['name']} {feature_desc}: "
                                     f"R{old_r} -> R{params.get('radius')}")
                    else:
                        changelog = (f"{tpl['name']} {feature_desc}: {operation}")
                else:
                    new_shape = cad_assembly.apply_template_edit(shape, operation, params)
                    changelog = (f"{tpl['name']}: {operation} "
                                 + ", ".join(f"{k}={v}" for k, v in params.items()))

                # interference gate: edited template instances vs the rest
                all_shapes = cad_assembly.template_shapes_from_cache(cache_dir, manifest)
                all_shapes[tid] = new_shape
                hits = cad_assembly.check_interference(
                    manifest, all_shapes, edited_template=tid, edited_shape=new_shape)
                if hits:
                    # R15: structured rejection, nothing committed
                    return JSONResponse({
                        "ok": False, "error": "interference",
                        "stage": "interference_gate",
                        "interferences": hits,
                        "message": f"修改会导致 {len(hits)} 处物理干涉，已拒绝提交；"
                                   f"几何保持 {store.current} 不变。",
                        "version": store.current,
                    }, status_code=409)

                # prepare new version files in a temp dir (R6), then commit
                os.makedirs(tmp_dir, exist_ok=True)
                step_out = os.path.join(tmp_dir, f"{tid}.step")
                gltf_out = os.path.join(tmp_dir, f"{tid}.gltf")
                cad_core.write_shape(new_shape, step_out, overwrite=True)
                cad_assembly._export_template_gltf(
                    new_shape, tpl["name"], tpl.get("color"), gltf_out)
                record = store.commit(
                    {tid: {"step": step_out, "gltf": gltf_out}},
                    changelog=changelog, prepared_dir=tmp_dir)

                # refresh the edited template's feature cache with STABLE
                # ids (R1 fingerprint matching) so feature panels survive edits
                try:
                    cad_assembly.refresh_template_features(new_shape, cache_dir, tid)
                except Exception:  # noqa: BLE001 -- feature refresh best-effort
                    pass
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e),
                                 "stage": "validation"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e),
                                 "stage": "internal"}, status_code=422)
        finally:
            import shutil as _shutil
            _shutil.rmtree(tmp_dir, ignore_errors=True)

        return JSONResponse({
            "ok": True, "version": record["id"], "changelog": changelog,
            "parent": record["parent"],
            "manifest": _view_manifest(cache_key),
        })

    async def versions_list(request):
        try:
            check_auth(request)
            cache_key = str(request.query_params["cache_key"])
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except KeyError as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        try:
            _edit_context(cache_key)
            store = _store(cache_key)
        except KeyError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        m = store.manifest
        return JSONResponse({"cache_key": cache_key, "current": m["current"],
                             "versions": [{"id": v["id"], "parent": v["parent"],
                                           "created": v["created"],
                                           "changelog": v["changelog"],
                                           "changes": v["changes"]}
                                          for v in m["versions"]]})

    async def versions_checkout(request):
        """POST /api/versions/checkout -- pointer rollback (files never
        rewritten, D10)."""
        try:
            check_auth(request)
            body = json.loads(await request.body() or b"{}")
            cache_key = str(body["cache_key"])
            vid = str(body["version"])
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        try:
            _edit_context(cache_key)
            store = _store(cache_key)
            store.checkout(vid)
        except KeyError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse({"ok": True, "current": vid,
                             "manifest": _view_manifest(cache_key)})

    async def audit(request):
        """GET /api/assembly/audit -- 一键体检（模块七）：干涉 + DFM。"""
        try:
            check_auth(request)
            cache_key = str(request.query_params["cache_key"])
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except KeyError as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        try:
            cache_dir, manifest = _edit_context(cache_key)
        except KeyError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        try:
            with _GEOMETRY_LOCK, _SuppressStdout():
                report = cad_assembly.audit_assembly(cache_dir, manifest)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)}, status_code=422)
        report["cache_key"] = cache_key
        return JSONResponse(report)

    async def drawing_import(request):
        """POST /api/drawing/import -- D5: DXF native / DWG via ODA ->
        semantics + SVG cache (模块六 语义真理提取)."""
        try:
            check_auth(request)
            body = json.loads(await request.body() or b"{}")
            src = safe_input_path(str(body["input_path"]))
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)

        import hashlib
        sha = hashlib.sha256()
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                sha.update(chunk)
        key = sha.hexdigest()[:16]
        out_dir = os.path.join(drawings_root, key)

        import cad_drawing
        cached = os.path.isfile(os.path.join(out_dir, "drawing.json"))
        if not cached:
            try:
                with _GEOMETRY_LOCK, _SuppressStdout():
                    cad_drawing.import_drawing(src, out_dir)
            except cad_drawing.DrawingError as e:
                return JSONResponse({"error": str(e)}, status_code=422)
            except Exception as e:  # noqa: BLE001
                return JSONResponse({"error": str(e)}, status_code=422)
        with open(os.path.join(out_dir, "drawing.json"), encoding="utf-8") as f:
            result = json.load(f)
        result["cache_key"] = key
        result["cache_hit"] = cached
        result["base_url"] = f"/drawings/{key}"
        return JSONResponse(result)

    async def drawings_file(request):
        try:
            fp = safe_drawings_path(request.path_params["rest"])
        except PermissionError:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not os.path.isfile(fp):
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(
            fp, media_type="image/svg+xml" if fp.endswith(".svg")
            else "application/json")

    async def versions_file(request):
        try:
            fp = safe_versions_path(request.path_params["rest"])
        except PermissionError:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not os.path.isfile(fp):
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(fp)

    # ----------------------------------------------------------------------
    # Phase D optional plugins (ADR-0002 D7): probe + FEA + render
    # ----------------------------------------------------------------------

    async def plugins(request):
        """GET /api/plugins -- probe optional plugin dependencies (D5/D7):
        ODA converter, FreeCAD+CalculiX (FEA), Blender (render)."""
        try:
            check_auth(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401)
        import cad_drawing
        import cad_fea
        import cad_render
        oda = cad_drawing.probe_oda_converter()
        return JSONResponse({
            "oda": {"available": oda is not None, "path": oda},
            "fea": cad_fea.fea_status(),
            "blender": cad_render.render_status(),
        })

    async def fea_static(request):
        """POST /api/fea/static -- D7 FEA plugin: CalculiX static single
        scenario on a version-resolved template STEP (D6: FreeCAD runs as a
        headless subprocess). Sync + clamped timeout; the R5 job/progress
        protocol is a later increment."""
        try:
            check_auth(request)
            body = json.loads(await request.body() or b"{}")
            cache_key = str(body["cache_key"])
            tid = str(body["template_id"])
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        try:
            cache_dir, manifest = _edit_context(cache_key)
        except KeyError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        if next((t for t in manifest["templates"] if t["id"] == tid), None) is None:
            return JSONResponse(
                {"error": f"unknown template_id: {tid}"}, status_code=400)

        spec = body.get("spec") or {}
        import cad_fea
        try:
            store = _store(cache_key)
            step_path = store.resolve_step(
                tid, baseline_step=os.path.join(cache_dir, "parts", f"{tid}.step"))
            key = cad_fea.fea_cache_key(step_path, spec)
            out_dir = os.path.join(fea_root, key)
            result = cad_fea.run_static(step_path, out_dir, spec,
                                        force=bool(body.get("force")))
        except cad_fea.FEAError as e:
            code = {"missing": 503, "timeout": 504}.get(e.kind, 422)
            return JSONResponse({"ok": False, "error": str(e), "plugin": "fea",
                                 "kind": e.kind, "missing": e.missing},
                                status_code=code)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e),
                                 "stage": "validation"}, status_code=400)
        except FileNotFoundError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
        result["fea_key"] = key
        result["base_url"] = f"/fea/{key}"
        return JSONResponse(result)

    async def render(request):
        """POST /api/render -- D7 render plugin: Blender headless still of
        the version-resolved assembly state (R9: external dependency, never
        bundled). Sync + clamped timeout; R5 job/protocol later."""
        try:
            check_auth(request)
            body = json.loads(await request.body() or b"{}")
            cache_key = str(body["cache_key"])
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        try:
            cache_dir, manifest = _edit_context(cache_key)
        except KeyError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

        spec = body.get("spec") or {}
        import cad_render
        try:
            store = _store(cache_key)

            def resolve_gltf_abs(tid: str) -> str | None:
                vurl = store.resolve_gltf(tid)
                if vurl:
                    return os.path.join(workspace, vurl.lstrip("/"))
                return os.path.join(cache_dir, "gltf_library", f"{tid}.gltf")

            entries = cad_render.build_render_entries(manifest, resolve_gltf_abs)
            key = cad_render.render_cache_key(entries, spec)
            out_dir = os.path.join(render_root, key)
            result = cad_render.render_scene(entries, out_dir, spec,
                                             force=bool(body.get("force")))
        except cad_render.RenderError as e:
            code = {"missing": 503, "timeout": 504}.get(e.kind, 422)
            return JSONResponse({"ok": False, "error": str(e),
                                 "plugin": "render", "kind": e.kind,
                                 "missing": e.missing}, status_code=code)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e),
                                 "stage": "validation"}, status_code=400)
        result["render_key"] = key
        result["base_url"] = f"/render/{key}"
        result["png_url"] = f"/render/{key}/render.png"
        return JSONResponse(result)

    async def fea_file(request):
        try:
            fp = safe_fea_path(request.path_params["rest"])
        except PermissionError:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not os.path.isfile(fp):
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(fp)

    async def render_file(request):
        try:
            fp = safe_render_path(request.path_params["rest"])
        except PermissionError:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not os.path.isfile(fp):
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(
            fp, media_type=_MIME.get(os.path.splitext(fp)[1].lower()))

    async def ws(websocket):
        """Minimal JSON protocol skeleton (full job/progress protocol is a
        later increment per R5/R13): {action: "ping"} | {action: "parse",
        input_path}."""
        supplied = websocket.query_params.get("token", "")
        if not secrets.compare_digest(supplied, token):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            while True:
                msg = json.loads(await websocket.receive_text())
                action = msg.get("action")
                if action == "ping":
                    await websocket.send_json({"ok": True, "action": "ping"})
                elif action == "parse":
                    src = safe_input_path(str(msg["input_path"]))
                    key = cad_assembly._sha256_file(src)[:16]
                    cache_dir = os.path.join(cache_root, key)
                    with _GEOMETRY_LOCK, _SuppressStdout():
                        manifest = cad_assembly.build_cache(src, cache_dir)
                    await websocket.send_json({
                        "ok": True, "action": "parse",
                        "cache_key": key, "base_url": f"/cache/{key}",
                        "manifest": manifest})
                else:
                    await websocket.send_json(
                        {"ok": False, "error": f"unknown action: {action}"})
        except PermissionError as e:
            await websocket.send_json({"ok": False, "error": str(e)})
        except Exception:  # connection closed mid-message etc.
            pass

    # ----------------------------------------------------------------------
    # SPA static serving (/app) -- same origin as /api and /cache (no CORS)
    # ----------------------------------------------------------------------

    async def app_static(request):
        if not os.path.isdir(frontend_dir):
            return JSONResponse(
                {"error": "frontend not built; run `npm install && npm run build` "
                          "in frontend/ (dist/ is normally committed)"},
                status_code=503)
        rel = request.path_params.get("rest", "")
        fp = os.path.realpath(os.path.join(frontend_dir, rel))
        if fp != frontend_dir and not fp.startswith(frontend_dir + os.sep):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if os.path.isfile(fp):
            return FileResponse(
                fp, media_type=_MIME.get(os.path.splitext(fp)[1].lower()))
        # SPA fallback: unknown paths serve the app shell
        index = os.path.join(frontend_dir, "index.html")
        if os.path.isfile(index):
            return FileResponse(index, media_type="text/html")
        return JSONResponse({"error": "not found"}, status_code=404)

    # ----------------------------------------------------------------------
    # Routes
    # ----------------------------------------------------------------------

    async def not_found(request, exc):  # noqa: ANN001
        return JSONResponse({"error": "not found"}, status_code=404)

    app = Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/api/assembly/parse", parse, methods=["POST"]),
        Route("/api/assembly/edit", edit, methods=["POST"]),
        Route("/api/assembly/audit", audit, methods=["GET"]),
        Route("/api/drawing/import", drawing_import, methods=["POST"]),
        Route("/api/versions", versions_list, methods=["GET"]),
        Route("/api/versions/checkout", versions_checkout, methods=["POST"]),
        Route("/api/plugins", plugins, methods=["GET"]),
        Route("/api/fea/static", fea_static, methods=["POST"]),
        Route("/api/render", render, methods=["POST"]),
        Route("/cache/{rest:path}", cache_file, methods=["GET"]),
        Route("/versions/{rest:path}", versions_file, methods=["GET"]),
        Route("/drawings/{rest:path}", drawings_file, methods=["GET"]),
        Route("/fea/{rest:path}", fea_file, methods=["GET"]),
        Route("/render/{rest:path}", render_file, methods=["GET"]),
        Route("/app", app_static, methods=["GET"]),
        Route("/app/{rest:path}", app_static, methods=["GET"]),
        WebSocketRoute("/ws", ws),
    ], exception_handlers={404: not_found})
    app.state.token = token
    app.state.allowed_dirs = allowed_dirs
    app.state.workspace = workspace
    app.state.frontend_dir = frontend_dir
    return app


def main() -> None:
    import uvicorn
    host = os.environ.get("CAD_SERVICE_HOST", DEFAULT_HOST)
    port = int(os.environ.get("CAD_SERVICE_PORT", DEFAULT_PORT))
    app = create_app()
    print(f"[cad-service] listening on http://{host}:{port} "
          f"(token: {app.state.token})")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
