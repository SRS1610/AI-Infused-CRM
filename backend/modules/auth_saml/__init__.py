"""auth_saml — SAML 2.0 single sign-on (service provider side, D-013)."""

from modules.auth_saml import api as _api  # noqa: F401 - registers the router
from modules.auth_saml import service as _service  # noqa: F401 - registers the clean-up job
