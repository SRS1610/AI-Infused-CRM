import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { request } from "../../kernel/api/client";
import { registerClientAction } from "../../kernel/extensions";
import { formatNumber } from "../../kernel/format";
import { t, tx } from "../../kernel/i18n";
import { AiForecastSection } from "./AiInsights";

interface Velocity {
  open_opportunities: number;
  average_deal: number;
  win_rate: number;
  average_cycle_days: number;
  velocity_per_day: number;
}

/** Sales analysis: forecast, funnel, win rate and sales velocity — each reconciles with the pivot views. */
export function CrmReports() {
  const [groupBy, setGroupBy] = useState<"user_id" | "team_id">("user_id");
  const forecast = useQuery({
    queryKey: ["crm-report", "forecast"],
    queryFn: () =>
      request<{ month: string | null; count: number; pipeline: number; weighted: number }[]>(
        "GET",
        "/crm/reports/forecast",
      ),
  });
  const funnel = useQuery({
    queryKey: ["crm-report", "funnel"],
    queryFn: () =>
      request<{ stage: string; count: number; revenue: number; reached: number; conversion: number }[]>(
        "GET",
        "/crm/reports/funnel",
      ),
  });
  const winRate = useQuery({
    queryKey: ["crm-report", "win-rate", groupBy],
    queryFn: () =>
      request<{ label: string; won: number; lost: number; win_rate: number; won_revenue: number }[]>(
        "GET",
        "/crm/reports/win-rate",
        {
          query: { group_by: groupBy },
        },
      ),
  });
  const velocity = useQuery({
    queryKey: ["crm-report", "velocity"],
    queryFn: () => request<Velocity>("GET", "/crm/reports/velocity"),
  });
  const maxReached = Math.max(1, ...(funnel.data ?? []).map((r) => r.reached));

  return (
    <div className="crm-reports">
      <h1 className="page-title">{t("Sales analysis")}</h1>
      {velocity.data && (
        <div className="kpis" aria-label={t("Sales velocity")}>
          <div className="kpi">
            <span className="kpi-value">{formatNumber(velocity.data.velocity_per_day, 0)}</span>
            <span className="kpi-label">{t("Sales velocity per day")}</span>
          </div>
          <div className="kpi">
            <span className="kpi-value">{velocity.data.open_opportunities}</span>
            <span className="kpi-label">{t("Open opportunities")}</span>
          </div>
          <div className="kpi">
            <span className="kpi-value">{formatNumber(velocity.data.average_deal, 0)}</span>
            <span className="kpi-label">{t("Average won deal")}</span>
          </div>
          <div className="kpi">
            <span className="kpi-value">{formatNumber(velocity.data.win_rate, 1)} %</span>
            <span className="kpi-label">{t("Win rate")}</span>
          </div>
          <div className="kpi">
            <span className="kpi-value">{formatNumber(velocity.data.average_cycle_days, 1)}</span>
            <span className="kpi-label">{t("Average cycle (days)")}</span>
          </div>
        </div>
      )}
      <section aria-labelledby="funnel-h">
        <h2 id="funnel-h">{t("Funnel")}</h2>
        <table className="pivot-table">
          <thead>
            <tr>
              <th scope="col">{t("Stage")}</th>
              <th scope="col" className="num">
                {t("In stage")}
              </th>
              <th scope="col" className="num">
                {t("Reached")}
              </th>
              <th scope="col">{t("Conversion")}</th>
              <th scope="col" className="num">
                {t("Revenue in stage")}
              </th>
            </tr>
          </thead>
          <tbody>
            {(funnel.data ?? []).map((r) => (
              <tr key={r.stage}>
                <th scope="row">{r.stage}</th>
                <td className="num">{r.count}</td>
                <td className="num">{r.reached}</td>
                <td>
                  <span className="bar" style={{ width: `${(100 * r.reached) / maxReached}%` }} aria-hidden="true" />{" "}
                  {r.conversion} %
                </td>
                <td className="num">{formatNumber(r.revenue, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <AiForecastSection />
      <section aria-labelledby="forecast-h">
        <h2 id="forecast-h">{t("Forecast by expected closing month")}</h2>
        <table className="pivot-table">
          <thead>
            <tr>
              <th scope="col">{t("Month")}</th>
              <th scope="col" className="num">
                {t("Opportunities")}
              </th>
              <th scope="col" className="num">
                {t("Pipeline")}
              </th>
              <th scope="col" className="num">
                {t("Weighted")}
              </th>
            </tr>
          </thead>
          <tbody>
            {(forecast.data ?? []).map((r) => (
              <tr key={r.month ?? "none"}>
                <th scope="row">{r.month ? r.month.slice(0, 7) : t("No date")}</th>
                <td className="num">{r.count}</td>
                <td className="num">{formatNumber(r.pipeline, 2)}</td>
                <td className="num">{formatNumber(r.weighted, 2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section aria-labelledby="winrate-h">
        <h2 id="winrate-h">
          {tx("Win rate by {group}", {
            group: (
              <select
                className="input small"
                aria-label={t("Group win rate by")}
                value={groupBy}
                onChange={(e) => setGroupBy(e.target.value as "user_id" | "team_id")}
              >
                <option value="user_id">{t("salesperson")}</option>
                <option value="team_id">{t("team")}</option>
              </select>
            ),
          })}
        </h2>
        <table className="pivot-table">
          <thead>
            <tr>
              <th scope="col">{groupBy === "user_id" ? t("Salesperson") : t("Team")}</th>
              <th scope="col" className="num">
                {t("Won")}
              </th>
              <th scope="col" className="num">
                {t("Lost")}
              </th>
              <th scope="col" className="num">
                {t("Win rate")}
              </th>
              <th scope="col" className="num">
                {t("Won revenue")}
              </th>
            </tr>
          </thead>
          <tbody>
            {(winRate.data ?? []).map((r) => (
              <tr key={r.label}>
                <th scope="row">{r.label}</th>
                <td className="num">{r.won}</td>
                <td className="num">{r.lost}</td>
                <td className="num">{formatNumber(r.win_rate, 1)} %</td>
                <td className="num">{formatNumber(r.won_revenue, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}

registerClientAction("crm.reports", () => <CrmReports />);
