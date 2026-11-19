// A list whose items are reordered by drag and drop (drop onto an item to move after it) or with the
// arrow buttons; each item can carry its own controls.
import { useState, type ReactNode } from "react";
import { t } from "../../kernel/i18n";
import { moveAfter, step } from "./api";

export function OrderedList<T>({
  items,
  keyOf,
  labelOf,
  detailOf,
  onReorder,
  controls,
  className,
  label,
}: {
  items: T[];
  keyOf: (item: T) => string;
  labelOf: (item: T) => string;
  detailOf?: (item: T) => ReactNode;
  onReorder: (items: T[]) => void;
  controls?: (item: T, index: number) => ReactNode;
  className?: (item: T) => string | undefined;
  label?: string;
}) {
  const [dragging, setDragging] = useState<number | null>(null);
  return (
    <ol className="studio-layout" aria-label={label}>
      {items.map((item, i) => (
        <li
          key={keyOf(item)}
          draggable
          onDragStart={() => setDragging(i)}
          onDragOver={(e) => e.preventDefault()}
          onDrop={() => {
            if (dragging !== null) onReorder(moveAfter(items, dragging, i));
            setDragging(null);
          }}
          className={className?.(item)}
        >
          <span className="studio-handle" aria-hidden="true">
            ⠿
          </span>
          <span className="studio-name">
            {labelOf(item)}
            {detailOf && <span className="muted small"> {detailOf(item)}</span>}
          </span>
          <button
            type="button"
            className="btn ghost small"
            aria-label={t("Move {field} up", { field: labelOf(item) })}
            disabled={i === 0}
            onClick={() => onReorder(step(items, i, -1))}
          >
            ↑
          </button>
          <button
            type="button"
            className="btn ghost small"
            aria-label={t("Move {field} down", { field: labelOf(item) })}
            disabled={i === items.length - 1}
            onClick={() => onReorder(step(items, i, 1))}
          >
            ↓
          </button>
          {controls?.(item, i)}
        </li>
      ))}
    </ol>
  );
}
