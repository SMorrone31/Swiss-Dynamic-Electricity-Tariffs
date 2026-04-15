"""
api_keys.py
===========
Sistema completo di gestione API key per Swiss Tariff Hub.

FLUSSO CON APPROVAZIONE (opzione A — consigliata per B2B):
  1. Cliente va su POST /register con nome, email, azienda
  2. Sistema salva la richiesta in stato "pending" e ti manda email
  3. Tu vai su POST /admin/keys/{key_id}/approve con la tua ADMIN_KEY
  4. Sistema genera la chiave e la manda via email direttamente al cliente
  5. Cliente usa la chiave in X-API-Key su ogni chiamata
  6. Tu puoi revocare con DELETE /admin/keys/{key_id}

FLUSSO DIRETTO (opzione B — senza approvazione):
  1. Cliente va su POST /register con nome, email, azienda
  2. Sistema genera la chiave e la manda subito via email al cliente
  (attiva impostando AUTO_APPROVE=true nel .env)

CONFIGURAZIONE .env:
  ADMIN_KEY=tua-chiave-master-segreta
  AUTO_APPROVE=false          # true = chiave inviata subito senza approvazione
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=tua@gmail.com
  SMTP_PASSWORD=app-password-gmail
  SERVICE_NAME=Swiss Tariff Hub          # nome del servizio nelle email
  SERVICE_URL=https://tuoserver.com      # URL base del servizio

INTEGRAZIONE in main.py (dopo app = FastAPI(...)):
  from api_keys import setup_api_keys
  setup_api_keys(app, lambda: SessionLocal)
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from sqlalchemy import Boolean, Column, DateTime, Index, String, Text

log = logging.getLogger("api_keys")


# ── Modello DB ────────────────────────────────────────────────────────────────

def get_api_key_model(Base):
    class ApiKey(Base):
        """
        Registrazione + API key di un cliente.
        Stati:
          pending  → registrazione ricevuta, in attesa di approvazione
          active   → chiave attiva, cliente può chiamare le API
          revoked  → chiave revocata, tutte le chiamate tornano 401
        """
        __tablename__ = "api_keys"

        key_id        = Column(String(20),  primary_key=True)
        key_hash      = Column(String(64),  nullable=True,  unique=True, index=True)
        # Dati registrazione cliente
        client_name   = Column(Text,        nullable=False)
        client_email  = Column(Text,        nullable=False)
        company       = Column(Text,        default="")
        notes         = Column(Text,        default="")
        # Stato: "pending" | "active" | "revoked"
        status        = Column(String(20),  default="pending", nullable=False)
        # Timestamp lifecycle
        requested_at  = Column(DateTime(timezone=True),
                               default=lambda: datetime.now(timezone.utc))
        approved_at   = Column(DateTime(timezone=True), nullable=True)
        revoked_at    = Column(DateTime(timezone=True), nullable=True)
        last_used_at  = Column(DateTime(timezone=True), nullable=True)
        use_count     = Column(String(20),  default="0")

        __table_args__ = (
            Index("ix_api_keys_hash",   "key_hash"),
            Index("ix_api_keys_status", "status"),
            Index("ix_api_keys_email",  "client_email"),
        )

    return ApiKey


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _next_id(session, ApiKey) -> str:
    last = session.query(ApiKey.key_id).order_by(ApiKey.key_id.desc()).first()
    if not last:
        return "1"
    try:
        return str(int(last[0]) + 1)
    except (ValueError, TypeError):
        return str(secrets.randbelow(9000) + 1000)


def _verify_key(session, ApiKey, raw_key: str) -> Optional[object]:
    """Verifica chiave raw contro DB. Aggiorna statistiche se valida."""
    row = session.query(ApiKey).filter(
        ApiKey.key_hash == _hash_key(raw_key),
        ApiKey.status   == "active",
    ).first()
    if row:
        row.last_used_at = datetime.now(timezone.utc)
        try:
            row.use_count = str(int(row.use_count or "0") + 1)
        except ValueError:
            row.use_count = "1"
        session.commit()
    return row


# ── Email ─────────────────────────────────────────────────────────────────────

def _send_email(to: str, subject: str, body_text: str, body_html: str = "") -> bool:
    """Invia email usando le stesse credenziali SMTP dello scheduler."""
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASSWORD", "")

    if not smtp_user or not smtp_pass:
        log.warning(f"SMTP non configurato — email non inviata a {to}")
        log.warning(f"  Soggetto: {subject}")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = to
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            if smtp_port != 25:
                server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        log.info(f"Email inviata a {to}: {subject}")
        return True
    except Exception as e:
        log.error(f"Invio email fallito a {to}: {e}")
        return False


def _email_to_admin(row, admin_email: str, service_url: str, service_name: str):
    """Notifica te quando arriva una nuova richiesta di accesso."""
    subject = f"[{service_name}] Nuova richiesta accesso API — {row.company or row.client_name}"
    text = f"""\
{service_name} — Nuova richiesta accesso API
=============================================

Nome:    {row.client_name}
Email:   {row.client_email}
Azienda: {row.company or '—'}
Note:    {row.notes or '—'}
ID:      {row.key_id}
Ricevuta: {row.requested_at.strftime('%Y-%m-%d %H:%M UTC')}

Per APPROVARE (invia subito la chiave al cliente):
  curl -X POST {service_url}/admin/keys/{row.key_id}/approve \\
       -H "X-API-Key: TUA_ADMIN_KEY"

Per RIFIUTARE:
  curl -X DELETE {service_url}/admin/keys/{row.key_id} \\
       -H "X-API-Key: TUA_ADMIN_KEY"

Per vedere tutte le richieste pendenti:
  curl {service_url}/admin/keys?status=pending \\
       -H "X-API-Key: TUA_ADMIN_KEY"
"""
    _send_email(admin_email, subject, text)


def _email_to_client_pending(row, service_name: str):
    """Conferma al cliente che la richiesta è stata ricevuta."""
    subject = f"[{service_name}] Richiesta accesso ricevuta"
    text = f"""\
Gentile {row.client_name},

la tua richiesta di accesso alle API di {service_name} è stata ricevuta.

Dati registrazione:
  Nome:    {row.client_name}
  Email:   {row.client_email}
  Azienda: {row.company or '—'}

La tua richiesta verrà valutata a breve.
Riceverai un'altra email con le istruzioni di accesso non appena approvata.

Per qualsiasi domanda rispondi a questa email.

{service_name}
"""
    _send_email(row.client_email, subject, text)


def _email_to_client_approved(row, raw_key: str, service_name: str, service_url: str):
    """Invia la chiave API al cliente dopo approvazione."""
    subject = f"[{service_name}] Accesso API approvato — la tua chiave"
    text = f"""\
Gentile {row.client_name},

la tua richiesta di accesso alle API di {service_name} è stata approvata.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
API KEY: {raw_key}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

IMPORTANTE: salva questa chiave in modo sicuro.
Non verrà mostrata di nuovo — se la perdi dovrai richiederne una nuova.

━━ Come usare la chiave ━━━━━━━━━━━━━━━━━━━

Includi l'header X-API-Key in ogni chiamata:

  # Prezzi di oggi per CAP 6810 (Lugano):
  curl -H "X-API-Key: {raw_key}" \\
       "{service_url}/api/v1/prices/today?zip_code=6810"

  # Prezzi di domani:
  curl -H "X-API-Key: {raw_key}" \\
       "{service_url}/api/v1/prices/tomorrow?zip_code=6810"

  # Lista di tutte le tariffe disponibili:
  curl -H "X-API-Key: {raw_key}" \\
       "{service_url}/api/v1/tariffs"

━━ Endpoint principali ━━━━━━━━━━━━━━━━━━━━

  GET /api/v1/prices/today?zip_code=XXXX
      → 96 slot da 15 min con prezzi in CHF/kWh per oggi

  GET /api/v1/prices/tomorrow?zip_code=XXXX
      → prezzi di domani (disponibili dopo le 12:05 per CKW,
        dopo le 18:30 per gli altri provider)

  GET /api/v1/tariffs
      → lista provider con metadati (CAP coperti, aggiornamento, ecc.)

  GET /api/v1/health
      → stato del sistema e data ultimo aggiornamento per provider

━━ Formato risposta ━━━━━━━━━━━━━━━━━━━━━━━

  Tutti i timestamp sono in UTC (ISO 8601).
  I prezzi sono in CHF/kWh.
  Risoluzione temporale: 15 minuti (96 slot/giorno).

  Esempio risposta /prices/today:
  {{
    "tariff_id":  "aem_tariffa_dinamica",
    "date":       "2026-04-15",
    "slot_count": 96,
    "grid_price_utc": [
      {{"2026-04-14T22:00:00+00:00": 0.0842}},
      {{"2026-04-14T22:15:00+00:00": 0.0842}},
      ...
    ]
  }}

Per supporto tecnico rispondi a questa email.

{service_name}
"""
    _send_email(row.client_email, subject, text)


def _email_to_client_revoked(row, service_name: str):
    """Notifica il cliente che la sua chiave è stata revocata."""
    subject = f"[{service_name}] Accesso API disattivato"
    text = f"""\
Gentile {row.client_name},

il tuo accesso alle API di {service_name} è stato disattivato.

La tua chiave API non è più valida.
Tutte le chiamate riceveranno una risposta 401 Unauthorized.

Se ritieni che si tratti di un errore o desideri rinnovare l'accesso,
rispondi a questa email.

{service_name}
"""
    _send_email(row.client_email, subject, text)


# ── Setup principale ──────────────────────────────────────────────────────────

def setup_api_keys(app: FastAPI, get_session_local):
    """
    Registra middleware + endpoint sull'app FastAPI.

    get_session_local: callable che ritorna la SessionLocal factory.
    Esempio: setup_api_keys(app, lambda: SessionLocal)
    """
    from database import Base
    ApiKey = get_api_key_model(Base)

    # Crea tabella se non esiste
    def _ensure_table():
        from database import _engine
        if _engine is not None:
            Base.metadata.create_all(_engine, tables=[ApiKey.__table__])
    _ensure_table()

    ADMIN_KEY    = os.getenv("ADMIN_KEY", "")
    AUTO_APPROVE = os.getenv("AUTO_APPROVE", "false").lower() == "true"
    SERVICE_NAME = os.getenv("SERVICE_NAME", "Swiss Tariff Hub")
    SERVICE_URL  = os.getenv("SERVICE_URL",  "https://tuoserver.com").rstrip("/")
    ADMIN_EMAIL  = os.getenv("ALERT_EMAIL",  "")   # riusa la stessa email degli alert

    # ── Middleware autenticazione ─────────────────────────────────────────────

    @app.middleware("http")
    async def api_key_middleware(request: Request, call_next):
        path = request.url.path

        # Sempre pubblici
        if path in {"/", "/docs", "/redoc", "/openapi.json",
                    "/api/v1/health", "/register", "/register/"}:
            return await call_next(request)

        provided_key = request.headers.get("X-API-Key", "")

        # Admin endpoints → ADMIN_KEY
        if path.startswith("/admin/"):
            if not ADMIN_KEY:
                return JSONResponse(
                    {"error": "ADMIN_KEY not configured in .env"},
                    status_code=503,
                )
            if provided_key != ADMIN_KEY:
                return JSONResponse(
                    {"error": "unauthorized", "message": "Invalid ADMIN_KEY"},
                    status_code=401,
                )
            return await call_next(request)

        # API endpoints → chiave client
        if not provided_key:
            return JSONResponse(
                {"error":   "missing_api_key",
                 "message": "Include your API key in the X-API-Key header",
                 "docs":    f"{SERVICE_URL}/docs"},
                status_code=401,
            )

        # Chiavi statiche nel .env (retrocompatibilità)
        static_keys = {
            k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()
        }
        if provided_key in static_keys:
            return await call_next(request)

        # Verifica nel DB
        SessionLocal = get_session_local()
        with SessionLocal() as session:
            row = _verify_key(session, ApiKey, provided_key)

        if not row:
            return JSONResponse(
                {"error":   "invalid_api_key",
                 "message": "API key not found or revoked. Request access at "
                            f"{SERVICE_URL}/register"},
                status_code=401,
            )

        return await call_next(request)

    # ── Endpoint registrazione cliente (pubblico) ─────────────────────────────

    @app.get("/register", response_class=HTMLResponse,
             summary="Form registrazione accesso API")
    def register_form():
        """Pagina HTML con form di registrazione."""
        return f"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{SERVICE_NAME} — Richiedi accesso API</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:system-ui,sans-serif;background:#f4f5f7;
          display:flex;align-items:center;justify-content:center;min-height:100vh;padding:1rem}}
    .card{{background:white;border-radius:12px;border:0.5px solid #e5e5e5;
           padding:2rem;width:100%;max-width:480px}}
    h1{{font-size:18px;font-weight:500;margin-bottom:4px;color:#1a1a2e}}
    .sub{{font-size:13px;color:#888;margin-bottom:1.5rem}}
    label{{display:block;font-size:12px;color:#555;margin-bottom:4px;margin-top:14px}}
    input,textarea{{width:100%;padding:9px 12px;border:0.5px solid #ddd;border-radius:8px;
                    font-size:14px;outline:none;transition:border-color .15s}}
    input:focus,textarea:focus{{border-color:#185fa5}}
    textarea{{resize:vertical;min-height:80px;font-family:inherit}}
    .req{{color:#e24b4a;margin-left:2px}}
    button{{width:100%;margin-top:1.5rem;padding:11px;background:#1a1a2e;color:white;
            border:none;border-radius:8px;font-size:14px;cursor:pointer;transition:opacity .15s}}
    button:hover{{opacity:.85}}
    .msg{{margin-top:1rem;padding:10px 14px;border-radius:8px;font-size:13px;display:none}}
    .msg.ok{{background:#e1f5ee;color:#085041;border:0.5px solid #9fe1cb}}
    .msg.err{{background:#fcebeb;color:#791f1f;border:0.5px solid #f7c1c1}}
  </style>
</head>
<body>
<div class="card">
  <h1>{SERVICE_NAME}</h1>
  <div class="sub">Richiedi accesso alle API tariffe elettriche dinamiche svizzere</div>
  <form id="form">
    <label>Nome e cognome <span class="req">*</span></label>
    <input type="text" id="name" placeholder="Mario Rossi" required>
    <label>Email aziendale <span class="req">*</span></label>
    <input type="email" id="email" placeholder="mario.rossi@azienda.ch" required>
    <label>Azienda</label>
    <input type="text" id="company" placeholder="Azienda SA">
    <label>Uso previsto</label>
    <textarea id="notes" placeholder="Energy Management System, ottimizzazione consumi..."></textarea>
    <button type="submit">Richiedi accesso</button>
  </form>
  <div class="msg" id="msg"></div>
</div>
<script>
document.getElementById('form').onsubmit = async e => {{
  e.preventDefault();
  const btn = e.target.querySelector('button');
  btn.textContent = 'Invio in corso...'; btn.disabled = true;
  const msg = document.getElementById('msg');
  try {{
    const r = await fetch('/register', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        client_name:  document.getElementById('name').value.trim(),
        client_email: document.getElementById('email').value.trim(),
        company:      document.getElementById('company').value.trim(),
        notes:        document.getElementById('notes').value.trim(),
      }})
    }});
    const data = await r.json();
    if (r.ok) {{
      msg.className = 'msg ok';
      msg.textContent = data.message;
      msg.style.display = 'block';
      e.target.style.display = 'none';
    }} else {{
      throw new Error(data.detail || data.message || 'Errore');
    }}
  }} catch(err) {{
    msg.className = 'msg err';
    msg.textContent = 'Errore: ' + err.message;
    msg.style.display = 'block';
    btn.textContent = 'Richiedi accesso'; btn.disabled = false;
  }}
}};
</script>
</body>
</html>"""

    @app.post("/register", summary="Registrazione accesso API")
    def register(body: dict):
        """
        Il cliente invia nome, email, azienda.
        Se AUTO_APPROVE=false → stato pending, email a te + conferma al cliente.
        Se AUTO_APPROVE=true  → chiave generata e inviata subito al cliente.
        """
        client_name  = (body.get("client_name", "") or "").strip()
        client_email = (body.get("client_email", "") or "").strip()
        company      = (body.get("company", "") or "").strip()
        notes        = (body.get("notes", "") or "").strip()

        if not client_name:
            raise HTTPException(400, {"message": "client_name obbligatorio"})
        if not client_email or "@" not in client_email:
            raise HTTPException(400, {"message": "client_email non valida"})

        SessionLocal = get_session_local()
        with SessionLocal() as session:
            # Verifica email già registrata e attiva
            existing = session.query(ApiKey).filter(
                ApiKey.client_email == client_email,
                ApiKey.status.in_(["pending", "active"]),
            ).first()
            if existing:
                raise HTTPException(409, {
                    "message": f"Email {client_email} già registrata "
                               f"(stato: {existing.status}). "
                               f"Controlla la tua casella email."
                })

            key_id = _next_id(session, ApiKey)

            if AUTO_APPROVE:
                # Genera chiave subito
                raw_key  = secrets.token_urlsafe(32)
                key_hash = _hash_key(raw_key)
                row = ApiKey(
                    key_id=key_id, key_hash=key_hash,
                    client_name=client_name, client_email=client_email,
                    company=company, notes=notes,
                    status="active",
                    approved_at=datetime.now(timezone.utc),
                )
                session.add(row)
                session.commit()
                # Manda chiave al cliente
                _email_to_client_approved(row, raw_key, SERVICE_NAME, SERVICE_URL)
                return {
                    "status":  "approved",
                    "message": f"Accesso approvato. Controlla la tua email ({client_email}) "
                               f"per la chiave API e le istruzioni di utilizzo.",
                }
            else:
                # Stato pending — aspetta approvazione manuale
                row = ApiKey(
                    key_id=key_id, key_hash=None,
                    client_name=client_name, client_email=client_email,
                    company=company, notes=notes,
                    status="pending",
                )
                session.add(row)
                session.commit()
                # Email a te (admin)
                if ADMIN_EMAIL:
                    _email_to_admin(row, ADMIN_EMAIL, SERVICE_URL, SERVICE_NAME)
                # Conferma al cliente
                _email_to_client_pending(row, SERVICE_NAME)
                return {
                    "status":  "pending",
                    "message": f"Richiesta ricevuta. Riceverai un'email a {client_email} "
                               f"non appena la richiesta sarà approvata.",
                }

    # ── Endpoint admin ────────────────────────────────────────────────────────

    @app.get("/admin/keys", summary="Lista API key")
    def list_keys(status: Optional[str] = None):
        """Lista chiavi. status: pending | active | revoked | (vuoto = tutte)"""
        SessionLocal = get_session_local()
        with SessionLocal() as session:
            q = session.query(ApiKey)
            if status:
                q = q.filter(ApiKey.status == status)
            rows = q.order_by(ApiKey.key_id).all()

        return [
            {
                "key_id":       r.key_id,
                "client_name":  r.client_name,
                "client_email": r.client_email,
                "company":      r.company,
                "notes":        r.notes,
                "status":       r.status,
                "key_hint":     (r.key_hash[:8] + "...") if r.key_hash else None,
                "requested_at": r.requested_at.isoformat() if r.requested_at else None,
                "approved_at":  r.approved_at.isoformat()  if r.approved_at  else None,
                "revoked_at":   r.revoked_at.isoformat()   if r.revoked_at   else None,
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
                "use_count":    r.use_count,
            }
            for r in rows
        ]

    @app.post("/admin/keys/{key_id}/approve", summary="Approva richiesta e invia chiave al cliente")
    def approve_key(key_id: str):
        """
        Approva una richiesta pending:
        1. Genera la chiave API
        2. La salva nel DB (solo hash)
        3. Manda email al cliente con la chiave e le istruzioni
        """
        SessionLocal = get_session_local()
        with SessionLocal() as session:
            row = session.query(ApiKey).filter(ApiKey.key_id == key_id).first()
            if not row:
                raise HTTPException(404, {"error": f"Key '{key_id}' not found"})
            if row.status != "pending":
                raise HTTPException(400, {
                    "error": f"Key '{key_id}' is '{row.status}', not 'pending'"
                })

            raw_key        = secrets.token_urlsafe(32)
            row.key_hash   = _hash_key(raw_key)
            row.status     = "active"
            row.approved_at = datetime.now(timezone.utc)
            session.commit()

            client_email = row.client_email
            client_name  = row.client_name

        # Manda email al cliente con la chiave
        _email_to_client_approved(row, raw_key, SERVICE_NAME, SERVICE_URL)

        return {
            "key_id":       key_id,
            "client_name":  client_name,
            "client_email": client_email,
            "status":       "active",
            "approved_at":  row.approved_at.isoformat(),
            "message":      f"Chiave generata e inviata a {client_email}",
            "note":         "La chiave raw non è mai recuperabile — solo il cliente la conosce",
        }

    @app.delete("/admin/keys/{key_id}", summary="Revoca chiave o rifiuta richiesta")
    def revoke_key(key_id: str, notify_client: bool = True):
        """
        Revoca una chiave attiva o rifiuta una richiesta pending.
        notify_client=true (default) → manda email al cliente.
        """
        SessionLocal = get_session_local()
        with SessionLocal() as session:
            row = session.query(ApiKey).filter(ApiKey.key_id == key_id).first()
            if not row:
                raise HTTPException(404, {"error": f"Key '{key_id}' not found"})
            if row.status == "revoked":
                raise HTTPException(400, {"error": f"Key '{key_id}' already revoked"})

            old_status     = row.status
            row.status     = "revoked"
            row.revoked_at = datetime.now(timezone.utc)
            session.commit()

        if notify_client and old_status == "active":
            _email_to_client_revoked(row, SERVICE_NAME)

        return {
            "key_id":      key_id,
            "client_name": row.client_name,
            "old_status":  old_status,
            "status":      "revoked",
            "revoked_at":  row.revoked_at.isoformat(),
            "client_notified": notify_client and old_status == "active",
        }

    @app.post("/admin/keys/{key_id}/reactivate", summary="Riattiva chiave revocata")
    def reactivate_key(key_id: str):
        """
        Riattiva una chiave revocata rigenerando una nuova chiave.
        (La vecchia chiave è irrecuperabile — viene creata una nuova.)
        """
        SessionLocal = get_session_local()
        with SessionLocal() as session:
            row = session.query(ApiKey).filter(ApiKey.key_id == key_id).first()
            if not row:
                raise HTTPException(404, {"error": f"Key '{key_id}' not found"})
            if row.status == "active":
                raise HTTPException(400, {"error": f"Key '{key_id}' is already active"})

            raw_key       = secrets.token_urlsafe(32)
            row.key_hash  = _hash_key(raw_key)
            row.status    = "active"
            row.revoked_at = None
            row.approved_at = datetime.now(timezone.utc)
            session.commit()

        _email_to_client_approved(row, raw_key, SERVICE_NAME, SERVICE_URL)

        return {
            "key_id":       key_id,
            "client_name":  row.client_name,
            "client_email": row.client_email,
            "status":       "active",
            "message":      f"Nuova chiave generata e inviata a {row.client_email}",
        }

    log.info(
        f"API key system ready — "
        f"auto_approve={AUTO_APPROVE}, "
        f"admin={'configured' if ADMIN_KEY else 'NOT SET'}, "
        f"service={SERVICE_URL}"
    )