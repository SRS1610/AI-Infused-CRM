"""account_bank — bank statements, statement import and bank reconciliation (Phase 2, ERD_PHASE_2 §5)."""

from modules.account_bank import api as _api  # noqa: F401 - registers the router
from modules.account_bank import demo as _demo  # noqa: F401 - registers the demo seeding step
from modules.account_bank import jobs as _jobs  # noqa: F401 - registers the auto-reconcile job
