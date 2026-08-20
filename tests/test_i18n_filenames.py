"""TE7 — international (Chinese) filename handling (Windows GBK / UTF-8).

Confirms the toolchain can READ and WRITE CAD files whose names contain
non-ASCII characters without encoding corruption. This is the classic
Windows-cp936/GBK pitfall area, so the test is explicit about round-tripping
Chinese names through read -> properties -> write.
(The historical preview-outlet variants were retired with the static-HTML
previews; the Web upload/download paths carry the same i18n burden now.)
"""
from __future__ import annotations

import os
import shutil

import cad_core


def test_chinese_filename_read_roundtrip(selftest_step, tmp_path):
    cn_src = tmp_path / "中文测试零件.step"
    shutil.copy(selftest_step, str(cn_src))
    assert os.path.exists(str(cn_src))

    # read a Chinese-named file back (exercises the OS file API on Windows)
    s = cad_core.read_shape(str(cn_src))
    p = cad_core.properties(s)
    assert p["volume"] > 0

    # write into a Chinese-named output dir -> Chinese-named output file
    out_dir = tmp_path / "输出目录"
    out_dir.mkdir()
    out = str(out_dir / "中文输出.step")
    cad_core.write_shape(s, out)
    assert os.path.getsize(out) > 0
    # the Chinese base name survives into the output file name
    s2 = cad_core.read_shape(out)
    assert abs(cad_core.properties(s2)["volume"] - p["volume"]) < max(1.0, p["volume"] * 0.05)
