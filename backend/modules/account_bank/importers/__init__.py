"""Bank statement file parsers: format detection and dispatch."""

from __future__ import annotations

from typing import Any

from modules.account_bank.importers import camt, csv_import, mt940, ofx
from modules.account_bank.importers.base import ParsedStatement, ParsedTransaction
from tessera.exceptions import UserError
from tessera.i18n import _

FORMATS = ("camt053", "ofx", "mt940", "csv")

__all__ = ["FORMATS", "ParsedStatement", "ParsedTransaction", "detect_format", "parse_file"]


def detect_format(content: bytes, filename: str | None = None) -> str:
    name = (filename or "").lower()
    if camt.is_camt(content):
        return "camt053"
    if ofx.is_ofx(content) or name.endswith((".ofx", ".qfx")):
        return "ofx"
    if mt940.is_mt940(content) or name.endswith((".sta", ".mt940", ".940")):
        return "mt940"
    if name.endswith((".csv", ".txt", ".tsv")) or b"," in content[:2000] or b";" in content[:2000]:
        return "csv"
    raise UserError(_("Unrecognised statement format: use CSV, OFX, CAMT.053 or MT940"))


def parse_file(
    content: bytes,
    filename: str | None = None,
    *,
    file_format: str | None = None,
    options: dict[str, Any] | None = None,
) -> tuple[str, list[ParsedStatement]]:
    """(format, statements) of a statement file."""
    if not content or not content.strip():
        raise UserError(_("The file is empty"))
    kind = file_format or detect_format(content, filename)
    if kind == "camt053":
        return kind, camt.parse(content)
    if kind == "ofx":
        return kind, ofx.parse(content)
    if kind == "mt940":
        return kind, mt940.parse(content)
    if kind == "csv":
        return kind, csv_import.parse(content, options)
    raise UserError(_("Unknown statement format '{format}'", format=kind))
