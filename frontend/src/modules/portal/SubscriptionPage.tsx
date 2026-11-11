import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { request } from "../../kernel/api/client";
import { formatMonetary } from "../../kernel/format";
import { t } from "../../kernel/i18n";
import { paymentStateLabel } from "./InvoicePage";

export interface SubscriptionSummary {
  id: number;
  name: string;
  plan: string | null;
  state: string;
  next_invoice_date: string | null;
  recurring_total: number;
  recurring_total_taxed: number;
  currency: string;
}

export interface PortalSubscription {
  id: number;
  name: string;
  state: string;
  state_label: string;
  plan: string | null;
  billing_period: { value: number | null; unit: string | null; unit_label: string | null };
  start_date: string | null;
  next_invoice_date: string | null;
  end_date: string | null;
  closing: boolean;
  currency: string;
  recurring_total: number;
  recurring_total_taxed: number;
  payment_method: string | null;
  can_close: boolean;
  close_reasons: { id: number; name: string }[];
  lines: { id: number; name: string; quantity: number; price_unit: number; discount: number; price_subtotal: number }[];
  invoices: {
    id: number;
    name: string;
    move_type: string;
    invoice_date: string | null;
    period_start: string | null;
    period_end: string | null;
    amount_total: number;
    payment_state: string;
    url: string;
  }[];
}

/** A subscription's status as the customer reads it. */
export function subscriptionStateLabel(state: string): string {
  const labels: Record<string, string> = {
    draft: t("Not started yet"),
    progress: t("Active"),
    paused: t("Paused"),
    closed: t("Renewed"),
    churned: t("Ended"),
  };
  return labels[state] ?? state;
}

/** One subscription: status, next invoice, lines, recent invoices and (when the plan allows) closing. */
export function SubscriptionPage() {
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const token = params.get("token") ?? undefined;
  const queryClient = useQueryClient();
  const key = ["portal-subscription", id, token];
  const sub = useQuery({
    queryKey: key,
    queryFn: () => request<PortalSubscription>("GET", `/sale/subscriptions/${id}`, { query: { token } }),
  });
  const [closing, setClosing] = useState(false);
  const [reasonId, setReasonId] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (sub.isLoading)
    return (
      <div className="loading" role="status">
        {t("Loading…")}
      </div>
    );
  if (sub.isError || !sub.data)
    return (
      <div className="portal-page">
        <div className="alert error" role="alert">
          {(sub.error as Error)?.message ?? t("Subscription not found")}
        </div>
      </div>
    );
  const s = sub.data;
  const reason = reasonId ?? s.close_reasons[0]?.id ?? null;
  const close = async (e: React.FormEvent) => {
    e.preventDefault();
    if (reason === null) return;
    setError(null);
    try {
      const updated = await request<PortalSubscription>("POST", `/sale/subscriptions/${id}/close`, {
        body: { reason_id: reason, note: note || null, token: token ?? null },
      });
      queryClient.setQueryData(key, updated);
      setClosing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };
  const period =
    s.billing_period.value && s.billing_period.value > 1
      ? t("Billed every {count} {unit}", { count: s.billing_period.value, unit: s.billing_period.unit_label ?? "" })
      : s.billing_period.unit === "week"
        ? t("Billed weekly")
        : s.billing_period.unit === "year"
          ? t("Billed yearly")
          : t("Billed monthly");

  return (
    <div className="portal-page portal-subscription">
      <h1>{t("Subscription {name}", { name: s.name })}</h1>
      <p>
        {s.plan && <strong>{s.plan}</strong>} · {period} ·{" "}
        <span className={`badge state-${s.state}`}>{subscriptionStateLabel(s.state)}</span>
      </p>
      <dl className="portal-facts">
        <dt>{t("Started on")}</dt>
        <dd>{s.start_date ?? "—"}</dd>
        <dt>{t("Next invoice")}</dt>
        <dd>{s.next_invoice_date ?? "—"}</dd>
        <dt>{t("Amount per period")}</dt>
        <dd>{formatMonetary(s.recurring_total_taxed, s.currency)}</dd>
        {s.end_date && (
          <>
            <dt>{t("Ends on")}</dt>
            <dd>{s.end_date}</dd>
          </>
        )}
        {s.payment_method && (
          <>
            <dt>{t("Paid with")}</dt>
            <dd>{s.payment_method}</dd>
          </>
        )}
      </dl>
      {s.closing && (
        <div className="alert info" role="status">
          {t("Your subscription ends on {date}.", { date: s.end_date ?? "" })}
        </div>
      )}
      <table className="pivot-table">
        <thead>
          <tr>
            <th scope="col">{t("Description")}</th>
            <th scope="col" className="num">
              {t("Quantity")}
            </th>
            <th scope="col" className="num">
              {t("Unit price")}
            </th>
            <th scope="col" className="num">
              {t("Amount")}
            </th>
          </tr>
        </thead>
        <tbody>
          {s.lines.map((line) => (
            <tr key={line.id}>
              <th scope="row">{line.name}</th>
              <td className="num">{line.quantity}</td>
              <td className="num">{formatMonetary(line.price_unit, s.currency)}</td>
              <td className="num">{formatMonetary(line.price_subtotal, s.currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h2>{t("Invoices")}</h2>
      {s.invoices.length === 0 ? (
        <p className="muted">{t("No invoices yet.")}</p>
      ) : (
        <table className="pivot-table">
          <thead>
            <tr>
              <th scope="col">{t("Number")}</th>
              <th scope="col">{t("Period")}</th>
              <th scope="col" className="num">
                {t("Total")}
              </th>
              <th scope="col">{t("Status")}</th>
            </tr>
          </thead>
          <tbody>
            {s.invoices.map((inv) => (
              <tr key={inv.id}>
                <th scope="row">
                  <Link to={inv.url}>{inv.name}</Link>
                </th>
                <td>{inv.period_start ? `${inv.period_start} – ${inv.period_end}` : inv.invoice_date}</td>
                <td className="num">
                  {formatMonetary(inv.move_type === "out_refund" ? -inv.amount_total : inv.amount_total, s.currency)}
                </td>
                <td>{paymentStateLabel(inv.payment_state)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {s.can_close && !closing && (
        <button type="button" className="btn" onClick={() => setClosing(true)}>
          {t("Close my subscription")}
        </button>
      )}
      {s.can_close && closing && (
        <form className="portal-form" onSubmit={close}>
          <p>{t("Your subscription stays active until the end of the period already invoiced.")}</p>
          <label>
            {t("Why are you leaving?")}
            <select className="input" value={reason ?? ""} onChange={(e) => setReasonId(Number(e.target.value))}>
              {s.close_reasons.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t("Comment (optional)")}
            <textarea className="input" value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          <button type="submit" className="btn primary">
            {t("Confirm closing")}
          </button>{" "}
          <button type="button" className="btn ghost" onClick={() => setClosing(false)}>
            {t("Keep my subscription")}
          </button>
        </form>
      )}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
