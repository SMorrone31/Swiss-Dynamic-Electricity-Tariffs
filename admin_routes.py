"""
admin_routes.py
===============
Tutti gli endpoint per il sistema API key management.
Da importare e registrare in main.py.

Include:
  - Middleware aggiornato (sostituisce VALID_API_KEYS statico)
  - POST /api/v1/register
  - POST /api/v1/validate-key
  - GET  /admin
  - POST /admin/login
  - POST /admin/logout
  - GET  /admin/setup-totp  (solo prima configurazione)
  - GET  /api/v1/admin/keys
  - GET  /api/v1/admin/stats
  - POST /api/v1/admin/keys/{id}/approve
  - POST /api/v1/admin/keys/{id}/reject
  - POST /api/v1/admin/keys/{id}/suspend
  - POST /api/v1/admin/keys/{id}/reactivate
  - POST /api/v1/admin/keys/{id}/revoke
  - PATCH /api/v1/admin/keys/{id}
  - DELETE /api/v1/admin/keys/{id}
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, EmailStr, field_validator

# ── Percorsi pubblici (no API key richiesta) ──────────────────────────────────

PUBLIC_PATHS = {
    "/",
    "/api/v1/health",
    "/api/v1/zip-map",
    "/api/v1/municipalities",
    "/api/v1/register",
    "/api/v1/validate-key",
    "/api/v1/check-key-status",
    "/api/v1/check-email",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/admin",
    "/admin/login",
    "/admin/logout",
    "/admin/setup-totp",
}
 
# Endpoint dati: accessibili senza key MA se la key è presente viene verificata e contata
DATA_PATHS = {
    "/api/v1/tariffs",
    "/api/v1/smart",
    "/api/v1/summary/daily",
    "/api/v1/summary/monthly",
    "/api/v1/latest",
    "/api/v1/metrics",
    "/api/v1/prices/all"
    "api/v1/prices"
}

DATA_PREFIXES = (
    "/api/v1/prices",   # /api/v1/prices, /api/v1/prices/today, /api/v1/prices/all
)
 
# Prefissi pubblici (es. /admin/... gestito internamente)
PUBLIC_PREFIXES = ("/admin",)


def is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path.startswith("/admin"):
        return True
    if path.startswith("/api/v1/admin/"):
        return True
    # Endpoint dati — pubblici (protetti solo dalla logica business, non da API key)
    if path.startswith("/api/v1/summary/"):
        return True
    if path.startswith("/api/v1/latest"):
        return True
    if path.startswith("/api/v1/health"):
        return True
    return False
  
 
def is_data_path(path: str) -> bool:
    """Endpoint dati: semi-pubblici. Se key presente viene contata, altrimenti passa."""
    if path in DATA_PATHS:
        return True
    for prefix in DATA_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


# ── Middleware ────────────────────────────────────────────────────────────────

async def api_key_middleware(request: Request, call_next):
    """
    Middleware aggiornato — verifica le API key dal DB invece di .env statico.
    L'ADMIN_KEY bypassa tutto (per uso interno/debug).
    """
    path = request.url.path
 
    # Percorsi sempre pubblici
    if is_public(path):
        return await call_next(request)
 
    raw_key = request.headers.get("X-API-Key", "").strip()
 
    # ADMIN_KEY bypassa tutto
    admin_key = os.getenv("ADMIN_KEY", "")
    if admin_key and raw_key == admin_key:
        return await call_next(request)
 
    # Endpoint dati semi-pubblici: se no key passa, se key presente verifica e conta
    if is_data_path(path):
        if not raw_key:
            return await call_next(request)  # nessuna key → accesso libero (per ora)
        # Key presente → verifica e conta
        from database import init_db
        from api_keys.registration import verify_api_key
        SessionLocal = init_db()
        with SessionLocal() as session:
            record, outcome = verify_api_key(session, raw_key)
        if outcome == "ok":
            return await call_next(request)
        if outcome == "rate_limit":
            return JSONResponse(
                {"error": "Rate limit exceeded", "limit": record.rate_limit_day if record else 500,
                 "used": record.requests_today if record else 0, "resets": "midnight CET/CEST"},
                status_code=429
            )
        if outcome == "suspended":
            return JSONResponse({"error": "API key suspended"}, status_code=403)
        if outcome in ("revoked", "not_found"):
            return JSONResponse({"error": "Invalid or revoked API key"}, status_code=401)
        return await call_next(request)
 
    if not raw_key:
        return JSONResponse(
            {"error": "Missing API key", "hint": "Add X-API-Key header"},
            status_code=401
        )
 
    # Verifica nel DB
    from database import init_db
    from api_keys.registration import verify_api_key
 
    SessionLocal = init_db()
    with SessionLocal() as session:
        record, outcome = verify_api_key(session, raw_key)
 
    if outcome == "ok":
        return await call_next(request)
 
    if outcome == "rate_limit":
        return JSONResponse(
            {
                "error":    "Rate limit exceeded",
                "limit":    record.rate_limit_day if record else 500,
                "used":     record.requests_today if record else 0,
                "resets":   "midnight CET/CEST",
                "hint":     "Contact us to upgrade your plan",
            },
            status_code=429
        )
 
    if outcome == "suspended":
        return JSONResponse(
            {"error": "API key suspended", "hint": "Contact support"},
            status_code=403
        )
 
    if outcome in ("revoked", "not_found"):
        return JSONResponse(
            {"error": "Invalid or revoked API key"},
            status_code=401
        )
 
    return JSONResponse({"error": "Unauthorized"}, status_code=401)
 

# ── Pydantic models ───────────────────────────────────────────────────────────

class RegistrationRequest(BaseModel):
    full_name:      str
    email:          str
    country:        str
    lang:           str = "en"
    company:        Optional[str] = None
    vat_number:     Optional[str] = None
    force_replace:  bool = False

    @field_validator("full_name")
    @classmethod
    def name_not_empty(cls, v):
        if not v.strip():
            raise ValueError("Full name is required")
        return v.strip()

    @field_validator("email")
    @classmethod
    def email_valid(cls, v):
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Invalid email address")
        return v

    @field_validator("country")
    @classmethod
    def country_not_empty(cls, v):
        if not v.strip():
            raise ValueError("Country is required")
        return v.strip().upper()

    @field_validator("lang")
    @classmethod
    def lang_valid(cls, v):
        if v not in ("en", "de", "fr", "it"):
            return "en"
        return v


class ValidateKeyRequest(BaseModel):
    api_key: str


class AdminLoginRequest(BaseModel):
    admin_key:  str
    totp_code:  str


class ApproveRequest(BaseModel):
    pass  # nessun body necessario


class RejectRequest(BaseModel):
    reason: str = ""


class SuspendRequest(BaseModel):
    reason: str = ""


class UpdateKeyRequest(BaseModel):
    rate_limit_day: Optional[int] = None
    notes:          Optional[str] = None


# ── Helper: require admin session ─────────────────────────────────────────────

def _require_admin(request: Request, admin_session: Optional[str] = Cookie(default=None)):
    from api_keys.admin_auth import validate_session, get_client_ip
    ip = get_client_ip(request)
    if not validate_session(admin_session, ip):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return True


# ── Funzione per registrare tutte le route su un'app FastAPI ─────────────────

def register_routes(app):
    """
    Registra tutti gli endpoint admin e di registrazione sull'app FastAPI.
    Chiamare in main.py dopo la creazione dell'app:
        from admin_routes import register_routes, api_key_middleware
        app.middleware("http")(api_key_middleware)
        register_routes(app)
    """

    # ── POST /api/v1/register ─────────────────────────────────────────────────
    @app.post("/api/v1/register")
    async def register(req: RegistrationRequest):
        """Registra una nuova richiesta di API key."""
        from database import init_db
        from api_keys.registration import register_new_key
        from api_keys.email_templates import (
            send_registration_confirmation,
            send_admin_new_registration,
            send_new_key_requested,
        )

        SessionLocal = init_db()
        try:
            with SessionLocal() as session:
                record, msg = register_new_key(
                    session        = session,
                    full_name      = req.full_name,
                    email          = req.email,
                    country        = req.country,
                    preferred_lang = req.lang,
                    company        = req.company,
                    vat_number     = req.vat_number,
                    _force_replace = req.force_replace,
                )

            # Email all'utente
            if msg == "replacement_pending":
                send_new_key_requested(req.email, req.full_name, req.lang)
            else:
                send_registration_confirmation(req.email, req.full_name, req.lang)

            # Email admin
            send_admin_new_registration(record)

            return {
                "status":  "pending",
                "message": "Registration received. You will receive your API key by email once approved.",
                "replaced": msg == "replacement_pending",
            }

        except ValueError as e:
            err = str(e)
            if err == "pending_exists":
                raise HTTPException(
                    409,
                    "You already have a pending registration. Please wait for approval."
                )
            if err == "active_key_exists":
                raise HTTPException(
                    409,
                    "active_key_exists"
                )
            raise HTTPException(400, str(e))

    # ── POST /api/v1/validate-key ─────────────────────────────────────────────
    @app.post("/api/v1/validate-key")
    async def validate_key(req: ValidateKeyRequest):
        """
        Valida una API key e ritorna un JWT temporaneo per il frontend.
        Non incrementa il contatore richieste (è solo una validazione di login).
        """
        import secrets as _secrets
        import hashlib as _hashlib
        from database import init_db
        from api_keys.registration import ApiKey, _hash_key

        if not req.api_key.strip():
            raise HTTPException(401, "Missing API key")

        SessionLocal = init_db()
        with SessionLocal() as session:
            key_hash = _hash_key(req.api_key.strip())
            record   = session.query(ApiKey).filter(
                ApiKey.api_key_hash == key_hash
            ).first()

            if not record:
                raise HTTPException(401, "Invalid or inactive API key")

            if record.status == "suspended":
                raise HTTPException(403, "suspended")

            if record.status == "revoked":
                raise HTTPException(403, "revoked")

            if record.status != "active":
                raise HTTPException(401, "Invalid or inactive API key")

            # Genera un session token temporaneo (non incrementa requests_today)
            session_token = _secrets.token_urlsafe(32)

            return {
                "valid":          True,
                "session_token":  session_token,
                "user": {
                    "full_name":        record.full_name,
                    "email":            record.email,
                    "company":          record.company,
                    "key_prefix":       record.key_prefix,
                    "requests_today":   record.requests_today,
                    "rate_limit_day":   record.rate_limit_day,
                    "requests_total":   record.requests_total,
                },
            }


    # ── GET /api/v1/check-email ───────────────────────────────────────────────
    @app.get("/api/v1/check-email")
    async def check_email(email: str):
        """
        Controlla se una email ha già una key attiva o pending.
        Usato dal frontend per mostrare il popup di conferma sostituzione.
        Returns: {status: "none"|"active"|"pending"|"suspended"}
        """
        from database import init_db
        from api_keys.registration import ApiKey

        SessionLocal = init_db()
        with SessionLocal() as session:
            existing = session.query(ApiKey).filter(
                ApiKey.email == email.lower().strip()
            ).all()

        if not existing:
            return {"status": "none"}

        statuses = {k.status for k in existing}
        if "active" in statuses:
            return {"status": "active"}
        if "pending" in statuses:
            return {"status": "pending"}
        if "suspended" in statuses:
            return {"status": "suspended"}
        return {"status": "none"}

    # ── POST /api/v1/check-key-status ─────────────────────────────────────────
    @app.post("/api/v1/check-key-status")
    async def check_key_status(req: ValidateKeyRequest):
        """
        Polling leggero per verificare se una key è ancora valida.
        Usato dal frontend ogni 60s per rilevare sospensioni/revoche in tempo reale.
        NON incrementa il contatore richieste.
        Returns: {valid: bool, status: "active"|"suspended"|"revoked"|"not_found",
                  requests_today: int, rate_limit_day: int}
        """
        from database import init_db
        from api_keys.registration import ApiKey, _hash_key

        if not req.api_key.strip():
            return {"valid": False, "status": "not_found"}

        SessionLocal = init_db()
        with SessionLocal() as session:
            key_hash = _hash_key(req.api_key.strip())
            record   = session.query(ApiKey).filter(
                ApiKey.api_key_hash == key_hash
            ).first()

            if not record:
                return {"valid": False, "status": "not_found"}

            return {
                "valid":          record.status == "active",
                "status":         record.status,
                "requests_today": record.requests_today,
                "rate_limit_day": record.rate_limit_day,
                "requests_total": record.requests_total,
            }

    # ── GET /admin ────────────────────────────────────────────────────────────
    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page(
        request: Request,
        admin_session: Optional[str] = Cookie(default=None),
    ):
        """Pagina admin. Se non autenticato mostra il form di login."""
        from api_keys.admin_auth import validate_session, get_client_ip
        ip = get_client_ip(request)

        if validate_session(admin_session, ip):
            return HTMLResponse(_admin_dashboard_html())
        else:
            return HTMLResponse(_admin_login_html())

    # ── POST /admin/login ─────────────────────────────────────────────────────
    @app.post("/admin/login")
    async def admin_login_endpoint(
        req:      AdminLoginRequest,
        request:  Request,
        response: Response,
    ):
        from api_keys.admin_auth import admin_login, create_session, set_session_cookie, get_client_ip
        ip      = get_client_ip(request)
        ok, err = admin_login(req.admin_key, req.totp_code, ip)

        if not ok:
            raise HTTPException(401, err)

        token = create_session(ip)
        set_session_cookie(response, token)
        return {"status": "ok"}

    # ── POST /admin/logout ────────────────────────────────────────────────────
    @app.post("/admin/logout")
    async def admin_logout(
        response:      Response,
        admin_session: Optional[str] = Cookie(default=None),
    ):
        from api_keys.admin_auth import destroy_session, clear_session_cookie
        destroy_session(admin_session)
        clear_session_cookie(response)
        return {"status": "ok"}

    # ── GET /admin/setup-totp ─────────────────────────────────────────────────
    @app.get("/admin/setup-totp")
    async def setup_totp():
        """Mostra il TOTP secret corrente (solo in dev, disabilita in prod)."""
        if os.getenv("ENVIRONMENT", "development") != "development":
            raise HTTPException(404, "Not found")
        from api_keys.admin_auth import get_totp_secret
        import pyotp
        secret = get_totp_secret()
        uri = pyotp.TOTP(secret).provisioning_uri(
            name=os.getenv("ADMIN_EMAIL", "admin"),
            issuer_name="Swiss Tariff Hub",
        )
        return {"secret": secret, "uri": uri}

    # ── GET /api/v1/admin/stats ───────────────────────────────────────────────
    @app.get("/api/v1/admin/stats")
    async def admin_stats(request: Request, admin_session: Optional[str] = Cookie(default=None)):
        _require_admin(request, admin_session)
        from database import init_db
        from api_keys.registration import get_stats
        SessionLocal = init_db()
        with SessionLocal() as session:
            return get_stats(session)

    # ── GET /api/v1/admin/keys ────────────────────────────────────────────────
    @app.get("/api/v1/admin/keys")
    async def admin_list_keys(
        request:       Request,
        status:        Optional[str] = None,
        admin_session: Optional[str] = Cookie(default=None),
    ):
        _require_admin(request, admin_session)
        from database import init_db
        from api_keys.registration import get_all_keys
        SessionLocal = init_db()
        with SessionLocal() as session:
            keys = get_all_keys(session, status=status)
            return [_key_to_dict(k) for k in keys]

    # ── POST /api/v1/admin/keys/{id}/approve ─────────────────────────────────
    @app.post("/api/v1/admin/keys/{key_id}/approve")
    async def admin_approve(
        key_id:        str,
        request:       Request,
        admin_session: Optional[str] = Cookie(default=None),
    ):
        _require_admin(request, admin_session)
        from database import init_db
        from api_keys.registration import approve_key
        from api_keys.email_templates import send_key_approved

        SessionLocal = init_db()
        try:
            with SessionLocal() as session:
                record, raw_key = approve_key(session, key_id)

            # Invia la key via email UNA SOLA VOLTA
            send_key_approved(
                email       = record.email,
                full_name   = record.full_name,
                raw_key     = raw_key,
                rate_limit  = record.rate_limit_day,
                lang        = record.preferred_lang,
                company     = record.company,
                approved_at = record.approved_at,
                key_prefix  = record.key_prefix,
            )
            return {"status": "approved", "key_prefix": record.key_prefix}

        except ValueError as e:
            raise HTTPException(400, str(e))

    # ── POST /api/v1/admin/keys/{id}/reject ──────────────────────────────────
    @app.post("/api/v1/admin/keys/{key_id}/reject")
    async def admin_reject(
        key_id:        str,
        req:           RejectRequest,
        request:       Request,
        admin_session: Optional[str] = Cookie(default=None),
    ):
        _require_admin(request, admin_session)
        from database import init_db
        from api_keys.registration import reject_key
        from api_keys.email_templates import send_registration_rejected

        SessionLocal = init_db()
        try:
            with SessionLocal() as session:
                email, full_name, lang = reject_key(session, key_id, req.reason)

            send_registration_rejected(email, full_name, req.reason, lang)
            return {"status": "rejected"}

        except ValueError as e:
            raise HTTPException(400, str(e))

    # ── POST /api/v1/admin/keys/{id}/suspend ─────────────────────────────────
    @app.post("/api/v1/admin/keys/{key_id}/suspend")
    async def admin_suspend(
        key_id:        str,
        req:           SuspendRequest,
        request:       Request,
        admin_session: Optional[str] = Cookie(default=None),
    ):
        _require_admin(request, admin_session)
        from database import init_db
        from api_keys.registration import suspend_key
        from api_keys.email_templates import send_key_suspended

        SessionLocal = init_db()
        try:
            with SessionLocal() as session:
                record = suspend_key(session, key_id, req.reason)

            send_key_suspended(record.email, record.full_name, req.reason, record.preferred_lang)
            return {"status": "suspended"}

        except ValueError as e:
            raise HTTPException(400, str(e))

    # ── POST /api/v1/admin/keys/{id}/reactivate ───────────────────────────────
    @app.post("/api/v1/admin/keys/{key_id}/reactivate")
    async def admin_reactivate(
        key_id:        str,
        request:       Request,
        admin_session: Optional[str] = Cookie(default=None),
    ):
        _require_admin(request, admin_session)
        from database import init_db
        from api_keys.registration import reactivate_key
        from api_keys.email_templates import send_key_reactivated

        SessionLocal = init_db()
        try:
            with SessionLocal() as session:
                record = reactivate_key(session, key_id)

            send_key_reactivated(record.email, record.full_name, record.preferred_lang)
            return {"status": "active"}

        except ValueError as e:
            raise HTTPException(400, str(e))

    # ── POST /api/v1/admin/keys/{id}/revoke ──────────────────────────────────
    @app.post("/api/v1/admin/keys/{key_id}/revoke")
    async def admin_revoke(
        key_id:        str,
        request:       Request,
        admin_session: Optional[str] = Cookie(default=None),
    ):
        _require_admin(request, admin_session)
        from database import init_db
        from api_keys.registration import revoke_key

        SessionLocal = init_db()
        try:
            with SessionLocal() as session:
                revoke_key(session, key_id)
            return {"status": "revoked"}

        except ValueError as e:
            raise HTTPException(400, str(e))

    # ── PATCH /api/v1/admin/keys/{id} ─────────────────────────────────────────
    @app.patch("/api/v1/admin/keys/{key_id}")
    async def admin_update(
        key_id:        str,
        req:           UpdateKeyRequest,
        request:       Request,
        admin_session: Optional[str] = Cookie(default=None),
    ):
        _require_admin(request, admin_session)
        from database import init_db
        from api_keys.registration import update_key

        SessionLocal = init_db()
        try:
            with SessionLocal() as session:
                record = update_key(session, key_id, req.rate_limit_day, req.notes)
            return _key_to_dict(record)

        except ValueError as e:
            raise HTTPException(400, str(e))

    # ── DELETE /api/v1/admin/keys/{id} ───────────────────────────────────────
    @app.delete("/api/v1/admin/keys/{key_id}")
    async def admin_delete(
        key_id:        str,
        request:       Request,
        admin_session: Optional[str] = Cookie(default=None),
    ):
        _require_admin(request, admin_session)
        from database import init_db
        from api_keys.registration import ApiKey

        SessionLocal = init_db()
        with SessionLocal() as session:
            record = session.get(ApiKey, key_id)
            if not record:
                raise HTTPException(404, "Key not found")
            if record.status == "active":
                raise HTTPException(400, "Cannot delete an active key. Revoke it first.")
            session.delete(record)
            session.commit()
        return {"status": "deleted"}


# ── Helper: serializza ApiKey ─────────────────────────────────────────────────

def _key_to_dict(k) -> dict:
    return {
        "id":               k.id,
        "full_name":        k.full_name,
        "email":            k.email,
        "company":          k.company,
        "vat_number":       k.vat_number,
        "country":          k.country,
        "key_prefix":       k.key_prefix,
        "status":           k.status,
        "rate_limit_day":   k.rate_limit_day,
        "requests_today":   k.requests_today,
        "requests_total":   k.requests_total,
        "last_used_at":     k.last_used_at.isoformat() if k.last_used_at else None,
        "created_at":       k.created_at.isoformat() if k.created_at else None,
        "approved_at":      k.approved_at.isoformat() if k.approved_at else None,
        "revoked_at":       k.revoked_at.isoformat() if k.revoked_at else None,
        "suspended_at":     k.suspended_at.isoformat() if k.suspended_at else None,
        "notes":            k.notes,
        "preferred_lang":   k.preferred_lang,
        "warning_80_sent":  k.warning_80_sent,
        "warning_100_sent": k.warning_100_sent,
    }


# ── Admin HTML: Login page ────────────────────────────────────────────────────

def _admin_login_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin — Swiss Tariff Hub</title>
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#0f1117;min-height:100vh;
       display:flex;align-items:center;justify-content:center}
  .card{background:#1a1a2e;border:0.5px solid #2a2a45;border-radius:16px;
        padding:40px;width:100%;max-width:400px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
  .logo{font-size:12px;font-weight:600;color:#185fa5;text-transform:uppercase;
        letter-spacing:.1em;margin-bottom:8px}
  h1{font-size:20px;font-weight:600;color:white;margin-bottom:4px}
  .sub{font-size:12px;color:#666;margin-bottom:32px}
  label{display:block;font-size:11px;color:#888;text-transform:uppercase;
        letter-spacing:.05em;margin-bottom:6px;margin-top:18px}
  input{width:100%;background:#0f1117;border:0.5px solid #2a2a45;border-radius:8px;
        padding:10px 14px;font-size:14px;color:white;outline:none;transition:border .15s}
  input:focus{border-color:#185fa5}
  input::placeholder{color:#444}
  .totp-hint{font-size:11px;color:#555;margin-top:6px}
  button{width:100%;margin-top:28px;background:#185fa5;color:white;border:none;
         border-radius:8px;padding:12px;font-size:14px;font-weight:600;
         cursor:pointer;transition:background .15s;letter-spacing:.02em}
  button:hover{background:#1470c0}
  button:disabled{background:#333;cursor:not-allowed}
  .error{background:#2d0f0f;border:0.5px solid #5c1a1a;border-radius:8px;
         padding:10px 14px;font-size:12px;color:#e24b4a;margin-top:16px;display:none}
  .shield{font-size:32px;margin-bottom:20px;display:block}
  .dots{display:flex;gap:4px;justify-content:center;margin-top:8px}
  .dot{width:8px;height:8px;border-radius:50%;background:#185fa5;
       animation:bounce .6s infinite alternate}
  .dot:nth-child(2){animation-delay:.15s}
  .dot:nth-child(3){animation-delay:.3s}
  @keyframes bounce{to{transform:translateY(-6px);opacity:.4}}
</style>
</head>
<body>
<div class="card">
  <span class="shield">🛡️</span>
  <div class="logo">Swiss Tariff Hub</div>
  <h1>Admin Access</h1>
  <p class="sub">Two-factor authentication required</p>

  <label>Admin Password</label>
  <input type="password" id="admin-key" placeholder="Enter admin password" autocomplete="off">

  <label>Authenticator Code</label>
  <input type="text" id="totp-code" placeholder="000000" maxlength="6"
         inputmode="numeric" pattern="[0-9]{6}" autocomplete="one-time-code">
  <div class="totp-hint">6-digit code from Google Authenticator (refreshes every 30s)</div>

  <button id="login-btn" onclick="doLogin()">Sign In</button>
  <div class="error" id="error-msg"></div>

  <div id="loading" style="display:none;text-align:center;margin-top:20px">
    <div class="dots"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>
  </div>
</div>

<script>
document.getElementById('totp-code').addEventListener('keydown', e => {
  if (e.key === 'Enter') doLogin();
});
document.getElementById('admin-key').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('totp-code').focus();
});

async function doLogin() {
  const key  = document.getElementById('admin-key').value.trim();
  const totp = document.getElementById('totp-code').value.trim();
  const err  = document.getElementById('error-msg');
  const btn  = document.getElementById('login-btn');
  const load = document.getElementById('loading');

  err.style.display  = 'none';
  btn.disabled       = true;
  load.style.display = 'block';

  try {
    const r = await fetch('/admin/login', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({admin_key: key, totp_code: totp}),
    });

    if (r.ok) {
      window.location.reload();
    } else {
      const data = await r.json();
      err.textContent    = data.detail || 'Authentication failed';
      err.style.display  = 'block';
      document.getElementById('totp-code').value = '';
      document.getElementById('totp-code').focus();
    }
  } catch(e) {
    err.textContent   = 'Network error — try again';
    err.style.display = 'block';
  } finally {
    btn.disabled       = false;
    load.style.display = 'none';
  }
}
</script>
</body>
</html>"""


# ── Admin HTML: Dashboard ─────────────────────────────────────────────────────

def _admin_dashboard_html() -> str:
    app_url = os.getenv("APP_URL", "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin Dashboard — Swiss Tariff Hub</title>
<style>
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:system-ui,sans-serif;background:#0f1117;color:#e0e0e0;min-height:100vh}}
  header{{background:#1a1a2e;border-bottom:0.5px solid #2a2a45;padding:14px 28px;
          display:flex;align-items:center;gap:12px}}
  header h1{{font-size:14px;font-weight:600;color:white;flex:1}}
  .tag{{font-size:10px;background:#185fa5;color:white;padding:2px 8px;border-radius:999px;font-weight:600}}
  .logout-btn{{font-size:11px;padding:5px 14px;border-radius:6px;border:0.5px solid #444;
               background:transparent;color:#888;cursor:pointer;transition:all .15s}}
  .logout-btn:hover{{border-color:#e24b4a;color:#e24b4a}}
  main{{max-width:1400px;margin:0 auto;padding:24px 28px}}

  /* Stats row */
  .stats{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:28px}}
  .stat{{background:#1a1a2e;border:0.5px solid #2a2a45;border-radius:10px;padding:16px 18px}}
  .stat-val{{font-size:28px;font-weight:600;line-height:1}}
  .stat-lbl{{font-size:11px;color:#666;margin-top:4px;text-transform:uppercase;letter-spacing:.04em}}
  .stat-val.green{{color:#1d9e75}} .stat-val.yellow{{color:#ba7517}}
  .stat-val.red{{color:#e24b4a}}   .stat-val.blue{{color:#185fa5}}

  /* Tabs */
  .tabs{{display:flex;gap:0;border-bottom:0.5px solid #2a2a45;margin-bottom:20px}}
  .tab-btn{{font-size:12px;font-weight:500;padding:10px 20px;border:none;background:transparent;
            color:#666;cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}}
  .tab-btn.active{{color:white;border-bottom-color:#185fa5}}
  .tab-btn .badge{{background:#e24b4a;color:white;font-size:9px;padding:1px 5px;
                   border-radius:999px;margin-left:5px;font-weight:700}}

  /* Table */
  .table-wrap{{overflow-x:auto}}
  table{{width:100%;border-collapse:collapse;font-size:12px}}
  th{{text-align:left;color:#555;font-weight:500;padding:8px 12px;
      border-bottom:0.5px solid #2a2a45;white-space:nowrap;text-transform:uppercase;
      letter-spacing:.04em;font-size:10px}}
  td{{padding:10px 12px;border-bottom:0.5px solid #1a1a2e;vertical-align:middle}}
  tr:hover td{{background:#1a1a2e}}

  /* Status badges */
  .s-active{{background:#0d2e1e;color:#1d9e75;font-size:10px;padding:2px 8px;border-radius:999px;font-weight:600}}
  .s-pending{{background:#2a1f08;color:#ba7517;font-size:10px;padding:2px 8px;border-radius:999px;font-weight:600}}
  .s-suspended{{background:#1f1008;color:#e24b4a;font-size:10px;padding:2px 8px;border-radius:999px;font-weight:600}}
  .s-revoked{{background:#1a0808;color:#7a2222;font-size:10px;padding:2px 8px;border-radius:999px;font-weight:600}}
  .s-superseded{{background:#111;color:#444;font-size:10px;padding:2px 8px;border-radius:999px;font-weight:600}}

  /* Progress bar */
  .rbar{{height:10px;background:#0d0d1a;border:1px solid #333366;border-radius:5px;width:110px;overflow:hidden;display:inline-block;vertical-align:middle;margin-left:8px}}
  .rbar-fill{{display:block;height:100%;border-radius:5px;background:#22c55e;transition:width .4s}}
  .rbar-fill.warn{{background:#f59e0b}}
  .rbar-fill.full{{background:#e24b4a}}

  /* Buttons */
  .btn-sm{{font-size:10px;padding:3px 10px;border-radius:5px;border:0.5px solid;
           cursor:pointer;transition:all .15s;white-space:nowrap;font-weight:500}}
  .btn-approve{{border-color:#1d9e75;color:#1d9e75;background:transparent}}
  .btn-approve:hover{{background:#1d9e75;color:white}}
  .btn-reject{{border-color:#e24b4a;color:#e24b4a;background:transparent}}
  .btn-reject:hover{{background:#e24b4a;color:white}}
  .btn-suspend{{border-color:#ba7517;color:#ba7517;background:transparent}}
  .btn-suspend:hover{{background:#ba7517;color:white}}
  .btn-reactivate{{border-color:#1d9e75;color:#1d9e75;background:transparent}}
  .btn-reactivate:hover{{background:#1d9e75;color:white}}
  .btn-revoke{{border-color:#e24b4a;color:#e24b4a;background:transparent}}
  .btn-revoke:hover{{background:#e24b4a;color:white}}
  .btn-edit{{border-color:#555;color:#888;background:transparent}}
  .btn-edit:hover{{border-color:#185fa5;color:#185fa5}}
  .btn-delete{{border-color:#333;color:#555;background:transparent}}
  .btn-delete:hover{{border-color:#e24b4a;color:#e24b4a}}
  .actions{{display:flex;gap:5px;flex-wrap:wrap}}

  /* Modal */
  .modal-bg{{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:100;
             display:none;align-items:center;justify-content:center}}
  .modal-bg.open{{display:flex}}
  .modal{{background:#1a1a2e;border:0.5px solid #2a2a45;border-radius:12px;
          padding:28px;max-width:440px;width:100%}}
  .modal h3{{font-size:15px;font-weight:600;color:white;margin-bottom:12px}}
  .modal p{{font-size:13px;color:#888;margin-bottom:16px;line-height:1.6}}
  .modal input,.modal textarea{{width:100%;background:#0f1117;border:0.5px solid #2a2a45;
    border-radius:8px;padding:8px 12px;font-size:13px;color:white;margin-bottom:14px;
    outline:none;font-family:inherit}}
  .modal textarea{{resize:vertical;min-height:70px}}
  .modal input:focus,.modal textarea:focus{{border-color:#185fa5}}
  .modal-btns{{display:flex;gap:8px;justify-content:flex-end}}
  .modal-btns button{{font-size:12px;padding:7px 18px;border-radius:7px;border:none;cursor:pointer;font-weight:500}}
  .modal-cancel{{background:#2a2a45;color:#aaa}}
  .modal-confirm{{background:#185fa5;color:white}}
  .modal-confirm.danger{{background:#e24b4a}}

  /* Key prefix */
  .key-prefix{{font-family:monospace;font-size:11px;color:#185fa5;
               background:#0a1929;padding:2px 6px;border-radius:4px}}
  .empty{{text-align:center;padding:40px;color:#444;font-size:13px}}

  /* Rate limit input */
  .rl-input{{width:70px;background:#0f1117;border:0.5px solid #2a2a45;border-radius:5px;
             padding:3px 7px;font-size:12px;color:white;outline:none;text-align:right}}
  .rl-input:focus{{border-color:#185fa5}}

  @media(max-width:900px){{.stats{{grid-template-columns:repeat(3,1fr)}}}}
</style>
</head>
<body>
<header>
  <h1>🛡️ Swiss Tariff Hub — Admin</h1>
  <span class="tag">ADMIN</span>
  <button class="logout-btn" onclick="doLogout()">Logout</button>
</header>
<main>

  <!-- Stats -->
  <div class="stats" id="stats-row">
    <div class="stat"><div class="stat-val blue" id="s-active">—</div><div class="stat-lbl">Active keys</div></div>
    <div class="stat"><div class="stat-val yellow" id="s-pending">—</div><div class="stat-lbl">Pending approval</div></div>
    <div class="stat"><div class="stat-val red" id="s-suspended">—</div><div class="stat-lbl">Suspended</div></div>
    <div class="stat"><div class="stat-val" id="s-today">—</div><div class="stat-lbl">Requests today</div></div>
    <div class="stat"><div class="stat-val" id="s-total">—</div><div class="stat-lbl">Requests total</div></div>
  </div>

  <!-- Tabs -->
  <div class="tabs">
    <button class="tab-btn active" id="tab-pending" onclick="switchTab('pending')">
      Pending <span class="badge" id="pending-badge" style="display:none">0</span>
    </button>
    <button class="tab-btn" id="tab-active"    onclick="switchTab('active')">Active</button>
    <button class="tab-btn" id="tab-suspended" onclick="switchTab('suspended')">Suspended</button>
    <button class="tab-btn" id="tab-revoked"   onclick="switchTab('revoked')">Revoked</button>
    <button class="tab-btn" id="tab-all"       onclick="switchTab('all')">All</button>
  </div>

  <!-- Table -->
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Name / Company</th>
          <th>Email</th>
          <th>Country</th>
          <th>Key</th>
          <th>Status</th>
          <th>Usage today</th>
          <th>Total</th>
          <th>Last used</th>
          <th>Registered</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="keys-table"></tbody>
    </table>
    <div class="empty" id="empty-msg" style="display:none">No entries found</div>
  </div>

</main>

<!-- Modal generico -->
<div class="modal-bg" id="modal">
  <div class="modal">
    <h3 id="modal-title">Confirm</h3>
    <p id="modal-desc"></p>
    <input type="text" id="modal-input" placeholder="" style="display:none">
    <textarea id="modal-textarea" placeholder="Reason (optional)" style="display:none"></textarea>
    <div class="modal-btns">
      <button class="modal-cancel" onclick="closeModal()">Cancel</button>
      <button class="modal-confirm" id="modal-confirm-btn" onclick="confirmModal()">Confirm</button>
    </div>
  </div>
</div>

<script>
let currentTab = 'pending';
let allKeys    = [];
let modalAction = null;
let modalKeyId  = null;

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {{
  await Promise.all([loadStats(), loadKeys('pending')]);
  setInterval(loadStats, 30000);
}}

// ── Stats ─────────────────────────────────────────────────────────────────────
async function loadStats() {{
  try {{
    const r = await fetch('/api/v1/admin/stats', {{credentials:'include'}});
    if (!r.ok) return;
    const d = await r.json();
    document.getElementById('s-active').textContent    = d.active;
    document.getElementById('s-pending').textContent   = d.pending;
    document.getElementById('s-suspended').textContent = d.suspended;
    document.getElementById('s-today').textContent     = d.today_requests.toLocaleString();
    document.getElementById('s-total').textContent     = d.total_requests.toLocaleString();
    const badge = document.getElementById('pending-badge');
    badge.textContent    = d.pending;
    badge.style.display  = d.pending > 0 ? 'inline' : 'none';
  }} catch(e) {{ console.error(e); }}
}}

// ── Keys table ────────────────────────────────────────────────────────────────
async function loadKeys(status) {{
  const url = status && status !== 'all'
    ? `/api/v1/admin/keys?status=${{status}}`
    : '/api/v1/admin/keys';
  try {{
    const r = await fetch(url, {{credentials:'include'}});
    if (!r.ok) {{ window.location.href = '/admin'; return; }}
    allKeys = await r.json();
    renderTable(allKeys);
  }} catch(e) {{ console.error(e); }}
}}

function renderTable(keys) {{
  const tbody = document.getElementById('keys-table');
  const empty = document.getElementById('empty-msg');
  if (!keys.length) {{
    tbody.innerHTML = '';
    empty.style.display = 'block';
    return;
  }}
  empty.style.display = 'none';

  tbody.innerHTML = keys.map(k => {{
    const pct    = k.rate_limit_day > 0 ? k.requests_today / k.rate_limit_day : 0;
    const barCls = pct >= 1 ? 'full' : pct >= 0.8 ? 'warn' : '';
    const sCls   = {{active:'s-active',pending:'s-pending',suspended:'s-suspended',
                     revoked:'s-revoked',superseded:'s-superseded'}}[k.status] || 's-revoked';
    const lastUsed = k.last_used_at
      ? new Date(k.last_used_at).toLocaleString('en-CH', {{dateStyle:'short',timeStyle:'short'}})
      : '—';
    const created = k.created_at
      ? new Date(k.created_at).toLocaleString('en-CH', {{dateStyle:'short',timeStyle:'short'}})
      : '—';
    const company = k.company ? `<br><span style="color:#555;font-size:10px">${{k.company}}</span>` : '';
    const keyDisp = k.key_prefix
      ? `<span class="key-prefix">${{k.key_prefix}}...</span>`
      : '<span style="color:#333;font-size:10px">—</span>';

    const actions = _buildActions(k);

    return `<tr id="row-${{k.id}}">
      <td><strong style="color:white">${{k.full_name}}</strong>${{company}}</td>
      <td style="color:#888">${{k.email}}</td>
      <td style="color:#555">${{k.country}}</td>
      <td>${{keyDisp}}</td>
      <td><span class="${{sCls}}">${{k.status}}</span></td>
      <td>
        ${{k.requests_today}} / ${{k.rate_limit_day}}
        <span class="rbar"><span class="rbar-fill ${{barCls}}" style="width:${{Math.min(100,pct*100)}}%"></span></span>
      </td>
      <td style="color:#888">${{k.requests_total.toLocaleString()}}</td>
      <td style="color:#555;font-size:11px">${{lastUsed}}</td>
      <td style="color:#555;font-size:11px">${{created}}</td>
      <td><div class="actions">${{actions}}</div></td>
    </tr>`;
  }}).join('');
}}

function _buildActions(k) {{
  const id = k.id;
  let btns = '';
  if (k.status === 'pending') {{
    btns += `<button class="btn-sm btn-approve" onclick="doApprove('${{id}}')">Approve</button>`;
    btns += `<button class="btn-sm btn-reject"  onclick="openReject('${{id}}')">Reject</button>`;
  }}
  if (k.status === 'active') {{
    btns += `<button class="btn-sm btn-suspend" onclick="openSuspend('${{id}}')">Suspend</button>`;
    btns += `<button class="btn-sm btn-revoke"  onclick="openRevoke('${{id}}')">Revoke</button>`;
    btns += `<button class="btn-sm btn-edit"    onclick="openEdit('${{id}}')">Edit</button>`;
  }}
  if (k.status === 'suspended') {{
    btns += `<button class="btn-sm btn-reactivate" onclick="doReactivate('${{id}}')">Reactivate</button>`;
    btns += `<button class="btn-sm btn-revoke"     onclick="openRevoke('${{id}}')">Revoke</button>`;
  }}
  if (['revoked','superseded'].includes(k.status)) {{
    btns += `<button class="btn-sm btn-delete" onclick="openDelete('${{id}}')">Delete</button>`;
  }}
  return btns;
}}

// ── Tab switching ──────────────────────────────────────────────────────────────
function switchTab(tab) {{
  currentTab = tab;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  loadKeys(tab === 'all' ? null : tab);
}}

// ── Actions ───────────────────────────────────────────────────────────────────
async function doApprove(id) {{
  if (!confirm('Approve this key? The user will receive their API key by email.')) return;
  const r = await fetch(`/api/v1/admin/keys/${{id}}/approve`, {{method:'POST',credentials:'include'}});
  if (r.ok) {{ showToast('✅ Key approved — email sent'); loadKeys(currentTab); loadStats(); }}
  else {{ const d = await r.json(); showToast('❌ ' + (d.detail||'Error'), true); }}
}}

async function doReactivate(id) {{
  if (!confirm('Reactivate this key?')) return;
  const r = await fetch(`/api/v1/admin/keys/${{id}}/reactivate`, {{method:'POST',credentials:'include'}});
  if (r.ok) {{ showToast('✅ Key reactivated'); loadKeys(currentTab); loadStats(); }}
  else {{ const d = await r.json(); showToast('❌ ' + (d.detail||'Error'), true); }}
}}

function openReject(id) {{
  modalKeyId  = id;
  modalAction = 'reject';
  document.getElementById('modal-title').textContent   = 'Reject Registration';
  document.getElementById('modal-desc').textContent    = 'The user will receive a rejection email.';
  document.getElementById('modal-textarea').style.display = 'block';
  document.getElementById('modal-textarea').value      = '';
  document.getElementById('modal-textarea').placeholder= 'Reason for rejection (will be sent to user)';
  document.getElementById('modal-input').style.display = 'none';
  document.getElementById('modal-confirm-btn').textContent = 'Reject';
  document.getElementById('modal-confirm-btn').className   = 'modal-confirm danger';
  document.getElementById('modal').classList.add('open');
}}

function openSuspend(id) {{
  modalKeyId  = id;
  modalAction = 'suspend';
  document.getElementById('modal-title').textContent   = 'Suspend Key';
  document.getElementById('modal-desc').textContent    = 'The user will receive a suspension email. You can reactivate later.';
  document.getElementById('modal-textarea').style.display = 'block';
  document.getElementById('modal-textarea').value      = '';
  document.getElementById('modal-textarea').placeholder= 'Reason (will be sent to user)';
  document.getElementById('modal-input').style.display = 'none';
  document.getElementById('modal-confirm-btn').textContent = 'Suspend';
  document.getElementById('modal-confirm-btn').className   = 'modal-confirm danger';
  document.getElementById('modal').classList.add('open');
}}

function openRevoke(id) {{
  modalKeyId  = id;
  modalAction = 'revoke';
  document.getElementById('modal-title').textContent   = '⚠ Permanent Revocation';
  document.getElementById('modal-desc').textContent    = 'This action is PERMANENT and cannot be undone. The key hash will be immediately destroyed.';
  document.getElementById('modal-textarea').style.display = 'none';
  document.getElementById('modal-input').style.display    = 'none';
  document.getElementById('modal-confirm-btn').textContent = 'Revoke permanently';
  document.getElementById('modal-confirm-btn').className   = 'modal-confirm danger';
  document.getElementById('modal').classList.add('open');
}}

function openEdit(id) {{
  const key = allKeys.find(k => k.id === id);
  if (!key) return;
  modalKeyId  = id;
  modalAction = 'edit';
  document.getElementById('modal-title').textContent   = 'Edit Key Settings';
  document.getElementById('modal-desc').textContent    = `${{key.full_name}} — ${{key.email}}`;
  document.getElementById('modal-input').style.display    = 'block';
  document.getElementById('modal-input').value            = key.rate_limit_day;
  document.getElementById('modal-input').placeholder      = 'Daily rate limit';
  document.getElementById('modal-input').type             = 'number';
  document.getElementById('modal-textarea').style.display = 'block';
  document.getElementById('modal-textarea').value         = key.notes || '';
  document.getElementById('modal-textarea').placeholder   = 'Internal notes (not sent to user)';
  document.getElementById('modal-confirm-btn').textContent = 'Save';
  document.getElementById('modal-confirm-btn').className   = 'modal-confirm';
  document.getElementById('modal').classList.add('open');
}}

function openDelete(id) {{
  modalKeyId  = id;
  modalAction = 'delete';
  document.getElementById('modal-title').textContent   = 'Delete Record';
  document.getElementById('modal-desc').textContent    = 'Delete this record permanently from the database.';
  document.getElementById('modal-textarea').style.display = 'none';
  document.getElementById('modal-input').style.display    = 'none';
  document.getElementById('modal-confirm-btn').textContent = 'Delete';
  document.getElementById('modal-confirm-btn').className   = 'modal-confirm danger';
  document.getElementById('modal').classList.add('open');
}}

function closeModal() {{
  document.getElementById('modal').classList.remove('open');
  modalAction = null; modalKeyId = null;
}}

async function confirmModal() {{
  const id      = modalKeyId;
  const action  = modalAction;
  const textarea= document.getElementById('modal-textarea').value.trim();
  const input   = document.getElementById('modal-input').value.trim();
  closeModal();

  let r;
  if (action === 'reject') {{
    r = await fetch(`/api/v1/admin/keys/${{id}}/reject`, {{
      method:'POST', credentials:'include',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{reason: textarea}}),
    }});
    if (r.ok) {{ showToast('Registration rejected'); loadKeys(currentTab); loadStats(); }}
  }} else if (action === 'suspend') {{
    r = await fetch(`/api/v1/admin/keys/${{id}}/suspend`, {{
      method:'POST', credentials:'include',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{reason: textarea}}),
    }});
    if (r.ok) {{ showToast('Key suspended'); loadKeys(currentTab); loadStats(); }}
  }} else if (action === 'revoke') {{
    r = await fetch(`/api/v1/admin/keys/${{id}}/revoke`, {{
      method:'POST', credentials:'include',
    }});
    if (r.ok) {{ showToast('Key permanently revoked'); loadKeys(currentTab); loadStats(); }}
  }} else if (action === 'edit') {{
    r = await fetch(`/api/v1/admin/keys/${{id}}`, {{
      method:'PATCH', credentials:'include',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{
        rate_limit_day: parseInt(input)||500,
        notes: textarea || null,
      }}),
    }});
    if (r.ok) {{ showToast('✅ Saved'); loadKeys(currentTab); }}
  }} else if (action === 'delete') {{
    r = await fetch(`/api/v1/admin/keys/${{id}}`, {{
      method:'DELETE', credentials:'include',
    }});
    if (r.ok) {{ showToast('Record deleted'); loadKeys(currentTab); loadStats(); }}
  }}

  if (r && !r.ok) {{
    const d = await r.json().catch(()=>({{}}));
    showToast('❌ ' + (d.detail||'Error'), true);
  }}
}}

// ── Toast ─────────────────────────────────────────────────────────────────────
function showToast(msg, isError=false) {{
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = `position:fixed;bottom:24px;right:24px;padding:10px 20px;
    border-radius:8px;font-size:13px;font-weight:500;z-index:999;
    background:${{isError?'#2d0f0f':'#0d2e1e'}};
    color:${{isError?'#e24b4a':'#1d9e75'}};
    border:0.5px solid ${{isError?'#5c1a1a':'#1d4030'}};
    box-shadow:0 4px 20px rgba(0,0,0,.4);animation:fadeIn .2s`;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}}

// ── Logout ────────────────────────────────────────────────────────────────────
async function doLogout() {{
  await fetch('/admin/logout', {{method:'POST', credentials:'include'}});
  window.location.reload();
}}

// ── Keyboard shortcuts ────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') closeModal();
}});

init();
</script>
</body>
</html>"""