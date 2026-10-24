"""打包脚本：python build.py [--zip]

产出 dist/AIInterviewer/ 绿色版目录，加 --zip 会额外压成可直接分发的 zip。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "AIInterviewer"

EXCLUDES = [
    "tkinter", "matplotlib", "pandas", "scipy", "pytest",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtTest", "PySide6.QtDesigner",
]

ADD_DATA = [
    ("app/ui/qss", "app/ui/qss"),
]

HIDDEN = ["sounddevice", "_cffi_backend", "miniaudio"]


def build() -> Path:
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed",
        "--onedir",
        "--name", APP_NAME,
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
    ]
    for source, target in ADD_DATA:
        args += ["--add-data", f"{ROOT / source}{';' if sys.platform == 'win32' else ':'}{target}"]
    for module in HIDDEN:
        args += ["--hidden-import", module]
    for module in EXCLUDES:
        args += ["--exclude-module", module]

    icon = ROOT / "resources" / "app.ico"
    if icon.exists():
        args += ["--icon", str(icon)]

    args.append(str(ROOT / "run.py"))
    print("执行：", " ".join(args))
    subprocess.run(args, check=True, cwd=ROOT)
    return DIST / APP_NAME


def make_zip(target: Path) -> Path:
    archive = DIST / f"{APP_NAME}-green.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for item in target.rglob("*"):
            bundle.write(item, item.relative_to(target.parent))
    return archive


def main() -> int:
    if BUILD.exists():
        shutil.rmtree(BUILD, ignore_errors=True)
    target = build()
    print(f"\n打包完成：{target}")
    print(f"启动文件：{target / (APP_NAME + '.exe')}")
    if "--zip" in sys.argv:
        archive = make_zip(target)
        size = archive.stat().st_size / 1024 / 1024
        print(f"压缩包：{archive}（{size:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
