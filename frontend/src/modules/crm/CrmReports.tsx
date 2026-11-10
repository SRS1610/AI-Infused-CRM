import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { request } from "../../kernel/api/client";
import { registerClientAction } from "../../kernel/extensions";
import { formatNumber } from "../../kernel/format";
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
      <h1 className="page-title">Sales analysis</h1>
      {velocity.data && (
        <div className="kpis" aria-label="Sales velocity">
          <div className="kpi">
            <span className="kpi-value">{formatNumber(velocity.data.velocity_per_day, 0)}</span>
            <span className="kpi-label">Sales velocity per day</span>
          </div>
          <div className="kpi">
            <span className="kpi-value">{velocity.data.open_opportunities}</span>
            <span className="kpi-label">Open opportunities</span>
          </div>
          <div className="kpi">
            <span className="kpi-value">{formatNumber(velocity.data.average_deal, 0)}</span>
            <span className="kpi-label">Average won deal</span>
          </div>
          <div className="kpi">
            <span className="kpi-value">{formatNumber(velocity.data.win_rate, 1)} %</span>
            <span className="kpi-label">Win rate</span>
          </div>
          <div className="kpi">
            <span className="kpi-value">{formatNumber(velocity.data.average_cycle_days, 1)}</span>
            <span className="kpi-label">Average cycle (days)</span>
          </div>
        </div>
      )}
      <section aria-labelledby="funnel-h">
        <h2 id="funnel-h">Funnel</h2>
        <table className="pivot-table">
          <thead>
            <tr>
              <th scope="col">Stage</th>
              <th scope="col" className="num">
                In stage
              </th>
              <th scope="col" className="num">
                Reached
              </th>
              <th scope="col">Conversion</th>
              <th scope="col" className="num">
                Revenue in stage
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
        <h2 id="forecast-h">Forecast by expected closing month</h2>
        <table className="pivot-table">
          <thead>
            <tr>
              <th scope="col">Month</th>
              <th scope="col" className="num">
                Opportunities
              </th>
              <th scope="col" className="num">
                Pipeline
              </th>
              <th scope="col" className="num">
                Weighted
              </th>
            </tr>
          </thead>
          <tbody>
            {(forecast.data ?? []).map((r) => (
              <tr key={r.month ?? "none"}>
                <th scope="row">{r.month ? r.month.slice(0, 7) : "No date"}</th>
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
          Win rate by{" "}
          <select
            className="input small"
            aria-label="Group win rate by"
            value={groupBy}
            onChange={(e) => setGroupBy(e.target.value as "user_id" | "team_id")}
          >
            <option value="user_id">salesperson</option>
            <option value="team_id">team</option>
          </select>
        </h2>
        <table className="pivot-table">
          <thead>
            <tr>
              <th scope="col">{groupBy === "user_id" ? "Salesperson" : "Team"}</th>
              <th scope="col" className="num">
                Won
              </th>
              <th scope="col" className="num">
                Lost
              </th>
              <th scope="col" className="num">
                Win rate
              </th>
              <th scope="col" className="num">
                Won revenue
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
