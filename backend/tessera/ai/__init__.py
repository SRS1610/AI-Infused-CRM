"""AI runtime: prompts, tools, retrieval, proposals and evaluation on top of the AI gateway.

Business code uses :func:`run_feature` / :func:`stream_feature`, declares tools with :func:`tool`
and never talks to a provider directly (AI architecture note §4)."""

from tessera.ai.errors import (
    AIError,
    AIUnavailable,
    BudgetExceeded,
    FeatureDisabled,
    InvalidOutput,
    ModelRefused,
    PolicyRefused,
    QuotaExceeded,
)
from tessera.ai.runtime import RunResult, propose_from_output, run_feature, stream_feature
from tessera.ai.tools import Source, ToolContext, tool

__all__ = [
    "AIError",
    "AIUnavailable",
    "BudgetExceeded",
    "FeatureDisabled",
    "InvalidOutput",
    "ModelRefused",
    "PolicyRefused",
    "QuotaExceeded",
    "RunResult",
    "Source",
    "ToolContext",
    "propose_from_output",
    "run_feature",
    "stream_feature",
    "tool",
]
