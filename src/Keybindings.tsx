import { Button, Field, ModalRoot, SliderField, ToggleField, showModal } from "@decky/ui";
import { callable } from "@decky/api";
import { useEffect, useState } from "react";

type BindingCommand = "refresh" | "pause_resume" | "touch_translate";

interface BindingDraft {
  id: string;
  command: BindingCommand;
  keys: string[];
  long_press: boolean;
  threshold_ms: number;
}

interface KeybindingSettings {
  bindings: BindingDraft[];
}

interface SetKeybindingsResult {
  ok: boolean;
  error?: string;
  bindings?: BindingDraft[];
}

interface CaptureResult {
  success: boolean;
  key?: string;
  label?: string;
  error?: string;
}

// Built-in Steam Deck controller key labels only - "kbd:<code>"/"pad:..."
// keys are captured live (see describeKey) rather than picked from a list,
// so there's no fixed bindable-key set to enumerate anymore.
const KEY_LABELS: Record<string, string> = {
  trackpad_left_tap: "Left Trackpad Tap",
  trackpad_right_tap: "Right Trackpad Tap",
};

// Common Linux keycodes (linux/input-event-codes.h) worth a friendly label
// for existing saved bindings reopened in a later session, when there's no
// captured label to fall back on (see describeKey). Not exhaustive -
// anything missing just shows as "Key <code>", still a stable, correct
// identifier, just less pretty.
const KBD_KEY_LABELS: Record<number, string> = {
  1: "Esc", 14: "Backspace", 15: "Tab", 28: "Enter", 29: "Left Ctrl", 42: "Left Shift",
  54: "Right Shift", 56: "Left Alt", 57: "Space", 58: "Caps Lock", 97: "Right Ctrl", 100: "Right Alt",
  102: "Home", 103: "Up", 104: "Page Up", 105: "Left", 106: "Right", 107: "End",
  108: "Down", 109: "Page Down", 110: "Insert", 111: "Delete",
  2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8", 10: "9", 11: "0",
  16: "Q", 17: "W", 18: "E", 19: "R", 20: "T", 21: "Y", 22: "U", 23: "I", 24: "O", 25: "P",
  30: "A", 31: "S", 32: "D", 33: "F", 34: "G", 35: "H", 36: "J", 37: "K", 38: "L",
  44: "Z", 45: "X", 46: "C", 47: "V", 48: "B", 49: "N", 50: "M",
  59: "F1", 60: "F2", 61: "F3", 62: "F4", 63: "F5", 64: "F6",
  65: "F7", 66: "F8", 67: "F9", 68: "F10", 87: "F11", 88: "F12",
};

function describeKey(key: string, capturedLabels: Record<string, string>): string {
  if (!key) return "Press to bind";
  if (capturedLabels[key]) return capturedLabels[key];
  if (KEY_LABELS[key]) return KEY_LABELS[key];
  const kbdMatch = /^kbd:(\d+)$/.exec(key);
  if (kbdMatch) {
    const code = Number(kbdMatch[1]);
    return KBD_KEY_LABELS[code] ?? `Key ${code}`;
  }
  const padMatch = /^pad:([0-9a-f]{4}):([0-9a-f]{4}):/.exec(key);
  if (padMatch) {
    return `External Pad (${padMatch[1]}:${padMatch[2]})`;
  }
  const padevMatch = /^padev:([0-9a-f]{4}):([0-9a-f]{4}):/.exec(key);
  if (padevMatch) {
    return `External Pad, no analog triggers (${padevMatch[1]}:${padevMatch[2]})`;
  }
  return key; // a bare built-in Deck name ("L1", "A", ...) - already readable
}

const EMPTY_KEY = "";

const MAX_KEYS_PER_BINDING = 3;
const DEFAULT_THRESHOLD_MS = 900;

const COMMAND_ORDER: BindingCommand[] = ["refresh", "pause_resume", "touch_translate"];

const COMMAND_META: Record<BindingCommand, { label: string; supportsLongPress: boolean; description: string }> = {
  refresh: {
    label: "Refresh",
    supportsLongPress: true,
    description: "Restarts Dynamic Capture.",
  },
  pause_resume: {
    label: "Pause / Resume",
    supportsLongPress: true,
    description: "Toggles Dynamic Capture's paused state.",
  },
  touch_translate: {
    label: "Touch Translate",
    supportsLongPress: false,
    description:
      "Only while paused. Fires the whole time these keys are held, so you can hold them and tap-and-hold the touchscreen to translate what's under your finger.",
  },
};

const getKeybindingSettings = callable<[], KeybindingSettings>("get_keybinding_settings");
const setKeybindingSettingsCall = callable<[KeybindingSettings], SetKeybindingsResult>("set_keybinding_settings");
const captureInputSignal = callable<[], CaptureResult>("capture_input_signal");
// Reuses RoiCrop.tsx's flag/RPC rather than a dedicated one - this modal is
// just as much PlayTranslate's own on-screen UI as the region-crop editor
// (same "don't read my own text" self-capture problem), and sharing the
// flag means it also gets set_dynamic_qam_open()'s existing stale-flag
// recovery (main.py's _clear_stale_roi_editor_flag, run on every QAM
// reopen) for free, instead of needing a second copy of that exception path.
const setDynamicRoiEditorOpen = callable<[boolean, string], { roi_editor_open: boolean }>(
  "set_dynamic_roi_editor_open"
);

function createToken(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `keybindings_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

let nextBindingSeq = 1;
function newBindingId(): string {
  return `binding_${nextBindingSeq++}_${Date.now()}`;
}

function bindingSignature(binding: BindingDraft): string {
  return `${[...binding.keys].sort().join("+")}|${binding.command === "touch_translate" ? false : binding.long_press}`;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

// ModalRoot wraps its children in a <form>, and Button/DialogButton
// defaults to type="submit", so every click submits (and closes) the
// modal unless this stops it first.
function preventFormSubmit(handler: () => void) {
  return (event: { preventDefault(): void }) => {
    event.preventDefault();
    handler();
  };
}

function KeybindingsContent({ onClose, modalToken }: { onClose: () => void; modalToken: string }) {
  const [bindings, setBindings] = useState<BindingDraft[]>([]);
  const [status, setStatus] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [capturedLabels, setCapturedLabels] = useState<Record<string, string>>({});
  const [capturingFor, setCapturingFor] = useState<{ id: string; index: number } | null>(null);

  // This modal is just as much PlayTranslate's own on-screen UI as the
  // region-crop editor, and an already-running Dynamic Capture needs the
  // same "don't read my own text" protection while it's up - see
  // setDynamicRoiEditorOpen's own comment above for why the flag is shared.
  // openKeybindings() below already awaits arming this before the modal
  // ever mounts - this effect's own arm call is just a redundant safety
  // net (same token, idempotent), the unmount cleanup is what it's really
  // here for. See openRoiCropEditor's comment for why the pre-open await
  // is the one that actually matters: confirmed live 2026-08-20 that this
  // mount effect alone was too slow to win the race against
  // capture_dynamic.py's per-frame flag check, so it read this modal's own
  // content as if it were game text.
  useEffect(() => {
    setDynamicRoiEditorOpen(true, modalToken).catch(() => {});
    return () => {
      setDynamicRoiEditorOpen(false, modalToken).catch(() => {});
    };
  }, [modalToken]);

  useEffect(() => {
    (async () => {
      setBusy(true);
      setStatus("loading...");
      try {
        const settings = await getKeybindingSettings();
        setBindings(settings.bindings.map((b) => ({ ...b, keys: [...b.keys] })));
        setStatus("");
      } catch (error) {
        setStatus(`error: ${errorMessage(error)}`);
      } finally {
        setBusy(false);
      }
    })();
  }, []);

  const hasUnsetKey = (binding: BindingDraft) => binding.keys.some((key) => !key);
  const hasAnyUnsetKey = bindings.some(hasUnsetKey);

  const signatureCounts = new Map<string, number>();
  for (const binding of bindings) {
    const sig = bindingSignature(binding);
    signatureCounts.set(sig, (signatureCounts.get(sig) ?? 0) + 1);
  }
  const isDuplicate = (binding: BindingDraft) => (signatureCounts.get(bindingSignature(binding)) ?? 0) > 1;
  const hasAnyDuplicate = Array.from(signatureCounts.values()).some((count) => count > 1);

  const updateBinding = (id: string, patch: Partial<BindingDraft>) => {
    setBindings((prev) => prev.map((b) => (b.id === id ? { ...b, ...patch } : b)));
  };

  const deleteBinding = (id: string) => {
    setBindings((prev) => prev.filter((b) => b.id !== id));
  };

  const addBinding = (command: BindingCommand) => {
    const draft: BindingDraft = {
      id: newBindingId(),
      command,
      keys: [EMPTY_KEY],
      long_press: false,
      threshold_ms: DEFAULT_THRESHOLD_MS,
    };
    setBindings((prev) => [...prev, draft]);
  };

  const addKeyToBinding = (id: string) => {
    setBindings((prev) =>
      prev.map((b) => {
        if (b.id !== id || b.keys.length >= MAX_KEYS_PER_BINDING) return b;
        return { ...b, keys: [...b.keys, EMPTY_KEY] };
      })
    );
  };

  const removeKeyFromBinding = (id: string, index: number) => {
    setBindings((prev) =>
      prev.map((b) => {
        if (b.id !== id || b.keys.length <= 1) return b;
        return { ...b, keys: b.keys.filter((_, i) => i !== index) };
      })
    );
  };

  const setBindingKey = (id: string, index: number, key: string) => {
    setBindings((prev) =>
      prev.map((b) => {
        if (b.id !== id) return b;
        const keys = [...b.keys];
        keys[index] = key;
        return { ...b, keys };
      })
    );
  };

  const startCapture = async (id: string, index: number) => {
    setCapturingFor({ id, index });
    setStatus("press a button or key now...");
    try {
      const result = await captureInputSignal();
      if (result.success && result.key) {
        if (result.label) {
          setCapturedLabels((prev) => ({ ...prev, [result.key as string]: result.label as string }));
        }
        setBindingKey(id, index, result.key);
        setStatus("");
      } else {
        setStatus(`error: ${result.error ?? "no input detected"}`);
      }
    } catch (error) {
      setStatus(`error: ${errorMessage(error)}`);
    } finally {
      setCapturingFor(null);
    }
  };

  const handleSave = async (): Promise<boolean> => {
    setBusy(true);
    setStatus("saving...");
    try {
      const payload: KeybindingSettings = {
        bindings: bindings.map((b) => ({
          id: b.id,
          command: b.command,
          keys: b.keys,
          long_press: b.command === "touch_translate" ? false : b.long_press,
          threshold_ms: b.threshold_ms,
        })),
      };
      const result = await setKeybindingSettingsCall(payload);
      if (result.ok) {
        setStatus("saved");
        window.dispatchEvent(new CustomEvent("playtranslate-keybindings-changed"));
        return true;
      }
      setStatus(`error: ${result.error ?? "unknown"}`);
      return false;
    } catch (error) {
      setStatus(`error: ${errorMessage(error)}`);
      return false;
    } finally {
      setBusy(false);
    }
  };

  // Both the top and bottom action are the same "Save & Close" - a single
  // combined button rather than a separate Close (which used to discard
  // silently) and Save, after a user report 2026-08-20 that it was easy to
  // press Close (or the hardware B-button, which still just cancels)
  // thinking a just-captured binding was already in effect.
  const handleSaveAndClose = async () => {
    const saved = await handleSave();
    if (saved) {
      onClose();
    }
  };

  return (
    <div
      onClick={(event) => event.stopPropagation()}
      style={{
        display: "flex",
        flexDirection: "column",
        boxSizing: "border-box",
        color: "#f2efe9",
        fontSize: "14px",
        minHeight: "70vh",
        gap: "16px",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: "18px", fontWeight: 700 }}>Keybindings</div>
        <Button
          disabled={busy || hasAnyDuplicate || hasAnyUnsetKey}
          onClick={preventFormSubmit(() => handleSaveAndClose())}
        >
          Save & Close
        </Button>
      </div>
      <div style={{ fontSize: "12px", opacity: 0.75 }}>
        Click a key slot below, then press the actual button or key you want to bind - works for the Deck's own
        controller, an external gamepad, or a keyboard.
      </div>

      {COMMAND_ORDER.map((command) => {
        const meta = COMMAND_META[command];
        const commandBindings = bindings.filter((b) => b.command === command);
        return (
          <div key={command} style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
            <div style={{ fontSize: "16px", fontWeight: 600 }}>{meta.label}</div>
            <div style={{ fontSize: "12px", opacity: 0.75 }}>{meta.description}</div>

            {commandBindings.map((binding) => {
              const duplicate = isDuplicate(binding);
              const unset = hasUnsetKey(binding);
              return (
                <div
                  key={binding.id}
                  style={{
                    display: "flex",
                    flexWrap: "wrap",
                    alignItems: "center",
                    gap: "10px",
                    padding: "8px",
                    border: duplicate || unset ? "1px solid #ff8a80" : "1px solid rgba(255,255,255,0.15)",
                    borderRadius: "4px",
                  }}
                >
                  {binding.keys.map((key, index) => {
                    const listening = capturingFor?.id === binding.id && capturingFor?.index === index;
                    return (
                      <div key={index} style={{ display: "flex", alignItems: "center", gap: "4px" }}>
                        <div style={{ minWidth: "160px" }}>
                          <Button
                            disabled={capturingFor !== null && !listening}
                            onClick={preventFormSubmit(() => startCapture(binding.id, index))}
                          >
                            {listening ? "Listening..." : describeKey(key, capturedLabels)}
                          </Button>
                        </div>
                        {binding.keys.length > 1 && (
                          <Button onClick={preventFormSubmit(() => removeKeyFromBinding(binding.id, index))}>
                            x
                          </Button>
                        )}
                        {index < binding.keys.length - 1 && <span style={{ opacity: 0.7 }}>+</span>}
                      </div>
                    );
                  })}
                  {binding.keys.length < MAX_KEYS_PER_BINDING && (
                    <Button
                      disabled={capturingFor !== null}
                      onClick={preventFormSubmit(() => addKeyToBinding(binding.id))}
                    >
                      + Add key
                    </Button>
                  )}

                  {meta.supportsLongPress && (
                    <div style={{ minWidth: "140px" }}>
                      <Field label="Long press">
                        <ToggleField
                          checked={binding.long_press}
                          onChange={(checked) => updateBinding(binding.id, { long_press: checked })}
                        />
                      </Field>
                    </div>
                  )}
                  {meta.supportsLongPress && binding.long_press && (
                    <div style={{ minWidth: "220px" }}>
                      <SliderField
                        label="Threshold"
                        value={binding.threshold_ms}
                        min={300}
                        max={3000}
                        step={50}
                        showValue
                        editableValue
                        valueSuffix="ms"
                        onChange={(value) => updateBinding(binding.id, { threshold_ms: value })}
                      />
                    </div>
                  )}

                  <Button onClick={preventFormSubmit(() => deleteBinding(binding.id))}>Delete</Button>

                  {duplicate && (
                    <div style={{ fontSize: "11px", color: "#ff8a80", width: "100%" }}>
                      Duplicate of another binding (same keys, same long-press setting).
                    </div>
                  )}
                  {unset && (
                    <div style={{ fontSize: "11px", color: "#ff8a80", width: "100%" }}>
                      Press a key slot above to bind it before saving.
                    </div>
                  )}
                </div>
              );
            })}

            <div>
              <Button disabled={capturingFor !== null} onClick={preventFormSubmit(() => addBinding(command))}>
                Add binding
              </Button>
            </div>
          </div>
        );
      })}

      <div style={{ display: "flex", alignItems: "center", gap: "10px", marginTop: "8px" }}>
        <Button
          disabled={busy || hasAnyDuplicate || hasAnyUnsetKey}
          onClick={preventFormSubmit(() => handleSaveAndClose())}
        >
          Save & Close
        </Button>
        <div style={{ fontSize: "12px", opacity: 0.85 }}>
          {hasAnyDuplicate
            ? "Resolve the duplicate binding(s) above before saving."
            : hasAnyUnsetKey
              ? "Bind every key slot above before saving."
              : busy
                ? "working..."
                : status}
        </div>
      </div>
    </div>
  );
}

// Uses showModal + ModalRoot rather than a plain div in the QAM panel
// (gamepad focus-nav, dropdown portaling, and QAM's own right-third-of-
// screen clipping) - same reasoning as RoiCrop.tsx's openRoiCropEditor.
export async function openKeybindings() {
  // Awaited before the modal ever mounts - see RoiCrop.tsx's
  // openRoiCropEditor for why (the same fix, applied there for the same
  // reason after this exact race was confirmed live here first).
  const modalToken = createToken();
  await setDynamicRoiEditorOpen(true, modalToken).catch(() => {});

  let close = () => {};
  const modal = showModal(
    <ModalRoot bAllowFullSize onCancel={() => close()} closeModal={() => close()}>
      <KeybindingsContent onClose={() => close()} modalToken={modalToken} />
    </ModalRoot>,
    window
  );
  close = () => modal.Close();
}
