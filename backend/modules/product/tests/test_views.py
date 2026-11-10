"""Product views, actions and menus resolve; variant generation and the shared-field refusal through the API."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.db


def test_views_actions_and_menus(admin: Any) -> None:
    product = admin.get("/api/v1/web/views", params={"model": "product.product", "types": "form,list,search"}).json()
    form = product["views"]["form"]
    assert form["root"]["children"][0]["children"][0]["method"] == "action_generate_variants"
    filters = {f["name"] for f in product["views"]["search"]["filters"]}
    assert {"templates", "variants", "sellable"} <= filters
    for model in ("product.pricelist", "product.attribute", "product.pricelist_item"):
        views = admin.get("/api/v1/web/views", params={"model": model, "types": "form,list"}).json()["views"]
        assert views["form"]["root"]["kind"] == "sheet" and views["list"]["columns"]
    action = admin.get("/api/v1/web/actions/product.action_variants").json()
    assert action["model"] == "product.product" and action["domain"] == [["template_id", "is_set"]]
    names = {m["name"] for m in admin.get("/api/v1/web/menus").json()}
    assert {"Catalogue", "Sales"} <= names


def test_generate_and_refuse_through_the_api(admin: Any, env: Any) -> None:
    tshirt = env.ref("product.product_tshirt").ids[0]
    res = admin.post(f"/api/v1/models/product.product/{tshirt}/call/action_generate_variants", json={})
    assert res.status_code == 200, res.text
    assert res.json()["result"] == {"created": 0, "archived": 0, "reactivated": 0}
    red_l = env.ref("product.product_tshirt_red_l").ids[0]
    refused = admin.patch(f"/api/v1/models/product.product/{red_l}", json={"values": {"list_price": 1}})
    assert refused.status_code in (400, 409, 422), refused.text
    assert "template" in refused.text
