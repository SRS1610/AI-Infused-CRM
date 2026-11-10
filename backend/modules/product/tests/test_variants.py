"""Attributes and variants (D-071): generation, codes, archiving, shared fields kept in sync and refused on
variants, extra prices, structure constraints."""

from __future__ import annotations

from typing import Any

import pytest

from modules.product.models import product as product_models
from tessera.exceptions import UserError, ValidationError

pytestmark = pytest.mark.db


def _attribute(env: Any, name: str, values: list[tuple[str, str, float]], sequence: int = 10) -> Any:
    return env["product.attribute"].create(
        {
            "name": name,
            "sequence": sequence,
            "value_ids": [
                ("create", {"name": n, "code": c, "price_extra": extra, "sequence": i})
                for i, (n, c, extra) in enumerate(values)
            ],
        }
    )


@pytest.fixture
def mug(env: Any) -> Any:
    colour = _attribute(env, "Mug colour", [("White", "WHT", 0), ("Black", "BLK", 1.5)], sequence=1)
    size = _attribute(env, "Mug size", [("Small", "S", 0), ("Large", "L", 3)], sequence=2)
    template = env["product.product"].create(
        {
            "name": "Enamel mug",
            "default_code": "MUG",
            "type": "goods",
            "category_id": env.ref("product.category_goods").ids[0],
            "list_price": 10,
            "standard_price": 4,
            "tax_percent": 20,
            "description_sale": "A sturdy enamel mug.",
            "attribute_line_ids": [
                ("create", {"attribute_id": colour.ids[0], "sequence": 1, "value_ids": colour.value_ids.ids}),
                ("create", {"attribute_id": size.ids[0], "sequence": 2, "value_ids": size.value_ids.ids}),
            ],
        }
    )
    return template


def _variants(template: Any, *, archived: bool = False) -> Any:
    products = template.env["product.product"].with_context(active_test=not archived)
    return products.search([("template_id", "=", template.ids[0])], order="default_code")


def test_demo_tshirt_is_a_template_with_nine_variants(env: Any) -> None:
    tshirt = env.ref("product.product_tshirt")
    assert tshirt.is_template is True and tshirt.is_sellable is False and tshirt.variant_count == 9
    assert tshirt.action_generate_variants() == {"created": 0, "archived": 0, "reactivated": 0}
    red_l = env.ref("product.product_tshirt_red_l")
    assert red_l.display_name == "[TSHIRT-RED-L] Organic cotton T-shirt (Red, L)"
    assert red_l.price_extra == 2 and red_l.list_price == 27 and red_l.is_sellable is True
    blue_s = env.ref("product.product_tshirt_blue_s")
    assert blue_s.list_price == 25 and blue_s.tax_percent == 20 and blue_s.category_id.name == "Apparel"
    assert env.ref("product.product_robot_arm").is_sellable is True  # Phase 1 products stay single products
    sellable = env["product.product"].search([("is_sellable", "=", True), ("default_code", "starts_with", "TSHIRT")])
    assert len(sellable) == 9


def test_generate_creates_every_combination_with_codes_and_shared_fields(env: Any, mug: Any) -> None:
    assert mug.is_template and mug.variant_count == 0
    result = mug.action_generate_variants()
    assert result == {"created": 4, "archived": 0, "reactivated": 0}
    variants = _variants(mug)
    assert variants.mapped("default_code") == ["MUG-BLK-L", "MUG-BLK-S", "MUG-WHT-L", "MUG-WHT-S"]
    assert mug.variant_count == 4 and set(mug.variant_ids.ids) == set(variants.ids)
    by_code = {v.default_code: v for v in variants}
    assert by_code["MUG-BLK-L"].price_extra == 4.5 and by_code["MUG-BLK-L"].list_price == 14.5
    assert by_code["MUG-WHT-S"].price_extra == 0 and by_code["MUG-WHT-S"].list_price == 10
    for v in variants:
        assert v.template_id == mug and len(v.attribute_value_ids) == 2
        assert (v.name, v.type, v.category_id, v.uom_id, v.currency_id) == (
            mug.name,
            mug.type,
            mug.category_id,
            mug.uom_id,
            mug.currency_id,
        )
        assert (
            v.standard_price == 4 and v.tax_percent == 20 and v.sale_ok and v.description_sale == "A sturdy enamel mug."
        )
        assert not v.is_template and v.is_sellable
    assert by_code["MUG-BLK-L"].display_name == "[MUG-BLK-L] Enamel mug (Black, Large)"
    assert mug.action_generate_variants()["created"] == 0  # only the missing ones


def test_template_without_code_gives_variants_without_code(env: Any, mug: Any) -> None:
    mug.write({"default_code": False})
    mug.action_generate_variants()
    assert set(_variants(mug).mapped("default_code")) == {None}
    assert _variants(mug)[0].display_name.startswith("Enamel mug (")


def test_removed_values_archive_variants_and_come_back(env: Any, mug: Any) -> None:
    mug.action_generate_variants()
    size_line = mug.attribute_line_ids.filtered(lambda line: line.attribute_id.name == "Mug size")
    large = size_line.value_ids.filtered(lambda v: v.code == "L")
    size_line.write({"value_ids": [("remove", large.ids[0])]})
    assert mug.action_generate_variants() == {"created": 0, "archived": 2, "reactivated": 0}
    assert len(_variants(mug)) == 2 and len(_variants(mug, archived=True)) == 4  # never deleted
    size_line.write({"value_ids": [("add", large.ids[0])]})
    assert mug.action_generate_variants() == {"created": 0, "archived": 0, "reactivated": 2}
    assert len(_variants(mug)) == 4


def test_new_attribute_value_adds_variants(env: Any, mug: Any) -> None:
    mug.action_generate_variants()
    colour = mug.attribute_line_ids.sorted(lambda line: line.sequence)[0]
    red = env["product.attribute_value"].create(
        {"attribute_id": colour.attribute_id.ids[0], "name": "Red", "code": "RED"}
    )
    colour.write({"value_ids": [("add", red.ids[0])]})
    assert mug.action_generate_variants()["created"] == 2
    assert {"MUG-RED-S", "MUG-RED-L"} <= set(_variants(mug).mapped("default_code"))


def test_template_changes_are_copied_to_variants(env: Any, mug: Any) -> None:
    mug.action_generate_variants()
    services = env.ref("product.category_services")
    mug.write({"name": "Camping mug", "list_price": 12, "standard_price": 5, "category_id": services.ids[0]})
    for v in _variants(mug):
        assert v.name == "Camping mug" and v.category_id == services and v.standard_price == 5
        assert v.list_price == 12 + v.price_extra
    mug.with_context(lang="fr_FR").write({"name": "Tasse de camping", "description_sale": "Une tasse solide."})
    variant = _variants(mug)[0]
    assert variant.with_context(lang="fr_FR").name == "Tasse de camping"
    assert variant.with_context(lang="fr_FR").description_sale == "Une tasse solide."
    assert variant.name == "Camping mug"  # every language follows, not only the one written
    mug.write({"active": False})
    assert not _variants(mug) and len(_variants(mug, archived=True)) == 4


def test_shared_fields_cannot_be_written_on_a_variant(env: Any, mug: Any) -> None:
    mug.action_generate_variants()
    variant = _variants(mug)[0]
    for vals in ({"list_price": 99}, {"name": "Other"}, {"category_id": env.ref("product.category_services").ids[0]}):
        with pytest.raises(UserError, match="change it on the template"):
            variant.write(vals)
    variant.write({"name": variant.name, "list_price": variant.list_price})  # unchanged values are accepted
    variant.write({"default_code": "MUG-SPECIAL", "barcode": "4006381333948"})  # its own fields
    assert variant.default_code == "MUG-SPECIAL"
    variant.action_archive()
    assert not variant.active


def test_extra_price_changes_follow_on_variants(env: Any, mug: Any) -> None:
    mug.action_generate_variants()
    black = env["product.attribute_value"].search([("code", "=", "BLK"), ("name", "=", "Black")])
    black.write({"price_extra": 2})
    blk_s = _variants(mug).filtered(lambda v: v.default_code == "MUG-BLK-S")
    assert blk_s.price_extra == 2 and blk_s.list_price == 12
    white_s = _variants(mug).filtered(lambda v: v.default_code == "MUG-WHT-S")
    large = env["product.attribute_value"].search([("code", "=", "L"), ("name", "=", "Large")])
    white_s.write({"attribute_value_ids": [("add", large.ids[0])]})
    assert white_s.price_extra == 3 and white_s.list_price == 13


def test_variant_created_by_hand_takes_the_template_values(env: Any, mug: Any) -> None:
    black = env["product.attribute_value"].search([("code", "=", "BLK"), ("name", "=", "Black")])
    variant = env["product.product"].create(
        {"template_id": mug.ids[0], "name": "Ignored", "list_price": 1, "attribute_value_ids": [("set", black.ids)]}
    )
    assert variant.name == "Enamel mug" and variant.list_price == 11.5
    # a one-value variant is not a full combination: generation archives it
    assert mug.action_generate_variants() == {"created": 4, "archived": 1, "reactivated": 0}
    assert mug.action_generate_variants()["archived"] == 0


def test_structure_constraints(env: Any, mug: Any) -> None:
    mug.action_generate_variants()
    variant = _variants(mug)[0]
    colour = mug.attribute_line_ids.sorted(lambda line: line.sequence)[0]
    size = mug.attribute_line_ids.sorted(lambda line: line.sequence)[1]
    with pytest.raises(ValidationError, match="cannot have attributes of its own"):
        env["product.attribute_line"].create(
            {
                "product_id": variant.ids[0],
                "attribute_id": colour.attribute_id.ids[0],
                "value_ids": colour.value_ids.ids,
            }
        )
    with pytest.raises(ValidationError, match="must belong to its attribute"):
        colour.write({"value_ids": [("add", size.value_ids.ids[0])]})
    with pytest.raises(ValidationError, match="at least one value"):
        colour.write({"value_ids": [("clear",)]})
    with pytest.raises(ValidationError, match="template of other variants"):
        env["product.product"].create({"name": "Nested", "template_id": variant.ids[0]})
    with pytest.raises(UserError, match="generate variants from its template"):
        variant.action_generate_variants()
    plain = env.ref("product.product_gripper")
    with pytest.raises(UserError, match="Add attributes"):
        plain.action_generate_variants()
    with pytest.raises(ValidationError, match="#rgb"):
        colour.attribute_id.value_ids[0].write({"html_color": "red"})


def test_too_many_combinations_are_refused(env: Any, mug: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(product_models, "MAX_VARIANTS", 3)
    with pytest.raises(UserError, match="the limit is 3"):
        mug.action_generate_variants()


def test_copying_a_template_copies_its_attributes_not_its_variants(env: Any, mug: Any) -> None:
    mug.action_generate_variants()
    copy = mug.copy()
    assert copy.is_template and len(copy.attribute_line_ids) == 2 and copy.variant_count == 0
    variant_copy = _variants(mug)[0].copy()
    assert not variant_copy.template_id and not variant_copy.attribute_value_ids


def test_a_user_reads_variant_names(env_for: Any, env: Any) -> None:
    demo = env_for("demo")
    red_l = demo["product.product"].browse(env.ref("product.product_tshirt_red_l").ids)
    assert red_l.display_name == "[TSHIRT-RED-L] Organic cotton T-shirt (Red, L)"
    with pytest.raises(Exception, match="not allowed"):
        demo["product.attribute"].create({"name": "Material"})
