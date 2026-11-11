"""account_reports — financial reports computed from journal items (ERD_PHASE_2 §6)."""

from modules.account_reports import ai_features as _ai_features  # noqa: F401 - AI handler
from modules.account_reports import api as _api  # noqa: F401 - registers the router
from modules.account_reports import demo as _demo  # noqa: F401 - registers the demo seeding step
