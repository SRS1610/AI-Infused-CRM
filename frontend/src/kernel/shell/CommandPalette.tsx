import { useEffect, useMemo, useRef, useState } from "react";
import { search, type SearchHit } from "../api/platform";
import type { MenuNode } from "../api/types";
import { t } from "../i18n";

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
  const hay = text.toLowerCase();
  if (!q) return 1;
  if (hay.includes(q)) return 100 - hay.indexOf(q);
  let ti = 0;
  for (const ch of q) {
    ti = hay.indexOf(ch, ti);
    if (ti < 0) return 0;
    ti++;
  }
  return 10;
}

export function CommandPalette({
  items,
  onClose,
  searchRecords,
}: {
  items: PaletteItem[];
  onClose: () => void;
  searchRecords?: (hit: SearchHit) => void;
}) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [records, setRecords] = useState<PaletteItem[]>([]);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => input.current?.focus(), []);
  useEffect(() => {
    if (!searchRecords || query.trim().length < 2) {
      setRecords([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        const found = await search.global(query);
        if (!cancelled)
          setRecords(
            found.records.map((hit) => ({
              id: `rec:${hit.model}:${hit.id}`,
              label: hit.title,
              hint: hit.model_label,
              run: () => searchRecords(hit),
            })),
          );
      } catch {
        if (!cancelled) setRecords([]);
      }
    }, 200);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [query, searchRecords]);
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
        .map((r) => r.item)
        .concat(records)
        .slice(0, 20),
    [items, query, records],
  );
  return (
    <div className="palette-backdrop" onMouseDown={onClose}>
      <div
        className="palette"
        role="dialog"
        aria-modal="true"
        aria-label={t("Command palette")}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <input
          ref={input}
          className="input palette-input"
          placeholder={t("Go to…")}
          aria-label={t("Go to menu or record")}
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
          {results.length === 0 && <li className="muted">{t("No match")}</li>}
        </ul>
      </div>
    </div>
  );
}
