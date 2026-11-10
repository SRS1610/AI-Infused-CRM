"""account_dunning — payment reminders (Phase 2, ERD_PHASE_2 §5)."""

from modules.account_dunning import demo as _demo  # noqa: F401 - registers the demo seeding step
from modules.account_dunning import dunning as _dunning  # noqa: F401 - registers the reminder job
