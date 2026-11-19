"""Domain DSL: parsing, normalisation, negation, substitution, in-memory evaluation."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tessera.exceptions import ValidationError
from tessera.orm.domain import And, Domain, Leaf, Not, Or, combine_and, evaluate, negate, parse, substitute


class Rec:
    """Minimal record-like object for in-memory evaluation."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)
        self._ids = (kw.get("id", 1),)

    def __iter__(self):  # type: ignore[no-untyped-def]
        yield self


def test_parse_leaf_and_implicit_all() -> None:
    node = parse([["name", "=", "x"], ["age", ">", 3]])
    assert isinstance(node, And)
    assert node.children == (Leaf("name", "=", "x"), Leaf("age", ">", 3))


def test_parse_nested_any_not() -> None:
    node = parse({"all": [["a", "=", 1], {"any": [["b", "=", 2], {"not": ["c", "is_set"]}]}]})
    assert isinstance(node, And)
    assert isinstance(node.children[1], Or)
    assert isinstance(node.children[1].children[1], Not)


def test_parse_json_string_and_empty() -> None:
    assert parse('{"all": [["a", "=", 1]]}') == And((Leaf("a", "=", 1),))
    assert parse("") == And(())
    assert parse(None) == And(())
    assert parse(False) == Or(())


@pytest.mark.parametrize(
    "bad",
    [
        {"both": [], "all": []},
        {"xor": []},
        ["name", "???", 1],
        [["name", "=", 1, 2, 3]],
        42,
        [["9bad", "=", 1]],
    ],
)
def test_parse_rejects_invalid(bad: object) -> None:
    with pytest.raises(ValidationError):
        parse(bad)


def test_negation_pushes_down() -> None:
    node = parse({"all": [["a", "=", 1], {"any": [["b", "in", [1, 2]], ["c", "ilike", "x"]]}]})
    neg = negate(node)
    assert neg.to_json() == {"any": [["a", "!=", 1], {"all": [["b", "not_in", [1, 2]], ["c", "not_ilike", "x"]]}]}
    assert negate(negate(node)) == node


def test_substitute_variables() -> None:
    node = parse({"all": [["user_id", "=", "$uid"], ["company_id", "in", "$companies"], ["x", "in", ["$uid", 5]]]})
    out = substitute(node, {"uid": 7, "companies": [1, 2]})
    assert out.to_json() == {"all": [["user_id", "=", 7], ["company_id", "in", [1, 2]], ["x", "in", [7, 5]]]}


def test_substitute_user_path() -> None:
    class User:
        _ids = (3,)
        partner_id = Rec(id=9)

    node = parse([["partner_id", "=", "$user.partner_id"]])
    assert substitute(node, {"user": User()}).to_json() == ["partner_id", "=", 9]


def test_substitute_unknown_variable() -> None:
    with pytest.raises(ValidationError):
        substitute(parse([["a", "=", "$nope"]]), {})


def test_evaluate_scalars() -> None:
    rec = Rec(name="Alice Smith", age=30, active=True, score=None)
    assert evaluate(parse([["name", "ilike", "alice"]]), rec)
    assert not evaluate(parse([["name", "like", "alice"]]), rec)
    assert evaluate(parse([["name", "starts_with", "ali"]]), rec)
    assert evaluate(parse([["age", ">=", 30], ["age", "<", 31]]), rec)
    assert evaluate(parse([["age", "in", [29, 30]]]), rec)
    assert evaluate(parse([["age", "not_in", [1]]]), rec)
    assert evaluate(parse([["score", "is_not_set"]]), rec)
    assert evaluate(parse([["name", "is_set"]]), rec)
    assert evaluate(parse([["active", "=", True]]), rec)
    assert evaluate(parse({"not": ["active", "=", False]}), rec)
    assert evaluate(parse({"any": [["age", "=", 1], ["age", "=", 30]]}), rec)


def test_evaluate_relational_paths_and_any() -> None:
    country = Rec(id=5, code="FR")
    partner = Rec(id=2, name="ACME", country_id=country)
    tag1, tag2 = Rec(id=11, name="vip"), Rec(id=12, name="new")

    class Tags:
        _ids = (11, 12)

        def __iter__(self):  # type: ignore[no-untyped-def]
            yield from (tag1, tag2)

    lead = Rec(id=1, partner_id=partner, tag_ids=Tags())
    assert evaluate(parse([["partner_id.country_id.code", "=", "FR"]]), lead)
    assert evaluate(parse([["partner_id", "=", 2]]), lead)
    assert evaluate(parse([["partner_id", "in", [2, 3]]]), lead)
    assert evaluate(parse([["tag_ids", "in", [12]]]), lead)
    assert evaluate(parse([["tag_ids", "any", [["name", "=", "vip"]]]]), lead)
    assert not evaluate(parse([["tag_ids", "not_any", [["name", "=", "vip"]]]]), lead)
    assert evaluate(parse([["tag_ids.name", "=", "new"]]), lead)


def test_domain_wrapper_combination() -> None:
    d = Domain([["a", "=", 1]]) & [["b", "=", 2]]
    assert d.to_json() == {"all": [["a", "=", 1], ["b", "=", 2]]}
    assert (d | [["c", "=", 3]]).to_json() == {"any": [{"all": [["a", "=", 1], ["b", "=", 2]]}, ["c", "=", 3]]}
    assert (~Domain([["a", "=", 1]])).to_json() == ["a", "!=", 1]
    assert Domain().is_true
    assert Domain(False).is_false
    assert d.fields() == {"a", "b"}


def test_combine_and_flattens() -> None:
    node = combine_and([["a", "=", 1]], [["b", "=", 2]], {"all": [["c", "=", 3]]})
    assert node.to_json() == {"all": [["a", "=", 1], ["b", "=", 2], ["c", "=", 3]]}


leaf_strategy = st.tuples(
    st.sampled_from(["name", "age", "partner_id.name"]),
    st.sampled_from(["=", "!=", ">", "in", "ilike", "is_set"]),
    st.one_of(st.integers(), st.text(max_size=5), st.lists(st.integers(), max_size=3)),
).map(list)


@given(st.lists(leaf_strategy, min_size=1, max_size=5))
def test_roundtrip_json(leaves: list[list[object]]) -> None:
    cleaned = [[p, o, v] if o != "is_set" else [p, o] for p, o, v in leaves]
    node = parse(cleaned)
    again = parse(node.to_json())
    assert again == node
