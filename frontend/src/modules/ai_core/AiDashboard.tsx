// Administrator view: AI usage and cost per company and feature (from the gateway ledger), acceptance
// rates (from the run audit) and per-company kill switches.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ai, aiErrorMessage, type AdminFeature, type UsageRow } from "../../kernel/ai/api";
import { registerClientAction } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
const usd = (v: string | number) =>
  Number(v).toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
const int = (v: number) => v.toLocaleString();

/** Risk classes of the EU AI Act as the gateway reports them; unknown classes show as sent. */
function riskLabel(risk: string): string {
  const labels: Record<string, string> = {
    minimal: t("minimal"),
    limited: t("limited"),
    high: t("high"),
    unacceptable: t("unacceptable"),
  };
  return labels[risk] ?? risk;
}

function firstOfMonth(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-01`;
}

function UsageTable({ rows }: { rows: UsageRow[] }) {
  const total = rows.reduce((s, r) => s + Number(r.cost), 0);
  const requests = rows.reduce((s, r) => s + r.requests, 0);
  return (
    <table className="data-table">
      <caption className="sr-only">{t("AI usage per company and feature")}</caption>
      <thead>
        <tr>
          <th scope="col">{t("Company")}</th>
          <th scope="col">{t("Feature")}</th>
          <th scope="col" className="num">
            {t("Requests")}
          </th>
          <th scope="col" className="num">
            {t("Input tokens")}
          </th>
          <th scope="col" className="num">
            {t("Output tokens")}
          </th>
          <th scope="col" className="num">
            {t("Spend")}
          </th>
          <th scope="col" className="num">
            {t("Latency p50")}
          </th>
          <th scope="col" className="num">
            {t("Latency p95")}
          </th>
          <th scope="col" className="num">
            {t("Accepted")}
          </th>
          <th scope="col" className="num">
            {t("Fallback")}
          </th>
          <th scope="col" className="num">
            {t("Cache hits")}
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={`${r.company_id}-${r.feature}`}>
            <td>{r.company || "—"}</td>
            <td title={r.feature}>{r.feature_title}</td>
            <td className="num">{int(r.requests)}</td>
            <td className="num">{int(r.input_tokens + r.cache_read_tokens)}</td>
            <td className="num">{int(r.output_tokens)}</td>
            <td className="num">{usd(r.cost)}</td>
            <td className="num">{Math.round(r.latency_p50)} ms</td>
            <td className="num">{Math.round(r.latency_p95)} ms</td>
            <td className="num">{pct(r.acceptance_rate)}</td>
            <td className="num">{pct(r.fallback_rate)}</td>
            <td className="num">{pct(r.cache_hit_rate)}</td>
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr>
          <th scope="row" colSpan={2}>
            {t("Total")}
          </th>
          <td className="num">{int(requests)}</td>
          <td colSpan={2} />
          <td className="num">{usd(total)}</td>
          <td colSpan={5} />
        </tr>
      </tfoot>
    </table>
  );
}

function Switches({ features }: { features: AdminFeature[] }) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const companies = features[0]?.companies ?? [];
  const toggle = async (f: AdminFeature, company: number | null, enabled: boolean) => {
    setError(null);
    try {
      await ai.setFlag(f.name, enabled, company);
      await qc.invalidateQueries({ queryKey: ["ai"] });
    } catch (e) {
      setError(aiErrorMessage(e));
    }
  };
  return (
    <>
      {error && <div className="alert error">{error}</div>}
      <table className="data-table">
        <caption className="sr-only">{t("AI features and kill switches")}</caption>
        <thead>
          <tr>
            <th scope="col">{t("Feature")}</th>
            <th scope="col">{t("Risk")}</th>
            <th scope="col">{t("Everywhere")}</th>
            {companies.map((c) => (
              <th scope="col" key={c.id}>
                {c.name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {features.map((f) => (
            <tr key={f.name}>
              <th scope="row" title={f.name}>
                {f.title}
                {(f.writes || f.outbound) && <span className="muted small"> · {t("proposes changes")}</span>}
              </th>
              <td>{riskLabel(f.risk_class)}</td>
              <td>
                <input
                  type="checkbox"
                  aria-label={t("{feature} enabled everywhere", { feature: f.title })}
                  checked={f.enabled_global}
                  onChange={(e) => void toggle(f, null, e.target.checked)}
                />
              </td>
              {f.companies.map((c) => (
                <td key={c.id}>
                  <input
                    type="checkbox"
                    aria-label={t("{feature} enabled for {company}", { feature: f.title, company: c.name })}
                    checked={c.enabled}
                    disabled={!f.enabled_global}
                    onChange={(e) => void toggle(f, c.id, e.target.checked)}
                  />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

export function AiDashboard() {
  const [since, setSince] = useState(firstOfMonth);
  const usage = useQuery({ queryKey: ["ai", "usage", since], queryFn: () => ai.usage({ since }) });
  const features = useQuery({ queryKey: ["ai", "admin-features"], queryFn: ai.adminFeatures });
  return (
    <div className="page ai-dashboard">
      <header className="page-header">
        <h1>{t("AI usage and cost")}</h1>
        <label className="field inline">
          <span>{t("Since")}</span>
          <input type="date" value={since} onChange={(e) => setSince(e.target.value)} />
        </label>
      </header>
      <section aria-labelledby="ai-usage-h">
        <h2 id="ai-usage-h">{t("Usage")}</h2>
        {usage.isLoading && <p className="muted">{t("Loading…")}</p>}
        {usage.error && <div className="alert error">{aiErrorMessage(usage.error)}</div>}
        {usage.data && !usage.data.available && (
          <div className="alert info">
            {t("The AI gateway is unavailable: {message}", { message: usage.data.message })}
          </div>
        )}
        {usage.data?.available &&
          (usage.data.rows.length ? (
            <UsageTable rows={usage.data.rows} />
          ) : (
            <p className="muted">{t("No AI usage in this period.")}</p>
          ))}
      </section>
      <section aria-labelledby="ai-flags-h">
        <h2 id="ai-flags-h">{t("Features and kill switches")}</h2>
        {features.data &&
          (features.data.length ? (
            <Switches features={features.data} />
          ) : (
            <p className="muted">{t("No AI features are installed yet.")}</p>
          ))}
      </section>
    </div>
  );
}

registerClientAction("ai.dashboard", () => <AiDashboard />);
