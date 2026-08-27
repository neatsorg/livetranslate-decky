"""Runs another script under pythonw.exe with stdout/stderr redirected to a
log file, so Task Scheduler test runs never pop a visible console window.

Why this exists: a scheduled task action of `cmd /c python script.py ...`
opens a real console window that steals foreground focus the instant it
runs - confirmed live 2026-08-25, it knocked a game out of focus mid-test,
which is exactly the scenario this whole project needs to not disturb.
pythonw.exe never allocates a console at all, but it also can't be given
shell-style `> log 2>&1` redirection directly (no shell parses that for it),
so this wrapper does the redirection itself in Python instead.

Usage: pythonw.exe run_hidden.py <log_path> <target_script.py> [args...]
"""
import runpy
import sys

if len(sys.argv) < 3:
    sys.exit("usage: run_hidden.py <log_path> <target_script.py> [args...]")

log_path = sys.argv[1]
target = sys.argv[2]
sys.argv = [target] + sys.argv[3:]

log_file = open(log_path, "w", encoding="utf-8", buffering=1)
sys.stdout = log_file
sys.stderr = log_file

try:
    runpy.run_path(target, run_name="__main__")
except SystemExit:
    raise
except Exception:
    import traceback
    traceback.print_exc()
finally:
    log_file.flush()
