"""``manifest.toml`` parsing and validation."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from tessera.exceptions import ModuleError

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
LOAD_KEYS = ("security", "data", "views", "menus", "actions", "reports", "demo", "ai", "i18n")


@dataclass
class Manifest:
    name: str
    path: Path
    version: str = "0.1.0"
    summary: str = ""
    description: str = ""
    category: str = "Uncategorized"
    phase: int = 0
    depends: list[str] = field(default_factory=list)
    license: str = "Apache-2.0"
    installable: bool = True
    auto_install: bool = False
    application: bool = False
    load: dict[str, list[str]] = field(default_factory=dict)
    post_init: str | None = None
    pre_uninstall: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def parsed_version(self) -> Version:
        try:
            return Version(self.version)
        except InvalidVersion as exc:
            raise ModuleError(f"Module '{self.name}' has an invalid version '{self.version}'") from exc

    def files(self, kind: str) -> list[Path]:
        return [self.path / rel for rel in self.load.get(kind, [])]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "summary": self.summary,
            "description": self.description,
            "category": self.category,
            "phase": self.phase,
            "depends": list(self.depends),
            "license": self.license,
            "installable": self.installable,
            "auto_install": self.auto_install,
            "application": self.application,
        }


def load_manifest(module_dir: Path) -> Manifest:
    path = module_dir / "manifest.toml"
    if not path.is_file():
        raise ModuleError(f"No manifest.toml in {module_dir}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ModuleError(f"Invalid manifest {path}: {exc}") from exc
    name = raw.get("name") or module_dir.name
    if not NAME_RE.match(name):
        raise ModuleError(f"Invalid module name '{name}' in {path} (lowercase snake_case required)")
    if name != module_dir.name:
        raise ModuleError(f"Manifest name '{name}' does not match directory '{module_dir.name}'")
    load_section = raw.get("load", {}) or {}
    load: dict[str, list[str]] = {}
    for key, value in load_section.items():
        if key not in LOAD_KEYS:
            raise ModuleError(f"Unknown load key '{key}' in {path} (allowed: {', '.join(LOAD_KEYS)})")
        if isinstance(value, str):
            load[key] = [value]
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            load[key] = list(value)
        else:
            raise ModuleError(f"load.{key} in {path} must be a string or a list of strings")
    depends = raw.get("depends", [])
    if not isinstance(depends, list) or not all(isinstance(d, str) for d in depends):
        raise ModuleError(f"'depends' in {path} must be a list of module names")
    if name != "core" and "core" not in depends:
        depends = ["core", *depends]
    known = {
        "name",
        "version",
        "summary",
        "description",
        "category",
        "phase",
        "depends",
        "license",
        "installable",
        "auto_install",
        "application",
        "load",
        "post_init",
        "pre_uninstall",
    }
    manifest = Manifest(
        name=name,
        path=module_dir,
        version=str(raw.get("version", "0.1.0")),
        summary=str(raw.get("summary", "")),
        description=str(raw.get("description", "")),
        category=str(raw.get("category", "Uncategorized")),
        phase=int(raw.get("phase", 0)),
        depends=[d for d in dict.fromkeys(depends) if d != name],
        license=str(raw.get("license", "Apache-2.0")),
        installable=bool(raw.get("installable", True)),
        auto_install=bool(raw.get("auto_install", False)),
        application=bool(raw.get("application", False)),
        load=load,
        post_init=raw.get("post_init"),
        pre_uninstall=raw.get("pre_uninstall"),
        extra={k: v for k, v in raw.items() if k not in known},
    )
    manifest.parsed_version  # noqa: B018 - validates
    for kind in LOAD_KEYS:
        for f in manifest.files(kind):
            if not f.is_file():
                raise ModuleError(
                    f"Module '{name}' declares missing file {f.relative_to(module_dir)} under load.{kind}"
                )
    return manifest
