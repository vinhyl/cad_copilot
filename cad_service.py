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

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute

import cad_assembly
import cad_core
import cad_jobs
from cad_core import _SuppressStdout

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8764

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FRONTEND_DIR = os.path.join(_REPO_ROOT, "frontend", "dist")

log = logging.getLogger("cad")

# R4: OCCT is not thread-safe; serialize all geometry calls.
_GEOMETRY_LOCK = threading.Lock()

# 前端上报错误的环形缓冲（GET /api/logs/client 可查最近条目）
_CLIENT_ERRORS: deque = deque(maxlen=200)


def _setup_logging(workspace: str) -> None:
    """结构化日志：stdout + workspace/logs/service.log（2MB×3 轮转）。

    幂等：重复 create_app（测试）不会叠加 handler。
    """
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    try:
        logs_dir = os.path.join(workspace, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(logs_dir, "service.log"),
                                 maxBytes=2_000_000, backupCount=3,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass   # 日志目录不可写时仅 stdout


class _LoggingMiddleware:
    """ASGI 中间件：API 请求耗时/状态日志 + 未捕获异常兜底（500 + 堆栈）。

    此前端点里抛出的未处理异常会直接断开连接（前端只见 network error，
    无任何服务端痕迹），这是「页面加载不了但不知为何」的主要盲区。
    """

    def __init__(self, app):  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        t0 = time.monotonic()
        state = {"status": 0}

        async def send_wrap(message):  # noqa: ANN001
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrap)
        except Exception:  # noqa: BLE001
            log.exception("unhandled error: %s %s",
                          scope.get("method"), scope.get("path"))
            resp = JSONResponse({"error": "internal server error"},
                                status_code=500)
            try:
                await resp(scope, receive, send)
            except Exception:  # noqa: BLE001
                pass   # 连接已断，无法回写
            return
        dur_ms = (time.monotonic() - t0) * 1000
        path = scope.get("path", "")
        # 静态资源不记（噪音）；API 全记，慢请求（>1s）加标记
        if path.startswith("/api/"):
            slow = " SLOW" if dur_ms > 1000 else ""
            log.info("%s %s -> %s %.0fms%s",
                     scope.get("method"), path, state["status"], dur_ms, slow)

# Explicit MIME map for the SPA static server (Windows registry mimetypes
# can mislabel .js as text/plain -- CI runs on all 3 platforms).
_MIME = {
    ".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript",
    ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
    ".png": "image/png", ".ico": "image/x-icon", ".map": "application/json",
    ".wasm": "application/wasm",
}


class _UploadTooLarge(Exception):
    """Internal: upload body exceeded _MAX_UPLOAD_BYTES."""


def _silent_remove(path: str) -> None:
    """Best-effort delete; missing file or locked file is not an error."""
    try:
        os.remove(path)
    except OSError:
        pass


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
            Default: CAD_SERVICE_ALLOWED_DIRS env or the cwd. The
            workspace/uploads dir is always appended (upload landing zone).
        workspace: root for cache output. Default: CAD_SERVICE_WORKSPACE
            env or ./workspace.
        frontend_dir: built SPA directory served at /app/. Default:
            CAD_SERVICE_FRONTEND_DIR env or ./frontend/dist. Missing dir ->
            /app returns 503 with a hint (run npm run build).
    """
    # 固定默认 token，保证任何启动方式（含直接 `python cad_service.py`）产出的
    # token 都与文档/agent 使用的 `cad-local-dev-2026` 一致，避免 LLM 用随机 token
    # 拼 URL 导致鉴权失败。可用 CAD_SERVICE_TOKEN 环境变量覆盖。
    token = token or os.environ.get("CAD_SERVICE_TOKEN") or "cad-local-dev-2026"
    allowed_dirs = allowed_dirs or [
        os.path.abspath(p)
        for p in os.environ.get("CAD_SERVICE_ALLOWED_DIRS", ".").split(os.pathsep)
        if p
    ]
    workspace = os.path.realpath(
        workspace or os.environ.get("CAD_SERVICE_WORKSPACE", "workspace"))
    _setup_logging(workspace)
    # realpath：workspace 配置在带软链的路径下（如 macOS 的 /var/...）时，
    # safe_*_path 的 realpath 前缀检查才不会误判为路径逃逸
    cache_root = os.path.join(workspace, "cache")
    os.makedirs(cache_root, exist_ok=True)
    # Uploads (explicit-grant input channel): user hands a specific file to
    # the service, so the uploads dir is appended to allowed_dirs. Uploads
    # are content-addressed (uploads/<sha256>/<name>) -- identical re-uploads
    # dedupe to the same path; different content never shares a dir (ODA
    # converts whole src dirs).
    uploads_root = os.path.join(workspace, "uploads")
    os.makedirs(uploads_root, exist_ok=True)
    allowed_dirs = allowed_dirs + [uploads_root]
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

    # M2: 草稿单槽位存储（声明式步骤表，每 cacheKey 一份）
    drafts_root = os.path.join(workspace, "drafts")
    os.makedirs(drafts_root, exist_ok=True)

    def _draft_path(cache_key: str) -> str:
        # 同 cacheKey 一份，覆盖式保存（单槽位约定）
        return os.path.join(drafts_root, f"{cache_key}.json")

    def _draft_preview_dir(cache_key: str) -> str:
        """草稿预览 gltf 落盘目录（重放产物，不进版本链）。"""
        d = os.path.join(drafts_root, cache_key, "preview")
        os.makedirs(d, exist_ok=True)
        return d

    def safe_drafts_path(rest: str) -> str:
        rp = os.path.realpath(os.path.join(drafts_root, rest))
        if not (rp == drafts_root or rp.startswith(drafts_root + os.sep)):
            raise PermissionError("drafts path escape")
        return rp

    fea_root = os.path.join(workspace, "fea")
    os.makedirs(fea_root, exist_ok=True)

    # M6: 用户选中上行存储（每 cacheKey 一份，last-click-wins；agent 经
    # MCP get_user_selection 读取以消解对话中的"这个"）
    selection_root = os.path.join(workspace, "selection")
    os.makedirs(selection_root, exist_ok=True)

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

    # M4: 报告中心存储（每个 cacheKey 一个目录，报告为时间戳 JSON 快照）
    reports_root = os.path.join(workspace, "reports")
    os.makedirs(reports_root, exist_ok=True)

    def safe_reports_path(rest: str) -> str:
        rp = os.path.realpath(os.path.join(reports_root, rest))
        if not (rp == reports_root or rp.startswith(reports_root + os.sep)):
            raise PermissionError("reports path escape")
        return rp

    def _report_dir(cache_key: str) -> str:
        d = os.path.join(reports_root, cache_key)
        os.makedirs(d, exist_ok=True)
        return d

    # R5: long-task jobs (FEA solve / Blender render) -- job id + progress
    # + cooperative cancel, in-memory (ephemeral by design).
    jobs_mgr = cad_jobs.JobManager()

    def _ctx_progress(ctx):
        return None if ctx is None else ctx.report

    def _ctx_cancel(ctx):
        return None if ctx is None else ctx.should_cancel

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

    async def config(request):
        """UI guidance: which dirs user-supplied input paths may live in."""
        try:
            check_auth(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401)
        return JSONResponse({"allowed_dirs": allowed_dirs})

    _UPLOAD_EXTS = {".step", ".stp", ".dxf", ".dwg"}
    _MAX_UPLOAD_BYTES = 1 << 30  # 1 GiB

    async def upload(request):
        """Explicit-grant input channel: raw body -> workspace/uploads.

        Content-addressed (mirrors the parse cache): stream to a temp file
        while hashing, then land at ``uploads/<sha256>/<name>``. Re-uploading
        identical content+name returns the SAME path (deduplicated=true) --
        no new dir, and the frontend recent-list path dedup kicks in.
        Filename comes from the ?name= query param (URL-decoded); it is
        stripped to a basename and sanitized. Different content gets a
        different hash dir (ODA converts whole source dirs, so unrelated DWGs
        never share one). Returns {"path"} that existing parse/import
        endpoints accept.
        """
        try:
            check_auth(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401)
        raw = request.query_params.get("name", "")
        base = os.path.basename(raw.replace("\\", "/"))
        ext = os.path.splitext(base)[1].lower()
        if ext not in _UPLOAD_EXTS:
            return JSONResponse(
                {"error": f"不支持的文件类型 {ext or '(无扩展名)'}："
                          "仅支持 STEP / DXF / DWG"}, status_code=400)
        safe = "".join(c for c in base if c not in '<>:"/\\|?*\x00-\x1f').strip()
        if not safe:
            safe = "upload" + ext
        tmp_dir = os.path.join(uploads_root, ".tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp = os.path.join(tmp_dir, secrets.token_hex(8) + ext)
        h = hashlib.sha256()
        size = 0
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > _MAX_UPLOAD_BYTES:
                        raise _UploadTooLarge
                    h.update(chunk)
                    f.write(chunk)
        except _UploadTooLarge:
            _silent_remove(tmp)
            return JSONResponse({"error": "文件超过 1 GiB 上限"},
                                status_code=413)
        except OSError as e:
            _silent_remove(tmp)
            return JSONResponse({"error": f"写入失败: {e}"}, status_code=500)
        dest_dir = os.path.join(uploads_root, h.hexdigest())
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, safe)
        deduplicated = os.path.isfile(dest)
        if deduplicated:
            _silent_remove(tmp)          # identical bytes already on disk
        else:
            os.replace(tmp, dest)        # atomic move into place
        return JSONResponse({"path": dest, "name": safe, "size": size,
                             "deduplicated": deduplicated})

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
                # D10: 反映当前版本指针——落过版本/切过版本后，重进首页
                # 也要看到版本解析后的模板几何（否则只显示基线）
                try:
                    manifest = _view_manifest(key)
                except Exception:  # noqa: BLE001
                    pass   # 无版本存储等异常时退回基线 manifest
                return JSONResponse({"cache_key": key, "cache_hit": True,
                                     "base_url": f"/cache/{key}",
                                     "manifest": manifest})

        try:
            # OCP 导入是重活：下放线程池，避免阻塞事件循环（否则解析
            # 期间整个服务——含静态文件与 WS——全部无响应）
            def work():
                with _GEOMETRY_LOCK, _SuppressStdout():
                    return cad_assembly.build_cache(src, cache_dir)
            manifest = await run_in_threadpool(work)
        except Exception as e:  # noqa: BLE001
            log.exception("parse failed: %s", src)
            return JSONResponse({"error": str(e)}, status_code=422)
        return JSONResponse({"cache_key": key, "cache_hit": False,
                             "base_url": f"/cache/{key}", "manifest": manifest})

    async def assembly_view(request):
        """GET /api/assembly/view?cache_key=ck —— 按 cacheKey 直载缓存。

        与 parse 的差异：不读源文件（无需再算内容 hash），源文件已
        移动/删除或路径不在 allowed_dirs 时仍可加载。回首页（编辑页
        「← 首页」、最近列表点击）与跨浏览器恢复走此通道。
        返回结构同 parse：{cache_key, cache_hit, base_url, manifest}。
        """
        try:
            check_auth(request)
            key = str(request.query_params.get("cache_key") or "")
            if not key:
                return JSONResponse({"error": "cache_key required"},
                                    status_code=400)
            cache_dir, _ = _edit_context(key)   # 校验 cache 存在
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except KeyError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse({"cache_key": key, "cache_hit": True,
                             "base_url": f"/cache/{key}",
                             "manifest": _view_manifest(key)})

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
        M6.5: version-resolved instance moves are applied to the tree
        matrices (move-only versions carry no template files).
        """
        manifest = cad_assembly.load_cache(os.path.join(cache_root, cache_key))
        store = _store(cache_key)
        for t in manifest["templates"]:
            vurl = store.resolve_gltf(t["id"])
            if vurl:
                t["gltf"] = vurl
        moves = store.resolve_moves()
        if moves:
            manifest = cad_assembly.apply_moves(manifest, moves)
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
            # OCP 重活下放线程池：几何仍在全局锁内串行，但事件循环不被
            # 阻塞（编辑期间其余请求/静态文件/WS 正常响应）
            def work():
                with _GEOMETRY_LOCK, _SuppressStdout():
                    # resolve the CURRENT geometry of this template (version
                    # chain first, baseline cache second)
                    step_path = store.resolve_step(
                        tid, baseline_step=os.path.join(
                            cache_dir, "parts", f"{tid}.step"))
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
                        feat = next((x for x in feats
                                     if x.get("id") == feature_id), None)
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
                    all_shapes = cad_assembly.template_shapes_from_cache(
                        cache_dir, manifest)
                    all_shapes[tid] = new_shape
                    hits = cad_assembly.check_interference(
                        manifest, all_shapes, edited_template=tid,
                        edited_shape=new_shape)
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
                    return record, changelog
            result = await run_in_threadpool(work)
            if isinstance(result, JSONResponse):   # 守门/校验拒绝路径
                return result
            record, changelog = result
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e),
                                 "stage": "validation"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            log.exception("edit failed: %s %s", cache_key, tid)
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
        await _broadcast({"type": "version_changed", "cache_key": cache_key,
                          "version": vid})
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
            def work():
                with _GEOMETRY_LOCK, _SuppressStdout():
                    return cad_assembly.audit_assembly(cache_dir, manifest)
            report = await run_in_threadpool(work)
        except Exception as e:  # noqa: BLE001
            log.exception("audit failed: %s", cache_key)
            return JSONResponse({"error": str(e)}, status_code=422)
        report["cache_key"] = cache_key
        return JSONResponse(report)

    # ==================================================================
    # M2: 草稿 API（声明式步骤表 · 多目标 · 单槽位 · 增量干涉）
    # ==================================================================
    async def draft_load(request):
        """GET /api/drafts?cache_key=ck -- 读取草稿步骤表。

        无草稿返回 ``{"empty": true}``。有草稿时返回完整结构：
        ``{cache_key, baseline_version, baseline_source_file, steps, ...}``，
        且每个 step 附带 ``title``（后端重算）便于前端展示。
        """
        try:
            check_auth(request)
            cache_key = str(request.query_params["cache_key"])
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except KeyError as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        path = _draft_path(cache_key)
        if not os.path.isfile(path):
            return JSONResponse({"cache_key": cache_key, "empty": True, "steps": []})
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            # 补 title（manifest 可能已变，每次重算）
            try:
                _, manifest = _edit_context(cache_key)
                for s in d.get("steps", []):
                    s["title"] = cad_assembly.draft_step_title(s, manifest)
            except Exception:  # noqa: BLE001
                pass
            return JSONResponse(d)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)}, status_code=422)

    async def draft_save(request):
        """POST /api/drafts/save -- 单槽位整体覆盖保存。

        body: ``{cache_key, baseline_version, baseline_source_file, steps}``
        其中 ``steps`` 是声明式步骤表，每项含
        ``{id, template_id, operation, params, feature_id?}``。
        """
        try:
            check_auth(request)
            body = json.loads(await request.body() or b"{}")
            cache_key = str(body["cache_key"])
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        steps = body.get("steps") or []
        # 轻量校验：每项必须有 template_id/operation
        for i, s in enumerate(steps):
            if not s.get("template_id") or not s.get("operation"):
                return JSONResponse(
                    {"error": f"step #{i} missing template_id/operation"},
                    status_code=400)
        import time as _t
        draft = {
            "schema_version": cad_assembly.DRAFT_SCHEMA_VERSION,
            "cache_key": cache_key,
            "baseline_version": str(body.get("baseline_version") or "v0"),
            "baseline_source_file": str(body.get("baseline_source_file") or ""),
            "created": str(body.get("created") or _t.strftime("%Y-%m-%d %H:%M:%S")),
            "updated": _t.strftime("%Y-%m-%d %H:%M:%S"),
            "steps": steps,
        }
        # 加上每步的 title（若前端已带则保留，否则后端补）
        try:
            _, manifest = _edit_context(cache_key)
            for s in draft["steps"]:
                s.setdefault("title",
                             cad_assembly.draft_step_title(s, manifest))
        except Exception:  # noqa: BLE001
            pass
        path = _draft_path(cache_key)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(draft, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            _silent_remove(tmp)
            return JSONResponse({"error": str(e)}, status_code=422)
        await _broadcast({"type": "draft_saved", "cache_key": cache_key,
                          "step_count": len(steps),
                          "client": str(body.get("client") or "")})
        return JSONResponse({"ok": True, "cache_key": cache_key,
                             "step_count": len(steps)})

    async def draft_delete(request):
        """DELETE /api/drafts?cache_key=ck -- 放弃草稿（删除文件）。

        同时清理草稿预览 gltf 目录。幂等：不存在不算错误。
        """
        try:
            check_auth(request)
            cache_key = str(request.query_params["cache_key"])
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except KeyError as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        import shutil as _shutil
        _silent_remove(_draft_path(cache_key))
        _shutil.rmtree(os.path.join(drafts_root, cache_key),
                       ignore_errors=True)
        await _broadcast({"type": "draft_deleted", "cache_key": cache_key,
                          "client": str(request.query_params.get("client")
                                        or "")})
        return JSONResponse({"ok": True, "cache_key": cache_key})

    async def draft_preview(request):
        """POST /api/drafts/preview -- 重放草稿步骤表，返回草稿 manifest
        + 增量干涉结果（自动检查）。

        body: ``{cache_key, steps, baseline_version?}``。
        若 ``baseline_version`` 缺省则用 store.current。

        返回：
          ``{ok, manifest, interferences, edited_templates}``
        其中 manifest 的 templates 的 gltf 指向
        ``/drafts/<ck>/preview/<tid>.gltf``（草稿几何落盘到预览目录，
        不进版本链）。``interferences`` 是多模板增量检查结果（列表同
        check_interference）。
        """
        try:
            check_auth(request)
            body = json.loads(await request.body() or b"{}")
            cache_key = str(body["cache_key"])
            steps = body.get("steps") or []
            # level: 'bbox'（默认，AABB 快速反馈，拖拽级交互用）|'exact'
            # （布尔精检，显式重检/确认前核对用）。确认保存的守门
            # 始终 exact，不受此参数影响。
            level = str(body.get("level") or "bbox")
            if level not in ("bbox", "exact"):
                return JSONResponse(
                    {"error": f"invalid level: {level}"}, status_code=400)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        try:
            cache_dir, manifest = _edit_context(cache_key)
        except KeyError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        store = _store(cache_key)
        preview_dir = _draft_preview_dir(cache_key)
        try:
            # OCP 重活（重放+导出+干涉）下放线程池，事件循环保持响应
            def work():
                with _GEOMETRY_LOCK, _SuppressStdout():
                    # 0) 空步骤短路：草稿=基线，无需重放/导出/检查。
                    # 否则删光步骤的 preview 会退化为全量 O(n²) 布尔
                    # 检查（62 模板实测 ~5 分钟）
                    if not steps:
                        return {}, {}, [], {"per_template": [], "totals": {}}, manifest
                    # 1) move 步骤解析+校验提前（未命中 node_id 直接
                    # ValueError 400，不再先花 ~7s 加载全模板形状）
                    moves = cad_assembly.moves_from_steps(steps)
                    gate_manifest = (cad_assembly.apply_moves(manifest, moves)
                                     if moves else manifest)
                    # 2) 重放草稿几何步骤，得到 {tid: shape}
                    edited_shapes = cad_assembly.replay_draft_shapes(
                        cache_dir, manifest, store, steps,
                        cache_root=cache_root)
                    # 3) 落盘每个被编辑模板的草稿 gltf（预览目录，不进版本链）
                    for tid, shp in edited_shapes.items():
                        tpl = next((t for t in manifest["templates"]
                                    if t["id"] == tid), None)
                        if not tpl:
                            continue
                        gltf_out = os.path.join(preview_dir, f"{tid}.gltf")
                        cad_assembly._export_template_gltf(
                            shp, tpl["name"], tpl.get("color"), gltf_out)
                    # 4) 全模板形状 map（基线缓存 + 草稿覆盖）
                    all_shapes = cad_assembly.template_shapes_from_cache(
                        cache_dir, manifest)
                    all_shapes.update(edited_shapes)
                    # 5) 多模板增量干涉（含 move 实例）。level=bbox 走
                    # AABB 快速反馈（交互拖拽级），exact 走布尔精检
                    hits = cad_assembly.check_interference(
                        gate_manifest, all_shapes,
                        edited_templates=edited_shapes,
                        moved_ids=set(moves.keys()), mode=level)
                    # 4b) M5 差异摘要：体积/表面积。基线走版本链解析（与
                    # replay 的输入同源），保证 baseline vs draft 可比
                    base_shapes = {}
                    for tid in edited_shapes:
                        base_shapes[tid] = cad_core.read_shape(
                            store.resolve_step(
                                tid, baseline_step=os.path.join(
                                    cache_dir, "parts", f"{tid}.step")))
                    diff = cad_assembly.draft_diff(manifest, base_shapes,
                                                   edited_shapes)
                    # 5) 构造草稿 manifest：基线 + 草稿 gltf 路径覆盖 + move 位移
                    draft_manifest = json.loads(json.dumps(gate_manifest))
                    for t in draft_manifest["templates"]:
                        if t["id"] in edited_shapes:
                            rel = os.path.relpath(
                                os.path.join(preview_dir, f"{t['id']}.gltf"),
                                os.path.dirname(drafts_root))
                            t["gltf"] = "/" + rel.replace(os.sep, "/")
                    return (edited_shapes, moves, hits, diff, draft_manifest)
            edited_shapes, moves, hits, diff, draft_manifest = \
                await run_in_threadpool(work)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e),
                                 "stage": "validation"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            log.exception("draft preview failed: %s", cache_key)
            return JSONResponse({"ok": False, "error": str(e),
                                 "stage": "internal"}, status_code=422)
        return JSONResponse({
            "ok": True,
            "manifest": draft_manifest,
            # 未被编辑的模板 gltf 仍是 cache 相对路径，前端以此拼接
            "base_url": f"/cache/{cache_key}",
            "interferences": hits,
            "interference_count": len(hits),
            "check_level": level,
            "edited_templates": list(edited_shapes.keys()),
            "moved_nodes": list(moves.keys()),
            "diff": diff,
        })

    async def draft_confirm(request):
        """POST /api/drafts/confirm -- 把草稿步骤全部落为一条版本。

        body: ``{cache_key, steps, baseline_version?}``。
        流程：重放草稿 -> 完整体检守门（多模板增量） -> 通过则批量
        提交（一个版本，changelog 列出所有步骤标题） -> 删除草稿文件。

        守门拒绝（409）则版本不动，返回结构化干涉结果。
        """
        try:
            check_auth(request)
            body = json.loads(await request.body() or b"{}")
            cache_key = str(body["cache_key"])
            steps = body.get("steps") or []
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        if not steps:
            return JSONResponse({"error": "no steps to confirm"},
                                status_code=400)
        try:
            cache_dir, manifest = _edit_context(cache_key)
        except KeyError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        store = _store(cache_key)
        tmp_dir = os.path.join(versions_root, cache_key, "_prep_draft")
        try:
            # OCP 重活下放线程池（同 preview）
            def work():
                with _GEOMETRY_LOCK, _SuppressStdout():
                    # 1) 重放草稿（move 步骤在 shapes 重放中自动跳过）
                    edited_shapes = cad_assembly.replay_draft_shapes(
                        cache_dir, manifest, store, steps,
                        cache_root=cache_root)
                    # 1b) M6.5 实例级位移
                    moves = cad_assembly.moves_from_steps(steps)
                    gate_manifest = (cad_assembly.apply_moves(manifest, moves)
                                     if moves else manifest)
                    # 2) 完整增量干涉守门（与 preview 同样的多模板 + move 检查）
                    all_shapes = cad_assembly.template_shapes_from_cache(
                        cache_dir, manifest)
                    all_shapes.update(edited_shapes)
                    hits = cad_assembly.check_interference(
                        gate_manifest, all_shapes,
                        edited_templates=edited_shapes,
                        moved_ids=set(moves.keys()))
                    if hits:
                        return JSONResponse({
                            "ok": False, "error": "interference",
                            "stage": "interference_gate",
                            "interferences": hits,
                            "message": f"草稿会导致 {len(hits)} 处干涉，"
                                       f"已拒绝提交；版本保持 {store.current}。",
                            "version": store.current,
                        }, status_code=409)
                    # 3) 批量提交：一个版本，包含所有被编辑模板的 step+gltf
                    os.makedirs(tmp_dir, exist_ok=True)
                    changes = {}
                    step_titles = []
                    for tid, shp in edited_shapes.items():
                        tpl = next((t for t in manifest["templates"]
                                    if t["id"] == tid), None)
                        if not tpl:
                            continue
                        step_out = os.path.join(tmp_dir, f"{tid}.step")
                        gltf_out = os.path.join(tmp_dir, f"{tid}.gltf")
                        cad_core.write_shape(shp, step_out, overwrite=True)
                        cad_assembly._export_template_gltf(
                            shp, tpl["name"], tpl.get("color"), gltf_out)
                        changes[tid] = {"step": step_out, "gltf": gltf_out}
                    # changelog 列出所有步骤标题
                    for s in steps:
                        step_titles.append(
                            cad_assembly.draft_step_title(s, manifest))
                    changelog = "草稿批量提交：\n  - " + "\n  - ".join(step_titles)
                    record = store.commit(
                        changes, changelog=changelog, prepared_dir=tmp_dir,
                        moves={nid: {"dx": d[0], "dy": d[1], "dz": d[2]}
                               for nid, d in moves.items()})
                    # 4) 刷新被编辑模板的特征缓存（保留稳定 id）
                    for tid, shp in edited_shapes.items():
                        try:
                            cad_assembly.refresh_template_features(
                                shp, cache_dir, tid)
                        except Exception:  # noqa: BLE001
                            pass
                    # 5) 删除草稿文件 + 预览目录
                    _silent_remove(_draft_path(cache_key))
                    import shutil as _shutil
                    _shutil.rmtree(os.path.join(drafts_root, cache_key),
                                   ignore_errors=True)
                    return record, changelog
            result = await run_in_threadpool(work)
            if isinstance(result, JSONResponse):   # 守门拒绝路径
                return result
            record, changelog = result
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e),
                                 "stage": "validation"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            log.exception("draft confirm failed: %s", cache_key)
            return JSONResponse({"ok": False, "error": str(e),
                                 "stage": "internal"}, status_code=422)
        finally:
            import shutil as _shutil
            _shutil.rmtree(tmp_dir, ignore_errors=True)
        await _broadcast({"type": "version_changed", "cache_key": cache_key,
                          "version": record["id"]})
        return JSONResponse({
            "ok": True, "version": record["id"],
            "changelog": changelog, "parent": record["parent"],
            "manifest": _view_manifest(cache_key),
        })

    # ==================================================================
    # M5: FEA 基线 vs 草稿双跑对比（R5 异步任务协议）
    # ==================================================================
    def _fea_compare_core(cache_key: str, tid: str, steps: list,
                          spec: dict, ctx=None) -> dict:
        """双跑核心：基线（版本链解析）与草稿（重放）各跑一次静力学。

        OCCT 段（read/replay/write step）在几何锁内；FreeCAD/CalculiX
        子进程段在锁外（外部进程，不碰 OCCT）。草稿 STEP 落在
        drafts/<ck>/fea/ 下，内容寻址 FEA 缓存自然生效（R8）。
        """
        import cad_fea
        cache_dir, manifest = _edit_context(cache_key)
        store = _store(cache_key)
        progress = _ctx_progress(ctx)
        should_cancel = _ctx_cancel(ctx)

        progress("prepare", 2, "解析基线几何（版本链）")
        base_step = store.resolve_step(
            tid, baseline_step=os.path.join(cache_dir, "parts", f"{tid}.step"))
        progress("replay", 6, "重放草稿步骤")
        with _GEOMETRY_LOCK, _SuppressStdout():
            edited = cad_assembly.replay_draft_shapes(
                cache_dir, manifest, store, steps, cache_root=cache_root)
            if tid not in edited:
                raise ValueError(
                    f"草稿没有编辑模板 {tid}（步骤表为空或不涉及该模板）")
            draft_step_dir = os.path.join(drafts_root, cache_key, "fea")
            os.makedirs(draft_step_dir, exist_ok=True)
            draft_step = os.path.join(draft_step_dir, f"{tid}_draft.step")
            cad_core.write_shape(edited[tid], draft_step, overwrite=True)

        def run_one(label, step_path, pct_base):
            p = progress

            def relay(phase, percent, detail):
                p(phase, pct_base + int(percent * 0.45), f"{label}: {detail}")
            key = cad_fea.fea_cache_key(step_path, spec)
            out_dir = os.path.join(fea_root, key)
            res = cad_fea.run_static(step_path, out_dir, spec,
                                     progress=relay,
                                     should_cancel=should_cancel)
            res["fea_key"] = key
            res["base_url"] = f"/fea/{key}"
            return res

        progress("solve", 10, "基线 FEA 求解")
        baseline = run_one("基线", base_step, 10)
        progress("solve", 55, "草稿 FEA 求解")
        draft = run_one("草稿", draft_step, 55)

        def pct(new, old):
            if old in (None, 0) or new is None:
                return None
            return round((new - old) / old * 100, 2)
        delta = {
            "max_displacement_pct": pct(
                draft.get("max_displacement_mm"),
                baseline.get("max_displacement_mm")),
            "max_von_mises_pct": pct(
                draft.get("max_von_mises_MPa"),
                baseline.get("max_von_mises_MPa")),
        }
        return {
            "ok": True, "template_id": tid,
            "spec": cad_fea.normalize_spec(spec),
            "baseline": baseline, "draft": draft, "delta": delta,
        }

    async def draft_fea_compare(request):
        """POST /api/drafts/fea-compare -- M5 基线 vs 草稿双跑对比。

        body: ``{cache_key, template_id, steps, spec?}``。异步任务协议
        （R5）：202 + job_id，轮询 /api/jobs/{id}，结果含
        ``{baseline, draft, delta}``。FEA 插件缺失 → 503（结构化 missing）。
        """
        try:
            check_auth(request)
            body = json.loads(await request.body() or b"{}")
            cache_key = str(body["cache_key"])
            tid = str(body["template_id"])
            steps = body.get("steps") or []
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        try:
            _, manifest = _edit_context(cache_key)
        except KeyError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        if next((t for t in manifest["templates"] if t["id"] == tid),
                None) is None:
            return JSONResponse(
                {"error": f"unknown template_id: {tid}"}, status_code=400)
        if not steps:
            return JSONResponse({"error": "no draft steps"}, status_code=400)
        spec = body.get("spec") or {}
        import cad_fea
        status = cad_fea.fea_status()
        if not status["available"]:
            return JSONResponse(
                {"ok": False, "plugin": "fea", "kind": "missing",
                 "missing": status["missing"], "error": status["hint"]},
                status_code=503)
        jid = jobs_mgr.submit(
            "fea_compare",
            lambda ctx: _fea_compare_core(cache_key, tid, steps, spec, ctx),
            meta={"cache_key": cache_key, "template_id": tid})
        return _job_started(jid, "fea_compare",
                            {"cache_key": cache_key, "template_id": tid})

    # ==================================================================
    # M6: 用户选中上行 + 会话发现（agent 通信回路）
    # ==================================================================
    def _find_node(root: dict, node_id: str):
        stack = [root]
        while stack:
            n = stack.pop()
            if n.get("id") == node_id:
                return n
            stack.extend(n.get("children", []))
        return None

    def _selection_path(cache_key: str) -> str:
        return os.path.join(selection_root, f"{cache_key}.json")

    def _selection_enriched(rec: dict) -> dict:
        """给选中记录组装 agent 友好上下文：节点名/类型、模板名、特征详情。"""
        out = dict(rec)
        try:
            cache_dir, manifest = _edit_context(rec["cache_key"])
            node = _find_node(manifest["root"], rec.get("node_id") or "")
            if node:
                out["node_name"] = node.get("name", "")
                out["node_type"] = node.get("type", "")
            tpl = next((t for t in manifest["templates"]
                        if t["id"] == rec.get("template_id")), None)
            if tpl:
                out["template_name"] = tpl.get("name", "")
            fid = rec.get("feature_id")
            if fid:
                fp = os.path.join(cache_dir, "features",
                                  f"{rec['template_id']}.json")
                if os.path.isfile(fp):
                    with open(fp, encoding="utf-8") as f:
                        feats = json.load(f)
                    feat = next((x for x in feats if x.get("id") == fid), None)
                    if feat:
                        out["feature"] = feat
        except Exception:  # noqa: BLE001
            pass
        return out

    async def selection_post(request):
        """POST /api/selection -- 用户选中上行（M6）。

        body: ``{cache_key, node_id?, template_id?, feature_id?,
        source_file?, page?, client?}``。单槽位 last-click-wins：同一
        cacheKey 的最新点选覆盖旧的（多 tab 时"这个"指用户最后点的）。
        广播 ``selection_changed`` 事件给 /ws 客户端。
        """
        try:
            check_auth(request)
            body = json.loads(await request.body() or b"{}")
            cache_key = str(body["cache_key"])
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        import time as _t
        rec = {
            "cache_key": cache_key,
            "node_id": body.get("node_id") or None,
            "template_id": body.get("template_id") or None,
            "feature_id": body.get("feature_id") or None,
            "source_file": str(body.get("source_file") or ""),
            "page": str(body.get("page") or ""),
            "client": str(body.get("client") or ""),
            "updated": _t.strftime("%Y-%m-%d %H:%M:%S"),
        }
        path = _selection_path(cache_key)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            _silent_remove(tmp)
            return JSONResponse({"error": str(e)}, status_code=422)
        await _broadcast({"type": "selection_changed", **rec})
        return JSONResponse({"ok": True, "cache_key": cache_key})

    async def selection_get(request):
        """GET /api/selection[?cache_key=ck | ?all=1] -- 读用户选中（M6）。

        带 cache_key 返回该会话的选中；带 ``all=1`` 返回全部缓存Key的选中
        （每条带 cache_key / source_file / 零件上下文，供 agent 跨文件分辨
        "哪个零件来自哪个文件"）；两者都不带则返回全部会话中最新的一条
        （agent 消解"这个"的默认语义）。响应附带节点/模板/特征上下文。
        """
        try:
            check_auth(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        cache_key = request.query_params.get("cache_key", "")
        if cache_key:
            fp = _selection_path(cache_key)
            if not os.path.isfile(fp):
                return JSONResponse({"empty": True})
            with open(fp, encoding="utf-8") as f:
                rec = json.load(f)
            return JSONResponse(_selection_enriched(rec))
        # ?all=1：聚合全部 cacheKey 的选中，跨文件语义（agent 一次看到多个）
        if request.query_params.get("all", "0") in ("1", "true", "yes"):
            items = []
            if os.path.isdir(selection_root):
                for fn in os.listdir(selection_root):
                    if not fn.endswith(".json"):
                        continue
                    fp = os.path.join(selection_root, fn)
                    try:
                        with open(fp, encoding="utf-8") as f:
                            rec = json.load(f)
                        if rec.get("node_id") or rec.get("feature_id"):
                            items.append(_selection_enriched(rec))
                    except Exception:  # noqa: BLE001
                        continue
            items.sort(key=lambda r: r.get("updated", ""), reverse=True)
            return JSONResponse({"selections": items})
        # 无 cache_key / all：全目录按 mtime 最新
        best, best_mt = None, -1.0
        if os.path.isdir(selection_root):
            for fn in os.listdir(selection_root):
                if not fn.endswith(".json"):
                    continue
                fp = os.path.join(selection_root, fn)
                mt = os.path.getmtime(fp)
                if mt > best_mt:
                    best, best_mt = fp, mt
        if best is None:
            return JSONResponse({"empty": True})
        with open(best, encoding="utf-8") as f:
            rec = json.load(f)
        return JSONResponse(_selection_enriched(rec))

    async def sessions_list(request):
        """GET /api/sessions -- 活跃会话发现（M6，agent 入口）。

        扫描 cache 目录，每个会话返回源文件、当前版本、草稿步骤数、
        最近活动时间（cache mtime）。按新→旧排序。
        """
        try:
            check_auth(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        items = []
        if os.path.isdir(cache_root):
            for key in os.listdir(cache_root):
                ck_dir = os.path.join(cache_root, key)
                if not os.path.isfile(os.path.join(ck_dir,
                                                   "tree_structure.json")):
                    continue
                try:
                    _, manifest = _edit_context(key)
                    store = _store(key)
                    draft_steps = 0
                    draft_baseline = None
                    if os.path.isfile(_draft_path(key)):
                        with open(_draft_path(key), encoding="utf-8") as f:
                            d = json.load(f)
                            draft_steps = len(d.get("steps") or [])
                            draft_baseline = d.get("baseline_version")
                    items.append({
                        "cache_key": key,
                        "source_file": manifest.get("source_file", ""),
                        "current_version": store.current,
                        "template_count": len(manifest.get("templates", [])),
                        "draft_steps": draft_steps,
                        "draft_baseline_version": draft_baseline,
                        "updated": _t_str(os.path.getmtime(ck_dir)),
                    })
                except Exception:  # noqa: BLE001
                    continue
        items.sort(key=lambda x: x["updated"], reverse=True)
        return JSONResponse({"sessions": items})

    def _t_str(ts: float) -> str:
        import time as _t
        return _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(ts))

    # ==================================================================
    # M4: 报告中心（快照报告：体检 + 统计 + 版本历史）
    # ==================================================================
    async def report_generate(request):
        """POST /api/reports/generate -- 生成快照报告并落盘。

        聚合：干涉 + DFM 体检（模块七）· 装配统计（模板/实例/体积/
        表面积，实例数加权）· 版本历史。确定性内容（D8），报告为不可变
        快照（生成后不受后续编辑影响）。
        """
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
        try:
            def work():
                with _GEOMETRY_LOCK, _SuppressStdout():
                    audit = cad_assembly.audit_assembly(cache_dir, manifest)
                    counts = cad_assembly.template_instance_counts(manifest)
                    stats = {"total_volume_mm3": 0.0, "total_area_mm2": 0.0}
                    for t in manifest["templates"]:
                        fp = os.path.join(cache_dir, "parts", f"{t['id']}.step")
                        if not os.path.isfile(fp):
                            continue
                        p = cad_core.properties(cad_core.read_shape(fp))
                        n = counts.get(t["id"], 1)
                        stats["total_volume_mm3"] += p.get("volume", 0.0) * n
                        stats["total_area_mm2"] += p.get("surface_area", 0.0) * n
                    return audit, stats, counts
            audit, stats, counts = await run_in_threadpool(work)
        except Exception as e:  # noqa: BLE001
            log.exception("report generate failed: %s", cache_key)
            return JSONResponse({"error": str(e)}, status_code=422)
        store = _store(cache_key)
        vers = store.manifest.get("versions", [])
        import time as _t
        report_id = "r" + _t.strftime("%Y%m%d_%H%M%S")
        report = {
            "schema_version": 1,
            "report_id": report_id,
            "cache_key": cache_key,
            "created": _t.strftime("%Y-%m-%d %H:%M:%S"),
            "source_file": manifest.get("source_file", ""),
            "version": store.current,
            "summary": {
                "templates": len(manifest["templates"]),
                "instances": sum(counts.values()),
                "interferences": audit["interference_count"],
                "dfm_warnings": sum(
                    1 for d in audit["dfm"] if d["severity"] == "warning"),
                "dfm_infos": sum(
                    1 for d in audit["dfm"] if d["severity"] == "info"),
            },
            "assembly_stats": {
                "total_volume_mm3": round(stats["total_volume_mm3"], 3),
                "total_area_mm2": round(stats["total_area_mm2"], 3),
            },
            "interferences": audit["interferences"],
            "dfm": audit["dfm"],
            "versions": [
                {"id": v.get("id"), "changelog": v.get("changelog", ""),
                 "created": v.get("created", "")}
                for v in reversed(vers or [])],
        }
        out = os.path.join(_report_dir(cache_key), f"{report_id}.json")
        tmp = out + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            os.replace(tmp, out)
        except Exception as e:  # noqa: BLE001
            _silent_remove(tmp)
            return JSONResponse({"error": str(e)}, status_code=422)
        await _broadcast({"type": "report_added", "cache_key": cache_key,
                          "report_id": report_id})
        return JSONResponse(report)

    async def reports_list(request):
        """GET /api/reports?cache_key=ck -- 报告列表（新→旧，只含摘要）。"""
        try:
            check_auth(request)
            cache_key = str(request.query_params["cache_key"])
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except KeyError as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        d = os.path.join(reports_root, cache_key)
        items = []
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(d, fn), encoding="utf-8") as f:
                        r = json.load(f)
                    items.append({
                        "report_id": r["report_id"], "created": r["created"],
                        "version": r.get("version"), "summary": r["summary"],
                    })
                except Exception:  # noqa: BLE001
                    continue
        items.sort(key=lambda x: x["report_id"], reverse=True)
        return JSONResponse({"cache_key": cache_key, "reports": items})

    async def report_get(request):
        """GET /api/reports/get?cache_key=ck&report_id=... -- 完整报告。"""
        try:
            check_auth(request)
            cache_key = str(request.query_params["cache_key"])
            report_id = str(request.query_params["report_id"])
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except KeyError as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)
        fp = safe_reports_path(
            os.path.join(cache_key, f"{report_id}.json"))
        if not os.path.isfile(fp):
            return JSONResponse({"error": "no such report"}, status_code=404)
        with open(fp, encoding="utf-8") as f:
            return JSONResponse(json.load(f))

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
        try:
            sha = hashlib.sha256()
            with open(src, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    sha.update(chunk)
        except FileNotFoundError:
            return JSONResponse({"error": f"文件不存在: {src}"}, status_code=400)
        key = sha.hexdigest()[:16]
        out_dir = os.path.join(drawings_root, key)

        # force=1：忽略已有缓存，删除后重建（便于确认重新导入/新的渲染逻辑生效）
        force = bool(body.get("force"))
        if force and os.path.isdir(out_dir):
            for fn in ("drawing.json", "view.svg", "dwg_converted.dxf"):
                p = os.path.join(out_dir, fn)
                if os.path.isfile(p):
                    os.remove(p)

        import cad_drawing
        # 缓存命中 = drawing.json 存在且 schema 版本与当前渲染逻辑一致；
        # 渲染逻辑升级后旧缓存自动重建，避免"缓存命中但 SVG 是旧逻辑"的困惑
        cached = False
        dj = os.path.join(out_dir, "drawing.json")
        if os.path.isfile(dj):
            try:
                with open(dj, encoding="utf-8") as f:
                    cached = (json.load(f).get("schema_version")
                              == cad_drawing.DRAWING_SCHEMA_VERSION)
            except Exception:  # noqa: BLE001
                cached = False
        if not cached:
            try:
                def work():
                    with _GEOMETRY_LOCK, _SuppressStdout():
                        cad_drawing.import_drawing(src, out_dir)
                await run_in_threadpool(work)
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

    async def drafts_file(request):
        """静态服务草稿预览 gltf/.bin（前端通过 /drafts/<ck>/preview/... 加载）。"""
        try:
            fp = safe_drafts_path(request.path_params["rest"])
        except PermissionError:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not os.path.isfile(fp):
            return JSONResponse({"error": "not found"}, status_code=404)
        media = "application/json" if fp.endswith(".json") else None
        return FileResponse(fp, media_type=media) if media else FileResponse(fp)

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

    def _fea_core(cache_key: str, tid: str, spec: dict, force: bool,
                  ctx=None) -> dict:
        """Shared FEA body (sync handler + R5 async job). ctx is a
        cad_jobs._JobCtx or None."""
        import cad_fea
        cache_dir, _ = _edit_context(cache_key)
        store = _store(cache_key)
        step_path = store.resolve_step(
            tid, baseline_step=os.path.join(cache_dir, "parts", f"{tid}.step"))
        key = cad_fea.fea_cache_key(step_path, spec)
        out_dir = os.path.join(fea_root, key)
        result = cad_fea.run_static(
            step_path, out_dir, spec, force=force,
            progress=_ctx_progress(ctx), should_cancel=_ctx_cancel(ctx))
        result["fea_key"] = key
        result["base_url"] = f"/fea/{key}"
        return result

    def _render_core(cache_key: str, spec: dict, force: bool,
                     ctx=None) -> dict:
        """Shared render body (sync handler + R5 async job)."""
        import cad_render
        cache_dir, manifest = _edit_context(cache_key)
        store = _store(cache_key)

        def resolve_gltf_abs(tid: str) -> str | None:
            vurl = store.resolve_gltf(tid)
            if vurl:
                return os.path.join(workspace, vurl.lstrip("/"))
            return os.path.join(cache_dir, "gltf_library", f"{tid}.gltf")

        entries = cad_render.build_render_entries(manifest, resolve_gltf_abs)
        key = cad_render.render_cache_key(entries, spec)
        out_dir = os.path.join(render_root, key)
        result = cad_render.render_scene(
            entries, out_dir, spec, force=force,
            progress=_ctx_progress(ctx), should_cancel=_ctx_cancel(ctx))
        result["render_key"] = key
        result["base_url"] = f"/render/{key}"
        result["png_url"] = f"/render/{key}/render.png"
        return result

    def _job_started(jid: str, kind: str, meta: dict) -> JSONResponse:
        return JSONResponse({"job_id": jid, "kind": kind, "status": "queued",
                             "url": f"/api/jobs/{jid}", "meta": meta},
                            status_code=202)

    async def fea_static(request):
        """POST /api/fea/static -- D7 FEA plugin: CalculiX static single
        scenario on a version-resolved template STEP (D6: FreeCAD runs as a
        headless subprocess). Default synchronous; ``"async": true``
        switches to the R5 job protocol (202 + job_id, poll
        /api/jobs/{id}, cancel via /api/jobs/{id}/cancel)."""
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
        force = bool(body.get("force"))
        if body.get("async"):
            jid = jobs_mgr.submit(
                "fea", lambda ctx: _fea_core(cache_key, tid, spec, force, ctx),
                meta={"cache_key": cache_key, "template_id": tid})
            return _job_started(jid, "fea",
                                {"cache_key": cache_key, "template_id": tid})
        import cad_fea
        try:
            result = _fea_core(cache_key, tid, spec, force)
        except cad_fea.FEAError as e:
            code = {"missing": 503, "timeout": 504,
                    "cancelled": 409}.get(e.kind, 422)
            return JSONResponse({"ok": False, "error": str(e), "plugin": "fea",
                                 "kind": e.kind, "missing": e.missing},
                                status_code=code)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e),
                                 "stage": "validation"}, status_code=400)
        except FileNotFoundError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
        return JSONResponse(result)

    async def render(request):
        """POST /api/render -- D7 render plugin: Blender headless still of
        the version-resolved assembly state (R9: external dependency, never
        bundled). Default synchronous; ``"async": true`` switches to the R5
        job protocol (202 + job_id, progress + cancel)."""
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
        force = bool(body.get("force"))
        if body.get("async"):
            jid = jobs_mgr.submit(
                "render", lambda ctx: _render_core(cache_key, spec, force, ctx),
                meta={"cache_key": cache_key})
            return _job_started(jid, "render", {"cache_key": cache_key})
        import cad_render
        try:
            result = _render_core(cache_key, spec, force)
        except cad_render.RenderError as e:
            code = {"missing": 503, "timeout": 504,
                    "cancelled": 409}.get(e.kind, 422)
            return JSONResponse({"ok": False, "error": str(e),
                                 "plugin": "render", "kind": e.kind,
                                 "missing": e.missing}, status_code=code)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e),
                                 "stage": "validation"}, status_code=400)
        return JSONResponse(result)

    # ----------------------------------------------------------------------
    # R5 job protocol: query / list / cancel
    # ----------------------------------------------------------------------

    async def job_get(request):
        """GET /api/jobs/{id} -- job snapshot (status/progress/result)."""
        try:
            check_auth(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401)
        snap = jobs_mgr.get(request.path_params["jid"])
        if snap is None:
            return JSONResponse({"error": "no such job"}, status_code=404)
        return JSONResponse(snap)

    async def jobs_list(request):
        """GET /api/jobs -- all job snapshots (newest last)."""
        try:
            check_auth(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401)
        return JSONResponse({"jobs": jobs_mgr.list()})

    async def job_cancel(request):
        """POST /api/jobs/{id}/cancel -- request cooperative cancellation.

        200 always (idempotent): finished jobs simply report their final
        status; running jobs get the terminate hook fired as a backstop."""
        try:
            check_auth(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401)
        snap = jobs_mgr.cancel(request.path_params["jid"])
        if snap is None:
            return JSONResponse({"error": "no such job"}, status_code=404)
        return JSONResponse(snap)

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

    # ==================================================================
    # M6: WebSocket 事件广播（浏览器实时刷新：agent 经 MCP 写草稿/落版本/
    # 生成报告后，已打开的页面原地更新，无需用户手动刷新）
    # ==================================================================
    ws_clients: set = set()

    async def _broadcast(event: dict) -> None:
        """向所有已连接的 /ws 客户端推送事件；发送失败的连接静默剔除。"""
        if not ws_clients:
            return
        dead = []
        for client in list(ws_clients):
            try:
                await client.send_json(event)
            except Exception:  # noqa: BLE001
                dead.append(client)
        for c in dead:
            ws_clients.discard(c)

    async def ws(websocket):
        """JSON 协议：客户端可发 ``{action: "ping"}`` 保活；服务端主动
        推送事件 ``{type: "draft_saved"|"draft_deleted"|"version_changed"|
        "report_added"|"selection_changed", ...}``（M6）。长任务仍走
        HTTP job 端点轮询（R5）。"""
        supplied = websocket.query_params.get("token", "")
        if not secrets.compare_digest(supplied, token):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        ws_clients.add(websocket)
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
        finally:
            ws_clients.discard(websocket)

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

    async def client_log_post(request):
        """POST /api/logs/client -- 前端全局错误上报（error + unhandledrejection）。

        body: ``{page, message, stack?, ua?}``。入环形缓冲 + 服务日志
        （WARNING 级），页面白屏/瘫痪时可直接在 service.log 定位前端堆栈。
        """
        try:
            check_auth(request)
            body = json.loads(await request.body() or b"{}")
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "page": str(body.get("page") or "")[:120],
            "message": str(body.get("message") or "")[:500],
            "stack": str(body.get("stack") or "")[:2000],
        }
        _CLIENT_ERRORS.append(entry)
        log.warning("client error [%s] %s\n%s",
                    entry["page"], entry["message"], entry["stack"] or "(no stack)")
        return JSONResponse({"ok": True})

    async def client_log_get(request):
        """GET /api/logs/client -- 最近前端错误（倒序，调试用）。"""
        try:
            check_auth(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401
                                if "token" in str(e) else 403)
        return JSONResponse({"errors": list(reversed(_CLIENT_ERRORS))})

    app = Starlette(middleware=[Middleware(_LoggingMiddleware)], routes=[
        Route("/health", health, methods=["GET"]),
        Route("/api/config", config, methods=["GET"]),
        Route("/api/upload", upload, methods=["POST"]),
        Route("/api/logs/client", client_log_post, methods=["POST"]),
        Route("/api/logs/client", client_log_get, methods=["GET"]),
        Route("/api/assembly/parse", parse, methods=["POST"]),
        Route("/api/assembly/view", assembly_view, methods=["GET"]),
        Route("/api/assembly/edit", edit, methods=["POST"]),
        Route("/api/assembly/audit", audit, methods=["GET"]),
        Route("/api/drafts", draft_load, methods=["GET"]),
        Route("/api/drafts", draft_delete, methods=["DELETE"]),
        Route("/api/drafts/save", draft_save, methods=["POST"]),
        Route("/api/drafts/preview", draft_preview, methods=["POST"]),
        Route("/api/drafts/confirm", draft_confirm, methods=["POST"]),
        Route("/api/drafts/fea-compare", draft_fea_compare, methods=["POST"]),
        Route("/api/reports/generate", report_generate, methods=["POST"]),
        Route("/api/reports/get", report_get, methods=["GET"]),
        Route("/api/reports", reports_list, methods=["GET"]),
        Route("/api/drawing/import", drawing_import, methods=["POST"]),
        Route("/api/versions", versions_list, methods=["GET"]),
        Route("/api/versions/checkout", versions_checkout, methods=["POST"]),
        Route("/api/plugins", plugins, methods=["GET"]),
        Route("/api/sessions", sessions_list, methods=["GET"]),
        Route("/api/selection", selection_post, methods=["POST"]),
        Route("/api/selection", selection_get, methods=["GET"]),
        Route("/api/fea/static", fea_static, methods=["POST"]),
        Route("/api/render", render, methods=["POST"]),
        Route("/api/jobs", jobs_list, methods=["GET"]),
        Route("/api/jobs/{jid}", job_get, methods=["GET"]),
        Route("/api/jobs/{jid}/cancel", job_cancel, methods=["POST"]),
        Route("/cache/{rest:path}", cache_file, methods=["GET"]),
        Route("/versions/{rest:path}", versions_file, methods=["GET"]),
        Route("/drawings/{rest:path}", drawings_file, methods=["GET"]),
        Route("/drafts/{rest:path}", drafts_file, methods=["GET"]),
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
