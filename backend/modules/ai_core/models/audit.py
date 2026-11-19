"""Audit trail of AI runs: runs, tool calls, proposals (human in the loop), citations, feedback,
embeddings and evaluation runs."""

from __future__ import annotations

from tessera.orm import Model, fields

RUN_STATUSES = [
    ("running", "Running"),
    ("ok", "Done"),
    ("refusal", "Declined by the model"),
    ("invalid_output", "Invalid output"),
    ("blocked", "Blocked"),
    ("error", "Failed"),
]
DECISIONS = [
    ("none", "No decision needed"),
    ("pending", "Awaiting review"),
    ("accepted", "Accepted"),
    ("edited", "Edited then accepted"),
    ("rejected", "Rejected"),
    ("partial", "Partly accepted"),
]
PROPOSAL_KINDS = [
    ("create", "Create a record"),
    ("write", "Change a record"),
    ("transition", "Move to another state"),
    ("message", "Send a message"),
    ("action", "Run an action"),
]
PROPOSAL_STATES = [
    ("pending", "Pending"),
    ("accepted", "Accepted"),
    ("edited", "Edited then accepted"),
    ("rejected", "Rejected"),
    ("expired", "Expired"),
    ("failed", "Failed"),
]


class Run(Model):
    _name = "ai_core.run"
    _description = "AI run"
    _rule_check_after_write = True  # owner-scoped: cannot be handed to another user
    _order = "id desc"
    _rec_name = "feature"
    _indexes = [("record", ["res_model", "res_id"])]

    feature_id = fields.Many2one("ai_core.feature", ondelete="set_null", index=True)
    feature = fields.Char(required=True, index=True)
    user_id = fields.Many2one("core.user", index=True)
    company_id = fields.Many2one("core.company", index=True)
    prompt_version = fields.Char(help="name@version")
    provider = fields.Char()
    model = fields.Char()
    inference_geo = fields.Char()
    status = fields.Selection(RUN_STATUSES, default="running", required=True)
    error_code = fields.Char()
    error = fields.Text()
    res_model = fields.Char("Record model")
    res_id = fields.Integer("Record id")
    input_ref = fields.Json(default=dict, help="Inputs and the ids of the records the prompt was built from")
    input_text = fields.Text(help="Rendered prompt (kept for the company's retention period)")
    output_text = fields.Text()
    output_data = fields.Json()
    input_tokens = fields.Integer()
    output_tokens = fields.Integer()
    cache_read_tokens = fields.Integer()
    cost = fields.Float(digits=(14, 6))
    latency_ms = fields.Integer()
    steps = fields.Integer()
    fallback_used = fields.Boolean()
    cached = fields.Boolean()
    redaction_applied = fields.Boolean()
    safety_flags = fields.Json(default=list)
    ai_generated = fields.Boolean(default=True)
    decision = fields.Selection(DECISIONS, default="none", required=True)
    decided_by = fields.Many2one("core.user")
    decided_at = fields.Datetime()
    content_purged = fields.Boolean(help="Inputs and outputs removed by the retention policy")
    tool_call_ids = fields.One2many("ai_core.tool_call", "run_id", "Tool calls")
    proposal_ids = fields.One2many("ai_core.proposal", "run_id", "Proposals")
    citation_ids = fields.One2many("ai_core.citation", "run_id", "Citations")
    feedback_ids = fields.One2many("ai_core.feedback", "run_id", "Feedback")


class ToolCall(Model):
    _name = "ai_core.tool_call"
    _description = "AI tool call"
    _order = "run_id, step, id"

    run_id = fields.Many2one("ai_core.run", required=True, ondelete="cascade", index=True)
    step = fields.Integer()
    name = fields.Char(required=True)
    arguments = fields.Json(default=dict)
    result = fields.Json()
    allowed = fields.Boolean(help="False when the tool was not whitelisted for the feature")
    error = fields.Char()
    duration_ms = fields.Integer()


class Proposal(Model):
    _name = "ai_core.proposal"
    _description = "AI proposal awaiting a human decision"
    _rule_check_after_write = True  # owner-scoped: cannot be handed to another user
    _order = "id desc"
    _indexes = [("record", ["res_model", "res_id"])]

    run_id = fields.Many2one("ai_core.run", ondelete="cascade", index=True)
    feature = fields.Char(index=True)
    user_id = fields.Many2one("core.user", required=True, index=True, help="Who asked; only they may decide")
    company_id = fields.Many2one("core.company")
    kind = fields.Selection(PROPOSAL_KINDS, required=True)
    title = fields.Char()
    res_model = fields.Char(required=True)
    res_id = fields.Integer()
    payload = fields.Json(required=True, help="The proposed values / message / transition / action")
    original = fields.Json(help="Current values of the proposed fields when the proposal was made (for the diff)")
    edited_payload = fields.Json()
    payload_signature = fields.Char(help="HMAC of the payload; a modified payload cannot be accepted")
    state = fields.Selection(PROPOSAL_STATES, default="pending", required=True, index=True)
    reason = fields.Char(help="Why it was rejected / why it failed")
    expires_at = fields.Datetime()
    decided_by = fields.Many2one("core.user")
    decided_at = fields.Datetime()
    result_res_id = fields.Integer(help="Record created by an accepted create proposal")
    ai_generated = fields.Boolean(default=True, help="Carries the AI-drafted label until a human edits or accepts")


class Citation(Model):
    _name = "ai_core.citation"
    _description = "AI citation"

    run_id = fields.Many2one("ai_core.run", required=True, ondelete="cascade", index=True)
    source = fields.Char(required=True, help="model:id as cited")
    res_model = fields.Char()
    res_id = fields.Integer()
    title = fields.Char()
    snippet = fields.Text()


class Feedback(Model):
    _name = "ai_core.feedback"
    _description = "AI feedback"
    _rule_check_after_write = True  # owner-scoped: cannot be handed to another user
    _order = "id desc"

    run_id = fields.Many2one("ai_core.run", required=True, ondelete="cascade", index=True)
    feature = fields.Char(index=True)
    user_id = fields.Many2one("core.user", default=lambda self: self.env.uid)
    rating = fields.Selection([("up", "Helpful"), ("down", "Not helpful")])
    correction = fields.Text(help="What the answer should have been → candidate evaluation case")
    comment = fields.Text()
    exported = fields.Boolean(help="Exported to the feature's candidate evaluation cases")


class Embedding(Model):
    _name = "ai_core.embedding"
    _description = "Retrieval chunk"
    _studio = False
    _indexes = [("source", ["res_model", "res_id"])]

    company_id = fields.Many2one("core.company", index=True)
    res_model = fields.Char(required=True)
    res_id = fields.Integer(required=True)
    source_kind = fields.Selection(
        [("record", "Record"), ("message", "Message"), ("attachment", "Attachment")], default="record"
    )
    source_id = fields.Integer(help="Message or attachment id when the chunk comes from one")
    chunk_index = fields.Integer()
    title = fields.Char()
    content = fields.Text(required=True)
    content_hash = fields.Char(index=True)
    vector = fields.Vector(dimensions=1024, index="hnsw", copy=False)
    embed_model = fields.Char()


class EvalRun(Model):
    _name = "ai_core.eval_run"
    _description = "AI evaluation run"
    _order = "id desc"

    feature = fields.Char(required=True, index=True)
    prompt_version = fields.Char()
    mode = fields.Selection([("replay", "Replay"), ("live", "Live"), ("record", "Record")], default="replay")
    cases = fields.Integer()
    metrics = fields.Json(default=dict)
    thresholds = fields.Json(default=dict)
    passed = fields.Boolean()
    failures = fields.Json(default=list)
    latency_p95_ms = fields.Integer()
    cost = fields.Float(digits=(14, 6))
