import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ViewNode } from "../api/types";
import { t } from "../i18n";

/** A parameter a form button asks for before calling its method (``"params"`` in the view). */
export interface ButtonParam {
  name: string;
  label: string;
  type?: "text" | "textarea" | "number" | "date" | "select" | "boolean";
  required?: boolean;
  default?: string | number | boolean | null;
  options?: [string, string][];
  help?: string;
}

function initial(params: ButtonParam[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const p of params) {
    if (p.default !== undefined && p.default !== null) out[p.name] = p.default;
    else if (p.type === "date") out[p.name] = new Date().toISOString().slice(0, 10);
    else if (p.type === "boolean") out[p.name] = false;
    else if (p.type === "select" && p.options?.length) out[p.name] = p.options[0][0];
  }
  return out;
}

/** Values to send: empty optional fields are left out so the server applies its defaults. */
export function cleanParams(params: ButtonParam[], values: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const p of params) {
    const v = values[p.name];
    if (v === undefined || v === "" || v === null) continue;
    out[p.name] = p.type === "number" ? Number(v) : v;
  }
  return out;
}

interface Props {
  node: ViewNode;
  onSubmit: (kwargs: Record<string, unknown>) => void;
  onCancel: () => void;
}

/** Collects a button's parameters (and shows its confirmation text) before the method runs. */
export function ButtonDialog({ node, onSubmit, onCancel }: Props) {
  const params = (node.params ?? []) as ButtonParam[];
  const [values, setValues] = useState<Record<string, unknown>>(() => initial(params));
  const titleId = useId();
  const first = useRef<HTMLElement | null>(null);
  useEffect(() => first.current?.focus(), []);
  const missing = params.filter((p) => p.required && (values[p.name] === undefined || values[p.name] === ""));
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    e.stopPropagation(); // rendered in a portal: React would bubble the submit to the record's form
    if (missing.length) return;
    onSubmit(cleanParams(params, values));
  };
  const set = (name: string, value: unknown) => setValues((v) => ({ ...v, [name]: value }));
  return createPortal(
    <div className="palette-backdrop" onMouseDown={onCancel} onKeyDown={(e) => e.key === "Escape" && onCancel()}>
      <form
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onSubmit={submit}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h2 id={titleId}>{node.dialog_title ?? node.label ?? node.method}</h2>
        {node.confirm && <p>{node.confirm}</p>}
        {params.map((p, i) => {
          const id = `${titleId}-${p.name}`;
          const ref = (el: HTMLElement | null) => {
            if (i === 0) first.current = el;
          };
          const value = values[p.name];
          let input: React.ReactNode;
          if (p.type === "select") {
            input = (
              <select id={id} ref={ref} value={String(value ?? "")} onChange={(e) => set(p.name, e.target.value)}>
                {(p.options ?? []).map(([v, label]) => (
                  <option key={v} value={v}>
                    {label}
                  </option>
                ))}
              </select>
            );
          } else if (p.type === "textarea") {
            input = (
              <textarea id={id} ref={ref} value={String(value ?? "")} onChange={(e) => set(p.name, e.target.value)} />
            );
          } else if (p.type === "boolean") {
            input = (
              <input
                id={id}
                ref={ref}
                type="checkbox"
                checked={Boolean(value)}
                onChange={(e) => set(p.name, e.target.checked)}
              />
            );
          } else {
            input = (
              <input
                id={id}
                ref={ref}
                type={p.type === "number" ? "number" : p.type === "date" ? "date" : "text"}
                step={p.type === "number" ? "any" : undefined}
                required={p.required}
                value={String(value ?? "")}
                onChange={(e) => set(p.name, e.target.value)}
              />
            );
          }
          return (
            <div className="form-field" key={p.name}>
              <label htmlFor={id}>
                {p.label}
                {p.required && (
                  <span aria-hidden="true" className="req">
                    *
                  </span>
                )}
              </label>
              {input}
              {p.help && <small className="help">{p.help}</small>}
            </div>
          );
        })}
        <div className="button-row">
          <button type="submit" className="btn primary" disabled={missing.length > 0}>
            {node.submit_label ?? t("Confirm")}
          </button>
          <button type="button" className="btn" onClick={onCancel}>
            {t("Cancel")}
          </button>
        </div>
      </form>
    </div>,
    document.body,
  );
}
