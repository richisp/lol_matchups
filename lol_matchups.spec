# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the LoL draft helper. Build with:
#     pyinstaller lol_matchups.spec
#
# Notes:
#   - templates/ is bundled and extracted to sys._MEIPASS at runtime
#     (app.py picks it up via the frozen-aware template_folder).
#   - lolalytics.db is NOT bundled — it sits next to the .exe so the user
#     can update it without rebuilding (config.py handles this).

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

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
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,            # no terminal window pops up
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Add icon=... when you have one:
    # icon="icon.ico",
)
