"""Makes print()/stdout-writing code safe no matter how the process was
launched.

Confirmed live 2026-08-26: a real detached `pythonw.exe` launch (no console,
no inherited pipe - the exact situation a double-clicked Start Menu/Desktop
shortcut, or a Startup-folder entry, would produce) sets `sys.stdout` and
`sys.stderr` to `None`, not a dummy writable stream. Every entry-point
script in this package guarded its UTF-8 codepage fix with
`sys.stdout.reconfigure(...)`, which assumes a stream already exists -
`None.reconfigure(...)` raises `AttributeError` immediately, and since
there's no console and nothing was redirecting output, the process just
exits silently with no visible error at all. This went unnoticed all
session because every test launch so far went through run_hidden.py's log
redirection (Task Scheduler console-suppression convenience for dev
testing), which happens to also dodge this bug as a side effect by giving
`sys.stdout` a real file object before the target script runs. A real
end-user launch has no such wrapper.

setup_stdio() must be called before anything else in the module prints.
"""
import sys


def setup_stdio(log_name):
    if sys.stdout is not None:
        # A real console, or an inherited pipe (e.g. over SSH, or invoked
        # via python.exe instead of pythonw.exe) - just fix the codepage.
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        return

    import os

    log_dir = os.path.join(os.environ["APPDATA"], "LiveTranslator-kun", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = open(os.path.join(log_dir, log_name), "a", encoding="utf-8", buffering=1)
    sys.stdout = log_file
    sys.stderr = log_file
