"""TE7 — international (Chinese) filename handling (Windows GBK / UTF-8).

Confirms the toolchain can READ a CAD file whose name contains non-ASCII
characters and WRITE preview outputs whose names carry the same characters,
without encoding corruption. This is the classic Windows-cp936/GBK pitfall
area, so the test is explicit about round-tripping Chinese names through
read -> properties -> preview.
"""
from __future__ import annotations

import os
import shutil

import cad_core
import make_preview


def test_chinese_filename_read_and_preview(selftest_step, tmp_path):
    cn_src = tmp_path / "中文测试零件.step"
    shutil.copy(selftest_step, str(cn_src))
    assert os.path.exists(str(cn_src))

    # read a Chinese-named file back (exercises the OS file API on Windows)
    s = cad_core.read_shape(str(cn_src))
    p = cad_core.properties(s)
    assert p["volume"] > 0

    # preview into a Chinese-named output dir -> Chinese-named html/stl
    out_dir = tmp_path / "输出目录"
    res = make_preview.make_preview(str(cn_src), out_dir=str(out_dir))
    assert os.path.exists(res["html"])
    assert os.path.exists(res["stl"])
    assert os.path.getsize(res["html"]) > 0
    assert os.path.getsize(res["stl"]) > 0
    # the Chinese base name survives into the output file names
    assert "中文测试零件" in os.path.basename(res["html"])
    assert "中文测试零件" in os.path.basename(res["stl"])


def test_chinese_filename_pick_features(selftest_step, tmp_path):
    """feature_picker (interactive HTML) on a Chinese-named file.

    Skipped if the offline three.js vendor fails verification, so a vendoring
    issue does not look like an i18n regression.
    """
    import pytest
    import feature_picker

    cn_src = tmp_path / "特征_拾取测试.step"
    shutil.copy(selftest_step, str(cn_src))
    try:
        res = feature_picker.make_picker(str(cn_src), out_dir=str(tmp_path / "prev"))
    except RuntimeError as e:
        if "vendor" in str(e).lower():
            pytest.skip("offline three.js vendor verification unavailable")
        raise
    assert os.path.exists(res["html"])
    assert "特征_拾取测试" in os.path.basename(res["html"])
