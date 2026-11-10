"""The :class:`Environment`: who is acting, on which database, in which context.

An environment is cheap and immutable; ``with_user`` / ``with_context`` / ``with_company``
return siblings that share the same transaction (cursor, cache, pending recomputations).
Elevation (``elevated()``) is explicit and logged — it is for kernel/platform code only.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from tessera.db.engine import Cursor, get_engine
from tessera.exceptions import MissingError
from tessera.logging import get_logger
from tessera.orm.cache import Cache

if TYPE_CHECKING:
    from tessera.orm.model import Model
    from tessera.orm.registry import Registry

log = get_logger(__name__)

SYSTEM_UID = 1
ADMIN_UID = 2


class Transaction:
    """State shared by every environment of one database transaction."""

    def __init__(self, registry: Registry, cr: Cursor) -> None:
        self.registry = registry
        self.cr = cr
        self.cache = Cache()
        # stored computed fields waiting for recomputation: (model, field) -> set(ids)
        self.tocompute: dict[tuple[str, str], set[int]] = defaultdict(set)
        # fields being written through an inverse (skip recompute loops): (model, field) -> ids
        self.protected: dict[tuple[str, str], set[int]] = defaultdict(set)
        self.envs: list[Environment] = []
        # external-id cache: (module, name) -> (model, res_id)
        self.xmlid_cache: dict[str, tuple[str, int]] = {}
        cr.on_savepoint_rollback(self._savepoint_rolled_back)

    def _savepoint_rolled_back(self) -> None:
        self.cache.clear()
        self.xmlid_cache.clear()

    def reset(self) -> None:
        self.cache.clear()
        self.tocompute.clear()
        self.protected.clear()
        self.xmlid_cache.clear()


class Environment:
    __slots__ = ("_companies_cache", "_user_cache", "context", "su", "transaction", "uid")

    def __init__(
        self, transaction: Transaction, uid: int, context: Mapping[str, Any] | None = None, su: bool = False
    ) -> None:
        self.transaction = transaction
        self.uid = uid
        self.context: dict[str, Any] = dict(context or {})
        self.su = su
        self._user_cache: Model | None = None
        self._companies_cache: tuple[int, ...] | None = None
        transaction.envs.append(self)

    # -- construction ------------------------------------------------------------------
    @classmethod
    def new(
        cls,
        registry: Registry,
        cr: Cursor,
        uid: int = SYSTEM_UID,
        context: Mapping[str, Any] | None = None,
        su: bool = False,
    ) -> Environment:
        return cls(Transaction(registry, cr), uid, context, su)

    def _derive(
        self, *, uid: int | None = None, context: Mapping[str, Any] | None = None, su: bool | None = None
    ) -> Environment:
        env = Environment(
            self.transaction,
            self.uid if uid is None else uid,
            self.context if context is None else context,
            self.su if su is None else su,
        )
        return env

    def with_user(self, user: int | Model) -> Environment:
        uid = user if isinstance(user, int) else user._single_id()
        ctx = {k: v for k, v in self.context.items() if k not in ("allowed_company_ids", "company_id")}
        return self._derive(uid=uid, context=ctx, su=False)

    def with_context(self, **values: Any) -> Environment:
        return self._derive(context={**self.context, **values})

    def with_company(self, company: int | Model, allowed: list[int] | None = None) -> Environment:
        cid = company if isinstance(company, int) else company._single_id()
        allowed_ids = list(allowed) if allowed is not None else list(dict.fromkeys([cid, *self.companies.ids]))
        return self.with_context(company_id=cid, allowed_company_ids=allowed_ids)

    def elevated(self, reason: str = "") -> Environment:
        """Bypass access rights and record rules. Kernel/platform code only; always logged."""
        if not self.su:
            log.debug("environment elevated", uid=self.uid, reason=reason)
        return self._derive(su=True)

    # -- accessors ---------------------------------------------------------------------
    @property
    def registry(self) -> Registry:
        return self.transaction.registry

    @property
    def cr(self) -> Cursor:
        return self.transaction.cr

    @property
    def cache(self) -> Cache:
        return self.transaction.cache

    @property
    def is_system(self) -> bool:
        return self.su or self.uid == SYSTEM_UID

    def __getitem__(self, model_name: str) -> Model:
        cls = self.registry.models.get(model_name)
        if cls is None:
            raise MissingError(f"Unknown model '{model_name}'", model=model_name)
        return cls._browse(self, ())

    def __contains__(self, model_name: str) -> bool:
        return model_name in self.registry.models

    @property
    def user(self) -> Model:
        if self._user_cache is None:
            self._user_cache = self["core.user"].browse([self.uid])
        return self._user_cache

    @property
    def company(self) -> Model:
        cid = self.context.get("company_id")
        companies = self["core.company"]
        if cid:
            return companies.browse([int(cid)])
        allowed = self.companies
        if allowed:
            return companies.browse([allowed.ids[0]])
        return companies.browse([])

    @property
    def companies(self) -> Model:
        companies = self["core.company"]
        allowed = self.context.get("allowed_company_ids")
        if allowed:
            return companies.browse([int(c) for c in allowed])
        if self._companies_cache is None:
            try:
                user = self.user.elevated()
                ids = tuple(user.company_ids.ids) if user.exists() else ()
                main = user.company_id.ids if user.exists() else []
                self._companies_cache = tuple(dict.fromkeys([*main, *ids]))
            except Exception:
                self._companies_cache = ()
        return companies.browse(list(self._companies_cache))

    @property
    def lang(self) -> str:
        lang = self.context.get("lang")
        if lang:
            return str(lang)
        try:
            user_lang = self.user.elevated().lang if self.uid else None
        except Exception:
            user_lang = None
        return str(user_lang or self.registry.default_lang)

    @property
    def tz(self) -> ZoneInfo:
        name = self.context.get("tz")
        if not name:
            try:
                name = self.user.elevated().tz
            except Exception:
                name = None
        try:
            return ZoneInfo(str(name or "UTC"))
        except Exception:
            return ZoneInfo("UTC")

    @property
    def active_test(self) -> bool:
        return bool(self.context.get("active_test", True))

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC).replace(microsecond=0)

    def today(self) -> dt.date:
        return dt.datetime.now(self.tz).date()

    # -- external ids ----------------------------------------------------------------------
    def ref(self, external_id: str, raise_if_not_found: bool = True) -> Model | None:
        """Resolve ``module.name`` to a record via ``core.module_record``."""
        found = self.transaction.xmlid_cache.get(external_id)
        if found is None:
            module, _, name = external_id.partition(".")
            if not name:
                raise MissingError(f"External id '{external_id}' must be 'module.name'")
            row = self.cr.fetchone(
                "SELECT model, res_id FROM core_module_record WHERE module = :module AND name = :name",
                {"module": module, "name": name},
            )
            if row is None:
                if raise_if_not_found:
                    raise MissingError(f"External id '{external_id}' not found", external_id=external_id)
                return None
            found = (row[0], row[1])
            self.transaction.xmlid_cache[external_id] = found
        model_name, res_id = found
        return self[model_name].browse([res_id])

    def set_ref(self, external_id: str, record: Model, noupdate: bool = False) -> None:
        module, _, name = external_id.partition(".")
        self.cr.execute(
            """INSERT INTO core_module_record (module, name, model, res_id, noupdate, create_date, write_date)
               VALUES (:module, :name, :model, :res_id, :noupdate, now(), now())
               ON CONFLICT (module, name) DO UPDATE SET model = EXCLUDED.model, res_id = EXCLUDED.res_id,
                                                        noupdate = EXCLUDED.noupdate, write_date = now()""",
            {
                "module": module,
                "name": name,
                "model": record._name,
                "res_id": record._single_id(),
                "noupdate": noupdate,
            },
        )
        self.transaction.xmlid_cache[external_id] = (record._name, record._single_id())

    # -- recomputation ---------------------------------------------------------------------
    def add_to_compute(self, model_name: str, field_name: str, ids: Any) -> None:
        self.transaction.tocompute[(model_name, field_name)].update(ids)

    def flush(self) -> None:
        """Recompute every pending stored computed field."""
        from tessera.orm.model import Model as _Model  # noqa: F401 - typing only

        pending = self.transaction.tocompute
        guard = 0
        while pending:
            guard += 1
            if guard > 50:
                raise RuntimeError("Recomputation did not converge (circular dependency?)")
            (model_name, field_name), ids = next(iter(pending.items()))
            del pending[(model_name, field_name)]
            if not ids:
                continue
            records = self[model_name].elevated().browse(sorted(ids))
            records._recompute_field(field_name)

    def invalidate_all(self) -> None:
        self.transaction.reset()

    def __repr__(self) -> str:
        return f"<Environment uid={self.uid} su={self.su} db={self.cr.dbname}>"


@contextmanager
def environment_for(
    database_url: str | None = None,
    uid: int = SYSTEM_UID,
    context: Mapping[str, Any] | None = None,
    *,
    su: bool = False,
) -> Iterator[Environment]:
    """Open a transaction on ``database_url`` and yield an environment; commit on success."""
    from tessera.config import get_settings
    from tessera.orm.registry import current_registry

    url = database_url or get_settings().database_url
    cr = Cursor(get_engine(url))
    registry = current_registry(url, cr)
    env = Environment.new(registry, cr, uid, context, su)
    try:
        yield env
        env.flush()
        cr.commit()
    except BaseException:
        cr.rollback()
        raise
    finally:
        cr.close()
