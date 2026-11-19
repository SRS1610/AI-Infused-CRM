// Operators offered for a condition on one field (the search panel's custom filter and the
// condition editor), with labels in the user's language.
import type { FieldMeta } from "./api/types";
import { t } from "./i18n";

export const operatorTable = (): Record<string, [string, string][]> => ({
  text: [
    ["ilike", t("contains")],
    ["not_ilike", t("does not contain")],
    ["=", t("is")],
    ["!=", t("is not")],
    ["is_set", t("is set")],
    ["is_not_set", t("is not set")],
  ],
  number: [
    ["=", "="],
    ["!=", "≠"],
    [">", ">"],
    [">=", "≥"],
    ["<", "<"],
    ["<=", "≤"],
    ["is_set", t("is set")],
  ],
  date: [
    ["=", t("on")],
    [">=", t("on or after")],
    ["<=", t("on or before")],
    ["is_set", t("is set")],
    ["is_not_set", t("is not set")],
  ],
  boolean: [["=", t("is")]],
  selection: [
    ["=", t("is")],
    ["!=", t("is not")],
    ["is_set", t("is set")],
  ],
  relation: [
    ["ilike", t("name contains")],
    ["is_set", t("is set")],
    ["is_not_set", t("is not set")],
  ],
  record: [
    ["=", t("is")],
    ["!=", t("is not")],
    ["is_set", t("is set")],
    ["is_not_set", t("is not set")],
  ],
  set: [
    ["is_set", t("is set")],
    ["is_not_set", t("is not set")],
  ],
});

export const NUMERIC_TYPES = ["integer", "float", "monetary"];
export const DATE_TYPES = ["date", "datetime"];
export const RELATION_TYPES = ["many2one", "many2many", "one2many"];

/** Operators of the search panel's custom filter. */
export function operatorsFor(field?: FieldMeta): [string, string][] {
  const OPERATORS = operatorTable();
  if (!field) return OPERATORS.text;
  if (NUMERIC_TYPES.includes(field.type)) return OPERATORS.number;
  if (DATE_TYPES.includes(field.type)) return OPERATORS.date;
  if (field.type === "boolean") return OPERATORS.boolean;
  if (field.type === "selection") return OPERATORS.selection;
  if (RELATION_TYPES.includes(field.type)) return OPERATORS.relation;
  return OPERATORS.text;
}

/** Operators of the condition editor: a link to one record compares with a chosen record. */
export function conditionOperatorsFor(field?: FieldMeta): [string, string][] {
  const OPERATORS = operatorTable();
  if (field?.type === "many2one") return OPERATORS.record;
  if (field && (field.type === "many2many" || field.type === "one2many")) return OPERATORS.set;
  if (field?.type === "selection") return [...OPERATORS.selection, ["is_not_set", t("is not set")]];
  return operatorsFor(field);
}

export const valueless = (op: string) => op === "is_set" || op === "is_not_set";
