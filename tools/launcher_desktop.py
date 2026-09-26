"""窗口入口：ShaDesktop.exe —— 双击直接打开桌面端（无控制台）。"""
import sys

from harness.cli import main

sys.exit(main(["desktop"]))
