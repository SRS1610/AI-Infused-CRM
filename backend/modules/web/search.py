"""Global search: index maintenance (hooks) and ranked, access-filtered queries.

Ranking runs on the index (pg_trgm similarity plus a prefix bonus); every candidate is then re-checked
with the caller's own access rights and record rules before it is returned, so the index never leaks a
record the caller could not open. Over-fetching (``CANDIDATES``) keeps result pages full after filtering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tessera.jobs import job
from tessera.orm import fields as F
from tessera.orm.hooks import hook

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

CANDIDATES = 200
MAX_TEXT = 4000


def searchable(records: Model) -> list[str]:
    return list(getattr(type(records), "_global_search", []) or [])


def _text_for(rec: Model, names: list[str]) -> str:
    parts: list[str] = []
    for name in names:
        f = rec._fields.get(name)
        if f is None:
            continue
        value = rec[name]
        if isinstance(f, F.Many2one | F.Many2many | F.One2many):
            parts.extend(value.mapped("display_name"))
        elif value not in (None, False, ""):
            parts.append(str(value))
    return " ".join(parts)[:MAX_TEXT]


def index_records(records: Model) -> int:
    names = searchable(records)
    if not names or not records._ids or "web.search_index" not in records.env.registry.models:
        return 0
    elevated = records.elevated("search index").with_context(active_test=False)
    rows = []
    for rec in elevated:
        company = rec.company_id.ids[0] if "company_id" in rec._fields and rec.company_id else None
        rows.append(
            {
                "model": rec._name,
                "res_id": rec.ids[0],
                "title": (rec.display_name or "")[:500],
                "content": _text_for(rec, names),
                "company_id": company,
            }
        )
    if rows:
        records.env.cr.execute(
            """INSERT INTO web_search_index (model, res_id, title, content, company_id, indexed_at)
               VALUES (:model, :res_id, :title, :content, :company_id, now())
               ON CONFLICT (model, res_id) DO UPDATE SET title = EXCLUDED.title, content = EXCLUDED.content,
                   company_id = EXCLUDED.company_id, indexed_at = now()""",
            rows,
        )
    return len(rows)


def unindex(model_name: str, ids: list[int], env: Environment) -> None:
    if ids and "web.search_index" in env.registry.models:
        env.cr.execute(
            "DELETE FROM web_search_index WHERE model = :m AND res_id = ANY(:ids)", {"m": model_name, "ids": list(ids)}
        )


def rebuild(env: Environment, model_names: list[str] | None = None, batch: int = 1000) -> dict[str, int]:
    """Re-index every searchable model (or the given ones)."""
    counts: dict[str, int] = {}
    for name, cls in env.registry.models.items():
        if model_names and name not in model_names:
            continue
        if not getattr(cls, "_global_search", None):
            continue
        env.cr.execute("DELETE FROM web_search_index WHERE model = :m", {"m": name})
        model = env[name].elevated("search rebuild").with_context(active_test=False)
        ids = model.search([]).ids
        total = 0
        for start in range(0, len(ids), batch):
            total += index_records(model.browse(ids[start : start + batch]))
        counts[name] = total
    return counts


def search(env: Environment, query: str, limit: int = 20, models: list[str] | None = None) -> list[dict[str, Any]]:
    q = " ".join(query.split())[:200]
    if len(q) < 2:
        return []
    params: dict[str, Any] = {"q": q, "pat": f"%{q}%", "prefix": f"{q}%", "n": CANDIDATES}
    where = "(content ILIKE :pat OR title ILIKE :pat OR content % :q)"
    if models:
        where += " AND model = ANY(:models)"
        params["models"] = models
    rows = env.cr.fetchall(
        f"""SELECT model, res_id, title,
                   GREATEST(similarity(title, :q), word_similarity(:q, content))
                   + CASE WHEN title ILIKE :prefix THEN 0.5 ELSE 0 END
                   + CASE WHEN title ILIKE :pat THEN 0.2 ELSE 0 END AS score
              FROM web_search_index WHERE {where}
          ORDER BY score DESC, res_id DESC LIMIT :n""",  # noqa: S608 - where clause is built from constants
        params,
    )
    by_model: dict[str, list[int]] = {}
    for model, res_id, _, _ in rows:
        by_model.setdefault(model, []).append(res_id)
    visible: dict[str, set[int]] = {}
    for model, ids in by_model.items():
        if model not in env.registry.models:
            continue
        records = env[model]
        if not records.check_access_rights("read", raise_exception=False):
            continue
        visible[model] = set(records.search([("id", "in", ids)]).ids)  # the caller's record rules apply
    out = []
    for model, res_id, title, score in rows:
        if res_id in visible.get(model, set()):
            out.append(
                {
                    "model": model,
                    "model_label": env.registry.models[model]._description,
                    "id": res_id,
                    "title": title,
                    "score": round(float(score), 3),
                }
            )
            if len(out) >= limit:
                break
    return out


@hook("after_create")
def search_on_create(records: Model, vals_list: list[dict[str, Any]]) -> None:
    if searchable(records):
        index_records(records)


@hook("after_write")
def search_on_write(records: Model, vals: dict[str, Any], old_values: dict[Any, Any]) -> None:
    names = searchable(records)
    if names and (set(vals) & {*names, "company_id", "active"} or records._rec_name in vals):
        index_records(records)


@hook("after_restore")
def search_on_restore(records: Model) -> None:
    if searchable(records):
        index_records(records)


@hook("after_unlink")
def search_on_unlink(model: Model, ids: list[int]) -> None:
    if searchable(model):
        unindex(model._name, ids, model.env)


@job("web.search_rebuild", max_attempts=1, as_system=True, timeout_seconds=3600)
def rebuild_job(env: Any, models: list[str] | None = None) -> dict[str, int]:
    return rebuild(env, models)
