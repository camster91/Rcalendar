"""Probe: does a cancel-returning `closing` handler defeat window.destroy()?

Mirrors the Tray._quit path (stop the worker, stop the tray, then
window.destroy()) and the `closing` handler that hides to tray, against the
installed pywebview. No network, no LSM, no app imports.

Run with no argument for the fixed shape, or `--before` for the shape this
fix replaced (destroy() before the quitting flag is set).

  py -3 .repro_quit.py --before   ->  the handler cancels the quit; HANG
  py -3 .repro_quit.py            ->  the close goes through; start() returns
"""
import os
import sys
import threading
import time

import webview

BEFORE = "--before" in sys.argv


def log(msg):
    print(msg, flush=True)


window = webview.create_window("repro", "about:blank", width=400, height=300)

quitting = threading.Event()


def on_closing():
    # app/main.py on_closing: hide to tray, unless this close is a quit.
    if quitting.is_set():
        log("closing handler ran; returning TRUE (let it through)")
        return True
    try:
        window.hide()
    except Exception as exc:  # noqa: BLE001
        log(f"hide raised {exc!r}")
    log("closing handler ran; returning False (cancel)")
    return False


window.events.closing += on_closing


def quit_path():
    """Tray._quit(), in the order it did (--before) or does (fixed)."""
    time.sleep(3)
    if BEFORE:
        log("calling window.destroy() WITHOUT having set the flag")
    else:
        quitting.set()
        log("set the quitting flag, then calling window.destroy()")
    t0 = time.time()
    try:
        window.destroy()
        log(f"destroy() RETURNED in {time.time() - t0:.2f}s")
    except Exception as exc:  # noqa: BLE001
        log(f"destroy() raised {exc!r}")


def watchdog():
    time.sleep(12)
    log("WATCHDOG: webview.start() never returned 12s after destroy() -> HANG")
    os._exit(42)


threading.Thread(target=quit_path, name="tray").start()
threading.Thread(target=watchdog, name="watchdog", daemon=True).start()
webview.start()
log("webview.start() RETURNED -> the process would exit normally")
os._exit(0)
