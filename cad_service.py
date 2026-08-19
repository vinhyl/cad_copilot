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
            manifest = cad_assembly.load_cache(cache_dir)
            return JSONResponse({"cache_key": key, "cache_hit": True,
                                 "base_url": f"/cache/{key}", "manifest": manifest})

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
        Route("/cache/{rest:path}", cache_file, methods=["GET"]),
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
