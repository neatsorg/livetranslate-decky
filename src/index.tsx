import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  staticClasses,
  findAllModules,
} from "@decky/ui";
import { callable, definePlugin, routerHook } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { FaLanguage } from "react-icons/fa";
import { CompositionRequest, UIComposition } from "./Composition";
import { openRegionCalibration } from "./Calibration";

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
  error?: string;
};

type AiServerStatus = {
  ok: boolean;
  url: string;
  status?: number;
  body?: string;
  error?: string;
};

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

const startCapture = callable<[], CaptureStatus>("start_capture");
const stopCapture = callable<[], CaptureStatus>("stop_capture");
const getStatus = callable<[], CaptureStatus>("status");
const translateLatest = callable<[], CaptureStatus>("translate_latest");
const testHidrawButtonState = callable<[], HidrawTestResult>("test_hidraw_button_state");
const checkAiServer = callable<[], AiServerStatus>("check_ai_server");
const getActiveBlocks = callable<[], ActiveBlocksState>("get_active_blocks");

type DynamicStatus = {
  running: boolean;
  paused?: boolean;
  pid: number | null;
  returncode: number | null;
  log_path: string;
  log_tail: string;
  blocks: ActiveBlock[];
  updated_at: number | null;
  error?: string;
};

type PauseToggleResult = { paused: boolean; error?: string };

const startDynamicCapture = callable<[], DynamicStatus>("start_dynamic_capture");
const stopDynamicCapture = callable<[], DynamicStatus>("stop_dynamic_capture");
const refreshDynamicCapture = callable<[], DynamicStatus>("refresh_dynamic_capture");
const toggleDynamicPause = callable<[], PauseToggleResult>("toggle_dynamic_pause");
const getDynamicStatus = callable<[], DynamicStatus>("dynamic_status");

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

/** Dynamic-engine output counts as "live" only within this window - past it,
 * fall back to the single-region pipeline's status().translation so a stale
 * capture_dynamic.py run (or one that was never started) doesn't leave the
 * HUD showing frozen multi-block data. */
const ACTIVE_BLOCKS_FRESHNESS_S = 15;

// How long L4 must be held before release counts as a "long press" in
// Dynamic mode (toggle pause) rather than a short tap (refresh). Comfortably
// above the 150ms poll interval and the legacy hold-to-show threshold below,
// so a normal tap can't accidentally register as a long press.
const DYNAMIC_LONG_PRESS_MS = 900;

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
  // only updates while the QAM panel is open). While true, L4 completely
  // changes role: short tap = refresh, long hold = pause/resume, instead of
  // the legacy hold-to-show/press-to-hide SubtitleHud gestures below. Once
  // Dynamic mode is the only mode, the legacy branch goes away entirely.
  let dynamicRunning = false;
  // Guards the Dynamic-mode long-press from firing more than once per hold
  // (it fires mid-hold now, not on release - see the dynamicRunning branch
  // below) and tells release-handling not to also treat the same press as
  // a short-tap refresh.
  let dynamicLongPressFired = false;

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
  };

  const handleHudVisible = () => {
    hudVisible = true;
  };
  const handleHudHidden = () => {
    hudVisible = false;
  };
  const handleDynamicRunningChanged = (event: Event) => {
    const detail = (event as CustomEvent<{ running?: boolean }>).detail;
    dynamicRunning = !!detail?.running;
    holdStart = null;
    dynamicLongPressFired = false;
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
        dynamicLongPressFired = false;
        showTriggeredForPress = false;
        l4WasPressed = false;
        l5WasPressed = false;
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
        // Long-press fires the instant the hold crosses
        // DYNAMIC_LONG_PRESS_MS - *while still held*, not on release - so
        // the toast appears as immediate feedback the user can watch for,
        // telling them it's safe to let go rather than having to guess how
        // long is long enough. A short tap (released before the threshold)
        // fires refresh on release instead.
        if (l4Pressed && !l4WasPressed) {
          holdStart = Date.now();
          dynamicLongPressFired = false;
        } else if (l4Pressed && l4WasPressed && holdStart !== null && !dynamicLongPressFired) {
          if (Date.now() - holdStart >= DYNAMIC_LONG_PRESS_MS) {
            dynamicLongPressFired = true;
            hotkeyBusy = true;
            try {
              const toggled = await toggleDynamicPause();
              if (toggled.error) {
                console.warn(`toggle dynamic pause: ${toggled.error}`);
              } else {
                showToast(toggled.paused ? "PlayTranslate: paused (release now)" : "PlayTranslate: resumed (release now)");
              }
            } finally {
              hotkeyBusy = false;
            }
          }
        } else if (!l4Pressed && l4WasPressed) {
          if (holdStart !== null && !dynamicLongPressFired) {
            hotkeyBusy = true;
            try {
              const refreshed = await refreshDynamicCapture();
              if (refreshed.error) {
                console.warn(`refresh dynamic capture: ${refreshed.error}`);
              } else {
                showToast("PlayTranslate: refreshed");
              }
            } finally {
              hotkeyBusy = false;
            }
          }
          holdStart = null;
          dynamicLongPressFired = false;
        }
        l4WasPressed = l4Pressed;
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
      dynamicLongPressFired = false;
      showTriggeredForPress = false;
      l5WasPressed = false;
    } finally {
      pollingInput = false;
    }
  }, 150);

  return () => {
    window.clearInterval(timer);
    window.removeEventListener("playtranslate-hud-visible", handleHudVisible);
    window.removeEventListener("playtranslate-hud-hidden", handleHudHidden);
    window.removeEventListener("playtranslate-dynamic-running-changed", handleDynamicRunningChanged);
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
    const poll = async () => {
      try {
        const state = await getDynamicStatus();
        if (cancelled) return;
        if (state.running !== lastRunning) {
          lastRunning = state.running;
          window.dispatchEvent(
            new CustomEvent("playtranslate-dynamic-running-changed", { detail: { running: state.running } })
          );
        }
      } catch {
        if (!cancelled && lastRunning !== false) {
          lastRunning = false;
          window.dispatchEvent(new CustomEvent("playtranslate-dynamic-running-changed", { detail: { running: false } }));
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
  const [busy, setBusy] = useState(false);
  const [inputTest, setInputTest] = useState<string>("not tested");
  const [aiStatus, setAiStatus] = useState<string>("not checked");
  const [hudVisible, setHudVisible] = useState(false);
  const hudTimerRef = useRef<number | null>(null);

  const refresh = async () => {
    const next = await getStatus();
    setStatus(next);
    const nextDynamic = await getDynamicStatus();
    setDynamicStatus(nextDynamic);
  };

  const runAction = async (action: () => Promise<CaptureStatus>, title: string) => {
    setBusy(true);
    try {
      const next = await action();
      setStatus(next);
      if (next.error) {
        console.warn(`${title}: ${next.error}`);
      }
    } finally {
      setBusy(false);
    }
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

  const runInputTest = async () => {
    setBusy(true);
    setInputTest("testing...");
    try {
      const result = await testHidrawButtonState();
      if (!result.success) {
        setInputTest(`error: ${result.error ?? "unknown"}`);
      } else if (result.buttons.length === 0) {
        setInputTest(`no buttons (${result.device ?? "device unknown"})`);
      } else {
        setInputTest(`${result.buttons.join(", ")} (${result.device ?? "device unknown"})`);
      }
    } catch (error) {
      setInputTest(`error: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setBusy(false);
    }
  };

  const runAiServerTest = async () => {
    setBusy(true);
    setAiStatus("checking...");
    try {
      const result = await checkAiServer();
      if (result.ok) {
        setAiStatus(`ok: ${result.url}`);
      } else {
        setAiStatus(`error: ${result.error ?? result.url}`);
      }
    } catch (error) {
      setAiStatus(`error: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setBusy(false);
    }
  };

  const showHud = (durationMs = 9000) => {
    const text = status?.translation?.trim() ?? "";
    window.dispatchEvent(new CustomEvent("playtranslate-show-hud", { detail: { text } }));
    setHudVisible(true);
    if (hudTimerRef.current !== null) {
      window.clearTimeout(hudTimerRef.current);
    }
    hudTimerRef.current = window.setTimeout(() => {
      setHudVisible(false);
      hudTimerRef.current = null;
    }, durationMs);
  };

  const hideHud = () => {
    window.dispatchEvent(new CustomEvent("playtranslate-hide-hud"));
    setHudVisible(false);
    if (hudTimerRef.current !== null) {
      window.clearTimeout(hudTimerRef.current);
      hudTimerRef.current = null;
    }
  };

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    return () => {
      if (hudTimerRef.current !== null) {
        window.clearTimeout(hudTimerRef.current);
      }
    };
  }, []);

  const stateText = status?.running
    ? `Running${status.pid ? ` (${status.pid})` : ""}`
    : "Stopped";

  const dynamicStateText = dynamicStatus?.running
    ? `Running${dynamicStatus.pid ? ` (${dynamicStatus.pid})` : ""}${dynamicStatus.paused ? " / paused" : ""}`
    : "Stopped";

  return (
    <>
    <PanelSection title="PlayTranslate">
      <PanelSectionRow>
        <div>{stateText}</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          disabled={busy || status?.running === true}
          layout="below"
          onClick={() => runAction(startCapture, "PlayTranslate started")}
        >
          Start Capture
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          disabled={busy || status?.running !== true}
          layout="below"
          onClick={() => runAction(stopCapture, "PlayTranslate stopped")}
        >
          Stop Capture
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem disabled={busy} layout="below" onClick={refresh}>
          Refresh Status
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          disabled={busy}
          layout="below"
          onClick={() => runAction(translateLatest, "PlayTranslate translated")}
        >
          Translate Latest
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem disabled={busy} layout="below" onClick={runInputTest}>
          Test L4 Input
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem disabled={busy} layout="below" onClick={runAiServerTest}>
          Test AI Server
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem disabled={!status?.translation} layout="below" onClick={() => showHud()}>
          Show HUD
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem disabled={!hudVisible} layout="below" onClick={hideHud}>
          Hide HUD
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem disabled={busy} layout="below" onClick={() => openRegionCalibration()}>
          Calibrate Regions
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={{ fontSize: "12px", opacity: 0.8, overflowWrap: "anywhere" }}>
          <div>Engine: {status?.engine_dir ?? "not found"}</div>
          <div>Log: {status?.log_path ?? "-"}</div>
          <div>Translation: {status?.translation_path ?? "-"}</div>
          <div>AI URL: {status?.translate_url ?? "-"}</div>
          <div>AI: {aiStatus}</div>
          <div>
            Worker: {status?.translation_worker ? (status.translation_in_progress ? "translating" : "running") : "stopped"}
            {status?.translation_stale ? " / stale" : ""}
          </div>
          <div>Input: {inputTest}</div>
          <div>HUD: {hudVisible ? "visible" : "hidden"}</div>
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <pre
          style={{
            maxHeight: "160px",
            overflow: "auto",
            whiteSpace: "pre-wrap",
            fontSize: "14px",
            lineHeight: "18px",
          }}
        >
          {status?.translation || status?.translation_error || ""}
        </pre>
      </PanelSectionRow>
      <PanelSectionRow>
        <pre
          style={{
            maxHeight: "220px",
            overflow: "auto",
            whiteSpace: "pre-wrap",
            fontSize: "11px",
            lineHeight: "14px",
          }}
        >
          {status?.error ?? status?.log_tail ?? ""}
        </pre>
      </PanelSectionRow>
    </PanelSection>
    <PanelSection title="PlayTranslate — Dynamic (Beta)">
      <PanelSectionRow>
        <div>{dynamicStateText}</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={{ fontSize: "11px", opacity: 0.8 }}>
          Wide-area multi-block discovery. Mutually exclusive with regular
          capture above — starting one stops the other (see
          PHASE_A_HANDOFF.md).
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          disabled={busy || dynamicStatus?.running === true}
          layout="below"
          onClick={() => {
            runDynamicAction(startDynamicCapture, "PlayTranslate dynamic started");
            tryCloseQuickAccessMenu();
          }}
        >
          Start Dynamic Capture
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          disabled={busy || dynamicStatus?.running !== true}
          layout="below"
          onClick={() => runDynamicAction(stopDynamicCapture, "PlayTranslate dynamic stopped")}
        >
          Stop Dynamic Capture
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={{ fontSize: "11px", opacity: 0.8 }}>
          If the display looks wrong (garbled/overlapping boxes, stuck on
          old text) and doesn't fix itself in a few seconds, use this -
          clears what's shown and restarts detection from scratch. Safe to
          press repeatedly; presses queue instead of interfering with each
          other.
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          disabled={busy || dynamicStatus?.running !== true}
          layout="below"
          onClick={() => runDynamicAction(refreshDynamicCapture, "PlayTranslate dynamic refreshed")}
        >
          Refresh Dynamic Capture
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={{ fontSize: "11px", opacity: 0.8 }}>
          Stops/resumes translation work without restarting the process -
          use when a noisy scene is producing too much garbled output and
          you just want it to stop for a while. Also bound to L4 while
          Dynamic Capture is running: short tap = Refresh above, long hold
          (~1s) = this.
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          disabled={busy || dynamicStatus?.running !== true}
          layout="below"
          onClick={async () => {
            setBusy(true);
            try {
              const result = await toggleDynamicPause();
              if (result.error) {
                console.warn(`toggle dynamic pause: ${result.error}`);
              }
              const next = await getDynamicStatus();
              setDynamicStatus(next);
            } finally {
              setBusy(false);
            }
          }}
        >
          {dynamicStatus?.paused ? "Resume Dynamic Capture" : "Pause Dynamic Capture"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <pre
          style={{
            maxHeight: "160px",
            overflow: "auto",
            whiteSpace: "pre-wrap",
            fontSize: "12px",
            lineHeight: "16px",
          }}
        >
          {(dynamicStatus?.blocks ?? [])
            .map((b) => `#${b.id} ${b.translation || b.text}`)
            .join("\n") || "(no blocks)"}
        </pre>
      </PanelSectionRow>
      <PanelSectionRow>
        <pre
          style={{
            maxHeight: "160px",
            overflow: "auto",
            whiteSpace: "pre-wrap",
            fontSize: "11px",
            lineHeight: "14px",
          }}
        >
          {dynamicStatus?.error ?? dynamicStatus?.log_tail ?? ""}
        </pre>
      </PanelSectionRow>
    </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  const stopHotkeyPolling = startHotkeyPolling();
  routerHook.addGlobalComponent("PlayTranslateHud", () => <GlobalHud />);
  routerHook.addGlobalComponent("PlayTranslatePositionedOverlay", () => <PositionedOverlay />);
  routerHook.addGlobalComponent("PlayTranslateStatusToast", () => <StatusToast />);

  return {
    name: "PlayTranslate",
    titleView: <div className={staticClasses.Title}>PlayTranslate</div>,
    content: <Content />,
    icon: <FaLanguage />,
    onDismount() {
      stopHotkeyPolling();
      routerHook.removeGlobalComponent("PlayTranslateHud");
      routerHook.removeGlobalComponent("PlayTranslatePositionedOverlay");
      routerHook.removeGlobalComponent("PlayTranslateStatusToast");
    },
    alwaysRender: true,
  };
});
