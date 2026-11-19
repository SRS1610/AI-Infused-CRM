"""calendar — meetings, rooms, booking links and external calendar sync."""

from modules.calendar import ai_features as _ai_features  # noqa: F401 - AI handlers and the brief job
from modules.calendar import api as _api  # noqa: F401 - registers the router
from modules.calendar.sync import engine as _engine  # noqa: F401 - registers the sync job
