import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, request } from "../../kernel/api/client";
import { registerClientAction } from "../../kernel/extensions";
import { formatMonetary } from "../../kernel/format";
import { t } from "../../kernel/i18n";

const LINE_MODEL = "account.bank_statement_line";

export interface BankLine {
  id: number;
  date: string | null;
  payment_ref: string | null;
  ref: string | null;
  partner: { id: number; name: string } | null;
  partner_name: string | null;
  amount: number;
  amount_residual: number;
  currency: string | null;
  state: string;
  journal: string;
  statement: string | null;
  rule: string | null;
}

interface Counterpart {
  id: number;
  account: string;
  label: string | null;
  partner: string | null;
  balance: number;
  suspense: boolean;
  matched: { id: number; move: string; move_id: number }[];
}

interface LineInfo extends BankLine {
  narration: string | null;
  counterparts: Counterpart[];
}

export interface OpenItem {
  id: number;
  move_name: string;
  kind: string;
  ref: string | null;
  date: string | null;
  date_maturity: string | null;
  partner: string | null;
  account: string;
  amount_residual: number;
  amount_residual_currency: number;
  currency: string;
}

export interface Candidate {
  kind: "items" | "writeoff";
  line_ids: number[];
  items: OpenItem[];
  amount: number;
  score: number;
  reasons: { code: string; message: string }[];
  model: { id: number; name: string } | null;
  writeoffs: { account_id: number; balance: number; label: string; tax_id: number | null }[];
}

const call = async <T,>(id: number | null, method: string, kwargs: Record<string, unknown> = {}) =>
  (await api.call(LINE_MODEL, id, method, [], kwargs)).result as T;

function stateLabel(state: string): string {
  const labels: Record<string, string> = {
    draft: t("Draft"),
    posted: t("To reconcile"),
    reconciled: t("Reconciled"),
  };
  return labels[state] ?? state;
}

/** Bank reconciliation: transactions on the left, suggestions, search and write-off on the right. */
export function BankReconcile() {
  const queryClient = useQueryClient();
  const [journalId, setJournalId] = useState<number | "">("");
  const [show, setShow] = useState<"open" | "reconciled" | "all">("open");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const [message, setMessage] = useState<{ kind: "info" | "error"; text: string } | null>(null);
  const [importing, setImporting] = useState(false);

  const journals = useQuery({
    queryKey: ["bank-journals"],
    queryFn: async () =>
      (
        await api.list("account.journal", {
          domain: [["type", "in", ["bank", "cash"]]],
          fields: ["id", "display_name"],
          count: false,
        })
      ).items as { id: number; display_name: string }[],
  });
  const lines = useQuery({
    queryKey: ["bank-lines", journalId, show, query],
    queryFn: () =>
      call<BankLine[]>(null, "get_reconcile_lines", {
        journal_id: journalId || null,
        show,
        query,
      }),
  });

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["bank-lines"] });
    await queryClient.invalidateQueries({ queryKey: ["bank-line"] });
  };

  const run = async (action: () => Promise<unknown>, done: string) => {
    setMessage(null);
    try {
      const result = await action();
      setMessage({ kind: "info", text: typeof result === "string" ? result : done });
      await refresh();
    } catch (e) {
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    }
  };

  const autoReconcile = () =>
    run(async () => {
      const res = await call<{ lines: number; reconciled: number }>(null, "action_auto_reconcile_all", {
        journal_id: journalId || null,
      });
      return t("{reconciled} of {lines} transactions reconciled by the rules", res);
    }, t("Rules applied"));

  const importFile = async (file: File) => {
    if (!journalId) {
      setMessage({ kind: "error", text: t("Choose the bank journal first") });
      return;
    }
    setImporting(true);
    setMessage(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const attachment = await request<{ id: number }>("POST", "/attachments", { body: form });
      const res = await request<{ lines: number; duplicates: number; reconciled: number }>(
        "POST",
        "/account_bank/import",
        { body: { journal_id: journalId, attachment_id: attachment.id } },
      );
      setMessage({
        kind: "info",
        text: t(
          "{lines} transactions imported, {duplicates} already imported, {reconciled} reconciled automatically",
          res,
        ),
      });
      await refresh();
    } catch (e) {
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setImporting(false);
    }
  };

  return (
    <div className="bank-reconcile">
      <h1 className="page-title">{t("Bank reconciliation")}</h1>
      <div className="toolbar" role="toolbar" aria-label={t("Bank reconciliation")}>
        <label>
          {t("Journal")}{" "}
          <select
            className="input"
            value={journalId}
            onChange={(e) => setJournalId(e.target.value ? Number(e.target.value) : "")}
          >
            <option value="">{t("All bank journals")}</option>
            {(journals.data ?? []).map((j) => (
              <option key={j.id} value={j.id}>
                {j.display_name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("Show")}{" "}
          <select className="input" value={show} onChange={(e) => setShow(e.target.value as typeof show)}>
            <option value="open">{t("To reconcile")}</option>
            <option value="reconciled">{t("Reconciled")}</option>
            <option value="all">{t("All")}</option>
          </select>
        </label>
        <input
          className="input"
          type="search"
          placeholder={t("Search transactions")}
          aria-label={t("Search transactions")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button type="button" className="btn" onClick={() => void autoReconcile()}>
          {t("Auto-reconcile")}
        </button>
        <label className="btn ghost">
          {importing ? t("Importing…") : t("Import a statement")}
          <input
            type="file"
            hidden
            aria-label={t("Statement file")}
            accept=".csv,.txt,.ofx,.qfx,.xml,.sta,.940,.mt940"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void importFile(file);
              e.target.value = "";
            }}
          />
        </label>
      </div>
      {message && (
        <div
          className={`alert ${message.kind === "error" ? "error" : "info"}`}
          role={message.kind === "error" ? "alert" : "status"}
        >
          {message.text}
        </div>
      )}
      <div className="bank-reconcile-panes">
        <section aria-labelledby="bank-lines-h" className="bank-lines">
          <h2 id="bank-lines-h">{t("Transactions")}</h2>
          {lines.isLoading && <p className="muted">{t("Loading…")}</p>}
          {lines.data && lines.data.length === 0 && <p className="muted">{t("Nothing to reconcile.")}</p>}
          <ul className="bank-line-list">
            {(lines.data ?? []).map((line) => (
              <li key={line.id}>
                <button
                  type="button"
                  className={`bank-line ${selected === line.id ? "active" : ""}`}
                  aria-pressed={selected === line.id}
                  onClick={() => setSelected(line.id)}
                >
                  <span className="date">{line.date}</span>
                  <span className="label">{line.payment_ref || line.partner_name || "—"}</span>
                  <span className="partner muted">{line.partner?.name ?? line.partner_name ?? ""}</span>
                  <span className={`num ${line.amount < 0 ? "negative" : ""}`}>
                    {formatMonetary(line.amount, line.currency)}
                  </span>
                  <span className={`badge state-${line.state}`}>{stateLabel(line.state)}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
        <section aria-labelledby="bank-match-h" className="bank-match">
          <h2 id="bank-match-h">{t("Match")}</h2>
          {selected === null ? (
            <p className="muted">{t("Select a transaction to see matching suggestions.")}</p>
          ) : (
            <LinePanel key={selected} lineId={selected} run={run} />
          )}
        </section>
      </div>
    </div>
  );
}

function LinePanel({
  lineId,
  run,
}: {
  lineId: number;
  run: (action: () => Promise<unknown>, done: string) => Promise<void>;
}) {
  const info = useQuery({
    queryKey: ["bank-line", lineId],
    queryFn: () => call<LineInfo>(lineId, "get_reconcile_info"),
  });
  const suggestions = useQuery({
    queryKey: ["bank-line", lineId, "suggestions"],
    queryFn: () => call<Candidate[]>(lineId, "get_suggestions", { limit: 5 }),
  });
  const [search, setSearch] = useState("");
  const found = useQuery({
    queryKey: ["bank-line", lineId, "search", search],
    queryFn: () => call<OpenItem[]>(lineId, "search_candidates", { query: search, limit: 20 }),
  });
  const [picked, setPicked] = useState<Record<number, string>>({});
  const [writeoff, setWriteoff] = useState<{ account: string; label: string; tax: string }>({
    account: "",
    label: "",
    tax: "",
  });
  const accounts = useQuery({
    queryKey: ["bank-writeoff-accounts", writeoff.account],
    queryFn: () => api.nameSearch("account.account", writeoff.account, [["deprecated", "=", false]], 8),
    enabled: writeoff.account.length > 0,
  });
  const [accountId, setAccountId] = useState<number | null>(null);
  const [taxId, setTaxId] = useState<number | null>(null);
  const taxes = useQuery({
    queryKey: ["bank-writeoff-taxes", writeoff.tax],
    queryFn: () => api.nameSearch("account.tax", writeoff.tax, undefined, 8),
    enabled: writeoff.tax.length > 0,
  });

  if (info.isLoading || !info.data) return <p className="muted">{t("Loading…")}</p>;
  const line = info.data;
  const matched = line.counterparts.some((c) => !c.suspense);
  const currency = line.currency;

  const matchItems = (items: { line_id: number; amount?: number }[]) =>
    run(() => call(lineId, "action_match", { items }), t("Transaction matched"));

  return (
    <div className="bank-line-panel">
      <dl className="bank-line-summary">
        <dt>{t("Label")}</dt>
        <dd>{line.payment_ref}</dd>
        <dt>{t("Partner")}</dt>
        <dd>{line.partner?.name ?? line.partner_name ?? "—"}</dd>
        <dt>{t("Amount")}</dt>
        <dd>{formatMonetary(line.amount, currency)}</dd>
        <dt>{t("Left to reconcile")}</dt>
        <dd>{formatMonetary(line.amount_residual, currency)}</dd>
      </dl>
      {line.narration && <p className="muted small">{line.narration}</p>}
      {matched && (
        <div className="bank-current">
          <h3>{t("Current matches")}</h3>
          <ul>
            {line.counterparts
              .filter((c) => !c.suspense)
              .map((c) => (
                <li key={c.id}>
                  {c.account} — {c.matched.map((m) => m.move).join(", ") || c.label} ·{" "}
                  {formatMonetary(-c.balance, currency)}
                </li>
              ))}
          </ul>
          <button
            type="button"
            className="btn danger small"
            onClick={() => void run(() => call(lineId, "action_unmatch"), t("Matches undone"))}
          >
            {t("Undo")}
          </button>
        </div>
      )}
      {line.state !== "reconciled" && (
        <>
          <h3>{t("Suggestions")}</h3>
          {suggestions.data && suggestions.data.length === 0 && <p className="muted">{t("No suggestion.")}</p>}
          <ul className="bank-suggestions">
            {(suggestions.data ?? []).map((c, i) => (
              <li key={i} className="bank-suggestion">
                <div>
                  <strong>
                    {c.kind === "writeoff"
                      ? (c.model?.name ?? t("Write-off"))
                      : c.items.map((item) => item.move_name).join(", ")}
                  </strong>{" "}
                  <span className="badge" title={t("Confidence")}>
                    {Math.round(c.score * 100)} %
                  </span>
                  {c.kind === "items" && <span className="muted"> · {formatMonetary(c.amount, currency)}</span>}
                </div>
                <div className="muted small">{c.reasons.map((r) => r.message).join(" · ")}</div>
                <button
                  type="button"
                  className="btn primary small"
                  onClick={() =>
                    void run(
                      () =>
                        call(lineId, "action_match", {
                          items: c.line_ids.map((id) => ({ line_id: id })),
                          writeoffs: c.writeoffs,
                        }),
                      t("Transaction matched"),
                    )
                  }
                >
                  {t("Accept")}
                </button>
              </li>
            ))}
          </ul>
          <h3>{t("Find open items")}</h3>
          <input
            className="input"
            type="search"
            aria-label={t("Search open items")}
            placeholder={t("Number, reference, partner or amount")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <table className="pivot-table bank-items">
            <thead>
              <tr>
                <th scope="col">{t("Select")}</th>
                <th scope="col">{t("Document")}</th>
                <th scope="col">{t("Partner")}</th>
                <th scope="col">{t("Due date")}</th>
                <th scope="col" className="num">
                  {t("Open amount")}
                </th>
                <th scope="col">{t("Amount to match")}</th>
              </tr>
            </thead>
            <tbody>
              {(found.data ?? []).map((item) => (
                <tr key={item.id}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={t("Select {name}", { name: item.move_name })}
                      checked={item.id in picked}
                      onChange={(e) =>
                        setPicked((p) => {
                          const next = { ...p };
                          if (e.target.checked) next[item.id] = "";
                          else delete next[item.id];
                          return next;
                        })
                      }
                    />
                  </td>
                  <th scope="row">{item.move_name}</th>
                  <td>{item.partner}</td>
                  <td>{item.date_maturity}</td>
                  <td className="num">{formatMonetary(item.amount_residual_currency, item.currency)}</td>
                  <td>
                    <input
                      className="input small"
                      type="number"
                      step="0.01"
                      aria-label={t("Amount to match for {name}", { name: item.move_name })}
                      placeholder={t("all")}
                      disabled={!(item.id in picked)}
                      value={picked[item.id] ?? ""}
                      onChange={(e) => setPicked((p) => ({ ...p, [item.id]: e.target.value }))}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <button
            type="button"
            className="btn primary"
            disabled={Object.keys(picked).length === 0}
            onClick={() =>
              void matchItems(
                Object.entries(picked).map(([id, amount]) =>
                  amount ? { line_id: Number(id), amount: Number(amount) } : { line_id: Number(id) },
                ),
              )
            }
          >
            {t("Match selected items")}
          </button>
          <h3>{t("Write off")}</h3>
          <div className="bank-writeoff">
            <label>
              {t("Account")}
              <input
                className="input"
                list={`bank-accounts-${lineId}`}
                value={writeoff.account}
                onChange={(e) => {
                  const value = e.target.value;
                  setWriteoff((w) => ({ ...w, account: value }));
                  const hit = (accounts.data ?? []).find((a) => a.display_name === value);
                  setAccountId(hit ? hit.id : null);
                }}
              />
              <datalist id={`bank-accounts-${lineId}`}>
                {(accounts.data ?? []).map((a) => (
                  <option key={a.id} value={a.display_name} />
                ))}
              </datalist>
            </label>
            <label>
              {t("Label")}
              <input
                className="input"
                value={writeoff.label}
                onChange={(e) => setWriteoff((w) => ({ ...w, label: e.target.value }))}
              />
            </label>
            <label>
              {t("Tax (optional)")}
              <input
                className="input"
                list={`bank-taxes-${lineId}`}
                value={writeoff.tax}
                onChange={(e) => {
                  const value = e.target.value;
                  setWriteoff((w) => ({ ...w, tax: value }));
                  const hit = (taxes.data ?? []).find((x) => x.display_name === value);
                  setTaxId(hit ? hit.id : null);
                }}
              />
              <datalist id={`bank-taxes-${lineId}`}>
                {(taxes.data ?? []).map((x) => (
                  <option key={x.id} value={x.display_name} />
                ))}
              </datalist>
            </label>
            <button
              type="button"
              className="btn"
              disabled={accountId === null}
              onClick={() =>
                void run(
                  () =>
                    call(lineId, "action_match", {
                      writeoffs: [{ account_id: accountId, label: writeoff.label || null, tax_id: taxId }],
                    }),
                  t("Written off"),
                )
              }
            >
              {t("Write off the rest")}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

registerClientAction("account_bank.reconcile", () => <BankReconcile />);
