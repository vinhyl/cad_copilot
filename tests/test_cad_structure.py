"""Targeted tests for 结构操作 (sub-assembly hierarchy editing):

  cad_assembly.structure_from_steps  -- 草稿步骤表 -> 结构最终态
  cad_assembly.apply_structure       -- manifest 上应用 reparent / group_create / dissolve

These are pure-Python (no OCP), so they run on synthetic manifest dicts.
A node is: {id, name, type: "assembly"|"part", matrix, children:[...]}.
"""
from __future__ import annotations

import pytest

import cad_assembly


def make_manifest():
    """确定性装配树（3 层）：
       n0 root(assembly)
       ├── n1 SubA(assembly)
       │     ├── n2 P1(part)
       │     └── n3 P2(part)
       └── n4 P3(part)
    """
    def node(nid, name, typ, children, matrix=None):
        return {"id": nid, "name": name, "type": typ,
                "matrix": matrix, "children": children}
    # 世界系累积矩阵（只测试"保留"，数值不必真实）
    root = node("n0", "root", "assembly", [
        node("n1", "SubA", "assembly", [
            node("n2", "P1", "part", [], matrix=[[1, 0, 0, 10], [0, 1, 0, 0], [0, 0, 1, 0]]),
            node("n3", "P2", "part", [], matrix=[[1, 0, 0, 20], [0, 1, 0, 0], [0, 0, 1, 0]]),
        ], matrix=[[1, 0, 0, 5], [0, 1, 0, 0], [0, 0, 1, 0]]),
        node("n4", "P3", "part", [], matrix=[[1, 0, 0, 30], [0, 1, 0, 0], [0, 0, 1, 0]]),
    ], matrix=[[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]])
    return {"root": root, "templates": []}


def child_ids(node):
    return [c["id"] for c in node.get("children", [])]


# --------------------------------------------------------------------------
# structure_from_steps
# --------------------------------------------------------------------------

def test_reparent_last_write_wins():
    steps = [
        {"operation": "reparent", "node_id": "n2", "params": {"parent_id": "n4"}},
        {"operation": "reparent", "node_id": "n2", "params": {"parent_id": "n0"}},
    ]
    s = cad_assembly.structure_from_steps(steps)
    assert s["reparents"] == {"n2": "n0"}


def test_group_create_dedup_rejects_dup():
    steps = [
        {"operation": "group_create", "node_id": "g1", "params": {"name": "A"}},
        {"operation": "group_create", "node_id": "g1", "params": {"name": "B"}},
    ]
    with pytest.raises(ValueError):
        cad_assembly.structure_from_steps(steps)


def test_group_create_default_name():
    s = cad_assembly.structure_from_steps(
        [{"operation": "group_create", "node_id": "g1", "params": {}}])
    assert s["groups"] == [{"id": "g1", "name": "g1", "parent_id": None}]


def test_dissolve_is_a_set():
    s = cad_assembly.structure_from_steps(
        [{"operation": "group_dissolve", "node_id": "n1"},
         {"operation": "group_dissolve", "node_id": "n1"}])
    assert s["dissolves"] == {"n1"}


# --------------------------------------------------------------------------
# apply_structure: reparent
# --------------------------------------------------------------------------

def test_reparent_moves_node_keeps_matrices():
    m = make_manifest()
    out = cad_assembly.apply_structure(m, {"reparents": {"n2": "n0"}})
    # n1 只剩 P2；n2 上提到根（追加到根的子列表尾部）
    assert child_ids(out["root"]["children"][0]) == ["n3"]
    assert [c["id"] for c in out["root"]["children"]] == ["n1", "n4", "n2"]
    # 世界位形不变：被移动节点 matrix 保持原值
    n2 = next(c for c in out["root"]["children"] if c["id"] == "n2")
    assert n2["matrix"] == [[1, 0, 0, 10], [0, 1, 0, 0], [0, 0, 1, 0]]
    # 深拷贝：不改原 manifest
    assert child_ids(m["root"]["children"][0]) == ["n2", "n3"]


def test_reparent_order_independent_detach_then_attach():
    m = make_manifest()
    # n1 上提到根、n2 保持挂在 n1 下 —— 先全摘、再按输入序挂载，
    # 因此不依赖输入顺序，n2 最终仍在 n1 下。
    out = cad_assembly.apply_structure(m, {"reparents": {"n1": "n0", "n2": "n1"}})
    n1 = next(c for c in out["root"]["children"] if c["id"] == "n1")
    # n3 保持原地；n2 被摘后再挂回 n1 尾部
    assert child_ids(n1) == ["n3", "n2"]


# --------------------------------------------------------------------------
# apply_structure: group_create / dissolve
# --------------------------------------------------------------------------

def test_group_create_empty_assembly():
    m = make_manifest()
    out = cad_assembly.apply_structure(
        m, {"groups": [{"id": "g1", "name": "新分组", "parent_id": "n0"}]})
    n0 = out["root"]
    g1 = next(c for c in n0["children"] if c["id"] == "g1")
    assert g1["type"] == "assembly"
    assert g1["name"] == "新分组"
    assert g1.get("synthetic") is True
    assert g1["children"] == []


def test_group_create_then_reparent_inside():
    m = make_manifest()
    out = cad_assembly.apply_structure(
        m, {"groups": [{"id": "g1", "name": "组", "parent_id": "n0"}],
            "reparents": {"n2": "g1", "n4": "g1"}})
    g1 = next(c for c in out["root"]["children"] if c["id"] == "g1")
    assert child_ids(g1) == ["n2", "n4"]
    assert child_ids(out["root"]["children"][0]) == ["n3"]  # SubA 只剩 P2


def test_dissolve_promotes_children():
    m = make_manifest()
    out = cad_assembly.apply_structure(m, {"dissolves": {"n1"}})
    # n1 被移除，其子 n2、n3 上提到根（追加到父尾部）
    root_ids = [c["id"] for c in out["root"]["children"]]
    assert "n1" not in root_ids
    assert root_ids == ["n4", "n2", "n3"]


def test_group_then_dissolve_self_cancels():
    m = make_manifest()
    out = cad_assembly.apply_structure(
        m, {"groups": [{"id": "g1", "name": "组", "parent_id": "n0"}],
            "dissolves": {"g1"}})
    assert "g1" not in [c["id"] for c in out["root"]["children"]]


# --------------------------------------------------------------------------
# apply_structure: validation errors
# --------------------------------------------------------------------------

def test_reparent_root_rejected():
    m = make_manifest()
    with pytest.raises(ValueError):
        cad_assembly.apply_structure(m, {"reparents": {"n0": "n1"}})


def test_reparent_unknown_node_rejected():
    m = make_manifest()
    with pytest.raises(ValueError):
        cad_assembly.apply_structure(m, {"reparents": {"zzz": "n0"}})


def test_reparent_unknown_parent_rejected():
    m = make_manifest()
    with pytest.raises(ValueError):
        cad_assembly.apply_structure(m, {"reparents": {"n2": "zzz"}})


def test_reparent_to_part_rejected():
    m = make_manifest()
    with pytest.raises(ValueError):
        cad_assembly.apply_structure(m, {"reparents": {"n2": "n4"}})


def test_reparent_onto_own_descendant_rejected():
    # A1 移到自身后代 A2 下
    root = {"id": "r0", "name": "root", "type": "assembly", "children": [
        {"id": "A1", "name": "A1", "type": "assembly",
         "matrix": None, "children": [
             {"id": "A2", "name": "A2", "type": "assembly",
              "matrix": None, "children": []}]}]}
    m = {"root": root, "templates": []}
    with pytest.raises(ValueError):
        cad_assembly.apply_structure(m, {"reparents": {"A1": "A2"}})


def test_reparent_cycle_rejected():
    # A1 -> A2 同时 A2 -> A1（真实环，互挂无限递归）
    root = {"id": "r0", "name": "root", "type": "assembly", "children": [
        {"id": "A1", "name": "A1", "type": "assembly",
         "matrix": None, "children": [
             {"id": "A2", "name": "A2", "type": "assembly",
              "matrix": None, "children": []}]}]}
    m = {"root": root, "templates": []}
    with pytest.raises(ValueError):
        cad_assembly.apply_structure(m, {"reparents": {"A1": "A2", "A2": "A1"}})


def test_group_create_dup_with_existing_node_rejected():
    m = make_manifest()
    with pytest.raises(ValueError):
        cad_assembly.apply_structure(m, {"groups": [{"id": "n1", "name": "x"}]})


def test_group_create_under_part_rejected():
    m = make_manifest()
    with pytest.raises(ValueError):
        cad_assembly.apply_structure(m, {"groups": [{"id": "g1", "parent_id": "n4"}]})


def test_dissolve_root_rejected():
    m = make_manifest()
    with pytest.raises(ValueError):
        cad_assembly.apply_structure(m, {"dissolves": {"n0"}})


def test_dissolve_part_rejected():
    m = make_manifest()
    with pytest.raises(ValueError):
        cad_assembly.apply_structure(m, {"dissolves": {"n2"}})


def test_dissolve_unknown_node_rejected():
    m = make_manifest()
    with pytest.raises(ValueError):
        cad_assembly.apply_structure(m, {"dissolves": {"zzz"}})


# --------------------------------------------------------------------------
# draft_step_title branches
# --------------------------------------------------------------------------

def test_title_reparent_group_create_dissolve():
    m = make_manifest()
    assert cad_assembly.draft_step_title(
        {"operation": "reparent", "node_id": "n2", "params": {"parent_id": "n0"}}, m) \
        == "P1: 层级 移至「root」"
    assert cad_assembly.draft_step_title(
        {"operation": "group_create", "node_id": "g1", "params": {"name": "新分组"}}, m) \
        == "新建分组「新分组」"
    assert cad_assembly.draft_step_title(
        {"operation": "group_dissolve", "node_id": "n1"}, m) == "解散分组「SubA」"