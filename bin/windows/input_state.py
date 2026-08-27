"""Reads currently-held input identifiers (keyboard/mouse/gamepad) each
poll - the Windows equivalent of main.py's hidraw button reads on Linux.
Produces the same *shape* of thing (a set of opaque string identifiers) that
keybinding_matcher.py's ported state machine expects, but the identifier
scheme itself is new here (no Deck hardware to name buttons after):

  kbd:<virtual_key_code>   - a keyboard key (GetAsyncKeyState)
  mouse:left/right/middle/x1/x2
  pad:<controller_index>:<NAME>  - an XInput gamepad digital button, or
                                    LT/RT for a trigger past _TRIGGER_THRESHOLD

XInput (built into Windows, no extra pip dependency) covers Xbox controllers
and the large majority of modern gamepads via XInput-compatible mode - a
real gap versus arbitrary DirectInput-only pads, worth remembering if a
specific unsupported controller ever comes up, but not worth pulling in a
new dependency for preemptively.
"""
import ctypes
import time

import win32api
import win32con

_KBD_VK_CANDIDATES = (
    list(range(0x30, 0x3A))  # 0-9
    + list(range(0x41, 0x5B))  # A-Z
    + list(range(0x70, 0x88))  # F1-F24
    + [
        win32con.VK_SPACE, win32con.VK_TAB, win32con.VK_RETURN, win32con.VK_ESCAPE,
        win32con.VK_LCONTROL, win32con.VK_RCONTROL, win32con.VK_LSHIFT, win32con.VK_RSHIFT,
        win32con.VK_LMENU, win32con.VK_RMENU, win32con.VK_LEFT, win32con.VK_RIGHT,
        win32con.VK_UP, win32con.VK_DOWN, win32con.VK_HOME, win32con.VK_END,
        win32con.VK_INSERT, win32con.VK_DELETE, win32con.VK_PRIOR, win32con.VK_NEXT,
    ]
)

_KBD_LABELS = {
    win32con.VK_SPACE: "Space", win32con.VK_TAB: "Tab", win32con.VK_RETURN: "Enter",
    win32con.VK_ESCAPE: "Esc", win32con.VK_LCONTROL: "Left Ctrl", win32con.VK_RCONTROL: "Right Ctrl",
    win32con.VK_LSHIFT: "Left Shift", win32con.VK_RSHIFT: "Right Shift",
    win32con.VK_LMENU: "Left Alt", win32con.VK_RMENU: "Right Alt",
    win32con.VK_LEFT: "Left", win32con.VK_RIGHT: "Right", win32con.VK_UP: "Up", win32con.VK_DOWN: "Down",
    win32con.VK_HOME: "Home", win32con.VK_END: "End", win32con.VK_INSERT: "Insert", win32con.VK_DELETE: "Delete",
    win32con.VK_PRIOR: "Page Up", win32con.VK_NEXT: "Page Down",
}

_MOUSE_VK = {
    "mouse:left": win32con.VK_LBUTTON,
    "mouse:right": win32con.VK_RBUTTON,
    "mouse:middle": win32con.VK_MBUTTON,
    "mouse:x1": win32con.VK_XBUTTON1,
    "mouse:x2": win32con.VK_XBUTTON2,
}

_MOUSE_LABELS = {
    "mouse:left": "Mouse Left", "mouse:right": "Mouse Right", "mouse:middle": "Mouse Middle",
    "mouse:x1": "Mouse Button 4", "mouse:x2": "Mouse Button 5",
}


def _is_down(vk):
    return bool(win32api.GetAsyncKeyState(vk) & 0x8000)


def _kbd_label(vk):
    if vk in _KBD_LABELS:
        return _KBD_LABELS[vk]
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    return f"Key {vk}"


class _XinputGamepad(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class _XinputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", _XinputGamepad)]


def _load_xinput():
    for dll_name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return ctypes.windll.LoadLibrary(dll_name)
        except OSError:
            continue
    return None


_xinput = _load_xinput()

_PAD_BUTTON_BITS = {
    0x0001: "DPAD_UP", 0x0002: "DPAD_DOWN", 0x0004: "DPAD_LEFT", 0x0008: "DPAD_RIGHT",
    0x0010: "START", 0x0020: "BACK", 0x0040: "LS", 0x0080: "RS",
    0x0100: "LB", 0x0200: "RB", 0x1000: "A", 0x2000: "B", 0x4000: "X", 0x8000: "Y",
}
# A single threshold chatters: confirmed live 2026-08-26 via a real user's
# tray_app.log - a controller bound to LT for both refresh (tap) and
# pause/resume (long-press) fired 'refresh' *eight times in under two
# seconds* from what was meant to be one press. An analog trigger resting
# near a single cutoff doesn't sit still - normal finger micro-movement (or
# a trigger that's simply worn/imprecise) flickers the raw byte value back
# and forth across it, and each crossing looked like a fresh press+release
# to the tap/hold state machine, which - compounding the effect - hides the
# HUD immediately on every refresh (trigger_refresh() calls
# _hide_all_labels() synchronously), so a shown one-shot result could get
# wiped again within the same fraction of a second by the very next chatter
# event. Two thresholds with a gap between them (classic hysteresis/Schmitt
# trigger) fixes this: once considered "pressed", the value has to drop
# further before it's considered "released", so a value hovering anywhere
# in that gap no longer flips state on every read. Requires remembering the
# previous held/not-held state per trigger, unlike a stateless single
# comparison - _TRIGGER_STATE is deliberately module-level (this module is
# already a de-facto singleton, see _xinput above) rather than threaded
# through every caller.
_TRIGGER_PRESS_THRESHOLD = 45  # out of 255 - must exceed this to register as newly pressed
_TRIGGER_RELEASE_THRESHOLD = 20  # must drop below this (not just below press) to register as released
_TRIGGER_STATE = {}  # (index, "LT"/"RT") -> bool, currently considered held


def _trigger_held(index, name, value):
    key = (index, name)
    was_held = _TRIGGER_STATE.get(key, False)
    now_held = value > (_TRIGGER_RELEASE_THRESHOLD if was_held else _TRIGGER_PRESS_THRESHOLD)
    _TRIGGER_STATE[key] = now_held
    return now_held


def _pad_held(index):
    if _xinput is None:
        return set()
    state = _XinputState()
    if _xinput.XInputGetState(index, ctypes.byref(state)) != 0:
        return set()  # controller not connected at this index
    held = set()
    buttons = state.Gamepad.wButtons
    for bit, name in _PAD_BUTTON_BITS.items():
        if buttons & bit:
            held.add(f"pad:{index}:{name}")
    if _trigger_held(index, "LT", state.Gamepad.bLeftTrigger):
        held.add(f"pad:{index}:LT")
    if _trigger_held(index, "RT", state.Gamepad.bRightTrigger):
        held.add(f"pad:{index}:RT")
    return held


def current_held():
    """The set of every input identifier currently held down, across
    keyboard, mouse, and up to 4 XInput gamepads."""
    held = set()
    for vk in _KBD_VK_CANDIDATES:
        if _is_down(vk):
            held.add(f"kbd:{vk}")
    for name, vk in _MOUSE_VK.items():
        if _is_down(vk):
            held.add(name)
    for i in range(4):
        held |= _pad_held(i)
    return held


def describe(identifier):
    """Human-readable label for a captured identifier, for the keybindings UI."""
    if identifier.startswith("kbd:"):
        return _kbd_label(int(identifier.split(":", 1)[1]))
    if identifier in _MOUSE_LABELS:
        return _MOUSE_LABELS[identifier]
    if identifier.startswith("pad:"):
        _, index, name = identifier.split(":", 2)
        return f"Pad {index}: {name}"
    return identifier


def capture_one(timeout_s=6.0, poll_interval_s=0.03):
    """Blocks (call from a worker thread, not the UI thread) until some
    input becomes held that wasn't held when this was called, or times out.
    Returns (identifier, label) or (None, None). Drives the settings UI's
    "press a button now" capture flow."""
    baseline = current_held()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        held = current_held()
        new = held - baseline
        if new:
            identifier = sorted(new)[0]
            return identifier, describe(identifier)
        time.sleep(poll_interval_s)
    return None, None
