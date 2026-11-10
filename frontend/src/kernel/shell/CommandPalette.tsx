import { useEffect, useMemo, useRef, useState } from "react";
import type { MenuNode } from "../api/types";

export interface PaletteItem {
  id: string;
  label: string;
  hint?: string;
  run: () => void;
}

export function flattenMenus(menus: MenuNode[], open: (action: string) => void, trail: string[] = []): PaletteItem[] {
  const out: PaletteItem[] = [];
  for (const m of menus) {
    const path = [...trail, m.name];
    if (m.action)
      out.push({ id: `menu:${m.id}`, label: m.name, hint: path.slice(0, -1).join(" / "), run: () => open(m.action!) });
    out.push(...flattenMenus(m.children, open, path));
  }
  return out;
}

export function fuzzyScore(query: string, text: string): number {
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  if (!q) return 1;
  if (t.includes(q)) return 100 - t.indexOf(q);
  let ti = 0;
  for (const ch of q) {
    ti = t.indexOf(ch, ti);
    if (ti < 0) return 0;
    ti++;
  }
  return 10;
}

export function CommandPalette({ items, onClose }: { items: PaletteItem[]; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => input.current?.focus(), []);
  const results = useMemo(
    () =>
      items
        .map((item) => ({
          item,
          score: Math.max(fuzzyScore(query, item.label), fuzzyScore(query, `${item.hint ?? ""} ${item.label}`) - 5),
        }))
        .filter((r) => r.score > 0)
        .sort((a, b) => b.score - a.score)
        .slice(0, 12)
        .map((r) => r.item),
    [items, query],
  );
  return (
    <div className="palette-backdrop" onMouseDown={onClose}>
      <div
        className="palette"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <input
          ref={input}
          className="input palette-input"
          placeholder="Go to…"
          aria-label="Go to menu or record"
          role="combobox"
          aria-expanded="true"
          aria-controls="palette-results"
          aria-activedescendant={results[active] ? `palette-${results[active].id}` : undefined}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") onClose();
            else if (e.key === "ArrowDown") {
              e.preventDefault();
              setActive((a) => Math.min(a + 1, results.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((a) => Math.max(a - 1, 0));
            } else if (e.key === "Enter" && results[active]) {
              results[active].run();
              onClose();
            }
          }}
        />
        <ul id="palette-results" role="listbox" className="palette-results">
          {results.map((r, i) => (
            <li
              key={r.id}
              id={`palette-${r.id}`}
              role="option"
              aria-selected={i === active}
              className={i === active ? "active" : undefined}
              onMouseDown={() => {
                r.run();
                onClose();
              }}
            >
              <span>{r.label}</span>
              {r.hint && <span className="muted"> — {r.hint}</span>}
            </li>
          ))}
          {results.length === 0 && <li className="muted">No match</li>}
        </ul>
      </div>
    </div>
  );
}
