"""account — invoicing and accounting (Phase 2, ERD_PHASE_2 §3)."""

from modules.account import ai_anomaly as _ai_anomaly  # noqa: F401 - anomaly job, hooks and explanation handler
from modules.account import ai_api as _ai_api  # noqa: F401 - registers the AI routes
from modules.account import ai_bill as _ai_bill  # noqa: F401 - document reading and its proposal action
from modules.account import ai_cashflow as _ai_cashflow  # noqa: F401 - cash-flow narrative handler
from modules.account import ai_demo as _ai_demo  # noqa: F401 - AI demo step (imports demo first: runs after it)
from modules.account import api as _api  # noqa: F401 - registers the router
from modules.account import demo as _demo  # noqa: F401 - registers the demo seeding step
from modules.account import fx as _fx  # noqa: F401 - registers the revaluation job
