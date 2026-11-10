"""Extract a module's translatable strings into ``i18n/<module>.pot`` and keep its ``<lang>.po`` in sync.

Sources (``tessera i18n extract <module>``):

* model definitions: ``_description``, field labels (explicit ``string`` or the label derived from the
  field name), ``help``, static selection labels and ``_sql_constraints`` messages;
* ``_("…")`` calls with a literal text in the module's Python code (the ``core`` catalogue also covers
  the kernel, ``tessera/``);
* views files: ``label``/``title``/``string``/``help``/``placeholder``/``confirm``/``text``/``tooltip``
  node attributes (see :data:`modules.web.views.TRANSLATABLE_ATTRS`);
* menus (``name``) and actions (``name``, ``help``);
* security/data files: values of ``translate=True`` fields, and ``_("…")`` calls inside string values
  (report templates).

Demo data is not extracted: it is sample content, not product text.
"""

from __future__ import annotations

import ast
import datetime as dt
import io
import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tessera.exceptions import ModuleError

if TYPE_CHECKING:
    from tessera.modules.manifest import Manifest
    from tessera.orm.registry import Registry

KERNEL_ROOT = Path(__file__).resolve().parent.parent  # the ``tessera`` package
# the kernel's own messages and the fields every model gets live in the core catalogue
MAGIC_LABELS = (
    "ID",
    "Created on",
    "Last updated on",
    "Created by",
    "Last updated by",
    "Deleted on",
    "Deleted by",
    "Display name",
)
VIEW_ATTRS = frozenset({"label", "title", "string", "help", "placeholder", "confirm", "text", "tooltip"})
VIEW_TOP_ATTRS = frozenset({"label", "string", "help"})
VIEW_META_KEYS = {"id", "model", "type", "name", "priority", "inherit", "operations", "active", "sequence", "noupdate"}
MARKER_FUNCS = frozenset({"_"})
TEMPLATE_CALL_RE = re.compile(r"""\b_\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)?(?:[!:][^{}]*)?\}")


@dataclass
class Entry:
    msgid: str
    locations: list[tuple[str, int | None]] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)


@dataclass
class Extraction:
    module: str
    entries: dict[str, Entry] = field(default_factory=dict)
    # translations found in the sources themselves (``{"en_US": …, "fr_FR": …}`` data values)
    seeds: dict[str, dict[str, str]] = field(default_factory=dict)

    def add(self, msgid: Any, location: str, line: int | None = None, comment: str | None = None) -> None:
        if not isinstance(msgid, str) or not msgid.strip():
            return
        entry = self.entries.get(msgid)
        if entry is None:
            entry = self.entries[msgid] = Entry(msgid)
        if (location, line) not in entry.locations:
            entry.locations.append((location, line))
        if comment and comment not in entry.comments:
            entry.comments.append(comment)

    def seed(self, lang: str, msgid: str, text: str) -> None:
        if msgid and text:
            self.seeds.setdefault(lang, {}).setdefault(msgid, text)

    @property
    def msgids(self) -> list[str]:
        return list(self.entries)


def placeholders(text: str) -> list[str]:
    """Sorted ``{name}`` placeholders of a message (format specs ignored)."""
    return sorted(m.group(1) or "" for m in PLACEHOLDER_RE.finditer(text.replace("{{", "").replace("}}", "")))


# -- registry --------------------------------------------------------------------------------------


_registries: dict[tuple[str, ...], Registry] = {}


def offline_registry(modules: Sequence[str], manifests: dict[str, Manifest]) -> Registry:
    """A registry composed from the code of ``modules`` and their dependencies, without a database."""
    from tessera.modules.graph import resolve_order
    from tessera.modules.loader import load_module_code
    from tessera.orm.registry import Registry

    order = tuple(resolve_order(manifests, list(modules)))
    cached = _registries.get(order)
    if cached is not None:
        return cached
    registry = Registry("postgresql://i18n-extract@localhost/i18n_extract")
    for name in order:
        load_module_code(registry, manifests[name])
    registry.build(dynamic=[])
    _registries[order] = registry
    return registry


# -- sources ---------------------------------------------------------------------------------------


def _python_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).parts
        if "tests" in rel or "testing" in rel or "migrations" in rel:
            continue
        yield path


def extract_calls(source: str) -> Iterator[tuple[str, int]]:
    """``(msgid, line)`` of every ``_("literal", …)`` call in Python ``source``."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in MARKER_FUNCS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            yield node.args[0].value, node.lineno


def _extract_python(out: Extraction, root: Path, rel_base: Path) -> None:
    for path in _python_files(root):
        try:
            calls = list(extract_calls(path.read_text(encoding="utf-8")))
        except SyntaxError as exc:
            raise ModuleError(f"Cannot parse {path}: {exc}") from exc
        for msgid, line in calls:
            out.add(msgid, str(path.relative_to(rel_base)), line)


def _extract_models(out: Extraction, module: str) -> None:
    from tessera.orm import fields as F
    from tessera.orm.registry import fragments_for

    for frag in fragments_for(module):
        model = frag.__dict__.get("_name") or frag.__dict__.get("_extend") or "?"
        defines = "_name" in frag.__dict__
        where = f"model:{model}"
        out.add(frag.__dict__.get("_description"), where, comment="model description")
        for constraint in frag.__dict__.get("_sql_constraints", ()):
            if len(constraint) >= 3:
                out.add(constraint[2], where, comment="constraint message")
        for attr, value in frag.__dict__.items():
            if not isinstance(value, F.Field) or isinstance(value, F.Id):
                continue
            args = value._args
            label_where = f"field:{model}.{attr}"
            if args.get("string"):
                out.add(args["string"], label_where, comment="field label")
            elif defines or type(value) is not F.Field:  # a new field: its label is derived from its name
                value.name = value.name or attr
                out.add(value.label, label_where, comment="field label")
            if args.get("help"):
                out.add(args["help"], label_where, comment="field help")
            selection = args.get("selection")
            if isinstance(selection, list | tuple):
                for item in selection:
                    if isinstance(item, list | tuple) and len(item) >= 2:
                        out.add(item[1], f"selection:{model}.{attr}", comment=f"selection value {item[0]}")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ModuleError(f"Invalid JSON in {path}: {exc}") from exc


def _walk_view(out: Extraction, node: Any, where: str, top: bool = False) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_view(out, item, where)
    elif isinstance(node, dict):
        attrs = VIEW_TOP_ATTRS if top else VIEW_ATTRS
        for key, value in node.items():
            if key in attrs and isinstance(value, str):
                out.add(value, where, comment=f"view {key}")
            else:
                _walk_view(out, value, where)


def _extract_views(out: Extraction, path: Path, rel: str) -> None:
    for entry in _read_json(path):
        if not isinstance(entry, dict):
            continue
        where = f"{rel}:{entry.get('id', '?')}"
        if "inherit" in entry:
            for op in entry.get("operations") or []:
                if isinstance(op, dict):
                    _walk_view(out, op.get("node"), where)
                    for key, value in (op.get("attrs") or {}).items():
                        if key in VIEW_ATTRS and isinstance(value, str):
                            out.add(value, where, comment=f"view {key}")
            continue
        definition = entry.get("definition") or {k: v for k, v in entry.items() if k not in VIEW_META_KEYS}
        _walk_view(out, definition, where, top=True)


def _extract_menus_actions(out: Extraction, path: Path, rel: str, keys: Iterable[str], kind: str) -> None:
    for entry in _read_json(path):
        if not isinstance(entry, dict):
            continue
        for key in keys:
            out.add(entry.get(key), f"{rel}:{entry.get('id', '?')}", comment=f"{kind} {key}")


def _template_calls(text: str) -> Iterator[str]:
    for match in TEMPLATE_CALL_RE.finditer(text):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        yield raw.encode().decode("unicode_escape") if "\\" in raw else raw


def _extract_values(
    out: Extraction, registry: Registry, model_name: str, values: dict[str, Any], where: str, source_lang: str
) -> None:
    cls = registry.models.get(model_name) or registry.abstract.get(model_name)
    for name, value in values.items():
        fld = cls._fields.get(name) if cls is not None else None
        if isinstance(value, str):
            for msgid in _template_calls(value):
                out.add(msgid, where, comment="template text")
        if fld is None:
            continue
        if fld.translate:
            if isinstance(value, str):
                out.add(value, where, comment=f"{model_name}.{name}")
            elif isinstance(value, dict) and isinstance(value.get(source_lang), str):
                msgid = value[source_lang]
                out.add(msgid, where, comment=f"{model_name}.{name}")
                for lang, text in value.items():
                    if lang != source_lang and isinstance(text, str):
                        out.seed(lang, msgid, text)
        comodel = getattr(fld, "comodel_name", None)
        if comodel and isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _extract_values(out, registry, comodel, item, where, source_lang)


def _extract_data(out: Extraction, registry: Registry, path: Path, rel: str) -> None:
    for entry in _read_json(path):
        if not isinstance(entry, dict) or not entry.get("model") or not isinstance(entry.get("values"), dict):
            continue
        _extract_values(
            out, registry, entry["model"], entry["values"], f"{rel}:{entry.get('id', '?')}", registry.default_lang
        )


def extract_module(name: str, manifests: dict[str, Manifest] | None = None) -> Extraction:
    """Every translatable string of module ``name``, in source order."""
    from tessera.modules.loader import discover

    manifests = manifests if manifests is not None else discover()
    if name not in manifests:
        raise ModuleError(f"Unknown module '{name}'")
    manifest = manifests[name]
    registry = offline_registry([name], manifests)
    out = Extraction(name)
    if name == "core":
        for label in MAGIC_LABELS:
            out.add(label, "kernel", comment="field label")
    _extract_models(out, name)
    _extract_python(out, manifest.path, manifest.path)
    if name == "core":
        _extract_python(out, KERNEL_ROOT, KERNEL_ROOT.parent)
    for kind in ("security", "data"):
        for path in manifest.files(kind):
            _extract_data(out, registry, path, str(path.relative_to(manifest.path)))
    for path in manifest.files("views"):
        _extract_views(out, path, str(path.relative_to(manifest.path)))
    for path in manifest.files("menus"):
        _extract_menus_actions(out, path, str(path.relative_to(manifest.path)), ("name",), "menu")
    for path in manifest.files("actions"):
        _extract_menus_actions(out, path, str(path.relative_to(manifest.path)), ("name", "help"), "action")
    return out


# -- catalogue files -------------------------------------------------------------------------------

HEADER = (
    "Translations of the Tessera {module} module.\n"
    "Generated by `tessera i18n extract {module}`; translate the msgstr lines."
)


def _read_catalog(path: Path) -> Any:
    from babel.messages.pofile import read_po

    with path.open("rb") as handle:
        return read_po(handle)


def _new_catalog(module: str, lang: str | None, previous: Any = None) -> Any:
    from babel.messages.catalog import Catalog

    now = dt.datetime.now(dt.UTC)
    catalog = Catalog(
        locale=lang,
        project="Tessera",
        version="1",
        copyright_holder="Tessera contributors",
        msgid_bugs_address="",
        creation_date=getattr(previous, "creation_date", None) or now,
        revision_date=getattr(previous, "revision_date", None) or now,
        last_translator="Tessera contributors",
        language_team=lang or "LANGUAGE",
        header_comment="\n".join(f"# {line}" for line in HEADER.format(module=module).splitlines()),
        fuzzy=False,
    )
    return catalog


def _write(path: Path, catalog: Any) -> bool:
    from babel.messages.pofile import write_po

    buffer = io.BytesIO()
    write_po(buffer, catalog, width=100, sort_output=False, include_previous=False, omit_header=False)
    data = buffer.getvalue()
    if path.is_file() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def build_catalog(extraction: Extraction, lang: str | None, previous: Any = None) -> Any:
    """A Babel catalogue of ``extraction`` (a template when ``lang`` is None), keeping ``previous``
    translations (and their fuzzy flag) and filling missing ones from the sources' seeds."""
    catalog = _new_catalog(extraction.module, lang, previous)
    seeds = extraction.seeds.get(lang or "", {})
    for entry in extraction.entries.values():
        old = previous.get(entry.msgid) if previous is not None else None
        text = ""
        flags: set[str] = set()
        if lang:
            if old is not None and old.string:
                text = str(old.string)
                flags = {f for f in old.flags if f == "fuzzy"}
            else:
                text = seeds.get(entry.msgid, "")
        catalog.add(
            entry.msgid,
            text,
            locations=entry.locations,
            auto_comments=entry.comments,
            flags=flags,
        )
    return catalog


def write_module_catalogs(extraction: Extraction, module_dir: Path, langs: Iterable[str] = ()) -> list[Path]:
    """Write ``i18n/<module>.pot`` and update every existing ``i18n/<lang>.po`` (plus ``langs``, created
    when missing). Returns the files that changed."""
    folder = module_dir / "i18n"
    changed: list[Path] = []
    pot = folder / f"{extraction.module}.pot"
    previous_pot = _read_catalog(pot) if pot.is_file() else None
    if _write(pot, build_catalog(extraction, None, previous_pot)):
        changed.append(pot)
    targets = {p.stem for p in folder.glob("*.po")} if folder.is_dir() else set()
    targets.update(langs)
    for lang in sorted(targets):
        po = folder / f"{lang}.po"
        previous = _read_catalog(po) if po.is_file() else None
        if _write(po, build_catalog(extraction, lang, previous)):
            changed.append(po)
    return changed


@dataclass
class Completeness:
    module: str
    lang: str
    total: int
    translated: int
    missing: list[str]
    placeholder_errors: list[str]

    @property
    def ratio(self) -> float:
        return self.translated / self.total if self.total else 1.0


def completeness(extraction: Extraction, module_dir: Path, lang: str) -> Completeness:
    """How much of ``extraction`` the module's ``<lang>.po`` translates, and which translations do not
    keep the placeholders of their source text."""
    from tessera.i18n.catalog import read_po_file

    po = module_dir / "i18n" / f"{lang}.po"
    translations = read_po_file(po) if po.is_file() else {}
    missing = [m for m in extraction.msgids if not translations.get(m)]
    errors = [m for m in extraction.msgids if translations.get(m) and placeholders(m) != placeholders(translations[m])]
    total = len(extraction.entries)
    return Completeness(extraction.module, lang, total, total - len(missing), missing, errors)
