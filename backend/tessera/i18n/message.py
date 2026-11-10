"""Translatable server messages.

``_("Field '{field}' is required", field=label)`` marks a user-facing text for extraction and returns
a :class:`Message`: a ``str`` rendered in the current language (the request's language inside an API
request, English elsewhere) that also remembers its ``msgid`` and parameters, so it can be rendered
again in another language — the API error handlers render exception messages in the caller's
language, notifications in each recipient's language.

Placeholders use ``str.format`` syntax (``{name}``) and must be kept verbatim in translations.
Parameters that are themselves messages are translated too. Log messages are never translated.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from tessera.i18n.catalog import SOURCE_LANG, lookup


@dataclass
class _RequestLanguage:
    """Mutable per-request holder: set by the API middleware, filled once the caller is known (the
    dependency that authenticates runs in a worker thread, whose context changes would be lost)."""

    lang: str | None = None
    modules: tuple[str, ...] | None = None


_language: contextvars.ContextVar[str | None] = contextvars.ContextVar("tessera_lang", default=None)
_request: contextvars.ContextVar[_RequestLanguage | None] = contextvars.ContextVar("tessera_req_lang", default=None)


def current_language() -> str | None:
    """Language messages are rendered in: an explicit :func:`language_scope`, else the request's."""
    lang = _language.get()
    if lang:
        return lang
    holder = _request.get()
    return holder.lang if holder is not None else None


def current_modules() -> tuple[str, ...] | None:
    holder = _request.get()
    return holder.modules if holder is not None else None


@contextmanager
def language_scope(lang: str | None) -> Iterator[None]:
    """Render messages created inside the block in ``lang`` (jobs, notifications, CLI)."""
    token = _language.set(lang)
    try:
        yield
    finally:
        _language.reset(token)


def begin_request() -> contextvars.Token[_RequestLanguage | None]:
    """Called by the API middleware for every request (returns the token for :func:`end_request`)."""
    return _request.set(_RequestLanguage())


def end_request(token: contextvars.Token[_RequestLanguage | None]) -> None:
    _request.reset(token)


def set_request_language(lang: str | None, modules: Iterable[str] | None = None) -> None:
    """Record the authenticated caller's language for the rest of the request."""
    holder = _request.get()
    if holder is not None:
        holder.lang = lang
        holder.modules = tuple(modules) if modules is not None else None


def _format(template: str, params: dict[str, Any]) -> str:
    return template.format(**params) if params else template


def render(msgid: str, params: dict[str, Any], lang: str | None, *, modules: Iterable[str] | None = None) -> str:
    """``msgid`` translated into ``lang`` with ``params`` substituted; parameters that are messages are
    translated as well (plain parameters are data and stay as they are)."""
    values = {k: v.in_language(lang, modules=modules) if isinstance(v, Message) else v for k, v in params.items()}
    template = lookup(msgid, lang, modules=modules) if lang and lang != SOURCE_LANG else msgid
    try:
        return _format(template, values)
    except (KeyError, IndexError, ValueError, AttributeError):
        pass  # a translation with broken placeholders: fall back to the source text
    try:
        return _format(msgid, values)
    except (KeyError, IndexError, ValueError, AttributeError):
        return msgid


class Message(str):
    """A rendered message that can be rendered again in another language (see :func:`_`)."""

    msgid: str
    params: dict[str, Any]

    def __new__(cls, msgid: str, params: dict[str, Any] | None = None, *, lang: str | None = None) -> Message:
        params = dict(params or {})
        text = render(msgid, params, lang if lang is not None else current_language(), modules=current_modules())
        obj = super().__new__(cls, text)
        obj.msgid = msgid
        obj.params = params
        return obj

    def in_language(self, lang: str | None, *, modules: Iterable[str] | None = None) -> str:
        return render(self.msgid, self.params, lang, modules=modules)

    def __reduce__(self) -> tuple[Any, ...]:  # pickled / deep-copied as the plain rendered text
        return (str, (str(self),))


def _(msgid: str, /, **params: Any) -> Message:
    """Mark ``msgid`` as a translatable message and render it (``{name}`` placeholders from ``params``)."""
    return Message(msgid, params)


def translatable(text: str) -> Message:
    """A text decided at run time (a field label, a selection label) that catalogues may translate.
    Unlike :func:`_`, the extractor does not collect it: the text must come from an extracted source."""
    return Message(text, {})


def translate(value: Any, lang: str | None, *, modules: Iterable[str] | None = None, module: str | None = None) -> Any:
    """``value`` in ``lang``: a :class:`Message` is rendered again; a plain string is looked up as a msgid
    (constant messages, labels); anything else is returned unchanged."""
    if isinstance(value, Message):
        return value.in_language(lang, modules=modules)
    if isinstance(value, str):
        return lookup(value, lang, modules=modules, module=module) if lang and lang != SOURCE_LANG else value
    return value
