// Workflow builder: the states of a record (in order, folded or final) and the transitions between
// them (label, from → to, optional condition and group), on a choice or link field of the model or on
// a new choice field. Saved as one Studio change through the workflow models.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../kernel/api/client";
import type { Domain, FieldsMeta } from "../../kernel/api/types";
import { t } from "../../kernel/i18n";
import { ConditionEditor, ConditionSummary } from "../../kernel/views/ConditionEditor";
import {
  errorText,
  slugify,
  studio,
  type Group,
  type WorkflowInfo,
  type WorkflowState,
  type WorkflowTransition,
} from "./api";
import { OrderedList } from "./OrderedList";

const NEW = "__new__";

interface StateRow extends WorkflowState {
  id: number;
}
interface TransitionRow extends WorkflowTransition {
  id: number;
}

let counter = 0;
const nextId = () => ++counter;

/** States in order, each with the transitions leaving it (a transition without origin leaves every state). */
export function WorkflowDiagram({
  states,
  transitions,
  fields,
  groups,
}: {
  states: WorkflowState[];
  transitions: WorkflowTransition[];
  fields: FieldsMeta;
  groups: Group[];
}) {
  const nameOf = (key: string) => states.find((s) => s.key === key)?.name ?? key;
  const groupName = (x: string) => groups.find((g) => g.xmlid === x)?.name ?? x;
  return (
    <ol className="workflow-diagram" aria-label={t("Workflow overview")}>
      {states.map((s) => {
        const leaving = transitions.filter((tr) => !tr.from.length || tr.from.includes(s.key));
        return (
          <li key={s.key}>
            <strong>{s.name || s.key}</strong>
            {s.done && <span className="badge">{t("done")}</span>}
            {s.fold && <span className="badge">{t("folded")}</span>}
            {leaving.length > 0 && (
              <ul>
                {leaving.map((tr, i) => (
                  <li key={i}>
                    <span aria-hidden="true">→ </span>
                    {t("{action} to {state}", { action: tr.name || "…", state: nameOf(tr.to) })}
                    {tr.condition ? (
                      <span className="muted small">
                        {" "}
                        · {t("only when")} <ConditionSummary fields={fields} domain={tr.condition} />
                      </span>
                    ) : null}
                    {tr.groups?.length ? (
                      <span className="muted small">
                        {" "}
                        · {t("for {groups}", { groups: tr.groups.map(groupName).join(", ") })}
                      </span>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </li>
        );
      })}
    </ol>
  );
}

export function WorkflowBuilder({ model, fields, onDone }: { model: string; fields: FieldsMeta; onDone: () => void }) {
  const current = useQuery({ queryKey: ["studio", "workflow", model], queryFn: () => studio.workflow(model) });
  const groups = useQuery({ queryKey: ["studio", "groups"], queryFn: studio.groups, staleTime: 300_000 });
  const existing = current.data?.workflow ?? null;
  const [editing, setEditing] = useState(false);
  if (current.isLoading) return <p className="muted">{t("Loading…")}</p>;
  return (
    <section className="studio-card" aria-label={t("Workflow")}>
      <h3>{t("Workflow")}</h3>
      {existing && !editing && (
        <>
          <p>
            {t("“{name}” follows the field {field}.", {
              name: existing.name,
              field: fields[existing.state_field]?.string ?? existing.state_field,
            })}{" "}
            {existing.strict ? t("The state changes only through its transitions.") : ""}
          </p>
          <WorkflowDiagram
            states={existing.states}
            transitions={existing.transitions}
            fields={fields}
            groups={groups.data ?? []}
          />
          <button type="button" className="btn" onClick={() => setEditing(true)}>
            {t("Change this workflow")}
          </button>
        </>
      )}
      {(!existing || editing) && (
        <WorkflowForm
          key={existing?.id ?? "new"}
          model={model}
          fields={fields}
          groups={groups.data ?? []}
          existing={existing}
          onCancel={existing ? () => setEditing(false) : undefined}
          onDone={() => {
            setEditing(false);
            void current.refetch();
            onDone();
          }}
        />
      )}
    </section>
  );
}

function WorkflowForm({
  model,
  fields,
  groups,
  existing,
  onCancel,
  onDone,
}: {
  model: string;
  fields: FieldsMeta;
  groups: Group[];
  existing: WorkflowInfo | null;
  onCancel?: () => void;
  onDone: () => void;
}) {
  const stateFields = Object.values(fields)
    .filter((f) => f.store && (f.type === "selection" || f.type === "many2one"))
    .sort((a, b) => a.string.localeCompare(b.string));
  const [name, setName] = useState(existing?.name ?? "");
  const [fieldName, setFieldName] = useState(existing?.state_field ?? NEW);
  const [newLabel, setNewLabel] = useState(t("Status"));
  const [strict, setStrict] = useState(existing?.strict ?? true);
  // transitions refer to state rows by id (as strings) until saved, so renaming a state keeps them
  const [initial] = useState(() => {
    const rows = (existing?.states ?? []).map((s) => ({ ...s, id: nextId() }));
    const idOf = (key: string) => String(rows.find((r) => r.key === key)?.id ?? "");
    const trs = (existing?.transitions ?? []).map((tr) => ({
      ...tr,
      id: nextId(),
      from: tr.from.map(idOf),
      to: idOf(tr.to),
    }));
    return { rows, trs };
  });
  const [states, setStates] = useState<StateRow[]>(initial.rows);
  const [transitions, setTransitions] = useState<TransitionRow[]>(initial.trs);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const field = fieldName === NEW ? undefined : fields[fieldName];
  const records = useQuery({
    queryKey: ["choices", field?.comodel],
    queryFn: () => api.nameSearch(field!.comodel!, "", undefined, 80),
    enabled: field?.type === "many2one" && !!field.comodel,
  });
  // the values the state field can take (none for a new field: the states define them)
  const values: [string, string][] =
    field?.type === "selection"
      ? (field.selection ?? [])
      : field?.type === "many2one"
        ? (records.data ?? []).map((r) => [String(r.id), r.display_name])
        : [];

  const keyFor = (row: StateRow) => (fieldName === NEW ? slugify(row.name) || `state_${row.id}` : row.key);
  const stateOptions = states.map((s) => [String(s.id), s.name || keyFor(s)] as const);
  const keyOfId = (id: string) => {
    const row = states.find((s) => String(s.id) === id);
    return row ? keyFor(row) : "";
  };
  const payloadTransitions = () =>
    transitions.map((tr) => ({
      name: tr.name,
      from: tr.from.map(keyOfId).filter(Boolean),
      to: keyOfId(tr.to),
      condition: tr.condition ?? null,
      groups: tr.groups ?? [],
      highlight: !!tr.highlight,
    }));
  const setState = (id: number, patch: Partial<StateRow>) =>
    setStates(states.map((s) => (s.id === id ? { ...s, ...patch } : s)));
  const setTransition = (id: number, patch: Partial<TransitionRow>) =>
    setTransitions(transitions.map((tr) => (tr.id === id ? { ...tr, ...patch } : tr)));

  const fillFromField = () =>
    setStates(values.map(([key, label]) => ({ id: nextId(), key, name: label, fold: false, done: false })));

  const payloadStates = () => states.map((s) => ({ key: keyFor(s), name: s.name, fold: !!s.fold, done: !!s.done }));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await studio.createWorkflow({
        model,
        name,
        state_field: fieldName === NEW ? "" : fieldName,
        new_field: fieldName === NEW ? { label: newLabel } : null,
        states: payloadStates(),
        transitions: payloadTransitions(),
        strict,
        replace: !!existing,
      });
      onDone();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="studio-subform" onSubmit={submit} aria-label={t("Build a workflow")}>
      <label className="field">
        <span>{t("Workflow name")}</span>
        <input className="input" required value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="field">
        <span>{t("State field")}</span>
        <select
          className="input"
          value={fieldName}
          onChange={(e) => {
            setFieldName(e.target.value);
            setStates([]);
            setTransitions([]);
          }}
        >
          <option value={NEW}>{t("A new choice field")}</option>
          {fieldName !== NEW && !stateFields.some((f) => f.name === fieldName) && (
            <option value={fieldName}>{fields[fieldName]?.string ?? fieldName}</option>
          )}
          {stateFields.map((f) => (
            <option key={f.name} value={f.name}>
              {f.string} ({f.name})
            </option>
          ))}
        </select>
      </label>
      {fieldName === NEW ? (
        <label className="field">
          <span>{t("Label of the new field")}</span>
          <input className="input" required value={newLabel} onChange={(e) => setNewLabel(e.target.value)} />
        </label>
      ) : (
        <button type="button" className="btn small" onClick={fillFromField} disabled={!values.length}>
          {t("Use the field's values as states")}
        </button>
      )}

      <h4>{t("States")}</h4>
      {!states.length && <p className="muted small">{t("Add the states in the order records go through them.")}</p>}
      <OrderedList
        label={t("States")}
        items={states}
        keyOf={(s) => String(s.id)}
        labelOf={(s) => s.name || t("New state")}
        onReorder={setStates}
        controls={(s) => (
          <>
            {fieldName === NEW ? (
              <input
                className="input small"
                aria-label={t("State name")}
                required
                value={s.name}
                onChange={(e) => setState(s.id, { name: e.target.value })}
              />
            ) : (
              <select
                className="input small"
                aria-label={t("State value")}
                required
                value={s.key}
                onChange={(e) =>
                  setState(s.id, { key: e.target.value, name: values.find(([k]) => k === e.target.value)?.[1] ?? "" })
                }
              >
                <option value="" />
                {values.map(([k, l]) => (
                  <option key={k} value={k}>
                    {l}
                  </option>
                ))}
              </select>
            )}
            <label className="checkbox small">
              <input type="checkbox" checked={!!s.fold} onChange={(e) => setState(s.id, { fold: e.target.checked })} />{" "}
              {t("Folded")}
            </label>
            <label className="checkbox small">
              <input type="checkbox" checked={!!s.done} onChange={(e) => setState(s.id, { done: e.target.checked })} />{" "}
              {t("Done")}
            </label>
            <button
              type="button"
              className="btn ghost small"
              aria-label={t("Remove state {name}", { name: s.name || "…" })}
              onClick={() => setStates(states.filter((x) => x.id !== s.id))}
            >
              ×
            </button>
          </>
        )}
      />
      <button
        type="button"
        className="btn small"
        onClick={() => setStates([...states, { id: nextId(), key: "", name: "", fold: false, done: false }])}
      >
        {t("Add a state")}
      </button>

      <h4>{t("Transitions")}</h4>
      {!transitions.length && <p className="muted small">{t("Transitions become buttons on the record.")}</p>}
      <ul className="studio-transitions">
        {transitions.map((tr) => (
          <li key={tr.id}>
            <div
              className="studio-transition"
              role="group"
              aria-label={t("Transition {name}", { name: tr.name || "…" })}
            >
              <input
                className="input small"
                aria-label={t("Button label")}
                required
                value={tr.name}
                onChange={(e) => setTransition(tr.id, { name: e.target.value })}
              />
              <select
                className="input small"
                aria-label={t("From")}
                multiple
                size={Math.min(4, Math.max(2, stateOptions.length))}
                value={tr.from}
                onChange={(e) => setTransition(tr.id, { from: [...e.target.selectedOptions].map((o) => o.value) })}
              >
                {stateOptions.map(([k, l]) => (
                  <option key={k} value={k}>
                    {l}
                  </option>
                ))}
              </select>
              <select
                className="input small"
                aria-label={t("To")}
                required
                value={tr.to}
                onChange={(e) => setTransition(tr.id, { to: e.target.value })}
              >
                <option value="" />
                {stateOptions.map(([k, l]) => (
                  <option key={k} value={k}>
                    {l}
                  </option>
                ))}
              </select>
              <select
                className="input small"
                aria-label={t("Allowed group")}
                value={tr.groups?.[0] ?? ""}
                onChange={(e) => setTransition(tr.id, { groups: e.target.value ? [e.target.value] : [] })}
              >
                <option value="">{t("Everyone who can edit")}</option>
                {groups.map((g) => (
                  <option key={g.xmlid} value={g.xmlid}>
                    {g.name}
                  </option>
                ))}
              </select>
              <label className="checkbox small">
                <input
                  type="checkbox"
                  checked={!!tr.highlight}
                  onChange={(e) => setTransition(tr.id, { highlight: e.target.checked })}
                />{" "}
                {t("Main button")}
              </label>
              <button
                type="button"
                className="btn ghost small"
                aria-label={t("Remove transition {name}", { name: tr.name || "…" })}
                onClick={() => setTransitions(transitions.filter((x) => x.id !== tr.id))}
              >
                ×
              </button>
              <details className="studio-more">
                <summary>{tr.condition ? t("Only when… (set)") : t("Only when…")}</summary>
                <ConditionEditor
                  fields={fields}
                  value={tr.condition as Domain | null}
                  onChange={(d) => setTransition(tr.id, { condition: d })}
                  label={t("Condition of {name}", { name: tr.name || "…" })}
                />
              </details>
            </div>
          </li>
        ))}
      </ul>
      <p className="muted small">{t("Without a “From” state, a transition is offered from every state.")}</p>
      <button
        type="button"
        className="btn small"
        disabled={!states.length}
        onClick={() => setTransitions([...transitions, { id: nextId(), name: "", from: [], to: "", groups: [] }])}
      >
        {t("Add a transition")}
      </button>
      <label className="checkbox">
        <input type="checkbox" checked={strict} onChange={(e) => setStrict(e.target.checked)} />{" "}
        {t("Change the state only through the transitions")}
      </label>
      {states.length > 0 && (
        <>
          <h4>{t("Preview")}</h4>
          <WorkflowDiagram
            states={payloadStates()}
            transitions={payloadTransitions()}
            fields={fields}
            groups={groups}
          />
        </>
      )}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      <div className="studio-actions">
        <button type="submit" className="btn primary" disabled={busy || !states.length || !name}>
          {existing ? t("Replace the workflow") : t("Create workflow")}
        </button>
        {onCancel && (
          <button type="button" className="btn ghost" onClick={onCancel}>
            {t("Cancel")}
          </button>
        )}
      </div>
    </form>
  );
}
