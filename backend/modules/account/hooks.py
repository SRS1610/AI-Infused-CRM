"""account hooks: the ledger's database guard (D-073) and charts for existing companies."""

from __future__ import annotations

from typing import Any

from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)

# A posted entry must balance in company currency when the transaction commits, whatever wrote it.
BALANCE_GUARD = """
CREATE OR REPLACE FUNCTION account_check_move_balanced() RETURNS trigger AS $$
DECLARE
    mid bigint;
    total numeric;
    st varchar;
BEGIN
    IF TG_TABLE_NAME = 'account_move' THEN
        mid := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        mid := OLD.move_id;
    ELSE
        mid := NEW.move_id;
    END IF;
    SELECT state INTO st FROM account_move WHERE id = mid;
    IF st IS DISTINCT FROM 'posted' THEN
        RETURN NULL;
    END IF;
    SELECT COALESCE(SUM(balance), 0) INTO total FROM account_move_line WHERE move_id = mid;
    IF round(total, 2) <> 0 THEN
        RAISE EXCEPTION 'Posted journal entry % is not balanced (difference %)', mid, total
            USING ERRCODE = 'check_violation', CONSTRAINT = 'account_move_balanced';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS account_move_line_balanced ON account_move_line;
CREATE CONSTRAINT TRIGGER account_move_line_balanced
    AFTER INSERT OR UPDATE OF balance, move_id OR DELETE ON account_move_line
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION account_check_move_balanced();
DROP TRIGGER IF EXISTS account_move_balanced ON account_move;
CREATE CONSTRAINT TRIGGER account_move_balanced
    AFTER UPDATE OF state ON account_move
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION account_check_move_balanced();
"""


def install_guard(env: Any) -> None:
    env.cr.execute(BALANCE_GUARD)


def post_init(env: Any) -> None:
    """Install the balance guard and a chart of accounts for every existing company."""
    from modules.account.chart import install_chart

    install_guard(env)
    for company in env["core.company"].elevated().search([]):
        if not company.chart_template:
            counts = install_chart(company)
            log.info("chart installed", company=company.name, chart=company.chart_template, **counts)


@hook("module_upgraded")
def reinstall_guard(env: Any, module: str) -> None:
    if module == "account":
        install_guard(env)


@hook("module_uninstalled")
def drop_guard(env: Any, module: str) -> None:
    if module == "account":
        env.cr.execute("DROP FUNCTION IF EXISTS account_check_move_balanced() CASCADE")


@hook("after_create")
def chart_for_new_company(records: Any, vals_list: Any) -> None:
    """A company created after installation gets the chart matching its country."""
    if records._name != "core.company" or records.env.context.get("account_no_chart"):
        return
    from modules.account.chart import install_chart

    for company in records.elevated("chart installation"):
        if not company.chart_template:
            install_chart(company)
