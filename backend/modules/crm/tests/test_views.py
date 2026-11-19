"""The CRM views resolve with their options (kanban sums, all stages, pivot/forecast layout)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.db


def test_views_resolve(admin) -> None:  # type: ignore[no-untyped-def]
    out = admin.get(
        "/api/v1/web/views", params={"model": "crm.lead", "types": "kanban,list,form,search,calendar,pivot,graph"}
    ).json()
    views = out["views"]
    assert (
        views["kanban"]["default_group_by"] == "stage_id"
        and views["kanban"]["sum_field"] == "expected_revenue"
        and views["kanban"]["all_groups"] is True
    )
    assert views["form"]["chatter"] is True and views["calendar"]["date_start"] == "date_deadline"
    assert views["pivot"]["rows"] == ["stage_id"] and views["pivot"]["col"] == "user_id"
    forecast = admin.get(
        "/api/v1/web/views",
        params={
            "model": "crm.lead",
            "types": "graph,pivot",
            "refs": "graph:crm.view_lead_forecast_graph,pivot:crm.view_lead_forecast_pivot",
        },
    ).json()["views"]
    assert forecast["graph"]["measures"] == ["prorated_revenue"] and forecast["pivot"]["rows"] == ["date_deadline"]
    action = admin.get("/api/v1/web/actions/crm.action_reports").json()
    assert action["kind"] == "client" and action["client_tag"] == "crm.reports"
    menus = [m["name"] for m in admin.get("/api/v1/web/menus").json()]
    assert {"CRM", "Contacts", "Sales"} <= set(menus)
