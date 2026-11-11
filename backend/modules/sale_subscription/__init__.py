"""sale_subscription — recurring orders, proration, renewals and churn (ERD_PHASE_2 §7, acceptance 2-f)."""

from modules.sale_subscription import api as _api  # noqa: F401 - registers the portal router
from modules.sale_subscription import billing as _billing  # noqa: F401 - registers the recurring job
from modules.sale_subscription import demo as _demo  # noqa: F401 - registers the demo seeding step
