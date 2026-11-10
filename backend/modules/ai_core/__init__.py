"""ai_core — the AI platform module (models, API, built-in tools and features)."""

from modules.ai_core import api as _api  # noqa: F401 - registers the router
from modules.ai_core import hooks as _hooks  # noqa: F401 - manifest loading, indexing, jobs
from modules.ai_core import tools as _tools  # noqa: F401 - built-in tools
