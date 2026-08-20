import { findModuleChild, Router } from "@decky/ui";

export enum UIComposition {
  Hidden = 0,
  Notification = 1,
  Overlay = 2,
}

export const useUIComposition: (composition: UIComposition) => void = findModuleChild((m) => {
  if (typeof m !== "object") return undefined;
  for (const prop in m) {
    const fn = (m as Record<string, unknown>)[prop];
    if (
      typeof fn === "function" &&
      fn.toString().includes("AddMinimumCompositionStateRequest") &&
      fn.toString().includes("ChangeMinimumCompositionStateRequest") &&
      fn.toString().includes("RemoveMinimumCompositionStateRequest") &&
      !fn.toString().includes("m_mapCompositionStateRequests")
    ) {
      return fn;
    }
  }
  return undefined;
});

export function CompositionRequest({ level }: { level: UIComposition }) {
  useUIComposition(level);
  return null;
}

// Explicit nudge alongside useUIComposition's own Add/Remove-on-(un)mount
// bookkeeping, not a replacement for it - confirmed live 2026-08-20 that on
// one machine (a desktop Linux box, gamescope running embedded rather than
// as the Deck's own full session) exiting an Overlay-level composition
// request (TapTranslateOverlay, and reportedly Steam's own QAM) left game
// input dead until the user manually clicked into the game window with a
// mouse - the automatic Remove call alone wasn't restoring focus there,
// though it does on a real Deck. SteamClient.Overlay.SetOverlayState is the
// same official, typed API (see @decky/ui's globals/steam-client/Overlay.d.ts)
// the Remove path presumably drives internally; calling it explicitly with
// Hidden should be a no-op wherever the automatic path already works
// correctly, and only matters as a fallback where it doesn't.
export function forceOverlayHidden() {
  const appId = Router.MainRunningApp?.appid;
  if (appId == null) return;
  try {
    (window as any).SteamClient?.Overlay?.SetOverlayState(String(appId), UIComposition.Hidden);
  } catch {
    // best-effort
  }
}
