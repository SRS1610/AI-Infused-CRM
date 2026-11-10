"""Tool framework.

Tools are plain functions bound to ORM operations and executed with the **requesting user's**
environment, so access rights, record rules and field groups apply to everything a model can reach.
JSON Schemas are derived from type hints (closed objects, so providers validate arguments strictly)
and arguments are validated again before execution.

``writes=True`` tools never write business records: they call ``ctx.propose(...)``, which queues an
``ai_core.proposal`` for a human to accept, edit or reject.
"""

from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Union, get_args, get_origin

from jsonschema import Draft202012Validator

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model


@dataclass
class ToolDef:
    name: str
    description: str
    func: Callable[..., Any]
    writes: bool
    schema: dict[str, Any]

    def spec(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


REGISTRY: dict[str, ToolDef] = {}


def _schema_for(tp: Any) -> dict[str, Any]:
    origin = get_origin(tp)
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    if tp is str:
        return {"type": "string"}
    if tp is bool:
        return {"type": "boolean"}
    if origin is Literal:
        return {"enum": list(get_args(tp))}
    if origin in (list, typing.List):  # noqa: UP006
        (item,) = get_args(tp) or (str,)
        return {"type": "array", "items": _schema_for(item)}
    if origin in (dict, typing.Dict) or tp is dict:  # noqa: UP006
        return {"type": "object"}
    if origin in (Union, types.UnionType):
        options = [a for a in get_args(tp) if a is not type(None)]
        return _schema_for(options[0]) if len(options) == 1 else {"anyOf": [_schema_for(a) for a in options]}
    return {}


def schema_from_signature(func: Callable[..., Any]) -> dict[str, Any]:
    hints = typing.get_type_hints(func)
    props: dict[str, Any] = {}
    required: list[str] = []
    params = list(inspect.signature(func).parameters.values())[1:]  # skip ctx
    for p in params:
        props[p.name] = _schema_for(hints.get(p.name, str))
        if p.default is inspect.Parameter.empty:
            required.append(p.name)
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def tool(name: str, description: str, *, writes: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRY[name] = ToolDef(name, description, func, writes, schema_from_signature(func))
        return func

    return decorator


@dataclass
class Source:
    """A piece of company data placed in the prompt; answers may cite it by ``id`` (``model:res_id``)."""

    id: str
    title: str
    text: str

    @property
    def model(self) -> str:
        return self.id.rsplit(":", 1)[0]

    @property
    def res_id(self) -> int:
        return int(self.id.rsplit(":", 1)[1])


@dataclass
class PendingProposal:
    kind: str
    res_model: str
    res_id: int | None
    payload: dict[str, Any]
    title: str = ""


@dataclass
class ToolContext:
    env: Environment
    feature: Model
    run: Model | None = None
    step: int = 0
    sources: dict[str, Source] = field(default_factory=dict)
    proposals: list[PendingProposal] = field(default_factory=list)

    def cite(self, record: Model, text: str) -> Source:
        src = Source(f"{record._name}:{record.ids[0]}", record.display_name or record._name, text)
        self.sources[src.id] = src
        return src

    def propose(
        self, kind: str, model: str, res_id: int | None = None, *, payload: dict[str, Any], title: str = ""
    ) -> dict[str, Any]:
        self.proposals.append(PendingProposal(kind, model, res_id, payload, title))
        return {"status": "proposed", "note": "A person will review this before anything changes.", "title": title}


class ToolRejected(Exception):
    """Arguments failed validation or the tool is not whitelisted (reported to the model as an error)."""


def validate_arguments(tdef: ToolDef, arguments: dict[str, Any]) -> None:
    errors = list(Draft202012Validator(tdef.schema).iter_errors(arguments))
    if errors:
        raise ToolRejected(f"invalid arguments: {errors[0].message}")
