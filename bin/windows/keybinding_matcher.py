"""Ported directly from index.tsx's startHotkeyPolling() (buildKeySetGroups
+ the per-group tap/hold state machine inside its setInterval body) - that
logic is a pure state machine over "what's currently held", nothing
Decky/React-specific about it, so it transfers as-is. Only the input source
differs (input_state.py's keyboard/mouse/XInput reads here, hidraw button
reads there).

A binding is {"id","command","keys","long_press","threshold_ms"}. Bindings
sharing the same key-set (order-independent) are grouped so at most one tap
(long_press=False) and one hold (long_press=True) binding can share a combo,
generalizing the Linux default's L4 dual-role (short tap = refresh, long
hold = pause/resume) to any key-set. A short press (released before
threshold_ms elapses) fires the tap binding on release; holding past
threshold_ms fires the hold binding immediately, while still held - not on
release, so the caller can give feedback the instant it's safe to let go.
"""
import time


def key_set_signature(keys):
    return "+".join(sorted(keys))


class _KeySetGroup:
    def __init__(self, keys):
        self.keys = list(keys)
        self.tap_binding = None
        self.hold_binding = None
        self.hold_start = None
        self.long_press_fired = False
        self.was_held = False


def build_groups(bindings):
    groups = {}
    for binding in bindings:
        sig = key_set_signature(binding["keys"])
        group = groups.setdefault(sig, _KeySetGroup(binding["keys"]))
        if binding.get("long_press"):
            group.hold_binding = binding
        else:
            group.tap_binding = binding
    return list(groups.values())


class KeybindingMatcher:
    def __init__(self, bindings, on_fire):
        """on_fire(binding, while_held: bool) is called whenever a binding
        fires - while_held=True for a long-press (fired mid-hold),
        False for a tap (fired on release)."""
        self.groups = build_groups(bindings)
        self.on_fire = on_fire

    def poll(self, held_set):
        now = time.monotonic() * 1000.0
        for group in self.groups:
            is_held = all(k in held_set for k in group.keys)
            if is_held and not group.was_held:
                group.hold_start = now
                group.long_press_fired = False
            elif (
                is_held
                and group.was_held
                and group.hold_start is not None
                and not group.long_press_fired
                and group.hold_binding is not None
            ):
                if now - group.hold_start >= group.hold_binding["threshold_ms"]:
                    group.long_press_fired = True
                    self.on_fire(group.hold_binding, True)
            elif not is_held and group.was_held:
                if group.hold_start is not None and not group.long_press_fired and group.tap_binding is not None:
                    self.on_fire(group.tap_binding, False)
                group.hold_start = None
                group.long_press_fired = False
            group.was_held = is_held
