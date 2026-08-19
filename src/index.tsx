import {
  ButtonItem,
  DropdownItem,
  Navigation,
  PanelSection,
  PanelSectionRow,
  TextField,
  staticClasses,
  findAllModules,
} from "@decky/ui";
import { callable, definePlugin, routerHook } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { FaLanguage } from "react-icons/fa";
import { SiKofi } from "react-icons/si";
import { CompositionRequest, UIComposition } from "./Composition";
import { openKeybindings } from "./Keybindings";
import { openRoiCropEditor } from "./RoiCrop";

type CaptureStatus = {
  running: boolean;
  pid: number | null;
  returncode: number | null;
  engine_dir: string | null;
  capture_py: string | null;
  log_path: string;
  log_tail: string;
  translation_path: string;
  translation: string;
  translation_error?: string;
  translate_url?: string;
  translation_worker?: boolean;
  translation_in_progress?: boolean;
  last_settled_mtime_ns?: number | null;
  last_translated_image_mtime_ns?: number | null;
  translation_stale?: boolean;
  translation_engine?: string;
  translate_server_running?: boolean;
  translate_server_pid?: number | null;
  error?: string;
};

type EngineConfig = {
  api_key?: string;
  model?: string;
};

type TranslationSettings = {
  engine: string;
  source_lang: string;
  target_lang: string;
  ollama: EngineConfig;
  gemini: EngineConfig;
  google: EngineConfig;
  google_cloud: EngineConfig;
  deepl: EngineConfig;
};

const ENGINE_OPTIONS = [
  { data: "google_cloud", label: "Google Cloud Translation (paid, API key)" },
  { data: "deepl", label: "DeepL (paid, API key)" },
  { data: "gemini", label: "Gemini AI (paid, API key)" },
  { data: "ollama", label: "Ollama (local server)" },
  { data: "google", label: "Google Translate (free)" },
];

type OcrSettings = {
  engine: string;
  chromescreenai: { min_confidence?: number };
};

const OCR_ENGINE_OPTIONS = [
  { data: "chromescreenai", label: "Chrome Screen AI (default, on-device)" },
  { data: "tesseract", label: "Tesseract (debug fallback)" },
];

type ScreenAIStatus = {
  installed: boolean;
  size_bytes: number;
  approx_size_mb: number;
  downloading: boolean;
  progress: number;
  error?: string | null;
};

type OcrContainerStatus = {
  distrobox_installed: boolean;
  podman_installed: boolean;
  container_name: string;
  container_exists: boolean;
  running: boolean;
  ready: boolean;
  last_exit_code?: number | null;
  log_tail: string;
};

const getOcrSettings = callable<[], OcrSettings>("get_ocr_settings");
const setOcrSettings = callable<[Partial<OcrSettings>], OcrSettings>("set_ocr_settings");
const getScreenaiStatus = callable<[], ScreenAIStatus>("get_screenai_status");
const downloadScreenai = callable<[], { ok: boolean; error?: string }>("download_screenai");
const cancelScreenaiDownload = callable<[], { ok: boolean }>("cancel_screenai_download");
const deleteScreenai = callable<[], { ok: boolean; error?: string }>("delete_screenai");
const getOcrContainerStatus = callable<[], OcrContainerStatus>("get_ocr_container_status");
const provisionOcrContainer = callable<
  [],
  { ok: boolean; error?: string; already_running?: boolean; started?: boolean }
>("provision_ocr_container");

const LANGUAGE_OPTIONS = [
  { data: "English", label: "English" },
  { data: "Japanese", label: "Japanese" },
  { data: "Korean", label: "Korean" },
  { data: "Chinese", label: "Chinese" },
  { data: "Spanish", label: "Spanish" },
  { data: "French", label: "French" },
  { data: "German", label: "German" },
];

type HidrawTestResult = {
  success: boolean;
  device?: string;
  buttons: string[];
  error?: string;
};

type ActiveBlock = {
  id: number;
  text: string;
  translation: string;
  bbox: { x0: number; y0: number; x1: number; y1: number };
  last_changed: number;
};

type ActiveBlocksState = {
  blocks: ActiveBlock[];
  updated_at: number | null;
  capture_width: number | null;
  capture_height: number | null;
};

const getStatus = callable<[], CaptureStatus>("status");
const translateLatest = callable<[], CaptureStatus>("translate_latest");
const testHidrawButtonState = callable<[], HidrawTestResult>("test_hidraw_button_state");
const getActiveBlocks = callable<[], ActiveBlocksState>("get_active_blocks");
const getTranslationSettings = callable<[], TranslationSettings>("get_translation_settings");
const setTranslationSettings = callable<[Partial<TranslationSettings>], TranslationSettings>(
  "set_translation_settings"
);

type DynamicStatus = {
  running: boolean;
  paused?: boolean;
  qam_open?: boolean;
  pid: number | null;
  returncode: number | null;
  log_path: string;
  log_tail: string;
  blocks: ActiveBlock[];
  updated_at: number | null;
  error?: string;
};

type PauseToggleResult = { paused: boolean; error?: string };

type TapResult = {
  ok: boolean;
  matched?: boolean;
  text?: string;
  translation?: string;
  bbox?: { x0: number; y0: number; x1: number; y1: number };
  error?: string;
};

const startDynamicCapture = callable<[], DynamicStatus>("start_dynamic_capture");
const stopDynamicCapture = callable<[], DynamicStatus>("stop_dynamic_capture");
const refreshDynamicCapture = callable<[], DynamicStatus>("refresh_dynamic_capture");
const toggleDynamicPause = callable<[], PauseToggleResult>("toggle_dynamic_pause");
const setDynamicQamOpen = callable<[boolean], { qam_open: boolean }>("set_dynamic_qam_open");
const setDynamicStatusToastVisible = callable<[boolean], { status_toast_visible: boolean }>(
  "set_dynamic_status_toast_visible"
);
const getDynamicStatus = callable<[], DynamicStatus>("dynamic_status");
const requestTapTranslate = callable<[number, number], TapResult>("request_tap_translate");
const getDynamicRoi = callable<[], { ok: boolean; roi?: RoiPct | null; error?: string }>("get_dynamic_roi");
const startDynamicCaptureFixedRoi = callable<[RoiPct], DynamicStatus>("start_dynamic_capture_fixed_roi");

type RoiPct = { x_pct: number; y_pct: number; width_pct: number; height_pct: number };

type BindingCommand = "refresh" | "pause_resume" | "touch_translate";

type Binding = {
  id: string;
  command: BindingCommand;
  keys: string[];
  long_press: boolean;
  threshold_ms: number;
};

type KeybindingSettings = { bindings: Binding[] };

const getKeybindingSettings = callable<[], KeybindingSettings>("get_keybinding_settings");

/**
 * Close the QAM sidebar right after starting the dynamic engine, so its own
 * first discovery pass doesn't bake the sidebar's own text into the block
 * set (confirmed live - see PHASE_A_HANDOFF.md). `window.Navigation` turned
 * out to be an unrelated empty function, and SteamClient.UI/Overlay/Window/
 * Browser/Apps have nothing QAM-visibility-related either (all checked live
 * via a debug probe once in this codebase's history - see git log around
 * this comment if that's ever useful again). The real controller is an
 * already-instantiated singleton found structurally, not by a documented
 * name: some module holds an object that already has a working
 * `CloseSideMenus()` method attached (confirmed live: calling it does
 * close the sidebar). Located via @decky/ui's findAllModules, the same
 * module-search machinery Composition.tsx already relies on for a similar
 * "no documented API for this" situation.
 *
 * capture_dynamic.py's own startup delay + periodic re-discovery safety net
 * (see PHASE_A_HANDOFF.md) stay in place regardless, in case this ever
 * stops finding the instance (e.g. after a Steam UI update renames things).
 */
const tryCloseQuickAccessMenu = () => {
  try {
    let instance: any;
    findAllModules((m: any) => {
      if (instance || typeof m !== "object" || m === null) return false;
      for (const prop in m) {
        const val = m[prop];
        if (val && typeof val === "object" && typeof val.CloseSideMenus === "function") {
          instance = val;
          return true;
        }
      }
      return false;
    });
    instance?.CloseSideMenus();
  } catch {
    // best-effort only - the startup delay + periodic re-discovery in
    // capture_dynamic.py are the real safety net if this doesn't work.
  }
};

// Ko-fi's own floating-chat overlay-widget script never renders inside
// Decky's QAM (silently no-ops - almost certainly Steam's CSP blocking the
// remote <script src>, same conclusion the Decky-Translator reference
// plugin's own Ko-fi button implementation implies by avoiding script
// injection entirely). Handing off to the system browser instead is the
// approach confirmed working there.
const KOFI_URL = "https://ko-fi.com/neatsorg";

/** Dynamic-engine output counts as "live" only within this window - past it,
 * fall back to the single-region pipeline's status().translation so a stale
 * capture_dynamic.py run (or one that was never started) doesn't leave the
 * HUD showing frozen multi-block data. */
const ACTIVE_BLOCKS_FRESHNESS_S = 15;

// StatusToast's own default visible duration (see its durationMs ?? 2000
// below) plus a margin, for how long showToast() below should suppress
// Dynamic Capture so the toast itself never gets OCR'd as game text.
const STATUS_TOAST_SUPPRESS_MS = 2500;

// A key-set signature groups bindings that share the same combo regardless
// of order - e.g. ["L4","L2"] and ["L2","L4"] are the same group. The
// backend's duplicate-signature rule (main.py's _binding_signature) means
// at most one non-long-press ("tap") and one long-press ("hold") binding
// can ever share a signature, generalizing today's L4-dual-role (short tap
// = refresh, long hold = pause/resume) to any key-set/command pair.
function keySetSignature(keys: string[]): string {
  return [...keys].sort().join("+");
}

type KeySetGroup = {
  keys: string[];
  tapBinding: Binding | null;
  holdBinding: Binding | null;
};

type GroupRuntimeState = {
  holdStart: number | null;
  longPressFired: boolean;
  wasHeld: boolean;
};

function buildKeySetGroups(bindings: Binding[]): KeySetGroup[] {
  const groups = new Map<string, KeySetGroup>();
  for (const binding of bindings) {
    if (binding.command === "touch_translate") continue;
    const signature = keySetSignature(binding.keys);
    let group = groups.get(signature);
    if (!group) {
      group = { keys: [...binding.keys], tapBinding: null, holdBinding: null };
      groups.set(signature, group);
    }
    if (binding.long_press) {
      group.holdBinding = binding;
    } else {
      group.tapBinding = binding;
    }
  }
  return Array.from(groups.values());
}

function startHotkeyPolling() {
  let hotkeyBusy = false;
  let pollingInput = false;
  let holdStart: number | null = null;
  let showTriggeredForPress = false;
  let l4WasPressed = false;
  let l5WasPressed = false;
  let hudVisible = false;
  // Whether the dynamic engine process is currently running (regardless of
  // paused/producing-fresh-blocks) - see PositionedOverlay's dynamic_status
  // poll, the one place this is tracked continuously (Content's own copy
  // only updates while the QAM panel is open). While true, input is driven
  // entirely by the user's configured keybindings (see keySetGroups below)
  // instead of the legacy hold-to-show/press-to-hide SubtitleHud gestures
  // below. Once Dynamic mode is the only mode, the legacy branch goes away
  // entirely.
  let dynamicRunning = false;
  // Whether the dynamic engine is currently paused (see toggle_dynamic_
  // pause() in main.py) - sourced from the same continuous dynamic_status
  // poll as dynamicRunning (PositionedOverlay's effect, below). Tap-to-
  // translate (L4+L2 hold + touch long-press) is deliberately paused-only
  // (see the design discussion in PHASE_A_HANDOFF.md/the tap-to-translate
  // plan) - gating on this here, not just on the capture_dynamic.py side,
  // means holding L4+L2 while still actively translating just falls
  // through to L4's normal Dynamic-mode tap/hold behavior below instead of
  // silently eating touch input for a feature that can't do anything yet.
  let dynamicPaused = false;
  // Whether the L4+L2-hold touch-capture overlay (TapTranslateOverlay) is
  // currently shown - level-tracked (not edge-triggered) since it should
  // stay active for the whole time both buttons are held, however long
  // that is.
  let tapModeActive = false;
  const setTapModeActive = (active: boolean) => {
    if (tapModeActive === active) return;
    tapModeActive = active;
    window.dispatchEvent(new CustomEvent("playtranslate-tap-mode-changed", { detail: { active } }));
  };

  // Tracks the pending timer that clears status_toast_flag, so a second
  // showToast() call before the first one's window closes reschedules
  // instead of leaving a stale early-clearing timer behind.
  let statusToastFlagTimer: number | null = null;

  const showHud = (text: string, blocks?: ActiveBlock[]) => {
    hudVisible = true;
    window.dispatchEvent(new CustomEvent("playtranslate-show-hud", { detail: { text, blocks } }));
  };

  const hideHud = () => {
    hudVisible = false;
    window.dispatchEvent(new CustomEvent("playtranslate-hide-hud"));
  };

  const showToast = (text: string) => {
    window.dispatchEvent(new CustomEvent("playtranslate-status-toast", { detail: { text } }));
    // StatusToast (below) renders this on screen for ~2s - suppress Dynamic
    // Capture for that window so the toast itself never gets OCR'd and
    // "translated" as if it were game dialogue (confirmed live 2026-08-19,
    // see PHASE_A_HANDOFF.md - the L4-refresh toast was doing exactly this
    // once --startup-delay was cut). Cancel+reschedule on repeat calls,
    // matching StatusToast's own timer-reset logic, so a second toast
    // firing before the first one's window closes doesn't let capture
    // resume early while the second one is still showing.
    setDynamicStatusToastVisible(true).catch(() => {});
    if (statusToastFlagTimer !== null) {
      window.clearTimeout(statusToastFlagTimer);
    }
    statusToastFlagTimer = window.setTimeout(() => {
      statusToastFlagTimer = null;
      setDynamicStatusToastVisible(false).catch(() => {});
    }, STATUS_TOAST_SUPPRESS_MS);
  };

  // User-configured keybindings (see Keybindings.tsx), fetched once here
  // and re-fetched whenever the config screen saves - fetched into closure
  // vars rather than React state, matching every other piece of state in
  // this polling loop. keySetGroups/touchTranslateBindings are derived from
  // bindings; groupState tracks per-key-set hold/tap timing, keyed by the
  // same signature used to build the groups.
  let bindings: Binding[] = [];
  let keySetGroups: KeySetGroup[] = [];
  let touchTranslateBindings: Binding[] = [];
  const groupState = new Map<string, GroupRuntimeState>();

  const applyBindings = (newBindings: Binding[]) => {
    bindings = newBindings;
    keySetGroups = buildKeySetGroups(bindings);
    touchTranslateBindings = bindings.filter((b) => b.command === "touch_translate");
    groupState.clear();
    for (const group of keySetGroups) {
      groupState.set(keySetSignature(group.keys), { holdStart: null, longPressFired: false, wasHeld: false });
    }
  };

  const loadBindings = () => {
    getKeybindingSettings()
      .then((settings) => applyBindings(settings.bindings))
      .catch(() => {});
  };
  loadBindings();
  const handleKeybindingsChanged = () => loadBindings();
  window.addEventListener("playtranslate-keybindings-changed", handleKeybindingsChanged);

  // command dispatch shared by every key-set group below - "refresh" and
  // "pause_resume" are the only two commands a non-touch_translate group
  // can hold. whileHeld distinguishes a long-press fire (still holding,
  // matches the original "(release now)" toast wording) from a tap fire
  // (already released, that wording wouldn't make sense).
  const fireBinding = async (binding: Binding, whileHeld: boolean) => {
    hotkeyBusy = true;
    try {
      if (binding.command === "refresh") {
        const refreshed = await refreshDynamicCapture();
        if (refreshed.error) {
          console.warn(`refresh dynamic capture: ${refreshed.error}`);
        } else {
          showToast("LiveTranslator-kun: refreshed");
        }
      } else if (binding.command === "pause_resume") {
        const toggled = await toggleDynamicPause();
        if (toggled.error) {
          console.warn(`toggle dynamic pause: ${toggled.error}`);
        } else {
          const suffix = whileHeld ? " (release now)" : "";
          showToast(toggled.paused ? `LiveTranslator-kun: paused${suffix}` : `LiveTranslator-kun: resumed${suffix}`);
        }
      }
    } finally {
      hotkeyBusy = false;
    }
  };

  const handleHudVisible = () => {
    hudVisible = true;
  };
  const handleHudHidden = () => {
    hudVisible = false;
  };
  const resetGroupStates = () => {
    for (const state of groupState.values()) {
      state.holdStart = null;
      state.longPressFired = false;
      state.wasHeld = false;
    }
  };
  const handleDynamicRunningChanged = (event: Event) => {
    const detail = (event as CustomEvent<{ running?: boolean; paused?: boolean }>).detail;
    dynamicRunning = !!detail?.running;
    dynamicPaused = !!detail?.paused;
    holdStart = null;
    resetGroupStates();
    setTapModeActive(false);
  };
  window.addEventListener("playtranslate-hud-visible", handleHudVisible);
  window.addEventListener("playtranslate-hud-hidden", handleHudHidden);
  window.addEventListener("playtranslate-dynamic-running-changed", handleDynamicRunningChanged);

  const timer = window.setInterval(async () => {
    if (hotkeyBusy || pollingInput) {
      return;
    }

    pollingInput = true;
    try {
      const result = await testHidrawButtonState();
      if (!result.success) {
        holdStart = null;
        resetGroupStates();
        showTriggeredForPress = false;
        l4WasPressed = false;
        l5WasPressed = false;
        setTapModeActive(false);
        return;
      }

      // L5: edge-triggered "show the next block" - independent of L4's
      // show/hold handling below, only does anything while the HUD is up.
      // No Dynamic-mode meaning (yet), so gated off while it's active.
      const l5Pressed = result.buttons.includes("L5");
      if (l5Pressed && !l5WasPressed && hudVisible && !dynamicRunning) {
        window.dispatchEvent(new CustomEvent("playtranslate-cycle-hud"));
      }
      l5WasPressed = l5Pressed;

      const l4Pressed = result.buttons.includes("L4");

      if (dynamicRunning) {
        const heldSet = new Set(result.buttons);
        const now = Date.now();

        // Tap-to-translate: any touch_translate binding whose key-set is a
        // subset of what's currently held, while paused, hands the
        // touchscreen to TapTranslateOverlay (a long-press-tap there looks
        // up whatever block capture_dynamic.py has boxed at that point).
        // Checked first, ahead of the other bindings below, so holding the
        // combo fully overrides refresh/pause for as long as it's held -
        // releasing any of its keys falls straight back to normal
        // tap/hold-group behavior with no in-progress state left over (the
        // per-group reset below, generalized from the original L4+L2-only
        // version's holdStart/dynamicLongPressFired/l4WasPressed reset).
        const touchMatch =
          dynamicPaused && touchTranslateBindings.some((b) => b.keys.every((k) => heldSet.has(k)));
        if (touchMatch) {
          setTapModeActive(true);
          for (const group of keySetGroups) {
            const state = groupState.get(keySetSignature(group.keys));
            if (!state) continue;
            state.holdStart = null;
            state.longPressFired = false;
            state.wasHeld = group.keys.every((k) => heldSet.has(k));
          }
          return;
        }
        setTapModeActive(false);

        for (const group of keySetGroups) {
          const state = groupState.get(keySetSignature(group.keys));
          if (!state) continue;
          const isHeld = group.keys.every((k) => heldSet.has(k));

          if (isHeld && !state.wasHeld) {
            state.holdStart = now;
            state.longPressFired = false;
          } else if (
            isHeld &&
            state.wasHeld &&
            state.holdStart !== null &&
            !state.longPressFired &&
            group.holdBinding
          ) {
            // Long-press fires the instant the hold crosses the binding's
            // threshold - *while still held*, not on release - so the
            // toast appears as immediate feedback the user can watch for,
            // telling them it's safe to let go rather than having to guess
            // how long is long enough.
            if (now - state.holdStart >= group.holdBinding.threshold_ms) {
              state.longPressFired = true;
              await fireBinding(group.holdBinding, true);
            }
          } else if (!isHeld && state.wasHeld) {
            // A short tap (released before the threshold, or a key-set
            // with no hold variant configured at all) fires its tap
            // binding on release.
            if (state.holdStart !== null && !state.longPressFired && group.tapBinding) {
              await fireBinding(group.tapBinding, false);
            }
            state.holdStart = null;
            state.longPressFired = false;
          }
          state.wasHeld = isHeld;
        }
        return;
      }

      if (!l4Pressed) {
        holdStart = null;
        showTriggeredForPress = false;
        l4WasPressed = false;
        return;
      }

      const isNewPress = !l4WasPressed;
      l4WasPressed = true;
      if (isNewPress && hudVisible) {
        hideHud();
        showTriggeredForPress = true;
        return;
      }

      const now = Date.now();
      if (holdStart === null) {
        holdStart = now;
        return;
      }

      if (showTriggeredForPress || now - holdStart < 600) {
        return;
      }

      showTriggeredForPress = true;
      hotkeyBusy = true;
      try {
        const activeBlocks = await getActiveBlocks();
        const isFresh =
          activeBlocks.updated_at != null &&
          Date.now() / 1000 - activeBlocks.updated_at < ACTIVE_BLOCKS_FRESHNESS_S;
        if (isFresh && activeBlocks.blocks.length > 0) {
          const top = activeBlocks.blocks[0];
          showHud(top.translation || top.text, activeBlocks.blocks);
          return;
        }

        const next = await getStatus();
        // next.translation_stale compares last_settled.png's mtime against
        // the last-translated image - meaningless once the legacy engine
        // isn't running (e.g. preempted by the dynamic engine's mutual
        // exclusivity), since no new screenshots ever arrive to diverge
        // from, so translation_stale stays false forever and an arbitrarily
        // old cached translation looks perpetually fresh (confirmed live:
        // this showed a translation from a completely different, much
        // earlier scene). Require the engine to actually be running too.
        if (!next.error && next.running && next.translation && !next.translation_stale) {
          showHud(next.translation);
        } else if (!next.error && next.running && !next.translation_in_progress) {
          const translated = await translateLatest();
          if (!translated.error && translated.translation && !translated.translation_stale) {
            showHud(translated.translation);
          }
        }
      } finally {
        hotkeyBusy = false;
      }
    } catch {
      holdStart = null;
      resetGroupStates();
      showTriggeredForPress = false;
      l5WasPressed = false;
      setTapModeActive(false);
    } finally {
      pollingInput = false;
    }
  }, 150);

  return () => {
    window.clearInterval(timer);
    window.removeEventListener("playtranslate-hud-visible", handleHudVisible);
    window.removeEventListener("playtranslate-hud-hidden", handleHudHidden);
    window.removeEventListener("playtranslate-dynamic-running-changed", handleDynamicRunningChanged);
    window.removeEventListener("playtranslate-keybindings-changed", handleKeybindingsChanged);
  };
}

function SubtitleHud({
  visible,
  text,
}: {
  visible: boolean;
  text: string;
}) {
  if (!visible || !text.trim()) {
    return null;
  }

  return (
    <>
      <CompositionRequest level={UIComposition.Notification} />
      <div
        style={{
          position: "fixed",
          left: 0,
          right: 0,
          bottom: "7vh",
          zIndex: 8003,
          pointerEvents: "none",
          display: "flex",
          justifyContent: "center",
          padding: "0 7vw",
          boxSizing: "border-box",
        }}
      >
        <div
          style={{
            maxWidth: "1100px",
            padding: "14px 22px",
            borderRadius: "8px",
            background: "rgba(8, 10, 12, 0.84)",
            color: "#f7f4ee",
            fontSize: "26px",
            lineHeight: "34px",
            fontWeight: 600,
            textAlign: "center",
            textShadow: "0 2px 3px rgba(0, 0, 0, 0.85)",
            boxShadow: "0 8px 32px rgba(0, 0, 0, 0.45)",
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
          }}
        >
          {text}
        </div>
      </div>
    </>
  );
}

function GlobalHud() {
  const [visible, setVisible] = useState(false);
  const [translation, setTranslation] = useState("");
  const hudTimerRef = useRef<number | null>(null);
  const visibleRef = useRef(false);
  // Blocks + cursor for the L5 "next block" operation. Refs (not state)
  // because they're only ever read/written from event handlers, never
  // rendered directly - only `translation` (the currently-displayed text)
  // needs to trigger a re-render.
  const blocksRef = useRef<ActiveBlock[]>([]);
  const blockIndexRef = useRef(0);

  const releaseFocus = () => {
    window.setTimeout(() => {
      const active = document.activeElement;
      if (active instanceof HTMLElement) {
        active.blur();
      }
    }, 0);
    window.setTimeout(() => {
      const active = document.activeElement;
      if (active instanceof HTMLElement) {
        active.blur();
      }
    }, 120);
  };

  const resetHideTimer = (durationMs: number) => {
    if (hudTimerRef.current !== null) {
      window.clearTimeout(hudTimerRef.current);
    }
    hudTimerRef.current = window.setTimeout(() => {
      visibleRef.current = false;
      window.dispatchEvent(new CustomEvent("playtranslate-hud-hidden"));
      setVisible(false);
      hudTimerRef.current = null;
    }, durationMs);
  };

  const showHud = (text: string, durationMs = 6000, blocks: ActiveBlock[] = []) => {
    if (!text.trim()) {
      return;
    }
    setTranslation(text.trim());
    blocksRef.current = blocks;
    blockIndexRef.current = 0;
    visibleRef.current = true;
    window.dispatchEvent(new CustomEvent("playtranslate-hud-visible"));
    setVisible(true);
    resetHideTimer(durationMs);
    releaseFocus();
  };

  // L5: advance to the next block in the current priority list and keep
  // the HUD up while the user browses. No-op with 0 or 1 blocks (nothing
  // to cycle to) or while hidden.
  const cycleHud = () => {
    const blocks = blocksRef.current;
    if (!visibleRef.current || blocks.length < 2) {
      return;
    }
    blockIndexRef.current = (blockIndexRef.current + 1) % blocks.length;
    const next = blocks[blockIndexRef.current];
    const text = (next.translation || next.text || "").trim();
    if (text) {
      setTranslation(text);
    }
    resetHideTimer(6000);
  };

  const hideHud = () => {
    visibleRef.current = false;
    window.dispatchEvent(new CustomEvent("playtranslate-hud-hidden"));
    setVisible(false);
    if (hudTimerRef.current !== null) {
      window.clearTimeout(hudTimerRef.current);
      hudTimerRef.current = null;
    }
    releaseFocus();
  };

  useEffect(() => {
    const handleShow = (event: Event) => {
      const detail = (event as CustomEvent<{ text?: string; blocks?: ActiveBlock[] }>).detail;
      showHud(detail?.text ?? "", undefined, detail?.blocks ?? []);
    };
    const handleHide = () => {
      hideHud();
    };
    const handleCycle = () => {
      cycleHud();
    };

    window.addEventListener("playtranslate-show-hud", handleShow);
    window.addEventListener("playtranslate-hide-hud", handleHide);
    window.addEventListener("playtranslate-cycle-hud", handleCycle);
    return () => {
      window.removeEventListener("playtranslate-show-hud", handleShow);
      window.removeEventListener("playtranslate-hide-hud", handleHide);
      window.removeEventListener("playtranslate-cycle-hud", handleCycle);
    };
  }, []);

  return <SubtitleHud visible={visible} text={translation} />;
}

/**
 * Small corner status toast for Dynamic-mode L4 feedback (refresh/pause/
 * resume) - deliberately separate from SubtitleHud/PositionedOverlay so it
 * never competes for the same screen space or gets caught up in their
 * block-list/priority logic. Fire-and-forget: dispatch
 * "playtranslate-status-toast" with {text, durationMs?} and it shows,
 * auto-hides, done.
 */
function StatusToast() {
  const [text, setText] = useState("");
  const [visible, setVisible] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    const handleToast = (event: Event) => {
      const detail = (event as CustomEvent<{ text?: string; durationMs?: number }>).detail;
      if (!detail?.text) {
        return;
      }
      setText(detail.text);
      setVisible(true);
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
      timerRef.current = window.setTimeout(() => {
        setVisible(false);
        timerRef.current = null;
      }, detail.durationMs ?? 2000);
    };
    window.addEventListener("playtranslate-status-toast", handleToast);
    return () => {
      window.removeEventListener("playtranslate-status-toast", handleToast);
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
    };
  }, []);

  if (!visible) {
    return null;
  }

  return (
    <>
      <CompositionRequest level={UIComposition.Notification} />
      <div
        style={{
          position: "fixed",
          top: "3vh",
          right: "3vw",
          zIndex: 8004,
          pointerEvents: "none",
          padding: "5px 10px",
          borderRadius: "5px",
          background: "rgba(8, 10, 12, 0.86)",
          color: "#f7f4ee",
          fontSize: "12px",
          lineHeight: "15px",
          fontWeight: 600,
          textShadow: "0 1px 3px rgba(0, 0, 0, 0.85)",
          boxShadow: "0 4px 14px rgba(0, 0, 0, 0.4)",
        }}
      >
        {text}
      </div>
    </>
  );
}

// Long-press threshold for a touchscreen tap to count as "translate this
// point" while TapTranslateOverlay is active - independent of any
// keybinding's own long-press threshold (that's a button hold, this is a
// touch gesture), comfortably above an accidental brush of the screen.
const TAP_LONG_PRESS_MS = 450;
// Above this many screen pixels of movement, a held touch is treated as a
// drag/swipe and the pending tap-translate is cancelled.
const TAP_MOVE_CANCEL_PX = 12;

const clamp01 = (value: number) => Math.min(1, Math.max(0, value));

/**
 * Touch-capture overlay for tap-to-translate: shown only while the user
 * holds L4+L2 with the dynamic engine paused (see startHotkeyPolling's
 * combo check, which dispatches "playtranslate-tap-mode-changed" - the
 * L4/L2 button-hold logic itself lives there, not here, since that's
 * already where every other button-driven mode switch is decided).
 * `pointerEvents: "auto"` here is what's meant to actually claim touch
 * input away from the running game underneath - **unverified until tested
 * live on hardware** (every other overlay in this file is deliberately
 * pointerEvents:"none" for the opposite reason - see the tap-to-translate
 * design notes). If touches still reach the game while this is showing,
 * try switching the CompositionRequest level below from Notification to
 * Overlay (defined in Composition.tsx, currently unused anywhere).
 *
 * A long-press-tap (not a plain tap, see TAP_LONG_PRESS_MS) converts the
 * touch point to capture-pixel coordinates - via the overlay div's own
 * bounding rect, the same percentage-of-viewport convention
 * PositionedOverlay already uses in the other direction - and asks
 * main.py's request_tap_translate() for whatever block capture_dynamic.py
 * currently has boxed at that point. A match is shown via the existing
 * bottom HUD (GlobalHud, through the same "playtranslate-show-hud" event
 * L4's legacy hold-to-show gesture already uses - no new display surface
 * needed). No match, an unmatched tap, or any error shows a short toast
 * instead (StatusToast, same as Dynamic mode's refresh/pause feedback).
 */
function TapTranslateOverlay() {
  const [active, setActive] = useState(false);
  const overlayRef = useRef<HTMLDivElement>(null);
  const pendingRef = useRef<{ pointerId: number; startX: number; startY: number; timer: number } | null>(null);

  const clearPending = () => {
    if (pendingRef.current) {
      window.clearTimeout(pendingRef.current.timer);
      pendingRef.current = null;
    }
  };

  useEffect(() => {
    const handleTapMode = (event: Event) => {
      const detail = (event as CustomEvent<{ active?: boolean }>).detail;
      setActive(!!detail?.active);
    };
    window.addEventListener("playtranslate-tap-mode-changed", handleTapMode);
    return () => {
      window.removeEventListener("playtranslate-tap-mode-changed", handleTapMode);
      clearPending();
    };
  }, []);

  // The combo can release mid-gesture (user lets go of L4/L2 before the
  // long-press threshold) - don't let a pending timer fire into a
  // now-hidden overlay.
  useEffect(() => {
    if (!active) clearPending();
  }, [active]);

  const fireTapTranslate = async (clientX: number, clientY: number) => {
    const rect = overlayRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0 || rect.height === 0) return;
    const xPct = clamp01((clientX - rect.left) / rect.width);
    const yPct = clamp01((clientY - rect.top) / rect.height);
    const toast = (text: string) => window.dispatchEvent(new CustomEvent("playtranslate-status-toast", { detail: { text } }));
    try {
      const blocksState = await getActiveBlocks();
      if (!blocksState.capture_width || !blocksState.capture_height) {
        toast("LiveTranslator-kun: capture size not known yet");
        return;
      }
      const x = Math.round(xPct * blocksState.capture_width);
      const y = Math.round(yPct * blocksState.capture_height);
      const result = await requestTapTranslate(x, y);
      if (!result.ok) {
        toast(`LiveTranslator-kun: ${result.error ?? "tap failed"}`);
      } else if (result.matched) {
        const text = (result.translation || result.text || "").trim();
        if (text) {
          window.dispatchEvent(new CustomEvent("playtranslate-show-hud", { detail: { text } }));
        } else {
          toast("LiveTranslator-kun: no text here");
        }
      } else {
        toast("LiveTranslator-kun: no text here");
      }
    } catch (error) {
      toast(`LiveTranslator-kun: ${error instanceof Error ? error.message : String(error)}`);
    }
  };

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (pendingRef.current) return; // one gesture at a time
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // best-effort - a missed capture just means onPointerMove/Up might
      // not fire if the touch leaves this element, which can't happen here
      // anyway (this div covers the full viewport).
    }
    const startX = event.clientX;
    const startY = event.clientY;
    const timer = window.setTimeout(() => {
      pendingRef.current = null;
      fireTapTranslate(startX, startY);
    }, TAP_LONG_PRESS_MS);
    pendingRef.current = { pointerId: event.pointerId, startX, startY, timer };
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const pending = pendingRef.current;
    if (!pending || pending.pointerId !== event.pointerId) return;
    if (Math.hypot(event.clientX - pending.startX, event.clientY - pending.startY) > TAP_MOVE_CANCEL_PX) {
      clearPending();
    }
  };

  const onPointerEnd = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (pendingRef.current?.pointerId === event.pointerId) {
      clearPending();
    }
  };

  if (!active) {
    return null;
  }

  return (
    <>
      {/* Notification-level (used by every other overlay in this file)
          confirmed live NOT to capture touch input - taps still reached the
          game underneath with nothing here ever receiving a pointer event
          (no tap_request.json was ever written). Overlay is the only other
          level this codebase's Composition.tsx exposes, so it's the
          remaining candidate for actually claiming input priority the way
          the QAM itself does. */}
      <CompositionRequest level={UIComposition.Overlay} />
      <div
        ref={overlayRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerEnd}
        onPointerCancel={onPointerEnd}
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 8005,
          pointerEvents: "auto",
          touchAction: "none",
          background: "rgba(20, 110, 200, 0.08)",
          display: "flex",
          justifyContent: "center",
        }}
      >
        <div
          style={{
            marginTop: "3vh",
            height: "fit-content",
            padding: "6px 14px",
            borderRadius: "6px",
            background: "rgba(8, 10, 12, 0.8)",
            color: "#f7f4ee",
            fontSize: "13px",
            fontWeight: 600,
            textShadow: "0 1px 3px rgba(0, 0, 0, 0.85)",
            pointerEvents: "none",
          }}
        >
          LiveTranslator-kun: タップして翻訳
        </div>
      </div>
    </>
  );
}

/**
 * Real-time translation mode ("option 1" from the block-display design
 * discussion): each currently-valid dynamic-engine block is rendered as its
 * own small overlay positioned at its original on-screen location, instead
 * of funneling everything through the single bottom banner + L4/L5 cycling.
 * This directly resolves the observability gap found testing against NORCO
 * (which block L5 will show next was never obvious) - translations just
 * appear where the source text is, no cycling needed.
 *
 * Independent of and additive to GlobalHud/L4/L5: renders nothing when the
 * dynamic engine isn't producing fresh data, so the legacy single-region
 * pipeline's bottom-HUD flow is completely unaffected.
 *
 * capture_dynamic.py already clears a block's translation the moment its
 * region starts changing (before it resettles) specifically so a
 * scrolling/growing text box (confirmed on NORCO) doesn't leave a
 * stale-position translation on screen - this component just needs to
 * stop rendering blocks that disappear from the list, which it does for
 * free by rendering from the polled block list directly.
 */
function PositionedOverlay() {
  const [blocks, setBlocks] = useState<ActiveBlock[]>([]);
  const [captureSize, setCaptureSize] = useState<{ w: number; h: number } | null>(null);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const state = await getActiveBlocks();
        if (cancelled) return;
        const isFresh =
          state.updated_at != null && Date.now() / 1000 - state.updated_at < ACTIVE_BLOCKS_FRESHNESS_S;
        if (isFresh && state.capture_width && state.capture_height) {
          setBlocks(state.blocks);
          setCaptureSize({ w: state.capture_width, h: state.capture_height });
        } else {
          setBlocks([]);
          setCaptureSize(null);
        }
      } catch {
        setBlocks([]);
        setCaptureSize(null);
      }
    };
    poll();
    // Lighter cadence than the 150ms hotkey poll - this drives passive
    // display refresh, not button-press responsiveness.
    const timer = window.setInterval(poll, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  // Separate from the block poll above: tracks whether the dynamic engine
  // process itself is running (not whether it currently has fresh blocks -
  // a paused engine has none, but L4 still needs to be in "Dynamic mode"
  // to resume it). Only dispatches on an actual change, and only reads
  // dynamic_status() - this is the one continuously-running poll of it,
  // since Content's own copy only runs while the QAM panel is mounted.
  // startHotkeyPolling listens for this to decide whether L4 should use
  // legacy (SubtitleHud) or Dynamic-mode (refresh/pause) behavior.
  useEffect(() => {
    let cancelled = false;
    let lastRunning: boolean | null = null;
    let lastPaused: boolean | null = null;
    const poll = async () => {
      try {
        const state = await getDynamicStatus();
        if (cancelled) return;
        const paused = !!state.paused;
        if (state.running !== lastRunning || paused !== lastPaused) {
          lastRunning = state.running;
          lastPaused = paused;
          window.dispatchEvent(
            new CustomEvent("playtranslate-dynamic-running-changed", { detail: { running: state.running, paused } })
          );
        }
      } catch {
        if (!cancelled && (lastRunning !== false || lastPaused !== false)) {
          lastRunning = false;
          lastPaused = false;
          window.dispatchEvent(
            new CustomEvent("playtranslate-dynamic-running-changed", { detail: { running: false, paused: false } })
          );
        }
      }
    };
    poll();
    const timer = window.setInterval(poll, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  // Auto-suppresses capture while the QAM sidebar is open, regardless of
  // which button (if any) the user is about to press inside it - see
  // PHASE_A_HANDOFF.md's 2026-08-19 section. Confirmed live via steam-debug
  // that Steam's own gamepad-UI window tracks this as a plain synchronous
  // property, no findAllModules search needed unlike tryCloseQuickAccessMenu
  // above: window.SteamUIStore.m_WindowStore.GamepadUIMainWindowInstance.
  // m_MenuStore.m_eOpenSideMenu, where 0 = closed, 1 = MainMenu, 2 =
  // QuickAccess. Polled fast (300ms) rather than at the 1500ms block-poll
  // cadence, since the whole point is to close the false-capture window
  // between the QAM opening and the user finding/pressing a button inside
  // it - a slower poll would just shrink that window, not close it.
  // set_dynamic_qam_open() is a no-op read on the backend if this ever
  // fails to resolve (optional chaining below falls through to `false`,
  // i.e. "not open") - failing open here would silently wedge capture off
  // forever if this property ever gets renamed by a Steam UI update, which
  // is a worse failure mode than losing this protection.
  useEffect(() => {
    let lastOpen = false;
    const poll = () => {
      const menuId = (window as any).SteamUIStore?.m_WindowStore?.GamepadUIMainWindowInstance?.m_MenuStore
        ?.m_eOpenSideMenu;
      const isOpen = typeof menuId === "number" && menuId !== 0;
      if (isOpen !== lastOpen) {
        lastOpen = isOpen;
        setDynamicQamOpen(isOpen).catch(() => {});
      }
    };
    poll();
    const timer = window.setInterval(poll, 300);
    return () => {
      window.clearInterval(timer);
      if (lastOpen) {
        setDynamicQamOpen(false).catch(() => {});
      }
    };
  }, []);

  if (!captureSize || blocks.length === 0) {
    return null;
  }

  return (
    <>
      <CompositionRequest level={UIComposition.Notification} />
      <div
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 8002,
          pointerEvents: "none",
        }}
      >
        {blocks.map((b) => {
          const leftPct = (b.bbox.x0 / captureSize.w) * 100;
          const topPct = (b.bbox.y0 / captureSize.h) * 100;
          const widthPct = ((b.bbox.x1 - b.bbox.x0) / captureSize.w) * 100;
          return (
            <div
              key={b.id}
              style={{
                position: "absolute",
                left: `${leftPct}%`,
                top: `${topPct}%`,
                width: `${widthPct}%`,
                minWidth: "80px",
                maxWidth: "42vw",
                padding: "3px 7px",
                borderRadius: "4px",
                background: "rgba(8, 10, 12, 0.86)",
                color: "#f7f4ee",
                fontSize: "12px",
                lineHeight: "15px",
                fontWeight: 600,
                textAlign: "left",
                textShadow: "0 1px 3px rgba(0, 0, 0, 0.85)",
                boxShadow: "0 4px 14px rgba(0, 0, 0, 0.4)",
                whiteSpace: "pre-wrap",
                overflowWrap: "anywhere",
              }}
            >
              {b.translation}
            </div>
          );
        })}
      </div>
    </>
  );
}

function Content() {
  const [status, setStatus] = useState<CaptureStatus | undefined>();
  const [dynamicStatus, setDynamicStatus] = useState<DynamicStatus | undefined>();
  const [regionModeStatus, setRegionModeStatus] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [activeTab, setActiveTab] = useState("main");
  const [translationSettings, setTranslationSettingsState] = useState<TranslationSettings | undefined>();
  const [geminiKeyInput, setGeminiKeyInput] = useState("");
  const [geminiModelInput, setGeminiModelInput] = useState("");
  const [deeplKeyInput, setDeeplKeyInput] = useState("");
  const [googleCloudKeyInput, setGoogleCloudKeyInput] = useState("");
  const [ocrSettings, setOcrSettingsState] = useState<OcrSettings | undefined>();
  const [screenaiStatus, setScreenaiStatus] = useState<ScreenAIStatus | undefined>();

  useEffect(() => {
    (async () => {
      const settings = await getTranslationSettings();
      setTranslationSettingsState(settings);
      setGeminiKeyInput(settings.gemini?.api_key ?? "");
      setGeminiModelInput(settings.gemini?.model ?? "gemini-3.6-flash");
      setDeeplKeyInput(settings.deepl?.api_key ?? "");
      setGoogleCloudKeyInput(settings.google_cloud?.api_key ?? "");
    })();
  }, []);

  useEffect(() => {
    (async () => {
      setOcrSettingsState(await getOcrSettings());
    })();
  }, []);

  // Polls continuously (not just while downloading) so switching to the OCR
  // tab always shows current install state without an extra round trip -
  // same "just keep polling" approach the dynamic-status poll above uses.
  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const next = await getScreenaiStatus();
        if (!cancelled) setScreenaiStatus(next);
      } catch {
        // transient RPC hiccup - keep the last known status, try again next tick
      }
    };
    poll();
    const timer = window.setInterval(poll, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const [ocrContainerStatus, setOcrContainerStatus] = useState<OcrContainerStatus | undefined>();
  const [showOcrSetupLog, setShowOcrSetupLog] = useState(false);

  // Same "just keep polling" approach as screenaiStatus above - the OCR
  // worker's distrobox container is a prerequisite for every OCR engine
  // (not just Tesseract), so its readiness is worth surfacing any time the
  // OCR tab is open, not only during an active setup run.
  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const next = await getOcrContainerStatus();
        if (!cancelled) setOcrContainerStatus(next);
      } catch {
        // transient RPC hiccup - keep the last known status, try again next tick
      }
    };
    poll();
    const timer = window.setInterval(poll, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const applyTranslationSettings = async (patch: Partial<TranslationSettings>) => {
    const next = await setTranslationSettings(patch);
    setTranslationSettingsState(next);
  };

  const applyOcrSettings = async (patch: Partial<OcrSettings>) => {
    const next = await setOcrSettings(patch);
    setOcrSettingsState(next);
  };

  const commitGeminiConfig = () => {
    applyTranslationSettings({ gemini: { api_key: geminiKeyInput, model: geminiModelInput || "gemini-3.6-flash" } });
  };

  const commitDeeplConfig = () => {
    applyTranslationSettings({ deepl: { api_key: deeplKeyInput } });
  };

  const commitGoogleCloudConfig = () => {
    applyTranslationSettings({ google_cloud: { api_key: googleCloudKeyInput } });
  };

  const refresh = async () => {
    const next = await getStatus();
    setStatus(next);
    const nextDynamic = await getDynamicStatus();
    setDynamicStatus(nextDynamic);
  };

  const runDynamicAction = async (action: () => Promise<DynamicStatus>, title: string) => {
    setBusy(true);
    try {
      const next = await action();
      setDynamicStatus(next);
      if (next.error) {
        console.warn(`${title}: ${next.error}`);
      }
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => window.clearInterval(timer);
  }, []);

  const dynamicStateText = dynamicStatus?.running
    ? `Running${dynamicStatus.pid ? ` (${dynamicStatus.pid})` : ""}${dynamicStatus.paused ? " / paused" : ""}`
    : "Stopped";

  const mainTabContent = (
    <>
    <PanelSection title="Capture Control">
      <PanelSectionRow>
        <div>{dynamicStateText}</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          disabled={busy || dynamicStatus?.running === true}
          layout="below"
          onClick={() => {
            runDynamicAction(startDynamicCapture, "LiveTranslator-kun dynamic started");
            tryCloseQuickAccessMenu();
          }}
        >
          Start Capture
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          disabled={busy || dynamicStatus?.running !== true}
          layout="below"
          onClick={() => runDynamicAction(stopDynamicCapture, "LiveTranslator-kun dynamic stopped")}
        >
          Stop Capture
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          disabled={busy}
          layout="below"
          onClick={async () => {
            setBusy(true);
            setRegionModeStatus("");
            try {
              const saved = await getDynamicRoi();
              if (!saved.ok || !saved.roi) {
                setRegionModeStatus(saved.error || "no region configured yet - use Region Mode Config first");
                return;
              }
              // Fired before the start RPC, not after (matching the wide
              // "Start Capture" button above) - closing the QAM only after
              // the new process is already capturing left its own
              // on-screen text as fair game for the engine's first
              // (self-confirming, non-retried) read. start_dynamic_capture()
              // also self-stops+restarts when the requested mode differs
              // from whatever's currently running, which is what lets this
              // button stay enabled at all times instead of no-oping.
              tryCloseQuickAccessMenu();
              const next = await startDynamicCaptureFixedRoi(saved.roi);
              setDynamicStatus(next);
              setRegionModeStatus(next.error || "");
            } finally {
              setBusy(false);
            }
          }}
        >
          Start Region Mode
        </ButtonItem>
      </PanelSectionRow>
      {regionModeStatus && (
        <PanelSectionRow>
          <div style={{ fontSize: "11px", color: "#ff8a80" }}>{regionModeStatus}</div>
        </PanelSectionRow>
      )}
    </PanelSection>
    <PanelSection title="Capture Settings">
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => setActiveTab("translation")}>
          Translation Settings
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => setActiveTab("ocr")}>
          OCR Settings
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
    <PanelSection title="Other Settings">
      <PanelSectionRow>
        <ButtonItem
          disabled={busy}
          layout="below"
          onClick={() => {
            // Closing QAM first (same as Region Mode Config below) matters
            // beyond cosmetics: without it, dismissing this modal via B
            // instead of its own Close button falls back to Steam's
            // gamepad-nav stack showing the QAM side menu again (it's still
            // the "parent" screen) instead of the game - confirmed live
            // 2026-08-20, that window opened a gap where neither
            // dynamic_qam_open_flag_path nor dynamic_roi_editor_open_flag_
            // path was reliably set in time, so Dynamic Capture read QAM's
            // own on-screen content as if it were game text. Region Mode
            // Config never hit this because it already closed QAM first,
            // so there's never a QAM to fall back to.
            tryCloseQuickAccessMenu();
            openKeybindings();
          }}
        >
          Key Bindings
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          disabled={busy}
          layout="below"
          onClick={() => {
            tryCloseQuickAccessMenu();
            openRoiCropEditor();
          }}
        >
          Region Mode Config
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
    <PanelSection>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={() => {
            Navigation.CloseSideMenus();
            Navigation.NavigateToExternalWeb(KOFI_URL);
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}>
            <SiKofi style={{ fontSize: "13px" }} />
            <span>Support</span>
          </div>
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
    </>
  );

  const translationTabContent = (
    <>
    <PanelSection title="Translation Engine">
      <PanelSectionRow>
        <DropdownItem
          label="Engine"
          rgOptions={ENGINE_OPTIONS}
          selectedOption={translationSettings?.engine ?? "ollama"}
          onChange={(option) => applyTranslationSettings({ engine: String(option.data) })}
        />
      </PanelSectionRow>
      {translationSettings?.engine === "gemini" && (
        <>
          <PanelSectionRow>
            <TextField
              label="Gemini API Key"
              bIsPassword
              value={geminiKeyInput}
              onChange={(e) => setGeminiKeyInput(e.target.value)}
              onBlur={commitGeminiConfig}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <TextField
              label="Gemini Model"
              value={geminiModelInput}
              onChange={(e) => setGeminiModelInput(e.target.value)}
              onBlur={commitGeminiConfig}
            />
          </PanelSectionRow>
        </>
      )}
      {translationSettings?.engine === "deepl" && (
        <PanelSectionRow>
          <TextField
            label="DeepL API Key"
            bIsPassword
            value={deeplKeyInput}
            onChange={(e) => setDeeplKeyInput(e.target.value)}
            onBlur={commitDeeplConfig}
          />
        </PanelSectionRow>
      )}
      {translationSettings?.engine === "google" && (
        <PanelSectionRow>
          <div style={{ fontSize: "11px", opacity: 0.8 }}>
            Uses Google's free translation endpoint - no API key needed.
          </div>
        </PanelSectionRow>
      )}
      {translationSettings?.engine === "google_cloud" && (
        <>
          <PanelSectionRow>
            <TextField
              label="Google Cloud API Key"
              bIsPassword
              value={googleCloudKeyInput}
              onChange={(e) => setGoogleCloudKeyInput(e.target.value)}
              onBlur={commitGoogleCloudConfig}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <div style={{ fontSize: "11px", opacity: 0.8 }}>
              Needs the Cloud Translation API enabled (with billing set up -
              it has a free monthly quota) on a Google Cloud project, and an
              API key from there. Not an LLM, so no "thinking" overhead -
              should be much faster than Gemini for plain translation.
            </div>
          </PanelSectionRow>
        </>
      )}
      {translationSettings?.engine === "ollama" && (
        <PanelSectionRow>
          <div style={{ fontSize: "11px", opacity: 0.8 }}>
            Uses the LAN server URL configured via translate_url.txt / PLAYTRANSLATE_TRANSLATE_URL.
            Unaffected by this screen.
          </div>
        </PanelSectionRow>
      )}
      {translationSettings && translationSettings.engine !== "ollama" && (
        <PanelSectionRow>
          <div style={{ fontSize: "11px", opacity: 0.8 }}>
            Local translate server: {status?.translate_server_running ? "running" : "starting / stopped"}
          </div>
        </PanelSectionRow>
      )}
    </PanelSection>
    <PanelSection title="Language">
      <PanelSectionRow>
        <DropdownItem
          label="Source Language"
          rgOptions={LANGUAGE_OPTIONS}
          selectedOption={translationSettings?.source_lang ?? "English"}
          onChange={(option) => applyTranslationSettings({ source_lang: String(option.data) })}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem
          label="Target Language"
          rgOptions={LANGUAGE_OPTIONS}
          selectedOption={translationSettings?.target_lang ?? "Japanese"}
          onChange={(option) => applyTranslationSettings({ target_lang: String(option.data) })}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={{ fontSize: "11px", opacity: 0.8 }}>
          Under construction: only Source = English / Target = Japanese is
          tested and supported today. OCR may pick up other scripts, but the
          translation prompts (especially Ollama/Gemini) are hand-tuned for
          English source text, so other language pairs aren't supported yet.
        </div>
      </PanelSectionRow>
    </PanelSection>
    </>
  );

  const ocrTabContent = (
    <>
    <PanelSection title="OCR Environment">
      <PanelSectionRow>
        <div style={{ fontSize: "11px", opacity: 0.8 }}>
          The OCR worker runs inside a distrobox container named "
          {ocrContainerStatus?.container_name ?? "playtranslate-ocr"}" - required for every OCR
          engine, including Chrome Screen AI below.
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={{ fontSize: "11px", opacity: 0.8 }}>
          distrobox: {ocrContainerStatus?.distrobox_installed ? "installed" : "not installed"} · container:{" "}
          {ocrContainerStatus?.container_exists ? "created" : "not created"}
        </div>
      </PanelSectionRow>
      {ocrContainerStatus?.running && (
        <PanelSectionRow>
          <div style={{ fontSize: "11px", opacity: 0.8 }}>
            Setting up… this installs distrobox/podman and the container on first run, which can
            take a few minutes.
          </div>
        </PanelSectionRow>
      )}
      {!ocrContainerStatus?.running && ocrContainerStatus?.last_exit_code != null && ocrContainerStatus.last_exit_code !== 0 && (
        <PanelSectionRow>
          <div style={{ fontSize: "11px", color: "#ff6b6b" }}>
            Setup exited with code {ocrContainerStatus.last_exit_code} - see the log below.
          </div>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        {!ocrContainerStatus?.running && (
          <ButtonItem
            layout="below"
            onClick={async () => {
              await provisionOcrContainer();
              setOcrContainerStatus(await getOcrContainerStatus());
            }}
          >
            {ocrContainerStatus?.ready ? "Re-run OCR Environment Setup" : "Set Up OCR Environment"}
          </ButtonItem>
        )}
      </PanelSectionRow>
      {ocrContainerStatus?.log_tail && (
        <>
          <PanelSectionRow>
            {/* Steam's QAM overlay doesn't reliably deliver the click/focus
              events a plain <details>/<summary> needs to toggle (confirmed
              live: tapping it did nothing) - an explicit ButtonItem-driven
              state toggle uses the same input path every other button in
              this panel already relies on. */}
            <ButtonItem layout="below" onClick={() => setShowOcrSetupLog((prev) => !prev)}>
              {showOcrSetupLog ? "Hide Setup Log" : "Show Setup Log"}
            </ButtonItem>
          </PanelSectionRow>
          {showOcrSetupLog && (
            <PanelSectionRow>
              <pre
                style={{
                  fontSize: "10px",
                  whiteSpace: "pre-wrap",
                  maxHeight: "160px",
                  overflowY: "auto",
                  margin: "4px 0 0",
                }}
              >
                {ocrContainerStatus.log_tail}
              </pre>
            </PanelSectionRow>
          )}
        </>
      )}
    </PanelSection>
    <PanelSection title="OCR Engine">
      <PanelSectionRow>
        <div style={{ fontSize: "11px", opacity: 0.8, marginBottom: "4px" }}>
          Controls Dynamic Capture's full-frame text discovery only. The
          fixed-region calibration path always uses Tesseract.
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem
          label="Engine"
          rgOptions={OCR_ENGINE_OPTIONS}
          selectedOption={ocrSettings?.engine ?? "chromescreenai"}
          onChange={(option) => applyOcrSettings({ engine: String(option.data) })}
        />
      </PanelSectionRow>
      {ocrSettings?.engine === "chromescreenai" && (
        <>
          <PanelSectionRow>
            <div style={{ fontSize: "11px", opacity: 0.8 }}>
              On-device neural OCR (Chromium's accessibility screen reader
              engine). Groups detected text into blocks itself, no manual
              region calibration needed. Downloads ~{screenaiStatus?.approx_size_mb ?? 120}MB on first use.
            </div>
          </PanelSectionRow>
          {screenaiStatus?.installed && (
            <PanelSectionRow>
              <div style={{ fontSize: "11px", opacity: 0.8 }}>
                Installed ({Math.round((screenaiStatus.size_bytes / 1024 / 1024) * 10) / 10} MB)
              </div>
            </PanelSectionRow>
          )}
          {screenaiStatus?.downloading && (
            <PanelSectionRow>
              <div
                style={{
                  height: "6px",
                  borderRadius: "3px",
                  background: "rgba(255,255,255,0.15)",
                  overflow: "hidden",
                  margin: "4px 0",
                }}
              >
                <div
                  style={{
                    height: "100%",
                    width: `${Math.round((screenaiStatus.progress ?? 0) * 100)}%`,
                    background: "#1a9fff",
                  }}
                />
              </div>
              <div style={{ fontSize: "11px", opacity: 0.8 }}>
                Downloading… {Math.round((screenaiStatus.progress ?? 0) * 100)}%
              </div>
            </PanelSectionRow>
          )}
          {screenaiStatus?.error && (
            <PanelSectionRow>
              <div style={{ fontSize: "11px", color: "#ff6b6b" }}>{screenaiStatus.error}</div>
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            {!screenaiStatus?.installed && !screenaiStatus?.downloading && (
              <ButtonItem
                layout="below"
                onClick={async () => {
                  await downloadScreenai();
                  setScreenaiStatus(await getScreenaiStatus());
                }}
              >
                Download
              </ButtonItem>
            )}
            {screenaiStatus?.downloading && (
              <ButtonItem
                layout="below"
                onClick={async () => {
                  await cancelScreenaiDownload();
                  setScreenaiStatus(await getScreenaiStatus());
                }}
              >
                Cancel
              </ButtonItem>
            )}
            {screenaiStatus?.installed && !screenaiStatus?.downloading && (
              <ButtonItem
                layout="below"
                onClick={async () => {
                  await deleteScreenai();
                  setScreenaiStatus(await getScreenaiStatus());
                }}
              >
                Delete
              </ButtonItem>
            )}
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
    </>
  );

  // @decky/ui's <Tabs> renders its content pane with a height that only
  // resolves against a percentage/flex-sized ancestor - something Steam's
  // own QAM tab views get for free but a plugin's `content` root doesn't
  // provide, so the whole pane (and every PanelSectionRow inside it)
  // collapsed to a few pixels here (confirmed live via CDP: the actual
  // content rendered at its full natural height, ~1100px+900px, nested
  // inside ancestors whose own computed height was ~56px). A plain
  // state-toggled pair of buttons sidesteps that entirely - no absolute
  // positioning or percentage-height math involved, just normal document
  // flow, same as this panel used before tabs were added.
  const backButton = (
    <PanelSection>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => setActiveTab("main")}>
          ← Back
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );

  return (
    <>
      {activeTab === "main" && mainTabContent}
      {activeTab === "translation" && (
        <>
          {backButton}
          {translationTabContent}
        </>
      )}
      {activeTab === "ocr" && (
        <>
          {backButton}
          {ocrTabContent}
        </>
      )}
    </>
  );
}

/**
 * Auto-pause dynamic capture while the QAM sidebar is open, so its own text
 * (button labels, status lines, whichever tab is showing) doesn't get
 * discovered and translated as if it were game dialogue - confirmed live
 * (via CDP) that document.visibilityState on this popup's own window
 * tracks QAM open/close directly (visible while open, hidden while
 * closed), so no Steam-internal singleton/module search is needed here,
 * unlike tryCloseQuickAccessMenu() above.
 *
 * Tracks whether *this* listener was the one that paused it, so a pause
 * the user already set manually (L4 long-hold) before opening the QAM is
 * left alone on close instead of being force-resumed.
 */
// Confirmed live (steam-debug + a log read): dynamic capture discovered the
// QAM's own engine-dropdown label text (e.g. "Ollama (LAN server)") right
// after a rapid QAM close - document.visibilityState flips to "hidden"
// before Steam's own closing animation has actually finished, so an
// immediate resume can still catch a frame or two of QAM content. Not
// measured precisely; picked to comfortably clear a normal close animation
// without making the pause feel sticky.
const QAM_RESUME_DEBOUNCE_MS = 400;

function startQamPauseSync() {
  let pausedByThis = false;
  let resumeTimer: number | null = null;

  const cancelPendingResume = () => {
    if (resumeTimer !== null) {
      window.clearTimeout(resumeTimer);
      resumeTimer = null;
    }
  };

  const handleVisibilityChange = async () => {
    try {
      if (document.visibilityState === "visible") {
        // QAM reopened before the debounced resume below fired - it's
        // still paused, so just cancel the pending resume and stay put.
        cancelPendingResume();
        const status = await getDynamicStatus();
        if (status.running && !status.paused) {
          const result = await toggleDynamicPause();
          if (!result.error && result.paused) {
            pausedByThis = true;
          }
        }
      } else if (pausedByThis) {
        cancelPendingResume();
        resumeTimer = window.setTimeout(async () => {
          resumeTimer = null;
          pausedByThis = false;
          const result = await toggleDynamicPause();
          if (result.error) {
            console.warn(`QAM auto-resume: ${result.error}`);
          }
        }, QAM_RESUME_DEBOUNCE_MS);
      }
    } catch (error) {
      console.warn(`QAM pause sync: ${error instanceof Error ? error.message : String(error)}`);
    }
  };

  document.addEventListener("visibilitychange", handleVisibilityChange);
  return () => {
    cancelPendingResume();
    document.removeEventListener("visibilitychange", handleVisibilityChange);
  };
}

export default definePlugin(() => {
  const stopHotkeyPolling = startHotkeyPolling();
  const stopQamPauseSync = startQamPauseSync();
  routerHook.addGlobalComponent("PlayTranslateHud", () => <GlobalHud />);
  routerHook.addGlobalComponent("PlayTranslatePositionedOverlay", () => <PositionedOverlay />);
  routerHook.addGlobalComponent("PlayTranslateStatusToast", () => <StatusToast />);
  routerHook.addGlobalComponent("PlayTranslateTapOverlay", () => <TapTranslateOverlay />);

  return {
    name: "LiveTranslator-kun",
    titleView: <div className={staticClasses.Title}>LiveTranslator-kun</div>,
    content: <Content />,
    icon: <FaLanguage />,
    onDismount() {
      stopHotkeyPolling();
      stopQamPauseSync();
      routerHook.removeGlobalComponent("PlayTranslateHud");
      routerHook.removeGlobalComponent("PlayTranslatePositionedOverlay");
      routerHook.removeGlobalComponent("PlayTranslateStatusToast");
      routerHook.removeGlobalComponent("PlayTranslateTapOverlay");
    },
    alwaysRender: true,
  };
});
