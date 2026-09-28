r"""打包 exe：dist\sha.exe（命令行）+ dist\ShaDesktop.exe（桌面端，无控制台）。

内置插件的 register.py 由 loader 按文件路径加载，必须以数据文件形式打包。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
WORK = ROOT / "build_pyi"

# nanoagent 是 PEP 660 editable 安装（__editable__ finder），PyInstaller 解析不到
# 它的子模块 —— 必须把真实源码目录显式加进 --paths（动态解析，不硬编码）。
import nanoagent  # noqa: E402

NANO_SRC = Path(nanoagent.__file__).resolve().parent.parent

COMMON = [
    "--noconfirm", "--clean",
    "--paths", str(ROOT),
    "--paths", str(NANO_SRC),
    "--collect-submodules", "harness",
    "--collect-submodules", "nanoagent",
    "--collect-all", "webview",
    "--collect-all", "clr_loader",
    "--collect-all", "pythonnet",
    "--add-data", str(ROOT / "harness" / "builtins") + ";harness/builtins",
    "--icon", str(ROOT / "assets" / "sha.ico"),
    "--distpath", str(DIST),
    "--workpath", str(WORK),
    "--specpath", str(WORK),
]

BUILDS = [
    ("sha", ["--onefile", str(ROOT / "tools" / "launcher_sha.py")]),
    ("ShaDesktop", ["--onefile", "--noconsole",
                    str(ROOT / "tools" / "launcher_desktop.py")]),
]

for name, extra in BUILDS:
    print(f"==> building {name}.exe")
    # check=False：下一行手工检查 returncode 并给出带名字的错误
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--name", name, *COMMON, *extra],
        check=False,
    )
    if result.returncode != 0:
        sys.exit(f"{name} build failed: {result.returncode}")

print("DONE:", DIST)
