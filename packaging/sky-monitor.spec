# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build of Sky Monitor: the console program and the tray program in one folder.

Run from the repository root through scripts/build-windows.ps1 (Windows) or
``pyinstaller packaging/sky-monitor.spec`` (any platform, for a check).  An
ffmpeg executable placed in packaging/ffmpeg/ is shipped next to the programs.
"""

import sys
from pathlib import Path

here = Path(SPECPATH)
ffmpeg_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
ffmpeg = here / "ffmpeg" / ffmpeg_name
binaries = [(str(ffmpeg), ".")] if ffmpeg.is_file() else []
datas = [(str(path), ".") for path in (here / "ffmpeg").glob("LICENSE*")] if ffmpeg.is_file() else []
hidden = ["pystray._win32", "PIL.ImageDraw", "PIL.ImageFont", "PIL.JpegImagePlugin", "PIL.PngImagePlugin"]
excludes = ["tkinter", "matplotlib", "IPython", "pytest", "scipy.io.matlab", "scipy.sparse.linalg"]

console = Analysis(
    [str(here / "sky_monitor_console.py")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=excludes,
)
tray = Analysis(
    [str(here / "sky_monitor_tray.py")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=excludes,
)

console_exe = EXE(
    PYZ(console.pure),
    console.scripts,
    exclude_binaries=True,
    name="sky-monitor",
    console=True,
)
tray_exe = EXE(
    PYZ(tray.pure),
    tray.scripts,
    exclude_binaries=True,
    name="sky-monitor-tray",
    console=False,
)
COLLECT(
    console_exe,
    console.binaries,
    console.datas,
    tray_exe,
    tray.binaries,
    tray.datas,
    name="sky-monitor",
)
