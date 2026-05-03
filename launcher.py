"""Native-window entry point.

Order of operations:
  1. Auto-update check — if a newer .exe exists on GitHub, swap and relaunch.
     Skipped when running from source.
  2. DB sync — pull the latest lolalytics.db from the crawler's release if
     it's newer than the local copy. Done before opening any sqlite
     connections (Windows won't let us replace an open .db file).
  3. Start Flask in a background thread; open pywebview pointed at it.

If pywebview isn't installed (e.g. Python version with no pythonnet wheel),
falls back to opening the default browser.
"""

import logging
import sys
import threading
import time

import config
import sync
import updater
from version import __version__

# File log lives next to the .exe (or next to launcher.py in dev). Lets us
# see what the auto-updater / db-sync did even when the windowed .exe has no
# console attached.
_LOG_PATH = config.APP_DIR / "lol-draft-helper.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOG_PATH, encoding="utf-8", mode="a"),
        logging.StreamHandler(sys.stdout),
    ],
)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.info("--- launcher start, version=%s, frozen=%s, exe=%s ---",
             __version__, getattr(sys, "frozen", False), sys.executable)

# Tell Windows this is a distinct app (not just "some Python process"), so
# the taskbar uses our .exe's embedded icon rather than a generic fallback
# and pywebview-Edge windows group correctly. Must be called before any
# windows are created.
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "richisp.lol-draft-helper"
        )
    except Exception as e:  # noqa: BLE001
        logging.info("AppUserModelID could not be set: %s", e)

    # Nudge Explorer to re-read the .exe's embedded icon. Windows caches icons
    # by (path, mtime, size) and is sticky across reinstalls of the same .exe
    # at the same path — without this, a fresh install over a prior icon-less
    # version keeps showing the old (Python) icon. SHCNE_UPDATEITEM is the
    # targeted refresh; SHCNE_ASSOCCHANGED is the broad fallback.
    if getattr(sys, "frozen", False):
        try:
            import ctypes
            SHCNE_UPDATEITEM = 0x00002000
            SHCNE_ASSOCCHANGED = 0x08000000
            SHCNF_PATHW = 0x0005
            exe = ctypes.c_wchar_p(sys.executable)
            ctypes.windll.shell32.SHChangeNotify(SHCNE_UPDATEITEM, SHCNF_PATHW, exe, None)
            ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, 0, None, None)
        except Exception as e:  # noqa: BLE001
            logging.info("SHChangeNotify failed: %s", e)

PORT = 5050
# Cache-bust the URL on each launch so the embedded WebView2 (which keeps a
# user-data folder shared across .exe versions) can't serve stale HTML from
# the previous version after an auto-update.
URL = f"http://127.0.0.1:{PORT}/draft?_v={int(time.time())}"


def _start_flask() -> None:
    # Import lazily — `app` opens template_folder etc.; we want sync_db to
    # finish first so the DB the app reads is the freshest one.
    from app import app
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)


def _show_update_dialog_and_apply(release_info: dict) -> None:
    """Block on a small Tk progress dialog while the update downloads and
    spawns the swap script. Caller should sys.exit afterwards."""
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("LoL Draft Helper")
    root.geometry("380x140")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    # No close button — don't let the user cancel a half-applied update.
    root.protocol("WM_DELETE_WINDOW", lambda: None)
    try:
        root.eval("tk::PlaceWindow . center")
    except tk.TclError:
        pass

    main_label = tk.Label(
        root,
        text=f"Updating to v{release_info['version']}",
        font=("Segoe UI", 11),
    )
    main_label.pack(pady=(22, 8))

    progress = ttk.Progressbar(root, length=320, mode="determinate", maximum=100)
    progress.pack(pady=(0, 6))

    sub_label = tk.Label(root, text="Connecting…", font=("Segoe UI", 9), fg="#666")
    sub_label.pack(pady=(0, 12))

    def on_progress(downloaded: int, total: int) -> None:
        pct = (downloaded / total) * 100 if total else 0
        progress["value"] = pct
        sub_label.config(
            text=f"{downloaded / 1_000_000:.1f} / {total / 1_000_000:.1f} MB",
        )
        # Pump the Tk event loop so the bar actually redraws while the
        # download blocks the main thread.
        try:
            root.update()
        except tk.TclError:
            pass

    # Let the window paint at least once before we start downloading.
    root.update()

    try:
        scheduled = updater.apply_update(release_info, progress_cb=on_progress)
        if scheduled:
            main_label.config(text="Restarting…")
            sub_label.config(text="")
            progress["value"] = 100
            root.update()
            time.sleep(0.6)  # let the user see "Restarting…" briefly
        else:
            main_label.config(text="Update failed")
            sub_label.config(text="See lol-draft-helper.log for details.")
            root.update()
            time.sleep(2.0)
    except Exception as e:  # noqa: BLE001
        logging.warning("update UI: %s", e)
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    # 1. Auto-update — show a progress dialog only if there's actually an
    #    update to fetch. The cheap detection step doesn't paint UI.
    info = updater.check_for_update()
    if info is not None:
        _show_update_dialog_and_apply(info)
        sys.exit(0)

    # 2. Sync the DB before any sqlite connection opens.
    try:
        sync.sync_db()
    except Exception as e:  # noqa: BLE001 — never let sync break startup
        logging.warning("DB sync raised: %s", e)

    # 3. Server + window.
    threading.Thread(target=_start_flask, daemon=True).start()

    try:
        import webview
    except ImportError:
        print("pywebview not available — opening default browser instead.")
        import webbrowser
        time.sleep(1.5)  # let Flask bind the port
        webbrowser.open(URL)
        # Keep the process alive so Flask keeps serving.
        threading.Event().wait()
        return

    webview.create_window(
        "LoL Draft Helper",
        URL,
        width=1500,
        height=950,
        resizable=True,
        maximized=True,
    )
    webview.start()


if __name__ == "__main__":
    main()
