// Helpers for the nested-JSON domain DSL (backend: tessera/orm/domain.py).
import type { Domain } from "./api/types";

export type Leaf = [string, string, unknown?];

export function isEmpty(domain: Domain | null | undefined): boolean {
  if (domain === null || domain === undefined) return true;
  if (Array.isArray(domain)) return domain.length === 0;
  if (typeof domain === "object" && "all" in (domain as any)) return ((domain as any).all as unknown[]).length === 0;
  return false;
}

const isLeaf = (d: unknown): boolean =>
  Array.isArray(d) && d.length >= 2 && typeof d[0] === "string" && typeof d[1] === "string";

/** Children of an implicit/explicit "all" (a bare list of leaves/nodes is an implicit all). */
function allChildren(d: Domain): Domain[] | null {
  if (Array.isArray(d) && !isLeaf(d)) return d as Domain[];
  if (d && typeof d === "object" && !Array.isArray(d) && "all" in (d as any)) return (d as any).all as Domain[];
  return null;
}

export function and(...parts: (Domain | null | undefined)[]): Domain {
  const flat: Domain[] = [];
  for (const p of parts) {
    if (isEmpty(p)) continue;
    const children = allChildren(p as Domain);
    if (children) flat.push(...children.filter((c) => !isEmpty(c)));
    else flat.push(p as Domain);
  }
  if (flat.length === 0) return [];
  if (flat.length === 1) return flat[0];
  return { all: flat };
}

export function or(...parts: (Domain | null | undefined)[]): Domain {
  const kept = parts
    .filter((p) => !isEmpty(p))
    .map((p) => {
      const children = allChildren(p as Domain);
      return children && children.length === 1 ? children[0] : (p as Domain);
    });
  if (kept.length === 0) return [];
  if (kept.length === 1) return kept[0];
  return { any: kept };
}

export function textSearch(fields: string[], text: string): Domain {
  const q = text.trim();
  if (!q) return [];
  return or(...fields.map((f) => [f, "ilike", q] as Leaf));
}

/** Substitute `$field` placeholders with values from a record (used by view-level domains). */
export function bindRecord(domain: Domain, record: Record<string, any>): Domain {
  const visit = (node: any): any => {
    if (Array.isArray(node)) {
      if (node.length >= 2 && typeof node[0] === "string" && typeof node[1] === "string") {
        const value = node[2];
        if (typeof value === "string" && value.startsWith("$") && value.slice(1) in record) {
          const v = record[value.slice(1)];
          return [node[0], node[1], v && typeof v === "object" && "id" in v ? v.id : v];
        }
        return node;
      }
      return node.map(visit);
    }
    if (node && typeof node === "object")
      return Object.fromEntries(Object.entries(node).map(([k, v]) => [k, visit(v)]));
    return node;
  };
  return visit(domain);
}

/** Minimal in-browser evaluation for view modifiers (invisible/readonly) over simple leaves. */
export function evaluate(domain: Domain | undefined | null, record: Record<string, any>): boolean {
  if (domain === undefined || domain === null) return false;
  if (typeof domain === "boolean") return domain;
  const value = (path: string) => {
    let v: any = record;
    for (const seg of path.split("."))
      v =
        v && typeof v === "object"
          ? seg in v
            ? v[seg]
            : v.id !== undefined && seg === "id"
              ? v.id
              : undefined
          : undefined;
    if (v && typeof v === "object" && "id" in v && !Array.isArray(v)) return v.id;
    return v;
  };
  const leaf = ([path, op, expected]: any[]): boolean => {
    const actual = value(path);
    switch (op) {
      case "=":
        return (actual ?? false) === (expected ?? false) || actual === expected;
      case "!=":
        return !((actual ?? false) === (expected ?? false));
      case "in":
        return Array.isArray(expected) && expected.includes(actual);
      case "not_in":
        return Array.isArray(expected) && !expected.includes(actual);
      case ">":
        return actual > expected;
      case ">=":
        return actual >= expected;
      case "<":
        return actual < expected;
      case "<=":
        return actual <= expected;
      case "ilike":
      case "not_ilike": {
        const found = String(actual ?? "")
          .toLowerCase()
          .includes(String(expected ?? "").toLowerCase());
        return op === "ilike" ? actual !== null && actual !== undefined && actual !== false && found : !found;
      }
      case "is_set":
        return (
          actual !== null && actual !== undefined && actual !== false && !(Array.isArray(actual) && actual.length === 0)
        );
      case "is_not_set":
        return (
          actual === null || actual === undefined || actual === false || (Array.isArray(actual) && actual.length === 0)
        );
      default:
        return false;
    }
  };
  const visit = (node: any): boolean => {
    if (Array.isArray(node)) {
      if (node.length >= 2 && typeof node[0] === "string" && typeof node[1] === "string") return leaf(node);
      return node.every(visit);
    }
    if (node && typeof node === "object") {
      if ("all" in node) return (node.all as any[]).every(visit);
      if ("any" in node) return (node.any as any[]).some(visit);
      if ("not" in node) return !visit(node.not);
    }
    return false;
  };
  return visit(domain);
}
