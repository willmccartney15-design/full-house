"""Jinja2 templates rendered to PDF with WeasyPrint."""

from __future__ import annotations

from pathlib import Path

import logging

from jinja2 import Environment, FileSystemLoader, select_autoescape

# WeasyPrint warns about optional HarfBuzz subsetting on every render; it is harmless.
logging.getLogger("weasyprint").setLevel(logging.ERROR)
logging.getLogger("fontTools").setLevel(logging.ERROR)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html"]))
_env.filters["money"] = lambda v: "–" if v is None else f"${v:,.0f}"
_env.filters["pct"] = lambda v: "–" if v is None else f"{v:+.1f}%"
_env.filters["title_room"] = lambda v: str(v).replace("_", " ").title()


def render_html(template: str, context: dict) -> str:
    return _env.get_template(template).render(**context)


class RenderedPdf(type(Path())):
    """A Path to the PDF that also knows how many pages it has."""

    pages: int = 0


def render_pdf(template: str, context: dict, out_path: Path) -> RenderedPdf:
    from weasyprint import HTML  # imported lazily: needs system Pango

    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = render_html(template, context)
    out_path.with_suffix(".html").write_text(html)
    document = HTML(string=html, base_url=str(TEMPLATE_DIR)).render()
    document.write_pdf(out_path)
    result = RenderedPdf(out_path)
    result.pages = len(document.pages)
    return result
