"""Contacts: hierarchy, normalisation, duplicates, merge with re-pointing and undo (1.4-b)."""

from __future__ import annotations

from typing import Any

import pytest

from modules.contacts.merge import UNDO_WINDOW
from tessera.exceptions import UserError, ValidationError

pytestmark = pytest.mark.db


def test_hierarchy_display_name_and_commercial_partner(env: Any) -> None:
    partners = env["contacts.partner"]
    company = partners.create({"name": "Northwind", "is_company": True, "city": "Seattle", "zip": "98101"})
    person = partners.create({"name": "Ada", "parent_id": company.ids[0]})
    address = partners.create({"parent_id": company.ids[0], "type": "invoice", "city": "Portland"})
    assert person.display_name == "Northwind, Ada" and address.display_name == "Northwind, Invoice address"
    assert person.city == "Seattle" and person.zip == "98101"  # inherits the company address
    assert person.commercial_partner_id == company and company.commercial_partner_id == company
    with pytest.raises(ValidationError):
        partners.create({"is_company": True})
    with pytest.raises(ValidationError):
        company.write({"parent_id": person.ids[0]})  # cycle
    company.write({"name": "Northwind Traders"})
    assert person.display_name == "Northwind Traders, Ada"


def test_normalisation_and_duplicates(env: Any) -> None:
    partners = env["contacts.partner"]
    a = partners.create({"name": "Grace Hopper", "email": "Grace <GRACE@navy.example>", "phone": "+1 (202) 555-0199"})
    b = partners.create({"name": "G. Hopper", "email": "grace@navy.example"})
    c = partners.create({"name": "Admiral", "mobile": "202-555-0199"})
    d = partners.create({"name": "grace hopper"})
    assert a.email_normalized == "grace@navy.example" and a.phone_normalized == "025550199"
    found = {row["id"]: row["reasons"] for row in a.find_duplicates()}
    assert found == {b.ids[0]: ["email"], c.ids[0]: ["phone"], d.ids[0]: ["name"]}


def test_users_get_a_contact(env: Any) -> None:
    user = env["core.user"].create({"login": "contact-user", "name": "Linus", "email": "linus@example.test"})
    assert user.partner_id.name == "Linus" and user.partner_id.email == "linus@example.test"
    user.write({"email": "linus@new.example"})
    assert user.partner_id.email == "linus@new.example"
    admin = env.ref("core.user_admin")
    assert admin.partner_id  # created at install for existing users


@pytest.fixture
def dupes(env: Any) -> Any:
    partners = env["contacts.partner"]
    keep = partners.create({"name": "Contoso", "is_company": True, "email": "info@contoso.example"})
    dup1 = partners.create({"name": "Contoso Ltd", "is_company": True, "phone": "+44 20 7946 0001", "vat": "GB123"})
    dup2 = partners.create({"name": "CONTOSO", "is_company": True, "city": "London"})
    employee = partners.create({"name": "Pat", "parent_id": dup1.ids[0]})
    tag = env["contacts.tag"].create({"name": "Merge me"})
    dup2.write({"tag_ids": [("set", tag.ids)]})
    book1 = env["ktest.book"].create(
        {"name": "Pub 1", "publisher_id": dup1.ids[0], "reviewer_ids": [("set", [dup1.ids[0], keep.ids[0]])]}
    )
    book2 = env["ktest.book"].create(
        {"name": "Pub 2", "publisher_id": dup2.ids[0], "reviewer_ids": [("set", [dup2.ids[0]])]}
    )
    dup1.message_post("history on dup1")
    dup2.activity_schedule(summary="call contoso")
    att = env["core.attachment"].create(
        {"name": "contract.pdf", "raw": b"%PDF-1", "model": "contacts.partner", "res_id": dup2.ids[0]}
    )
    return locals()


def test_merge_repoints_everything_and_preserves_history(env: Any, dupes: Any) -> None:
    """1.4-b: three duplicates → every many2one/many2many/generic reference moves to the survivor."""
    keep, dup1, dup2 = dupes["keep"], dupes["dup1"], dupes["dup2"]
    result = (keep | dup1 | dup2).action_merge(keep.ids[0], {"name": dup1.ids[0]})
    env.cache.invalidate()
    assert result["survivor_id"] == keep.ids[0]
    assert keep.name == "Contoso Ltd"  # chosen value
    assert keep.phone == "+44 20 7946 0001" and keep.vat == "GB123" and keep.city == "London"  # blanks filled
    assert keep.email == "info@contoso.example"  # own non-blank value kept
    assert dupes["employee"].parent_id == keep and dupes["employee"].display_name == "Contoso Ltd, Pat"
    assert dupes["book1"].publisher_id == keep and dupes["book2"].publisher_id == keep
    assert set(dupes["book1"].reviewer_ids.ids) == {keep.ids[0]} and dupes["book2"].reviewer_ids == keep
    assert keep.tag_ids == dupes["tag"]
    messages = (
        env["chatter.message"].elevated().search([("model", "=", "contacts.partner"), ("res_id", "=", keep.ids[0])])
    )
    assert any("history on dup1" in (m.body or "") for m in messages)
    assert any("Merged" in (m.body or "") for m in messages)
    assert (
        env["chatter.activity"]
        .elevated()
        .search([("res_id", "=", keep.ids[0]), ("model", "=", "contacts.partner")])
        .summary
        == "call contoso"
    )
    assert dupes["att"].res_id == keep.ids[0]
    assert not dup1.active and not dup2.active


def test_merge_undo_restores_everything(env: Any, dupes: Any) -> None:
    keep, dup1, dup2 = dupes["keep"], dupes["dup1"], dupes["dup2"]
    merge_id = (keep | dup1 | dup2).action_merge(keep.ids[0], {"name": dup1.ids[0]})["merge_id"]
    log = env["contacts.merge_log"].browse([merge_id])
    log.action_undo()
    env.cache.invalidate()
    assert dup1.active and dup2.active
    assert keep.name == "Contoso" and not keep.phone and not keep.city
    assert dupes["employee"].parent_id == dup1
    assert dupes["book1"].publisher_id == dup1 and dupes["book2"].publisher_id == dup2
    assert set(dupes["book1"].reviewer_ids.ids) == {dup1.ids[0], keep.ids[0]} and dupes["book2"].reviewer_ids == dup2
    assert dup2.tag_ids == dupes["tag"] and not keep.tag_ids
    assert dupes["att"].res_id == dup2.ids[0]
    with pytest.raises(UserError, match="already"):
        log.action_undo()


def test_undo_window(env: Any, dupes: Any) -> None:
    keep, dup1 = dupes["keep"], dupes["dup1"]
    merge_id = (keep | dup1).action_merge(keep.ids[0])["merge_id"]
    env.cr.execute(
        "UPDATE contacts_merge_log SET create_date = now() - :w - interval '1 hour' WHERE id = :id",
        {"w": UNDO_WINDOW, "id": merge_id},
    )
    env.cache.invalidate()
    with pytest.raises(UserError, match="undo window"):
        env["contacts.merge_log"].browse([merge_id]).action_undo()


def test_merge_guards(env: Any, dupes: Any) -> None:
    with pytest.raises(UserError):
        dupes["keep"].action_merge(dupes["keep"].ids[0])
    with pytest.raises(UserError):  # a company cannot disappear into its own employee
        (dupes["dup1"] | dupes["employee"]).action_merge(dupes["employee"].ids[0])


def test_merge_requires_write_access(env: Any, env_for: Any, dupes: Any) -> None:
    from tessera.exceptions import AccessError

    demo = env_for("demo")
    env.ref("core.group_portal")
    portal_user = env["core.user"].create(
        {"login": "merge-portal", "name": "Portal", "group_ids": [("set", [env.ref("core.group_portal").ids[0]])]}
    )
    with pytest.raises(AccessError):
        demo["contacts.partner"].with_user(portal_user.ids[0]).browse((dupes["keep"] | dupes["dup1"]).ids).action_merge(
            dupes["keep"].ids[0]
        )


def test_merge_api(admin) -> None:  # type: ignore[no-untyped-def]
    import uuid

    tag = uuid.uuid4().hex[:8]
    email = f"dup-{tag}@api.example"
    models = "/api/v1/models/contacts.partner"
    a = admin.post(models, json={"values": {"name": f"Api Dup {tag}", "email": email}}).json()
    b = admin.post(models, json={"values": {"name": f"Api Dup {tag} 2", "email": email, "city": "Oslo"}}).json()
    dups = admin.get(f"/api/v1/contacts/{a['id']}/duplicates").json()
    assert [d["id"] for d in dups] == [b["id"]] and dups[0]["reasons"] == ["email"]
    preview = admin.post("/api/v1/contacts/merge/preview", json=[a["id"], b["id"]]).json()
    assert "name" in preview["conflicts"] and "city" not in preview["conflicts"]
    merged = admin.post(
        "/api/v1/contacts/merge",
        json={"ids": [a["id"], b["id"]], "survivor_id": a["id"], "field_choices": {"name": b["id"]}},
    ).json()
    kept = admin.get(f"/api/v1/models/contacts.partner/{a['id']}", params={"fields": "name,city"}).json()
    assert kept["name"] == f"Api Dup {tag} 2" and kept["city"] == "Oslo"
    assert admin.post(f"/api/v1/contacts/merge/{merged['merge_id']}/undo").json() == {"undone": True}
