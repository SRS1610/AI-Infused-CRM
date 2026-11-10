"""crm — the sales pipeline."""

from modules.crm import api as _api  # noqa: F401 - registers the router
from modules.crm import hooks as _hooks  # noqa: F401 - quotation acceptance wins the opportunity
from modules.crm import services as _services  # noqa: F401 - registers the rescore job
