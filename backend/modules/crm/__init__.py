"""crm — the sales pipeline."""

from modules.crm import api as _api  # noqa: F401 - registers the router
from modules.crm import hooks as _hooks  # noqa: F401 - quotation acceptance wins the opportunity
from modules.crm import services as _services  # noqa: F401 - registers the rescore job
from modules.crm import ai_insights as _ai_insights, ai_insights_api as _ai_insights_api  # noqa: F401 - AI insights
from modules.crm import ai_assist as _ai_assist  # noqa: F401 - AI assist: extraction, drafts, calls, objections, search
