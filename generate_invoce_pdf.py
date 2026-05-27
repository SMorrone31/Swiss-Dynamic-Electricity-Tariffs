"""
generate_invoice_pdf.py
=======================
Genera due PDF distinti stile Anthropic/Stripe per l'upgrade Pro di Swiss Tariff Hub:
  - Receipt (ricevuta di pagamento) — sempre generata
  - Invoice (fattura fiscale)       — solo se l'utente ha un VAT number

Entry point: generate_payment_pdfs() -> (receipt_bytes, invoice_bytes)
invoice_bytes e' None se vat_number e' assente.

Alias legacy: generate_invoice_pdf() rimane per compatibilita' con codice esistente.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)

# ── Palette ───────────────────────────────────────────────────────────────────

BLUE_DARK  = colors.HexColor("#0C447C")
BLUE_MID   = colors.HexColor("#185fa5")
BLUE_LIGHT = colors.HexColor("#E6F1FB")
GREEN      = colors.HexColor("#1D9E75")
GRAY_LINE  = colors.HexColor("#D3D1C7")
GRAY_TEXT  = colors.HexColor("#5F5E5A")
GRAY_BG    = colors.HexColor("#F8F9FA")
WHITE      = colors.white
BLACK      = colors.HexColor("#1a1a1a")

ISSUER_NAME  = "Swiss Tariff Hub GmbH"
ISSUER_ADDR1 = "c/o Swiss Tariff Hub"
ISSUER_ADDR2 = "Switzerland"
ISSUER_EMAIL = "support@tariffhub.ch"
PRO_PRICE_CHF = 19.00

# ── Traduzioni ────────────────────────────────────────────────────────────────

_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "receipt_title":   "Receipt",
        "invoice_title":   "Invoice",
        "invoice_number":  "Invoice number",
        "receipt_number":  "Receipt number",
        "date_paid":       "Date paid",
        "bill_to":         "Bill to",
        "from_lbl":        "From",
        "description":     "Description",
        "qty":             "Qty",
        "unit_price":      "Unit price",
        "tax":             "Tax",
        "amount":          "Amount",
        "subtotal":        "Subtotal",
        "total":           "Total",
        "amount_paid":     "Amount paid",
        "payment_history": "Payment history",
        "payment_method":  "Payment method",
        "date_col":        "Date",
        "amount_col":      "Amount paid",
        "receipt_no_col":  "Receipt number",
        "reverse_charge":  "Reverse charge applies - Customer to account for tax under Article 196 of Directive 2006/112/EC.",
        "rc_note":         "[1] Tax to be paid on reverse charge basis",
        "rc_customer":     "Customer may be obliged to account for VAT on reverse charge basis.",
        "service_name":    "Swiss Tariff Hub Pro",
        "billing_period":  "Monthly subscription",
        "paid_on":         "paid on",
        "footer":          "Swiss Tariff Hub  \u00b7  tariffhub.ch  \u00b7  This document was generated automatically.",
        "plan_label":      "PRO PLAN",
    },
    "de": {
        "receipt_title":   "Quittung",
        "invoice_title":   "Rechnung",
        "invoice_number":  "Rechnungsnummer",
        "receipt_number":  "Quittungsnummer",
        "date_paid":       "Bezahlt am",
        "bill_to":         "Rechnungsempf\u00e4nger",
        "from_lbl":        "Von",
        "description":     "Beschreibung",
        "qty":             "Menge",
        "unit_price":      "Einzelpreis",
        "tax":             "MwSt",
        "amount":          "Betrag",
        "subtotal":        "Zwischensumme",
        "total":           "Gesamt",
        "amount_paid":     "Bezahlter Betrag",
        "payment_history": "Zahlungsverlauf",
        "payment_method":  "Zahlungsmethode",
        "date_col":        "Datum",
        "amount_col":      "Bezahlter Betrag",
        "receipt_no_col":  "Quittungsnummer",
        "reverse_charge":  "Umkehrung der Steuerschuldnerschaft gem\u00e4\u00df Artikel 196 der Richtlinie 2006/112/EG.",
        "rc_note":         "[1] Steuer auf Basis der Umkehrung der Steuerschuldnerschaft",
        "rc_customer":     "Kunde kann verpflichtet sein, MwSt in Umkehrung abzuf\u00fchren.",
        "service_name":    "Swiss Tariff Hub Pro",
        "billing_period":  "Monatliches Abonnement",
        "paid_on":         "bezahlt am",
        "footer":          "Swiss Tariff Hub  \u00b7  tariffhub.ch  \u00b7  Automatisch erstellt.",
        "plan_label":      "PRO-PLAN",
    },
    "fr": {
        "receipt_title":   "Re\u00e7u",
        "invoice_title":   "Facture",
        "invoice_number":  "Num\u00e9ro de facture",
        "receipt_number":  "Num\u00e9ro de re\u00e7u",
        "date_paid":       "Date de paiement",
        "bill_to":         "Facturer \u00e0",
        "from_lbl":        "De",
        "description":     "Description",
        "qty":             "Qt\u00e9",
        "unit_price":      "Prix unitaire",
        "tax":             "TVA",
        "amount":          "Montant",
        "subtotal":        "Sous-total",
        "total":           "Total",
        "amount_paid":     "Montant pay\u00e9",
        "payment_history": "Historique des paiements",
        "payment_method":  "Moyen de paiement",
        "date_col":        "Date",
        "amount_col":      "Montant pay\u00e9",
        "receipt_no_col":  "Num\u00e9ro de re\u00e7u",
        "reverse_charge":  "Autoliquidation - Article 196 de la directive 2006/112/CE.",
        "rc_note":         "[1] Taxe \u00e0 payer en autoliquidation",
        "rc_customer":     "Le client peut \u00eatre tenu de d\u00e9clarer la TVA en autoliquidation.",
        "service_name":    "Swiss Tariff Hub Pro",
        "billing_period":  "Abonnement mensuel",
        "paid_on":         "pay\u00e9 le",
        "footer":          "Swiss Tariff Hub  \u00b7  tariffhub.ch  \u00b7  Document g\u00e9n\u00e9r\u00e9 automatiquement.",
        "plan_label":      "PLAN PRO",
    },
    "it": {
        "receipt_title":   "Ricevuta",
        "invoice_title":   "Fattura",
        "invoice_number":  "Numero fattura",
        "receipt_number":  "Numero ricevuta",
        "date_paid":       "Data pagamento",
        "bill_to":         "Intestata a",
        "from_lbl":        "Da",
        "description":     "Descrizione",
        "qty":             "Qt\u00e0",
        "unit_price":      "Prezzo unitario",
        "tax":             "IVA",
        "amount":          "Importo",
        "subtotal":        "Subtotale",
        "total":           "Totale",
        "amount_paid":     "Importo pagato",
        "payment_history": "Storico pagamenti",
        "payment_method":  "Metodo di pagamento",
        "date_col":        "Data",
        "amount_col":      "Importo pagato",
        "receipt_no_col":  "Numero ricevuta",
        "reverse_charge":  "Reverse charge - Art. 196 Direttiva 2006/112/CE.",
        "rc_note":         "[1] Imposta da versare in regime di reverse charge",
        "rc_customer":     "Il cliente potrebbe essere tenuto a versare l\u2019IVA in regime di reverse charge.",
        "service_name":    "Swiss Tariff Hub Pro",
        "billing_period":  "Abbonamento mensile",
        "paid_on":         "pagato il",
        "footer":          "Swiss Tariff Hub  \u00b7  tariffhub.ch  \u00b7  Documento generato automaticamente.",
        "plan_label":      "PIANO PRO",
    },
}


def _t(lang: str, key: str) -> str:
    return _STRINGS.get(lang, _STRINGS["en"]).get(key, _STRINGS["en"].get(key, key))


def _fmt_date(dt: datetime, lang: str) -> str:
    months_en = ["January","February","March","April","May","June",
                 "July","August","September","October","November","December"]
    months_de = ["Januar","Februar","M\u00e4rz","April","Mai","Juni",
                 "Juli","August","September","Oktober","November","Dezember"]
    months_fr = ["janvier","f\u00e9vrier","mars","avril","mai","juin",
                 "juillet","ao\u00fbt","septembre","octobre","novembre","d\u00e9cembre"]
    months_it = ["gennaio","febbraio","marzo","aprile","maggio","giugno",
                 "luglio","agosto","settembre","ottobre","novembre","dicembre"]
    m, d, y = dt.month - 1, dt.day, dt.year
    if lang == "de":
        return f"{d}. {months_de[m]} {y}"
    if lang == "fr":
        return f"{d} {months_fr[m]} {y}"
    if lang == "it":
        return f"{d} {months_it[m]} {y}"
    return f"{months_en[m]} {d}, {y}"


def _generate_ids() -> tuple[str, str]:
    b1 = str(uuid.uuid4()).upper().replace("-", "")
    b2 = str(uuid.uuid4()).upper().replace("-", "")
    return f"{b1[:8]}-{b1[8:16]}", f"{b2[:4]}-{b2[4:8]}"


# ── Logo ──────────────────────────────────────────────────────────────────────

def _draw_logo(canvas, x, y, size=14):
    import math
    canvas.saveState()
    cx, cy = x + size / 2, y + size / 2
    r = size / 2
    pts = [(cx + r * math.cos(math.radians(60 * i - 30)),
            cy + r * math.sin(math.radians(60 * i - 30))) for i in range(6)]
    canvas.setFillColor(BLUE_DARK)
    p = canvas.beginPath()
    p.moveTo(*pts[0])
    for pt in pts[1:]:
        p.lineTo(*pt)
    p.close()
    canvas.drawPath(p, fill=1, stroke=0)
    canvas.setFillColor(WHITE)
    canvas.setFont("Helvetica-Bold", size * 0.45)
    canvas.drawCentredString(cx, cy - size * 0.15, "S")
    canvas.restoreState()


def _make_page_callbacks(lang: str):
    def _on_page(canvas, doc):
        from reportlab.lib.pagesizes import A4 as _A4
        W, H = _A4
        canvas.saveState()
        canvas.setFillColor(BLUE_DARK)
        canvas.rect(0, H - 22 * mm, W, 22 * mm, fill=1, stroke=0)
        _draw_logo(canvas, 14 * mm, H - 18 * mm, size=14 * mm)
        canvas.setFillColor(WHITE)
        canvas.setFont("Helvetica-Bold", 11)
        canvas.drawString(32 * mm, H - 9 * mm, "Swiss Tariff Hub")
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#B5D4F4"))
        canvas.drawString(32 * mm, H - 16 * mm, "Dynamic Electricity Tariff API")
        canvas.setFillColor(GRAY_LINE)
        canvas.rect(14 * mm, 10 * mm, W - 28 * mm, 0.3, fill=1, stroke=0)
        canvas.setFillColor(GRAY_TEXT)
        canvas.setFont("Helvetica", 7)
        canvas.drawCentredString(W / 2, 6 * mm, _t(lang, "footer"))
        canvas.restoreState()
    return _on_page


def _make_doc(buf: io.BytesIO, lang: str):
    W, H = A4
    mx, mt, mb = 18 * mm, 28 * mm, 20 * mm
    doc = BaseDocTemplate(buf, pagesize=A4,
        leftMargin=mx, rightMargin=mx, topMargin=mt, bottomMargin=mb)
    frame = Frame(mx, mb, W - 2 * mx, H - mt - mb, id="main")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame],
                                       onPage=_make_page_callbacks(lang))])
    return doc, W - 2 * mx


def _st():
    s_h1   = ParagraphStyle("h1",   fontName="Helvetica-Bold", fontSize=22, textColor=BLUE_DARK, spaceAfter=2*mm, leading=26)
    s_h2   = ParagraphStyle("h2",   fontName="Helvetica-Bold", fontSize=11, textColor=BLUE_DARK, spaceBefore=5*mm, spaceAfter=2*mm)
    s_lbl  = ParagraphStyle("lbl",  fontName="Helvetica-Bold", fontSize=8,  textColor=GRAY_TEXT)
    s_val  = ParagraphStyle("val",  fontName="Helvetica",      fontSize=9,  textColor=BLACK)
    s_sm   = ParagraphStyle("sm",   fontName="Helvetica",      fontSize=8,  textColor=GRAY_TEXT, leading=11)
    s_bold = ParagraphStyle("bold", fontName="Helvetica-Bold", fontSize=9,  textColor=BLACK)
    s_th   = ParagraphStyle("th",   fontName="Helvetica-Bold", fontSize=8,  textColor=WHITE)
    s_td   = ParagraphStyle("td",   fontName="Helvetica",      fontSize=9,  textColor=BLACK)
    s_tdb  = ParagraphStyle("tdb",  fontName="Helvetica-Bold", fontSize=9,  textColor=BLACK)
    return s_h1, s_h2, s_lbl, s_val, s_sm, s_bold, s_th, s_td, s_tdb


# ── Helper tabelle ────────────────────────────────────────────────────────────

def _info_block(title: str, lines: list[str], s_lbl, s_val) -> Table:
    rows = [[Paragraph(title, s_lbl)]]
    rows += [[Paragraph(line, s_val)] for line in lines]
    tbl = Table(rows, colWidths=[None])
    tbl.setStyle(TableStyle([
        ("TOPPADDING",    (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
    ]))
    return tbl


def _two_col(left, right, col_w: float) -> Table:
    tbl = Table([[left, right]], colWidths=[col_w / 2, col_w / 2])
    tbl.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return tbl


def _meta_tbl(rows_data: list, col_w: float) -> Table:
    tbl = Table(rows_data, colWidths=[55 * mm, col_w - 55 * mm])
    tbl.setStyle(TableStyle([
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
    ]))
    return tbl


def _items_tbl(head_row, item_row, col_w: float) -> Table:
    cw = [col_w - 90*mm, 14*mm, 26*mm, 18*mm, 32*mm]
    tbl = Table([head_row, item_row], colWidths=cw)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  BLUE_DARK),
        ("BOX",           (0, 0), (-1, -1), 0.4, GRAY_LINE),
        ("INNERGRID",     (0, 0), (-1, -1), 0.3, GRAY_LINE),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 7),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 7),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",         (1, 0), (-1, -1), "CENTER"),
    ]))
    return tbl


def _totals_tbl(amount_str: str, col_w: float, lang: str, s_lbl, s_val, s_bold) -> Table:
    cw = [col_w - 55*mm, 55*mm]
    rows = [
        [Paragraph(_t(lang, "subtotal"),    s_lbl),  Paragraph(amount_str, s_val)],
        [Paragraph(_t(lang, "total"),       s_lbl),  Paragraph(amount_str, s_val)],
        [Paragraph(_t(lang, "amount_paid"), s_bold), Paragraph(amount_str, s_bold)],
    ]
    tbl = Table(rows, colWidths=cw)
    tbl.setStyle(TableStyle([
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("ALIGN",         (1, 0), (-1, -1), "RIGHT"),
        ("LINEABOVE",     (0, 2), (-1, 2),  0.8, BLUE_DARK),
        ("BACKGROUND",    (0, 2), (-1, 2),  BLUE_LIGHT),
    ]))
    return tbl


def _payment_history_tbl(payment_method: str, paid_date: str, amount_str: str,
                          receipt_no: str, col_w: float, lang: str, s_th, s_td) -> Table:
    ph_head = [
        Paragraph(_t(lang, "payment_method"), s_th),
        Paragraph(_t(lang, "date_col"),       s_th),
        Paragraph(_t(lang, "amount_col"),     s_th),
        Paragraph(_t(lang, "receipt_no_col"), s_th),
    ]
    ph_row = [
        Paragraph(payment_method, s_td),
        Paragraph(paid_date,      s_td),
        Paragraph(amount_str,     s_td),
        Paragraph(receipt_no,     s_td),
    ]
    cw = [col_w - 105*mm, 35*mm, 35*mm, 35*mm]
    tbl = Table([ph_head, ph_row], colWidths=cw)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  BLUE_DARK),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [GRAY_BG]),
        ("BOX",           (0, 0), (-1, -1), 0.4, GRAY_LINE),
        ("INNERGRID",     (0, 0), (-1, -1), 0.3, GRAY_LINE),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 7),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 7),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return tbl


# ── Corpo comune (ricevuta e fattura hanno stessa struttura) ──────────────────

def _build_doc(
    doc_title:      str,
    full_name:      str,
    email:          str,
    invoice_no:     str,
    receipt_no:     str,
    pro_since:      datetime,
    amount_chf:     float,
    payment_method: str,
    company:        Optional[str],
    vat_number:     Optional[str],
    address_line1:  Optional[str],
    address_line2:  Optional[str],
    country:        str,
    lang:           str,
) -> bytes:
    buf = io.BytesIO()
    doc, col_w = _make_doc(buf, lang)
    s_h1, s_h2, s_lbl, s_val, s_sm, s_bold, s_th, s_td, s_tdb = _st()

    paid_date  = _fmt_date(pro_since, lang)
    amount_str = f"CHF {amount_chf:,.2f}"
    is_eu_vat  = bool(vat_number and not vat_number.upper().startswith("CHE"))
    # Period: +1 month (handle end-of-month edge cases)
    next_month = pro_since.month % 12 + 1
    next_year  = pro_since.year + (1 if pro_since.month == 12 else 0)
    import calendar
    last_day   = calendar.monthrange(next_year, next_month)[1]
    period_end = pro_since.replace(year=next_year, month=next_month,
                                   day=min(pro_since.day, last_day))
    period_str = f"{_fmt_date(pro_since, lang)} \u2013 {_fmt_date(period_end, lang)}"
    tax_str    = "0% [1]" if is_eu_vat else "0%"

    story = []

    # Titolo
    story.append(Paragraph(_t(lang, doc_title), s_h1))
    story.append(Spacer(1, 1 * mm))

    # Meta
    story.append(_meta_tbl([
        [Paragraph(_t(lang, "invoice_number"), s_lbl), Paragraph(invoice_no, s_val)],
        [Paragraph(_t(lang, "receipt_number"), s_lbl), Paragraph(receipt_no, s_val)],
        [Paragraph(_t(lang, "date_paid"),      s_lbl), Paragraph(paid_date,  s_val)],
    ], col_w))
    story.append(Spacer(1, 3 * mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=GRAY_LINE, spaceAfter=4*mm))

    # From / Bill To
    from_lines = [ISSUER_NAME, ISSUER_ADDR1, ISSUER_ADDR2, ISSUER_EMAIL]
    bill_lines: list[str] = [full_name]
    if company:       bill_lines.append(company)
    if address_line1: bill_lines.append(address_line1)
    if address_line2: bill_lines.append(address_line2)
    bill_lines.append(country)
    bill_lines.append(email)
    if vat_number:    bill_lines.append(f"VAT: {vat_number}")

    story.append(_two_col(
        _info_block(_t(lang, "from_lbl"), from_lines, s_lbl, s_val),
        _info_block(_t(lang, "bill_to"),  bill_lines, s_lbl, s_val),
        col_w,
    ))
    story.append(Spacer(1, 4 * mm))

    # Riga sommario pagamento
    story.append(Paragraph(
        f"<b>{amount_str} {_t(lang, 'paid_on')} {paid_date}</b>", s_bold))
    if is_eu_vat:
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(_t(lang, "reverse_charge"), s_sm))
    story.append(Spacer(1, 4 * mm))

    # Items table
    head_row = [
        Paragraph(_t(lang, "description"), s_th),
        Paragraph(_t(lang, "qty"),         s_th),
        Paragraph(_t(lang, "unit_price"),  s_th),
        Paragraph(_t(lang, "tax"),         s_th),
        Paragraph(_t(lang, "amount"),      s_th),
    ]
    item_row = [
        Paragraph(
            f"<b>{_t(lang, 'service_name')}</b><br/>"
            f"<font size=8 color='#888'>{_t(lang, 'billing_period')} · {period_str}</font>", s_td),
        Paragraph("1",         s_td),
        Paragraph(amount_str,  s_td),
        Paragraph(tax_str,     s_td),
        Paragraph(amount_str,  s_tdb),
    ]
    story.append(_items_tbl(head_row, item_row, col_w))
    story.append(Spacer(1, 2 * mm))
    story.append(_totals_tbl(amount_str, col_w, lang, s_lbl, s_val, s_bold))
    story.append(Spacer(1, 5 * mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=GRAY_LINE, spaceAfter=3*mm))

    # Payment history
    story.append(Paragraph(_t(lang, "payment_history"), s_h2))
    story.append(_payment_history_tbl(
        payment_method, paid_date, amount_str, receipt_no, col_w, lang, s_th, s_td))

    if is_eu_vat:
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph(_t(lang, "rc_customer"), s_sm))
        story.append(Paragraph(_t(lang, "rc_note"),     s_sm))

    doc.build(story)
    return buf.getvalue()


# ── Entry point pubblico ──────────────────────────────────────────────────────

def generate_payment_pdfs(
    full_name:      str,
    email:          str,
    pro_since:      datetime,
    amount_chf:     float           = PRO_PRICE_CHF,
    payment_method: str             = "Card",
    company:        Optional[str]   = None,
    vat_number:     Optional[str]   = None,
    address_line1:  Optional[str]   = None,
    address_line2:  Optional[str]   = None,
    country:        str             = "CH",
    lang:           str             = "en",
) -> Tuple[bytes, Optional[bytes]]:
    """
    Ritorna (receipt_bytes, invoice_bytes).
    invoice_bytes e' None se vat_number e' assente o vuoto.
    """
    lang = lang if lang in _STRINGS else "en"
    invoice_no, receipt_no = _generate_ids()

    common = dict(
        full_name=full_name, email=email,
        invoice_no=invoice_no, receipt_no=receipt_no,
        pro_since=pro_since, amount_chf=amount_chf,
        payment_method=payment_method,
        company=company, vat_number=vat_number,
        address_line1=address_line1, address_line2=address_line2,
        country=country, lang=lang,
    )

    receipt_bytes = _build_doc("receipt_title", **common)

    invoice_bytes = None
    if vat_number and vat_number.strip():
        invoice_bytes = _build_doc("invoice_title", **common)

    return receipt_bytes, invoice_bytes


# ── Alias backward-compat ─────────────────────────────────────────────────────

def generate_invoice_pdf(
    full_name:  str,
    email:      str,
    key_prefix: str,
    rate_limit: int,
    pro_since:  datetime,
    company:    Optional[str] = None,
    vat_number: Optional[str] = None,
    country:    str = "CH",
    lang:       str = "en",
) -> bytes:
    """Alias legacy — ritorna receipt_bytes per compatibilita' con codice esistente."""
    receipt, _ = generate_payment_pdfs(
        full_name=full_name, email=email,
        pro_since=pro_since, amount_chf=PRO_PRICE_CHF,
        payment_method="Card",
        company=company, vat_number=vat_number,
        country=country, lang=lang,
    )
    return receipt