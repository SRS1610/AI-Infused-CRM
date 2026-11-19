"""Rendering engine.

Templates are Jinja2 evaluated in a ``SandboxedEnvironment`` with autoescaping: they can read record
fields and call a few helpers, but cannot reach Python internals, write records, or call methods with
side effects (unsafe attribute names and callables are blocked by the sandbox; records expose only
fields, ``display_name`` and ``ids``). Rendering happens as the requesting user, so field access
rules apply to what a template can print.
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import io
from collections.abc import Mapping
from typing import Any

from jinja2 import StrictUndefined, pass_context, select_autoescape
from jinja2.sandbox import SandboxedEnvironment
from markupsafe import Markup

from tessera.exceptions import UserError
from tessera.i18n import _

ADMIN_ONLY_GROUP = "core.group_system"  # fields restricted to it hold secrets
PAPER = {"A4": ("210mm", "297mm"), "Letter": ("8.5in", "11in"), "Legal": ("8.5in", "14in"), "A5": ("148mm", "210mm")}


class RecordProxy:
    """Read-only view of a record for templates (fields, display_name, id; relations proxied)."""

    __slots__ = ("_rec",)

    def __init__(self, record: Any) -> None:
        object.__setattr__(self, "_rec", record)

    def __getattr__(self, name: str) -> Any:
        rec = object.__getattribute__(self, "_rec")
        if name.startswith("_"):
            raise AttributeError(name)
        if name == "id":
            return rec.ids[0] if len(rec.ids) == 1 else None
        if name == "display_name":
            return rec.display_name
        if name not in rec._fields:
            raise AttributeError(f"{rec._name} has no field '{name}'")
        if rec.env.su and ADMIN_ONLY_GROUP in (rec._fields[name].groups or ()):
            # administrator-only fields hold secrets (password hashes, TOTP seeds, tokens). Templates get some
            # records elevated (company, user, a customer's quotation), which skips field access: such a
            # record never shows them. Non-elevated records keep the ORM's own field access.
            raise AttributeError(f"{rec._name}.{name} cannot be used in a template")
        return wrap(rec[name])

    def __iter__(self) -> Any:
        rec = object.__getattribute__(self, "_rec")
        return (RecordProxy(r) for r in rec)

    def __len__(self) -> int:
        return len(object.__getattribute__(self, "_rec"))

    def __bool__(self) -> bool:
        return bool(object.__getattribute__(self, "_rec"))

    def __str__(self) -> str:
        rec = object.__getattribute__(self, "_rec")
        return ", ".join(rec.mapped("display_name"))


def wrap(value: Any) -> Any:
    return RecordProxy(value) if hasattr(value, "_ids") and hasattr(value, "_fields") else value


class ReportSandbox(SandboxedEnvironment):
    def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
        if attr.startswith("_"):
            return False
        return super().is_safe_attribute(obj, attr, value)


def qr_data_uri(data: str, scale: int = 4) -> Markup:
    import segno

    qr = segno.make(str(data), error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=2)
    encoded = base64.b64encode(buf.getvalue()).decode()
    return Markup(f'<img class="qr" alt="QR code" src="data:image/png;base64,{encoded}"/>')  # noqa: S704 - base64 only


def _money(value: Any, currency: Any = None, digits: int = 2) -> str:
    amount = float(value or 0)
    symbol = ""
    position = "before"
    if currency is not None:
        cur = object.__getattribute__(currency, "_rec") if isinstance(currency, RecordProxy) else currency
        if cur:
            symbol = cur.symbol or cur.name or ""
            position = getattr(cur, "position", None) or "before"
            digits = getattr(cur, "decimal_places", None) or digits
    text = f"{amount:,.{digits}f}"
    if not symbol:
        return text
    nbsp = chr(0xA0)
    return f"{symbol}{nbsp}{text}" if position == "before" else f"{text}{nbsp}{symbol}"


def _nl2br(text: Any) -> Markup:
    return Markup("<br/>".join(html.escape(str(text or "")).splitlines()))  # noqa: S704 - escaped above


def _date(value: Any, fmt: str = "%Y-%m-%d") -> str:
    if not value:
        return ""
    if isinstance(value, str):
        value = dt.date.fromisoformat(value[:10])
    return str(value.strftime(fmt))


@pass_context
def _translate(context: Any, msgid: str, **params: Any) -> str:
    """``{{ _("Valid until: {date}", date=...) }}``: the text in the document's language (``lang``)."""
    from tessera.i18n import render

    lang = context.get("lang")
    return render(str(msgid), params, str(lang) if lang else None)


def document_language(doc: Any, default: str | None) -> str | None:
    """A document is printed in its partner's language when it has one, else in ``default``."""
    if doc is None or "partner_id" not in getattr(doc, "_fields", {}):
        return default
    partner = doc.partner_id
    lang = getattr(partner.elevated(), "lang", None) if len(partner) == 1 else None
    return str(lang) if lang else default


def make_environment() -> ReportSandbox:
    env = ReportSandbox(autoescape=select_autoescape(default=True, default_for_string=True), undefined=StrictUndefined)
    env.filters.update(
        {
            "money": _money,
            "date": _date,
            "qr": qr_data_uri,
            "nl2br": _nl2br,
        }
    )
    env.globals.update({"qr": qr_data_uri, "now": lambda: dt.datetime.now(dt.UTC), "_": _translate})
    return env


_ENV = make_environment()


def render_html(
    body: str, context: Mapping[str, Any], *, layout: Mapping[str, Any] | None = None, title: str = "Report"
) -> str:
    """Render ``body`` (a Jinja template) for each document and wrap it in the layout's page frame."""
    try:
        template = _ENV.from_string(body)
    except Exception as exc:
        raise UserError(_("Template error: {exc}", exc=exc)) from exc
    docs = context.get("docs")
    pages = []
    try:
        for doc in docs if docs is not None else [None]:
            ctx = dict(context)
            ctx["doc"] = wrap(doc) if doc is not None else None
            ctx["lang"] = document_language(doc, context.get("lang"))
            pages.append(template.render(**ctx))
    except UserError:
        raise
    except Exception as exc:
        raise UserError(_("Template error: {exc}", exc=exc)) from exc
    return _frame(pages, layout or {}, title, context)


def _frame(pages: list[str], layout: Mapping[str, Any], title: str, context: Mapping[str, Any]) -> str:
    width, height = PAPER.get(str(layout.get("paper") or "A4"), PAPER["A4"])
    if layout.get("orientation") == "landscape":
        width, height = height, width
    margin = layout.get("margin") or "18mm 15mm 20mm 15mm"
    color = layout.get("primary_color") or "#1f4e79"
    font = layout.get("font") or "DejaVu Sans, Helvetica, Arial, sans-serif"
    header = _ENV.from_string(layout.get("header_html") or "").render(**context) if layout.get("header_html") else ""
    footer = _ENV.from_string(layout.get("footer_html") or "").render(**context) if layout.get("footer_html") else ""
    logo = layout.get("logo_data_uri")
    logo_html = f'<img class="logo" src="{html.escape(logo)}" alt="logo"/>' if logo else ""
    css = f"""
@page {{ size: {width} {height}; margin: {margin};
  @top-center {{ content: element(header); }}
  @bottom-center {{ content: element(footer); }}
  @bottom-right {{ content: "Page " counter(page) " / " counter(pages); font-size: 8pt; color: #777; }} }}
body {{ font-family: {font}; font-size: 10pt; color: #222; }}
.page-header {{ position: running(header); border-bottom: 2px solid {color}; padding-bottom: 4px; width: 100%; }}
.page-footer {{ position: running(footer); border-top: 1px solid #ccc; font-size: 8pt; color: #555; width: 100%; }}
.logo {{ max-height: 14mm; }}
h1, h2, h3 {{ color: {color}; }}
table.data {{ width: 100%; border-collapse: collapse; }}
table.data th {{ background: {color}; color: #fff; text-align: left; padding: 4px; }}
table.data td {{ border-bottom: 1px solid #ddd; padding: 4px; }}
.doc {{ page-break-after: always; }} .doc:last-child {{ page-break-after: auto; }}
img.qr {{ width: 30mm; height: 30mm; }}
{layout.get("extra_css") or ""}
"""
    body = "".join(f'<section class="doc">{p}</section>' for p in pages)
    head = f"<meta charset='utf-8'><title>{html.escape(title)}</title><style>{css}</style>"
    running = f"<div class='page-header'>{logo_html}{header}</div><div class='page-footer'>{footer}</div>"
    return f"<!doctype html><html><head>{head}</head><body>{running}{body}</body></html>"


def render_pdf(html_doc: str) -> bytes:
    """Print to PDF. Only ``data:`` resources are loaded: a template must not make the server fetch a URL
    (``http://169.254.169.254/…``) or read a local file (``<a rel="attachment" href="file:///…">`` would embed
    it in the PDF). Logos and QR codes are inlined as data URIs already."""
    from weasyprint import HTML
    from weasyprint.urls import URLFetcher

    fetcher = URLFetcher(allowed_protocols={"data"}, allow_redirects=False)
    return bytes(HTML(string=html_doc, base_url=None, url_fetcher=fetcher).write_pdf())


# -- basic block editor --------------------------------------------------------------------------------

BLOCK_TYPES = {"heading", "text", "field", "fields", "table", "qr", "spacer", "page_break", "html"}


def _expr(path: str, root: str = "doc") -> str:
    parts = [p for p in str(path).split(".") if p]
    if not parts or any(not p.isidentifier() or p.startswith("_") for p in parts):
        raise UserError(_("Invalid field path '{path}'", path=path))
    return root + "." + ".".join(parts)


def blocks_to_template(blocks: list[dict[str, Any]]) -> str:
    """Compile the block editor's JSON into a template body. Text is escaped; only ``html`` blocks
    (administrators) carry raw markup."""
    out: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind not in BLOCK_TYPES:
            raise UserError(_("Unknown block type '{kind}'", kind=kind))
        if kind == "heading":
            level = min(max(int(block.get("level", 2)), 1), 3)
            out.append(f"<h{level}>{html.escape(str(block.get('text', '')))}</h{level}>")
        elif kind == "text":
            out.append(f"<p>{html.escape(str(block.get('text', '')))}</p>")
        elif kind == "field":
            label = html.escape(str(block.get("label", "")))
            value = (
                "{{ "
                + _expr(block["field"])
                + (" | money(doc.currency_id)" if block.get("widget") == "money" else "")
                + " }}"
            )
            out.append(f"<p><strong>{label}</strong> {value}</p>" if label else f"<p>{value}</p>")
        elif kind == "fields":
            rows = "".join(
                f"<tr><th>{html.escape(str(f.get('label', f['field'])))}</th>"
                f"<td>{{{{ {_expr(f['field'])} }}}}</td></tr>"
                for f in block.get("fields", [])
            )
            out.append(f"<table class='fields'>{rows}</table>")
        elif kind == "table":
            source = _expr(block["source"])
            cols = block.get("columns", [])
            head = "".join(f"<th>{html.escape(str(c.get('label', c['field'])))}</th>" for c in cols)
            cells = "".join("<td>{{ " + _expr(c["field"], "line") + " }}</td>" for c in cols)
            loop = f"{{% for line in {source} %}}<tr>{cells}</tr>{{% endfor %}}"
            out.append(f"<table class='data'><thead><tr>{head}</tr></thead><tbody>{loop}</tbody></table>")
        elif kind == "qr":
            qr_value = block.get("value")
            if qr_value is not None:
                out.append("{{ qr(" + repr(str(qr_value)) + ") }}")
            else:
                out.append("{{ qr(" + _expr(block["field"]) + ") }}")
        elif kind == "spacer":
            out.append(f"<div style='height:{int(block.get('height', 8))}mm'></div>")
        elif kind == "page_break":
            out.append("<div style='page-break-after: always'></div>")
        elif kind == "html":
            out.append(str(block.get("html", "")))
    return "\n".join(out)
