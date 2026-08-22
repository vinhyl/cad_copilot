"""Incremental version store for assembly edits (Phase C, ADR-0002 D10).

Per-project (cache-key scoped) version chain under the workspace:

    workspace/versions/<cache_key>/
    ├── manifest.json      {"schema_version", "current", "versions": [...]}
    ├── v1/
    │   ├── t1.step        modified part template B-rep (only changed templates)
    │   ├── t1.gltf/.bin   frontend geometry for the modified template
    └── v2/ ...

Semantics (D10 / R6 / R15):
  * Version v0 is implicit = the baseline cache (no files of its own).
  * A version node stores ONLY the templates it changed; resolving template
    geometry at version vN walks the chain vN -> v1 and returns the newest
    file <= vN, falling back to the baseline cache's ``parts/tN.step``.
  * Atomic commit: files are written into ``.tmp_vN/`` first, then renamed
    into place; the manifest is updated via ``manifest.json.tmp`` +
    os.replace. A crash leaves at most a stale ``.tmp_*`` dir which
    ``cleanup_temp()`` removes on startup (R6).
  * Rollback / checkout = moving the "current" pointer only. Historical
    version files are NEVER rewritten or deleted (D10).
  * Linear chain (each version's parent is the previous one); branching is
    a documented non-goal for now.
"""
from __future__ import annotations

import json
import os
import shutil
import time

SCHEMA_VERSION = 1


class VersionStore:
    def __init__(self, versions_root: str, cache_key: str):
        self.root = os.path.join(versions_root, cache_key)
        self.manifest_path = os.path.join(self.root, "manifest.json")
        os.makedirs(self.root, exist_ok=True)
        self._manifest = self._load()

    # ------------------------------------------------------------------
    # manifest handling
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if not os.path.isfile(self.manifest_path):
            return {"schema_version": SCHEMA_VERSION, "current": "v0",
                    "versions": []}
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_manifest(self) -> None:
        """Atomic manifest write: tmp file + os.replace (R6)."""
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._manifest, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.manifest_path)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    @property
    def manifest(self) -> dict:
        return json.loads(json.dumps(self._manifest))   # deep copy

    @property
    def current(self) -> str:
        return self._manifest["current"]

    def version_ids(self) -> list:
        return [v["id"] for v in self._manifest["versions"]]

    def get_version(self, vid: str) -> dict:
        if vid == "v0":
            return {"id": "v0", "parent": None, "created": "",
                    "changelog": "baseline", "changes": {}}
        for v in self._manifest["versions"]:
            if v["id"] == vid:
                return v
        raise KeyError(f"no such version: {vid}")

    def next_version_id(self) -> str:
        return f"v{len(self._manifest['versions']) + 1}"

    def _version_order(self) -> dict:
        """{version_id: index} with v0 (baseline) at 0."""
        order = {"v0": 0}
        for i, v in enumerate(self._manifest["versions"]):
            order[v["id"]] = i + 1
        return order

    def resolve_step(self, tid: str, version: str | None = None,
                     baseline_step: str | None = None) -> str:
        """Newest ``tid.step`` file at ``version`` (default: current),
        else ``baseline_step`` (the cache's parts/tN.step)."""
        vid = version or self.current
        order = self._version_order()
        if vid not in order:
            raise KeyError(f"no such version: {vid}")
        best = None
        for v in self._manifest["versions"]:
            if order[v["id"]] <= order[vid] and tid in v.get("changes", {}):
                if best is None or order[v["id"]] > order[best]:
                    best = v["id"]
        if best is not None:
            return os.path.join(self.root, best, f"{tid}.step")
        if baseline_step:
            return baseline_step
        raise FileNotFoundError(f"template {tid} has no geometry at {vid}")

    def resolve_gltf(self, tid: str, version: str | None = None) -> str | None:
        """Absolute URL path (/versions/...) for the newest tid.gltf at
        ``version``, or None when the baseline gltf should be used."""
        vid = version or self.current
        order = self._version_order()
        if vid not in order:
            raise KeyError(f"no such version: {vid}")
        best = None
        for v in self._manifest["versions"]:
            if order[v["id"]] <= order[vid] and tid in v.get("changes", {}):
                if best is None or order[v["id"]] > order[best]:
                    best = v["id"]
        if best is None:
            return None
        rel = os.path.relpath(os.path.join(self.root, best, f"{tid}.gltf"),
                              os.path.dirname(os.path.dirname(self.root)))
        return "/" + rel.replace(os.sep, "/")

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def commit(self, changes: dict, changelog: str,
               prepared_dir: str | None = None,
               moves: dict | None = None) -> dict:
        """Atomically commit a new version.

        Args:
            changes: {template_id: {"step": <abs path>, "gltf": <abs path>}}
                -- files must already exist (typically inside prepared_dir).
            changelog: deterministic human-readable description.
            prepared_dir: directory holding the files; its contents are
                moved into the new version dir (R6: prepare then rename).
            moves: M6.5 实例级位移 {node_id: {"dx","dy","dz"}}（草稿 move
                步骤落版本）。允许 moves-only 提交（changes 为空）；
                语义为"该节点相对基线的总位移"，跨版本后写覆盖。

        Returns the new version record.
        """
        if not changes and not moves:
            raise ValueError("empty changes")
        vid = self.next_version_id()
        target = os.path.join(self.root, vid)
        if os.path.exists(target):
            raise FileExistsError(f"version dir exists: {target}")

        for tid, files in changes.items():
            for key in ("step", "gltf"):
                if not os.path.isfile(files[key]):
                    raise FileNotFoundError(f"missing {key} for {tid}: {files[key]}")

        # R6: prepare-then-rename. Build the version dir under a temp name,
        # then atomically rename it into place.
        tmp_dir = os.path.join(self.root, f".tmp_{vid}_{int(time.time())}")
        os.makedirs(tmp_dir, exist_ok=True)
        try:
            for tid, files in changes.items():
                shutil.copy2(files["step"], os.path.join(tmp_dir, f"{tid}.step"))
                shutil.copy2(files["gltf"], os.path.join(tmp_dir, f"{tid}.gltf"))
                bin_file = files["gltf"].replace(".gltf", ".bin")
                if os.path.isfile(bin_file):
                    shutil.copy2(bin_file, os.path.join(tmp_dir, f"{tid}.bin"))
            os.rename(tmp_dir, target)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        record = {
            "id": vid,
            "parent": self.current,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "changelog": changelog,
            "changes": {tid: f"{vid}/{tid}.step" for tid in changes},
        }
        if moves:
            record["moves"] = {nid: {"dx": float(d.get("dx", 0)),
                                     "dy": float(d.get("dy", 0)),
                                     "dz": float(d.get("dz", 0))}
                               for nid, d in moves.items()}
        self._manifest["versions"].append(record)
        self._manifest["current"] = vid
        self._save_manifest()
        return record

    def resolve_moves(self, version: str | None = None) -> dict:
        """{node_id: (dx, dy, dz)} —— 版本链上 ≤ version 的全部 moves，
        后写覆盖（与 resolve_step 的"最新者胜"语义一致）。"""
        vid = version or self.current
        order = self._version_order()
        if vid not in order:
            raise KeyError(f"no such version: {vid}")
        out: dict[str, tuple] = {}
        for v in self._manifest["versions"]:
            if order[v["id"]] <= order[vid]:
                for nid, m in (v.get("moves") or {}).items():
                    out[nid] = (m.get("dx", 0), m.get("dy", 0), m.get("dz", 0))
        return out

    def checkout(self, vid: str) -> dict:
        """Move the current pointer to an existing version (rollback)."""
        self.get_version(vid)   # raises KeyError on unknown id
        self._manifest["current"] = vid
        self._save_manifest()
        return self.manifest

    def cleanup_temp(self) -> int:
        """Remove stale ``.tmp_*`` dirs left by crashed commits (R6)."""
        n = 0
        for name in os.listdir(self.root):
            if name.startswith(".tmp_"):
                shutil.rmtree(os.path.join(self.root, name), ignore_errors=True)
                n += 1
        return n
