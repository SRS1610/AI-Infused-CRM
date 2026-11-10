import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { request } from "../../kernel/api/client";
import { formatMonetary } from "../../kernel/format";
import { t, tx } from "../../kernel/i18n";

interface QuoteLine {
  id: number;
  display_type: "section" | "note" | null;
  name: string;
  quantity: number;
  uom: string | null;
  price_unit: number;
  discount: number;
  taxes: string[];
  price_subtotal: number;
  price_total?: number;
  is_optional: boolean;
}

export interface Quote {
  id: number;
  name: string;
  state: string;
  date_order: string | null;
  validity_date: string | null;
  customer: string;
  company: { name: string; email: string | null; phone: string | null };
  salesperson: string | null;
  currency: string;
  amount_untaxed: number;
  amount_tax: number;
  amount_total: number;
  payment_terms?: string | null;
  can_add_options?: boolean;
  note: string | null;
  signed_by: string | null;
  signed_at: string | null;
  lines: QuoteLine[];
}

/** A quotation's state as the customer reads it; unknown states show as sent by the server. */
export function quoteStateLabel(state: string): string {
  const labels: Record<string, string> = {
    draft: t("In preparation"),
    sent: t("Waiting for your answer"),
    accepted: t("Confirmed"),
    rejected: t("Declined"),
    expired: t("Expired"),
    cancelled: t("Cancelled"),
  };
  return labels[state] ?? state;
}

/** A quotation as the customer sees it: by token link (no login) or as a signed-in portal user. */
export function QuotePage() {
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const token = params.get("token") ?? undefined;
  const queryClient = useQueryClient();
  const quote = useQuery({
    queryKey: ["portal-quote", id, token],
    queryFn: () => request<Quote>("GET", `/sale/quotes/${id}`, { query: { token } }),
  });
  const [name, setName] = useState("");
  const [reason, setReason] = useState("");
  const [mode, setMode] = useState<"accept" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (quote.isLoading)
    return (
      <div className="loading" role="status">
        {t("Loading…")}
      </div>
    );
  if (quote.isError)
    return (
      <div className="portal-page">
        <div className="alert error" role="alert">
          {(quote.error as Error).message}
        </div>
      </div>
    );
  const q = quote.data!;
  const money = (v: number) => formatMonetary(v, q.currency);
  const act = async (kind: "accept" | "reject") => {
    setError(null);
    try {
      const body = kind === "accept" ? { name, token } : { reason, token };
      const updated = await request<Quote>("POST", `/sale/quotes/${id}/${kind}`, { body });
      queryClient.setQueryData(["portal-quote", id, token], updated);
      setMode(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  const addOption = async (lineId: number) => {
    setError(null);
    try {
      const updated = await request<Quote>("POST", `/sale/quotes/${id}/optional/${lineId}`, { body: { token } });
      queryClient.setQueryData(["portal-quote", id, token], updated);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  const pdf = `/api/v1/sale/quotes/${id}/pdf${token ? `?token=${encodeURIComponent(token)}` : ""}`;

  return (
    <div className="portal-page">
      <header className="portal-quote-head">
        <div>
          <p className="muted">{q.company.name}</p>
          <h1>{t("Quotation {name}", { name: q.name })}</h1>
          <p>
            {tx("For {customer}", { customer: <strong>{q.customer}</strong> })}
            {q.validity_date && <> · {t("valid until {date}", { date: q.validity_date })}</>}
          </p>
        </div>
        <span className={`badge state-${q.state}`}>{quoteStateLabel(q.state)}</span>
      </header>
      <div className="table-scroll">
        <table className="pivot-table quote-lines">
          <thead>
            <tr>
              <th scope="col">{t("Description")}</th>
              <th scope="col" className="num">
                {t("Quantity")}
              </th>
              <th scope="col" className="num">
                {t("Unit price")}
              </th>
              <th scope="col">{t("Taxes")}</th>
              <th scope="col" className="num">
                {t("Amount")}
              </th>
            </tr>
          </thead>
          <tbody>
            {q.lines.map((line) =>
              line.display_type === "section" ? (
                <tr key={line.id} className="section">
                  <th scope="rowgroup" colSpan={5}>
                    {line.name}
                  </th>
                </tr>
              ) : line.display_type === "note" ? (
                <tr key={line.id}>
                  <td colSpan={5} className="muted">
                    {line.name}
                  </td>
                </tr>
              ) : (
                <tr key={line.id} className={line.is_optional ? "optional" : undefined}>
                  <td>
                    {line.name}
                    {line.is_optional && <span className="badge">{t("Optional")}</span>}
                    {line.discount ? <span className="muted"> (−{line.discount} %)</span> : null}
                    {line.is_optional && q.can_add_options && (
                      <>
                        {" "}
                        <button
                          type="button"
                          className="btn small"
                          onClick={() => void addOption(line.id)}
                          aria-label={t("Add {name} to my order", { name: line.name })}
                        >
                          {t("Add to my order")}
                        </button>
                      </>
                    )}
                  </td>
                  <td className="num">
                    {line.quantity} {line.uom ?? ""}
                  </td>
                  <td className="num">{money(line.price_unit)}</td>
                  <td>{(line.taxes ?? []).join(", ")}</td>
                  <td className="num">{money(line.price_subtotal)}</td>
                </tr>
              ),
            )}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row" colSpan={4}>
                {t("Untaxed")}
              </th>
              <td className="num">{money(q.amount_untaxed)}</td>
            </tr>
            <tr>
              <th scope="row" colSpan={4}>
                {t("Taxes")}
              </th>
              <td className="num">{money(q.amount_tax)}</td>
            </tr>
            <tr>
              <th scope="row" colSpan={4}>
                {t("Total")}
              </th>
              <td className="num total">{money(q.amount_total)}</td>
            </tr>
          </tfoot>
        </table>
      </div>
      {q.payment_terms && <p className="muted">{t("Payment terms: {terms}", { terms: q.payment_terms })}</p>}
      {q.note && <div className="quote-note" dangerouslySetInnerHTML={{ __html: q.note }} />}
      <div className="button-row">
        <a className="btn" href={pdf} target="_blank" rel="noreferrer">
          {t("Download PDF")}
        </a>
        {q.state === "sent" && (
          <>
            <button type="button" className="btn primary" onClick={() => setMode("accept")}>
              {t("Accept")}
            </button>
            <button type="button" className="btn ghost" onClick={() => setMode("reject")}>
              {t("Decline")}
            </button>
          </>
        )}
      </div>
      {mode === "accept" && (
        <form
          className="portal-form"
          onSubmit={(e) => {
            e.preventDefault();
            void act("accept");
          }}
        >
          <label>
            {t("Type your full name to accept this quotation")}
            <input
              className="input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              autoComplete="name"
            />
          </label>
          <button type="submit" className="btn primary">
            {t("Accept and sign")}
          </button>
        </form>
      )}
      {mode === "reject" && (
        <form
          className="portal-form"
          onSubmit={(e) => {
            e.preventDefault();
            void act("reject");
          }}
        >
          <label>
            {t("Tell us why (optional)")}
            <textarea className="input" value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          <button type="submit" className="btn">
            {t("Decline quotation")}
          </button>
        </form>
      )}
      {q.state === "accepted" && q.signed_by && (
        <div className="alert info" role="status">
          {t("Accepted by {name}. Thank you!", { name: q.signed_by })}
        </div>
      )}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      <p className="muted small">
        {t("Questions?")} {q.salesperson && <>{t("Your contact is {name}.", { name: q.salesperson })} </>}
        {q.company.email && <a href={`mailto:${q.company.email}`}>{q.company.email}</a>}
      </p>
    </div>
  );
}
