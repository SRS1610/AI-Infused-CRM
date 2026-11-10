"""Expressions, report definitions, access rights and company isolation."""

from __future__ import annotations

import pytest

from modules.account_reports.expressions import parse
from modules.account_reports.params import ReportParams
from modules.account_reports.tests.common import D, by_code, entry, run
from tessera.exceptions import AccessError, UserError, ValidationError

pytestmark = pytest.mark.db


def test_expression_parser():
    expr = parse("type(equity) + type(income, expense)@before_fy - line(A) + children - -code(10, 11)")
    assert [(t.sign, t.func, t.args, t.scope) for t in expr.terms] == [
        (1, "type", ("equity",), None),
        (1, "type", ("income", "expense"), "before_fy"),
        (-1, "line", ("A",), None),
        (1, "children", (), None),
        (1, "code", ("10", "11"), None),
    ]
    assert expr.line_refs == {"A"} and expr.uses_children and expr.account_based
    assert parse("").terms == []
    assert parse("grid(+01)").terms[0].args == ("+01",)
    for bad in (
        "type(",
        "type(asset_cash) type(equity)",
        "nope(x)",
        "type()",
        "type(x)@never",
        "line(A, B)",
        "type(x) +",
    ):
        with pytest.raises(ValidationError):
            parse(bad)


def _report(env, **vals):
    return env["account.report"].create({"name": "Custom", "code": "custom_test", **vals})


def test_custom_report_lines_and_validation(env):
    report = _report(env)
    lines = env["account.report_line"]
    a = lines.create(
        {"report_id": report.ids[0], "name": "Bank", "code": "A", "expression": "code(100)", "date_scope": "as_of"}
    )
    lines.create({"report_id": report.ids[0], "name": "Twice", "code": "B", "expression": "line(A) + line(A)"})
    lines.create({"report_id": report.ids[0], "name": "Tagged", "code": "T", "expression": "tag(Operating)"})
    with pytest.raises(ValidationError):
        lines.create({"report_id": report.ids[0], "name": "Bad", "expression": "type(nope)"})
    with pytest.raises(ValidationError):
        lines.create({"report_id": report.ids[0], "name": "Bad", "expression": "cash(nope)"})
    other = _report(env, code="custom_other")
    with pytest.raises(ValidationError):
        lines.create({"report_id": other.ids[0], "parent_id": a.ids[0], "name": "Wrong parent"})
    tag = env["account.account_tag"].create({"name": "Operating", "applicability": "accounts"})
    bank = env["account.account"].by_code("1000")
    bank.write({"tag_ids": [("add", tag.ids[0])]})
    entry(env, D(2031, 1, 5), [("1000", 70.0), ("3000", -70.0)])
    values = by_code(run(env, "custom_test", date_from=D(2031, 1, 1), date_to=D(2031, 1, 31)))
    assert values == {"A": pytest.approx(70), "B": pytest.approx(140), "T": pytest.approx(70)}
    assert report.check_definition()
    lines.create({"report_id": report.ids[0], "name": "Dangling", "expression": "line(ZZZ)"})
    with pytest.raises(ValidationError):
        report.check_definition()
    with pytest.raises(UserError):
        run(env, "custom_test")


def test_cycles_are_refused(env):
    report = _report(env)
    lines = env["account.report_line"]
    lines.create({"report_id": report.ids[0], "name": "A", "code": "A", "expression": "line(B)"})
    lines.create({"report_id": report.ids[0], "name": "B", "code": "B", "expression": "line(A)"})
    with pytest.raises(UserError):
        run(env, "custom_test")


def test_params_schema_for_the_ai_feature():
    schema = ReportParams.model_json_schema()
    assert {"report", "date_from", "date_to", "comparison", "company_ids", "currency"} <= set(schema["properties"])
    with pytest.raises(ValueError):
        ReportParams(report="x", unknown=1)
    assert ReportParams(report="x", currency="usd").currency == "USD"


def _user(env, login, groups, companies):
    return (
        env["core.user"]
        .elevated()
        .create(
            {
                "login": login,
                "name": login,
                "password": "secret-password-1",
                "group_ids": [("set", [env.ref(g).ids[0] for g in groups])],
                "company_id": companies[0],
                "company_ids": [("set", companies)],
            }
        )
    )


def test_access_rights(env, env_for):
    main = env.ref("core.main_company").ids[0]
    eu = env.ref("core.demo_company_eu").ids[0]
    _user(env, "rep_plain", ["core.group_user"], [main])
    _user(env, "rep_auditor", ["account.group_account_readonly"], [main])
    _user(env, "rep_adviser", ["account.group_account_manager"], [main])
    with pytest.raises(AccessError):
        run(env_for("rep_plain"), "balance_sheet")
    auditor = env_for("rep_auditor")
    assert run(auditor, "balance_sheet")["lines"]
    with pytest.raises(AccessError):
        run(auditor, "balance_sheet", company_ids=[main, eu])
    # definitions: read for auditors, maintained by advisers
    assert auditor["account.report"].search_count([]) > 0
    with pytest.raises(AccessError):
        auditor["account.report"].create({"name": "X", "code": "x_auditor"})
    with pytest.raises(AccessError):
        auditor["account.report_line"].create(
            {"report_id": auditor.ref("account_reports.report_profit_loss").ids[0], "name": "X"}
        )
    adviser = env_for("rep_adviser")
    created = adviser["account.report"].create({"name": "Adviser report", "code": "x_adviser"})
    assert created.ids


def test_company_isolation(env, env_for):
    main = env.ref("core.main_company").ids[0]
    _user(env, "rep_eu_only", ["account.group_account_readonly"], [env.ref("core.demo_company_eu").ids[0]])
    with pytest.raises(AccessError):
        run(env_for("rep_eu_only"), "trial_balance", company_ids=[main])
    data = run(env_for("rep_eu_only"), "trial_balance")
    assert data["companies"][0]["name"] == "My Company (Europe)"
