import { findModuleChild } from "@decky/ui";

export enum UIComposition {
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
