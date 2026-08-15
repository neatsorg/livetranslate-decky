import { Button, Dropdown, Field, ModalRoot, SliderField, TextField, ToggleField, showModal } from "@decky/ui";
import { callable } from "@decky/api";
import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";

type RegionRole = "speaker" | "text" | "context";

interface RegionDraft {
  id: string;
  name: string;
  role: RegionRole;
  x_pct: number;
  y_pct: number;
  width_pct: number;
  height_pct: number;
  psm: number;
  oem: number;
  resize: number;
  white_text_threshold: number | null;
}

interface RawRegion {
  name?: string;
  role?: string;
  x_pct?: number;
  y_pct?: number;
  width_pct?: number;
  height_pct?: number;
  psm?: number;
  oem?: number;
  resize?: number;
  white_text_threshold?: number;
}

interface ImageResult {
  ok: boolean;
  base64?: string;
  mtime_ns?: number;
  path?: string;
  error?: string;
}

interface GameListResult {
  games: string[];
  active: string;
}

interface RegionConfigResult {
  ok: boolean;
  regions?: RawRegion[];
  error?: string;
}

interface SaveResult {
  ok: boolean;
  path?: string;
  error?: string;
}

interface ActiveGameResult {
  ok?: boolean;
  active?: string;
  error?: string;
}

interface TestRegionResult {
  ok: boolean;
  text?: string;
  raw_text?: string;
  useful?: boolean;
  error?: string;
}

const getLastSettledImage = callable<[], ImageResult>("get_last_settled_image");
const getLatestSteamScreenshot = callable<[], ImageResult>("get_latest_steam_screenshot");
const listRegionConfigs = callable<[], GameListResult>("list_region_configs");
const getRegionConfig = callable<[string], RegionConfigResult>("get_region_config");
const saveRegionConfig = callable<[string, RawRegion[], string | null], SaveResult>("save_region_config");
const getReferenceImage = callable<[string], ImageResult>("get_reference_image");
const setActiveGameCall = callable<[string], ActiveGameResult>("set_active_game");
const testOcrRegionCall = callable<[RawRegion, string], TestRegionResult>("test_ocr_region");

let nextRegionSeq = 1;
function newRegionId(): string {
  return `region_${nextRegionSeq++}_${Date.now()}`;
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

// A touch/gamepad-draggable slider instead of a text field: typing a number
// on the Deck opens the full on-screen keyboard (there's no numeric-only
// layout available here), and editing a field that already has digits in it
// means backspacing through them first. Dragging (or D-pad + A) sets the
// value without any of that. editableValue still allows typing when precision
// beats dragging.
function NumberField({
  label,
  value,
  onChange,
  min = 0,
  max = 100,
  step = 1,
  suffix,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  suffix?: string;
}) {
  return (
    <div style={{ minWidth: "220px" }}>
      <SliderField
        label={label}
        value={value}
        min={min}
        max={max}
        step={step}
        showValue
        editableValue
        valueSuffix={suffix}
        onChange={onChange}
      />
    </div>
  );
}

// ModalRoot wraps its children in a <form> (confirmed via live DOM
// inspection), and Button/DialogButton renders a plain <button> with no
// type attribute - which defaults to type="submit" inside a form. Every
// click was therefore submitting that form (and, it seems, closing the
// modal as the form's default handling) regardless of what our own onClick
// did. preventDefault() on the click stops that submit before it happens.
function preventFormSubmit(handler: () => void) {
  return (event: { preventDefault(): void }) => {
    event.preventDefault();
    handler();
  };
}

function toRawRegion(region: RegionDraft): RawRegion {
  const raw: RawRegion = {
    name: region.name,
    role: region.role,
    x_pct: round2(region.x_pct),
    y_pct: round2(region.y_pct),
    width_pct: round2(region.width_pct),
    height_pct: round2(region.height_pct),
    psm: region.psm,
    oem: region.oem,
    resize: region.resize,
  };
  if (region.white_text_threshold !== null) {
    raw.white_text_threshold = region.white_text_threshold;
  }
  return raw;
}

function fromRawRegion(raw: RawRegion, index: number): RegionDraft {
  return {
    id: newRegionId(),
    name: raw.name || `region_${index + 1}`,
    role: (raw.role as RegionRole) || "text",
    x_pct: raw.x_pct ?? 10,
    y_pct: raw.y_pct ?? 10,
    width_pct: raw.width_pct ?? 30,
    height_pct: raw.height_pct ?? 15,
    psm: raw.psm ?? 6,
    oem: raw.oem ?? 1,
    resize: raw.resize ?? 100,
    white_text_threshold: raw.white_text_threshold ?? null,
  };
}

type DragMode = "create" | "move" | "resize";
type ResizeHandle = "nw" | "ne" | "sw" | "se";

interface DragState {
  mode: DragMode;
  regionId: string;
  handle?: ResizeHandle;
  startClientX: number;
  startClientY: number;
  startRegion: RegionDraft;
}

const MIN_SIZE_PCT = 2;
const RESIZE_HANDLES: ResizeHandle[] = ["nw", "ne", "sw", "se"];
// Below this many screen pixels of movement, a touch is treated as a tap
// (selects/deselects, creates nothing) rather than the start of a drag - see
// onOverlayPointerDown/pendingCreateRef. Without this, every tap on empty
// space left behind a phantom MIN_SIZE_PCT-sized region.
const CREATE_DRAG_THRESHOLD_PX = 10;

function pointToPct(clientX: number, clientY: number, rect: DOMRect) {
  return {
    x: clamp(((clientX - rect.left) / rect.width) * 100, 0, 100),
    y: clamp(((clientY - rect.top) / rect.height) * 100, 0, 100),
  };
}

function RegionCalibrationContent({ onClose }: { onClose: () => void }) {
  const [imageData, setImageData] = useState<string>("");
  const [imageBase64, setImageBase64] = useState<string>("");
  const [imagePath, setImagePath] = useState<string>("");
  const [loadError, setLoadError] = useState<string>("");
  const [regions, setRegions] = useState<RegionDraft[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [games, setGames] = useState<string[]>([]);
  const [gameId, setGameId] = useState<string>("");
  const [status, setStatus] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState<string>("");

  const overlayRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const pendingCreateRef = useRef<{ startClientX: number; startClientY: number } | null>(null);

  const applyImageResult = (result: ImageResult, notFoundMessage: string) => {
    if (!result.ok || !result.base64) {
      setLoadError(result.error || notFoundMessage);
      return false;
    }
    setLoadError("");
    setImageBase64(result.base64);
    setImageData(`data:image/png;base64,${result.base64}`);
    setImagePath(result.path || "");
    return true;
  };

  const loadImage = useCallback(async () => {
    try {
      const result = await getLastSettledImage();
      applyImageResult(result, "no screenshot available yet");
    } catch (error) {
      setLoadError(errorMessage(error));
    }
  }, []);

  const steamScreenshotPollRef = useRef<number | null>(null);

  const stopSteamScreenshotPoll = () => {
    if (steamScreenshotPollRef.current !== null) {
      window.clearInterval(steamScreenshotPollRef.current);
      steamScreenshotPollRef.current = null;
    }
  };

  // Both SteamClient.Screenshots.GetLastScreenshotTaken() (query on demand)
  // and GameSessions.RegisterForScreenshotNotification() (event-driven) were
  // tried first, but neither ever resolved/fired in live testing, despite
  // screenshots visibly accumulating in Steam's own userdata folder as soon
  // as R1+STEAM was pressed - that SteamClient surface doesn't seem to work
  // reliably from a Decky plugin's sandboxed context. Polling the folder
  // directly on the backend (get_latest_steam_screenshot in main.py, the
  // same mtime-watch pattern already used for translation results) sidesteps
  // it entirely: arm here, poll until a file newer than "now" shows up.
  const loadSteamScreenshot = () => {
    stopSteamScreenshotPoll();
    const armedAtMs = Date.now();
    const deadlineMs = armedAtMs + 60000;
    setStatus("press R1+STEAM now to take a screenshot...");

    steamScreenshotPollRef.current = window.setInterval(async () => {
      if (Date.now() > deadlineMs) {
        stopSteamScreenshotPoll();
        setStatus("no new Steam screenshot showed up within 60s - try again");
        return;
      }
      try {
        const result = await getLatestSteamScreenshot();
        if (result.ok && result.mtime_ns && result.mtime_ns / 1e6 > armedAtMs) {
          stopSteamScreenshotPoll();
          if (applyImageResult(result, "")) {
            setStatus("loaded Steam screenshot");
          }
        }
      } catch {
        // transient failure - keep polling until the deadline
      }
    }, 1000);
  };

  useEffect(() => stopSteamScreenshotPoll, []);

  const loadGames = useCallback(async () => {
    try {
      const result = await listRegionConfigs();
      setGames(result.games || []);
      setGameId((current) => current || result.active || "");
    } catch (error) {
      setStatus(`error: ${errorMessage(error)}`);
    }
  }, []);

  useEffect(() => {
    // last_settled.png (what loadImage/"Refresh Screenshot" fetches) is
    // whatever capture.py's diff-settle heuristic happened to save most
    // recently - it might be stale, QAM-contaminated, or (for a game with no
    // saved regions yet) not even a screenshot of this game at all. The
    // saved reference for the active game (what its regions were actually
    // drawn against) is a far more meaningful thing to show on open; if
    // there isn't one yet, say so explicitly rather than silently loading
    // last_settled.png and letting the user assume it's meaningful.
    (async () => {
      try {
        const result = await listRegionConfigs();
        setGames(result.games || []);
        const active = result.active || "";
        setGameId((current) => current || active);
        if (active) {
          const reference = await getReferenceImage(active).catch(() => null);
          if (reference && reference.ok) {
            applyImageResult(reference, "");
            setStatus(`showing saved reference for ${active}`);
            return;
          }
        }
        setLoadError('No screenshot loaded yet - press "Load Steam Screenshot" (recommended) or "Refresh Screenshot" below.');
      } catch (error) {
        setStatus(`error: ${errorMessage(error)}`);
      }
    })();
  }, []);

  const loadRegionsForGame = async (id: string) => {
    if (!id) return;
    setBusy(true);
    setStatus("loading...");
    try {
      const result = await getRegionConfig(id);
      if (!result.ok) {
        setStatus(`error: ${result.error ?? "unknown"}`);
        return;
      }
      setRegions((result.regions || []).map((raw, index) => fromRawRegion(raw, index)));
      setSelectedId(null);
      // Best-effort: show the screenshot this game's regions were drawn
      // against, so switching games doesn't leave stale rectangles overlaid
      // on a screenshot from a different game. A missing reference (never
      // saved with one, or an RPC failure) just silently leaves whatever
      // image is already showing rather than surfacing a false-alarm error.
      const reference = await getReferenceImage(id).catch(() => null);
      if (reference && reference.ok) {
        applyImageResult(reference, "");
      }
      setStatus(`loaded ${id}`);
    } catch (error) {
      setStatus(`error: ${errorMessage(error)}`);
    } finally {
      setBusy(false);
    }
  };

  const handleSave = async () => {
    const id = gameId.trim();
    if (!id) {
      setStatus("error: game id is required");
      return;
    }
    setBusy(true);
    setStatus("saving...");
    try {
      const result = await saveRegionConfig(id, regions.map(toRawRegion), imageBase64 || null);
      setStatus(result.ok ? `saved to ${result.path}` : `error: ${result.error ?? "unknown"}`);
      if (result.ok) {
        loadGames();
      }
    } catch (error) {
      setStatus(`error: ${errorMessage(error)}`);
    } finally {
      setBusy(false);
    }
  };

  const handleSetActive = async () => {
    const id = gameId.trim();
    if (!id) {
      setStatus("error: game id is required");
      return;
    }
    setBusy(true);
    setStatus("switching...");
    try {
      const result = await setActiveGameCall(id);
      setStatus(result.ok ? `active game: ${result.active}` : `error: ${result.error ?? "unknown"}`);
      if (result.ok) {
        loadGames();
      }
    } catch (error) {
      setStatus(`error: ${errorMessage(error)}`);
    } finally {
      setBusy(false);
    }
  };

  const handleTestRegion = async (region: RegionDraft) => {
    if (!imagePath) {
      setTestResult("error: no screenshot loaded yet");
      return;
    }
    setBusy(true);
    setTestResult("testing...");
    try {
      const result = await testOcrRegionCall(toRawRegion(region), imagePath);
      setTestResult(result.ok ? result.text || "(no text)" : `error: ${result.error ?? "unknown"}`);
    } catch (error) {
      setTestResult(`error: ${errorMessage(error)}`);
    } finally {
      setBusy(false);
    }
  };

  const selected = regions.find((r) => r.id === selectedId) || null;

  const updateSelected = (patch: Partial<RegionDraft>) => {
    if (!selectedId) return;
    setRegions((prev) => prev.map((r) => (r.id === selectedId ? { ...r, ...patch } : r)));
  };

  const deleteSelected = () => {
    if (!selectedId) return;
    setRegions((prev) => prev.filter((r) => r.id !== selectedId));
    setSelectedId(null);
  };

  // Drag-to-draw hasn't proven reliable via touch in Gaming Mode (see
  // session notes); this gives a complete draw-free path to the same result
  // - add a default-sized region, then place it with the NumberField
  // sliders below, which touch has consistently handled fine.
  const addRegion = () => {
    const createdBox: { region: RegionDraft | null } = { region: null };
    setRegions((prev) => {
      const draft: RegionDraft = {
        id: newRegionId(),
        name: `region_${prev.length + 1}`,
        role: "text",
        x_pct: 10,
        y_pct: 10,
        width_pct: 30,
        height_pct: 15,
        psm: 6,
        oem: 1,
        resize: 200,
        white_text_threshold: null,
      };
      createdBox.region = draft;
      return [...prev, draft];
    });
    if (createdBox.region) {
      setSelectedId(createdBox.region.id);
    }
  };

  // Doesn't create a region yet - see CREATE_DRAG_THRESHOLD_PX. Pointer
  // capture keeps this gesture reliably targeted at this element (and stops
  // the browser from reinterpreting it as a scroll) even once the finger
  // moves outside its bounds.
  const onOverlayPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    pendingCreateRef.current = { startClientX: event.clientX, startClientY: event.clientY };
  };

  const onRegionPointerDown = (event: ReactPointerEvent<HTMLDivElement>, region: RegionDraft) => {
    event.stopPropagation();
    if (!overlayRef.current) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    setSelectedId(region.id);
    dragRef.current = {
      mode: "move",
      regionId: region.id,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startRegion: region,
    };
  };

  const onHandlePointerDown = (
    event: ReactPointerEvent<HTMLDivElement>,
    region: RegionDraft,
    handle: ResizeHandle
  ) => {
    event.stopPropagation();
    if (!overlayRef.current) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    setSelectedId(region.id);
    dragRef.current = {
      mode: "resize",
      regionId: region.id,
      handle,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startRegion: region,
    };
  };

  useEffect(() => {
    const onMove = (event: PointerEvent) => {
      const pending = pendingCreateRef.current;
      if (pending && !dragRef.current) {
        if (!overlayRef.current) return;
        const movedPx = Math.hypot(event.clientX - pending.startClientX, event.clientY - pending.startClientY);
        if (movedPx < CREATE_DRAG_THRESHOLD_PX) return;

        pendingCreateRef.current = null;
        const rect = overlayRef.current.getBoundingClientRect();
        const { x, y } = pointToPct(pending.startClientX, pending.startClientY, rect);
        // Named inside the setRegions updater (not from the `regions`
        // closure) so it uses the true current count even though this
        // handler was created once inside this effect's empty-deps closure.
        // Boxed in an object rather than reassigning a `let created` because
        // TS won't carry narrowing for a plain variable mutated inside a
        // nested closure.
        const createdBox: { region: RegionDraft | null } = { region: null };
        setRegions((prev) => {
          const draft: RegionDraft = {
            id: newRegionId(),
            name: `region_${prev.length + 1}`,
            role: "text",
            x_pct: x,
            y_pct: y,
            width_pct: MIN_SIZE_PCT,
            height_pct: MIN_SIZE_PCT,
            psm: 6,
            oem: 1,
            resize: 200,
            white_text_threshold: null,
          };
          createdBox.region = draft;
          return [...prev, draft];
        });
        if (createdBox.region) {
          setSelectedId(createdBox.region.id);
          dragRef.current = {
            mode: "create",
            regionId: createdBox.region.id,
            startClientX: pending.startClientX,
            startClientY: pending.startClientY,
            startRegion: createdBox.region,
          };
        }
      }

      const drag = dragRef.current;
      if (!drag || !overlayRef.current) return;
      // Re-measure on every move rather than reusing the rect captured at
      // pointerdown: selecting/creating a region changes the side panel,
      // which can reflow the image to a new position mid-gesture. Deriving
      // both the start and current pointer position through the *same*
      // fresh rect keeps the delta correct even if the layout just shifted.
      const rect = overlayRef.current.getBoundingClientRect();
      const start = drag.startRegion;
      const currentPt = pointToPct(event.clientX, event.clientY, rect);
      const startPt = pointToPct(drag.startClientX, drag.startClientY, rect);
      const dxPct = currentPt.x - startPt.x;
      const dyPct = currentPt.y - startPt.y;

      setRegions((prev) =>
        prev.map((r) => {
          if (r.id !== drag.regionId) return r;

          if (drag.mode === "move") {
            return {
              ...r,
              x_pct: clamp(start.x_pct + dxPct, 0, 100 - r.width_pct),
              y_pct: clamp(start.y_pct + dyPct, 0, 100 - r.height_pct),
            };
          }

          if (drag.mode === "create") {
            const { x, y } = currentPt;
            const left = Math.min(start.x_pct, x);
            const top = Math.min(start.y_pct, y);
            return {
              ...r,
              x_pct: left,
              y_pct: top,
              width_pct: Math.max(MIN_SIZE_PCT, Math.abs(x - start.x_pct)),
              height_pct: Math.max(MIN_SIZE_PCT, Math.abs(y - start.y_pct)),
            };
          }

          // resize
          let { x_pct, y_pct, width_pct, height_pct } = start;
          const right = start.x_pct + start.width_pct;
          const bottom = start.y_pct + start.height_pct;
          if (drag.handle === "se") {
            width_pct = Math.max(MIN_SIZE_PCT, start.width_pct + dxPct);
            height_pct = Math.max(MIN_SIZE_PCT, start.height_pct + dyPct);
          } else if (drag.handle === "nw") {
            x_pct = clamp(start.x_pct + dxPct, 0, right - MIN_SIZE_PCT);
            y_pct = clamp(start.y_pct + dyPct, 0, bottom - MIN_SIZE_PCT);
            width_pct = right - x_pct;
            height_pct = bottom - y_pct;
          } else if (drag.handle === "ne") {
            y_pct = clamp(start.y_pct + dyPct, 0, bottom - MIN_SIZE_PCT);
            width_pct = Math.max(MIN_SIZE_PCT, start.width_pct + dxPct);
            height_pct = bottom - y_pct;
          } else if (drag.handle === "sw") {
            x_pct = clamp(start.x_pct + dxPct, 0, right - MIN_SIZE_PCT);
            width_pct = right - x_pct;
            height_pct = Math.max(MIN_SIZE_PCT, start.height_pct + dyPct);
          }
          width_pct = Math.min(width_pct, 100 - x_pct);
          height_pct = Math.min(height_pct, 100 - y_pct);
          return { ...r, x_pct, y_pct, width_pct, height_pct };
        })
      );
    };

    const onUp = () => {
      dragRef.current = null;
      pendingCreateRef.current = null;
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    // A touch-drag the browser decides to treat as a scroll/gesture fires
    // pointercancel instead of pointerup - without this, dragRef never
    // clears and the next touch keeps acting on stale drag state.
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, []);

  return (
    <div
      // Every button started closing the modal even with try/catch on every
      // RPC call, which rules out an uncaught exception as the cause. The
      // remaining likely explanation: ModalRoot listens for clicks that
      // bubble past its content (its "click outside to dismiss" handler) and
      // isn't distinguishing "outside" correctly for content rendered this
      // way. Stopping propagation here keeps every click inside this content
      // from ever reaching that listener.
      onClick={(event) => event.stopPropagation()}
      style={{
        display: "flex",
        flexDirection: "column",
        boxSizing: "border-box",
        color: "#f2efe9",
        fontSize: "14px",
        minHeight: "70vh",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "10px" }}>
        <div style={{ fontSize: "18px", fontWeight: 700 }}>OCR Region Calibration</div>
        <Button onClick={preventFormSubmit(onClose)}>Close</Button>
      </div>

      <div style={{ display: "flex", gap: "10px", alignItems: "center", marginBottom: "10px", flexWrap: "wrap" }}>
          <div style={{ minWidth: "200px" }}>
            <Dropdown
              rgOptions={games.map((g) => ({ data: g, label: g }))}
              selectedOption={gameId}
              strDefaultLabel={gameId || "select a game"}
              onChange={(option) => setGameId(String(option.data))}
            />
          </div>
          <div style={{ minWidth: "200px" }}>
            <TextField
              label="Game ID"
              value={gameId}
              onChange={(event) => setGameId(event.target.value)}
            />
          </div>
          <Button onClick={preventFormSubmit(() => loadRegionsForGame(gameId.trim()))}>Load</Button>
          <Button onClick={preventFormSubmit(() => handleSave())}>Save</Button>
          <Button onClick={preventFormSubmit(() => handleSetActive())}>Set Active</Button>
          <Button onClick={preventFormSubmit(() => loadImage())}>Refresh Screenshot</Button>
          <Button onClick={preventFormSubmit(() => loadSteamScreenshot())}>Load Steam Screenshot</Button>
          <div style={{ fontSize: "12px", opacity: 0.85 }}>{busy ? "working..." : status}</div>
        </div>
        <div style={{ fontSize: "11px", opacity: 0.7, marginTop: "-6px", marginBottom: "10px" }}>
          "Refresh Screenshot" may show the QAM sidebar if it was open when the screen last settled. For a clean,
          full-resolution shot (needed for a brand new game's crop), press "Load Steam Screenshot" and then press
          R1+STEAM in-game.
        </div>

        <div style={{ display: "flex", flexDirection: "column", flex: 1, gap: "8px", minHeight: 0, overflowY: "auto" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "10px", flexShrink: 0 }}>
            <Button onClick={preventFormSubmit(addRegion)}>Add Region</Button>
            <span style={{ fontSize: "12px", opacity: 0.85 }}>
              Drag on the screenshot to draw a new region, or use "Add Region" and place it with the sliders below.
              Tap a region to edit it.
            </span>
          </div>
          <div
            style={{
              position: "relative",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              flexShrink: 0,
            }}
          >
            {loadError && <div style={{ color: "#ff8a80" }}>{loadError}</div>}
            {imageData && (
              <div style={{ position: "relative", maxWidth: "100%", maxHeight: "100%" }}>
                <img
                  src={imageData}
                  style={{ display: "block", maxWidth: "100%", maxHeight: "42vh", objectFit: "contain" }}
                  alt="Last captured screenshot"
                />
                <div
                  ref={overlayRef}
                  onPointerDown={onOverlayPointerDown}
                  style={{ position: "absolute", inset: 0, cursor: "crosshair", touchAction: "none" }}
                >
                  {regions.map((region) => (
                    <div
                      key={region.id}
                      onPointerDown={(event) => onRegionPointerDown(event, region)}
                      style={{
                        position: "absolute",
                        left: `${region.x_pct}%`,
                        top: `${region.y_pct}%`,
                        width: `${region.width_pct}%`,
                        height: `${region.height_pct}%`,
                        border: region.id === selectedId ? "2px solid #4fc3f7" : "1px solid #ffca28",
                        background: region.id === selectedId ? "rgba(79,195,247,0.15)" : "rgba(255,202,40,0.08)",
                        boxSizing: "border-box",
                        cursor: "move",
                        touchAction: "none",
                      }}
                    >
                      <div
                        style={{
                          position: "absolute",
                          top: "-18px",
                          left: 0,
                          fontSize: "11px",
                          background: "rgba(0,0,0,0.7)",
                          padding: "1px 4px",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {region.name} ({region.role})
                      </div>
                      {RESIZE_HANDLES.map((handle) => (
                        <div
                          key={handle}
                          onPointerDown={(event) => onHandlePointerDown(event, region, handle)}
                          style={{
                            position: "absolute",
                            width: "12px",
                            height: "12px",
                            background: "#4fc3f7",
                            border: "1px solid #0a0c0e",
                            top: handle.includes("n") ? "-6px" : undefined,
                            bottom: handle.includes("s") ? "-6px" : undefined,
                            left: handle.includes("w") ? "-6px" : undefined,
                            right: handle.includes("e") ? "-6px" : undefined,
                            cursor: `${handle}-resize`,
                            touchAction: "none",
                          }}
                        />
                      ))}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          <div style={{ flexShrink: 0 }}>
            {!selected && <div style={{ opacity: 0.7 }}>No region selected.</div>}
            {selected && (
              <>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "12px", alignItems: "flex-end" }}>
                  <NumberField
                    label="X position"
                    value={round2(selected.x_pct)}
                    min={0}
                    max={100}
                    suffix="%"
                    onChange={(value) => updateSelected({ x_pct: clamp(value, 0, 100) })}
                  />
                  <NumberField
                    label="Y position"
                    value={round2(selected.y_pct)}
                    min={0}
                    max={100}
                    suffix="%"
                    onChange={(value) => updateSelected({ y_pct: clamp(value, 0, 100) })}
                  />
                  <NumberField
                    label="Width"
                    value={round2(selected.width_pct)}
                    min={MIN_SIZE_PCT}
                    max={100}
                    suffix="%"
                    onChange={(value) => updateSelected({ width_pct: clamp(value, MIN_SIZE_PCT, 100) })}
                  />
                  <NumberField
                    label="Height"
                    value={round2(selected.height_pct)}
                    min={MIN_SIZE_PCT}
                    max={100}
                    suffix="%"
                    onChange={(value) => updateSelected({ height_pct: clamp(value, MIN_SIZE_PCT, 100) })}
                  />
                  <NumberField
                    label="Resize"
                    value={selected.resize}
                    min={50}
                    max={400}
                    step={10}
                    suffix="%"
                    onChange={(value) => updateSelected({ resize: value || 100 })}
                  />
                  <NumberField
                    label="PSM"
                    value={selected.psm}
                    min={0}
                    max={13}
                    onChange={(value) => updateSelected({ psm: value })}
                  />
                  <NumberField
                    label="OEM"
                    value={selected.oem}
                    min={0}
                    max={3}
                    onChange={(value) => updateSelected({ oem: value })}
                  />
                  <div style={{ minWidth: "160px" }}>
                    <Field label="White-text binarization">
                      <ToggleField
                        checked={selected.white_text_threshold !== null}
                        onChange={(checked) => updateSelected({ white_text_threshold: checked ? 150 : null })}
                      />
                    </Field>
                  </div>
                  {selected.white_text_threshold !== null && (
                    <NumberField
                      label="Threshold"
                      value={selected.white_text_threshold}
                      min={0}
                      max={255}
                      onChange={(value) => updateSelected({ white_text_threshold: clamp(value, 0, 255) })}
                    />
                  )}
                </div>

                <div style={{ display: "flex", flexWrap: "wrap", gap: "12px", alignItems: "flex-end", marginTop: "12px" }}>
                  <div style={{ minWidth: "180px" }}>
                    <Field label="Name" description='Your own label, e.g. "speaker_left" or "main_text" - not read by OCR or translation.'>
                      <TextField
                        value={selected.name}
                        onChange={(event) => updateSelected({ name: event.target.value })}
                      />
                    </Field>
                  </div>
                  <div style={{ minWidth: "150px" }}>
                    <Field
                      label="Role"
                      description='speaker = name tag. text = dialogue body. context = fallback used only if no "text" region has output.'
                    >
                      <Dropdown
                        rgOptions={[
                          { data: "speaker", label: "speaker" },
                          { data: "text", label: "text" },
                          { data: "context", label: "context" },
                        ]}
                        selectedOption={selected.role}
                        onChange={(option) => updateSelected({ role: option.data as RegionRole })}
                      />
                    </Field>
                  </div>
                  <Button onClick={preventFormSubmit(() => handleTestRegion(selected))}>Test OCR</Button>
                  <Button onClick={preventFormSubmit(deleteSelected)}>Delete</Button>
                </div>
                <div style={{ fontSize: "11px", opacity: 0.7, marginTop: "4px" }}>
                  Gender, age, and tone for translation come from a separate per-game profile file, not from OCR or
                  this screen.
                </div>
                {testResult && (
                  <pre
                    style={{
                      marginTop: "8px",
                      whiteSpace: "pre-wrap",
                      fontSize: "12px",
                      background: "rgba(255,255,255,0.06)",
                      padding: "6px",
                      borderRadius: "4px",
                    }}
                  >
                    {testResult}
                  </pre>
                )}
              </>
            )}
          </div>
        </div>
      </div>
  );
}

// showModal is Steam's own dialog/popup system (the same one used for every
// native Steam dialog), unlike a plain fixed-position div mounted under the
// QAM panel's own DOM subtree: that gets visually clipped to the QAM's
// right-third-of-screen bounds, and - worse - doesn't participate in Steam's
// gamepad focus-navigation or dropdown/menu portaling, which is why the
// first version of this screen only took touch input and had a broken
// Dropdown. ModalRoot + bAllowFullSize gets a properly-integrated, large
// popup for free. `close` is reassigned right after showModal returns; the
// props below close over the *binding*, not its value at render time, so
// calling it later (after the button is clicked) correctly resolves to
// modal.Close.
export function openRegionCalibration() {
  let close = () => {};
  const modal = showModal(
    <ModalRoot bAllowFullSize onCancel={() => close()} closeModal={() => close()}>
      <RegionCalibrationContent onClose={() => close()} />
    </ModalRoot>,
    window
  );
  close = () => modal.Close();
}
