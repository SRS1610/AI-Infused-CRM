"""account_asset — fixed assets and depreciation boards (ERD_PHASE_2 §6, acceptance 2-g)."""

from modules.account_asset import ai_features as _ai_features  # noqa: F401 - AI router, handler and action
from modules.account_asset import demo as _demo  # noqa: F401 - registers the demo seeding step
from modules.account_asset import services as _services  # noqa: F401 - registers the depreciation job
