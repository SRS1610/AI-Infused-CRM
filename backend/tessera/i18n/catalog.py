"""Translation catalogues: ``modules/<module>/i18n/<lang>.po`` files, merged per language.

Every module ships its own catalogue; a lookup merges the catalogues of a set of modules (the
modules installed in a database, in load order — a later module overrides an earlier one) and can
prefer one module's catalogue (the module that declared a field, for instance). Catalogues are read
with Babel once per process and cached; :func:`reset_catalogs` drops the cache (install, upgrade and
``tessera i18n load`` call it).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path

SOURCE_LANG = "en_US"
I18N_DIR = "i18n"

_lock = threading.RLock()
_file_cache: dict[Path, dict[str, str]] = {}


def read_po_file(path: Path) -> dict[str, str]:
    """``{msgid: msgstr}`` of a ``.po`` file: empty and fuzzy entries are left out."""
    from babel.messages.pofile import read_po

    with path.open("rb") as handle:
        catalog = read_po(handle)
    out: dict[str, str] = {}
    for message in catalog:
        if not message.id or message.fuzzy or not isinstance(message.id, str):
            continue
        text = message.string
        if isinstance(text, str) and text:
            out[message.id] = text
    return out


def _file(path: Path) -> dict[str, str]:
    """A catalogue file, read once per process (until :func:`reset_catalogs`)."""
    with _lock:
        cached = _file_cache.get(path)
        if cached is None:
            cached = _file_cache[path] = read_po_file(path) if path.is_file() else {}
        return cached


def module_dirs(modules: Sequence[str] | None = None) -> list[tuple[str, Path]]:
    """``(name, directory)`` of ``modules`` (in the given order), or of every module on the module path."""
    from tessera.modules.loader import discover

    manifests = discover()
    names = list(modules) if modules is not None else sorted(manifests)
    return [(name, manifests[name].path) for name in names if name in manifests]


@lru_cache(maxsize=512)
def _module_dir(name: str) -> Path | None:
    found = module_dirs([name])
    return found[0][1] if found else None


def module_catalog(module_dir: Path, lang: str) -> dict[str, str]:
    return _file(module_dir / I18N_DIR / f"{lang}.po")


def module_languages(module_dir: Path) -> list[str]:
    folder = module_dir / I18N_DIR
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.po"))


def _key(modules: Iterable[str] | None) -> tuple[str, ...] | None:
    return tuple(modules) if modules is not None else None


@lru_cache(maxsize=256)
def _languages(modules: tuple[str, ...] | None) -> frozenset[str]:
    langs: set[str] = set()
    for _name, path in module_dirs(modules):
        langs.update(module_languages(path))
    return frozenset(langs)


@lru_cache(maxsize=256)
def _merged(lang: str, modules: tuple[str, ...] | None) -> dict[str, str]:
    merged: dict[str, str] = {}
    for _name, path in module_dirs(modules):
        merged.update(module_catalog(path, lang))
    return merged


@lru_cache(maxsize=1024)
def _resolve(lang: str, modules: tuple[str, ...] | None) -> str | None:
    available = _languages(modules)
    if lang in available:
        return lang
    base = lang.replace("-", "_").split("_")[0].lower()
    candidates = sorted(code for code in available if code.split("_")[0].lower() == base)
    return candidates[0] if candidates else None


def available_languages(modules: Iterable[str] | None = None) -> list[str]:
    """Languages with at least one catalogue among ``modules`` (every module when None)."""
    return sorted(_languages(_key(modules)))


def resolve_language(lang: str | None, modules: Iterable[str] | None = None) -> str | None:
    """The catalogue language serving ``lang``: itself, else one of the same base language
    (``fr_BE`` → ``fr_FR``); None for the source language or when no catalogue exists."""
    if not lang or lang == SOURCE_LANG:
        return None
    return _resolve(lang, _key(modules))


def catalog(lang: str, modules: Iterable[str] | None = None) -> dict[str, str]:
    """Merged ``{msgid: msgstr}`` for ``lang`` over ``modules`` (later modules win)."""
    resolved = resolve_language(lang, modules)
    return _merged(resolved, _key(modules)) if resolved else {}


def find(
    msgid: str, lang: str | None, *, modules: Iterable[str] | None = None, module: str | None = None
) -> str | None:
    """The catalogue entry for ``msgid`` in ``lang`` — ``module``'s own catalogue first, then the merged
    catalogue of ``modules`` — or None when no catalogue translates it."""
    if not msgid or not lang or lang == SOURCE_LANG:
        return None
    key = _key(modules)
    resolved = _resolve(lang, key)
    if resolved is None:
        return None
    if module is not None:
        path = _module_dir(module)
        own = module_catalog(path, resolved).get(msgid) if path is not None else None
        if own:
            return own
    return _merged(resolved, key).get(msgid)


def lookup(msgid: str, lang: str | None, *, modules: Iterable[str] | None = None, module: str | None = None) -> str:
    """Translation of ``msgid`` in ``lang``; ``msgid`` itself when nothing translates it (the en_US fallback)."""
    return find(msgid, lang, modules=modules, module=module) or msgid


def reset_catalogs() -> None:
    """Forget every cached catalogue (after files changed: install, upgrade, ``tessera i18n load``)."""
    with _lock:
        _file_cache.clear()
    _languages.cache_clear()
    _merged.cache_clear()
    _resolve.cache_clear()
    _module_dir.cache_clear()
