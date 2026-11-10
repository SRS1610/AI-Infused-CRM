import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api, request } from "../api/client";
import type { Domain, ViewDef, ViewsResponse } from "../api/types";
import { and } from "../domain";
import { t, tn } from "../i18n";

interface Props {
  model: string;
  meta: ViewsResponse;
  view: ViewDef;
  domain: Domain;
  onOpen: (id: number) => void;
}

type Mode = "month" | "week" | "day";
interface Item {
  id: number;
  title: string;
  start: Date | null;
  dayKey: string;
  stop: Date | null;
  recurring?: boolean;
}

const WEEKDAYS = [1, 2, 3, 4, 5, 6, 0];
const MODE_LABELS: Record<Mode, () => string> = { day: () => t("Day"), week: () => t("Week"), month: () => t("Month") };

function iso(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function startOfWeek(d: Date): Date {
  const out = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  out.setDate(out.getDate() - ((out.getDay() + 6) % 7)); // weeks start on Monday
  return out;
}

function addDays(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

const time = (d: Date | null) => (d ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : null);

/** Month, week and day views of records placed on their ``date_start`` field. Views may declare an
 * ``expand_endpoint`` (e.g. recurring meetings) returning occurrences for a window. */
export function CalendarView({ model, meta, view, domain, onOpen }: Props) {
  const [mode, setMode] = useState<Mode>("month");
  const [anchor, setAnchor] = useState(() => new Date());
  const dateField = view.date_start ?? "";
  const titleField = (view.title as string | undefined) ?? "display_name";
  const isDatetime = meta.fields[dateField]?.type === "datetime";
  const endpoint = typeof view.expand_endpoint === "string" ? view.expand_endpoint : null;

  const days = useMemo(() => {
    if (mode === "day") return [new Date(anchor.getFullYear(), anchor.getMonth(), anchor.getDate())];
    if (mode === "week") {
      const first = startOfWeek(anchor);
      return Array.from({ length: 7 }, (_, i) => addDays(first, i));
    }
    const first = startOfWeek(new Date(anchor.getFullYear(), anchor.getMonth(), 1));
    return Array.from({ length: 42 }, (_, i) => addDays(first, i));
  }, [mode, anchor]);
  const windowStart = days[0];
  const windowEnd = addDays(days[days.length - 1], 1);

  const records = useQuery({
    queryKey: ["list", model, "calendar", dateField, mode, iso(windowStart), endpoint, JSON.stringify(domain)],
    enabled: !!dateField,
    queryFn: async (): Promise<Item[]> => {
      if (endpoint) {
        const rows = await request<{ id: number; name: string; start: string; stop: string; recurring: boolean }[]>(
          "GET",
          endpoint,
          { query: { start: windowStart.toISOString(), end: windowEnd.toISOString() } },
        );
        return rows.map((r) => {
          const s = new Date(r.start);
          return { id: r.id, title: r.name, start: s, stop: new Date(r.stop), dayKey: iso(s), recurring: r.recurring };
        });
      }
      const to = iso(addDays(windowEnd, -1));
      const data = await api.list(model, {
        domain: and(domain, {
          all: [
            [dateField, ">=", isDatetime ? windowStart.toISOString() : iso(windowStart)],
            [dateField, "<=", isDatetime ? windowEnd.toISOString() : to],
          ],
        }),
        fields: [dateField, titleField === "display_name" ? "display_name" : titleField, "display_name"],
        limit: 500,
        order: dateField,
      });
      return data.items
        .filter((r) => r[dateField])
        .map((r) => {
          const raw = String(r[dateField]);
          const s = isDatetime ? new Date(raw) : null;
          return {
            id: r.id as number,
            title: String(r[titleField] ?? r.display_name ?? ""),
            start: s,
            stop: null,
            dayKey: s ? iso(s) : raw.slice(0, 10),
          };
        });
    },
  });
  const byDay = useMemo(() => {
    const out = new Map<string, Item[]>();
    for (const item of records.data ?? []) out.set(item.dayKey, [...(out.get(item.dayKey) ?? []), item]);
    return out;
  }, [records.data]);

  if (!dateField) return <div className="alert info">{t("This calendar view has no date field.")}</div>;
  const today = iso(new Date());
  const shift = (n: number) =>
    setAnchor((a) =>
      mode === "month" ? new Date(a.getFullYear(), a.getMonth() + n, 1) : addDays(a, n * (mode === "week" ? 7 : 1)),
    );
  const label =
    mode === "month"
      ? anchor.toLocaleDateString(undefined, { month: "long", year: "numeric" })
      : mode === "week"
        ? `${days[0].toLocaleDateString(undefined, { day: "numeric", month: "short" })} – ${days[6].toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}`
        : anchor.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" });

  const event = (it: Item) => (
    <button
      key={`${it.id}-${it.start?.toISOString() ?? it.dayKey}`}
      type="button"
      className="calendar-event"
      onClick={() => onOpen(it.id)}
    >
      {it.start && <span className="muted">{time(it.start)} </span>}
      {it.title}
      {it.recurring && <span aria-label={t("repeats")}> ↻</span>}
    </button>
  );

  return (
    <div className="calendar-view">
      <div className="calendar-toolbar">
        <button
          type="button"
          className="btn small"
          onClick={() => shift(-1)}
          aria-label={mode === "month" ? t("Previous month") : mode === "week" ? t("Previous week") : t("Previous day")}
        >
          ‹
        </button>
        <button type="button" className="btn small" onClick={() => setAnchor(new Date())}>
          {t("Today")}
        </button>
        <button
          type="button"
          className="btn small"
          onClick={() => shift(1)}
          aria-label={mode === "month" ? t("Next month") : mode === "week" ? t("Next week") : t("Next day")}
        >
          ›
        </button>
        <h2 className="calendar-title" aria-live="polite">
          {label}
        </h2>
        <div className="view-switcher" role="tablist" aria-label={t("Calendar range")}>
          {(["day", "week", "month"] as Mode[]).map((m) => (
            <button
              key={m}
              type="button"
              role="tab"
              aria-selected={mode === m}
              className={`btn small${mode === m ? " active" : " ghost"}`}
              onClick={() => setMode(m)}
            >
              {MODE_LABELS[m]()}
            </button>
          ))}
        </div>
      </div>
      {mode === "month" ? (
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
                    aria-label={tn("{date}: {count} record", "{date}: {count} records", items.length, {
                      date: d.toLocaleDateString(),
                    })}
                    className={`calendar-cell${d.getMonth() !== anchor.getMonth() ? " outside" : ""}${key === today ? " today" : ""}`}
                  >
                    <span className="day-number">{d.getDate()}</span>
                    {items.slice(0, 4).map(event)}
                    {items.length > 4 && (
                      <span className="muted small">{t("+{count} more", { count: items.length - 4 })}</span>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      ) : (
        <div className={`calendar-agenda ${mode}`} role="list" aria-label={label}>
          {days.map((d) => {
            const key = iso(d);
            const items = byDay.get(key) ?? [];
            return (
              <section
                key={key}
                role="listitem"
                className={`agenda-day${key === today ? " today" : ""}`}
                aria-label={d.toLocaleDateString()}
              >
                <h3>{d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })}</h3>
                {items.length ? items.map(event) : <span className="muted small">—</span>}
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
