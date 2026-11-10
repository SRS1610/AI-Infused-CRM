from collections.abc import Iterator

import pytest

from tessera.api.middleware import limiter
from tessera.api.tests.conftest import Session, admin, app, client, librarian, reader  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh_rate_limits() -> Iterator[None]:
    """Every test client shares one address: each test starts with an empty login budget."""
    limiter._hits.clear()
    yield
    limiter._hits.clear()
