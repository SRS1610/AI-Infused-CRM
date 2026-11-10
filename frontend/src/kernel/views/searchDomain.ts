import type { Domain, ViewDef, ViewsResponse } from "../api/types";
import { and, or, textSearch } from "../domain";
import type { SearchState } from "./SearchBar";

/** Combine the search view's selected filters (OR inside the panel), free text and custom conditions (AND). */
export function buildDomain(meta: ViewsResponse, view: ViewDef | undefined, state: SearchState): Domain {
  const fields = (view?.fields?.length ? view.fields : [meta.model.rec_name]).filter(
    (f) => f in meta.fields || f === "display_name",
  );
  const text = textSearch(fields.length ? fields : ["display_name"], state.text);
  const selected = (view?.filters ?? []).filter((f) => state.filters.includes(f.name)).map((f) => f.domain);
  return and(text, or(...selected), ...state.custom);
}
