# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the LoL draft helper. Build with:
#     pyinstaller lol_matchups.spec
#
# Notes:
#   - templates/ is bundled and extracted to sys._MEIPASS at runtime
#     (app.py picks it up via the frozen-aware template_folder).
#   - lolalytics.db is NOT bundled — it sits next to the .exe so the user
#     can update it without rebuilding (config.py handles this).

import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Resolve paths against the spec's directory rather than CWD — PyInstaller's
# eval CWD has been observed to differ from where the spec lives in some
# environments, and `SPEC` is the canonical path it provides.
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))

# Generate icon.ico from the source .webp if needed. Inno Setup also reads
# this file (see installer.iss → SetupIconFile=icon.ico).
ICON_SRC = os.path.join(SPEC_DIR, "heimerdinger-emote.webp")
ICON_OUT = os.path.join(SPEC_DIR, "icon.ico")
_needs_build = (
    os.path.exists(ICON_SRC)
    and (not os.path.exists(ICON_OUT)
         or os.path.getmtime(ICON_SRC) > os.path.getmtime(ICON_OUT))
)
if _needs_build:
    try:
        from PIL import Image
        img = Image.open(ICON_SRC).convert("RGBA")
        img.save(ICON_OUT, format="ICO",
                 sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        print(f"[spec] generated {ICON_OUT} from {ICON_SRC}")
    except Exception as e:
        print(f"[spec] WARN could not generate {ICON_OUT}: {e}")
ICON_FILE = ICON_OUT if os.path.exists(ICON_OUT) else None
print(f"[spec] ICON_FILE = {ICON_FILE!r}")

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("templates", "templates"),
    ],
    hiddenimports=[
        # pywebview's runtime backend + Flask's auto-detected modules
        *collect_submodules("webview"),
        "_cffi_backend",
        # New modules wired into launcher.py
        "sync",
        "updater",
        "version",
        # Tk progress dialog shown during auto-update.
        "tkinter",
        "tkinter.ttk",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Don't ship the crawler stack to end users.
        "playwright",
        "scrape_lolalytics",
        "crawl_champions",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="lol-draft-helper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,            # no terminal window pops up
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_FILE,
)
# Disable UPX-compression on the icon-bearing .exe — UPX has been observed to
# strip or break the icon resource in PyInstaller-produced binaries.
