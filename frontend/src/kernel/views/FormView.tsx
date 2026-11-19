import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useId, useMemo, useState } from "react";
import { api, ApiError, CLIENT_ID } from "../api/client";
import type { FieldMeta, FieldsMeta, RecordData, ViewDef, ViewNode, ViewsResponse } from "../api/types";
import { evaluate } from "../domain";
import { FieldInput } from "../fields/FieldInput";
import { useHotkeys } from "../hotkeys";
import { reports } from "../api/platform";
import { Chatter } from "../chatter/Chatter";
import { formPanelsFor } from "../extensions";
import { useRealtime, type RealtimeEvent } from "../realtime";
import { useSession } from "../session";
import { StatusBar } from "./StatusBar";
import { ButtonDialog } from "./ButtonDialog";
import { Icon, initials, menuIcon } from "../icons";
import { AccessDenied, isAccessError } from "../shell/AccessDenied";
import { displayValue } from "../format";
import { t } from "../i18n";

interface Props {
  model: string;
  recordId: number | null;
  meta: ViewsResponse;
  view: ViewDef;
  defaults?: RecordData;
  onSaved: (id: number) => void;
  onBack: () => void;
}

const SYSTEM = new Set(["id", "display_name", "create_date", "write_date", "create_uid", "write_uid"]);

/** Names of every field referenced by a view tree. */
export function collectFields(node: ViewNode | ViewNode[] | undefined, out: Set<string> = new Set()): Set<string> {
  if (!node) return out;
  if (Array.isArray(node)) {
    node.forEach((n) => collectFields(n, out));
    return out;
  }
  if (node.kind === "field" && node.name) out.add(node.name);
  if (node.field) out.add(node.field);
  for (const key of ["children", "pages"] as const) if (Array.isArray(node[key])) collectFields(node[key], out);
  return out;
}

/** Always required, or required while the record matches the field's `required_if` condition. */
export function isRequired(field: FieldMeta, record: RecordData): boolean {
  return field.required || (!!field.required_if && evaluate(field.required_if, record));
}

/** Convert a read() value to the shape the API accepts on write. */
function toWrite(fields: FieldsMeta, name: string, value: any): any {
  const f = fields[name];
  if (!f) return value;
  if (f.type === "many2one") return value ? value.id : null;
  if (f.type === "many2many" || f.type === "one2many") return [["set", Array.isArray(value) ? value : []]];
  return value;
}

const AMOUNT_FIELDS = ["amount_total", "expected_revenue", "amount_residual", "recurring_monthly", "amount"];
const TYPE_FIELDS = ["move_type", "type"];
const STATUS_FIELDS = ["state", "payment_state", "subscription_state", "invoice_status"];

/** Up to three facts for the record header: amount, owner, status. */
function headerFacts(
  fields: FieldsMeta,
  names: string[],
  record: RecordData,
): { label: string; value: React.ReactNode }[] {
  const out: { label: string; value: React.ReactNode }[] = [];
  const amount = AMOUNT_FIELDS.find((n) => names.includes(n) && fields[n]?.type === "monetary");
  if (amount && record[amount] !== null && record[amount] !== undefined)
    out.push({ label: fields[amount].string, value: displayValue(fields[amount], record[amount], record) });
  const owner = fields.user_id;
  if (names.includes("user_id") && owner?.type === "many2one" && record.user_id?.display_name)
    out.push({
      label: owner.string,
      value: (
        <>
          <span className="avatar small" aria-hidden="true">
            {initials(record.user_id.display_name)}
          </span>
          {record.user_id.display_name}
        </>
      ),
    });
  const status = STATUS_FIELDS.find((n) => names.includes(n) && fields[n]?.type === "selection" && record[n]);
  if (status)
    out.push({
      label: fields[status].string,
      value: <span className={`badge badge-${record[status]}`}>{displayValue(fields[status], record[status])}</span>,
    });
  return out;
}

export function FormView({ model, recordId, meta, view, defaults, onSaved, onBack }: Props) {
  const queryClient = useQueryClient();
  const { session } = useSession();
  const modules = new Set(session?.modules ?? []);
  const printable = useQuery({
    queryKey: ["reports", model],
    queryFn: () => reports.forModel(model),
    enabled: recordId !== null && modules.has("reports"),
  });
  const fieldNames = useMemo(() => {
    const names = collectFields(view.root);
    for (const f of Object.values(meta.fields)) if (f.type === "monetary") names.add(f.currency_field ?? "currency_id");
    names.add("display_name");
    return [...names].filter((n) => n in meta.fields);
  }, [view, meta.fields]);

  const loaded = useQuery({
    queryKey: ["record", model, recordId, fieldNames.join(",")],
    queryFn: () => api.getWithEtag(model, recordId as number, fieldNames),
    enabled: recordId !== null,
  });

  const [changes, setChanges] = useState<RecordData>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<{ kind: "error" | "info" | "conflict"; text: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const formId = useId();

  useEffect(() => {
    setChanges(recordId === null ? { ...(defaults ?? {}) } : {});
    setErrors({});
    setMessage(null);
  }, [recordId, model, defaults]);

  const base: RecordData = recordId === null ? {} : (loaded.data?.record ?? {});
  const record: RecordData = { ...base, ...changes };
  const dirty = Object.keys(changes).length > 0;
  const canWrite = recordId === null ? meta.model.access.create : meta.model.access.write;

  const onRemote = useCallback(
    (event: RealtimeEvent) => {
      if (event.data.client === CLIENT_ID) return; // our own save
      if (event.event === "unlink") {
        setMessage({ kind: "info", text: t("This record was deleted by someone else.") });
        return;
      }
      if (dirty)
        setMessage({
          kind: "conflict",
          text: t("Someone else changed this record. Saving will be refused until you reload."),
        });
      else void loaded.refetch();
    },
    [dirty, loaded],
  );
  useRealtime(recordId ? `record:${model}:${recordId}` : null, onRemote);

  const setValue = (name: string, value: any) => {
    setChanges((c) => ({ ...c, [name]: value }));
    setErrors((e) => {
      if (!(name in e)) return e;
      const rest = { ...e };
      delete rest[name];
      return rest;
    });
  };

  const validate = (): boolean => {
    const missing: Record<string, string> = {};
    for (const name of fieldNames) {
      const f = meta.fields[name];
      if (!f || !isRequired(f, record) || f.type === "boolean" || SYSTEM.has(name)) continue;
      const v = record[name];
      if (v === null || v === undefined || v === "" || (Array.isArray(v) && v.length === 0 && f.type !== "one2many"))
        missing[name] = t("{field} is required", { field: f.string });
    }
    setErrors(missing);
    return Object.keys(missing).length === 0;
  };

  const save = async (): Promise<number | null> => {
    if (!dirty && recordId !== null) return recordId;
    if (!validate()) {
      setMessage({ kind: "error", text: t("Some required fields are empty.") });
      return null;
    }
    setSaving(true);
    try {
      const values = Object.fromEntries(Object.entries(changes).map(([k, v]) => [k, toWrite(meta.fields, k, v)]));
      const id = recordId === null ? Number((await api.create(model, values, ["id"])).id) : recordId;
      if (recordId !== null) await api.update(model, recordId, values, { fields: ["id"], etag: loaded.data?.etag });
      setChanges({});
      setMessage(null);
      await queryClient.invalidateQueries({ queryKey: ["list", model] });
      await queryClient.invalidateQueries({ queryKey: ["record", model, id] });
      onSaved(id);
      return id;
    } catch (e) {
      if (e instanceof ApiError && e.isConflict) setMessage({ kind: "conflict", text: e.message });
      else setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
      return null;
    } finally {
      setSaving(false);
    }
  };

  const discard = () => {
    setChanges(recordId === null ? { ...(defaults ?? {}) } : {});
    setErrors({});
    setMessage(null);
    if (recordId === null) onBack();
  };

  const [pending, setPending] = useState<ViewNode | null>(null);
  const callMethod = async (method: string, kwargs: Record<string, unknown> = {}) => {
    const id = await save();
    if (!id) return;
    try {
      await api.call(model, id, method, [], kwargs);
      await loaded.refetch();
      await queryClient.invalidateQueries({ queryKey: ["list", model] });
    } catch (e) {
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    }
  };

  const remove = async () => {
    if (recordId === null || !window.confirm(t("Delete this record?"))) return;
    try {
      await api.remove(model, recordId);
      await queryClient.invalidateQueries({ queryKey: ["list", model] });
      onBack();
    } catch (e) {
      setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    }
  };

  useHotkeys({ "mod+s": () => void save(), escape: () => dirty && discard(), "alt+arrowleft": onBack });

  if (recordId !== null && loaded.isLoading)
    return (
      <div className="loading" role="status">
        {t("Loading…")}
      </div>
    );
  if (recordId !== null && isAccessError(loaded.error)) return <AccessDenied />;
  if (recordId !== null && loaded.isError)
    return (
      <div className="alert error" role="alert">
        {(loaded.error as Error).message}
      </div>
    );

  const render = (node: ViewNode, key: string): React.ReactNode => {
    if (node.invisible !== undefined && evaluate(node.invisible, record)) return null;
    const children = (list?: ViewNode[]) => list?.map((child, i) => render(child, `${key}.${i}`));
    switch (node.kind) {
      case "sheet": {
        // one card for the fields, then one card per notebook
        const kids = (node.children ?? []).map((child, i) => ({ child, i }));
        const main = kids.filter(({ child }) => child.kind !== "notebook");
        const books = kids.filter(({ child }) => child.kind === "notebook");
        return (
          <div key={key} className="sheet">
            {main.length > 0 && (
              <div className="sheet-card">{main.map(({ child, i }) => render(child, `${key}.${i}`))}</div>
            )}
            {books.map(({ child, i }) => render(child, `${key}.${i}`))}
          </div>
        );
      }
      case "group": {
        const cols = typeof node.columns === "number" ? node.columns : 1;
        return (
          <div key={key} className={`form-group cols-${cols}`} role="group" aria-label={node.title}>
            {node.title && <h3 className="group-title">{node.title}</h3>}
            {children(node.children)}
          </div>
        );
      }
      case "notebook":
        return (
          <Notebook
            key={key}
            pages={node.pages ?? []}
            renderPage={(page, i) => children(page.children ?? []) ?? `${i}`}
          />
        );
      case "buttons":
        return (
          <div key={key} className="button-row">
            {children(node.children)}
          </div>
        );
      case "button":
        return (
          <button
            key={key}
            type="button"
            className="btn"
            onClick={() => {
              if (!node.method) return;
              if (node.params?.length || node.confirm) setPending(node);
              else void callMethod(node.method);
            }}
            disabled={saving}
          >
            {node.label ?? node.method}
          </button>
        );
      case "label":
        return (
          <p key={key} className="form-label-text">
            {node.text}
          </p>
        );
      case "separator":
        return <hr key={key} />;
      case "field":
        return renderField(node, key);
      default:
        return <div key={key}>{children(node.children)}</div>;
    }
  };

  const renderField = (node: ViewNode, key: string) => {
    const name = node.name ?? "";
    const field = meta.fields[name];
    if (!field) return null;
    const id = `${formId}-${name}`;
    const readonly =
      !canWrite ||
      node.readonly === true ||
      (field.readonly && !(recordId === null && !field.compute)) ||
      evaluate(node.readonly_if ?? field.readonly_if, record);
    const required = isRequired(field, record);
    const isTitle = node.widget === "title";
    return (
      <div
        key={key}
        className={`form-field${isTitle ? " title-field" : ""}${field.type === "one2many" || field.type === "html" ? " wide" : ""}`}
      >
        <label htmlFor={id}>
          {node.label ?? field.string}
          {required && !readonly && (
            <span aria-hidden="true" className="req">
              *
            </span>
          )}
        </label>
        <FieldInput
          field={required === field.required ? field : { ...field, required }}
          value={record[name]}
          record={record}
          onChange={(v) => setValue(name, v)}
          readonly={readonly}
          node={node}
          id={id}
          invalid={!!errors[name]}
        />
        {errors[name] && (
          <span className="field-error" role="alert">
            {errors[name]}
          </span>
        )}
        {field.help && !isTitle && <span className="help">{field.help}</span>}
      </div>
    );
  };

  const showChatter = recordId !== null && !!view.chatter && modules.has("chatter");
  // the sheet's own buttons go to the command bar and its stage pickers to the process bar
  const top = view.root?.kind === "sheet" ? (view.root.children ?? []) : [];
  const isProcess = (n: ViewNode) => n.kind === "field" && n.widget === "statusbar";
  const commandNodes = top.filter((n) => n.kind === "buttons");
  const processNodes = top.filter(isProcess);
  const body: ViewNode | undefined =
    view.root?.kind === "sheet"
      ? { ...view.root, children: top.filter((n) => n.kind !== "buttons" && !isProcess(n)) }
      : view.root;
  const facts = recordId !== null ? headerFacts(meta.fields, fieldNames, record) : [];
  const kind = TYPE_FIELDS.find((n) => fieldNames.includes(n) && meta.fields[n]?.type === "selection" && record[n]);
  const typeLabel = kind ? displayValue(meta.fields[kind], record[kind]) : meta.model.description;
  const title =
    recordId === null ? t("New {model}", { model: meta.model.description }) : String(record.display_name ?? "");
  const formDomId = `${formId}-form`;
  const head = (
    <>
      <div className="form-toolbar">
        <button type="button" className="btn ghost" onClick={onBack} aria-label={t("Back to list")}>
          <Icon name="arrow-left" />
          {t("Back")}
        </button>
        <span className="cmd-sep" aria-hidden="true" />
        {canWrite && (
          <>
            <button
              type="submit"
              form={formDomId}
              className="btn primary"
              disabled={saving || (!dirty && recordId !== null)}
              title={t("Save (Ctrl/⌘+S)")}
            >
              <Icon name="save" />
              {saving ? t("Saving…") : t("Save")}
            </button>
            <button type="button" className="btn ghost" onClick={discard} disabled={!dirty} title={t("Discard (Esc)")}>
              <Icon name="undo" />
              {t("Discard")}
            </button>
          </>
        )}
        {commandNodes.map((n, i) => render(n, `cmd.${i}`))}
        {recordId !== null && (printable.data?.length ?? 0) > 0 && (
          <PrintMenu templates={printable.data!} recordId={recordId} />
        )}
        {recordId !== null && meta.model.access.unlink && (
          <button type="button" className="btn danger ghost" onClick={remove}>
            <Icon name="trash" />
            {t("Delete")}
          </button>
        )}
      </div>
      <header className="record-header">
        <div className="rh-main">
          <span className="app-glyph" aria-hidden="true">
            <Icon name={menuIcon({ action: model })} size={20} />
          </span>
          <div>
            <div className="rh-type">{typeLabel}</div>
            <h1 className="rh-name" aria-live="polite">
              {title}
              {dirty && (
                <span className="dirty" title={t("Unsaved changes")}>
                  {" "}
                  •
                </span>
              )}
            </h1>
          </div>
        </div>
        {facts.length > 0 && (
          <dl className="rh-facts">
            {facts.map((f) => (
              <div key={f.label} className="rh-fact">
                <dt>{f.label}</dt>
                <dd>{f.value}</dd>
              </div>
            ))}
          </dl>
        )}
      </header>
      {processNodes.length > 0 && (
        <div className="process-bar">{processNodes.map((n, i) => render(n, `process.${i}`))}</div>
      )}
      {recordId !== null && modules.has("workflow") && (
        <StatusBar
          model={model}
          resId={recordId}
          onChanged={() => {
            void loaded.refetch();
            void queryClient.invalidateQueries({ queryKey: ["list", model] });
          }}
        />
      )}
    </>
  );
  const form = (
    <form
      id={formDomId}
      className="form-view"
      aria-label={
        record.display_name ? String(record.display_name) : t("New {model}", { model: meta.model.description })
      }
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      {message && (
        <div className={`alert ${message.kind}`} role="alert">
          {message.text}
          {message.kind === "conflict" && (
            <button
              type="button"
              className="btn small"
              onClick={() => {
                setChanges({});
                setMessage(null);
                void loaded.refetch();
              }}
            >
              {t("Reload")}
            </button>
          )}
        </div>
      )}
      {body ? render(body, "root") : null}
      {pending && (
        <ButtonDialog
          node={pending}
          onCancel={() => setPending(null)}
          onSubmit={(kwargs) => {
            const method = pending.method as string;
            setPending(null);
            void callMethod(method, kwargs);
          }}
        />
      )}
    </form>
  );
  const panels = recordId !== null ? formPanelsFor(model) : [];
  const side = panels.map((p) => (
    <section key={p.title} className="form-panel" aria-label={p.title}>
      <h2 className="form-panel-title">{p.title}</h2>
      {p.render({ model, resId: recordId as number, record, reload: () => void loaded.refetch() })}
    </section>
  ));
  return (
    <div className="record-page">
      {head}
      {!showChatter && !side.length ? (
        form
      ) : (
        <div className="form-with-chatter">
          {form}
          <div className="form-side">
            {side}
            {showChatter && <Chatter model={model} resId={recordId as number} canWrite={meta.model.access.write} />}
          </div>
        </div>
      )}
    </div>
  );
}

function PrintMenu({ templates, recordId }: { templates: { id: number; name: string }[]; recordId: number }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="dropdown">
      <button
        type="button"
        className="btn ghost"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <Icon name="printer" />
        {t("Print")}
      </button>
      {open && (
        <div className="dropdown-menu right" role="menu">
          {templates.map((tpl) => (
            <a
              key={tpl.id}
              role="menuitem"
              className="btn ghost block"
              href={reports.url(tpl.id, [recordId])}
              target="_blank"
              rel="noreferrer"
            >
              {tpl.name}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

function Notebook({
  pages,
  renderPage,
}: {
  pages: ViewNode[];
  renderPage: (page: ViewNode, i: number) => React.ReactNode;
}) {
  const [active, setActive] = useState(0);
  const baseId = useId();
  return (
    <div className="notebook">
      <div role="tablist" className="tabs">
        {pages.map((page, i) => (
          <button
            key={i}
            type="button"
            role="tab"
            id={`${baseId}-tab-${i}`}
            aria-selected={i === active}
            aria-controls={`${baseId}-panel-${i}`}
            tabIndex={i === active ? 0 : -1}
            className={i === active ? "tab active" : "tab"}
            onClick={() => setActive(i)}
            onKeyDown={(e) => {
              if (e.key === "ArrowRight") setActive((a) => (a + 1) % pages.length);
              if (e.key === "ArrowLeft") setActive((a) => (a - 1 + pages.length) % pages.length);
            }}
          >
            {page.title ?? page.name ?? t("Page {number}", { number: i + 1 })}
          </button>
        ))}
      </div>
      {pages.map((page, i) => (
        <div
          key={i}
          role="tabpanel"
          id={`${baseId}-panel-${i}`}
          aria-labelledby={`${baseId}-tab-${i}`}
          hidden={i !== active}
          className="tab-panel"
        >
          {renderPage(page, i)}
        </div>
      ))}
    </div>
  );
}
