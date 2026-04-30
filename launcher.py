"""Native-window entry point.

Starts the Flask server on a local port in a background thread, then opens
a pywebview window pointed at it. Used both for `python launcher.py` during
development and as the PyInstaller .exe entry point in production.

If pywebview isn't installed (e.g. Python version with no pythonnet wheel),
falls back to opening the default browser.
"""

import logging
import threading
import time

# Quiet Flask's per-request logging in the launcher console.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

from app import app

PORT = 5050
URL = f"http://127.0.0.1:{PORT}/draft"


def _start_flask() -> None:
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)


def main() -> None:
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
