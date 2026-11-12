"""account_einvoice — UBL 2.1 / Peppol BIS Billing 3.0 e-invoicing (Phase 2, ERD_PHASE_2 §5)."""

from modules.account_einvoice import ai_explain as _ai_explain  # noqa: F401 - AI router and handler
from modules.account_einvoice import demo as _demo  # noqa: F401 - registers the demo seeding step
from modules.account_einvoice import edi as _edi  # noqa: F401 - registers the status poll job
