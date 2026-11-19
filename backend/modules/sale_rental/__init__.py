"""sale_rental — rentals on sales orders: prices by duration, reservations, pickup and return, late fees
(Phase 2, ERD_PHASE_2 §7)."""

from modules.sale_rental import ai_api as _ai_api  # noqa: F401 - registers the AI router
from modules.sale_rental import ai_features as _ai_features  # noqa: F401 - AI handlers and the usage check job
from modules.sale_rental import api as _api  # noqa: F401 - registers the router
from modules.sale_rental import demo as _demo  # noqa: F401 - registers the demo seeding step
from modules.sale_rental import rental as _rental  # noqa: F401 - registers the overdue job
