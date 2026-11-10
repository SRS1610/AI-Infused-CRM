"""sale — quotations and sales orders: pricing, taxes, templates, promotions, invoicing (D-011, D-074)."""

from modules.sale import api as _api  # noqa: F401 - registers the router
from modules.sale import demo as _demo  # noqa: F401 - registers the demo seeding step
