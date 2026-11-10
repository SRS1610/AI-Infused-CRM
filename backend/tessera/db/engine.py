"""Engine management and the :class:`Cursor` transaction wrapper.

The ORM is synchronous by design (D-024): FastAPI runs ORM work in its threadpool and
keeps async for realtime and streaming. SQLAlchemy Core is used only to build SQL;
psycopg 3 executes it.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.sql.base import Executable

from tessera.config import get_settings
from tessera.logging import get_logger

log = get_logger(__name__)

_engines: dict[str, Engine] = {}
_engines_lock = threading.Lock()


def normalize_url(url: str) -> str:
    """Force the psycopg 3 driver for plain ``postgresql://`` URLs."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


def database_name(url: str) -> str:
    return normalize_url(url).rsplit("/", 1)[-1].split("?", 1)[0]


def get_engine(url: str | None = None) -> Engine:
    settings = get_settings()
    url = normalize_url(url or settings.database_url)
    with _engines_lock:
        engine = _engines.get(url)
        if engine is None:
            engine = create_engine(
                url,
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_pre_ping=True,
                future=True,
            )

            @event.listens_for(engine, "connect")
            def _on_connect(dbapi_conn: Any, _record: Any) -> None:  # pragma: no cover - driver glue
                with dbapi_conn.cursor() as cur:
                    cur.execute(f"SET statement_timeout = {settings.db_statement_timeout_ms}")
                    cur.execute("SET timezone = 'UTC'")
                try:
                    from pgvector.psycopg import register_vector

                    register_vector(dbapi_conn)
                except Exception:  # noqa: S110 - vector extension absent until ai_core needs it
                    pass

            _engines[url] = engine
    return engine


def dispose_engines() -> None:
    with _engines_lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()


class Cursor:
    """One database transaction.

    Use as a context manager: the transaction is committed on clean exit and rolled back
    on exception. ``after_commit`` callbacks run once the commit succeeded (used for
    realtime events, job enqueueing and webhooks so nothing leaks from rolled-back work).
    """

    def __init__(self, engine: Engine, *, dbname: str | None = None) -> None:
        self.engine = engine
        self.dbname = dbname or engine.url.database or ""
        self._conn: Connection = engine.connect()
        self._trans = self._conn.begin()
        self._after_commit: list[Callable[[], None]] = []
        self._undo: list[tuple[int, Callable[[], None]]] = []  # (savepoint depth, callback)
        self._savepoint_depth = 0
        self._rollback_listeners: list[weakref.WeakMethod[Callable[[], None]]] = []
        self.closed = False

    # -- execution -------------------------------------------------------------------
    def execute(
        self,
        statement: str | Executable,
        params: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    ) -> CursorResult[Any]:
        if isinstance(statement, str):
            statement = text(statement)
        return self._conn.execute(statement, params) if params is not None else self._conn.execute(statement)

    def fetchall(self, statement: str | Executable, params: Mapping[str, Any] | None = None) -> list[Any]:
        return list(self.execute(statement, params).fetchall())

    def fetchone(self, statement: str | Executable, params: Mapping[str, Any] | None = None) -> Any:
        return self.execute(statement, params).fetchone()

    def scalar(self, statement: str | Executable, params: Mapping[str, Any] | None = None) -> Any:
        return self.execute(statement, params).scalar()

    @property
    def connection(self) -> Connection:
        return self._conn

    # -- transaction control ----------------------------------------------------------
    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """Nested transaction; rolled back on exception, released otherwise."""
        nested = self._conn.begin_nested()
        self._savepoint_depth += 1
        mark = len(self._after_commit)
        try:
            yield
        except BaseException:
            if nested.is_active:
                nested.rollback()
            # work done inside the savepoint is gone: so are its after-commit effects and cached values
            del self._after_commit[mark:]
            self._run_undo(self._savepoint_depth)
            self._notify_rollback()
            raise
        else:
            if nested.is_active:
                nested.commit()
            # released: its work now belongs to the enclosing level, and so do its undo callbacks (left at
            # this depth, a later sibling savepoint's rollback would run them)
            parent = self._savepoint_depth - 1
            self._undo = [(min(d, parent), cb) for d, cb in self._undo]
        finally:
            self._savepoint_depth -= 1

    def on_savepoint_rollback(self, method: Callable[[], None]) -> None:
        """Register a bound method called when a savepoint rolls back (held weakly)."""
        self._rollback_listeners = [ref for ref in self._rollback_listeners if ref() is not None]
        self._rollback_listeners.append(weakref.WeakMethod(method))

    def _notify_rollback(self) -> None:
        for ref in list(self._rollback_listeners):
            listener = ref()
            if listener is not None:
                listener()

    def after_commit(self, callback: Callable[[], None]) -> None:
        self._after_commit.append(callback)

    def on_rollback(self, callback: Callable[[], None]) -> None:
        """Call ``callback`` if the work done so far is rolled back (the whole transaction, or the
        savepoint the call happens in). Used to undo in-memory state tied to the transaction."""
        self._undo.append((self._savepoint_depth, callback))

    def _run_undo(self, depth: int) -> None:
        keep: list[tuple[int, Callable[[], None]]] = []
        run: list[tuple[int, Callable[[], None]]] = []
        for d, cb in self._undo:
            (run if d >= depth else keep).append((d, cb))
        self._undo = keep
        for _, cb in reversed(run):
            try:
                cb()
            except Exception:
                log.exception("rollback callback failed")

    def commit(self) -> None:
        if self._trans.is_active:
            self._trans.commit()
        self._undo.clear()
        callbacks, self._after_commit = self._after_commit, []
        for cb in callbacks:
            try:
                cb()
            except Exception:
                log.exception("after_commit callback failed")
        self._trans = self._conn.begin()

    def rollback(self) -> None:
        if self._trans.is_active:
            self._trans.rollback()
        self._after_commit.clear()
        self._run_undo(0)
        self._trans = self._conn.begin()

    def close(self) -> None:
        if self.closed:
            return
        try:
            if self._trans.is_active:
                self._trans.rollback()
                self._run_undo(0)
        finally:
            self._conn.close()
            self.closed = True

    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()


@contextmanager
def cursor(url: str | None = None) -> Iterator[Cursor]:
    cr = Cursor(get_engine(url))
    try:
        yield cr
        cr.commit()
    except BaseException:
        cr.rollback()
        raise
    finally:
        cr.close()
