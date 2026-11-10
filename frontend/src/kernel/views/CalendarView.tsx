import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../api/client";
import type { Domain, ViewDef, ViewsResponse } from "../api/types";
import { and } from "../domain";

interface Props {
  model: string;
  meta: ViewsResponse;
  view: ViewDef;
  domain: Domain;
  onOpen: (id: number) => void;
}

const WEEKDAYS = [1, 2, 3, 4, 5, 6, 0];

function iso(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Month grid of records placed on their ``date_start`` field (date or datetime). */
export function CalendarView({ model, meta, view, domain, onOpen }: Props) {
  const [month, setMonth] = useState(() => {
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth(), 1);
  });
  const dateField = view.date_start ?? "";
  const titleField = (view.title as string | undefined) ?? "display_name";
  const isDatetime = meta.fields[dateField]?.type === "datetime";
  const gridStart = useMemo(() => {
    const d = new Date(month);
    const offset = (d.getDay() + 6) % 7; // weeks start on Monday
    d.setDate(d.getDate() - offset);
    return d;
  }, [month]);
  const days = useMemo(
    () =>
      Array.from({ length: 42 }, (_, i) => {
        const d = new Date(gridStart);
        d.setDate(gridStart.getDate() + i);
        return d;
      }),
    [gridStart],
  );
  const from = iso(days[0]);
  const to = iso(days[41]);
  const range = [
    [dateField, ">=", from],
    [dateField, "<=", isDatetime ? `${to}T23:59:59` : to],
  ];
  const records = useQuery({
    queryKey: ["list", model, "calendar", dateField, from, JSON.stringify(domain)],
    queryFn: () =>
      api.list(model, {
        domain: and(domain, { all: range }),
        fields: [dateField, titleField === "display_name" ? "display_name" : titleField, "display_name"],
        limit: 500,
        order: dateField,
      }),
    enabled: !!dateField,
  });
  const byDay = useMemo(() => {
    const out = new Map<string, { id: number; title: string; time: string | null }[]>();
    for (const r of records.data?.items ?? []) {
      const raw = r[dateField] as string | null;
      if (!raw) continue;
      const local = isDatetime ? new Date(raw) : null;
      const key = local ? iso(local) : raw.slice(0, 10);
      const time = local ? local.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : null;
      const list = out.get(key) ?? [];
      list.push({ id: r.id as number, title: String(r[titleField] ?? r.display_name ?? ""), time });
      out.set(key, list);
    }
    return out;
  }, [records.data, dateField, titleField, isDatetime]);

  if (!dateField) return <div className="alert info">This calendar view has no date field.</div>;
  const label = month.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  const today = iso(new Date());
  const shift = (n: number) => setMonth((m) => new Date(m.getFullYear(), m.getMonth() + n, 1));
  return (
    <div className="calendar-view">
      <div className="calendar-toolbar">
        <button type="button" className="btn small" onClick={() => shift(-1)} aria-label="Previous month">
          ‹
        </button>
        <button
          type="button"
          className="btn small"
          onClick={() => setMonth(new Date(new Date().getFullYear(), new Date().getMonth(), 1))}
        >
          Today
        </button>
        <button type="button" className="btn small" onClick={() => shift(1)} aria-label="Next month">
          ›
        </button>
        <h2 className="calendar-title" aria-live="polite">
          {label}
        </h2>
      </div>
      <div className="calendar-grid" role="grid" aria-label={label}>
        <div role="row" className="calendar-row head">
          {WEEKDAYS.map((w) => (
            <div key={w} role="columnheader" className="calendar-cell head">
              {new Date(2024, 0, w === 0 ? 7 : w).toLocaleDateString(undefined, { weekday: "short" })}
            </div>
          ))}
        </div>
        {Array.from({ length: 6 }, (_, week) => (
          <div role="row" className="calendar-row" key={week}>
            {days.slice(week * 7, week * 7 + 7).map((d) => {
              const key = iso(d);
              const items = byDay.get(key) ?? [];
              return (
                <div
                  key={key}
                  role="gridcell"
                  aria-label={`${d.toLocaleDateString()}: ${items.length} record(s)`}
                  className={`calendar-cell${d.getMonth() !== month.getMonth() ? " outside" : ""}${key === today ? " today" : ""}`}
                >
                  <span className="day-number">{d.getDate()}</span>
                  {items.slice(0, 4).map((it) => (
                    <button key={it.id} type="button" className="calendar-event" onClick={() => onOpen(it.id)}>
                      {it.time && <span className="muted">{it.time} </span>}
                      {it.title}
                    </button>
                  ))}
                  {items.length > 4 && <span className="muted small">+{items.length - 4} more</span>}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
