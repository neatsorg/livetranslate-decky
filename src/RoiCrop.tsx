import { Button, ModalRoot, SliderField, showModal } from "@decky/ui";
import { callable } from "@decky/api";
import { useCallback, useEffect, useState } from "react";

interface ImageResult {
  ok: boolean;
  base64?: string;
  error?: string;
}

interface Roi {
  x_pct: number;
  y_pct: number;
  width_pct: number;
  height_pct: number;
}

interface RoiResult {
  ok: boolean;
  roi?: Roi | null;
  error?: string;
}

interface DynamicStatus {
  running: boolean;
  error?: string;
  [key: string]: unknown;
}

const getRoiCropScreenshot = callable<[], ImageResult>("get_roi_crop_screenshot");
const getDynamicRoi = callable<[], RoiResult>("get_dynamic_roi");
const saveDynamicRoi = callable<[Roi], RoiResult>("save_dynamic_roi");
const startDynamicCaptureFixedRoi = callable<[Roi], DynamicStatus>("start_dynamic_capture_fixed_roi");
// Deliberately not set_dynamic_qam_open - reusing that flag was tried
// first and confirmed live not to work: opening this modal closes the QAM
// side menu (a different UI layer to Steam), so index.tsx's 300ms QAM-open
// poll immediately overwrote "open" back to "false" while this modal (with
// its own on-screen text) was still fully visible. This flag is driven
// only by this component's own mount/unmount, so nothing else can race it.
const setDynamicRoiEditorOpen = callable<[boolean, string], { roi_editor_open: boolean }>(
  "set_dynamic_roi_editor_open"
);

const DEFAULT_ROI: Roi = { x_pct: 20, y_pct: 60, width_pct: 58, height_pct: 36 };

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function createToken(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `roi_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

// ModalRoot wraps children in a <form>, and Button renders a plain <button>
// with no type attribute (defaults to submit) - without this, every click
// submits the form and closes the modal before the handler's async work
// finishes.
function preventFormSubmit(handler: () => void) {
  return (event: { preventDefault(): void }) => {
    event.preventDefault();
    handler();
  };
}

// Deliberately just four sliders over a screenshot, not a draggable/
// resizable editor - this mode only ever tracks one fixed region, so
// there's nothing to name, add, or remove.
function RoiCropContent({ onClose }: { onClose: () => void }) {
  const [imageData, setImageData] = useState<string>("");
  const [loadError, setLoadError] = useState<string>("");
  const [roi, setRoi] = useState<Roi>(DEFAULT_ROI);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string>("");
  const [modalToken] = useState(createToken);

  const loadScreenshot = useCallback(async () => {
    setLoadError("");
    try {
      const result = await getRoiCropScreenshot();
      if (!result.ok || !result.base64) {
        setLoadError(result.error || "screenshot failed");
        return;
      }
      setImageData(`data:image/png;base64,${result.base64}`);
    } catch (error) {
      setLoadError(errorMessage(error));
    }
  }, []);

  useEffect(() => {
    loadScreenshot();
    getDynamicRoi()
      .then((result) => {
        if (result.ok && result.roi) {
          setRoi(result.roi);
        }
      })
      .catch(() => {});
  }, [loadScreenshot]);

  // This modal is just as much PlayTranslate's own on-screen UI as the QAM
  // sidebar, and an already-running engine (opened for reconfiguring, not
  // just freshly started) needs the same "don't read my own text"
  // protection while this is up - see setDynamicRoiEditorOpen's own
  // comment for why this has to be a dedicated flag, not qam_open reused.
  useEffect(() => {
    setDynamicRoiEditorOpen(true, modalToken).catch(() => {});
    return () => {
      setDynamicRoiEditorOpen(false, modalToken).catch(() => {});
    };
  }, [modalToken]);

  const updateRoi = (patch: Partial<Roi>) => {
    setRoi((prev) => {
      const next = { ...prev, ...patch };
      next.x_pct = clamp(next.x_pct, 0, 100);
      next.y_pct = clamp(next.y_pct, 0, 100);
      next.width_pct = clamp(next.width_pct, 1, 100 - next.x_pct);
      next.height_pct = clamp(next.height_pct, 1, 100 - next.y_pct);
      return next;
    });
  };

  const runSave = async (thenStart: boolean) => {
    setBusy(true);
    setStatus("");
    try {
      const rounded: Roi = {
        x_pct: round2(roi.x_pct),
        y_pct: round2(roi.y_pct),
        width_pct: round2(roi.width_pct),
        height_pct: round2(roi.height_pct),
      };
      const saved = await saveDynamicRoi(rounded);
      if (!saved.ok) {
        setStatus(saved.error || "save failed");
        return;
      }
      if (thenStart) {
        // dynamic_roi_editor_open_flag_path is untouched by
        // start_dynamic_capture() (unlike qam_open_flag, which it always
        // clears at spawn) - it was already armed by this component's
        // mount effect above and stays armed straight through this call,
        // so a process spawned while this modal is still open is
        // protected from its very first frame, no re-arming needed here.
        const started = await startDynamicCaptureFixedRoi(rounded);
        if (started.error) {
          setStatus(started.error);
          return;
        }
        onClose();
        return;
      }
      setStatus("saved");
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "12px", padding: "8px" }}>
      <div style={{ fontSize: "14px", fontWeight: 600 }}>Dynamic Capture — Region</div>
      <div style={{ fontSize: "11px", opacity: 0.8 }}>
        Pick one rectangle (e.g. the subtitle box) for Dynamic Capture to watch and translate,
        instead of scanning the whole screen.
      </div>
      <div style={{ position: "relative", display: "flex", justifyContent: "center" }}>
        {loadError && <div style={{ color: "#ff8a80" }}>{loadError}</div>}
        {imageData && (
          <div style={{ position: "relative", maxWidth: "100%" }}>
            <img
              src={imageData}
              style={{ display: "block", maxWidth: "100%", maxHeight: "42vh", objectFit: "contain" }}
              alt="Current game screen"
            />
            <div
              style={{
                position: "absolute",
                left: `${roi.x_pct}%`,
                top: `${roi.y_pct}%`,
                width: `${roi.width_pct}%`,
                height: `${roi.height_pct}%`,
                border: "2px solid #4fc3f7",
                background: "rgba(79, 195, 247, 0.15)",
                boxSizing: "border-box",
                pointerEvents: "none",
              }}
            />
          </div>
        )}
      </div>
      <Button disabled={busy} onClick={preventFormSubmit(() => loadScreenshot())}>
        Retake Screenshot
      </Button>
      <SliderField
        label="Left"
        value={roi.x_pct}
        min={0}
        max={99}
        step={1}
        showValue
        editableValue
        valueSuffix="%"
        onChange={(value) => updateRoi({ x_pct: value })}
      />
      <SliderField
        label="Top"
        value={roi.y_pct}
        min={0}
        max={99}
        step={1}
        showValue
        editableValue
        valueSuffix="%"
        onChange={(value) => updateRoi({ y_pct: value })}
      />
      <SliderField
        label="Width"
        value={roi.width_pct}
        min={1}
        max={100 - roi.x_pct}
        step={1}
        showValue
        editableValue
        valueSuffix="%"
        onChange={(value) => updateRoi({ width_pct: value })}
      />
      <SliderField
        label="Height"
        value={roi.height_pct}
        min={1}
        max={100 - roi.y_pct}
        step={1}
        showValue
        editableValue
        valueSuffix="%"
        onChange={(value) => updateRoi({ height_pct: value })}
      />
      {status && <div style={{ fontSize: "11px", opacity: 0.8 }}>{status}</div>}
      <div style={{ display: "flex", gap: "8px" }}>
        <Button disabled={busy} onClick={preventFormSubmit(() => runSave(false))}>
          Save
        </Button>
        <Button disabled={busy} onClick={preventFormSubmit(() => runSave(true))}>
          Save & Start
        </Button>
        <Button disabled={busy} onClick={preventFormSubmit(() => onClose())}>
          Close
        </Button>
      </div>
    </div>
  );
}

export function openRoiCropEditor() {
  let close = () => {};
  const modal = showModal(
    <ModalRoot bAllowFullSize onCancel={() => close()} closeModal={() => close()}>
      <RoiCropContent onClose={() => close()} />
    </ModalRoot>,
    window
  );
  close = () => modal.Close();
}
