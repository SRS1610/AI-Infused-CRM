// Add a field, or edit a Studio field: label, type, placement and options (widget, default value,
// required / read-only always or under a condition, groups that may see it).
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { Domain, FieldsMeta } from "../../kernel/api/types";
import { t } from "../../kernel/i18n";
import { ConditionEditor, RecordSelect } from "../../kernel/views/ConditionEditor";
import { errorText, slug, slugify, studio, widgetsFor, type StudioFieldInfo, type StudioFieldOptions } from "./api";

export const fieldTypes = (): [string, string][] => [
  ["char", t("Text")],
  ["text", t("Long text")],
  ["html", t("Rich text")],
  ["integer", t("Whole number")],
  ["float", t("Decimal number")],
  ["monetary", t("Amount")],
  ["boolean", t("Checkbox")],
  ["date", t("Date")],
  ["datetime", t("Date and time")],
  ["selection", t("Choice")],
  ["many2one", t("Link to one record")],
  ["many2many", t("Links to several records")],
];

type Mode = "no" | "always" | "when";

export interface OptionsState {
  widget: string;
  default: unknown;
  required: Mode;
  requiredIf: Domain | null;
  readonly: Mode;
  readonlyIf: Domain | null;
  groups: string[];
  help: string;
}

export const emptyOptions = (): OptionsState => ({
  widget: "",
  default: null,
  required: "no",
  requiredIf: null,
  readonly: "no",
  readonlyIf: null,
  groups: [],
  help: "",
});

const modeOf = (always: boolean, condition: Domain | null) => (always ? "always" : condition ? "when" : "no");

export function optionsFrom(o: StudioFieldOptions): OptionsState {
  return {
    widget: o.widget ?? "",
    default: o.default ?? null,
    required: modeOf(o.required, o.required_if),
    requiredIf: o.required_if,
    readonly: modeOf(o.readonly, o.readonly_if),
    readonlyIf: o.readonly_if,
    groups: o.groups ?? [],
    help: o.help ?? "",
  };
}

/** The options as the API takes them. */
export function optionsPayload(s: OptionsState): Record<string, unknown> {
  return {
    widget: s.widget || null,
    default: s.default === "" ? null : s.default,
    required: s.required === "always",
    required_if: s.required === "when" ? s.requiredIf : null,
    readonly: s.readonly === "always",
    readonly_if: s.readonly === "when" ? s.readonlyIf : null,
    groups: s.groups,
    help: s.help || null,
  };
}

function DefaultInput({
  type,
  selection,
  comodel,
  value,
  onChange,
}: {
  type: string;
  selection: [string, string][];
  comodel: string;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const label = t("Default value");
  if (type === "many2many") return null;
  let input: React.ReactNode;
  if (type === "selection")
    input = (
      <select
        className="input"
        aria-label={label}
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">{t("None")}</option>
        {selection.map(([k, l]) => (
          <option key={k} value={k}>
            {l}
          </option>
        ))}
      </select>
    );
  else if (type === "boolean")
    input = (
      <select
        className="input"
        aria-label={label}
        value={value === true ? "true" : value === false ? "false" : ""}
        onChange={(e) => onChange(e.target.value === "" ? null : e.target.value === "true")}
      >
        <option value="">{t("None")}</option>
        <option value="true">{t("Ticked")}</option>
        <option value="false">{t("Not ticked")}</option>
      </select>
    );
  else if (type === "many2one")
    input = comodel ? (
      <RecordSelect
        model={comodel}
        label={label}
        value={typeof value === "number" ? value : null}
        onChange={onChange}
        emptyLabel={t("None")}
      />
    ) : (
      <span className="muted small">{t("Choose the linked model first.")}</span>
    );
  else {
    const numeric = ["integer", "float", "monetary"].includes(type);
    input = (
      <input
        className="input"
        aria-label={label}
        type={numeric ? "number" : type === "date" ? "date" : type === "datetime" ? "datetime-local" : "text"}
        step={type === "integer" ? 1 : "any"}
        value={value === null || value === undefined ? "" : String(value)}
        onChange={(e) =>
          onChange(
            e.target.value === ""
              ? null
              : numeric
                ? type === "integer"
                  ? Number.parseInt(e.target.value, 10)
                  : Number(e.target.value)
                : e.target.value,
          )
        }
      />
    );
  }
  return (
    <div className="field">
      <span>{label}</span>
      {input}
    </div>
  );
}

function ModeSelect({ label, value, onChange }: { label: string; value: Mode; onChange: (m: Mode) => void }) {
  return (
    <label className="field">
      <span>{label}</span>
      <select className="input" value={value} onChange={(e) => onChange(e.target.value as Mode)}>
        <option value="no">{t("No")}</option>
        <option value="always">{t("Always")}</option>
        <option value="when">{t("When…")}</option>
      </select>
    </label>
  );
}

/** Widget, default, required / read-only conditions, groups and help of a field. */
export function FieldOptions({
  type,
  selection,
  comodel,
  fields,
  value,
  onChange,
}: {
  type: string;
  selection: [string, string][];
  comodel: string;
  fields: FieldsMeta;
  value: OptionsState;
  onChange: (v: OptionsState) => void;
}) {
  const groups = useQuery({ queryKey: ["studio", "groups"], queryFn: studio.groups, staleTime: 300_000 });
  const widgets = widgetsFor(type);
  const set = (patch: Partial<OptionsState>) => onChange({ ...value, ...patch });
  return (
    <>
      {widgets.length > 0 && (
        <label className="field">
          <span>{t("Widget")}</span>
          <select className="input" value={value.widget} onChange={(e) => set({ widget: e.target.value })}>
            <option value="">{t("Standard")}</option>
            {widgets.map(([w, l]) => (
              <option key={w} value={w}>
                {l}
              </option>
            ))}
          </select>
        </label>
      )}
      <DefaultInput
        type={type}
        selection={selection}
        comodel={comodel}
        value={value.default}
        onChange={(v) => set({ default: v })}
      />
      <ModeSelect label={t("Required")} value={value.required} onChange={(m) => set({ required: m })} />
      {value.required === "when" && (
        <ConditionEditor
          fields={fields}
          value={value.requiredIf}
          onChange={(d) => set({ requiredIf: d })}
          label={t("Required when")}
        />
      )}
      <ModeSelect label={t("Read-only")} value={value.readonly} onChange={(m) => set({ readonly: m })} />
      {value.readonly === "when" && (
        <ConditionEditor
          fields={fields}
          value={value.readonlyIf}
          onChange={(d) => set({ readonlyIf: d })}
          label={t("Read-only when")}
        />
      )}
      <label className="field">
        <span>{t("Visible to (empty: everyone)")}</span>
        <select
          className="input"
          multiple
          size={4}
          value={value.groups}
          onChange={(e) => set({ groups: [...e.target.selectedOptions].map((o) => o.value) })}
        >
          {(groups.data ?? []).map((g) => (
            <option key={g.xmlid} value={g.xmlid}>
              {g.name}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>{t("Help text")}</span>
        <input className="input" value={value.help} onChange={(e) => set({ help: e.target.value })} />
      </label>
    </>
  );
}

const choiceOptions = (text: string): [string, string][] =>
  text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => [slugify(s), s]);

export function AddField({
  model,
  fields,
  meta,
  onDone,
}: {
  model: string;
  fields: StudioFieldInfo[];
  meta: FieldsMeta;
  onDone: () => void;
}) {
  const [label, setLabel] = useState("");
  const [type, setType] = useState("char");
  const [after, setAfter] = useState("");
  const [inList, setInList] = useState(true);
  const [onCard, setOnCard] = useState(false);
  const [options, setOptions] = useState("");
  const [comodel, setComodel] = useState("");
  const [opts, setOpts] = useState<OptionsState>(emptyOptions);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const models = useQuery({ queryKey: ["studio", "models"], queryFn: studio.models });
  const selection = choiceOptions(options);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const payload = optionsPayload(opts);
    const body: Record<string, unknown> = Object.fromEntries(
      Object.entries(payload).filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length)),
    );
    body.required = payload.required;
    if (type === "selection") body.selection = selection;
    if (type === "many2one" || type === "many2many") body.comodel = comodel;
    const placements = [
      { view_type: "form", ...(after ? { after } : {}) },
      ...(inList ? [{ view_type: "list" }] : []),
      ...(onCard ? [{ view_type: "kanban" }] : []),
    ];
    try {
      await studio.addField({ model, name: slug(label), type, label, placements, options: body });
      setLabel("");
      setOpts(emptyOptions());
      onDone();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="studio-card" onSubmit={submit} aria-label={t("Add a field")}>
      <h3>{t("Add a field")}</h3>
      <label className="field">
        <span>{t("Label")}</span>
        <input className="input" required value={label} onChange={(e) => setLabel(e.target.value)} />
      </label>
      <p className="muted small">{t("Technical name: {name}", { name: label ? slug(label) : "—" })}</p>
      <label className="field">
        <span>{t("Type")}</span>
        <select
          className="input"
          value={type}
          onChange={(e) => {
            setType(e.target.value);
            setOpts({ ...opts, widget: "", default: null });
          }}
        >
          {fieldTypes().map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
      </label>
      {type === "selection" && (
        <label className="field">
          <span>{t("Options (comma separated)")}</span>
          <input className="input" required value={options} onChange={(e) => setOptions(e.target.value)} />
        </label>
      )}
      {(type === "many2one" || type === "many2many") && (
        <label className="field">
          <span>{t("Linked model")}</span>
          <select className="input" required value={comodel} onChange={(e) => setComodel(e.target.value)}>
            <option value="">{t("Choose…")}</option>
            {(models.data ?? []).map((m) => (
              <option key={m.model} value={m.model}>
                {m.description}
              </option>
            ))}
          </select>
        </label>
      )}
      <label className="field">
        <span>{t("Place on the form after")}</span>
        <select className="input" value={after} onChange={(e) => setAfter(e.target.value)}>
          <option value="">{t("End of the form")}</option>
          {fields.map((f) => (
            <option key={f.name} value={f.name}>
              {f.label}
            </option>
          ))}
        </select>
      </label>
      <label className="checkbox">
        <input type="checkbox" checked={inList} onChange={(e) => setInList(e.target.checked)} /> {t("Show in the list")}
      </label>
      <label className="checkbox">
        <input type="checkbox" checked={onCard} onChange={(e) => setOnCard(e.target.checked)} />{" "}
        {t("Show on the kanban card")}
      </label>
      <details className="studio-more">
        <summary>{t("More options")}</summary>
        <FieldOptions
          type={type}
          selection={selection}
          comodel={comodel}
          fields={meta}
          value={opts}
          onChange={setOpts}
        />
      </details>
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      <button type="submit" className="btn primary" disabled={busy || !label}>
        {busy ? t("Adding…") : t("Add field")}
      </button>
    </form>
  );
}

function EditFieldForm({
  model,
  field,
  meta,
  onDone,
}: {
  model: string;
  field: StudioFieldInfo & { options: StudioFieldOptions };
  meta: FieldsMeta;
  onDone: () => void;
}) {
  const original = field.options;
  const [label, setLabel] = useState(original.label);
  const [choices, setChoices] = useState((original.selection ?? []).map(([, l]) => l).join("\n"));
  const [opts, setOpts] = useState<OptionsState>(() => optionsFrom(original));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const selection: [string, string][] = choices
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((l, i) => [original.selection?.[i]?.[0] ?? slugify(l), l]);
  const changes = (): Record<string, unknown> => {
    const out: Record<string, unknown> = {};
    const before = optionsPayload(optionsFrom(original));
    const after = optionsPayload(opts);
    for (const [k, v] of Object.entries(after)) if (JSON.stringify(v) !== JSON.stringify(before[k])) out[k] = v;
    if (label !== original.label) out.label = label;
    if (original.ttype === "selection" && JSON.stringify(selection) !== JSON.stringify(original.selection ?? []))
      out.selection = selection;
    return out;
  };
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const body = changes();
    setError(null);
    if (!Object.keys(body).length) {
      setError(t("Nothing to save: change an option first."));
      return;
    }
    setBusy(true);
    try {
      await studio.editField(model, field.name, body);
      onDone();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={submit} aria-label={t("Field options")} className="studio-subform">
      <label className="field">
        <span>{t("Label")}</span>
        <input className="input" required value={label} onChange={(e) => setLabel(e.target.value)} />
      </label>
      {original.ttype === "selection" && (
        <label className="field">
          <span>{t("Options (one per line; add or rename, never remove)")}</span>
          <textarea className="input textarea" rows={3} value={choices} onChange={(e) => setChoices(e.target.value)} />
        </label>
      )}
      <FieldOptions
        type={original.ttype}
        selection={selection}
        comodel={original.comodel ?? ""}
        fields={meta}
        value={opts}
        onChange={setOpts}
      />
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      <button type="submit" className="btn primary" disabled={busy}>
        {t("Save field")}
      </button>
    </form>
  );
}

export function EditField({
  model,
  fields,
  meta,
  onDone,
}: {
  model: string;
  fields: StudioFieldInfo[];
  meta: FieldsMeta;
  onDone: () => void;
}) {
  const studioFields = fields.filter((f) => f.studio && f.options);
  const [name, setName] = useState("");
  const current = studioFields.find((f) => f.name === name);
  return (
    <section className="studio-card" aria-label={t("Edit a field")}>
      <h3>{t("Edit a field")}</h3>
      {studioFields.length === 0 ? (
        <p className="muted">{t("Fields added with Studio can be edited here.")}</p>
      ) : (
        <label className="field">
          <span>{t("Studio field")}</span>
          <select className="input" value={name} onChange={(e) => setName(e.target.value)}>
            <option value="">{t("Choose…")}</option>
            {studioFields.map((f) => (
              <option key={f.name} value={f.name}>
                {f.label} ({f.name})
              </option>
            ))}
          </select>
        </label>
      )}
      {current && (
        <EditFieldForm
          key={`${current.name}-${JSON.stringify(current.options)}`}
          model={model}
          field={current as StudioFieldInfo & { options: StudioFieldOptions }}
          meta={meta}
          onDone={onDone}
        />
      )}
    </section>
  );
}
