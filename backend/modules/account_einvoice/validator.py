"""Validation of UBL documents with the official artefacts vendored in ``validation/`` (see its README):

1. the OASIS UBL 2.1 XML schemas (Invoice, CreditNote);
2. the CEN EN 16931 UBL rules (compiled XSLT published by CEN/TC 434: BR-*, BR-CO-*, BR-S/Z/E/AE/IC/G-*);
3. the OpenPeppol BIS Billing 3.0 rules (``PEPPOL-EN16931-R*``, ``PEPPOL-COMMON-*``), compiled from the
   published schematron with the ISO skeleton.

Schematron XSLTs are XSLT 2.0 and run with Saxon-HE (``saxonche``); they are compiled once per process.
Every finding is a :class:`Finding` (rule id, flag, location, message) so it can be stored and explained.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from typing import Any

from lxml import etree

ROOT = Path(__file__).resolve().parent / "validation"
SCHEMAS = {  # entry points that import the unmodified OASIS schemas (see validation/README.md)
    "Invoice": ROOT / "xsd" / "tessera-ubl-invoice.xsd",
    "CreditNote": ROOT / "xsd" / "tessera-ubl-creditnote.xsd",
}
SCHEMATRONS = {
    "CEN-EN16931": ROOT / "schematron" / "CEN-EN16931-UBL.xslt",
    "PEPPOL-BIS3": ROOT / "schematron" / "PEPPOL-EN16931-UBL.xslt",
}
SVRL = "{http://purl.oclc.org/dsdl/svrl}"
_lock = threading.Lock()


@dataclass
class Finding:
    rule_id: str
    flag: str  # fatal / warning / information / xsd
    location: str
    message: str
    source: str  # XSD / CEN-EN16931 / PEPPOL-BIS3

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@cache
def _schema(kind: str) -> etree.XMLSchema:
    return etree.XMLSchema(etree.parse(str(SCHEMAS[kind])))


def validate_xsd(xml: bytes) -> list[Finding]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(xml, parser=parser)
    except etree.XMLSyntaxError as exc:
        return [Finding("XML", "fatal", "/", str(exc), "XSD")]
    kind = etree.QName(root).localname
    if kind not in SCHEMAS:
        return [Finding("UBL-ROOT", "fatal", "/", f"Unsupported document type {kind}", "XSD")]
    schema = _schema(kind)
    if schema.validate(root):
        return []
    return [
        Finding("XSD", "fatal", f"line {err.line}", err.message, "XSD")
        for err in schema.error_log  # type: ignore[attr-defined]
    ]


class _Saxon:
    """Saxon processor and compiled schematron executables (kept for the life of the process)."""

    _instance: _Saxon | None = None

    def __init__(self) -> None:
        from saxonche import PySaxonProcessor

        self.proc = PySaxonProcessor(license=False)
        xslt = self.proc.new_xslt30_processor()
        self.executables = {
            name: xslt.compile_stylesheet(stylesheet_file=str(path)) for name, path in SCHEMATRONS.items()
        }

    @classmethod
    def get(cls) -> _Saxon:
        with _lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance


def schematron_available() -> bool:
    try:
        import saxonche  # noqa: F401
    except ImportError:  # pragma: no cover - saxonche is a declared dependency
        return False
    return all(p.is_file() for p in SCHEMATRONS.values())


def validate_schematron(xml: bytes) -> list[Finding]:
    saxon = _Saxon.get()
    findings: list[Finding] = []
    with _lock:
        node = saxon.proc.parse_xml(xml_text=xml.decode("utf-8"))
        reports = {name: ex.transform_to_string(xdm_node=node) for name, ex in saxon.executables.items()}
    for source, report in reports.items():
        root = etree.fromstring(report.encode("utf-8"))
        for failed in root.iter(f"{SVRL}failed-assert", f"{SVRL}successful-report"):
            text = failed.find(f"{SVRL}text")
            findings.append(
                Finding(
                    rule_id=failed.get("id") or "",
                    flag=failed.get("flag") or "fatal",
                    location=failed.get("location") or "",
                    message=" ".join((text.text or "").split()) if text is not None else "",
                    source=source,
                )
            )
    return findings


def validate(xml: bytes) -> list[Finding]:
    """Every finding of the XSD and of both schematrons (the schematrons run only on schema-valid XML)."""
    findings = validate_xsd(xml)
    if findings:
        return findings
    return validate_schematron(xml)


def fatal(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.flag == "fatal"]


def compile_schematron(sch: Path, out: Path) -> Path:
    """Compile an ISO schematron (XSLT 2 binding) to an XSLT with the vendored ISO skeleton (include,
    abstract-pattern expansion, SVRL output)."""
    from saxonche import PySaxonProcessor

    skeleton = ROOT / "iso-schematron"
    steps = ["iso_dsdl_include.xsl", "iso_abstract_expand.xsl", "iso_svrl_for_xslt2.xsl"]
    with PySaxonProcessor(license=False) as proc:
        xslt = proc.new_xslt30_processor()
        current = str(sch)
        for i, step in enumerate(steps):
            target = out if i == len(steps) - 1 else out.with_suffix(f".step{i}.xml")
            xslt.compile_stylesheet(stylesheet_file=str(skeleton / step)).transform_to_file(
                source_file=current, output_file=str(target)
            )
            current = str(target)
    for i in range(len(steps) - 1):
        out.with_suffix(f".step{i}.xml").unlink(missing_ok=True)
    return out
