import { Button, Dropdown, Field, ModalRoot, SliderField, ToggleField, showModal } from "@decky/ui";
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

// No d-pad - see the keybinding design discussion (this software never
// takes over the d-pad). Dynamic Capture Start/Stop is deliberately never
// in this list either - UI-button only, never bindable.
const BINDABLE_KEYS = [
  "A", "B", "X", "Y",
  "L1", "L2", "L3", "L4", "L5",
  "R1", "R2", "R3", "R4", "R5",
  "select", "start",
  "trackpad_left_tap", "trackpad_right_tap",
];

const KEY_LABELS: Record<string, string> = {
  trackpad_left_tap: "Left Trackpad Tap",
  trackpad_right_tap: "Right Trackpad Tap",
};

const KEY_OPTIONS = BINDABLE_KEYS.map((key) => ({ data: key, label: KEY_LABELS[key] ?? key }));

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

function defaultKeyFor(existingKeys: string[]): string {
  return BINDABLE_KEYS.find((key) => !existingKeys.includes(key)) ?? BINDABLE_KEYS[0];
}

function KeybindingsContent({ onClose }: { onClose: () => void }) {
  const [bindings, setBindings] = useState<BindingDraft[]>([]);
  const [status, setStatus] = useState<string>("");
  const [busy, setBusy] = useState(false);

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
      keys: [BINDABLE_KEYS[0]],
      long_press: false,
      threshold_ms: DEFAULT_THRESHOLD_MS,
    };
    setBindings((prev) => [...prev, draft]);
  };

  const addKeyToBinding = (id: string) => {
    setBindings((prev) =>
      prev.map((b) => {
        if (b.id !== id || b.keys.length >= MAX_KEYS_PER_BINDING) return b;
        return { ...b, keys: [...b.keys, defaultKeyFor(b.keys)] };
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

  const handleSave = async () => {
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
      } else {
        setStatus(`error: ${result.error ?? "unknown"}`);
      }
    } catch (error) {
      setStatus(`error: ${errorMessage(error)}`);
    } finally {
      setBusy(false);
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
        <Button onClick={preventFormSubmit(onClose)}>Close</Button>
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
              return (
                <div
                  key={binding.id}
                  style={{
                    display: "flex",
                    flexWrap: "wrap",
                    alignItems: "center",
                    gap: "10px",
                    padding: "8px",
                    border: duplicate ? "1px solid #ff8a80" : "1px solid rgba(255,255,255,0.15)",
                    borderRadius: "4px",
                  }}
                >
                  {binding.keys.map((key, index) => (
                    <div key={index} style={{ display: "flex", alignItems: "center", gap: "4px" }}>
                      <div style={{ minWidth: "160px" }}>
                        <Dropdown
                          rgOptions={KEY_OPTIONS.filter(
                            (opt) => opt.data === key || !binding.keys.includes(opt.data)
                          )}
                          selectedOption={key}
                          onChange={(option) => setBindingKey(binding.id, index, String(option.data))}
                        />
                      </div>
                      {binding.keys.length > 1 && (
                        <Button onClick={preventFormSubmit(() => removeKeyFromBinding(binding.id, index))}>
                          x
                        </Button>
                      )}
                      {index < binding.keys.length - 1 && <span style={{ opacity: 0.7 }}>+</span>}
                    </div>
                  ))}
                  {binding.keys.length < MAX_KEYS_PER_BINDING && (
                    <Button onClick={preventFormSubmit(() => addKeyToBinding(binding.id))}>+ Add key</Button>
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
                </div>
              );
            })}

            <div>
              <Button onClick={preventFormSubmit(() => addBinding(command))}>Add binding</Button>
            </div>
          </div>
        );
      })}

      <div style={{ display: "flex", alignItems: "center", gap: "10px", marginTop: "8px" }}>
        <Button disabled={busy || hasAnyDuplicate} onClick={preventFormSubmit(() => handleSave())}>
          Save
        </Button>
        <div style={{ fontSize: "12px", opacity: 0.85 }}>
          {hasAnyDuplicate ? "Resolve the duplicate binding(s) above before saving." : busy ? "working..." : status}
        </div>
      </div>
    </div>
  );
}

// Uses showModal + ModalRoot rather than a plain div in the QAM panel
// (gamepad focus-nav, dropdown portaling, and QAM's own right-third-of-
// screen clipping) - same reasoning as RoiCrop.tsx's openRoiCropEditor.
export function openKeybindings() {
  let close = () => {};
  const modal = showModal(
    <ModalRoot bAllowFullSize onCancel={() => close()} closeModal={() => close()}>
      <KeybindingsContent onClose={() => close()} />
    </ModalRoot>,
    window
  );
  close = () => modal.Close();
}
