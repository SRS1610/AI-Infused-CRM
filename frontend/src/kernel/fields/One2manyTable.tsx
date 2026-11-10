import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ListColumn } from "../api/types";
import { displayValue } from "../format";
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
    <table className="table compact" aria-label={label}>
      <thead>
        <tr>
          {cols.map((c) => (
            <th key={c.field} scope="col">
              {c.label ?? meta[c.field]?.string ?? c.field}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {(rows.data?.items ?? []).map((row) => (
          <tr key={row.id} onClick={() => onOpen?.(Number(row.id))} className={onOpen ? "clickable" : undefined}>
            {cols.map((c) => (
              <td key={c.field}>{displayValue(meta[c.field], row[c.field], row)}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
