# -*- coding: utf-8 -*-
"""把 image_compressor.pyw 打包成单文件 exe（Windows 10 / 11，x64）。

用法
    python -m pip install pyinstaller
    python build_exe.py

产物
    ..\\release\\图片压缩工具.exe

要点
    - tkinterdnd2 自带多个平台的 tkdnd 二进制，PyInstaller 没有对应 hook，
      必须手工把当前平台所需的那一份挂载进包内（见 add_data 参数）。
    - tkdnd 目录名随 Tcl 主版本变化：Tcl 8 -> win-x64，Tcl 9 -> win-x64-tcl9。
      Python 3.13+ 自带 Tcl/Tk 9，因此实际取到的是 -tcl9 那一份。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RELEASE = ROOT / "release"
WORK = ROOT / ".buildenv" / "pyi"
SPEC = ROOT / ".buildenv" / "spec"
APP_NAME = "图片压缩工具"
ENTRY = HERE / "image_compressor.pyw"
ICON = HERE / "app.ico"
VERSION_FILE = HERE / "version_info.txt"


def tkdnd_data_spec() -> str:
    """返回 --add-data 的值：把当前平台需要的 tkdnd 目录挂进包里。"""
    import tkinter
    import tkinterdnd2

    machine = os.environ.get("PROCESSOR_ARCHITECTURE", platform.machine()).upper()
    arch = {"AMD64": "win-x64", "X86": "win-x86", "ARM64": "win-arm64"}.get(machine)
    if arch is None:
        raise SystemExit("不支持的处理器架构: %s" % machine)

    sub = arch + ("-tcl9" if int(tkinter.TkVersion) >= 9 else "")
    src = Path(tkinterdnd2.__file__).resolve().parent / "tkdnd" / sub
    if not src.is_dir():
        raise SystemExit("未找到 tkdnd 资源目录: %s" % src)
    return "%s;tkinterdnd2/tkdnd/%s" % (src, sub)


def clean_previous() -> None:
    for p in (RELEASE / APP_NAME, RELEASE / (APP_NAME + ".exe")):
        if p.is_file():
            p.unlink()
    for d in (WORK, SPEC):
        if d.is_dir():
            shutil.rmtree(d)


def main() -> int:
    if not ENTRY.is_file():
        raise SystemExit("找不到入口文件: %s" % ENTRY)

    RELEASE.mkdir(parents=True, exist_ok=True)
    clean_previous()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",                          # 不弹控制台窗口
        "--name", APP_NAME,
        "--distpath", str(RELEASE),
        "--workpath", str(WORK),
        "--specpath", str(SPEC),
        "--add-data", tkdnd_data_spec(),
        # 用不到的重型模块，排除以缩小体积
        "--exclude-module", "numpy",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "pandas",
        "--exclude-module", "pytest",
        "--exclude-module", "setuptools",
        "--exclude-module", "pip",
    ]
    if ICON.is_file():
        cmd += ["--icon", str(ICON)]
    if VERSION_FILE.is_file():
        cmd += ["--version-file", str(VERSION_FILE)]
    cmd.append(str(ENTRY))

    print(" ".join('"%s"' % c if " " in c else c for c in cmd))
    print()
    rc = subprocess.call(cmd)
    if rc != 0:
        return rc

    out = RELEASE / (APP_NAME + ".exe")
    if not out.is_file():
        print("构建结束但未找到产物:", out)
        return 1
    print()
    print("完成 -> %s  (%.1f MB)" % (out, out.stat().st_size / 1048576.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
