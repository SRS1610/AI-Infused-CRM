"""discuss — team chat on the same message engine as record threads."""

from modules.discuss import ai_api as _ai_api  # noqa: F401 - AI summary / catch-up endpoints
from modules.discuss import ai_features as _ai_features  # noqa: F401 - AI feature handlers and jobs
from modules.discuss import api as _api  # noqa: F401 - registers the router
from modules.discuss import hooks as _hooks  # noqa: F401 - unread counters and member notifications
