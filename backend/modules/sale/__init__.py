"""sale — quotations and sales orders: pricing, taxes, templates, promotions, invoicing (D-011, D-074)."""

from modules.sale import ai_api as _ai_api  # noqa: F401 - AI routes: upsell, discounts, pricing flags
from modules.sale import ai_demo as _ai_demo  # noqa: F401 - AI demo: bands and test-provider answers
from modules.sale import ai_pricing as _ai_pricing  # noqa: F401 - pricing check when a quotation is sent/confirmed
from modules.sale import ai_quote_text as _ai_quote_text  # noqa: F401 - AI handler: lines from pasted text
from modules.sale import ai_upsell as _ai_upsell  # noqa: F401 - AI handler: upsell explanations
from modules.sale import api as _api  # noqa: F401 - registers the router
from modules.sale import demo as _demo  # noqa: F401 - registers the demo seeding step
