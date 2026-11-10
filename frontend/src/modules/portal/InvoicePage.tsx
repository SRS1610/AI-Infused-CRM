import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { request } from "../../kernel/api/client";
import { formatMonetary } from "../../kernel/format";
import { t } from "../../kernel/i18n";

interface InvoiceLine {
  id: number;
  display_type: string;
  name: string | null;
  quantity: number;
  uom: string | null;
  price_unit: number;
  discount: number;
  taxes: string[];
  price_subtotal: number;
}

export interface PortalInvoice {
  id: number;
  name: string;
  move_type: string;
  state: string;
  payment_state: string;
  invoice_date: string | null;
  invoice_date_due: string | null;
  customer: string;
  company: { name: string; email: string | null; phone: string | null };
  currency: string;
  amount_untaxed: number;
  amount_tax: number;
  amount_total: number;
  amount_residual: number;
  payment_reference: string;
  lines: InvoiceLine[];
}

export interface PaymentOptions {
  can_pay: boolean;
  providers: { id: number; name: string; code: string; test_mode: boolean; can_save: boolean }[];
  transactions: { reference: string; state: string; message: string | null; amount: number }[];
}

interface PaymentStatus {
  reference: string;
  state: string;
  message: string | null;
  payment_state: string;
}

/** An invoice's payment state as the customer reads it. */
export function paymentStateLabel(state: string): string {
  const labels: Record<string, string> = {
    not_paid: t("To pay"),
    partial: t("Partially paid"),
    in_payment: t("Payment received, being processed"),
    paid: t("Paid"),
    reversed: t("Credited"),
  };
  return labels[state] ?? state;
}

function transactionMessage(status: PaymentStatus): { kind: "info" | "error"; text: string } {
  if (status.state === "done") return { kind: "info", text: t("Thank you, your payment has been received.") };
  if (status.state === "pending" || status.state === "authorized")
    return { kind: "info", text: t("Your payment is being processed.") };
  if (status.state === "cancel") return { kind: "error", text: t("The payment was cancelled.") };
  return { kind: "error", text: status.message || t("The payment failed.") };
}

/** A customer invoice by token link or for a signed-in portal user, with online payment. */
export function InvoicePage() {
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const token = params.get("token") ?? undefined;
  const reference = params.get("payment");
  const invoice = useQuery({
    queryKey: ["portal-invoice", id, token],
    queryFn: () => request<PortalInvoice>("GET", `/account/invoices/${id}`, { query: { token } }),
  });
  const options = useQuery({
    queryKey: ["portal-invoice-pay", id, token],
    queryFn: () => request<PaymentOptions>("GET", `/payment/invoices/${id}`, { query: { token } }),
    retry: false,
  });
  const status = useQuery({
    queryKey: ["portal-invoice-status", id, token, reference],
    queryFn: () =>
      request<PaymentStatus>("GET", `/payment/invoices/${id}/status`, { query: { token, reference: reference! } }),
    enabled: !!reference,
    refetchInterval: (q) => (q.state.data && ["pending", "authorized"].includes(q.state.data.state) ? 3000 : false),
  });
  const [providerId, setProviderId] = useState<number | null>(null);
  const [saveMethod, setSaveMethod] = useState(false);
  const [paying, setPaying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (invoice.isLoading)
    return (
      <div className="loading" role="status">
        {t("Loading…")}
      </div>
    );
  if (invoice.isError || !invoice.data)
    return (
      <div className="portal-page">
        <div className="alert error" role="alert">
          {(invoice.error as Error)?.message ?? t("Invoice not found")}
        </div>
      </div>
    );
  const inv = invoice.data;
  const providers = options.data?.providers ?? [];
  const chosen = providers.find((p) => p.id === providerId) ?? providers[0];
  const pay = async () => {
    if (!chosen) return;
    setPaying(true);
    setError(null);
    try {
      const res = await request<{ redirect_url: string }>("POST", `/payment/invoices/${id}/pay`, {
        body: { token: token ?? null, provider_id: chosen.id, save_method: saveMethod },
      });
      window.location.assign(res.redirect_url);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPaying(false);
    }
  };
  const notice = status.data ? transactionMessage(status.data) : null;
  const title =
    inv.move_type === "out_refund"
      ? t("Credit note {name}", { name: inv.name })
      : t("Invoice {name}", { name: inv.name });

  return (
    <div className="portal-page portal-invoice">
      <h1>{title}</h1>
      <p>
        <strong>{inv.company.name}</strong> · {inv.customer}
      </p>
      <p>
        {t("Date")}: {inv.invoice_date} · {t("Due date")}: {inv.invoice_date_due ?? "—"} ·{" "}
        <span className={`badge state-${inv.payment_state}`}>{paymentStateLabel(inv.payment_state)}</span>
      </p>
      {notice && (
        <div
          className={`alert ${notice.kind === "error" ? "error" : "info"}`}
          role={notice.kind === "error" ? "alert" : "status"}
        >
          {notice.text}
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
            <th scope="col">{t("Taxes")}</th>
            <th scope="col" className="num">
              {t("Amount")}
            </th>
          </tr>
        </thead>
        <tbody>
          {inv.lines.map((line) =>
            line.display_type === "line_section" || line.display_type === "line_note" ? (
              <tr key={line.id}>
                <th scope="row" colSpan={5}>
                  {line.name}
                </th>
              </tr>
            ) : (
              <tr key={line.id}>
                <th scope="row">{line.name}</th>
                <td className="num">
                  {line.quantity} {line.uom ?? ""}
                </td>
                <td className="num">{formatMonetary(line.price_unit, inv.currency)}</td>
                <td>{line.taxes.join(", ")}</td>
                <td className="num">{formatMonetary(line.price_subtotal, inv.currency)}</td>
              </tr>
            ),
          )}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row" colSpan={4}>
              {t("Untaxed amount")}
            </th>
            <td className="num">{formatMonetary(inv.amount_untaxed, inv.currency)}</td>
          </tr>
          <tr>
            <th scope="row" colSpan={4}>
              {t("Taxes")}
            </th>
            <td className="num">{formatMonetary(inv.amount_tax, inv.currency)}</td>
          </tr>
          <tr>
            <th scope="row" colSpan={4}>
              {t("Total")}
            </th>
            <td className="num">{formatMonetary(inv.amount_total, inv.currency)}</td>
          </tr>
          <tr>
            <th scope="row" colSpan={4}>
              {t("Amount due")}
            </th>
            <td className="num">
              <strong>{formatMonetary(inv.amount_residual, inv.currency)}</strong>
            </td>
          </tr>
        </tfoot>
      </table>
      <p className="muted">{t("Payment reference: {ref}", { ref: inv.payment_reference })}</p>
      {options.data?.can_pay && chosen && (
        <div className="pay">
          {providers.length > 1 && (
            <label>
              {t("Pay with")}{" "}
              <select className="input" value={chosen.id} onChange={(e) => setProviderId(Number(e.target.value))}>
                {providers.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          {chosen.can_save && (
            <label>
              <input type="checkbox" checked={saveMethod} onChange={(e) => setSaveMethod(e.target.checked)} />{" "}
              {t("Save my payment method")}
            </label>
          )}
          <button type="button" className="btn primary" disabled={paying} onClick={() => void pay()}>
            {paying
              ? t("Redirecting…")
              : t("Pay now {amount}", { amount: formatMonetary(inv.amount_residual, inv.currency) })}
          </button>
          {chosen.test_mode && <span className="muted small">{t("Test mode: no real payment is taken.")}</span>}
        </div>
      )}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
