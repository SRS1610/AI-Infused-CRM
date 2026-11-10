"""Pre-filing checks: each deterministic finding on a seeded anomaly, and a clean period without findings."""

from __future__ import annotations

import pytest

from modules.account.chart import chart_ref
from modules.account.tests.common import invoice, make_tax, partner
from modules.account_tax.prefiling import prefiling_checks, summarise
from modules.account_tax.tests.common import Q2, D, eu_golden, line

pytestmark = pytest.mark.db


def _codes(findings):
    return {f["code"]: f for f in findings}


def test_clean_golden_period_has_no_findings(env):
    golden = eu_golden(env)
    findings = prefiling_checks(golden["company"], *Q2)
    assert findings == []
    assert summarise(findings) == "No findings"


def test_seeded_anomalies_are_found(env):
    golden = eu_golden(env)
    e, company = golden["env"], golden["company"]
    fr = e.ref("core.country_fr").ids[0]
    de = e.ref("core.country_de").ids[0]
    day = D(2031, 5, 10)
    # reverse charge for a supplier without a tax ID (fiscal position set by hand on the partner)
    no_vat = partner(
        e, "Lieferant ohne USt", country_id=de, property_account_position_id=chart_ref(company, "fp_eu_b2b").ids[0]
    )
    rc = invoice(e, no_vat, [line(e, 100.0, "purchase_vat_20")], move_type="in_invoice", invoice_date=day, date=day)
    # an unusual rate for French customers (5 lines at 20 % already, one at 5.5 %)
    domestic = partner(e, "Client Nantes", country_id=fr)
    for price in (10.0, 20.0, 30.0, 40.0):
        invoice(e, domestic, [line(e, price, "sale_vat_20")], invoice_date=day)
    odd = invoice(e, domestic, [line(e, 50.0, "sale_vat_5")], invoice_date=day)
    # a German business customer invoiced without the intra-EU fiscal position
    eu_customer = partner(e, "Kunde München", country_id=de, vat="DE111222333")
    wrong_fp = invoice(e, eu_customer, [line(e, 60.0, "sale_vat_20")], invoice_date=day, fiscal_position_id=None)
    # a tax without grids, used in the period, and a draft entry
    gridless = make_tax(e, "Gridless 7%", 7, company=company)
    untagged = invoice(e, domestic, [line(e, 100.0, "sale_vat_20", tax_ids=[("set", gridless.ids)])], invoice_date=day)
    draft = invoice(e, domestic, [line(e, 10.0, "sale_vat_20")], invoice_date=day, post=False)
    findings = prefiling_checks(company, *Q2)
    codes = _codes(findings)
    assert codes["reverse_charge_without_vat"]["res_ids"] == rc.ids
    assert codes["unusual_tax_for_country"]["severity"] == "warning"
    assert odd.ids[0] in codes["unusual_tax_for_country"]["res_ids"]
    combo = codes["unusual_tax_for_country"]["details"]["combinations"]
    assert any(c["tax_ids"] == chart_ref(company, "sale_vat_5").ids for c in combo)
    assert wrong_fp.ids[0] in codes["fiscal_position_mismatch"]["res_ids"]
    assert gridless.ids[0] in codes["taxes_without_grids"]["res_ids"]
    assert codes["tax_lines_without_grid"]["res_ids"] == untagged.ids
    assert codes["draft_entries"]["res_ids"] == draft.ids
    mismatch = codes["report_ledger_mismatch"]
    assert mismatch["details"]["difference"] == pytest.approx(-7.0)
    # errors first, then warnings
    severities = [f["severity"] for f in findings]
    assert severities == sorted(severities, key=["error", "warning", "info"].index)
    text = summarise(findings)
    assert text.startswith("Error:")


def test_locked_period_is_reported(env):
    golden = eu_golden(env)
    env_eu, company = golden["env"], golden["company"]
    env_eu["account.tax_return"].action_close_period(*(d.isoformat() for d in Q2))
    codes = _codes(prefiling_checks(company, *Q2))
    assert codes["period_locked"]["details"]["tax_lock_date"] == "2031-06-30"
    ret = env_eu["account.tax_return"].search([("date_from", "=", Q2[0])])
    findings = ret.action_check()
    assert "period_locked" in {f["code"] for f in findings}
    assert ret.findings[0]["code"] == "period_locked"
