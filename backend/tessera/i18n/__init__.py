"""Server-side translations (D-004: ``.po`` catalogues per module; D-009: translatable fields as JSONB).

* ``_("text {name}", name=...)`` — user-facing messages (exceptions, notifications) rendered in the
  caller's language; see :mod:`tessera.i18n.message`.
* :mod:`tessera.i18n.catalog` — ``modules/<module>/i18n/<lang>.po`` lookup with the en_US fallback.
* :mod:`tessera.i18n.extract` — builds a module's ``.pot`` from fields, views, menus, actions, data and
  ``_()`` calls (``tessera i18n extract``).
* :mod:`tessera.i18n.apply` — writes catalogue translations into the JSONB values of the module's
  translatable records (install, upgrade, ``tessera i18n load``).
"""

from tessera.i18n.catalog import (
    SOURCE_LANG,
    available_languages,
    catalog,
    find,
    lookup,
    reset_catalogs,
    resolve_language,
)
from tessera.i18n.message import (
    Message,
    _,
    current_language,
    language_scope,
    render,
    set_request_language,
    translatable,
    translate,
)

__all__ = [
    "SOURCE_LANG",
    "Message",
    "_",
    "available_languages",
    "catalog",
    "current_language",
    "find",
    "language_scope",
    "lookup",
    "render",
    "reset_catalogs",
    "resolve_language",
    "set_request_language",
    "translatable",
    "translate",
]
