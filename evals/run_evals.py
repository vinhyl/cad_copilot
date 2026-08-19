#!/usr/bin/env python3
"""D9 eval runner: replay golden trajectories + geometry assertions.

The golden trajectory of an eval instruction is a DETERMINISTIC tool-call
sequence (no LLM involved), so the assertion layer runs fully automated
today. The LLM layer plugs in later: an agent session replays the same
instructions and its ACTUAL trajectories are compared against these goldens
(four-layer metrics per ADR-0002 D9).

Usage:
    venv/Scripts/python evals/run_evals.py --list
    venv/Scripts/python evals/run_evals.py                # run all goldens
    venv/Scripts/python evals/run_evals.py --run E001     # one instruction

Exit code: 0 all pass, 1 any failure. Geometry assertions never estimate
(D8): every check is a measured comparison against the expected value.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

# The runner builds its own fixture assembly on the fly: deterministic and
# identical to the golden trajectories recorded against it.
from _assembly_helpers import build_assembly_doc, write_assembly_step  # noqa: E402
import cad_assembly  # noqa: E402
import cad_core  # noqa: E402


def _make_workspace() -> str:
    td = tempfile.mkdtemp(prefix="eval_")
    step = os.path.join(td, "eval_asm.step")
    write_assembly_step(build_assembly_doc(), step)
    return td


# --- trajectory step executors (map eval tool names to library calls) ---

def exec_step(step: dict, ctx: dict) -> None:
    tool = step["tool"]
    if tool == "parse_assembly":
        ctx["manifest"] = cad_assembly.build_cache(
            ctx["step_path"], os.path.join(ctx["ws"], "cache"))
        ctx["cache_dir"] = os.path.join(ctx["ws"], "cache")
    elif tool == "edit":
        body = dict(step.get("args", {}))
        ctx["shape"] = cad_core.read_shape(
            os.path.join(ctx["cache_dir"], "parts", f"{body['template_id']}.step"))
        ctx["new_shape"] = cad_assembly.apply_template_edit(
            ctx["shape"], body["operation"], body.get("params", {}))
    elif tool == "feature_edit":
        body = dict(step.get("args", {}))
        feats = json.load(open(
            os.path.join(ctx["cache_dir"], "features",
                         f"{body['template_id']}.json"), encoding="utf-8"))
        feat = next(f for f in feats if f["id"] == body["feature_id"])
        ctx["shape"] = cad_core.read_shape(
            os.path.join(ctx["cache_dir"], "parts", f"{body['template_id']}.step"))
        ctx["new_shape"] = cad_assembly.apply_feature_edit(
            ctx["shape"], feat, body["operation"], body.get("params", {}))
    else:
        raise ValueError(f"unknown tool in golden trajectory: {tool}")


# --- assertion checkers ---

def check_assertion(a: dict, ctx: dict) -> tuple:
    kind = a["type"]
    if kind == "volume_delta":
        v0 = cad_core.properties(ctx["shape"])["volume"]
        v1 = cad_core.properties(ctx["new_shape"])["volume"]
        got = v0 - v1
        return a["expect_min"] <= got <= a["expect_max"], \
            f"Δvolume={got:.2f} in [{a['expect_min']}, {a['expect_max']}]"
    if kind == "feature_radius":
        feats = json.load(open(
            os.path.join(ctx["cache_dir"], "features",
                         f"{ctx['tid']}.json"), encoding="utf-8"))
        f = next(x for x in feats if x["id"] == a["feature_id"])
        got = max(f["radii"])
        return abs(got - a["expect"]) < 1e-6, f"{a['feature_id']} R={got} == {a['expect']}"
    if kind == "interference_rejected":
        hits = cad_assembly.check_interference(
            ctx["manifest"], ctx["shapes"], edited_template=ctx["tid"],
            edited_shape=ctx["new_shape"])
        return len(hits) == a["expect_count"], \
            f"interferences={len(hits)} == {a['expect_count']}"
    raise ValueError(f"unknown assertion type: {kind}")


def run_one(item: dict) -> list:
    """Run one instruction's golden trajectory + assertions.
    Returns [(name, ok, detail)]."""
    ctx = {"ws": _make_workspace(), "tid": None, "shapes": {}}
    ctx["step_path"] = os.path.join(ctx["ws"], "eval_asm.step")
    results = []
    try:
        for step in item.get("golden_trajectory", []):
            exec_step(step, ctx)
        # expose shapes for interference assertions
        ctx["shapes"] = cad_assembly.template_shapes_from_cache(
            ctx["cache_dir"], ctx["manifest"])
        tid = None
        for step in item.get("golden_trajectory", []):
            if step["tool"] in ("edit", "feature_edit"):
                tid = step["args"]["template_id"]
        ctx["tid"] = tid
        for i, a in enumerate(item.get("assertions", []), 1):
            ok, detail = check_assertion(a, ctx)
            results.append((f"{item['id']}.A{i}({a['type']})", ok, detail))
    except Exception as e:  # noqa: BLE001
        results.append((f"{item['id']}.trajectory", False, f"error: {e}"))
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--run", metavar="ID")
    args = ap.parse_args()

    with open(os.path.join(HERE, "instructions.yaml"), encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # example items are FORMAT references (descriptive trajectories) -- only
    # team instructions with executable golden trajectories run automatically.
    items = [i for i in data["instructions"]
             if i.get("source") == "team" and i.get("status") in
             ("annotated", "verified")]

    if args.list:
        for it in items:
            print(f"{it['id']:6} [{it['source']:7}] {it['instruction']}")
            print(f"       轨迹 {len(it.get('golden_trajectory', []))} 步 · "
                  f"断言 {len(it.get('assertions', []))} 条 · {it['status']}")
        return 0

    if args.run:
        items = [i for i in items if i["id"] == args.run] or items

    all_ok = True
    for it in items:
        for name, ok, detail in run_one(it):
            print(f"{'PASS' if ok else 'FAIL'}  {name}  -- {detail}")
            all_ok &= ok
    print("\n" + ("=== ALL PASS ===" if all_ok else "=== FAILURES ==="))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
