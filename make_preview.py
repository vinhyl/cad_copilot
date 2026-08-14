"""Generate a self-contained 3D preview HTML for a CAD file.

Unlike the legacy cad_core.export_preview (which relied on three@0.160
examples/js global scripts that no longer exist on the CDN, and on a
sibling-STL relative fetch that fails under file:// CORS), this:

  * meshes the BREP with a size-relative deflection,
  * embeds the STL as a base64 data blob (no external file to fetch),
  * loads three.js + OrbitControls + STLLoader via an ES-module importmap
    (the supported modern path on current three versions).

Output: a single .html you can open anywhere with network access.
"""
from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cad_core  # noqa: E402

from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.StlAPI import StlAPI_Writer


def _mesh_to_file(shape, stl_path: str) -> bytes:
    """Mesh the shape, write the binary STL to stl_path, return its bytes.

    We write straight to the final .stl (also kept as a standalone artifact)
    to avoid a temp file + os.remove, which the sandbox safe-delete shim
    blocks on Windows (recycle bin unavailable).
    """
    props = cad_core.properties(shape)
    size = props["bounding_box"]["size"]
    maxdim = max(size) or 1.0
    # Size-relative deflection: ~1/800 of the largest extent (finer, so
    # fine features like threads tessellate cleanly), clamped so tiny parts
    # still get enough tessellation and huge ones stay fast.
    deflection = max(min(maxdim / 800.0, 0.5), 1e-5)
    BRepMesh_IncrementalMesh(shape, deflection)
    if not StlAPI_Writer().Write(shape, stl_path):
        raise RuntimeError("STL mesh write failed")
    with open(stl_path, "rb") as fh:
        return fh.read()


_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>CAD 预览 — __NAME__</title>
<style>html,body{margin:0;height:100%;background:#0e1116;color:#cdd6e4;
font-family:system-ui,"Microsoft YaHei",sans-serif;overflow:hidden}
#info{position:fixed;left:12px;top:10px;font-size:13px;line-height:1.5;
opacity:.85;z-index:2;pointer-events:none}
#info b{font-size:15px}#c{width:100%;height:100%;display:block}
#err{position:fixed;inset:0;display:none;place-items:center;text-align:center;
padding:24px;color:#ff9b9b;font-size:14px}</style>
<script type="importmap">
{ "imports": {
  "three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
  "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"
} }
</script>
</head><body>
<div id="info"><b>__NAME__</b><br>拖拽旋转 · 滚轮缩放 · 右键平移
<br>体积: __VOL__ · 面: __FACES__ · 边: __EDGES__<br>尺寸: __SIZE__</div>
<canvas id="c"></canvas>
<div id="err">3D 库加载失败（需要网络访问 CDN）。<br>请联网后重新打开本页面。</div>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';

const b64 = "__B64__";
const bin = atob(b64);
const bytes = new Uint8Array(bin.length);
for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0e1116);
const canvas = document.getElementById('c');
const cam = new THREE.PerspectiveCamera(45, innerWidth / innerHeight, 1e-3, 1e6);
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, logarithmicDepthBuffer: true });
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));

scene.add(new THREE.AmbientLight(0xffffff, 0.45));
const l1 = new THREE.DirectionalLight(0xffffff, 1.0); l1.position.set(1, 1, 1); scene.add(l1);
const l2 = new THREE.DirectionalLight(0x88aaff, 0.5); l2.position.set(-1, -0.5, -1); scene.add(l2);

const controls = new OrbitControls(cam, renderer.domElement);
controls.enableDamping = true;

try {
  const geo = new STLLoader().parse(bytes.buffer);
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({
    color: 0x6ea8fe, metalness: 0.35, roughness: 0.55 }));
  geo.computeBoundingBox();
  const b = geo.boundingBox, c = new THREE.Vector3(), s = new THREE.Vector3();
  b.getCenter(c); b.getSize(s);
  geo.translate(-c.x, -c.y, -c.z);
  scene.add(mesh);
  const r = Math.max(s.x, s.y, s.z) || 1;
  // Tighten near/far to the model's real scale: a huge near/far ratio is the
  // classic cause of Z-fighting flicker on fine features like threads.
  cam.near = Math.max(r / 1000, 1e-3);
  cam.far = r * 20;
  cam.updateProjectionMatrix();
  cam.position.set(0, -r * 2, r * 1.1);
  controls.target.set(0, 0, 0);
  controls.update();
} catch (e) {
  console.error(e);
  document.getElementById('err').style.display = 'grid';
}

function loop() {
  requestAnimationFrame(loop);
  controls.update();
  renderer.render(scene, cam);
}
loop();
addEventListener('resize', () => {
  cam.aspect = innerWidth / innerHeight;
  cam.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});
</script></body></html>"""


def make_preview(input_path: str, out_dir: str | None = None) -> dict:
    shape = cad_core.read_shape(input_path)
    props = cad_core.properties(shape)
    base = os.path.splitext(os.path.basename(input_path))[0]
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "previews")
    os.makedirs(out_dir, exist_ok=True)
    html = os.path.join(out_dir, base + "_preview.html")
    stl = os.path.join(out_dir, base + ".stl")
    stl_bytes = _mesh_to_file(shape, stl)
    b64 = base64.b64encode(stl_bytes).decode("ascii")

    bb = props["bounding_box"]["size"]
    topo = props["topology"]
    html_text = (_HTML
                 .replace("__NAME__", base)
                 .replace("__VOL__", f"{props['volume']:.4g}")
                 .replace("__FACES__", str(topo["faces"]))
                 .replace("__EDGES__", str(topo["edges"]))
                 .replace("__SIZE__", f"{bb[0]:.3g} x {bb[1]:.3g} x {bb[2]:.3g}")
                 .replace("__B64__", b64))
    with open(html, "w", encoding="utf-8") as f:
        f.write(html_text)
    with open(stl, "wb") as f:
        f.write(stl_bytes)
    return {"html": html, "stl": stl, "props": props,
            "html_kb": round(os.path.getsize(html) / 1024, 1),
            "stl_kb": round(len(stl_bytes) / 1024, 1)}


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else None
    if not src:
        print("usage: make_preview.py <input.step>")
        sys.exit(1)
    res = make_preview(src)
    print("HTML  :", res["html"], f"({res['html_kb']} KB)")
    print("STL   :", res["stl"], f"({res['stl_kb']} KB)")
    print("Props :", res["props"])
