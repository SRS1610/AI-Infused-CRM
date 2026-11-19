import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { tn } from "../i18n";

/** Names of the records in a many2many value (the API returns ids), fetched once per set of ids.
 *  Falls back to the count while loading or when the user cannot read the related records. */
export function RelatedNames({
  comodel,
  ids,
  chips = false,
}: {
  comodel?: string | null;
  ids: number[];
  chips?: boolean;
}) {
  const sorted = [...ids].sort((a, b) => a - b);
  const names = useQuery({
    queryKey: ["names", comodel, sorted.join(",")],
    queryFn: () =>
      api.list(comodel!, { domain: [["id", "in", sorted]], fields: ["display_name"], limit: sorted.length }),
    enabled: !!comodel && sorted.length > 0,
    staleTime: 60_000,
    retry: false,
  });
  if (!sorted.length) return null;
  const items = names.data?.items ?? [];
  if (!items.length) return <>{tn("{count} record", "{count} records", sorted.length)}</>;
  if (!chips) return <>{items.map((r) => String(r.display_name)).join(", ")}</>;
  return (
    <span className="card-tags">
      {items.map((r) => (
        <span key={String(r.id)} className="tag">
          {String(r.display_name)}
        </span>
      ))}
    </span>
  );
}
