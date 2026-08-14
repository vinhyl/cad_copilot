#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-platform bootstrap: create the venv and install dependencies.

Works on Windows and macOS (and Linux) with one command:

    python bootstrap.py

After that, run the tools with the venv's python, e.g.:

    venv/Scripts/python feature_picker.py model.step --out-dir previews   # Windows
    venv/bin/python        feature_picker.py model.step --out-dir previews  # macOS/Linux

three.js is vendored under ./vendor/ and needs no network; only the Python
deps below require a one-time PyPI fetch.
"""
from __future__ import annotations

import os
import subprocess
import sys

# 强制 stdout/stderr 使用 UTF-8，规避 Windows 控制台(cp1252)下打印中文触发
# UnicodeEncodeError 的问题——CI 的 windows runner 默认非 UTF-8 控制台。
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    venv_dir = os.path.join(HERE, "venv")
    if not os.path.isdir(venv_dir):
        print(f"[bootstrap] 创建虚拟环境 {venv_dir} …")
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
    else:
        print("[bootstrap] 虚拟环境已存在，跳过创建。")

    if os.name == "nt":
        pip = os.path.join(venv_dir, "Scripts", "pip.exe")
        python = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        pip = os.path.join(venv_dir, "bin", "pip")
        python = os.path.join(venv_dir, "bin", "python")

    print("[bootstrap] 升级 pip …")
    subprocess.check_call([pip, "install", "--upgrade", "pip"])
    print("[bootstrap] 安装依赖 (requirements.txt) …")
    subprocess.check_call([pip, "install", "-r", os.path.join(HERE, "requirements.txt")])

    print("\n[bootstrap] 完成。试跑示例：")
    print(f"  {python} feature_picker.py selftest.step --out-dir previews")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
