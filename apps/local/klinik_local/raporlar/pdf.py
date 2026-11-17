"""PDF building blocks for the reports: A4, the clinic's colours, Turkish-capable font.

Every page carries the logo and report title at the top, and at the bottom who created
it and when, "Gizli" (confidential) and "Sayfa x / y".
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Flowable,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from klinik_shared.timeutil import format_datetime_tr

FONTS = Path(__file__).parent / "fonts"

GREEN = colors.HexColor("#356650")
GREEN_DARK = colors.HexColor("#1f3d30")
GREEN_LIGHT = colors.HexColor("#e6efe9")
BEIGE = colors.HexColor("#f5efe3")
BEIGE_LIGHT = colors.HexColor("#fbf8f2")
BORDER = colors.HexColor("#ebe1cf")
BROWN = colors.HexColor("#504845")
MUTED = colors.HexColor("#6f6660")
TEXT = colors.HexColor("#2b2826")

_registered = False


def register_fonts() -> None:
    global _registered
    if _registered:
        return
    pdfmetrics.registerFont(TTFont("DejaVu", str(FONTS / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(FONTS / "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVu-Oblique", str(FONTS / "DejaVuSans-Oblique.ttf")))
    pdfmetrics.registerFontFamily(
        "DejaVu", normal="DejaVu", bold="DejaVu-Bold", italic="DejaVu-Oblique",
        boldItalic="DejaVu-Bold",
    )  # fmt: skip
    _registered = True


def _logo() -> Path:
    from klinik_shared.web import UI_DIR

    return Path(UI_DIR) / "static" / "img" / "mark-96.png"


# ---------------------------------------------------------------- styles

register_fonts()
BODY = ParagraphStyle("body", fontName="DejaVu", fontSize=9, leading=12.5, textColor=TEXT)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=7.5, leading=10, textColor=MUTED)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=8, leading=10.5)
CELL_BOLD = ParagraphStyle("cellb", parent=CELL, fontName="DejaVu-Bold")
CELL_RIGHT = ParagraphStyle("cellr", parent=CELL, alignment=TA_RIGHT)
HEAD_CELL = ParagraphStyle("headcell", parent=CELL, fontName="DejaVu-Bold", textColor=GREEN_DARK)
H1 = ParagraphStyle(
    "h1", fontName="DejaVu-Bold", fontSize=16, leading=20, textColor=GREEN_DARK, spaceAfter=2
)
H2 = ParagraphStyle(
    "h2", fontName="DejaVu-Bold", fontSize=11.5, leading=15, textColor=GREEN,
    spaceBefore=10, spaceAfter=5,
)  # fmt: skip
H3 = ParagraphStyle(
    "h3", fontName="DejaVu-Bold", fontSize=9.5, leading=13, textColor=BROWN,
    spaceBefore=6, spaceAfter=3,
)  # fmt: skip
NOTE = ParagraphStyle(
    "note", parent=BODY, fontSize=8.5, leading=12, leftIndent=6, borderPadding=(4, 6, 4, 6),
    backColor=BEIGE_LIGHT, borderColor=BORDER, borderWidth=0.5, spaceBefore=2, spaceAfter=4,
)  # fmt: skip


def p(text: object, style: ParagraphStyle = CELL) -> Paragraph:
    """Paragraph from untrusted text: escaped, newlines kept."""
    return Paragraph(escape(str(text if text is not None else "")).replace("\n", "<br/>"), style)


def markup(text: str, style: ParagraphStyle = BODY) -> Paragraph:
    """Paragraph from our own markup (bold etc.). Never pass user text here."""
    return Paragraph(text, style)


# ---------------------------------------------------------------- tables


def table(
    header: Sequence[str],
    rows: Sequence[Sequence[Any]],
    widths: Sequence[float],
    *,
    right: Sequence[int] = (),
    total: Sequence[Any] | None = None,
    struck: Sequence[int] = (),
) -> Table:
    """A themed table. Cells may be strings (escaped) or flowables."""

    def cell(value: Any, column: int, bold: bool = False) -> Any:
        if isinstance(value, Flowable):
            return value
        style = CELL_RIGHT if column in right else (CELL_BOLD if bold else CELL)
        if bold and column in right:
            style = ParagraphStyle("cellrb", parent=CELL_RIGHT, fontName="DejaVu-Bold")
        return p(value, style)

    data: list[list[Any]] = [[p(h, HEAD_CELL) for h in header]]
    data += [[cell(v, i) for i, v in enumerate(row)] for row in rows]
    if total is not None:
        data.append([cell(v, i, bold=True) for i, v in enumerate(total)])
    t = Table(data, colWidths=list(widths), repeatRows=1)
    commands: list[tuple[Any, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), BEIGE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, GREEN),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, BORDER),
    ]
    for index in range(1, len(rows) + 1):
        if index % 2 == 0:
            commands.append(("BACKGROUND", (0, index), (-1, index), BEIGE_LIGHT))
    for index in struck:
        commands.append(("TEXTCOLOR", (0, index + 1), (-1, index + 1), MUTED))
    if total is not None:
        commands += [
            ("LINEABOVE", (0, -1), (-1, -1), 0.8, GREEN),
            ("BACKGROUND", (0, -1), (-1, -1), GREEN_LIGHT),
        ]
    t.setStyle(TableStyle(commands))
    return t


def facts(pairs: Sequence[tuple[str, Any]], width: float, columns: int = 2) -> Table:
    """Label / value pairs laid out in a compact grid (summary boxes)."""
    cells: list[Any] = []
    for label, value in pairs:
        cells.append([p(label, SMALL), p(value, CELL_BOLD)])
    rows: list[list[Any]] = []
    for start in range(0, len(cells), columns):
        chunk = cells[start : start + columns]
        chunk += [["", ""]] * (columns - len(chunk))
        rows.append([item for pair in chunk for item in pair])
    label_w = width / columns * 0.45
    value_w = width / columns - label_w
    t = Table(rows, colWidths=[label_w, value_w] * columns)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), BEIGE_LIGHT),
                ("BOX", (0, 0), (-1, -1), 0.5, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return t


class Bars(Flowable):
    """Horizontal bar chart: label, bar, value. Drawn in vector, no images."""

    def __init__(
        self, items: Sequence[tuple[str, float, str]], width: float, *, bar_height: float = 9
    ) -> None:
        super().__init__()
        self.items = list(items)
        self.width = width
        self.bar_height = bar_height
        self.row = bar_height + 5
        self.height = self.row * max(len(self.items), 1)

    def wrap(self, avail_width: float, avail_height: float) -> tuple[float, float]:
        return self.width, self.height

    def draw(self) -> None:
        c = self.canv
        label_w = self.width * 0.32
        value_w = self.width * 0.18
        bar_w = self.width - label_w - value_w - 8
        largest = max((v for _, v, _ in self.items), default=0) or 1
        for index, (label, value, shown) in enumerate(self.items):
            y = self.height - (index + 1) * self.row + 2
            c.setFont("DejaVu", 8)
            c.setFillColor(TEXT)
            c.drawString(0, y + 1.5, _fit(label, "DejaVu", 8, label_w - 4))
            c.setFillColor(BEIGE)
            c.rect(label_w, y, bar_w, self.bar_height, stroke=0, fill=1)
            c.setFillColor(GREEN)
            c.rect(label_w, y, bar_w * (value / largest), self.bar_height, stroke=0, fill=1)
            c.setFillColor(TEXT)
            c.drawRightString(self.width, y + 1.5, shown)


def _fit(text: str, font: str, size: float, width: float) -> str:
    if pdfmetrics.stringWidth(text, font, size) <= width:
        return text
    while text and pdfmetrics.stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text + "…"


# ---------------------------------------------------------------- document


@dataclass(frozen=True)
class Meta:
    title: str
    subtitle: str
    author: str  # who generated it
    generated: datetime
    landscape: bool = False


class _NumberedCanvas(Canvas):
    """Two-pass canvas so the footer can say "Sayfa x / y"."""

    def __init__(self, *args: Any, meta: Meta, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pages: list[dict[str, Any]] = []
        self._meta = meta

    def showPage(self) -> None:
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self._decorate(total)
            super().showPage()
        super().save()

    def _decorate(self, total: int) -> None:
        width, height = self._pagesize
        meta = self._meta
        margin = 16 * mm
        # header band
        self.setFillColor(GREEN)
        self.rect(0, height - 6, width, 6, stroke=0, fill=1)
        logo = _logo()
        if logo.exists():
            self.drawImage(str(logo), margin, height - 21 * mm, 11 * mm, 11 * mm, mask="auto")
        self.setFillColor(GREEN_DARK)
        self.setFont("DejaVu-Bold", 11)
        self.drawString(margin + 13 * mm, height - 14 * mm, "Nil Psikoloji")
        self.setFont("DejaVu", 8)
        self.setFillColor(MUTED)
        self.drawString(margin + 13 * mm, height - 18.5 * mm, meta.title)
        self.drawRightString(width - margin, height - 14 * mm, meta.subtitle)
        self.setStrokeColor(BORDER)
        self.setLineWidth(0.6)
        self.line(margin, height - 23 * mm, width - margin, height - 23 * mm)
        # footer
        self.line(margin, 14 * mm, width - margin, 14 * mm)
        self.setFont("DejaVu", 7)
        self.drawString(
            margin,
            10 * mm,
            f"Gizli — kişisel sağlık verisi içerir. Oluşturan: {meta.author}, "
            f"{format_datetime_tr(meta.generated)}",
        )
        self.drawRightString(width - margin, 10 * mm, f"Sayfa {self._pageNumber} / {total}")


def build(meta: Meta, story: list[Flowable]) -> bytes:
    register_fonts()
    buffer = io.BytesIO()
    size = landscape(A4) if meta.landscape else A4
    margin = 16 * mm
    doc = BaseDocTemplate(
        buffer,
        pagesize=size,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=27 * mm,
        bottomMargin=19 * mm,
        title=meta.title,
        author="Nil Psikoloji",
        subject=meta.subtitle,
        creator="Nil Psikoloji Klinik",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates([PageTemplate(id="page", frames=[frame])])
    doc.build(story, canvasmaker=lambda *a, **k: _NumberedCanvas(*a, meta=meta, **k))
    return buffer.getvalue()


def content_width(meta: Meta) -> float:
    size = landscape(A4) if meta.landscape else A4
    return size[0] - 32 * mm


def heading(title: str, lines: Sequence[str]) -> list[Flowable]:
    """The report's own title block on page 1."""
    out: list[Flowable] = [p(title, H1)]
    out += [p(line, SMALL) for line in lines]
    out.append(Spacer(1, 6))
    return out


def section(title: str) -> list[Flowable]:
    return [CondPageBreak(30 * mm), p(title, H2)]


def keep(*items: Flowable) -> KeepTogether:
    return KeepTogether(list(items))


def empty(text: str = "Bu dönemde kayıt yok.") -> Paragraph:
    return p(text, ParagraphStyle("empty", parent=BODY, textColor=MUTED, alignment=TA_LEFT))
