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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Quiet Flask's per-request logging in the launcher console.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

import sync
import updater

PORT = 5050
URL = f"http://127.0.0.1:{PORT}/draft"


def _start_flask() -> None:
    # Import lazily — `app` opens template_folder etc.; we want sync_db to
    # finish first so the DB the app reads is the freshest one.
    from app import app
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)


def main() -> None:
    # 1. Auto-update first — exits this process if a swap is scheduled.
    if updater.check_and_apply():
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
    )
    webview.start()


if __name__ == "__main__":
    main()
