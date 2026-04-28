"""
admin_auth.py
=============
Autenticazione admin con doppio fattore:
  1. ADMIN_KEY (password lunga da .env)
  2. TOTP 6 cifre (Google Authenticator)

Session cookie: HttpOnly, Secure, SameSite=Lax, 8 ore.
Rate limiting: 5 tentativi falliti → lockout 15 min per IP.
Lockout totale: 20 tentativi in 1 ora → alert email.

Le sessioni vengono persistite su file (/tmp/sth_admin_sessions.json)
per sopravvivere ai reload di uvicorn in sviluppo.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import pyotp
from fastapi import Cookie, Request, Response

log = logging.getLogger("admin_auth")


# ── Config ────────────────────────────────────────────────────────────────────

SESSION_TTL_SECONDS  = 8 * 3600
SESSION_IDLE_SECONDS = 3600
LOCKOUT_ATTEMPTS     = 5
LOCKOUT_DURATION     = 15 * 60
TOTAL_LOCKOUT_WINDOW = 3600
TOTAL_LOCKOUT_LIMIT  = 20
COOKIE_NAME          = "admin_session"

# Sessioni persistite su file → sopravvivono al reload tra processi uvicorn
_SESSION_FILE = "/tmp/sth_admin_sessions.json"

# Rate limiting in-memory (va bene per single-host)
_lockouts: dict[str, float] = {}
_attempts: dict[str, list]  = defaultdict(list)


# ── Session persistence ───────────────────────────────────────────────────────

def _load_sessions() -> dict:
    try:
        if os.path.exists(_SESSION_FILE):
            with open(_SESSION_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_sessions(sessions: dict):
    try:
        with open(_SESSION_FILE, "w") as f:
            json.dump(sessions, f)
    except Exception as e:
        log.warning(f"[admin_auth] Impossibile salvare sessioni: {e}")


def _clean_sessions():
    """Rimuovi sessioni scadute dal file."""
    now = time.time()
    sessions = _load_sessions()
    cleaned = {
        token: data for token, data in sessions.items()
        if now - data["last_active"] <= SESSION_IDLE_SECONDS
        and now - data["created_at"] <= SESSION_TTL_SECONDS
    }
    _save_sessions(cleaned)


# ── TOTP setup ────────────────────────────────────────────────────────────────

def get_totp_secret() -> str:
    """
    Ritorna il TOTP secret da .env.
    Se non esiste, ne genera uno nuovo, lo stampa in console con QR code,
    e chiede di aggiungerlo al .env.
    """
    secret = os.getenv("TOTP_SECRET", "")
    if secret:
        return secret

    new_secret  = pyotp.random_base32()
    app_name    = "Swiss Tariff Hub"
    admin_email = os.getenv("ADMIN_EMAIL", "admin@example.com")

    totp_uri = pyotp.totp.TOTP(new_secret).provisioning_uri(
        name=admin_email,
        issuer_name=app_name,
    )

    print("\n" + "=" * 60)
    print("  TOTP SETUP — PRIMA CONFIGURAZIONE")
    print("=" * 60)
    print(f"  Secret: {new_secret}")
    print()
    print("  Aggiungi al .env:")
    print(f"  TOTP_SECRET={new_secret}")
    print()
    print("  QR Code URI (incolla su https://qr.io o usa l'app):")
    print(f"  {totp_uri}")
    print()

    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(totp_uri)
        qr.make(fit=True)
        print("  Scansiona questo QR con Google Authenticator:")
        qr.print_ascii(invert=True)
    except ImportError:
        print("  (installa qrcode per il QR ASCII: pip install qrcode)")

    print("=" * 60)
    print("  ⚠ AGGIUNGI TOTP_SECRET AL .env PRIMA DI RIAVVIARE")
    print("=" * 60 + "\n")

    return new_secret


def verify_totp(code: str) -> bool:
    """Verifica un codice TOTP. Accetta il codice corrente e quello precedente (30s)."""
    secret = get_totp_secret()
    totp   = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


# ── Session management ────────────────────────────────────────────────────────

def create_session(ip: str) -> str:
    """Crea una nuova sessione, la persiste su file e ritorna il token."""
    _clean_sessions()
    token = secrets.token_urlsafe(32)
    now   = time.time()
    sessions = _load_sessions()
    sessions[token] = {
        "created_at":  now,
        "last_active": now,
        "ip":          ip,
    }
    _save_sessions(sessions)
    log.info(f"[admin_auth] Nuova sessione creata per IP {ip}")
    return token


def validate_session(token: Optional[str], ip: str) -> bool:
    """Valida una sessione leggendo dal file. Aggiorna last_active se valida."""
    if not token:
        return False

    sessions = _load_sessions()
    if token not in sessions:
        return False

    data = sessions[token]
    now  = time.time()

    if now - data["created_at"] > SESSION_TTL_SECONDS:
        sessions.pop(token, None)
        _save_sessions(sessions)
        return False

    if now - data["last_active"] > SESSION_IDLE_SECONDS:
        sessions.pop(token, None)
        _save_sessions(sessions)
        return False

    data["last_active"] = now
    _save_sessions(sessions)
    return True


def destroy_session(token: Optional[str]) -> None:
    if not token:
        return
    sessions = _load_sessions()
    sessions.pop(token, None)
    _save_sessions(sessions)
    log.info("[admin_auth] Sessione distrutta")


# ── Rate limiting ─────────────────────────────────────────────────────────────

def _is_locked_out(ip: str) -> bool:
    if ip in _lockouts:
        if time.time() < _lockouts[ip]:
            return True
        else:
            del _lockouts[ip]
    return False


def _record_failed_attempt(ip: str) -> bool:
    now = time.time()
    _attempts[ip] = [t for t in _attempts[ip] if now - t < TOTAL_LOCKOUT_WINDOW]
    _attempts[ip].append(now)

    recent_short = [t for t in _attempts[ip] if now - t < 300]
    if len(recent_short) >= LOCKOUT_ATTEMPTS:
        _lockouts[ip] = now + LOCKOUT_DURATION
        log.warning(f"[admin_auth] IP {ip} in lockout per {LOCKOUT_DURATION//60} minuti")
        return True

    if len(_attempts[ip]) >= TOTAL_LOCKOUT_LIMIT:
        _send_lockout_alert(ip, len(_attempts[ip]))
        log.critical(f"[admin_auth] ALERT: {len(_attempts[ip])} tentativi falliti da {ip} in 1 ora")

    return False


def _send_lockout_alert(ip: str, attempts: int) -> None:
    try:
        admin_email = os.getenv("ADMIN_EMAIL", "")
        if not admin_email:
            return
        from api_keys.email_templates import _send
        subject = "⚠ Security Alert — Admin login attempts"
        html = f"""
        <div style="font-family:system-ui;padding:24px;max-width:500px">
          <h2 style="color:#e24b4a">Security Alert</h2>
          <p>Rilevati <strong>{attempts}</strong> tentativi di login falliti
          dall'IP <code>{ip}</code> nell'ultima ora.</p>
          <p>Timestamp: {datetime.now(timezone.utc).isoformat()}</p>
          <p>Controlla i log del server immediatamente.</p>
        </div>
        """
        _send(admin_email, subject, html)
    except Exception as e:
        log.error(f"[admin_auth] Impossibile inviare alert: {e}")


# ── Login logic ───────────────────────────────────────────────────────────────

def admin_login(
    admin_key: str,
    totp_code: str,
    ip: str,
) -> tuple[bool, str]:
    """
    Verifica credenziali admin.
    Returns: (success, error_message)
    """
    if _is_locked_out(ip):
        remaining = int((_lockouts.get(ip, 0) - time.time()) / 60) + 1
        return False, f"Too many attempts. Try again in {remaining} minutes."

    expected_key = os.getenv("ADMIN_KEY", "")
    if not expected_key:
        log.error("[admin_auth] ADMIN_KEY non configurata nel .env!")
        return False, "Server configuration error."

    key_ok = secrets.compare_digest(
        hashlib.sha256(admin_key.encode()).hexdigest(),
        hashlib.sha256(expected_key.encode()).hexdigest(),
    )

    totp_ok = verify_totp(totp_code.strip())

    if not key_ok or not totp_ok:
        locked = _record_failed_attempt(ip)
        if locked:
            return False, "Too many failed attempts. IP locked for 15 minutes."
        log.warning(f"[admin_auth] Login fallito da {ip} (key_ok={key_ok}, totp_ok={totp_ok})")
        return False, "Invalid credentials."

    log.info(f"[admin_auth] Login admin riuscito da {ip}")
    return True, ""


# ── FastAPI helpers ───────────────────────────────────────────────────────────

def get_client_ip(request: Request) -> str:
    """Estrae l'IP reale anche dietro proxy/Render."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def require_admin(
    request: Request,
    admin_session: Optional[str] = Cookie(default=None),
) -> Optional[dict]:
    ip = get_client_ip(request)
    if validate_session(admin_session, ip):
        return {"authenticated": True, "ip": ip}
    return None


def set_session_cookie(response, token: str):
    secure = os.getenv("ENVIRONMENT", "development") != "development"
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",   # "strict" bloccava il cookie dopo il redirect post-login
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME)