// Studio API client, shared types and helpers.
import { ApiError, request } from "../../kernel/api/client";
import type { Domain } from "../../kernel/api/types";
import { t } from "../../kernel/i18n";

export interface StudioModel {
  model: string;
  description: string;
  module: string;
}
export interface StudioFieldOptions {
  label: string;
  help: string | null;
  required: boolean;
  readonly: boolean;
  required_if: Domain | null;
  readonly_if: Domain | null;
  default: unknown;
  selection: [string, string][] | null;
  groups: string[];
  widget: string | null;
  tracking: boolean;
  ttype: string;
  comodel: string | null;
}
export interface StudioFieldInfo {
  name: string;
  label: string;
  type: string;
  studio: boolean;
  store?: boolean;
  options?: StudioFieldOptions;
}
export interface Change {
  id: number;
  kind: string;
  model: string | null;
  summary: string;
  state: "applied" | "rolled_back";
  user: string | null;
  date: string | null;
}
export interface Group {
  xmlid: string;
  name: string;
}
export interface WorkflowState {
  key: string;
  name: string;
  fold?: boolean;
  done?: boolean;
}
export interface WorkflowTransition {
  name: string;
  from: string[];
  to: string;
  condition?: Domain | null;
  groups?: string[];
  highlight?: boolean;
}
export interface WorkflowInfo {
  id: number;
  name: string;
  state_field: string;
  strict: boolean;
  studio_change_id: number | null;
  states: WorkflowState[];
  transitions: (WorkflowTransition & { id: number })[];
}
export type Op = Record<string, unknown>;

export const studio = {
  models: () => request<StudioModel[]>("GET", "/studio/models"),
  fields: (model: string) => request<StudioFieldInfo[]>("GET", `/studio/models/${model}/fields`),
  groups: () => request<Group[]>("GET", "/studio/groups"),
  addField: (body: unknown) => request<{ field: string; change_id: number }>("POST", "/studio/fields", { body }),
  editField: (model: string, name: string, options: Record<string, unknown>) =>
    request<Change>("PATCH", `/studio/models/${model}/fields/${name}`, { body: { options } }),
  changeView: (model: string, viewType: string, operations: Op[], summary: string) =>
    request<Change>("POST", `/studio/views/${model}/${viewType}/operations`, { body: { operations, summary } }),
  createView: (body: { model: string; view_type: string; name: string; make_default: boolean }) =>
    request<Change>("POST", "/studio/views", { body }),
  addAutomation: (values: unknown) => request<Change>("POST", "/studio/automations", { body: values }),
  workflow: (model: string) => request<{ workflow: WorkflowInfo | null }>("GET", `/studio/models/${model}/workflow`),
  createWorkflow: (body: unknown) => request<Change>("POST", "/studio/workflows", { body }),
  changes: (model?: string) => request<Change[]>("GET", "/studio/changes", { query: { model } }),
  rollback: (id: number) => request<Change>("POST", `/studio/changes/${id}/rollback`),
  snapshot: (name: string) => request<{ id: number }>("POST", "/studio/snapshots", { query: { name } }),
  snapshots: () => request<{ id: number; name: string; date: string }[]>("GET", "/studio/snapshots"),
  rollbackTo: (id: number) => request<{ rolled_back: number }>("POST", `/studio/snapshots/${id}/rollback`),
};

export const errorText = (e: unknown) =>
  e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);

/** Lowercase identifier from a label ("Trade fair" → "trade_fair"). */
export const slugify = (label: string) =>
  label
    .toLowerCase()
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_|_$/g, "")
    .slice(0, 40);

/** Technical name of a Studio field from its label. */
export const slug = (label: string) => "x_" + slugify(label);

/** Widgets the client renders, per field type (an empty choice keeps the type's default). */
export const widgetsFor = (type: string): [string, string][] =>
  (
    ({
      char: [
        ["email", t("Email address")],
        ["url", t("Web address")],
        ["badge", t("Badge")],
      ],
      selection: [
        ["statusbar", t("Status bar")],
        ["badge", t("Badge")],
      ],
      many2one: [["statusbar", t("Status bar")]],
    }) as Record<string, [string, string][]>
  )[type] ?? [];

/**
 * Inheritance operations that turn the `original` order of items into `target` (both lists of keys,
 * `target` a permutation of the kept items): each misplaced item is removed and inserted again after
 * its new predecessor (or before the first item).
 */
export function reorderOps(
  original: string[],
  target: string[],
  anchor: (key: string) => Op,
  node: (key: string) => Op,
) {
  const current = original.filter((k) => target.includes(k));
  const ops: Op[] = [];
  target.forEach((key, i) => {
    if (current[i] === key) return;
    current.splice(current.indexOf(key), 1);
    ops.push({ op: "remove", anchor: anchor(key) });
    ops.push(
      i === 0
        ? { op: "insert_before", anchor: anchor(current[0]), node: node(key) }
        : { op: "insert_after", anchor: anchor(target[i - 1]), node: node(key) },
    );
    current.splice(i, 0, key);
  });
  return ops;
}

/** Move `key` one step (`delta` -1 or 1) in `list`. */
export function step<T>(list: T[], index: number, delta: -1 | 1): T[] {
  const j = index + delta;
  if (index < 0 || j < 0 || j >= list.length) return list;
  const next = [...list];
  [next[index], next[j]] = [next[j], next[index]];
  return next;
}

/** Move the item at `from` right after the item at `to` (drag and drop). */
export function moveAfter<T>(list: T[], from: number, to: number): T[] {
  if (from === to || from < 0 || to < 0) return list;
  const next = [...list];
  const [item] = next.splice(from, 1);
  next.splice(from < to ? to : to + 1, 0, item);
  return next;
}
