"""Loading a module's AI declarations: ``ai/features.yaml`` and ``ai/prompts/*.yaml``.

Called when a module is installed or upgraded (and for every installed module when ``ai_core``
itself is installed). Prompt files create a new ``ai_core.prompt_version`` only when their content
changes; versions are immutable once active.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from tessera.ai.tools import REGISTRY
from tessera.exceptions import ConfigurationError
from tessera.logging import get_logger

if TYPE_CHECKING:
    from tessera.orm.environment import Environment

log = get_logger(__name__)

FEATURE_KEYS = {
    "name", "title", "module", "description", "task_class", "prompt", "tools", "writes", "outbound", "redaction",
    "risk_class", "employment_related", "human_decision", "bias_tests", "interactive", "disclosure", "cache_ttl",
    "max_steps", "eval", "flag",
}  # fmt: skip


def module_ai_dir(module_path: Path) -> Path:
    return module_path / "ai"


def read_features(module_path: Path) -> list[dict[str, Any]]:
    path = module_ai_dir(module_path) / "features.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    features = data.get("features") or []
    for f in features:
        unknown = set(f) - FEATURE_KEYS
        if unknown:
            raise ConfigurationError(f"{path}: feature '{f.get('name')}' has unknown keys {sorted(unknown)}")
        if f.get("risk_class") == "high" and f.get("employment_related") and not f.get("bias_tests"):
            raise ConfigurationError(f"{path}: high-risk employment feature '{f['name']}' must link its bias tests")
    return list(features)


def read_prompts(module_path: Path) -> list[dict[str, Any]]:
    folder = module_ai_dir(module_path) / "prompts"
    if not folder.is_dir():
        return []
    out = []
    for file in sorted(folder.glob("*.yaml")):
        data = yaml.safe_load(file.read_text()) or {}
        if not data.get("name") or not data.get("system") or not data.get("template"):
            raise ConfigurationError(f"{file}: a prompt needs name, system and template")
        out.append(data)
    return out


def load_module(env: Environment, module: str, module_path: Path) -> dict[str, int]:
    from modules.ai_core.models.prompt import content_hash

    env = env.elevated("load AI manifest")
    counts = {"features": 0, "prompt_versions": 0}
    prompts = env["ai_core.prompt"]
    versions = env["ai_core.prompt_version"]
    for data in read_prompts(module_path):
        prompt = prompts.search([("name", "=", data["name"])], limit=1)
        if not prompt:
            prompt = prompts.create({"name": data["name"], "module": module, "description": data.get("description")})
        digest = content_hash(data)
        latest = versions.search([("prompt_id", "=", prompt.ids[0])], order="version desc", limit=1)
        if latest and latest.content_hash == digest:
            continue
        new = versions.create(
            {
                "prompt_id": prompt.ids[0],
                "version": (latest.version if latest else 0) + 1,
                "status": "draft",
                "task_class": data.get("task_class"),
                "system": data["system"],
                "template": data["template"],
                "input_schema": data.get("input_schema") or {},
                "output_schema": data.get("output_schema"),
                "model_hints": data.get("model_hints") or {},
                "test_inputs": data.get("test_inputs") or [],
                "content_hash": digest,
                "note": data.get("note"),
            }
        )
        if data.get("status", "active") == "active":
            new.activate()
        else:
            new.write({"status": data["status"]})
        counts["prompt_versions"] += 1
    features = env["ai_core.feature"]
    for f in read_features(module_path):
        missing = [t for t in f.get("tools", []) if t not in REGISTRY]
        if missing:
            log.warning("AI feature references unknown tools", feature=f["name"], tools=missing)
        evald = f.get("eval") or {}
        flag = f.get("flag") or {}
        vals = {
            "name": f["name"],
            "title": f.get("title") or f["name"],
            "module": f.get("module") or module,
            "description": f.get("description"),
            "task_class": f["task_class"],
            "prompt": f.get("prompt") or f["name"],
            "tools": f.get("tools", []),
            "writes": bool(f.get("writes")),
            "outbound": bool(f.get("outbound")),
            "redaction": f.get("redaction", "redact"),
            "risk_class": f.get("risk_class", "minimal"),
            "employment_related": bool(f.get("employment_related")),
            "human_decision": bool(f.get("human_decision")),
            "bias_tests": f.get("bias_tests"),
            "interactive": f.get("interactive", True),
            "disclosure": f.get("disclosure", "ai_drafted"),
            "cache_ttl": int(f.get("cache_ttl", 0)),
            "max_steps": int(f.get("max_steps", 4)),
            "eval_set": evald.get("set"),
            "min_eval_cases": int(evald.get("min_cases", 30)),
            "thresholds": evald.get("thresholds", {}),
            "flag_default": bool(flag.get("default", True)),
        }
        existing = features.search([("name", "=", f["name"])], limit=1)
        if existing:
            existing.write(vals)
        else:
            features.create(vals)
        counts["features"] += 1
    return counts


def load_installed(env: Environment) -> dict[str, dict[str, int]]:
    from tessera.modules.loader import discover

    manifests = discover()
    installed = env["core.module"].elevated().search([("state", "=", "installed")]).mapped("name")
    out = {}
    for name in installed:
        m = manifests.get(name)
        if m is not None and module_ai_dir(m.path).is_dir():
            out[name] = load_module(env, name, m.path)
    return out
