import pytest

pytestmark = pytest.mark.db


def test_chart_installed_on_main_company(env):
    company = env.ref("core.main_company")
    assert company.chart_template
    assert company.default_receivable_account_id.account_type == "asset_receivable"
    assert env["account.journal"].search_count([("company_id", "=", company.ids[0])]) >= 5
