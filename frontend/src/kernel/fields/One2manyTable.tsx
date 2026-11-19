import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ListColumn } from "../api/types";
import { displayValue } from "../format";
import { RelatedNames } from "./RelatedNames";

const NUMERIC = new Set(["integer", "float", "monetary"]);
import { t } from "../i18n";

interface Props {
  comodel: string;
  ids: number[];
  columns?: ListColumn[];
  label: string;
  onOpen?: (id: number) => void;
}

/** Read-only table of linked records (inline editing of lines arrives with Phase 1.3). */
export function One2manyTable({ comodel, ids, columns, label, onOpen }: Props) {
  const views = useQuery({
    queryKey: ["views", comodel, "list"],
    queryFn: () => api.views(comodel, ["list"]),
    enabled: ids.length > 0,
  });
  const cols: ListColumn[] = columns ?? views.data?.views.list?.columns ?? [{ field: "display_name" }];
  const fieldNames = cols.map((c) => c.field);
  const rows = useQuery({
    queryKey: ["o2m", comodel, ids.join(","), fieldNames.join(",")],
    queryFn: () => api.list(comodel, { domain: [["id", "in", ids]], fields: fieldNames, limit: 500, count: false }),
    enabled: ids.length > 0 && (!!columns || views.isSuccess),
  });
  if (!ids.length) return <p className="muted">{t("No {items}.", { items: label.toLowerCase() })}</p>;
  const meta = views.data?.fields ?? {};
  return (
    <div className="table-scroll" tabIndex={0} role="region" aria-label={label}>
      <table className="table compact" aria-label={label}>
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c.field} scope="col" className={NUMERIC.has(meta[c.field]?.type ?? "") ? "num" : undefined}>
                {c.label ?? meta[c.field]?.string ?? c.field}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {(rows.data?.items ?? []).map((row) => (
            <tr key={row.id} onClick={() => onOpen?.(Number(row.id))} className={onOpen ? "clickable" : undefined}>
              {cols.map((c) => (
                <td key={c.field} className={NUMERIC.has(meta[c.field]?.type ?? "") ? "num" : undefined}>
                  {meta[c.field]?.type === "many2many" && Array.isArray(row[c.field]) ? (
                    <RelatedNames comodel={meta[c.field].comodel} ids={row[c.field]} />
                  ) : (
                    displayValue(meta[c.field], row[c.field], row)
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
