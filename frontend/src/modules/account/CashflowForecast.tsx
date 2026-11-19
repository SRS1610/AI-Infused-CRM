// Cash-flow forecast (account.cashflow_forecast) under Accounting → Reporting: the weekly cash position from
// the forecast engine (chart with its band, and the same figures as a table), and an optional narrative
// written by a model. Every number comes from the engine; a narrative quoting another number is replaced by
// a summary of the facts on the server.
import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import { aiErrorMessage } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, useAiFeature } from "../../kernel/ai/components";
import { registerClientAction } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";
import { PageHeader } from "../../kernel/shell/PageHeader";
import { ScrollTable } from "../../kernel/views/ScrollTable";
import { accountAi, money, type Forecast, type Narrative } from "./api";

export const CASHFLOW = "account.cashflow_forecast";
const W = 640;
const H = 240;
const PAD = { left: 72, right: 16, top: 16, bottom: 36 };

export function ForecastChart({ forecast }: { forecast: Forecast }) {
  const titleId = useId();
  const descId = useId();
  const weeks = forecast.weeks;
  const values = [forecast.opening, ...weeks.flatMap((w) => [w.low, w.high, w.position])];
  const min = Math.min(0, ...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const x = (i: number) => PAD.left + (i * (W - PAD.left - PAD.right)) / Math.max(1, weeks.length);
  const y = (v: number) => PAD.top + ((max - v) * (H - PAD.top - PAD.bottom)) / span;
  const points = [[0, forecast.opening] as const, ...weeks.map((w, i) => [i + 1, w.position] as const)];
  const line = points.map(([i, v]) => `${x(i)},${y(v)}`).join(" ");
  const band = [
    `${x(0)},${y(forecast.opening)}`,
    ...weeks.map((w, i) => `${x(i + 1)},${y(w.high)}`),
    ...[...weeks].reverse().map((w, i) => `${x(weeks.length - i)},${y(w.low)}`),
  ].join(" ");
  const ticks = [max, (max + min) / 2, min];
  return (
    <figure className="cashflow-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={`${titleId} ${descId}`} width="100%">
        <title id={titleId}>{t("Forecast cash position by week")}</title>
        <desc id={descId}>
          {t("From {opening} today to {closing} at the end; lowest {low} on {date}.", {
            opening: money(forecast.opening),
            closing: money(forecast.closing),
            low: money(forecast.lowest.balance),
            date: forecast.lowest.date,
          })}
        </desc>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(v)} y2={y(v)} className="grid" />
            <text x={PAD.left - 6} y={y(v) + 4} textAnchor="end" className="tick">
              {Math.round(v).toLocaleString()}
            </text>
          </g>
        ))}
        {min < 0 && <line x1={PAD.left} x2={W - PAD.right} y1={y(0)} y2={y(0)} className="zero" />}
        <polygon points={band} className="band" />
        <polyline points={line} className="position" fill="none" />
        {points.map(([i, v]) => (
          <circle key={i} cx={x(i)} cy={y(v)} r={3} className="point" />
        ))}
        {weeks.map((w, i) => (
          <text key={w.week} x={x(i + 1)} y={H - 12} textAnchor="middle" className="tick">
            {t("W{n}", { n: w.week })}
          </text>
        ))}
      </svg>
      <figcaption className="muted small">
        {t("Line: expected position; shaded: range from the variability of past movements.")}
      </figcaption>
    </figure>
  );
}

export function CashflowForecastPage() {
  const feature = useAiFeature(CASHFLOW);
  const [weeks, setWeeks] = useState(8);
  const [narrative, setNarrative] = useState<Narrative | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const selectId = useId();
  const forecast = useQuery({
    queryKey: ["account-cashflow", weeks],
    queryFn: () => accountAi.forecast(weeks),
    enabled: feature.enabled,
    retry: false,
  });
  const narrate = async () => {
    setBusy(true);
    setError(null);
    try {
      setNarrative(await accountAi.narrative(weeks));
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  if (!feature.loading && !feature.enabled)
    return (
      <div className="page">
        <PageHeader title={t("Cash-flow forecast")} />
        <p className="muted">{t("The cash-flow forecast is turned off for this company.")}</p>
      </div>
    );
  const f = forecast.data;
  return (
    <div className="page cashflow-forecast">
      <PageHeader
        title={t("Cash-flow forecast")}
        subtitle={t("Expected bank and cash position from open invoices, bills and recurring movements")}
      >
        <label htmlFor={selectId}>{t("Horizon")}</label>{" "}
        <select
          id={selectId}
          className="input small"
          value={weeks}
          onChange={(e) => {
            setWeeks(Number(e.target.value));
            setNarrative(null);
          }}
        >
          {[4, 8, 13, 26].map((n) => (
            <option key={n} value={n}>
              {t("{n} weeks", { n })}
            </option>
          ))}
        </select>
      </PageHeader>
      {forecast.isLoading && (
        <p role="status" className="loading">
          {t("Loading…")}
        </p>
      )}
      {forecast.isError && (
        <div className="alert error" role="alert">
          {aiErrorMessage(forecast.error)}
        </div>
      )}
      {f && (
        <>
          <div className="kpis">
            <div className="kpi">
              <span>{t("Cash today")}</span>
              <strong>{money(f.opening, f.currency)}</strong>
            </div>
            <div className="kpi">
              <span>{t("Open receivables")}</span>
              <strong>{money(f.open_receivables)}</strong>
            </div>
            <div className="kpi">
              <span>{t("Open payables")}</span>
              <strong>{money(f.open_payables)}</strong>
            </div>
            {f.weeks.length > 0 && (
              <div className="kpi">
                <span>{t("Lowest point")}</span>
                <strong>{money(Math.min(...f.weeks.map((w) => Number(w.position))))}</strong>
              </div>
            )}
            {f.weeks.length > 0 && (
              <div className="kpi">
                <span>{t("End of horizon")}</span>
                <strong>{money(f.weeks[f.weeks.length - 1].position)}</strong>
              </div>
            )}
          </div>
          <div className="panel-card">
            <h2 className="panel-card-title">{t("Expected cash position")}</h2>
            <ForecastChart forecast={f} />
          </div>
          <section className="panel-card ai-card" aria-label={t("Narrative")}>
            {narrative ? (
              <p className="ai-text">
                <AiLabel text={narrative.narrative_source === "model" ? t("AI-generated") : t("From the figures")} />{" "}
                {narrative.narrative} <Feedback runId={narrative.run_id} />
              </p>
            ) : (
              <p>{f.summary}</p>
            )}
            {feature.available ? (
              <button type="button" className="btn small" disabled={busy} onClick={() => void narrate()}>
                {t("Explain the forecast")}
              </button>
            ) : (
              <AiUnavailable />
            )}
            {error && (
              <div className="alert error" role="alert">
                {error}
              </div>
            )}
          </section>
          <div className="card-grid">
            <div className="panel-card flush">
              <ScrollTable label={t("Weekly cash position")}>
                <table className="data-table">
                  <caption className="table-caption">{t("Weekly cash position")}</caption>
                  <thead>
                    <tr>
                      <th scope="col">{t("Week")}</th>
                      <th scope="col">{t("Until")}</th>
                      <th scope="col">{t("In")}</th>
                      <th scope="col">{t("Out")}</th>
                      <th scope="col">{t("Position")}</th>
                      <th scope="col">{t("Range")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {f.weeks.map((w) => (
                      <tr key={w.week}>
                        <th scope="row">{w.week}</th>
                        <td>{w.end}</td>
                        <td className="num">{money(w.inflow)}</td>
                        <td className="num">{money(w.outflow)}</td>
                        <td className="num">{money(w.position)}</td>
                        <td className="num">
                          {money(w.low)} – {money(w.high)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </ScrollTable>
            </div>
            {f.largest_items.length > 0 && (
              <div className="panel-card flush">
                <h2>{t("Largest expected items")}</h2>
                <ScrollTable label={t("Largest expected items")}>
                  <table className="data-table">
                    <tbody>
                      {f.largest_items.map((b) => (
                        <tr key={`${b.label}-${b.date}`}>
                          <td>{b.date}</td>
                          <td>{b.label}</td>
                          <td className="num">{money(b.amount)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </ScrollTable>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

registerClientAction("account.cashflow_forecast", () => <CashflowForecastPage />);
