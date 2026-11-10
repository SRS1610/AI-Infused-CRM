"""account — invoicing and accounting (Phase 2, ERD_PHASE_2 §3)."""

from modules.account import api as _api  # noqa: F401 - registers the router
from modules.account import demo as _demo  # noqa: F401 - registers the demo seeding step
from modules.account import fx as _fx  # noqa: F401 - registers the revaluation job
