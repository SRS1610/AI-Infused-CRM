"""Report rendering: sandboxed Jinja2 templates → HTML → PDF (WeasyPrint)."""

from tessera.reports.engine import blocks_to_template, qr_data_uri, render_html, render_pdf

__all__ = ["blocks_to_template", "qr_data_uri", "render_html", "render_pdf"]
