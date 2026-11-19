"""Per-request / per-job runtime context (contextvars) shared by API, jobs, audit and logging."""

from __future__ import annotations

import contextvars
import uuid

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("tessera_request_id", default=None)
remote_ip_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("tessera_remote_ip", default=None)
user_agent_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("tessera_user_agent", default=None)
# browser tab id (X-Tessera-Client) so a tab can ignore realtime echoes of its own writes
client_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("tessera_client_id", default=None)


def new_request_id() -> str:
    rid = uuid.uuid4().hex
    request_id_var.set(rid)
    return rid


def current_request_id() -> str | None:
    return request_id_var.get()


def current_remote_ip() -> str | None:
    return remote_ip_var.get()
