"""Demo answers for the deterministic test provider (``tessera seed demo``).

With no provider keys, the demo routes every task class to the "Local test provider", which answers with
schema-valid placeholders. These rules give it realistic answers for the steps of the Phase 1 demo script
(docs/demos/phase-1.md), so the walkthrough shows what each feature does. They only apply to the demo
provider; a real provider ignores them.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.orm.hooks import hook

DEMO_EMAIL = (
    "Hello, I'm Priya Shah, Head of Operations at Lumen Robotics (priya.shah@lumen-robotics.example, "
    "+44 20 7946 0958). We need 8 collaborative robots for our Leeds plant by the end of March; our budget is "
    "about 90,000. Could someone call me this week?"
)
DEMO_CALL_NOTES = (
    "Call with Jordan Lee (Acme Robotics). They confirmed 6 cobots for line 2, maybe 8 if the pilot goes well. "
    "Budget approved at 75,600. Decision by 15 December. Jordan wants a revised quote with training included "
    "by Friday. Their new procurement contact is Sam Patel, Purchasing Manager."
)


def _next_weekday(today: dt.date, weekday: int) -> dt.date:
    days = (weekday - today.weekday()) % 7 or 7
    return today + dt.timedelta(days=days)


def demo_rules(today: dt.date) -> list[dict[str, Any]]:
    q_start = dt.date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    q_end = dt.date(q_start.year + (q_start.month + 3 > 12), (q_start.month + 2) % 12 + 1, 1) - dt.timedelta(days=1)
    return [
        {
            "match": {"feature": "calendar.nl_schedule", "contains": "lunch with Dana"},
            "respond": {
                "json": {
                    "title": "Lunch with Dana",
                    "date": _next_weekday(today, 1).isoformat(),
                    "start_time": "12:30",
                    "duration_minutes": 60,
                    "all_day": False,
                    "attendees": ["Dana Demo"],
                    "location": None,
                    "notes": "",
                }
            },
        },
        {
            "match": {"feature": "crm.nl_search", "contains": "over 10k"},
            "respond": {
                "json": {
                    "match": "all",
                    "conditions": [
                        {"field": "type", "operator": "=", "value": "opportunity"},
                        {"field": "stage_id.name", "operator": "!=", "value": "Won"},
                        {"field": "expected_revenue", "operator": ">", "value": 10000},
                        {"field": "date_deadline", "operator": ">=", "value": q_start.isoformat()},
                        {"field": "date_deadline", "operator": "<=", "value": q_end.isoformat()},
                        {"field": "user_id", "operator": "=", "value": "$uid"},
                    ],
                    "explanation": "Your opportunities not yet won, above 10,000, closing this quarter.",
                }
            },
        },
        {
            "match": {"feature": "crm.lead_extract", "contains": "Priya Shah"},
            "respond": {
                "json": {
                    "fields": {
                        "contact_name": "Priya Shah",
                        "email_from": "priya.shah@lumen-robotics.example",
                        "phone": "+44 20 7946 0958",
                        "mobile": None,
                        "function": "Head of Operations",
                        "partner_name": "Lumen Robotics",
                        "website": None,
                        "street": None,
                        "city": "Leeds",
                        "zip": None,
                        "country_code": "GB",
                        "expected_revenue": 90000,
                        "date_deadline": None,
                    },
                    "notes": "Wants 8 cobots for the Leeds plant by end of March; asked for a call this week.",
                }
            },
        },
        {
            "match": {"feature": "crm.call_summary", "contains": "Sam Patel"},
            "respond": {
                "json": {
                    "summary": "Acme confirmed 6 cobots for line 2 (possibly 8 after the pilot) with a budget of "
                    "75,600 "
                    "and a decision by 15 December. They want a revised quote including training by Friday.",
                    "key_points": [
                        "6 cobots confirmed, 8 if the pilot succeeds",
                        "Budget approved at 75,600",
                        "Decision expected by 15 December",
                        "New procurement contact: Sam Patel, Purchasing Manager",
                    ],
                    "next_step": {
                        "summary": "Send the revised quote including training",
                        "due_date": (today + dt.timedelta(days=3)).isoformat(),
                    },
                    "fields": {
                        "stage": None,
                        "expected_revenue": 75600,
                        "probability": 70,
                        "date_deadline": dt.date(today.year, 12, 15).isoformat(),
                        "priority": "2",
                        "contact_name": "Sam Patel",
                        "function": "Purchasing Manager",
                        "phone": None,
                    },
                }
            },
        },
        {
            "match": {"feature": "crm.next_best_action"},
            "respond": {
                "json": {
                    "actions": [
                        {
                            "title": "Call to confirm the decision date and the people involved",
                            "kind": "call",
                            "rationale": "The deal is in negotiation and nothing is scheduled to keep it moving.",
                            "signals": [],
                            "due_in_days": 1,
                        },
                        {
                            "title": "Send the revised quotation",
                            "kind": "email",
                            "rationale": "The customer asked for an updated quote; a prompt answer keeps momentum.",
                            "signals": [],
                            "due_in_days": 2,
                        },
                    ],
                    "citations": [],
                }
            },
        },
        {
            "match": {"feature": "crm.followup_draft"},
            "respond": {
                "text": "Hello,\n\nThank you for your time today. As promised, I have attached the revised quotation, "
                "which now includes on-site training for your operators.\n\nCould we schedule a short call this week "
                "to go through it and agree on the next steps?\n\nBest regards",
            },
        },
    ]


@hook("seed_demo")
def seed_demo_answers(env: Any) -> None:
    provider = env.ref("ai_core.provider_local", raise_if_not_found=False)
    if provider is None or provider.kind != "mock":
        return
    options = dict(provider.options or {})
    keep = [
        r
        for r in options.get("script", [])
        if not str(r.get("match", {}).get("feature", "")).startswith(("crm.", "calendar."))
    ]
    options["script"] = keep + demo_rules(env.today())
    provider.write({"options": options})


@hook("seed_demo")
def seed_demo_insights(env: Any) -> None:
    """Train the scoring models (the rules score companies with too little history), assess deal risk and
    compute the forecast, so the demo shows insights without waiting for the nightly jobs."""
    from modules.crm.ai_insights import deal_risk_job, forecast_job, train_score_models_job

    train_score_models_job(env)
    deal_risk_job(env)
    forecast_job(env)
