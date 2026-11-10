// Display formatting with Intl, honouring the user's language and timezone.
import type { FieldMeta, RecordData } from "./api/types";
import { t, tn } from "./i18n";

let locale = "en-US";
let timeZone: string | undefined;

export function configureFormat(lang: string, tz?: string): void {
  locale = lang.replace("_", "-");
  timeZone = tz || undefined;
}

export function formatNumber(value: number | null | undefined, digits?: number): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "";
  return new Intl.NumberFormat(
    locale,
    digits === undefined ? undefined : { minimumFractionDigits: digits, maximumFractionDigits: digits },
  ).format(value);
}

export function formatMonetary(value: number | null | undefined, currency?: string | null): string {
  if (value === null || value === undefined) return "";
  if (!currency) return formatNumber(value, 2);
  try {
    return new Intl.NumberFormat(locale, { style: "currency", currency }).format(value);
  } catch {
    return `${formatNumber(value, 2)} ${currency}`;
  }
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "";
  const [y, m, d] = value.slice(0, 10).split("-").map(Number);
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" }).format(
    new Date(Date.UTC(y, m - 1, d)),
  );
}

export function formatDatetime(value: string | null | undefined): string {
  if (!value) return "";
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short", timeZone }).format(new Date(value));
}

export function currencyCode(record: RecordData, field: FieldMeta): string | null {
  const cur = record[field.currency_field ?? "currency_id"];
  if (cur && typeof cur === "object" && cur.display_name) return String(cur.display_name);
  return null;
}

export function displayValue(field: FieldMeta | undefined, value: any, record: RecordData = {}): string {
  if (!field) return value === null || value === undefined ? "" : String(value);
  if (value === null || value === undefined || value === false) return field.type === "boolean" ? t("No") : "";
  switch (field.type) {
    case "many2one":
      return value.display_name ?? `#${value.id}`;
    case "one2many":
    case "many2many":
      return Array.isArray(value) ? tn("{count} record", "{count} records", value.length) : "";
    case "boolean":
      return value ? t("Yes") : t("No");
    case "selection":
      return field.selection?.find(([k]) => k === value)?.[1] ?? String(value);
    case "integer":
      return formatNumber(value, 0);
    case "float":
      return formatNumber(value);
    case "monetary":
      return formatMonetary(value, currencyCode(record, field));
    case "date":
      return formatDate(value);
    case "datetime":
      return formatDatetime(value);
    case "json":
      return JSON.stringify(value);
    case "html":
      return String(value)
        .replace(/<[^>]+>/g, " ")
        .replace(/\s+/g, " ")
        .trim();
    default:
      return String(value);
  }
}
