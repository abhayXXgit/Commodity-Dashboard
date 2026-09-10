"""PDF + workbook report generation (spec §13 export, §19 monthly)."""
from __future__ import annotations

import datetime as dt
import io
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.database import session_scope
from backend.utils.logging_config import get_logger
from backend.utils.units import inr_compact

log = get_logger(__name__)

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)
    HAS_RL = True
except Exception:                                          # pragma: no cover
    HAS_RL = False

NAVY = "#0B1B2B"
AMBER = "#F0A202"

RUPEE = "\u20b9"

# Core PDF fonts (Helvetica et al.) have no rupee glyph, so every amount renders
# as a hollow box. Try to register a Unicode TTF that actually contains U+20B9;
# if none is installed - a slim Docker image typically has none - fall back to
# the unambiguous "Rs." prefix rather than shipping a report full of tofu.
_FONT_CANDIDATES = [
    ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ("DejaVuSans", "/usr/share/fonts/TTF/DejaVuSans.ttf"),
    ("DejaVuSans", "/Library/Fonts/DejaVuSans.ttf"),
    ("NotoSans", "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
    ("ArialUnicode", "/Library/Fonts/Arial Unicode.ttf"),
    ("ArialUnicode", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
]
_UNICODE_FONT: str | None = None


def _register_unicode_font() -> str | None:
    """Return the name of a registered font that can draw the rupee sign."""
    global _UNICODE_FONT
    if _UNICODE_FONT is not None:
        return _UNICODE_FONT or None
    if not HAS_RL:
        _UNICODE_FONT = ""
        return None
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for name, path in _FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        try:
            font = TTFont(name, path)
            # verify the glyph is really present before committing to it
            if ord(RUPEE) not in font.face.charToGlyph:
                continue
            pdfmetrics.registerFont(font)
            _UNICODE_FONT = name
            log.info("PDF: using %s for the rupee sign", name)
            return name
        except Exception:                                   # noqa: BLE001
            continue
    log.info("PDF: no Unicode font with U+20B9 found - amounts will read 'Rs.'")
    _UNICODE_FONT = ""
    return None


def money(value: float) -> str:
    """Compact rupee amount that is guaranteed to render in the PDF."""
    text = inr_compact(value)
    if _register_unicode_font():
        return text
    return text.replace(RUPEE, "Rs.")


def _styles():
    ss = getSampleStyleSheet()
    uni = _register_unicode_font()
    body_font = uni or "Helvetica"
    ss.add(ParagraphStyle("H", parent=ss["Heading1"], fontSize=16,
                          textColor=colors.HexColor(NAVY)))
    ss.add(ParagraphStyle("H2x", parent=ss["Heading2"], fontSize=12,
                          textColor=colors.HexColor(NAVY), spaceBefore=10))
    ss.add(ParagraphStyle("Body", parent=ss["BodyText"], fontSize=9, leading=13,
                          fontName=body_font))
    ss.add(ParagraphStyle("Small", parent=ss["BodyText"], fontSize=7.5,
                          textColor=colors.grey, leading=10, fontName=body_font))
    return ss


def _table(data, widths=None, align_right_from=1):
    t = Table(data, colWidths=widths, hAlign="LEFT")
    uni = _register_unicode_font()
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(NAVY)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("ALIGN", (align_right_from, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C8CDD4")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F4F7")]),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if uni:
        style.append(("FONTNAME", (0, 1), (-1, -1), uni))
    t.setStyle(TableStyle(style))
    return t


def generate_pdf(db) -> bytes:
    """Management commodity + exposure report."""
    if not HAS_RL:
        raise RuntimeError("reportlab is not installed; add it to requirements.txt")

    from backend.services import bom_engine as B
    from backend.services import procurement as P
    from backend.services import summary as S

    ss = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Commodity & BOM Exposure Report",
                            author=settings.APP_NAME,
                            leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm)
    flow: list = []
    now = dt.datetime.now()

    flow.append(Paragraph("Commodity Price & Transformer BOM Exposure Report", ss["H"]))
    flow.append(Paragraph(
        f"{settings.APP_NAME} {settings.APP_VERSION} &nbsp;|&nbsp; generated "
        f"{now:%d %b %Y %H:%M} &nbsp;|&nbsp; data mode <b>{settings.DATA_MODE}</b>",
        ss["Small"]))
    flow.append(Spacer(1, 6))

    summ = S.management_summary(db)
    flow.append(Paragraph("Management summary", ss["H2x"]))
    _uni = _register_unicode_font()
    for sline in summ["sentences"]:
        # the narrative is built with rupee signs; swap them when the font cannot draw them
        text = sline if _uni else sline.replace(RUPEE, "Rs.")
        flow.append(Paragraph("&bull; " + text, ss["Body"]))
    flow.append(Spacer(1, 6))

    exec_v = S.executive_view(db)
    flow.append(Paragraph("Market and exposure", ss["H2x"]))
    rows = [["Commodity", "Price INR/MT", "1D %", "30D %", "Exposure", "Risk", "Signal"]]
    for c, m in exec_v["market"].items():
        rows.append([m["name"], f"{m['price']:,.0f}",
                     f"{m['d1']:+.2f}" if m["d1"] is not None else "-",
                     f"{m['d30']:+.2f}" if m["d30"] is not None else "-",
                     money(exec_v["exposure"].get(c, 0)),
                     exec_v["risk"].get(c, {}).get("risk_band", "-"),
                     exec_v["action"].get(c, {}).get("signal", "-")])
    flow.append(_table(rows, [32 * mm, 24 * mm, 16 * mm, 16 * mm, 24 * mm, 18 * mm, 32 * mm]))
    flow.append(Spacer(1, 4))
    flow.append(Paragraph(
        f"Total commodity exposure {money(exec_v['total_exposure'])} &nbsp;|&nbsp; "
        f"30-day forecast {money(exec_v['forecast_exposure']['30d'])} &nbsp;|&nbsp; "
        f"potential {money(exec_v['potential_exposure_30d'])} &nbsp;|&nbsp; "
        f"overall risk <b>{exec_v['overall_risk']}</b>", ss["Body"]))

    flow.append(Paragraph("Material-wise cost impact", ss["H2x"]))
    imp = B.price_impact(db)
    rows = [["Commodity", "MT/transformer", "Price INR/MT", "Change",
             "Impact/transformer", "Total exposure"]]
    for r in imp["rows"]:
        rows.append([r["commodity"], f"{r['bom_mt_per_transformer']:.3f}",
                     f"{r['current_price']:,.0f}", f"{r['price_change']:+,.0f}",
                     money(r["impact_per_transformer"]),
                     money(r["total_exposure"])])
    rows.append(["TOTAL", "", "", "", money(imp["total_impact_per_transformer"]),
                 money(imp["total_project_exposure"])])
    flow.append(_table(rows, [24 * mm, 28 * mm, 26 * mm, 24 * mm, 32 * mm, 30 * mm]))

    flow.append(Paragraph("Project exposure", ss["H2x"]))
    roll = B.bom_rollup(db)
    rows = [["Job", "Customer", "MVA", "Qty", "Cu MT", "Al MT", "Material cost", "Delivery"]]
    for t in roll["transformers"]:
        rows.append([t["job_no"], (t["customer"] or "")[:22], f"{t['rating_mva']:.0f}",
                     str(t["quantity"]), f"{t['copper_mt_project']:.2f}",
                     f"{t['aluminium_mt_project']:.2f}",
                     money(t["project_material_cost"]), t["delivery_date"] or "-"])
    flow.append(_table(rows, [18 * mm, 34 * mm, 14 * mm, 12 * mm, 18 * mm, 18 * mm,
                              26 * mm, 22 * mm]))

    flow.append(Paragraph("Procurement coverage", ss["H2x"]))
    rows = [["Commodity", "Requirement MT", "Covered MT", "Uncovered MT",
             "Coverage %", "Uncovered exposure"]]
    for c in B.coverage(db):
        rows.append([c["commodity"], f"{c['requirement_mt']:.2f}", f"{c['covered_mt']:.2f}",
                     f"{c['uncovered_mt']:.2f}",
                     f"{c['coverage_pct']:.1f}" if c["coverage_pct"] is not None else "-",
                     money(c["uncovered_exposure"])])
    flow.append(_table(rows, [24 * mm, 30 * mm, 26 * mm, 28 * mm, 24 * mm, 34 * mm]))

    flow.append(Spacer(1, 8))
    flow.append(Paragraph(P.DISCLAIMER, ss["Small"]))
    if settings.is_demo:
        flow.append(Paragraph(
            "<b>DEMO DATA.</b> The price history behind this report is a seeded "
            "simulation, not market data. Do not use these figures commercially "
            "until a licensed feed or official circulars are connected.", ss["Small"]))

    doc.build(flow)
    return buf.getvalue()


def generate_monthly_report() -> dict[str, Any]:
    """Monthly archive: PDF + workbook into reports/."""
    from backend.services.excel_io import export_workbook

    stamp = dt.date.today().strftime("%Y-%m")
    outdir = Path(settings.REPORT_DIR)
    outdir.mkdir(parents=True, exist_ok=True)
    written = []
    with session_scope() as db:
        try:
            pdf = generate_pdf(db)
            p = outdir / f"commodity_exposure_{stamp}.pdf"
            p.write_bytes(pdf)
            written.append(str(p))
        except Exception as exc:                            # noqa: BLE001
            log.warning("monthly PDF failed: %s", exc)
        xls = export_workbook(db)
        x = outdir / f"commodity_data_{stamp}.xlsx"
        x.write_bytes(xls)
        written.append(str(x))
    log.info("monthly report written: %s", written)
    return {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "files": written}
