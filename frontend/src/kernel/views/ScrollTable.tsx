import type { ReactNode } from "react";

/** A table that may be wider than its card: it scrolls sideways inside a region keyboard users can focus
 *  (WCAG 2.1.1; axe "scrollable-region-focusable"). */
export function ScrollTable({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="table-scroll" tabIndex={0} role="region" aria-label={label}>
      {children}
    </div>
  );
}
