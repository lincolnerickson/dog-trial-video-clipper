# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Dog Trial Video Clipper macOS app.

Builds a self-contained `Dog Trial Video Clipper.app` bundling Python, PySide6
(incl. QtMultimedia for the player), and the imageio-ffmpeg binary, so the end
user needs nothing installed. Build it on a Mac (or the GitHub Actions macOS
runner — see .github/workflows/build-macos.yml):

    pyinstaller --noconfirm packaging/Dog_Trial_Video_Clipper.spec

The result is dist/Dog Trial Video Clipper.app for the runner's architecture.
"""

import os

from PyInstaller.utils.hooks import collect_all

# packaging/ -> project root
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# Bundle the imageio-ffmpeg package *and its ffmpeg binary*. The import in
# clipper.ffmpeg_tools.find_ffmpeg is lazy, so PyInstaller wouldn't find it
# without this; collect_all also grabs the binary under imageio_ffmpeg/binaries.
datas, binaries, hiddenimports = collect_all("imageio_ffmpeg")
hiddenimports = list(set(hiddenimports + ["imageio_ffmpeg"]))

ICON = os.path.join(SPECPATH, "icon.icns")
icon = ICON if os.path.exists(ICON) else None

a = Analysis(
    [os.path.join(ROOT, "marker.py")],
    pathex=[ROOT],                 # so `clipper`, `markerlib`, `cutter` resolve
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Dog Trial Video Clipper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # windowed GUI app (no terminal)
    argv_emulation=False,
    target_arch=None,         # native arch of the runner (arm64 or x86_64)
    codesign_identity=None,   # unsigned; the user right-clicks > Open once
    entitlements_file=None,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Dog Trial Video Clipper",
)

app = BUNDLE(
    coll,
    name="Dog Trial Video Clipper.app",
    icon=icon,
    bundle_identifier="com.dogtrialclipper.marker",
    info_plist={
        "CFBundleName": "Dog Trial Video Clipper",
        "CFBundleDisplayName": "Dog Trial Video Clipper",
        "CFBundleShortVersionString": "1.0.4",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
