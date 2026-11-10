"""Server translations through the API: a French user (``marie``, lang fr_FR) gets French menus, field
labels, selection labels, view labels and error messages; English users are unchanged."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tessera.api.tests.conftest import Session

pytestmark = pytest.mark.db


@pytest.fixture
def marie(app, client: TestClient) -> Session:  # type: ignore[no-untyped-def]
    return Session(TestClient(app, base_url="http://localhost"), "marie", "demo1234")


def _menu_names(tree: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in tree:
        out[item["xmlid"]] = item["name"]
        out.update(_menu_names(item["children"]))
    return out


def _nodes(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [n for item in node for n in _nodes(item)]
    if isinstance(node, dict):
        return [node, *(n for value in node.values() for n in _nodes(value))]
    return []


def test_menus_are_translated_for_a_french_user(marie: Session, admin: Session) -> None:
    french = _menu_names(marie.get("/api/v1/web/menus").json())
    assert french["crm.menu_leads"] == "Pistes"
    assert french["crm.menu_reporting"] == "Analyse"
    english = _menu_names(admin.get("/api/v1/web/menus").json())
    assert english["crm.menu_leads"] == "Leads"
    assert english["web.menu_settings_root"] == "Settings"
    # the header overrides the user's language
    overridden = admin.get("/api/v1/web/menus", headers={"X-Tessera-Lang": "fr_FR"}).json()
    assert _menu_names(overridden)["web.menu_settings_root"] == "Paramètres"
    action = marie.get("/api/v1/web/actions/crm.action_leads").json()
    assert action["name"] == "Pistes"


def test_field_labels_selection_labels_and_help(marie: Session, admin: Session) -> None:
    fields = marie.get("/api/v1/models/crm.lead/fields").json()
    assert fields["description"] == "Piste / opportunité"
    assert fields["fields"]["expected_revenue"]["string"] == "Revenu attendu"
    assert fields["fields"]["create_date"]["string"] == "Créé le"  # kernel field, core catalogue
    assert dict(fields["fields"]["priority"]["selection"])["2"] == "Très bonne"
    stage_help = marie.get("/api/v1/models/crm.stage/fields").json()["fields"]["requirements"]["help"]
    assert stage_help == "Ce qui doit être vrai avant de déplacer une opportunité ici"
    english = admin.get("/api/v1/models/crm.lead/fields").json()
    assert english["fields"]["expected_revenue"]["string"] == "Expected revenue"
    assert dict(english["fields"]["priority"]["selection"])["2"] == "Very good"
    meta = marie.get("/api/v1/metadata/models/crm.lead").json()
    assert meta["description"] == "Piste / opportunité"
    assert meta["fields"]["probability"]["string"] == "Probabilité"


def test_views_are_translated(marie: Session, admin: Session) -> None:
    payload = marie.get("/api/v1/web/views", params={"model": "crm.lead", "types": "form,search,calendar"}).json()
    assert payload["model"]["description"] == "Piste / opportunité"
    assert payload["fields"]["expected_revenue"]["string"] == "Revenu attendu"
    labels = {n.get("method"): n.get("label") for n in _nodes(payload["views"]["form"]) if n.get("kind") == "button"}
    assert labels["action_convert"] == "Convertir en opportunité"
    pages = {n.get("name"): n.get("title") for n in _nodes(payload["views"]["form"]) if n.get("kind") == "page"}
    assert "Coordonnées" in pages.values()
    filters = {f["name"]: f["label"] for f in payload["views"]["search"]["filters"] if "label" in f}
    assert filters["mine"] == "Mon pipeline"
    # a calendar's top-level ``title`` names a field: never translated
    assert payload["views"]["calendar"]["title"] == "name"
    english = admin.get("/api/v1/web/views", params={"model": "crm.lead", "types": "form"}).json()
    labels = {n.get("method"): n.get("label") for n in _nodes(english["views"]["form"]) if n.get("kind") == "button"}
    assert labels["action_convert"] == "Convert to opportunity"
    # generated default views are translated too
    generated = marie.get("/api/v1/web/views", params={"model": "core.currency", "types": "search"}).json()
    assert {f["label"] for f in generated["views"]["search"]["filters"]} == {"Archivés"}


def test_validation_errors_are_translated(marie: Session, admin: Session) -> None:
    bad = {"values": {"name": "Translated error", "priority": "9"}}
    french = marie.post("/api/v1/models/crm.lead", json=bad)
    assert french.status_code == 422
    body = french.json()
    assert body["error"] == "ValidationError"
    assert body["message"] == "« 9 » n'est pas un choix valide pour le champ « Priorité »"
    assert body["field"] == "priority"
    english = admin.post("/api/v1/models/crm.lead", json=bad).json()
    assert english["message"] == "'9' is not a valid choice for field 'Priority'"
    # constant messages raised by the API layer
    missing = marie.get("/api/v1/models/nope.model/fields")
    assert missing.status_code == 404 and missing.json()["message"] == "Modèle « nope.model » inconnu"
    # JSON-RPC errors as well
    rpc = marie.post(
        "/api/v1/jsonrpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "create", "params": {"model": "crm.lead", "values": bad["values"]}},
    ).json()
    assert rpc["error"]["message"].startswith("« 9 » n'est pas un choix valide")


def test_anonymous_errors_follow_the_request_headers(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/login",
        json={"login": "nobody-i18n", "password": "wrong"},
        headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5"},
    )
    assert resp.status_code == 401
    assert resp.json()["message"] == "Identifiant ou mot de passe incorrect"
    english = client.post("/api/v1/auth/login", json={"login": "nobody-i18n", "password": "wrong"})
    assert english.json()["message"] == "Invalid login or password"


def test_missing_translations_fall_back_to_english(admin: Session) -> None:
    # a language without a catalogue gets English everything
    german = admin.get("/api/v1/models/crm.lead/fields", headers={"X-Tessera-Lang": "de_DE"}).json()
    assert german["fields"]["expected_revenue"]["string"] == "Expected revenue"
    # a regional variant uses the language's catalogue
    belgian = admin.get("/api/v1/models/crm.lead/fields", headers={"X-Tessera-Lang": "fr_BE"}).json()
    assert belgian["fields"]["expected_revenue"]["string"] == "Revenu attendu"
