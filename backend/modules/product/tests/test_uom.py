"""Units of measure: types, conversion, rounding, one reference per category, the 2.0.0 migration (D-070)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from tessera.exceptions import UserError, ValidationError

pytestmark = pytest.mark.db


def test_data_units_have_types_and_rounding(env: Any) -> None:
    unit, dozen, gram = env.ref("product.uom_unit"), env.ref("product.uom_dozen"), env.ref("product.uom_gram")
    assert (unit.uom_type, unit.factor, unit.rounding) == ("reference", 1.0, 0.01)
    assert (dozen.uom_type, dozen.factor) == ("bigger", 12.0)
    assert (gram.uom_type, gram.factor, gram.category) == ("smaller", 0.001, "weight")


def test_compute_quantity_converts_and_rounds(env: Any) -> None:
    unit, dozen = env.ref("product.uom_unit"), env.ref("product.uom_dozen")
    kg, gram = env.ref("product.uom_kg"), env.ref("product.uom_gram")
    hour, day = env.ref("product.uom_hour"), env.ref("product.uom_day")
    assert dozen._compute_quantity(2, unit) == 24
    assert unit._compute_quantity(5, dozen) == 0.42  # 0.41666… rounded to 0.01
    assert unit._compute_quantity(5, dozen, round=False) == pytest.approx(5 / 12)
    assert kg._compute_quantity(1.2345, gram) == 1235  # gram rounding is 1 (half-up)
    assert gram._compute_quantity(1500, kg) == 1.5
    assert day._compute_quantity(1.5, hour) == 12
    assert unit._compute_quantity(2.345, unit) == 2.35  # same unit: rounding only
    assert unit._compute_quantity(2.345, unit, round=False) == 2.345


def test_compute_price_per_unit(env: Any) -> None:
    unit, dozen = env.ref("product.uom_unit"), env.ref("product.uom_dozen")
    assert dozen._compute_price(120, unit) == pytest.approx(10)
    assert unit._compute_price(10, dozen) == pytest.approx(120)
    assert unit._compute_price(10, unit) == 10


def test_conversion_across_categories_is_refused(env: Any) -> None:
    with pytest.raises(UserError, match="measure different things"):
        env.ref("product.uom_unit")._compute_quantity(1, env.ref("product.uom_kg"))
    with pytest.raises(UserError, match="measure different things"):
        env.ref("product.uom_hour")._compute_price(1, env.ref("product.uom_meter"))


def test_one_reference_unit_per_category(env: Any) -> None:
    uoms = env["product.uom"]
    with pytest.raises(ValidationError, match="already has a reference unit"):
        uoms.create({"name": "Piece", "category": "unit", "uom_type": "reference", "factor": 1})
    # an archived reference does not count
    env.ref("product.uom_liter").write({"active": False})
    other = uoms.create({"name": "Litre (new)", "category": "volume", "uom_type": "reference", "factor": 1})
    assert other.uom_type == "reference"
    with pytest.raises(ValidationError, match="already has a reference unit"):
        env.ref("product.uom_liter").write({"active": True})


def test_type_follows_the_ratio(env: Any) -> None:
    uoms = env["product.uom"]
    pack = uoms.create({"name": "Pack of 6", "category": "unit", "factor": 6})
    tonne = uoms.create({"name": "t", "category": "weight", "factor": 1000})
    mm = uoms.create({"name": "mm", "category": "length", "factor": 0.001})
    assert (pack.uom_type, tonne.uom_type, mm.uom_type) == ("bigger", "bigger", "smaller")
    assert pack._compute_quantity(2, env.ref("product.uom_dozen")) == 1


@pytest.mark.parametrize(
    ("vals", "message"),
    [
        ({"uom_type": "bigger", "factor": 0.5}, "ratio above 1"),
        ({"uom_type": "smaller", "factor": 2}, "ratio below 1"),
        ({"uom_type": "reference", "factor": 2, "category": "length"}, "ratio of 1"),
        ({"uom_type": "bigger", "factor": 0}, "must be positive"),
        ({"uom_type": "bigger", "factor": 3, "rounding": 0}, "rounding precision"),
    ],
)
def test_unit_constraints(env: Any, vals: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        env["product.uom"].create({"name": "Odd", "category": "unit", **vals})


def test_post_migration_types_existing_units(env: Any) -> None:
    extra = env["product.uom"].create({"name": "Piece", "category": "unit", "uom_type": "bigger", "factor": 2})
    env.cr.execute("UPDATE product_uom SET uom_type = NULL, rounding = NULL")
    env.cr.execute("UPDATE product_uom SET factor = 1 WHERE id = :id", {"id": extra.ids[0]})
    env.cr.execute("UPDATE product_product SET is_template = NULL, is_sellable = NULL, price_extra = NULL")
    path = Path(__file__).resolve().parents[1] / "migrations" / "2.0.0" / "post.py"
    spec = importlib.util.spec_from_file_location("product_migration_200_post", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.migrate(env, "1.0.0")
    unit, dozen, gram = env.ref("product.uom_unit"), env.ref("product.uom_dozen"), env.ref("product.uom_gram")
    assert (unit.uom_type, dozen.uom_type, gram.uom_type) == ("reference", "bigger", "smaller")
    assert unit.rounding == 0.01
    assert extra.uom_type == "bigger"  # a second unit of ratio 1: kept apart from the reference
    arm = env.ref("product.product_robot_arm")
    assert arm.is_template is False and arm.is_sellable is True and arm.price_extra == 0
