"""Server translations: messages, catalogues, extraction, catalogue completeness and the database side
(translatable records written on install / upgrade / ``tessera i18n load``)."""

from __future__ import annotations

import copy
import json
import pickle
import sys
from pathlib import Path

import pytest
from babel.messages.pofile import read_po, write_po
from typer.testing import CliRunner

from tessera.config import get_settings
from tessera.i18n import Message, _, language_scope, lookup, render, resolve_language, translatable, translate
from tessera.i18n.catalog import available_languages, catalog, reset_catalogs
from tessera.i18n.extract import completeness, extract_calls, extract_module, placeholders
from tessera.modules.loader import clear_discovery_cache, discover

BACKEND = Path(__file__).resolve().parents[3]
PHASE_1_MODULES = sorted(p.parent.name for p in (BACKEND / "modules").glob("*/manifest.toml"))
MIN_COMPLETENESS = 0.95


# -- messages ----------------------------------------------------------------------------------------


def test_messages_render_in_the_current_language() -> None:
    msg = _("Field '{field}' is required", field=translatable("Priority"))
    assert msg == "Field 'Priority' is required"
    assert msg.msgid == "Field '{field}' is required"
    assert msg.in_language("fr_FR") == "Le champ « Priorité » est obligatoire"
    with language_scope("fr_FR"):
        assert _("Record not found") == "Enregistrement introuvable"
        assert str(_("Invalid domain: {exc}", exc="boom")) == "Domaine invalide : boom"
    assert _("Record not found") == "Record not found"


def test_unknown_texts_and_languages_fall_back_to_english() -> None:
    assert lookup("A text nobody translated", "fr_FR") == "A text nobody translated"
    assert lookup("Record not found", "de_DE") == "Record not found"
    assert lookup("Record not found", "en_US") == "Record not found"
    assert translate("Record not found", "fr_FR") == "Enregistrement introuvable"
    assert translate(42, "fr_FR") == 42
    assert _("Unheard of {thing}", thing="x").in_language("fr_FR") == "Unheard of x"
    # plain parameters are data and are not translated; message parameters are
    assert render("{a} / {b}", {"a": "Record not found", "b": _("Record not found")}, "fr_FR") == (
        "Record not found / Enregistrement introuvable"
    )


def test_a_broken_translation_keeps_the_source_text(monkeypatch: pytest.MonkeyPatch) -> None:
    import tessera.i18n.message as message

    monkeypatch.setattr(message, "lookup", lambda msgid, lang, modules=None: "Cassé {unknown}")
    assert message.render("Hello {name}", {"name": "Ada"}, "fr_FR") == "Hello Ada"
    monkeypatch.setattr(message, "lookup", lambda msgid, lang, modules=None: msgid)
    assert message.render("Hello {name}", {}, "fr_FR") == "Hello {name}"  # no params: no formatting
    assert message.render("Hello {name}", {"other": 1}, "fr_FR") == "Hello {name}"


def test_messages_pickle_and_copy_as_plain_text() -> None:
    msg = _("Record not found")
    assert type(pickle.loads(pickle.dumps(msg))) is str
    assert type(copy.deepcopy({"label": msg})["label"]) is str
    assert isinstance(Message("x {y}", {"y": 1}, lang="fr_FR"), str)


def test_language_resolution() -> None:
    assert "fr_FR" in available_languages()
    assert resolve_language("fr_FR") == "fr_FR"
    assert resolve_language("fr_BE") == "fr_FR"
    assert resolve_language("fr") == "fr_FR"
    assert resolve_language("en_US") is None
    assert resolve_language("xx_XX") is None
    assert resolve_language(None) is None
    assert catalog("en_US") == {}
    assert catalog("fr_FR", ["web"])["Settings"] == "Paramètres"
    assert lookup("Settings", "fr_FR", modules=["web"], module="web") == "Paramètres"
    reset_catalogs()
    assert lookup("Settings", "fr_FR") == "Paramètres"


def test_extraction_helpers() -> None:
    source = 'x = _("Hello {name}", name=n)\ny = _(variable)\nz = other("Nope")\n'
    assert list(extract_calls(source)) == [("Hello {name}", 1)]
    assert placeholders("{b} and {a:.2f} and {{literal}}") == ["a", "b"]


# -- catalogue completeness (U-8) --------------------------------------------------------------------


@pytest.mark.parametrize("module", PHASE_1_MODULES)
def test_french_catalogue_is_complete(module: str) -> None:
    """Every Phase 1 module translates at least 95 % of its strings into French, and every translation
    keeps the ``{placeholders}`` of its source text. Run ``tessera i18n extract`` then translate the new
    ``msgstr`` lines of ``i18n/fr_FR.po`` when this fails."""
    manifests = discover()
    report = completeness(extract_module(module, manifests), manifests[module].path, "fr_FR")
    assert report.total > 0
    assert report.ratio >= MIN_COMPLETENESS, (
        f"{module}: fr_FR is {report.ratio:.1%} complete; missing: {report.missing[:20]}"
    )
    assert not report.placeholder_errors, f"{module}: placeholders differ in {report.placeholder_errors}"


# -- database ----------------------------------------------------------------------------------------


@pytest.mark.db
def test_module_records_carry_their_translations(env) -> None:  # type: ignore[no-untyped-def]
    menu = env.ref("web.menu_settings_root")
    assert menu.with_context(lang="fr_FR").name == "Paramètres"
    assert menu.with_context(lang="en_US").name == "Settings"
    raw = env.cr.scalar("SELECT name FROM web_menu WHERE id = :i", {"i": menu.ids[0]})
    assert raw["fr_FR"] == "Paramètres" and raw["en_US"] == "Settings"
    stage = env.ref("crm.stage_qualified")
    assert stage.with_context(lang="fr_FR").name == "Qualifiée"
    transition = env.ref("sale.tr_cancel").with_context(lang="fr_FR")
    assert (transition.name, transition.confirm) == ("Annuler", "Annuler ce devis ?")
    # a record without a translation falls back to English
    created = env["web.menu"].create({"name": "No translation for this menu"})
    assert created.with_context(lang="fr_FR").name == "No translation for this menu"


@pytest.mark.db
def test_apply_keeps_edited_translations_of_noupdate_records(env) -> None:  # type: ignore[no-untyped-def]
    from tessera.i18n.apply import apply_module_translations, apply_translations

    france = env.ref("core.country_fr")
    env.cr.execute(
        'UPDATE core_country SET name = name || \'{"fr_FR": "Hexagone"}\'::jsonb WHERE id = :i', {"i": france.ids[0]}
    )
    env.cache.clear()
    apply_module_translations(env, "core")
    assert france.with_context(lang="fr_FR").name == "Hexagone"
    result = apply_translations(env, ["core"], langs=["fr_FR"], overwrite=True)
    assert result["core"].records >= 1
    assert france.with_context(lang="fr_FR").name == "France"
    assert apply_module_translations(env, "core", langs=["en_US"]).records == 0


@pytest.mark.db
def test_notifications_are_written_in_the_recipients_language(env) -> None:  # type: ignore[no-untyped-def]
    users = env["core.user"].elevated().search([("login", "in", ["marie", "demo"])])
    by_login = {u.login: u.ids[0] for u in users}
    notes = env["core.notification"].notify(
        list(by_login.values()), _("Import finished: {filename}", filename="leads.csv"), "plain body"
    )
    titles = {n.user_id.login: n.title for n in notes.elevated()}
    assert titles == {"marie": "Import terminé : leads.csv", "demo": "Import finished: leads.csv"}
    assert set(notes.elevated().mapped("body")) == {"plain body"}


@pytest.mark.db
def test_reports_render_template_texts_in_the_document_language(env) -> None:  # type: ignore[no-untyped-def]
    from tessera.reports.engine import render_html

    body = '<p>{{ _("Valid until: {date}", date="2026-12-01") }}</p><p>{{ _("Qty") }}</p>'
    french = render_html(body, {"docs": None, "lang": "fr_FR"})
    assert "Valable jusqu&#39;au : 2026-12-01" in french and "Qté" in french
    english = render_html(body, {"docs": None})
    assert "Valid until: 2026-12-01" in english


# -- lifecycle of a module with a catalogue -----------------------------------------------------------

MODULE = "ktest_i18n"
FILES = {
    "manifest.toml": f'''name = "{MODULE}"
version = "0.1.0"
summary = "i18n lifecycle test"
depends = ["web"]

[load]
security = ["security/access.json"]
data = ["data/tags.json"]
views = ["views.json"]
actions = "actions.json"
menus = "menus.json"
''',
    "__init__.py": "",
    "models/__init__.py": "",
    "models/tag.py": f'''from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields


class Tag(Model):
    _name = "{MODULE}.tag"
    _description = "Colour tag"

    name = fields.Char("Tag name", required=True, translate=True)
    kind = fields.Selection([("warm", "Warm colour"), ("cold", "Cold colour")], default="warm", help="Colour family")

    @api.constrains("name")
    def _check_name(self):
        for tag in self:
            if tag.name == "forbidden":
                raise ValidationError(_("The tag {{name}} is forbidden", name=tag.name), field="name")
''',
    "security/access.json": json.dumps(
        [
            {
                "id": "access_tag",
                "model": "core.model_access",
                "values": {
                    "name": "tag",
                    "model_name": f"{MODULE}.tag",
                    "group_id": "core.group_user",
                    "perm_read": True,
                    "perm_write": True,
                    "perm_create": True,
                    "perm_unlink": True,
                },
            }
        ]
    ),
    "data/tags.json": json.dumps(
        [
            {"id": "tag_red", "model": f"{MODULE}.tag", "values": {"name": "Red", "kind": "warm"}},
            {"id": "tag_blue", "model": f"{MODULE}.tag", "noupdate": True, "values": {"name": "Blue", "kind": "cold"}},
        ]
    ),
    "views.json": json.dumps(
        [
            {
                "id": "view_tag_form",
                "model": f"{MODULE}.tag",
                "type": "form",
                "root": {"kind": "sheet", "children": [{"kind": "group", "title": "Colour details", "children": []}]},
            }
        ]
    ),
    "actions.json": json.dumps([{"id": "action_tags", "name": "Colour tags", "model": f"{MODULE}.tag"}]),
    "menus.json": json.dumps([{"id": "menu_tags", "name": "Colour tags", "action": f"{MODULE}.action_tags"}]),
}
FRENCH = {
    "Colour tag": "Étiquette de couleur",
    "Tag name": "Nom de l'étiquette",
    "Warm colour": "Couleur chaude",
    "Cold colour": "Couleur froide",
    "Colour family": "Famille de couleurs",
    "The tag {name} is forbidden": "L'étiquette {name} est interdite",
    "Red": "Rouge",
    "Colour details": "Détails de la couleur",
    "Colour tags": "Étiquettes de couleur",
}


@pytest.fixture
def i18n_module(tmp_path: Path):  # type: ignore[no-untyped-def]
    for rel, text in FILES.items():
        target = tmp_path / MODULE / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    settings = get_settings()
    settings.module_paths.append(tmp_path)
    clear_discovery_cache()
    reset_catalogs()
    try:
        yield tmp_path / MODULE
    finally:
        settings.module_paths.remove(tmp_path)
        clear_discovery_cache()
        reset_catalogs()


def _translate_po(path: Path, table: dict[str, str]) -> None:
    with path.open("rb") as handle:
        po = read_po(handle)
    for msg in po:
        if msg.id in table:
            msg.string = table[msg.id]
    with path.open("wb") as handle:
        write_po(handle, po, width=100)


@pytest.mark.db
def test_install_upgrade_load_and_uninstall_a_translated_module(registry, database_url: str, i18n_module: Path):  # type: ignore[no-untyped-def]
    from tessera.cli.main import app as cli
    from tessera.i18n.apply import apply_translations
    from tessera.modules.installer import check_schema, install_modules, uninstall_modules, upgrade_modules
    from tessera.orm.environment import environment_for
    from tessera.orm.registry import Registry, forget_module_fragments

    runner = CliRunner()
    # -- extract: the template and an empty French catalogue ----------------------------------------
    extracted = runner.invoke(cli, ["i18n", "extract", MODULE, "--lang", "fr_FR"])
    assert extracted.exit_code == 0, extracted.output
    pot = (i18n_module / "i18n" / f"{MODULE}.pot").read_text()
    for msgid in [*FRENCH, "Blue", "Kind"]:
        assert f'msgid "{msgid}"' in pot
    assert "up to date" in runner.invoke(cli, ["i18n", "extract", MODULE]).output
    po_path = i18n_module / "i18n" / "fr_FR.po"
    _translate_po(po_path, FRENCH)  # "Blue" and "Kind" stay untranslated
    report = completeness(extract_module(MODULE), i18n_module, "fr_FR")
    assert set(report.missing) == {"Blue", "Kind"} and not report.placeholder_errors
    stats = runner.invoke(cli, ["i18n", "stats", MODULE, "--missing"])
    assert stats.exit_code == 0 and "Blue" in stats.output

    # -- install: catalogues are applied to the module's records -------------------------------------
    try:
        install_modules([MODULE], database_url=database_url)
        with environment_for(database_url, uid=1, su=True) as env:
            fr = env.with_context(lang="fr_FR")
            assert fr.ref(f"{MODULE}.menu_tags").name == "Étiquettes de couleur"
            assert fr.ref(f"{MODULE}.action_tags").name == "Étiquettes de couleur"
            assert fr.ref(f"{MODULE}.tag_red").name == "Rouge"
            assert fr.ref(f"{MODULE}.tag_blue").name == "Blue"  # no translation: English
            assert env.ref(f"{MODULE}.tag_red").name == "Red"
            described = fr[f"{MODULE}.tag"].fields_get(["name", "kind"])
            assert described["name"]["string"] == "Nom de l'étiquette"
            assert described["kind"]["help"] == "Famille de couleurs"
            assert dict(described["kind"]["selection"]) == {"warm": "Couleur chaude", "cold": "Couleur froide"}
            assert type(fr[f"{MODULE}.tag"])._description_in("fr_FR") == "Étiquette de couleur"
            with language_scope("fr_FR"), pytest.raises(Exception, match="L'étiquette forbidden est interdite"):
                env[f"{MODULE}.tag"].create({"name": "forbidden"})

        # -- upgrade with an updated catalogue -------------------------------------------------------
        _translate_po(po_path, {"Red": "Rouge vif", "Blue": "Bleu"})
        manifest = i18n_module / "manifest.toml"
        manifest.write_text(manifest.read_text().replace('version = "0.1.0"', 'version = "0.1.1"'))
        clear_discovery_cache()
        upgrade_modules([MODULE], database_url=database_url)
        with environment_for(database_url, uid=1, su=True) as env:
            fr = env.with_context(lang="fr_FR")
            assert fr.ref(f"{MODULE}.tag_red").name == "Rouge vif"
            assert fr.ref(f"{MODULE}.tag_blue").name == "Bleu"  # noupdate: a missing language is filled
            blue = fr.ref(f"{MODULE}.tag_blue")
            blue.write({"name": "Azur"})  # an administrator's own translation
        _translate_po(po_path, {"Blue": "Bleu clair"})
        loaded = runner.invoke(cli, ["i18n", "load", MODULE, "--lang", "fr_FR"])
        assert loaded.exit_code == 0, loaded.output
        with environment_for(database_url, uid=1, su=True) as env:
            assert env.with_context(lang="fr_FR").ref(f"{MODULE}.tag_blue").name == "Azur"
            apply_translations(env, [MODULE], overwrite=True)
            assert env.with_context(lang="fr_FR").ref(f"{MODULE}.tag_blue").name == "Bleu clair"
        assert not [d for d in check_schema(database_url=database_url) if MODULE.replace(".", "_") in str(d)]
    finally:
        uninstall_modules([MODULE], database_url=database_url, drop_data=True)
        for mod_name in [m for m in sys.modules if MODULE in m]:
            del sys.modules[mod_name]
        forget_module_fragments(MODULE)
        Registry.forget(database_url)
        Registry.get(database_url)
    with environment_for(database_url, uid=1, su=True) as env:
        assert env["core.module_record"].search_count([("module", "=", MODULE)]) == 0
        assert not env["web.menu"].search([("name", "=", "Colour tags")])
