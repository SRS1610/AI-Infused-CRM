"""account_budget — budgets and budget-vs-actual (ERD_PHASE_2 §6)."""

from modules.account_budget import ai_features as _ai_features  # noqa: F401 - AI handler
from modules.account_budget import demo as _demo  # noqa: F401 - registers the demo seeding step
from modules.account_budget import services as _services  # noqa: F401 - registers the refresh job
