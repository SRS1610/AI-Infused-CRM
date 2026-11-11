// Rental AI (sub-phase 2.6): on a rental product, the demand forecast for the next weeks beside what is already
// booked (availability), with its open usage alerts; on a rental order, the alerts about its rentals and customer.
// Alerts are notifications: "Dismiss" only marks them as dealt with.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { aiErrorMessage } from "../../kernel/ai/api";
import { AiLabel, Feedback, useAiFeature } from "../../kernel/ai/components";
import { request } from "../../kernel/api/client";
import { registerFormPanel } from "../../kernel/extensions";
import { formatNumber } from "../../kernel/format";
import { t } from "../../kernel/i18n";

export const FORECAST = "sale_rental.demand_forecast";
export const ANOMALY = "sale_rental.usage_anomaly";

export interface ForecastWeek {
  week: number;
  week_start: string;
  week_end: string;
  unit_days: number;
  average: number;
  low: number;
  high: number;
  utilisation?: number;
  booked_unit_days: number;
}

export interface ForecastView {
  product_id: number;
  product: string;
  units: number | null;
  method: string;
  history_days: number;
  trend_per_week: number;
  recent_weeks: number[];
  weeks: ForecastWeek[];
  run_id: number;
}

export interface RentalAlert {
  id: number;
  kind: "late_returns" | "long_booking" | "utilisation_spike" | "utilisation_drop";
  kind_label: string;
  summary: string;
  detected_on: string;
  state: "open" | "dismissed";
}

const rentalApi = {
  forecast: (id: number, weeks: number) =>
    request<ForecastView>("GET", `/sale_rental/ai/products/${id}/forecast`, { query: { weeks } }),
  alerts: (q: { product_id?: number; order_id?: number }) =>
    request<{ alerts: RentalAlert[] }>("GET", "/sale_rental/ai/alerts", { query: q }),
  dismiss: (id: number) => request<RentalAlert>("POST", `/sale_rental/ai/alerts/${id}/dismiss`),
};

const Hidden = () => <span className="ai-panel-hidden" hidden />;
const pct = (v: number | undefined) => (v === undefined ? "—" : `${Math.round(v * 100)} %`);

/** Demand forecast per week, with the unit-days already booked and the 80 % range. */
export function DemandForecast({ productId }: { productId: number }) {
  const [weeks, setWeeks] = useState(4);
  const data = useQuery({
    queryKey: ["rental-forecast", productId, weeks],
    queryFn: () => rentalApi.forecast(productId, weeks),
    retry: false,
  });
  if (data.isLoading) return <p className="muted small">{t("Forecasting demand…")}</p>;
  if (data.isError) return <div className="alert error">{aiErrorMessage(data.error)}</div>;
  const fc = data.data!;
  return (
    <section className="ai-rental-forecast" aria-label={t("Demand forecast")}>
      <h3 className="small">
        {t("Demand forecast")} <AiLabel text={t("AI forecast")} />
      </h3>
      <label className="small">
        {t("Weeks ahead")}{" "}
        <select value={weeks} onChange={(e) => setWeeks(Number(e.target.value))}>
          {[2, 4, 8, 12].map((w) => (
            <option key={w} value={w}>
              {w}
            </option>
          ))}
        </select>
      </label>
      <table className="data-table">
        <caption className="sr-only">{t("Units out per week")}</caption>
        <thead>
          <tr>
            <th scope="col">{t("Week of")}</th>
            <th scope="col" className="num">
              {t("Expected units out / day")}
            </th>
            <th scope="col" className="num">
              {t("Unit-days (80 % range)")}
            </th>
            <th scope="col" className="num">
              {t("Already booked")}
            </th>
            {fc.units ? (
              <th scope="col" className="num">
                {t("Utilisation")}
              </th>
            ) : null}
          </tr>
        </thead>
        <tbody>
          {fc.weeks.map((w) => (
            <tr key={w.week}>
              <th scope="row">{w.week_start}</th>
              <td className="num">{formatNumber(w.average, 1)}</td>
              <td className="num">
                {formatNumber(w.unit_days, 0)} ({formatNumber(w.low, 0)}–{formatNumber(w.high, 0)})
              </td>
              <td className="num">{formatNumber(w.booked_unit_days, 0)}</td>
              {fc.units ? <td className="num">{pct(w.utilisation)}</td> : null}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">
        {t("{units} units · {days} days of history · {method}", {
          units: fc.units ?? "?",
          days: fc.history_days,
          method: fc.method,
        })}
      </p>
      <Feedback runId={fc.run_id} />
    </section>
  );
}

/** Open usage alerts, each dismissible. */
export function RentalAlerts({ query }: { query: { product_id?: number; order_id?: number } }) {
  const qc = useQueryClient();
  const key = ["rental-alerts", query.product_id ?? 0, query.order_id ?? 0];
  const data = useQuery({ queryKey: key, queryFn: () => rentalApi.alerts(query), retry: false });
  const [error, setError] = useState<string | null>(null);
  if (data.isLoading) return null;
  if (data.isError) return <div className="alert error">{aiErrorMessage(data.error)}</div>;
  const alerts = data.data?.alerts ?? [];
  if (!alerts.length) return <p className="muted small">{t("No open usage alerts.")}</p>;
  const dismiss = async (id: number) => {
    setError(null);
    try {
      await rentalApi.dismiss(id);
      void qc.invalidateQueries({ queryKey: ["rental-alerts"] });
    } catch (e) {
      setError(aiErrorMessage(e));
    }
  };
  return (
    <section className="ai-rental-alerts" aria-label={t("Usage alerts")}>
      <h3 className="small">
        {t("Usage alerts")} <AiLabel text={t("AI alert")} />
      </h3>
      <ul>
        {alerts.map((a) => (
          <li key={a.id} className={`ai-alert kind-${a.kind}`} role="status">
            <span className="badge">{a.kind_label}</span> {a.summary}{" "}
            <span className="muted small">{a.detected_on}</span>{" "}
            <button
              type="button"
              className="btn ghost small"
              aria-label={t("Dismiss: {alert}", { alert: a.summary })}
              onClick={() => void dismiss(a.id)}
            >
              {t("Dismiss")}
            </button>
          </li>
        ))}
      </ul>
      {error && <div className="alert error">{error}</div>}
    </section>
  );
}

export function RentalProductPanel({ resId, record }: { resId: number; record: Record<string, unknown> }) {
  const forecast = useAiFeature(FORECAST);
  const anomaly = useAiFeature(ANOMALY);
  if (!record.rent_ok || (!forecast.enabled && !anomaly.enabled)) return <Hidden />;
  return (
    <div className="ai-rental">
      {forecast.enabled && <DemandForecast productId={resId} />}
      {anomaly.enabled && <RentalAlerts query={{ product_id: resId }} />}
    </div>
  );
}

export function RentalOrderPanel({ resId, record }: { resId: number; record: Record<string, unknown> }) {
  const anomaly = useAiFeature(ANOMALY);
  if (!record.is_rental_order || !anomaly.enabled) return <Hidden />;
  return <RentalAlerts query={{ order_id: resId }} />;
}

registerFormPanel("product.product", {
  get title() {
    return t("Rental demand (AI)");
  },
  render: ({ resId, record }) => <RentalProductPanel resId={resId} record={record} />,
});

registerFormPanel("sale.quotation", {
  get title() {
    return t("Rental alerts (AI)");
  },
  render: ({ resId, record }) => <RentalOrderPanel resId={resId} record={record} />,
});
