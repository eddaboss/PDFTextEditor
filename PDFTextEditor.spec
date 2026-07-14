# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for "PDF for Free" -- ONE spec for macOS and Windows.

This is the single source of truth for the desktop build on both platforms. The
shared Analysis / PYZ / EXE / COLLECT stages are identical everywhere; only the
final packaging differs by OS:

  * macOS   -> a .app BUNDLE carrying the Info.plist folder-access usage strings
              and the dark-appearance hint. Without them the Open dialog shows
              empty folders and a light title bar sits above the dark chrome.
  * Windows -> the COLLECT output directory holding "PDF for Free.exe" (a
              windowed, one-folder build). Inno Setup wraps that directory into
              the installer.

One-FOLDER (COLLECT) output is required on both platforms: the tufup self-updater
patches the app directory in place, which a one-FILE build could not support.

The bundle/exe version comes from the package's __version__ -- the single source
of truth, also shown in the About dialog. Without an explicit version PyInstaller
stamps 0.0.0 and Finder "Get Info" shows 0.0.0.
"""
import os
import sys

from PyInstaller.utils.hooks import (
    collect_submodules, collect_data_files, collect_dynamic_libs,
)

# SPEC is injected by PyInstaller; make the package importable for __version__.
sys.path.insert(0, os.path.dirname(os.path.abspath(SPEC)))
from pdftexteditor import __version__ as _VERSION

_IS_MAC = sys.platform == "darwin"

# Build channel: the release pipeline sets PDFTE_BUILD_CHANNEL=stable|dev. Bake it
# into the package (so the running app talks to the right backend + update
# channel) and use it for the app name + bundle id so the dev build installs
# side-by-side with stable.
_CHANNEL = os.environ.get("PDFTE_BUILD_CHANNEL", "stable")
_IS_DEV = _CHANNEL == "dev"
with open("pdftexteditor/_build_channel.py", "w", encoding="utf-8") as _bc:
    _bc.write(f"CHANNEL = {_CHANNEL!r}\n")
# Display name only (the .app/.exe, dock, DMG volume, installer). The tufup
# update id (appconfig.TUFUP_APP_NAME / repo_config.APP_NAME) and the bundle id
# below stay "PDFTextEditor"/com.eddaboss.pdftexteditor so the update channel and
# macOS app identity are unchanged.
_NAME = "PDF for Free (Dev)" if _IS_DEV else "PDF for Free"
_BUNDLE_ID = ("com.eddaboss.pdftexteditor.dev" if _IS_DEV
              else "com.eddaboss.pdftexteditor")

# Per-OS app icon. build_icon.py emits AppIcon.icns on macOS and AppIcon.ico for
# the Windows runner. Only the current platform's icon is referenced, so a
# missing other-OS icon file never breaks the build.
_ICON = "assets/AppIcon.icns" if _IS_MAC else "assets/AppIcon.ico"

_USAGE = "PDF for Free needs access so you can open and save your PDF files."

# OCR runtime data: RapidOCR ships its PP-OCR detection/recognition/cls models as
# .onnx PACKAGE DATA (~14 MB) and onnxruntime ships native provider libraries.
# PyInstaller does NOT auto-collect either, so a frozen build imports the modules
# but crashes at RapidOCR() with the models missing -- and Windows runs RapidOCR
# EXCLUSIVELY (Apple Vision is macOS-only), so without this OCR is dead on Windows.
# Guarded: collect_* returns [] when the OCR deps aren't installed (the lean base
# build), so that build stays byte-for-byte unchanged.
_ocr_datas, _ocr_bins, _ocr_hidden = [], [], []
for _pkg in ("rapidocr_onnxruntime", "onnxruntime"):
    try:
        _ocr_datas += collect_data_files(_pkg)
        _ocr_bins += collect_dynamic_libs(_pkg)
        _ocr_hidden += collect_submodules(_pkg)
    except Exception:
        pass

a = Analysis(
    ['launch.py'],
    pathex=[],
    binaries=_ocr_bins,
    # Ship the bundled DejaVu faces so a PDF set in them (mPDF / matplotlib /
    # many web-to-PDF tools) stays editable + saveable in the real face on a
    # machine that does not have them installed. Destination mirrors the package
    # layout so _bundled_font_dir() finds them under sys._MEIPASS.
    datas=[
        ('pdftexteditor/assets/fonts', 'pdftexteditor/assets/fonts'),
        # tufup trust anchor: the updater verifies every update against this.
        ('pdftexteditor/assets/root.json', 'pdftexteditor/assets'),
    ] + _ocr_datas,
    hiddenimports=collect_submodules('pdftexteditor') + _ocr_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The lean base build excludes the heavy OCR stack here (added by the
    # optional-OCR work); left empty so this stage stays behaviour-preserving.
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX off: it triggers Windows antivirus false positives and complicates
    # macOS signing/notarization, for little size win on an already-large bundle.
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Windows takes its icon from the EXE; on macOS the BUNDLE icns wins, so
    # setting it here is harmless on the Mac side.
    icon=_ICON,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=_NAME,
)

# macOS only: wrap the collected output in a .app bundle with the metadata the
# Open dialog and dark title bar need. On Windows the COLLECT dir above IS the
# deliverable (Inno Setup packages it), so there is nothing more to build.
if _IS_MAC:
    app = BUNDLE(
        coll,
        name=f"{_NAME}.app",
        icon='assets/AppIcon.icns',
        bundle_identifier=_BUNDLE_ID,
        version=_VERSION,
        info_plist={
            'NSHighResolutionCapable': True,
            # Force the DARK system appearance so the native macOS title bar
            # renders dark to match the Charcoal chrome (otherwise a light title
            # bar sits above the dark app). Content is dark via QSS regardless.
            'NSAppearance': 'NSAppearanceNameDarkAqua',
            'CFBundleName': _NAME,
            'CFBundleDisplayName': _NAME,
            'CFBundleVersion': _VERSION,
            'LSApplicationCategoryType': 'public.app-category.productivity',
            'NSDocumentsFolderUsageDescription': _USAGE,
            'NSDownloadsFolderUsageDescription': _USAGE,
            'NSDesktopFolderUsageDescription': _USAGE,
            'NSRemovableVolumesUsageDescription': _USAGE,
        },
    )
