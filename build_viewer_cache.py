"""Pre-build the assembly cache for the espresso machine STEP.

Mirrors cad_service's parse endpoint: writes workspace/cache/<sha256[:16]>/
with tree_structure.json + gltf_library + parts + features. Running this
ahead of the service means the viewer opens instantly (cache_hit) instead
of blocking on first load.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cad_assembly

SRC = "/Users/vvvvvv/Library/CloudStorage/OneDrive-个人/espresso maker/开发/图纸/2.0咖啡机图纸资料/2.0咖啡机图纸260129.STEP"
REPO = os.path.dirname(os.path.abspath(__file__))
CACHE_ROOT = os.path.join(REPO, "workspace", "cache")

key = cad_assembly._sha256_file(SRC)[:16]
cache_dir = os.path.join(CACHE_ROOT, key)
os.makedirs(cache_dir, exist_ok=True)
print("cache_key:", key)
print("cache_dir:", cache_dir)

t0 = time.time()
manifest = cad_assembly.build_cache(SRC, cache_dir)
dt = time.time() - t0
print("BUILD OK in %.1fs" % dt)
print("templates:", len(manifest["templates"]))
print("tree written:", os.path.isfile(os.path.join(cache_dir, "tree_structure.json")))
print("gltf files:", len([f for f in os.listdir(os.path.join(cache_dir, "gltf_library")) if f.endswith(".gltf")]))
