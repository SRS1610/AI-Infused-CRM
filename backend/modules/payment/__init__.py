"""payment — payment providers, transactions and portal invoice payment (Phase 2, ERD_PHASE_2 §5)."""

from modules.payment import ai_failures as _ai_failures  # noqa: F401 - AI router and handler
from modules.payment import api as _api  # noqa: F401 - registers the router
from modules.payment import demo as _demo  # noqa: F401 - registers the demo seeding step
