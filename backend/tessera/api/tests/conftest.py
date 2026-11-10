from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tessera.api.app import create_app
from tessera.api.middleware import limiter


@pytest.fixture(scope="session")
def app(registry):  # type: ignore[no-untyped-def]
    return create_app()


@pytest.fixture
def client(app) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    limiter._hits.clear()
    with TestClient(app, base_url="http://localhost") as c:
        yield c


class Session:
    """A logged-in user with its own cookie jar (pass ``client`` to share one deliberately)."""

    def __init__(self, client: TestClient, login: str, password: str) -> None:
        self.client = client
        resp = client.post("/api/v1/auth/login", json={"login": login, "password": password})
        assert resp.status_code == 200, resp.text
        self.payload = resp.json()
        self.csrf = self.payload["csrf_token"]

    @property
    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}

    def get(self, url: str, **kw):  # type: ignore[no-untyped-def]
        return self.client.get(url, **kw)

    def post(self, url: str, **kw):  # type: ignore[no-untyped-def]
        kw.setdefault("headers", {}).update(self.headers)
        return self.client.post(url, **kw)

    def patch(self, url: str, **kw):  # type: ignore[no-untyped-def]
        kw.setdefault("headers", {}).update(self.headers)
        return self.client.patch(url, **kw)

    def delete(self, url: str, **kw):  # type: ignore[no-untyped-def]
        kw.setdefault("headers", {}).update(self.headers)
        return self.client.delete(url, **kw)


def _own_client(app) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def admin(app, client: TestClient) -> Session:  # type: ignore[no-untyped-def]
    return Session(_own_client(app), "admin", "admin1234")


@pytest.fixture
def reader(app, client: TestClient) -> Session:  # type: ignore[no-untyped-def]
    return Session(_own_client(app), "reader", "reader1234")


@pytest.fixture
def librarian(app, client: TestClient) -> Session:  # type: ignore[no-untyped-def]
    return Session(_own_client(app), "librarian", "librarian1234")
