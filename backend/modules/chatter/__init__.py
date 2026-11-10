"""chatter — threads, notes, followers, tracking, activities on every business record."""

from modules.chatter import ai_todo as _ai_todo  # noqa: F401 - AI-ranked daily to-do (API, morning job)
from modules.chatter import api as _api  # noqa: F401 - registers the router
from modules.chatter import hooks as _hooks  # noqa: F401 - registers tracking / cleanup hooks
from modules.chatter import thread as _thread  # noqa: F401 - declares the chatter.thread mixin
