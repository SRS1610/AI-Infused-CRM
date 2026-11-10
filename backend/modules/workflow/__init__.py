"""workflow — state machines and automated actions for any model."""

from modules.workflow import api as _api  # noqa: F401 - registers the router
from modules.workflow import hooks as _hooks  # noqa: F401 - registers write guards and automation triggers
from modules.workflow import jobs as _jobs  # noqa: F401 - registers jobs
