// Reading bills and invoices from documents (account.bill_ocr): upload a PDF or an image, review what was
// read — partner, header, every auto-coded line, the totals checks and anomaly warnings — then accept to
// create the draft. Nothing is written before the person accepts.
import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../../kernel/api/client";
import { aiErrorMessage, ai, type Proposal } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, useAiFeature } from "../../kernel/ai/components";
import { registerClientAction, registerFormPanel, registerSearchAddon } from "../../kernel/extensions";
import { t } from "../../kernel/i18n";
import { accountAi, money, type ReadResult, type Review } from "./api";

export const FEATURE = "account.bill_ocr";
const MOVE = "account.move";

const ErrorNote = ({ message }: { message: string | null }) =>
  message ? (
    <div className="alert error" role="alert">
      {message}
    </div>
  ) : null;

function sourceLabel(source: string): string {
  const labels: Record<string, string> = {
    product: t("product"),
    history: t("partner history"),
    chart: t("chart keyword"),
    default: t("default account"),
  };
  return labels[source] ?? source;
}

function checkLabel(check: string): string {
  const labels: Record<string, string> = {
    lines_vs_untaxed: t("Lines vs untaxed amount"),
    tax: t("Computed tax vs printed tax"),
    total: t("Lines + tax vs total"),
  };
  return labels[check] ?? check;
}

type LineEdit = { account_id: number | null };

/** The review of one reading and its proposal (accept, edit accounts then accept, or reject). */
export function BillReview({
  result,
  onAccepted,
}: {
  result: ReadResult;
  onAccepted?: (moveId: number | null, proposal: Proposal) => void;
}) {
  const review = result.data as Review;
  const proposal = result.proposals[0];
  const [edits, setEdits] = useState<Record<number, LineEdit>>({});
  const [state, setState] = useState<Proposal["state"]>(proposal?.state ?? "pending");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const accounts = useQuery({
    queryKey: ["account-ai-accounts", review.move_type],
    queryFn: () =>
      api.list("account.account", {
        domain: [["internal_group", "in", review.move_type.startsWith("in_") ? ["expense", "asset"] : ["income"]]],
        fields: ["display_name"],
        limit: 200,
        order: "code",
      }),
    enabled: !!proposal,
  });
  const x = review.extraction;
  const edited = Object.keys(edits).length > 0;

  const payload = () => {
    if (!proposal || !edited) return undefined;
    const p = JSON.parse(JSON.stringify(proposal.payload)) as {
      args: { values: { invoice_line_ids: [string, Record<string, unknown>][] } };
    };
    p.args.values.invoice_line_ids.forEach((cmd, i) => {
      if (edits[i]) cmd[1].account_id = edits[i].account_id;
    });
    return p as unknown as Record<string, unknown>;
  };
  const accept = async () => {
    if (!proposal) return;
    setBusy(true);
    setError(null);
    try {
      const out = await accountAi.accept(proposal.id, payload());
      setState(out.proposal.state);
      onAccepted?.(out.move_id, out.proposal);
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const reject = async () => {
    if (!proposal) return;
    setBusy(true);
    try {
      setState((await ai.reject(proposal.id)).state);
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const decided = state !== "pending";

  return (
    <section className="ai-proposal bill-review" aria-label={t("Document reading")}>
      <header>
        <strong>{proposal?.title ?? t("Nothing to propose")}</strong> {!decided && <AiLabel />}
        {decided && <span className="badge">{state}</span>}
      </header>
      {review.needs_review ? (
        <div className="alert warning" role="alert">
          <strong>{t("Needs review")}</strong>
          <ul>
            {review.checks.differences.map((d) => (
              <li key={d.check}>
                {checkLabel(d.check)}: {money(d.document)} → {money(d.computed)} ({t("difference")}{" "}
                {money(d.difference)})
              </li>
            ))}
            {review.issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="muted small" role="status">
          {t("Totals check: the lines and taxes add up to the document's total.")}
        </p>
      )}
      {review.warnings.map((w) => (
        <p key={w} className="muted small">
          {w}
        </p>
      ))}
      {review.anomalies.length > 0 && (
        <div className="alert warning" role="alert" aria-label={t("Anomaly warnings")}>
          <strong>{t("Check before paying")}</strong>
          <ul>
            {review.anomalies.map((a) => (
              <li key={`${a.kind}-${a.related_id ?? 0}`}>
                {a.explanation}{" "}
                {a.related_id && <Link to={`/m/${MOVE}/${a.related_id}`}>{t("Open the similar document")}</Link>}
              </li>
            ))}
          </ul>
        </div>
      )}
      <table className="ai-diff">
        <caption className="sr-only">{t("Header read from the document")}</caption>
        <tbody>
          <tr>
            <th scope="row">{t("Partner")}</th>
            <td>
              {review.partner.id ? (
                <>
                  <Link to={`/m/contacts.partner/${review.partner.id}`}>{review.partner.display_name}</Link>{" "}
                  <span className="muted small">({t("matched by {how}", { how: review.partner.method })})</span>
                </>
              ) : (
                <>
                  {x.partner_name ?? "—"} <span className="badge">{t("new partner")}</span>
                </>
              )}
            </td>
          </tr>
          <tr>
            <th scope="row">{t("VAT ID")}</th>
            <td>{x.partner_vat ?? "—"}</td>
          </tr>
          <tr>
            <th scope="row">{t("Number")}</th>
            <td>{x.invoice_number ?? "—"}</td>
          </tr>
          <tr>
            <th scope="row">{t("Date")}</th>
            <td>{x.invoice_date ?? "—"}</td>
          </tr>
          <tr>
            <th scope="row">{t("Due date")}</th>
            <td>{x.due_date ?? "—"}</td>
          </tr>
          <tr>
            <th scope="row">{t("Total")}</th>
            <td>
              {money(x.amount_untaxed)} + {money(x.amount_tax)} ={" "}
              <strong>{money(x.amount_total, review.currency)}</strong>
            </td>
          </tr>
        </tbody>
      </table>
      <table className="ai-diff">
        <caption className="sr-only">{t("Auto-coded lines")}</caption>
        <thead>
          <tr>
            <th scope="col">{t("Description")}</th>
            <th scope="col">{t("Qty")}</th>
            <th scope="col">{t("Unit price")}</th>
            <th scope="col">{t("Amount")}</th>
            <th scope="col">{t("Account")}</th>
            <th scope="col">{t("Taxes")}</th>
          </tr>
        </thead>
        <tbody>
          {review.lines.map((ln, i) => (
            <tr key={`${ln.description}-${i}`}>
              <td>{ln.description}</td>
              <td>{ln.quantity}</td>
              <td>{money(ln.unit_price)}</td>
              <td>{money(ln.amount)}</td>
              <td>
                <select
                  aria-label={t("Account for {line}", { line: ln.description })}
                  value={String(edits[i]?.account_id ?? ln.account_id ?? "")}
                  disabled={decided || !proposal}
                  onChange={(e) =>
                    setEdits({ ...edits, [i]: { account_id: e.target.value ? Number(e.target.value) : null } })
                  }
                >
                  {ln.account_id && !accounts.data?.items.some((a) => a.id === ln.account_id) && (
                    <option value={ln.account_id}>{ln.account}</option>
                  )}
                  {accounts.data?.items.map((a) => (
                    <option key={a.id as number} value={a.id as number}>
                      {String(a.display_name)}
                    </option>
                  ))}
                </select>
                <div className="muted small">
                  {sourceLabel(ln.account_source)}
                  {ln.reason ? ` · ${ln.reason}` : ""}
                </div>
              </td>
              <td>
                {ln.taxes.join(", ") || "—"}
                {ln.issue && <div className="muted small">{ln.issue}</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ErrorNote message={error} />
      {proposal && !decided && (
        <div className="ai-actions">
          <button type="button" className="btn primary small" disabled={busy} onClick={() => void accept()}>
            {edited ? t("Accept edited") : t("Create the draft")}
          </button>
          <button type="button" className="btn small" disabled={busy} onClick={() => void reject()}>
            {t("Reject")}
          </button>
        </div>
      )}
      <Feedback runId={result.run_id} />
    </section>
  );
}

/** Upload one document and review the proposal. */
export function BillReader({ moveType, moveId, onDone }: { moveType?: string; moveId?: number; onDone?: () => void }) {
  const feature = useAiFeature(FEATURE);
  const navigate = useNavigate();
  const inputId = useId();
  const [result, setResult] = useState<ReadResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!feature.enabled) return null;
  const read = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await accountAi.readDocument(file, { moveType, moveId }));
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="bill-reader">
      <label htmlFor={inputId} className="btn small">
        {t("Read a document (PDF or image)")}
      </label>
      <input
        id={inputId}
        type="file"
        accept="application/pdf,image/png,image/jpeg,image/webp"
        className="sr-only"
        disabled={busy}
        onChange={(e) => void read(e.target.files?.[0])}
      />
      {!feature.available && <AiUnavailable />}
      {busy && (
        <p role="status" className="muted small">
          {t("Reading the document…")}
        </p>
      )}
      <ErrorNote message={error} />
      {result?.data && (
        <BillReview
          result={result}
          onAccepted={(id) => {
            onDone?.();
            if (id && !moveId) navigate(`/m/${MOVE}/${id}`);
          }}
        />
      )}
      {result && !result.data && <p className="muted small">{t("Nothing could be read from this document.")}</p>}
    </div>
  );
}

/** The client action: "Read from documents" under Customers and Vendors (the action's context gives the type). */
export function BillImportPage() {
  const { actionRef = "" } = useParams();
  const action = useQuery({
    queryKey: ["action", actionRef],
    queryFn: () => api.action(actionRef),
    enabled: !!actionRef,
  });
  const moveType = (action.data?.context as Record<string, unknown> | undefined)?.move_type;
  const feature = useAiFeature(FEATURE);
  return (
    <div className="page bill-import">
      <h1>{moveType === "out_invoice" ? t("Read invoices from documents") : t("Read bills from documents")}</h1>
      {!feature.enabled && !feature.loading ? (
        <p className="muted">{t("Document reading is turned off for this company.")}</p>
      ) : (
        <>
          <p className="muted">
            {t(
              "Upload a PDF or a scan. The partner, the lines, their accounts and taxes are proposed for review; the draft is created only when you accept.",
            )}
          </p>
          <BillReader moveType={typeof moveType === "string" ? moveType : undefined} />
        </>
      )}
    </div>
  );
}

registerClientAction("account.bill_import", () => <BillImportPage />);

registerSearchAddon(MOVE, {
  key: FEATURE,
  render: () => <BillReader />,
});

registerFormPanel(MOVE, {
  get title() {
    return t("Read from a document");
  },
  render: ({ resId, record, reload }) =>
    record.state === "draft" &&
    ["in_invoice", "in_refund", "out_invoice", "out_refund"].includes(String(record.move_type)) &&
    !(Array.isArray(record.invoice_line_ids) && record.invoice_line_ids.length) ? (
      <BillReader moveId={resId} moveType={String(record.move_type)} onDone={reload} />
    ) : (
      <span className="ai-panel-hidden" hidden />
    ),
});
