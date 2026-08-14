#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pickable 3D feature preview (phase-1: file://, no server).

Builds on feature_locator's cylinder enumeration, but exports EVERY feature
as its own STL fragment (tagged with its id) plus the neutral body, then
emits a self-contained three.js HTML where clicking a cylindrical surface
identifies which feature (#N / P#) it is, shows its properties, and lets
the user copy a canonical edit command.

Phase-1 deliverable: works from a plain file:// open — no localhost server,
no network at view time. three.js is vendored locally under ./vendor/ and
copied next to the generated HTML, so the preview is fully offline. A fresh
checkout without the vendored files auto-downloads them once from the CDN.
Clicking highlights the feature and produces a copyable command the user
pastes back into the chat.

Usage:
    feature_picker.py <input.step> [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# Local three.js vendoring (offline, no CDN at view time).
THREE_VERSION = "0.160.0"
VENDOR_DIR = os.path.join(HERE, "vendor")
_VENDOR_FILES = [
    ("build/three.module.min.js", "three.module.min.js"),
    ("examples/jsm/controls/OrbitControls.js", "jsm/controls/OrbitControls.js"),
    ("examples/jsm/loaders/STLLoader.js", "jsm/loaders/STLLoader.js"),
]
sys.path.insert(0, HERE)
import cad_core  # noqa: E402
import feature_locator as fl  # noqa: E402

from OCP.BRepMesh import BRepMesh_IncrementalMesh  # noqa: E402
from OCP.StlAPI import StlAPI_Writer  # noqa: E402
from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.TopoDS import TopoDS_Compound  # noqa: E402
from OCP.BRepAdaptor import BRepAdaptor_Surface  # noqa: E402
from OCP.GeomAbs import GeomAbs_Cylinder  # noqa: E402


def _mesh_bytes(shape, tmp_path, deflection):
    """Mesh `shape` and return its binary STL bytes.

    Reuses a single temp path (overwritten each call) to avoid the
    sandbox safe-delete shim that blocks os.remove on Windows."""
    BRepMesh_IncrementalMesh(shape, deflection)
    w = StlAPI_Writer()
    w.ASCIIMode = False
    if not w.Write(shape, tmp_path):
        raise RuntimeError("STL mesh write failed")
    with open(tmp_path, "rb") as fh:
        return fh.read()


def _compound_of(faces):
    comp = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(comp)
    for f in faces:
        b.Add(comp, f)
    return comp


def _group_faces_by_radius(faces):
    """Split a composite feature's cylinder faces into concentric rings.

    Returns a list of (radius, [faces]) sorted by radius DESCENDING, so the
    k-th entry maps to sub-id #{gid}.{k}. Faces at the same rounded radius
    belong to the same physical ring and are merged."""
    groups = {}
    for f in faces:
        ad = BRepAdaptor_Surface(f)
        r = round(ad.Cylinder().Radius(), 4) if ad.GetType() == GeomAbs_Cylinder else 0.0
        groups.setdefault(r, []).append(f)
    return sorted(groups.items(), key=lambda kv: -kv[0])


def _group_faces_by_canonical_radius(c):
    """Split a composite feature's faces into rings keyed by the canonical
    radii already computed by feature detection (c['radii']), instead of
    re-deriving per-face cylinder radii (which mis-fits some faces, e.g. a
    large sleeve). Each face is assigned to the nearest canonical radius by
    its vertex-based mid-radius. Guarantees displayed ring radii == c['radii']."""
    radii = sorted(c["radii"], reverse=True)
    groups = {r: [] for r in radii}
    A = c["axis"]
    for f in c["faces"]:
        pts = fl._vertices_of(f)
        rad, _ = fl._radial_of(pts, A)
        rmid = (min(rad) + max(rad)) / 2.0
        best = min(radii, key=lambda cr: abs(cr - rmid))
        groups[best].append(f)
    # keep only non-empty rings (a canonical radius with no nearby face is
    # not a real, pickable ring and would yield an empty STL compound)
    return [(r, groups[r]) for r in radii if groups[r]]


def build(shape, out_dir):
    feats_all = fl.collect_features(shape)
    comps = fl.group_features(feats_all)
    singles, patterns = fl.detect_patterns(comps)
    fl.assign_ids(singles, patterns)

    props = cad_core.properties(shape)
    size = props["bounding_box"]["size"]
    maxdim = max(size) or 1.0
    deflection = max(min(maxdim / 800.0, 0.5), 1e-5)
    tmp = os.path.join(out_dir, "_frag_tmp.stl")

    body_b64 = base64.b64encode(_mesh_bytes(shape, tmp, deflection)).decode("ascii")

    feats = []
    for c in singles:
        # category-aware kind + colour
        if c["axis"] is None:
            kind = "surface"
        elif c["stype"] == "torus":
            kind = "fillet"
        else:
            kind = fl._classify(shape, c["loc3"], c["axis"], c["extent"])
        if kind == "hole":
            color = "#e63946"
        elif kind == "boss":
            color = "#2a7d3b"
        elif kind == "fillet":
            color = "#e08600"
        else:
            color = "#8a8f98"
        dtype = c["stype"] if kind == "surface" else kind
        base_fid = f"#{c['id']}" if isinstance(c["id"], int) else str(c["id"])
        if c["composite"] and c["stype"] in ("cylinder", "cone", "sphere", "torus"):
            # split composite (counterbore / stepped hole / multi-radius
            # fillet such as F7) into individual concentric rings, each
            # pickable as #{gid}.{ring}
            for k, (r, rfaces) in enumerate(_group_faces_by_canonical_radius(c), 1):
                comp = _compound_of(rfaces)
                b64 = base64.b64encode(_mesh_bytes(comp, tmp, deflection)).decode("ascii")
                # per-sub-ring centroid so each ring has its own 3D position
                # (parent's loc3 would give the same xyz to every ring → markers
                # overlap). Fall back to parent loc3 if BRepGProp fails.
                ring_loc = cad_core.centroid_of_faces(rfaces) or c["loc3"]
                feats.append({"id": f"{base_fid}.{k}", "gid": c["id"], "ring": k,
                              "type": dtype, "composite": True, "axis": c["axis"],
                              "radii": [round(r, 4)],
                              "extent": round(c["extent"], 3),
                              "location": [round(x, 3) for x in ring_loc],
                              "center": [round(x, 3) for x in ring_loc],
                              "b64": b64, "color": color})
        else:
            comp = _compound_of(c["faces"])
            b64 = base64.b64encode(_mesh_bytes(comp, tmp, deflection)).decode("ascii")
            feats.append({"id": base_fid, "gid": c["id"], "ring": 0,
                          "type": dtype, "composite": c["composite"],
                          "axis": c["axis"],
                          "radii": [round(r, 4) for r in c["radii"]],
                          "extent": round(c["extent"], 3),
                          "location": [round(x, 3) for x in c["loc3"]],
                          "center": [round(x, 3) for x in c["loc3"]],
                          "b64": b64, "color": color})
    for p in patterns:
        comp = _compound_of(p["faces"])
        b64 = base64.b64encode(_mesh_bytes(comp, tmp, deflection)).decode("ascii")
        feats.append({"id": p["id"], "gid": p["id"], "ring": 0,
                      "type": "bolt_pattern", "axis": p["axis"],
                      "radii": [round(p["radius"], 4)],
                      "extent": round(p["extent"], 3),
                      "center": [round(x, 3) for x in p["center"]],
                      "count": p["count"], "pitch": round(p["pitch"], 3),
                      "b64": b64, "color": "#8a6d3b"})
    return body_b64, feats, props


_HTML = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>特征拾取 — __NAME__</title>
<style>html,body{margin:0;height:100%;background:#0e1116;color:#cdd6e4;
font-family:system-ui,"Microsoft YaHei",sans-serif;overflow:hidden}
#info{position:fixed;left:12px;top:10px;font-size:13px;line-height:1.5;
opacity:.9;z-index:3;pointer-events:none}
#info b{font-size:15px}
#c{width:100%;height:100%;display:block}
#panel{position:fixed;right:12px;top:10px;width:268px;z-index:3;
background:#161b22ee;border:1px solid #2b3340;border-radius:10px;
padding:12px 14px;font-size:13px;line-height:1.5;display:none}
#panel h3{margin:0 0 6px;font-size:14px}
#panel .row{display:flex;justify-content:space-between;gap:8px;margin:2px 0}
#panel .k{color:#8b97a8}#panel .v{color:#e6edf6;text-align:right}
#panel select,#panel input{background:#0e1116;color:#e6edf6;border:1px solid #2b3340;
border-radius:6px;padding:4px 6px;font-size:13px}
#cmd{margin-top:8px;display:flex;gap:6px}
#cmd input{flex:1}
#cmd button,#panel button{cursor:pointer;background:#1f6feb;color:#fff;border:0;
border-radius:6px;padding:5px 10px;font-size:13px}
#ghost{position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:4;
cursor:pointer;background:#222c3a;color:#cdd6e4;border:1px solid #3a4658;
border-radius:6px;padding:6px 14px;font-size:13px}
#ghost.on{background:#1f6feb;color:#fff;border-color:#1f6feb}
#focusBtn{position:fixed;top:10px;left:calc(50% + 96px);z-index:4;
cursor:pointer;background:#222c3a;color:#cdd6e4;border:1px solid #3a4658;
border-radius:6px;padding:6px 14px;font-size:13px}
#focusBtn.on{background:#1f6feb;color:#fff;border-color:#1f6feb}
#cmdstr{margin-top:6px;font-family:ui-monospace,Consolas,monospace;color:#7ee787;
font-size:13px;word-break:break-all}
#list{position:fixed;left:12px;bottom:10px;z-index:3;max-height:42%;overflow:auto;
background:#161b22cc;border:1px solid #2b3340;border-radius:10px;padding:6px 8px;
font-size:12px;min-width:210px}
#list div{padding:2px 6px;border-radius:5px;cursor:pointer}
#list div:hover{background:#22303f}
#list div.sel{background:#1f6feb}
#err{position:fixed;inset:0;display:none;place-items:center;text-align:center;
padding:24px;color:#ff9b9b;font-size:14px;z-index:5}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;
vertical-align:middle}</style>
<script type="importmap">
{ "imports": {
  "three": "./vendor/three.module.min.js",
  "three/addons/": "./vendor/jsm/"
} }
</script></head><body>
<div id="info"><b>__NAME__</b> · 特征拾取<br>
拖拽旋转 · 滚轮缩放 · 右键平移<br>
<b style="color:#e63946">红</b>=孔 <b style="color:#2a7d3b">绿</b>=凸台
<b style="color:#e08600">橙</b>=圆角 <b style="color:#9aa0a6">灰</b>=平面/自由曲面
<b style="color:#8a6d3b">棕</b>=孔组
· 点任意特征面即选中</div>
<button id="ghost">半透明轮廓：关</button>
<button id="focusBtn">自动聚焦：关</button>
<canvas id="c"></canvas>
<div id="panel"></div>
<div id="list"></div>
<div id="err">3D 库加载失败（本地 three.js 缺失）。<br>请确认本页面同目录下的 vendor/ 文件夹完整。</div>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';

const BODY = "__BODY__";
const FEATS = __FEATS__;
const NAME = "__NAME__";

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0e1116);
const canvas = document.getElementById('c');
const cam = new THREE.PerspectiveCamera(45, innerWidth/innerHeight, 1e-3, 1e6);
const renderer = new THREE.WebGLRenderer({canvas, antialias:true, logarithmicDepthBuffer:true});
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
scene.add(new THREE.AmbientLight(0xffffff, 0.5));
const l1 = new THREE.DirectionalLight(0xffffff, 1.0); l1.position.set(1,1,1); scene.add(l1);
const l2 = new THREE.DirectionalLight(0x88aaff, 0.5); l2.position.set(-1,-0.5,-1); scene.add(l2);
const controls = new OrbitControls(cam, renderer.domElement);
controls.enableDamping = true;

function b64ToBytes(b64){
  const bin = atob(b64), a = new Uint8Array(bin.length);
  for (let i=0;i<bin.length;i++) a[i]=bin.charCodeAt(i);
  return a.buffer;
}
function addMesh(b64, color, fid, meta){
  const geo = new STLLoader().parse(b64ToBytes(b64));
  geo.computeBoundingBox();
  const m = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({
    color: color, metalness:0.3, roughness:0.55, transparent:true, opacity:0.0,
    depthWrite:false,
    polygonOffset:true, polygonOffsetFactor:-1, polygonOffsetUnits:-1,
    emissive:0x000000, emissiveIntensity:1.0}));
  m.userData = {fid, meta};
  // always visible=true so it is raycast-able; opacity:0 keeps it invisible
  // until hovered/selected (robust regardless of raycaster visibility rules)
  scene.add(m);
  return m;
}

let featureMeshes = [], selected = null, hovered = null;
// Body centre / size / radius are needed by the camera-focus routine, so
// hoist them out of the try block. The old code declared `c`/`s`/`r` with
// const *inside* try → they were out of scope for the selection marker /
// focus helper, which is why back-side highlights never drew.
let BC = new THREE.Vector3(), BS = new THREE.Vector3(), BR = 1, bodyObj = null;
let ghostBtn = null, ghostOn = false, bodyEdges = null;
try {
  // body (neutral, not pickable)
  const bgeo = new STLLoader().parse(b64ToBytes(BODY));
  bgeo.computeBoundingBox();
  const b = bgeo.boundingBox;
  b.getCenter(BC); b.getSize(BS);
  bgeo.translate(-BC.x,-BC.y,-BC.z);
  const body = new THREE.Mesh(bgeo, new THREE.MeshStandardMaterial({
    color:0x6ea8fe, metalness:0.35, roughness:0.55}));
  bodyObj = body;
  scene.add(body);

  // ---- ghost / X-ray toggle: semi-transparent body + contour edges ----
  ghostBtn = document.getElementById('ghost');
  let edgesBuilt = false;
  ghostBtn.onclick = () => {
    ghostOn = !ghostOn;
    if (ghostOn && !edgesBuilt) {
      bodyEdges = new THREE.LineSegments(
        new THREE.EdgesGeometry(bgeo, 20),
        new THREE.LineBasicMaterial({color:0x9fd0ff, transparent:true, opacity:0.85}));
      scene.add(bodyEdges); edgesBuilt = true;
    }
    body.material.transparent = ghostOn;
    body.material.opacity = ghostOn ? 0.12 : 1.0;
    body.material.depthWrite = !ghostOn;
    body.material.needsUpdate = true;
    if (bodyEdges) bodyEdges.visible = ghostOn;
    ghostBtn.textContent = '半透明轮廓：' + (ghostOn ? '开' : '关');
    ghostBtn.classList.toggle('on', ghostOn);
  };
  BR = Math.max(BS.x,BS.y,BS.z)||1;
  cam.near = Math.max(BR/1000,1e-3); cam.far = BR*20; cam.updateProjectionMatrix();
  cam.position.set(0,-BR*2,BR*1.1); controls.target.set(0,0,0); controls.update();

  for (const f of FEATS){
    const m = addMesh(f.b64, f.color, String(f.id), f);
    m.geometry.translate(-BC.x, -BC.y, -BC.z);   // 与 BODY 同款居中，避免高亮纵向错位
    featureMeshes.push(m);
  }
} catch(e){ console.error(e); document.getElementById('err').style.display='grid'; }

//  buildSelEdges + the depthTest-disabled mesh are enough to mark
//  the selected feature; the marker visually duplicated the outline.)

// ---- camera auto-focus on selection ---------------------------------
// Selecting a feature (from the list or by clicking it, even on the far
// side) smoothly orbits the camera so the feature faces the viewer and is
// neatly framed. This is what makes back-facing / interior features
// instantly visible instead of hunting for them behind the body.
let autoFocus = false;
const focusBtn = document.getElementById('focusBtn');
focusBtn.onclick = () => {
  autoFocus = !autoFocus;
  focusBtn.textContent = '自动聚焦：' + (autoFocus ? '开' : '关');
  focusBtn.classList.toggle('on', autoFocus);
};
let tween = null;
function animateCamera(toPos, toTgt, ms){
  controls.enabled = false;   // ignore stray input during the flight
  tween = { fromPos: cam.position.clone(), toPos: toPos.clone(),
            fromTgt: controls.target.clone(), toTgt: toTgt.clone(),
            t0: performance.now(), ms: ms || 650 };
}
// Focus the camera on a feature's OWN geometry (real sizes), not on the
// often-missing radii/extent metadata. Distance comes from the mesh bounding
// box diagonal; direction is chosen by looking ALONG a "thin" bounding-box
// axis. A dimension is thin when it is < 30% of the largest dimension. That
// single rule gives the right view for every shape:
//   plane / plate  : thin = surface normal  -> face seen head-on
//   disc / bore    : thin = its axis        -> circular opening head-on
//   wire / edge    : two thin dims           -> view perpendicular to it, so
//                     the whole edge is laid out (never end-on / a dot)
//   chunky block   : no thin dim             -> gentle 3/4 tilt to read as 3D
const AX = [new THREE.Vector3(1,0,0), new THREE.Vector3(0,1,0), new THREE.Vector3(0,0,1)];
function focusOn(mesh){
  if (!mesh) return;
  mesh.geometry.computeBoundingBox();
  const bb = mesh.geometry.boundingBox;
  const size = new THREE.Vector3(); bb.getSize(size);
  const center = new THREE.Vector3(); bb.getCenter(center);
  // geometry was translated by -BC, so its local center is already in the
  // same world frame as the body (mesh.position stays 0,0,0).
  const target = center.clone();
  // current view direction (camera → target)
  const cv = cam.position.clone().sub(controls.target);
  if (cv.lengthSq() < 1e-9) cv.set(0,-1,0.55);
  cv.normalize();

  const dims = [size.x, size.y, size.z];
  const maxDim = Math.max(...dims);
  const minIdx = dims.indexOf(Math.min(...dims));

  let dir;
  if (maxDim < 1e-3){
    // degenerate / empty mesh (e.g. F1.1 has a zero-size bbox): keep the
    // current viewing angle, only the distance reframes.
    dir = cv.clone();
  } else {
    // A dimension is "thin" when it is < 30% of the largest dimension.
    // Looking ALONG a thin axis is the key to a good view of every shape:
    //   plane / plate : thin = surface normal  -> face seen head-on
    //   disc / bore   : thin = its axis         -> circular opening head-on
    //   wire / edge   : two thin dims            -> perpendicular, the whole
    //                    edge is laid out (never end-on)
    const thinIdx = [];
    for (let i=0;i<3;i++) if (dims[i] < maxDim * 0.3) thinIdx.push(i);
    if (thinIdx.length >= 1){
      dir = AX[thinIdx[0]].clone();
    } else {
      // chunky block: face the smallest dim with a gentle 3/4 tilt so it
      // reads as 3D (angle ~22°, never edge-on).
      dir = AX[minIdx].clone();
      const tilt = AX[(minIdx+1)%3].clone()
                     .add(AX[(minIdx+2)%3].clone().multiplyScalar(0.6)).normalize();
      dir = dir.add(tilt.multiplyScalar(0.4)).normalize();
    }
  }
  if (dir.dot(cv) < 0) dir.negate();   // stay on the same side, no flip

  // Distance: fit the feature diagonal to ~55% of the viewport via real FOV
  // (a bit of breathing room so the part never feels crammed). Multiply is
  // intentionally > 1 (further away), tuned from 1.3 → 1.6 on user feedback.
  const diag = size.length();
  let dist = (diag/2) / Math.tan(THREE.MathUtils.degToRad(cam.fov/2)) * 1.6;
  dist = Math.min(BR*4.5, Math.max(BR*0.22, dist));   // sane bounds, no glue / no yank
  const toPos = target.clone().add(dir.multiplyScalar(dist));
  animateCamera(toPos, target, 650);
}

const ray = new THREE.Raycaster();
const ndc = new THREE.Vector2();
function pick(ev){
  const rect = canvas.getBoundingClientRect();
  ndc.x = ((ev.clientX-rect.left)/rect.width)*2-1;
  ndc.y = -((ev.clientY-rect.top)/rect.height)*2+1;
  ray.setFromCamera(ndc, cam);
  const hit = ray.intersectObjects(featureMeshes, false);
  return hit.length ? hit[0].object : null;
}
// ---- selected-feature X-ray outline (always-on-top wireframe) ----
// A single-sided feature mesh disappears when viewed from its back face
// (face culling), so the selection highlight was only visible head-on.
// Fix: make the selected mesh DoubleSide, AND draw an always-on-top edge
// outline so the feature is unmistakable from ANY viewing angle.
let selEdges = null;
function clearSelEdges(){
  if (selEdges){ scene.remove(selEdges); selEdges.geometry.dispose(); selEdges = null; }
}
function buildSelEdges(mesh){
  clearSelEdges();
  if (!mesh) return;
  const eg = new THREE.EdgesGeometry(mesh.geometry, 30);
  const mat = new THREE.LineBasicMaterial({
    color:0xffae42, transparent:true, opacity:0.95, depthTest:false});
  selEdges = new THREE.LineSegments(eg, mat);
  selEdges.renderOrder = 6;   // drawn above the selected mesh (renderOrder 5)
  scene.add(selEdges);
}

function setLevel(mesh, lvl){
  if (!mesh) return;
  if (lvl === 0){
    mesh.material.opacity = 0.0;
    mesh.material.emissive.setHex(0x000000);
    mesh.material.depthTest = true;   // back to normal occlusion
    mesh.material.side = THREE.FrontSide;
    mesh.renderOrder = 0;
  } else {
    mesh.material.opacity = 1.0;
    mesh.material.emissive.setHex(lvl===2 ? 0x995500 : 0x443300);
    if (lvl === 2){
      // selected → draw ON TOP of the solid body (real X-ray) and show both
      // faces so it stays visible even when viewed from its back side.
      mesh.material.depthTest = false;
      mesh.material.side = THREE.DoubleSide;
      mesh.renderOrder = 5;
    } else {
      mesh.material.depthTest = true;
      mesh.material.side = THREE.FrontSide;
      mesh.renderOrder = 0;
    }
  }
}

// Single entry point for selection so edges/marker/panel/focus stay in sync
// and don't get clobbered by hover (which only touches non-selected meshes).
function selectFeature(mesh){
  if (selected && selected !== mesh) setLevel(selected, 0);
  selected = mesh;
  if (selected) setLevel(selected, 2);
  renderPanel(selected);
  if (selected){
    buildSelEdges(selected);
    if (autoFocus) focusOn(selected);
  } else {
    clearSelEdges();
  }
}
canvas.addEventListener('pointermove', ev=>{
  const o = pick(ev);
  if (o === hovered) return;
  if (hovered && hovered !== selected) setLevel(hovered, 0);
  hovered = o;
  if (hovered && hovered !== selected) setLevel(hovered, 1);
  canvas.style.cursor = o ? 'pointer' : 'default';
});
canvas.addEventListener('click', ev=>{
  selectFeature(pick(ev));
});

// ---- side panel + command builder ----
function fmtDia(f){
  if (f.type==='bolt_pattern') return `Ø${ (2*f.radii[0]).toFixed(2) } ×${f.count}`;
  if (!f.radii || !f.radii.length) return '—';
  return f.radii.map(r=>`Ø${(2*r).toFixed(2)}`).join(' / ');
}
function typeName(f){
  if (f.type==='bolt_pattern') return '螺栓孔组';
  if (f.type==='fillet') return '圆角';
  if (f.type==='plane') return '平面';
  if (f.type==='freeform') return '自由曲面';
  if (f.type==='cone') return '锥孔/锥台';
  if (f.type==='sphere') return '球凹/球凸';
  return f.type==='hole' ? '孔' : '凸台';
}
function renderPanel(mesh){
  const p = document.getElementById('panel');
  if (!mesh){ p.style.display='none'; return; }
  const f = mesh.userData.meta;
  const extra = f.type==='bolt_pattern'
    ? `<div class="row"><span class="k">节圆</span><span class="v">Ø${(2*f.pitch).toFixed(2)}</span></div>
       <div class="row"><span class="k">数量</span><span class="v">${f.count} 个</span></div>`
    : (f.composite ? `<div class="row"><span class="k">复合</span><span class="v">阶梯沉孔</span></div>` : '');
  p.innerHTML = `<h3><span class="dot" style="background:${f.color}"></span>特征 ${f.id}</h3>
    <div class="row"><span class="k">类型</span><span class="v">${typeName(f)}</span></div>
    <div class="row"><span class="k">直径</span><span class="v">${fmtDia(f)}</span></div>
    <div class="row"><span class="k">轴线</span><span class="v">${f.axis}</span></div>
    <div class="row"><span class="k">轴向长</span><span class="v">${f.extent}</span></div>
    ${extra}
    <div class="row"><span class="k">位置</span><span class="v">(${f.location?f.location.join(', '):f.center.join(', ')})</span></div>
    <div id="cmd"><select id="op">
      <option value="enlarge">扩大直径</option>
      <option value="shrink">缩小直径</option>
      <option value="set">改为直径</option></select>
      <input id="val" type="number" value="2" step="0.1" style="width:64px"></div>
    <button id="copy">复制指令</button>
    <button id="refocus" style="margin-top:6px;background:#2a7d3b">聚焦此特征</button>
    <div id="cmdstr"></div>`;
  p.style.display='block';
  const op=document.getElementById('op'), val=document.getElementById('val'),
        cs=document.getElementById('cmdstr');
  function upd(){ cs.textContent = buildCmd(f.id, op.value, val.value, f); }
  op.onchange=upd; val.oninput=upd; upd();
  document.getElementById('refocus').onclick = ()=>{ focusOn(meshByFid(String(f.id))); };
  document.getElementById('copy').onclick = ()=>{
    const t = cs.textContent;
    navigator.clipboard.writeText(t).then(()=>{ cs.textContent = t + '  ✓ 已复制'; })
      .catch(()=>{ cs.textContent = t + '  (请手动复制)'; });
  };
}
function buildCmd(id, op, v, f){
  id = String(id); v = String(v);
  if (op==='enlarge') return `把 ${id} 直径扩大 ${v} 倍`;
  if (op==='shrink')  return `把 ${id} 直径缩小到 ${v} 分之一`;
  return `把 ${id} 直径改为 ${v}`;
}

// ---- feature list (reverse hover-sync) ----
const listEl = document.getElementById('list');
FEATS.forEach(f=>{
  const d=document.createElement('div');
  d.dataset.fid=String(f.id);
  d.innerHTML=`<span class="dot" style="background:${f.color}"></span>${f.id} ${typeName(f)} ${fmtDia(f)}`;
  d.onmouseenter=()=>{ const m=meshByFid(String(f.id)); if(m&&m!==selected) setLevel(m,1); hovered=m; };
  d.onmouseleave=()=>{ const m=meshByFid(String(f.id)); if(m&&m!==selected) setLevel(m,0); if(hovered===m)hovered=null; };
  d.onclick=()=>{
    selectFeature(meshByFid(String(f.id)));
  };
  listEl.appendChild(d);
});
function meshByFid(fid){ return featureMeshes.find(m=>m.userData.fid===fid); }

function loop(){
  requestAnimationFrame(loop);
  if (tween){
    const k = Math.min(1, (performance.now()-tween.t0)/tween.ms);
    const e = k<0.5 ? 2*k*k : 1-Math.pow(-2*k+2,2)/2;   // easeInOutQuad
    cam.position.lerpVectors(tween.fromPos, tween.toPos, e);
    controls.target.lerpVectors(tween.fromTgt, tween.toTgt, e);
    if (k>=1){ tween=null; controls.enabled = true; }
  }
  controls.update();
  renderer.render(scene,cam);
}
loop();
addEventListener('resize', ()=>{ cam.aspect=innerWidth/innerHeight; cam.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight); });
</script></body></html>"""


def _ensure_vendor():
    """Ensure the local three.js copy exists; download once if missing.

    The generated HTML references ./vendor/ relatively, so three.js is fully
    offline at view time. If the vendored files are absent (fresh checkout or a
    package that didn't ship them), fetch them once from the CDN and cache under
    VENDOR_DIR. Subsequent runs and packaged copies skip the download.
    """
    if os.path.exists(os.path.join(VENDOR_DIR, "three.module.min.js")):
        return
    print(f"[feature_picker] 本地 three.js 缺失，正在从 CDN 下载 r{THREE_VERSION} …",
          file=sys.stderr)
    os.makedirs(VENDOR_DIR, exist_ok=True)
    for src, dst in _VENDOR_FILES:
        url = f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/{src}"
        out = os.path.join(VENDOR_DIR, dst)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as r, open(out, "wb") as f:
            f.write(r.read())
    print("[feature_picker] three.js 已本地化到 vendor/。", file=sys.stderr)


def make_picker(input_path, out_dir=None):
    shape = cad_core.read_shape(input_path)
    base = os.path.splitext(os.path.basename(input_path))[0]
    out_dir = out_dir or os.path.join(HERE, "previews")
    os.makedirs(out_dir, exist_ok=True)
    _ensure_vendor()
    # Copy the vendored three.js next to the HTML so ./vendor/ resolves offline.
    out_vendor = os.path.join(out_dir, "vendor")
    if os.path.exists(out_vendor):
        shutil.rmtree(out_vendor)
    shutil.copytree(VENDOR_DIR, out_vendor)
    body_b64, feats, props = build(shape, out_dir)

    bb = props["bounding_box"]["size"]
    topo = props["topology"]
    html_text = (_HTML
                 .replace("__NAME__", base)
                 .replace("__BODY__", body_b64)
                 .replace("__FEATS__", json.dumps(feats, ensure_ascii=False)))
    # also drop the leftover placeholder if name appears twice
    html_text = html_text.replace("__NAME__", base)
    html_path = os.path.join(out_dir, base + "_拾取.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    return {"html": html_path, "feature_count": len(feats),
            "features": [{"id": f["id"], "gid": f["gid"], "ring": f["ring"],
                          "type": f["type"], "radii": f["radii"],
                          "axis": f["axis"]} for f in feats],
            "props": {"volume": props["volume"], "faces": topo["faces"],
                      "edges": topo["edges"], "size": bb}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--out-dir", default=os.path.join(HERE, "previews"))
    args = ap.parse_args()
    res = make_picker(args.input, args.out_dir)
    print(json.dumps(res, ensure_ascii=False, indent=2))
