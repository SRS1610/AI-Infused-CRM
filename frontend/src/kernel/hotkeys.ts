// Keyboard shortcuts: "mod" = Ctrl on Windows/Linux, ⌘ on macOS.
import { useEffect } from "react";

export type Handler = (event: KeyboardEvent) => void;

export function matches(event: KeyboardEvent, combo: string): boolean {
  const parts = combo.toLowerCase().split("+");
  const key = parts.pop();
  const mod = parts.includes("mod");
  const wantCtrl = parts.includes("ctrl") || (mod && !isMac());
  const wantMeta = parts.includes("meta") || (mod && isMac());
  return (
    event.key.toLowerCase() === key &&
    event.ctrlKey === wantCtrl &&
    event.metaKey === wantMeta &&
    event.altKey === parts.includes("alt") &&
    event.shiftKey === parts.includes("shift")
  );
}

function isMac(): boolean {
  return typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
}

/** Register shortcuts while the component is mounted. Shortcuts with a modifier fire even in inputs. */
export function useHotkeys(bindings: Record<string, Handler>, enabled = true): void {
  useEffect(() => {
    if (!enabled) return undefined;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing = !!target && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
      for (const [combo, handler] of Object.entries(bindings)) {
        if (!matches(event, combo)) continue;
        if (typing && !/mod|ctrl|meta|alt/.test(combo) && combo !== "escape") continue;
        event.preventDefault();
        handler(event);
        return;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [bindings, enabled]);
}
