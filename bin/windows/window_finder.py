"""Find a target game window by (partial) title and return its client-area
rect in physical screen pixels, so capture can be bounded to just that
window instead of the whole monitor.

Physical pixels matter here specifically because dxcam's frames are
physical pixels too - if this process isn't itself declared per-monitor-DPI-
aware, win32's GetWindowRect/ClientToScreen silently return *virtualized*
(DPI-unaware) coordinates instead of real ones on a scaled display (this
laptop's internal LCD runs at 125%, see project_playtranslate_windows_port
memory), which would systematically misalign the crop from the real window.
call ensure_dpi_aware() once at process startup, before creating any Qt
window - Qt tries to set its own DPI awareness context too, and whichever
call happens first wins for the process's whole lifetime.
"""
import ctypes

import win32api
import win32con
import win32gui


def ensure_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except OSError:
        pass  # already set by something else (e.g. Qt) - fine either way


def list_window_titles():
    """Titles of every visible top-level window with a non-empty title,
    de-duplicated and sorted case-insensitively - used to populate the
    target-window dropdown in Settings, so a user can pick their game's
    actual window title instead of having to guess/type it exactly.
    Same per-window try/except pattern as find_window_rect() and no
    stricter a visibility filter, deliberately - both already proven live
    against this exact EnumWindows-mid-teardown race."""
    titles = []

    def callback(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            return True
        if title:
            titles.append(title)
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        return []

    seen = set()
    unique = []
    for title in titles:
        if title not in seen:
            seen.add(title)
            unique.append(title)
    return sorted(unique, key=str.lower)


def find_window_rect(title_substring):
    """(x0, y0, x1, y1) of the first visible top-level window whose title
    contains `title_substring` (case-insensitive), in physical screen
    pixels - or None if no match. Client-area only (excludes the title bar
    and borders), since that's the actual rendered game content."""
    matches = []

    def callback(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            # A window can be destroyed between EnumWindows handing us its
            # hwnd and this callback running (confirmed live: transient,
            # differently-worded pywintypes errors from short-lived windows
            # like IME/tooltip helpers) - an exception here otherwise
            # propagates out of EnumWindows itself and aborts the whole
            # enumeration, not just this one window.
            return True
        if title and title_substring.lower() in title.lower():
            matches.append(hwnd)
            return False
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        return None
    if not matches:
        return None

    hwnd = matches[0]
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    x0, y0 = win32gui.ClientToScreen(hwnd, (left, top))
    x1, y1 = win32gui.ClientToScreen(hwnd, (right, bottom))

    # A window's reported client rect can extend past the visible desktop -
    # confirmed live: NORCO's client rect ran to y=1118 on a 1920x1080
    # screen (its own title bar pushes a nominally-1080-tall client area a
    # bit further down than the screen has room for), and the overlap
    # lands exactly on the taskbar strip at the bottom of the screen.
    # Simply clipping to the monitor's full resolution still captures those
    # taskbar pixels (they're real on-screen content at those coordinates,
    # just not game content) - clip to the *work area* (monitor minus
    # taskbar) instead so a partially-taskbar-occluded window edge doesn't
    # get OCR'd as game dialogue.
    try:
        monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        work = win32api.GetMonitorInfo(monitor)["Work"]
        x0 = max(x0, work[0])
        y0 = max(y0, work[1])
        x1 = min(x1, work[2])
        y1 = min(y1, work[3])
    except Exception:
        pass  # best-effort - worst case the taskbar strip isn't excluded

    return (x0, y0, x1, y1)
