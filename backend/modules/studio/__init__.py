"""studio — no-code customisation (D-012)."""

from modules.studio import api as _api  # noqa: F401 - registers the router
from modules.studio import hooks as _hooks  # noqa: F401 - Studio fields join the registry
