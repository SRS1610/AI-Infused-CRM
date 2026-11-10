"""The report-parameter object: everything that defines a report run.

The natural-language reports feature (sub-phase 2.6) only ever produces a ``ReportParams``; the numbers
always come from :func:`modules.account_reports.engine.run_report`.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Comparison = Literal["none", "previous_period", "previous_year"]


class ReportParams(BaseModel):
    """Parameters of a report run (JSON schema: ``ReportParams.model_json_schema()``)."""

    model_config = ConfigDict(extra="forbid")

    report: str = Field(description="Report code, e.g. balance_sheet, profit_loss, trial_balance, tax")
    date_from: dt.date | None = Field(
        None, description="Start of the period (range reports; default: start of the fiscal year of date_to)"
    )
    date_to: dt.date | None = Field(None, description="End of the period / as-of date (default: today)")
    comparison: Comparison = Field("none", description="Extra columns for earlier periods")
    periods: int = Field(1, ge=1, le=12, description="Number of comparison columns")
    journal_ids: list[int] = Field(default_factory=list, description="Only these journals (empty = all)")
    partner_ids: list[int] = Field(default_factory=list, description="Only these partners (empty = all)")
    company_ids: list[int] = Field(
        default_factory=list, description="Companies; several = consolidation (empty = the current company)"
    )
    currency: str | None = Field(None, description="ISO code of the report currency (consolidation)")
    include_draft: bool = Field(False, description="Count draft entries too")
    account_type: Literal["receivable", "payable", "all"] = Field(
        "all", description="Partner ledger: receivable and / or payable accounts"
    )
    unfold: list[str] = Field(default_factory=list, description="Ids of the lines to unfold")
    unfold_all: bool = False
    expand: str | None = Field(None, description="Only return the items of this unfolded line (pagination)")
    offset: int = Field(0, ge=0)
    limit: int = Field(80, ge=1, le=100_000, description="Items per unfolded line")
    hide_zero: bool = Field(True, description="Hide lines that are zero in every column")

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value
