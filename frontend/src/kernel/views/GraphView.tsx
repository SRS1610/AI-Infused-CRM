import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../api/client";
import type { Domain, ViewDef, ViewsResponse } from "../api/types";
import { formatNumber } from "../format";
import { series } from "./pivotData";
import { groupableFields, measureFields } from "./PivotView";
import { t } from "../i18n";

interface Props {
  model: string;
  meta: ViewsResponse;
  view: ViewDef;
  domain: Domain;
}

type ChartType = "bar" | "line" | "pie";

function chartLabel(type: ChartType): string {
  return type === "bar" ? t("Bar") : type === "line" ? t("Line") : t("Pie");
}

function chartTitle(type: ChartType, measure: string, group: string): string {
  const vars = { measure, group };
  if (type === "bar") return t("{measure} by {group} (bar chart)", vars);
  if (type === "line") return t("{measure} by {group} (line chart)", vars);
  return t("{measure} by {group} (pie chart)", vars);
}
const W = 640;
const H = 280;
const PAD = { top: 16, right: 16, bottom: 56, left: 56 };
const PALETTE = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
];

/** Bar / line / pie chart of one grouping and one measure, with an accessible data table. */
export function GraphView({ model, meta, view, domain }: Props) {
  const groupable = groupableFields(meta);
  const [by, setBy] = useState<string>((view.row as string | undefined) ?? groupable[0]?.name ?? "");
  const [measure, setMeasure] = useState<string>(view.measures?.[0] ?? "__count");
  const [type, setType] = useState<ChartType>((view.chart as ChartType | undefined) ?? "bar");
  const byType = meta.fields[by]?.type;
  const spec = byType === "date" || byType === "datetime" ? `${by}:month` : by;
  const result = useQuery({
    queryKey: ["list", model, "graph", spec, measure, JSON.stringify(domain)],
    queryFn: () =>
      api.readGroup(model, { domain, aggregates: measure === "__count" ? [] : [`${measure}:sum`], groupby: [spec] }),
    enabled: !!by,
  });
  const data = useMemo(
    () => series(result.data?.groups ?? [], spec, measure === "__count" ? "__count" : `${measure}:sum`),
    [result.data, spec, measure],
  );
  const measureLabel = measure === "__count" ? t("Count") : (meta.fields[measure]?.string ?? measure);
  const byLabel = meta.fields[by]?.string ?? by;
  const max = Math.max(1, ...data.map((d) => d.value));
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const step = innerW / Math.max(data.length, 1);

  return (
    <div className="graph-view">
      <div className="pivot-controls">
        <label>
          {t("Group by")}
          <select className="input small" value={by} onChange={(e) => setBy(e.target.value)}>
            {groupable.map((f) => (
              <option key={f.name} value={f.name}>
                {f.string}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("Measure")}
          <select className="input small" value={measure} onChange={(e) => setMeasure(e.target.value)}>
            <option value="__count">{t("Count")}</option>
            {measureFields(meta).map((f) => (
              <option key={f.name} value={f.name}>
                {f.string}
              </option>
            ))}
          </select>
        </label>
        <div role="radiogroup" aria-label={t("Chart type")} className="view-switcher">
          {(["bar", "line", "pie"] as ChartType[]).map((c) => (
            <button
              key={c}
              type="button"
              role="radio"
              aria-checked={type === c}
              className={`btn small${type === c ? " active" : " ghost"}`}
              onClick={() => setType(c)}
            >
              {chartLabel(c)}
            </button>
          ))}
        </div>
      </div>
      {result.isLoading && <div className="loading">{t("Loading…")}</div>}
      {!result.isLoading && data.length === 0 && <div className="muted">{t("No data.")}</div>}
      {data.length > 0 && (
        <figure className="chart">
          <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={chartTitle(type, measureLabel, byLabel)}>
            {type !== "pie" && (
              <g>
                {[0, 0.25, 0.5, 0.75, 1].map((f) => {
                  const y = PAD.top + innerH * (1 - f);
                  return (
                    <g key={f}>
                      <line x1={PAD.left} x2={W - PAD.right} y1={y} y2={y} className="grid" />
                      <text x={PAD.left - 6} y={y + 4} className="axis-label" textAnchor="end">
                        {formatNumber(max * f, 0)}
                      </text>
                    </g>
                  );
                })}
                {data.map((d, i) => (
                  <text
                    key={d.label}
                    x={PAD.left + step * i + step / 2}
                    y={H - PAD.bottom + 16}
                    className="axis-label"
                    textAnchor="middle"
                  >
                    {d.label.length > 12 ? `${d.label.slice(0, 11)}…` : d.label}
                  </text>
                ))}
              </g>
            )}
            {type === "bar" &&
              data.map((d, i) => {
                const h = (d.value / max) * innerH;
                return (
                  <rect
                    key={d.label}
                    x={PAD.left + step * i + step * 0.15}
                    y={PAD.top + innerH - h}
                    width={step * 0.7}
                    height={h}
                    rx={2}
                    fill={PALETTE[0]}
                  >
                    <title>{`${d.label}: ${formatNumber(d.value)}`}</title>
                  </rect>
                );
              })}
            {type === "line" && (
              <polyline
                fill="none"
                stroke={PALETTE[0]}
                strokeWidth={2.5}
                points={data
                  .map((d, i) => `${PAD.left + step * i + step / 2},${PAD.top + innerH - (d.value / max) * innerH}`)
                  .join(" ")}
              />
            )}
            {type === "pie" && <Pie data={data} />}
          </svg>
          <figcaption className="sr-only">
            {t("{measure} by {group}", { measure: measureLabel, group: byLabel })}
          </figcaption>
          <table className="sr-only">
            <thead>
              <tr>
                <th scope="col">{byLabel}</th>
                <th scope="col">{measureLabel}</th>
              </tr>
            </thead>
            <tbody>
              {data.map((d) => (
                <tr key={d.label}>
                  <td>{d.label}</td>
                  <td>{d.value}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </figure>
      )}
    </div>
  );
}

function Pie({ data }: { data: { label: string; value: number }[] }) {
  const total = data.reduce((s, d) => s + d.value, 0) || 1;
  const cx = W / 2;
  const cy = H / 2;
  const r = H / 2 - 20;
  let angle = -Math.PI / 2;
  return (
    <g>
      {data.map((d, i) => {
        const slice = (d.value / total) * Math.PI * 2;
        const x1 = cx + r * Math.cos(angle);
        const y1 = cy + r * Math.sin(angle);
        angle += slice;
        const x2 = cx + r * Math.cos(angle);
        const y2 = cy + r * Math.sin(angle);
        const large = slice > Math.PI ? 1 : 0;
        const path =
          data.length === 1
            ? `M ${cx - r} ${cy} a ${r} ${r} 0 1 0 ${2 * r} 0 a ${r} ${r} 0 1 0 ${-2 * r} 0`
            : `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z`;
        return (
          <path key={d.label} d={path} fill={PALETTE[i % PALETTE.length]} stroke="var(--surface)" strokeWidth={1}>
            <title>{`${d.label}: ${formatNumber(d.value)} (${Math.round((d.value / total) * 100)}%)`}</title>
          </path>
        );
      })}
      {data.map((d, i) => (
        <g key={`legend-${d.label}`} transform={`translate(${W - 150}, ${20 + i * 18})`}>
          <rect width={10} height={10} fill={PALETTE[i % PALETTE.length]} />
          <text x={16} y={9} className="axis-label">
            {d.label}
          </text>
        </g>
      ))}
    </g>
  );
}
