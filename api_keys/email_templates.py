"""
email_templates.py
==================
Template email multilingua (EN/DE/FR/IT) per tutti gli eventi del sistema
di API key management.

Lingua scelta in base al campo preferred_lang dell'utente per le email
all'utente, e in base a EMAIL_LANG nel .env per le email admin.

Usa Brevo SMTP relay — funziona su Render free tier (porta 587).

Variabili d'ambiente richieste:
  SMTP_HOST      smtp-relay.brevo.com
  SMTP_PORT      587
  SMTP_USER      a9b617001@smtp-brevo.com   (login Brevo)
  SMTP_PASSWORD  la password/API key Brevo
  SMTP_FROM      Swiss Tariff Hub <tuamail@gmail.com>
"""

from __future__ import annotations

import logging
import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

log = logging.getLogger("email_templates")


# ── SMTP config ───────────────────────────────────────────────────────────────

def _smtp_config() -> dict:
    return {
        "host":     os.getenv("SMTP_HOST", "smtp-relay.brevo.com"),
        "port":     int(os.getenv("SMTP_PORT", "587")),
        "user":     os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "from":     os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "")),
    }


def _send(
    to: str,
    subject: str,
    html_body: str,
    text_body: str = "",
    attachment_bytes: Optional[bytes] = None,
    attachment_filename: str = "subscription.pdf",
) -> bool:
    """
    Invia una email via Brevo SMTP relay.
    Ritorna True se ok, False se fallisce.
    """
    cfg = _smtp_config()
    if not cfg["user"] or not cfg["password"]:
        log.warning("[email] SMTP non configurato — email non inviata")
        return False

    try:
        if attachment_bytes:
            msg = MIMEMultipart("mixed")
            alt = MIMEMultipart("alternative")
            if text_body:
                alt.attach(MIMEText(text_body, "plain", "utf-8"))
            alt.attach(MIMEText(html_body, "html", "utf-8"))
            msg.attach(alt)
            part = MIMEBase("application", "pdf")
            part.set_payload(attachment_bytes)
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{attachment_filename}"',
            )
            msg.attach(part)
        else:
            msg = MIMEMultipart("alternative")
            if text_body:
                msg.attach(MIMEText(text_body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))

        msg["Subject"] = subject
        msg["From"]    = cfg["from"]
        msg["To"]      = to

        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["from"], [to], msg.as_string())

        log.info(f"[email] Inviata a {to}: {subject}")
        return True

    except Exception as e:
        log.error(f"[email] Errore invio a {to}: {e}")
        return False


# ── Traduzioni soggetti e testi ───────────────────────────────────────────────

def _t(lang: str, key: str) -> str:
    T = {
        "en": {
            "reg_subject":        "Your API key request — Swiss Tariff Hub",
            "reg_heading":        "Request received",
            "reg_body":           "Thank you for registering. Your request is under review and you will receive your API key by email once approved.",
            "reg_footer":         "If you did not make this request, please ignore this email.",

            "approved_subject":   "Your API key — Swiss Tariff Hub",
            "approved_heading":   "Your API key is ready",
            "approved_body":      "Your API key has been approved. Please save it immediately — it will <strong>never be shown again</strong>.",
            "approved_key_label": "Your API key:",
            "approved_usage":     "Use this key in the <code>X-API-Key</code> header for all API requests.",
            "approved_limit":     "Your daily limit:",
            "approved_limit_val": "{limit} requests/day (resets at midnight CET/CEST)",
            "approved_pdf_note":  "Your free subscription certificate is attached as a PDF.",
            "approved_footer":    "If you did not request this key, contact us immediately.",

            "rejected_subject":   "Your API key request — Swiss Tariff Hub",
            "rejected_heading":   "Request not approved",
            "rejected_body":      "We were unable to approve your API key request at this time.",
            "rejected_reason":    "Reason:",
            "rejected_footer":    "You may submit a new request if your circumstances change.",

            "suspended_subject":  "API key suspended — Swiss Tariff Hub",
            "suspended_heading":  "Your API key has been suspended",
            "suspended_body":     "Your API key has been temporarily suspended and is no longer functional.",
            "suspended_reason":   "Reason:",
            "suspended_footer":   "Please contact us if you believe this is an error.",

            "reactivated_subject":"API key reactivated — Swiss Tariff Hub",
            "reactivated_heading":"Your API key is active again",
            "reactivated_body":   "Your API key has been reactivated and is now fully functional.",

            "newkey_subject":     "New API key request — Swiss Tariff Hub",
            "newkey_heading":     "New key request received",
            "newkey_body":        "Your previous API key has been permanently invalidated. A new key request is under review.",

            "rl80_subject":       "API key usage alert — Swiss Tariff Hub",
            "rl80_heading":       "80% of your daily limit reached",
            "rl80_body":          "You have used <strong>{used}</strong> of your <strong>{limit}</strong> daily requests.",
            "rl80_footer":        "Your limit resets at midnight CET/CEST.",

            "rl100_subject":      "Daily limit reached — Swiss Tariff Hub",
            "rl100_heading":      "Daily request limit reached",
            "rl100_body":         "You have reached your daily limit of <strong>{limit}</strong> requests.",
            "rl100_footer":       "Your limit will reset at midnight CET/CEST. Contact us to upgrade your plan.",

            "admin_newreg_subject": "New API key registration — {name}",
            "admin_newreg_heading": "New registration request",
            "admin_newreg_body":    "A new API key request has been submitted.",
            "admin_field_name":     "Name",
            "admin_field_email":    "Email",
            "admin_field_company":  "Company",
            "admin_field_vat":      "VAT",
            "admin_field_country":  "Country",
            "admin_approve_btn":    "Review in admin panel",
        },
        "de": {
            "reg_subject":        "Ihre API-Key-Anfrage — Swiss Tariff Hub",
            "reg_heading":        "Anfrage erhalten",
            "reg_body":           "Vielen Dank für Ihre Registrierung. Ihre Anfrage wird geprüft und Sie erhalten Ihren API-Key per E-Mail sobald er genehmigt wurde.",
            "reg_footer":         "Falls Sie diese Anfrage nicht gestellt haben, ignorieren Sie diese E-Mail.",

            "approved_subject":   "Ihr API-Key — Swiss Tariff Hub",
            "approved_heading":   "Ihr API-Key ist bereit",
            "approved_body":      "Ihr API-Key wurde genehmigt. Bitte speichern Sie ihn sofort — er wird <strong>nie wieder angezeigt</strong>.",
            "approved_key_label": "Ihr API-Key:",
            "approved_usage":     "Verwenden Sie diesen Key im <code>X-API-Key</code>-Header für alle API-Anfragen.",
            "approved_limit":     "Ihr Tageslimit:",
            "approved_limit_val": "{limit} Anfragen/Tag (Zurücksetzung um Mitternacht MEZ/MESZ)",
            "approved_pdf_note":  "Ihr kostenloser Abonnementnachweis ist als PDF beigefügt.",
            "approved_footer":    "Falls Sie diesen Key nicht beantragt haben, kontaktieren Sie uns sofort.",

            "rejected_subject":   "Ihre API-Key-Anfrage — Swiss Tariff Hub",
            "rejected_heading":   "Anfrage nicht genehmigt",
            "rejected_body":      "Wir konnten Ihre API-Key-Anfrage derzeit nicht genehmigen.",
            "rejected_reason":    "Grund:",
            "rejected_footer":    "Sie können eine neue Anfrage stellen, wenn sich Ihre Situation ändert.",

            "suspended_subject":  "API-Key gesperrt — Swiss Tariff Hub",
            "suspended_heading":  "Ihr API-Key wurde gesperrt",
            "suspended_body":     "Ihr API-Key wurde vorübergehend gesperrt und ist nicht mehr funktionsfähig.",
            "suspended_reason":   "Grund:",
            "suspended_footer":   "Bitte kontaktieren Sie uns, wenn Sie dies für einen Fehler halten.",

            "reactivated_subject":"API-Key reaktiviert — Swiss Tariff Hub",
            "reactivated_heading":"Ihr API-Key ist wieder aktiv",
            "reactivated_body":   "Ihr API-Key wurde reaktiviert und ist nun vollständig funktionsfähig.",

            "newkey_subject":     "Neue API-Key-Anfrage — Swiss Tariff Hub",
            "newkey_heading":     "Neue Key-Anfrage erhalten",
            "newkey_body":        "Ihr bisheriger API-Key wurde dauerhaft ungültig gemacht. Eine neue Key-Anfrage wird geprüft.",

            "rl80_subject":       "API-Key Nutzungswarnung — Swiss Tariff Hub",
            "rl80_heading":       "80% Ihres Tageslimits erreicht",
            "rl80_body":          "Sie haben <strong>{used}</strong> von <strong>{limit}</strong> täglichen Anfragen verwendet.",
            "rl80_footer":        "Ihr Limit wird um Mitternacht MEZ/MESZ zurückgesetzt.",

            "rl100_subject":      "Tageslimit erreicht — Swiss Tariff Hub",
            "rl100_heading":      "Tägliches Anfragelimit erreicht",
            "rl100_body":         "Sie haben Ihr Tageslimit von <strong>{limit}</strong> Anfragen erreicht.",
            "rl100_footer":       "Ihr Limit wird um Mitternacht MEZ/MESZ zurückgesetzt.",

            "admin_newreg_subject": "Neue API-Key-Registrierung — {name}",
            "admin_newreg_heading": "Neue Registrierungsanfrage",
            "admin_newreg_body":    "Eine neue API-Key-Anfrage wurde eingereicht.",
            "admin_field_name":     "Name",
            "admin_field_email":    "E-Mail",
            "admin_field_company":  "Unternehmen",
            "admin_field_vat":      "MwSt-Nr.",
            "admin_field_country":  "Land",
            "admin_approve_btn":    "Im Admin-Panel prüfen",
        },
        "fr": {
            "reg_subject":        "Votre demande de clé API — Swiss Tariff Hub",
            "reg_heading":        "Demande reçue",
            "reg_body":           "Merci pour votre inscription. Votre demande est en cours d'examen et vous recevrez votre clé API par e-mail une fois approuvée.",
            "reg_footer":         "Si vous n'avez pas effectué cette demande, veuillez ignorer cet e-mail.",

            "approved_subject":   "Votre clé API — Swiss Tariff Hub",
            "approved_heading":   "Votre clé API est prête",
            "approved_body":      "Votre clé API a été approuvée. Veuillez la sauvegarder immédiatement — elle ne sera <strong>plus jamais affichée</strong>.",
            "approved_key_label": "Votre clé API :",
            "approved_usage":     "Utilisez cette clé dans l'en-tête <code>X-API-Key</code> pour toutes les requêtes API.",
            "approved_limit":     "Votre limite quotidienne :",
            "approved_limit_val": "{limit} requêtes/jour (réinitialisation à minuit CET/CEST)",
            "approved_pdf_note":  "Votre certificat d'abonnement gratuit est joint en PDF.",
            "approved_footer":    "Si vous n'avez pas demandé cette clé, contactez-nous immédiatement.",

            "rejected_subject":   "Votre demande de clé API — Swiss Tariff Hub",
            "rejected_heading":   "Demande non approuvée",
            "rejected_body":      "Nous n'avons pas pu approuver votre demande de clé API pour le moment.",
            "rejected_reason":    "Motif :",
            "rejected_footer":    "Vous pouvez soumettre une nouvelle demande si votre situation évolue.",

            "suspended_subject":  "Clé API suspendue — Swiss Tariff Hub",
            "suspended_heading":  "Votre clé API a été suspendue",
            "suspended_body":     "Votre clé API a été temporairement suspendue et n'est plus fonctionnelle.",
            "suspended_reason":   "Motif :",
            "suspended_footer":   "Veuillez nous contacter si vous pensez qu'il s'agit d'une erreur.",

            "reactivated_subject":"Clé API réactivée — Swiss Tariff Hub",
            "reactivated_heading":"Votre clé API est à nouveau active",
            "reactivated_body":   "Votre clé API a été réactivée et est désormais pleinement fonctionnelle.",

            "newkey_subject":     "Nouvelle demande de clé API — Swiss Tariff Hub",
            "newkey_heading":     "Nouvelle demande de clé reçue",
            "newkey_body":        "Votre ancienne clé API a été définitivement invalidée. Une nouvelle demande est en cours d'examen.",

            "rl80_subject":       "Alerte d'utilisation API — Swiss Tariff Hub",
            "rl80_heading":       "80% de votre limite quotidienne atteints",
            "rl80_body":          "Vous avez utilisé <strong>{used}</strong> de vos <strong>{limit}</strong> requêtes quotidiennes.",
            "rl80_footer":        "Votre limite se réinitialise à minuit CET/CEST.",

            "rl100_subject":      "Limite quotidienne atteinte — Swiss Tariff Hub",
            "rl100_heading":      "Limite de requêtes quotidiennes atteinte",
            "rl100_body":         "Vous avez atteint votre limite quotidienne de <strong>{limit}</strong> requêtes.",
            "rl100_footer":       "Votre limite sera réinitialisée à minuit CET/CEST.",

            "admin_newreg_subject": "Nouvelle inscription API — {name}",
            "admin_newreg_heading": "Nouvelle demande d'inscription",
            "admin_newreg_body":    "Une nouvelle demande de clé API a été soumise.",
            "admin_field_name":     "Nom",
            "admin_field_email":    "E-mail",
            "admin_field_company":  "Entreprise",
            "admin_field_vat":      "N° TVA",
            "admin_field_country":  "Pays",
            "admin_approve_btn":    "Examiner dans le panneau admin",
        },
        "it": {
            "reg_subject":        "La tua richiesta di chiave API — Swiss Tariff Hub",
            "reg_heading":        "Richiesta ricevuta",
            "reg_body":           "Grazie per esserti registrato. La tua richiesta è in fase di revisione e riceverai la tua chiave API via email una volta approvata.",
            "reg_footer":         "Se non hai effettuato questa richiesta, ignora questa email.",

            "approved_subject":   "La tua chiave API — Swiss Tariff Hub",
            "approved_heading":   "La tua chiave API è pronta",
            "approved_body":      "La tua chiave API è stata approvata. Salvala immediatamente — <strong>non verrà mai più mostrata</strong>.",
            "approved_key_label": "La tua chiave API:",
            "approved_usage":     "Usa questa chiave nell'header <code>X-API-Key</code> per tutte le richieste API.",
            "approved_limit":     "Il tuo limite giornaliero:",
            "approved_limit_val": "{limit} richieste/giorno (reset a mezzanotte CET/CEST)",
            "approved_pdf_note":  "Il tuo attestato di abbonamento gratuito è allegato in PDF.",
            "approved_footer":    "Se non hai richiesto questa chiave, contattaci immediatamente.",

            "rejected_subject":   "La tua richiesta di chiave API — Swiss Tariff Hub",
            "rejected_heading":   "Richiesta non approvata",
            "rejected_body":      "Non siamo stati in grado di approvare la tua richiesta di chiave API al momento.",
            "rejected_reason":    "Motivo:",
            "rejected_footer":    "Puoi inviare una nuova richiesta se la tua situazione cambia.",

            "suspended_subject":  "Chiave API sospesa — Swiss Tariff Hub",
            "suspended_heading":  "La tua chiave API è stata sospesa",
            "suspended_body":     "La tua chiave API è stata temporaneamente sospesa e non è più funzionante.",
            "suspended_reason":   "Motivo:",
            "suspended_footer":   "Contattaci se ritieni che si tratti di un errore.",

            "reactivated_subject":"Chiave API riattivata — Swiss Tariff Hub",
            "reactivated_heading":"La tua chiave API è di nuovo attiva",
            "reactivated_body":   "La tua chiave API è stata riattivata ed è ora completamente funzionante.",

            "newkey_subject":     "Nuova richiesta di chiave API — Swiss Tariff Hub",
            "newkey_heading":     "Nuova richiesta di chiave ricevuta",
            "newkey_body":        "La tua precedente chiave API è stata invalidata definitivamente. Una nuova richiesta è in fase di revisione.",

            "rl80_subject":       "Avviso utilizzo API — Swiss Tariff Hub",
            "rl80_heading":       "Raggiunto l'80% del limite giornaliero",
            "rl80_body":          "Hai utilizzato <strong>{used}</strong> delle tue <strong>{limit}</strong> richieste giornaliere.",
            "rl80_footer":        "Il tuo limite si azzera a mezzanotte CET/CEST.",

            "rl100_subject":      "Limite giornaliero raggiunto — Swiss Tariff Hub",
            "rl100_heading":      "Limite giornaliero di richieste raggiunto",
            "rl100_body":         "Hai raggiunto il tuo limite giornaliero di <strong>{limit}</strong> richieste.",
            "rl100_footer":       "Il limite si azzererà a mezzanotte CET/CEST. Contattaci per aumentare il tuo piano.",

            "admin_newreg_subject": "Nuova registrazione API — {name}",
            "admin_newreg_heading": "Nuova richiesta di registrazione",
            "admin_newreg_body":    "È stata inviata una nuova richiesta di chiave API.",
            "admin_field_name":     "Nome",
            "admin_field_email":    "Email",
            "admin_field_company":  "Azienda",
            "admin_field_vat":      "P.IVA",
            "admin_field_country":  "Paese",
            "admin_approve_btn":    "Esamina nel pannello admin",
        },
    }
    lang_data = T.get(lang, T["en"])
    return lang_data.get(key, T["en"].get(key, key))


# ── HTML base template ────────────────────────────────────────────────────────

def _html_wrap(heading: str, body: str, footer: str = "") -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  body{{font-family:system-ui,sans-serif;background:#f4f5f7;margin:0;padding:24px}}
  .card{{background:white;border-radius:12px;padding:32px;max-width:560px;
         margin:0 auto;border:0.5px solid #e5e5e5}}
  h2{{color:#1a1a2e;font-size:18px;margin:0 0 16px}}
  p{{color:#555;font-size:14px;line-height:1.6;margin:0 0 12px}}
  .key-box{{background:#f0f4fa;border-radius:8px;padding:14px 18px;
            font-family:monospace;font-size:13px;color:#185fa5;
            word-break:break-all;margin:16px 0;border:0.5px solid #c5d8f0}}
  .badge{{display:inline-block;background:#1d9e75;color:white;
          font-size:11px;padding:3px 10px;border-radius:999px;font-weight:600}}
  .warn{{color:#ba7517;font-weight:600}}
  .footer{{font-size:11px;color:#aaa;margin-top:24px;padding-top:16px;
           border-top:0.5px solid #e5e5e5}}
  .btn{{display:inline-block;background:#185fa5;color:white;padding:10px 20px;
        border-radius:8px;text-decoration:none;font-size:13px;font-weight:500;
        margin-top:12px}}
</style></head><body>
<div class="card">
  <div style="margin-bottom:20px">
    <span style="font-size:11px;color:#888;font-weight:500;
                 text-transform:uppercase;letter-spacing:.05em">
      Swiss Tariff Hub
    </span>
  </div>
  <h2>{heading}</h2>
  {body}
  {f'<p class="footer">{footer}</p>' if footer else ''}
</div></body></html>"""


# ── Email: conferma registrazione ─────────────────────────────────────────────

def send_registration_confirmation(email: str, full_name: str, lang: str = "en") -> bool:
    subject = _t(lang, "reg_subject")
    body    = f"<p>{_t(lang, 'reg_body')}</p>"
    html    = _html_wrap(_t(lang, "reg_heading"), body, _t(lang, "reg_footer"))
    return _send(email, subject, html)


# ── Email: key approvata ──────────────────────────────────────────────────────

def send_key_approved(
    email: str,
    full_name: str,
    raw_key: str,
    rate_limit: int,
    lang: str = "en",
    company: Optional[str] = None,
    approved_at=None,
    key_prefix: str = "",
) -> bool:
    subject   = _t(lang, "approved_subject")
    limit_str = _t(lang, "approved_limit_val").format(limit=rate_limit)
    body = f"""
    <p>{_t(lang, "approved_body")}</p>
    <p><strong>{_t(lang, "approved_key_label")}</strong></p>
    <div class="key-box">{raw_key}</div>
    <p class="warn">&#9888; {_t(lang, "approved_body")}</p>
    <p>{_t(lang, "approved_usage")}</p>
    <p><strong>{_t(lang, "approved_limit")}</strong> {limit_str}</p>
    <p style="margin-top:16px;font-size:13px;color:#555">
        &#128196; {_t(lang, "approved_pdf_note")}
    </p>
    """
    html = _html_wrap(_t(lang, "approved_heading"), body, _t(lang, "approved_footer"))

    pdf_bytes = None
    try:
        from generate_subscription_pdf import generate_subscription_pdf
        from datetime import datetime, timezone
        at = approved_at or datetime.now(timezone.utc)
        pdf_bytes = generate_subscription_pdf(
            full_name=full_name, email=email,
            key_prefix=key_prefix or raw_key[:12],
            rate_limit=rate_limit, approved_at=at,
            company=company, lang=lang,
        )
    except Exception as e:
        log.error(f"[email] Errore generazione PDF per {email}: {e}")

    filename_map = {
        "en": "subscription.pdf",
        "de": "abonnement.pdf",
        "fr": "abonnement.pdf",
        "it": "abbonamento.pdf",
    }
    return _send(
        email, subject, html,
        attachment_bytes=pdf_bytes,
        attachment_filename=filename_map.get(lang, "subscription.pdf"),
    )


# ── Email: registrazione rifiutata ────────────────────────────────────────────

def send_registration_rejected(
    email: str,
    full_name: str,
    reason: str = "",
    lang: str = "en",
) -> bool:
    subject      = _t(lang, "rejected_subject")
    reason_block = f"<p><strong>{_t(lang, 'rejected_reason')}</strong> {reason}</p>" if reason else ""
    body         = f"<p>{_t(lang, 'rejected_body')}</p>{reason_block}"
    html         = _html_wrap(_t(lang, "rejected_heading"), body, _t(lang, "rejected_footer"))
    return _send(email, subject, html)


# ── Email: key sospesa ────────────────────────────────────────────────────────

def send_key_suspended(
    email: str,
    full_name: str,
    reason: str = "",
    lang: str = "en",
) -> bool:
    subject      = _t(lang, "suspended_subject")
    reason_block = f"<p><strong>{_t(lang, 'suspended_reason')}</strong> {reason}</p>" if reason else ""
    body         = f"<p>{_t(lang, 'suspended_body')}</p>{reason_block}"
    html         = _html_wrap(_t(lang, "suspended_heading"), body, _t(lang, "suspended_footer"))
    return _send(email, subject, html)


# ── Email: key riattivata ─────────────────────────────────────────────────────

def send_key_reactivated(email: str, full_name: str, lang: str = "en") -> bool:
    subject = _t(lang, "reactivated_subject")
    body    = f"<p>{_t(lang, 'reactivated_body')}</p>"
    html    = _html_wrap(_t(lang, "reactivated_heading"), body)
    return _send(email, subject, html)


# ── Email: nuova key richiesta (vecchia invalidata) ───────────────────────────

def send_new_key_requested(email: str, full_name: str, lang: str = "en") -> bool:
    subject = _t(lang, "newkey_subject")
    body    = f"<p>{_t(lang, 'newkey_body')}</p>"
    html    = _html_wrap(_t(lang, "newkey_heading"), body)
    return _send(email, subject, html)


# ── Email: rate limit warning ─────────────────────────────────────────────────

def send_rate_limit_warning(record, pct: int) -> bool:
    """record è un oggetto ApiKey."""
    lang  = record.preferred_lang or "en"
    used  = record.requests_today
    limit = record.rate_limit_day

    if pct >= 100:
        subject  = _t(lang, "rl100_subject")
        heading  = _t(lang, "rl100_heading")
        body_txt = _t(lang, "rl100_body").format(limit=limit)
        footer   = _t(lang, "rl100_footer")
    else:
        subject  = _t(lang, "rl80_subject")
        heading  = _t(lang, "rl80_heading")
        body_txt = _t(lang, "rl80_body").format(used=used, limit=limit)
        footer   = _t(lang, "rl80_footer")

    body = f"<p>{body_txt}</p>"
    html = _html_wrap(heading, body, footer)
    return _send(record.email, subject, html)


# ── Email: admin — nuova registrazione ────────────────────────────────────────

def send_admin_new_registration(record) -> bool:
    """Notifica admin di una nuova registrazione."""
    admin_email = os.getenv("ADMIN_EMAIL", "")
    if not admin_email:
        log.warning("[email] ADMIN_EMAIL non configurato")
        return False

    admin_url = os.getenv("APP_URL", "https://yourdomain.com")
    lang      = os.getenv("EMAIL_LANG", "en")

    subject = _t(lang, "admin_newreg_subject").format(name=record.full_name)

    company_row = ""
    if record.company:
        company_row = f"""
        <tr><td style="color:#888;padding:4px 8px">{_t(lang,'admin_field_company')}</td>
            <td style="padding:4px 8px"><strong>{record.company}</strong></td></tr>
        """
        if record.vat_number:
            company_row += f"""
        <tr><td style="color:#888;padding:4px 8px">{_t(lang,'admin_field_vat')}</td>
            <td style="padding:4px 8px">{record.vat_number}</td></tr>
        """

    body = f"""
    <p>{_t(lang, "admin_newreg_body")}</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin:16px 0">
      <tr><td style="color:#888;padding:4px 8px">{_t(lang,'admin_field_name')}</td>
          <td style="padding:4px 8px"><strong>{record.full_name}</strong></td></tr>
      <tr><td style="color:#888;padding:4px 8px">{_t(lang,'admin_field_email')}</td>
          <td style="padding:4px 8px">{record.email}</td></tr>
      {company_row}
      <tr><td style="color:#888;padding:4px 8px">{_t(lang,'admin_field_country')}</td>
          <td style="padding:4px 8px">{record.country}</td></tr>
    </table>
    <a href="{admin_url}/admin" class="btn">{_t(lang,'admin_approve_btn')} &rarr;</a>
    """
    html = _html_wrap(_t(lang, "admin_newreg_heading"), body)
    return _send(admin_email, subject, html)