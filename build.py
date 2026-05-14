#!/usr/bin/env python3
"""
打包脚本（跨平台版），等效于 build.bat
在 Windows 上运行: python build.py
"""

import subprocess
import sys
import os
from pathlib import Path

ROOT = Path(__file__).parent


def run(cmd, **kwargs):
    print(f"\n>>> {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    result = subprocess.run(cmd, shell=isinstance(cmd, str), **kwargs)
    if result.returncode != 0:
        print(f"[ERROR] 命令失败，退出码: {result.returncode}")
        sys.exit(result.returncode)


def main():
    print("=" * 50)
    print(" PSD Viewer — PyInstaller 打包")
    print("=" * 50)

    # 1. 安装依赖
    print("\n[1/2] 安装依赖…")
    run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])

    # 2. 打包
    print("\n[2/2] 打包中…")
    icon_path = ROOT / "assets" / "icon.ico"
    icon_arg = f"--icon={icon_path}" if icon_path.exists() else ""

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "PSD_Viewer",
        "--add-data", f"src{os.pathsep}src",
        "--hidden-import", "psd_tools",
        "--hidden-import", "psd_tools.composite",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.ImageQt",
        "--collect-all", "psd_tools",
    ]
    if icon_arg:
        cmd.append(icon_arg)
    cmd.append("src/main.py")

    run(cmd, cwd=ROOT)

    print("\n[完成] dist/PSD_Viewer.exe 已生成！")


if __name__ == "__main__":
    main()
