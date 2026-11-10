"""ai_core hooks: load AI manifests of modules, keep the retrieval index fresh, register actions."""

from __future__ import annotations

from typing import Any

from tessera.ai import manifest, proposals, retrieval
from tessera.jobs import enqueue, job
from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)


def post_init(env: Any) -> None:
    """Create the gateway ledger table (shared schema ``ai_gateway``) and the global AI switch."""
    try:
        from gateway.ledger import DDL
    except ImportError:  # the gateway service creates it itself
        DDL = None  # noqa: N806
    if DDL:
        for stmt in [s.strip() for s in DDL.split(";") if s.strip()]:
            env.cr.execute(stmt)
    settings = env["ai_core.company_setting"].elevated()
    if not settings.search([("company_id", "=", False)], limit=1):
        settings.create({"company_id": None, "ai_enabled": True})
    manifest.load_installed(env)


@hook("module_installed")
def load_ai_manifest(env: Any, module: str) -> None:
    from tessera.modules.loader import discover

    if module == "ai_core" or "ai_core.feature" not in env.registry.models:
        return
    m = discover().get(module)
    if m is not None and manifest.module_ai_dir(m.path).is_dir():
        manifest.load_module(env, module, m.path)


@hook("module_upgraded")
def reload_ai_manifest(env: Any, module: str) -> None:
    from tessera.modules.loader import discover

    m = discover().get(module)
    if m is not None and manifest.module_ai_dir(m.path).is_dir() and "ai_core.feature" in env.registry.models:
        manifest.load_module(env, module, m.path)


# -- retrieval index: queued after commit so record saves never wait for an embedding call ---------


def _queue(records: Any, *, delete: list[int] | None = None) -> None:
    env = records.env
    if "core.job" not in env.registry.models:
        return
    if delete:
        retrieval.remove(env, records._name, delete)
        return
    enqueue(env, "ai_core.index_records", {"model": records._name, "ids": list(records.ids)})


@hook("after_create")
def index_on_create(records: Any, vals_list: list[dict[str, Any]]) -> None:
    if retrieval.indexed_fields(records):
        _queue(records)
    elif records._name == "chatter.message":
        targets = {m.model for m in records.elevated()}
        if any(
            m in records.env.registry.models and retrieval.indexed_fields(records.env.registry.models[m])
            for m in targets
        ):
            enqueue(records.env, "ai_core.index_messages", {"ids": list(records.ids)})


@hook("after_write")
def index_on_write(records: Any, vals: dict[str, Any], old_values: dict[Any, Any]) -> None:
    names = retrieval.indexed_fields(records)
    if names and set(vals) & {*names, "company_id", "active", type(records)._rec_name}:
        _queue(records)


@hook("after_unlink")
def index_on_unlink(model: Any, ids: list[int]) -> None:
    if retrieval.indexed_fields(model):
        retrieval.remove(model.env, model._name, ids)


@job("ai_core.index_records", queue="ai", max_attempts=5, backoff_seconds=60, as_system=True)
def index_records_job(env: Any, model: str, ids: list[int]) -> int:
    if model not in env.registry.models:
        return 0
    return retrieval.index_records(env[model].browse(ids).exists())


@job("ai_core.index_messages", queue="ai", max_attempts=5, backoff_seconds=60, as_system=True)
def index_messages_job(env: Any, ids: list[int]) -> int:
    return retrieval.index_messages(env, env["chatter.message"].browse(ids).exists())


@job("ai_core.rebuild_index", queue="ai", max_attempts=1, as_system=True, timeout_seconds=7200)
def rebuild_index_job(env: Any, models: list[str] | None = None) -> dict[str, int]:
    return retrieval.rebuild(env, models)


@job("ai_core.expire_proposals", queue="ai", max_attempts=1, as_system=True)
def expire_proposals_job(env: Any) -> int:
    return proposals.expire(env)


@job("ai_core.purge_run_content", queue="ai", max_attempts=1, as_system=True)
def purge_run_content_job(env: Any) -> int:
    """Retention: drop prompt inputs and outputs of runs older than each company's retention period
    (the audit skeleton — who, what feature, cost, decision — is kept)."""
    import datetime as dt

    settings = {
        s.company_id.ids[0] if s.company_id else None: s.retention_days
        for s in env["ai_core.company_setting"].search([])
    }
    default_days = settings.get(None) or 90
    runs = env["ai_core.run"]
    purged = 0
    companies = env["core.company"].search([]).ids
    for company in [*companies, None]:
        days = settings.get(company) or default_days
        cutoff = env.now() - dt.timedelta(days=days)
        old = runs.search(
            [("company_id", "=", company or False), ("create_date", "<", cutoff), ("content_purged", "=", False)]
        )
        if old:
            old.write(
                {"input_text": None, "output_text": None, "output_data": None, "input_ref": {}, "content_purged": True}
            )
            purged += len(old)
    return purged


@proposals.action("chatter.schedule_activity")
def schedule_activity_action(env: Any, model: str, res_id: int | None, args: dict[str, Any]) -> Any:
    from modules.chatter.thread import schedule_activity

    env[model].browse([res_id]).check_access("read")
    return schedule_activity(
        env,
        model,
        int(res_id or 0),
        "chatter.activity_type_todo",
        summary=args.get("summary"),
        date_deadline=args.get("date_deadline"),
    )
