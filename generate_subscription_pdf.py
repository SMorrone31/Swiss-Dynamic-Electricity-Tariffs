"""
generate_subscription_pdf.py
============================
Genera un PDF "Attestato di Abbonamento Gratuito" per Swiss Tariff Hub.
Viene allegato all'email di approvazione API key.

Uso:
    pdf_bytes = generate_subscription_pdf(
        full_name   = "Mario Rossi",
        email       = "mario@example.com",
        company     = "Acme SA",          # opzionale
        key_prefix  = "stk_a1b2c3d4",
        rate_limit  = 500,
        approved_at = datetime(2026, 4, 28, ...),
        lang        = "it",
    )
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate,
    Paragraph, Spacer, Table, TableStyle, HRFlowable,
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT


# ── Palette colori Swiss Tariff Hub ──────────────────────────────────────────

BLUE_DARK  = colors.HexColor("#0C447C")   # header / accent
BLUE_MID   = colors.HexColor("#185fa5")   # titoli
BLUE_LIGHT = colors.HexColor("#E6F1FB")   # sfondo celle tabella
GREEN      = colors.HexColor("#1D9E75")   # badge FREE
GREEN_DARK = colors.HexColor("#085041")
GRAY_LINE  = colors.HexColor("#D3D1C7")
GRAY_TEXT  = colors.HexColor("#5F5E5A")
WHITE      = colors.white
BLACK      = colors.HexColor("#1a1a1a")


# ── Traduzioni minime ─────────────────────────────────────────────────────────

_STRINGS = {
    "en": {
        "doc_title":    "Free Subscription Certificate",
        "subtitle":     "Swiss Tariff Hub — API Access",
        "plan_label":   "FREE PLAN",
        "plan_desc":    "This document certifies that the following account has been granted free access to the Swiss Tariff Hub API.",
        "holder":       "Account Holder",
        "email_lbl":    "Email",
        "company_lbl":  "Organization",
        "key_lbl":      "API Key (prefix)",
        "limit_lbl":    "Daily Request Limit",
        "limit_val":    "{limit} requests / day  (resets at midnight CET/CEST)",
        "since_lbl":    "Active Since",
        "terms_title":  "Terms of Use",
        "terms_body":   (
            "This free subscription grants access to Swiss dynamic electricity tariff data "
            "via the REST API. The service is provided as-is. Usage is subject to the daily "
            "limit shown above. Anthropic AG reserves the right to modify or discontinue the "
            "free tier at any time with reasonable notice. Upgrade options for higher limits "
            "will be communicated by email."
        ),
        "footer":       "Swiss Tariff Hub  ·  tariffhub.ch  ·  This document was generated automatically.",
        "unlimited":    "Unlimited",
    },
    "de": {
        "doc_title":    "Kostenloser Abonnementnachweis",
        "subtitle":     "Swiss Tariff Hub — API-Zugang",
        "plan_label":   "GRATIS-PLAN",
        "plan_desc":    "Dieses Dokument bestätigt, dass dem folgenden Konto kostenloser Zugang zur Swiss Tariff Hub API gewährt wurde.",
        "holder":       "Kontoinhaber",
        "email_lbl":    "E-Mail",
        "company_lbl":  "Organisation",
        "key_lbl":      "API-Schlüssel (Präfix)",
        "limit_lbl":    "Tageslimit",
        "limit_val":    "{limit} Anfragen / Tag  (Reset um Mitternacht MEZ/MESZ)",
        "since_lbl":    "Aktiv seit",
        "terms_title":  "Nutzungsbedingungen",
        "terms_body":   (
            "Dieses kostenlose Abonnement gewährt Zugang zu dynamischen Schweizer Stromtariffdaten "
            "über die REST-API. Der Dienst wird ohne Gewährleistung bereitgestellt. Die Nutzung "
            "unterliegt dem oben angegebenen Tageslimit. Swiss Tariff Hub behält sich das Recht "
            "vor, den kostenlosen Tarif jederzeit mit angemessener Frist zu ändern oder einzustellen."
        ),
        "footer":       "Swiss Tariff Hub  ·  tariffhub.ch  ·  Dieses Dokument wurde automatisch erstellt.",
        "unlimited":    "Unbegrenzt",
    },
    "fr": {
        "doc_title":    "Certificat d'abonnement gratuit",
        "subtitle":     "Swiss Tariff Hub — Accès API",
        "plan_label":   "PLAN GRATUIT",
        "plan_desc":    "Ce document certifie que le compte suivant bénéficie d'un accès gratuit à l'API Swiss Tariff Hub.",
        "holder":       "Titulaire du compte",
        "email_lbl":    "E-mail",
        "company_lbl":  "Organisation",
        "key_lbl":      "Clé API (préfixe)",
        "limit_lbl":    "Limite journalière",
        "limit_val":    "{limit} requêtes / jour  (réinitialisation à minuit CET/CEST)",
        "since_lbl":    "Actif depuis",
        "terms_title":  "Conditions d'utilisation",
        "terms_body":   (
            "Cet abonnement gratuit donne accès aux données dynamiques de tarifs électriques suisses "
            "via l'API REST. Le service est fourni tel quel. L'utilisation est soumise à la limite "
            "journalière indiquée. Swiss Tariff Hub se réserve le droit de modifier ou d'interrompre "
            "l'offre gratuite à tout moment avec un préavis raisonnable."
        ),
        "footer":       "Swiss Tariff Hub  ·  tariffhub.ch  ·  Document généré automatiquement.",
        "unlimited":    "Illimité",
    },
    "it": {
        "doc_title":    "Attestato di Abbonamento Gratuito",
        "subtitle":     "Swiss Tariff Hub — Accesso API",
        "plan_label":   "PIANO GRATUITO",
        "plan_desc":    "Il presente documento certifica che all'account indicato è stato concesso l'accesso gratuito all'API Swiss Tariff Hub.",
        "holder":       "Intestatario dell'account",
        "email_lbl":    "Email",
        "company_lbl":  "Organizzazione",
        "key_lbl":      "Chiave API (prefisso)",
        "limit_lbl":    "Limite giornaliero",
        "limit_val":    "{limit} richieste / giorno  (reset a mezzanotte CET/CEST)",
        "since_lbl":    "Attivo dal",
        "terms_title":  "Termini di utilizzo",
        "terms_body":   (
            "Questo abbonamento gratuito garantisce l'accesso ai dati dinamici delle tariffe "
            "elettriche svizzere tramite l'API REST. Il servizio viene fornito senza garanzie. "
            "L'utilizzo è soggetto al limite giornaliero indicato. Swiss Tariff Hub si riserva "
            "il diritto di modificare o interrompere il piano gratuito in qualsiasi momento con "
            "ragionevole preavviso."
        ),
        "footer":       "Swiss Tariff Hub  ·  tariffhub.ch  ·  Documento generato automaticamente.",
        "unlimited":    "Illimitato",
    },
}

def _t(lang: str, key: str) -> str:
    return _STRINGS.get(lang, _STRINGS["en"]).get(key, _STRINGS["en"].get(key, ""))


# ── Logo SVG-like disegnato con ReportLab canvas ──────────────────────────────

def _draw_logo(canvas, x, y, size=36):
    """Disegna il logo STH: esagono blu con 'S' bianca."""
    import math
    canvas.saveState()
    # Esagono
    cx, cy = x + size / 2, y + size / 2
    r = size / 2
    pts = [(cx + r * math.cos(math.radians(60 * i - 30)),
            cy + r * math.sin(math.radians(60 * i - 30))) for i in range(6)]
    canvas.setFillColor(BLUE_DARK)
    canvas.setStrokeColor(BLUE_DARK)
    p = canvas.beginPath()
    p.moveTo(*pts[0])
    for pt in pts[1:]:
        p.lineTo(*pt)
    p.close()
    canvas.drawPath(p, fill=1, stroke=0)
    # "S" bianca
    canvas.setFillColor(WHITE)
    canvas.setFont("Helvetica-Bold", size * 0.45)
    canvas.drawCentredString(cx, cy - size * 0.15, "S")
    canvas.restoreState()


# ── Callback header/footer per ogni pagina ────────────────────────────────────

def _make_page_callbacks(lang: str, doc_title: str):

    def _header_footer(canvas, doc):
        W, H = A4
        canvas.saveState()

        # ── Header band ──
        canvas.setFillColor(BLUE_DARK)
        canvas.rect(0, H - 28*mm, W, 28*mm, fill=1, stroke=0)

        # Logo
        _draw_logo(canvas, 14*mm, H - 24*mm, size=18*mm)

        # Nome prodotto
        canvas.setFillColor(WHITE)
        canvas.setFont("Helvetica-Bold", 13)
        canvas.drawString(36*mm, H - 12*mm, "Swiss Tariff Hub")
        canvas.setFont("Helvetica", 9)
        canvas.setFillColor(colors.HexColor("#B5D4F4"))
        canvas.drawString(36*mm, H - 19*mm, "Dynamic Electricity Tariff API")

        # Badge piano
        badge_x = W - 50*mm
        canvas.setFillColor(GREEN)
        canvas.roundRect(badge_x, H - 21*mm, 36*mm, 10*mm, 2*mm, fill=1, stroke=0)
        canvas.setFillColor(WHITE)
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawCentredString(badge_x + 18*mm, H - 16.5*mm, _t(lang, "plan_label"))

        # ── Footer ──
        canvas.setFillColor(GRAY_LINE)
        canvas.rect(14*mm, 12*mm, W - 28*mm, 0.3, fill=1, stroke=0)
        canvas.setFillColor(GRAY_TEXT)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawCentredString(W / 2, 8*mm, _t(lang, "footer"))

        # Numero pagina
        canvas.setFont("Helvetica", 7.5)
        canvas.drawRightString(W - 14*mm, 8*mm, f"{doc.page}")

        canvas.restoreState()

    return _header_footer


# ── Funzione pubblica ─────────────────────────────────────────────────────────

def generate_subscription_pdf(
    full_name:   str,
    email:       str,
    key_prefix:  str,
    rate_limit:  int,
    approved_at: datetime,
    company:     Optional[str] = None,
    lang:        str = "en",
) -> bytes:
    """
    Genera il PDF abbonamento e ritorna i bytes pronti per allegare all'email.
    """
    lang = lang if lang in _STRINGS else "en"
    buf  = io.BytesIO()

    W, H = A4
    margin_x = 18*mm
    margin_top = 35*mm   # sotto header
    margin_bot = 22*mm   # sopra footer

    doc = BaseDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=margin_x,
        rightMargin=margin_x,
        topMargin=margin_top,
        bottomMargin=margin_bot,
    )

    cb = _make_page_callbacks(lang, _t(lang, "doc_title"))
    frame = Frame(margin_x, margin_bot, W - 2*margin_x, H - margin_top - margin_bot, id="main")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=cb)])

    # ── Stili ────────────────────────────────────────────────────────────────

    s_title = ParagraphStyle("title",
        fontName="Helvetica-Bold", fontSize=18,
        textColor=BLUE_DARK, spaceAfter=2*mm, leading=22)

    s_subtitle = ParagraphStyle("subtitle",
        fontName="Helvetica", fontSize=10,
        textColor=GRAY_TEXT, spaceAfter=6*mm)

    s_desc = ParagraphStyle("desc",
        fontName="Helvetica", fontSize=10,
        textColor=BLACK, leading=15, spaceAfter=6*mm)

    s_section = ParagraphStyle("section",
        fontName="Helvetica-Bold", fontSize=11,
        textColor=BLUE_MID, spaceBefore=5*mm, spaceAfter=3*mm)

    s_terms_title = ParagraphStyle("terms_title",
        fontName="Helvetica-Bold", fontSize=10,
        textColor=BLUE_DARK, spaceBefore=6*mm, spaceAfter=2*mm)

    s_terms = ParagraphStyle("terms",
        fontName="Helvetica", fontSize=9,
        textColor=GRAY_TEXT, leading=14)

    s_cell_label = ParagraphStyle("cell_label",
        fontName="Helvetica-Bold", fontSize=9, textColor=GRAY_TEXT)

    s_cell_value = ParagraphStyle("cell_value",
        fontName="Helvetica", fontSize=10, textColor=BLACK)

    # ── Contenuto ────────────────────────────────────────────────────────────

    story = []

    # Titolo documento
    story.append(Paragraph(_t(lang, "doc_title"), s_title))
    story.append(Paragraph(_t(lang, "subtitle"), s_subtitle))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BLUE_LIGHT, spaceAfter=5*mm))
    story.append(Paragraph(_t(lang, "plan_desc"), s_desc))

    # Tabella dati account
    story.append(Paragraph(_t(lang, "holder"), s_section))

    date_str = approved_at.strftime("%d %B %Y") if approved_at else "—"
    limit_str = _t(lang, "limit_val").format(limit=rate_limit) if rate_limit else _t(lang, "unlimited")

    rows = [
        (_t(lang, "holder"),    full_name),
        (_t(lang, "email_lbl"), email),
    ]
    if company:
        rows.append((_t(lang, "company_lbl"), company))
    rows += [
        (_t(lang, "key_lbl"),   key_prefix + "..."),
        (_t(lang, "limit_lbl"), limit_str),
        (_t(lang, "since_lbl"), date_str),
    ]

    col_w = [55*mm, W - 2*margin_x - 55*mm]
    table_data = [
        [Paragraph(r[0], s_cell_label), Paragraph(r[1], s_cell_value)]
        for r in rows
    ]

    tbl = Table(table_data, colWidths=col_w, repeatRows=0)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (-1, 0),  BLUE_LIGHT),
        ("BACKGROUND",  (0, 2), (-1, 2),  BLUE_LIGHT),
        ("BACKGROUND",  (0, 4), (-1, 4),  BLUE_LIGHT),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [BLUE_LIGHT, WHITE]),
        ("BOX",         (0, 0), (-1, -1), 0.5, GRAY_LINE),
        ("INNERGRID",   (0, 0), (-1, -1), 0.3, GRAY_LINE),
        ("TOPPADDING",  (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",(0, 0),(-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",(0, 0), (-1, -1), 8),
        ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
        ("ROUNDEDCORNERS", [3]),
    ]))
    story.append(tbl)

    # Sezione termini
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(_t(lang, "terms_title"), s_terms_title))
    story.append(HRFlowable(width="100%", thickness=0.3, color=GRAY_LINE, spaceAfter=3*mm))
    story.append(Paragraph(_t(lang, "terms_body"), s_terms))

    # Build
    doc.build(story)
    return buf.getvalue()