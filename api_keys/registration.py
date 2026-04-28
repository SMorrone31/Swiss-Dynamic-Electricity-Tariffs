"""
registration.py
===============
Logica completa per la gestione delle API key:
  - Registrazione utenti
  - Approvazione / rifiuto admin
  - Revoca / sospensione / riattivazione
  - Verifica key nelle richieste API
  - Rate limiting con reset ora svizzera
  - Notifiche 80% / 100% rate limit all'utente

Modello ApiKey definito qui e importato in database.py tramite Base.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Integer, String, Text,
    Index, Enum as SAEnum
)
from sqlalchemy.orm import Session

from database import Base

log = logging.getLogger("registration")


# ── Costanti ──────────────────────────────────────────────────────────────────

DEFAULT_RATE_LIMIT_DAY = 500          # richieste gratuite al giorno
KEY_PREFIX             = "stk_"      # prefisso visibile delle key
WARNING_THRESHOLD_80   = 0.80        # notifica al 80% del rate limit
WARNING_THRESHOLD_100  = 1.00        # notifica al 100%


# ── Timezone helper ───────────────────────────────────────────────────────────

def _tz_ch():
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz
        return pytz.timezone("Europe/Zurich")


def _today_ch() -> date:
    return datetime.now(_tz_ch()).date()


# ── Model ─────────────────────────────────────────────────────────────────────

class ApiKey(Base):
    """
    Una riga per ogni richiesta di API key.
    Un utente (email) può avere più righe — solo una in stato active.
    Le key passate vengono messe in stato superseded con hash azzerato.
    """
    __tablename__ = "api_keys"

    id              = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    full_name       = Column(String(200), nullable=False)
    email           = Column(String(200), nullable=False)
    company         = Column(String(200), nullable=True)    # None = privato
    vat_number      = Column(String(100), nullable=True)    # testo libero
    country         = Column(String(10),  nullable=False)   # codice ISO es. "CH"
    
    # La key vera non viene mai storata — solo il suo SHA256
    api_key_hash    = Column(String(64),  nullable=True)    # SHA256, None se superseded
    key_prefix      = Column(String(20),  nullable=False)   # es. "stk_a1b2c3d4" per display

    status          = Column(
        SAEnum("pending", "active", "suspended", "revoked", "superseded",
               name="apikey_status"),
        nullable=False,
        default="pending"
    )

    rate_limit_day  = Column(Integer, default=DEFAULT_RATE_LIMIT_DAY)
    requests_today  = Column(Integer, default=0)
    requests_total  = Column(Integer, default=0)
    last_reset_date = Column(String(10), nullable=True)     # "YYYY-MM-DD" ora CH

    last_used_at    = Column(DateTime(timezone=True), nullable=True)
    created_at      = Column(DateTime(timezone=True),
                             default=lambda: datetime.now(timezone.utc))
    approved_at     = Column(DateTime(timezone=True), nullable=True)
    revoked_at      = Column(DateTime(timezone=True), nullable=True)
    suspended_at    = Column(DateTime(timezone=True), nullable=True)

    # Note interne admin
    notes           = Column(Text, nullable=True)

    # ID della key che ha sostituito questa (per tracciabilità)
    superseded_by   = Column(String(36), nullable=True)

    # Notifiche rate limit già inviate (reset ogni giorno con requests_today)
    warning_80_sent  = Column(Boolean, default=False)
    warning_100_sent = Column(Boolean, default=False)

    # Lingua preferita dell'utente (dalla lingua selezionata al momento della registrazione)
    preferred_lang  = Column(String(5), default="en")

    __table_args__ = (
        Index("ix_api_keys_email",      "email"),
        Index("ix_api_keys_hash",       "api_key_hash"),
        Index("ix_api_keys_status",     "status"),
    )

    def __repr__(self) -> str:
        return f"ApiKey({self.key_prefix} | {self.email} | {self.status})"


# ── Key generation ────────────────────────────────────────────────────────────

def _generate_key() -> tuple[str, str, str]:
    """
    Genera una nuova API key.
    Returns: (raw_key, key_hash, key_prefix)
      raw_key    → mostrata UNA SOLA VOLTA all'utente via email
      key_hash   → storata nel DB (SHA256)
      key_prefix → es. "stk_a1b2c3d4" — usata nel display admin
    """
    random_part = secrets.token_urlsafe(32)   # 256 bit di entropia
    raw_key     = f"{KEY_PREFIX}{random_part}"
    key_hash    = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix  = raw_key[:12]                # "stk_" + 8 char
    return raw_key, key_hash, key_prefix


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


# ── Registrazione ─────────────────────────────────────────────────────────────

def register_new_key(
    session: Session,
    full_name: str,
    email: str,
    country: str,
    preferred_lang: str = "en",
    company: Optional[str] = None,
    vat_number: Optional[str] = None,
    _force_replace: bool = False,
) -> tuple[ApiKey, str]:
    """
    Registra una nuova richiesta di API key.

    Regole:
    - Se l'utente ha già una key PENDING → errore (già in attesa)
    - Se l'utente ha una key ACTIVE → la mette in SUPERSEDED (hash azzerato)
      e crea una nuova PENDING
    - Se l'utente ha solo REVOKED/SUPERSEDED → crea nuova PENDING normalmente

    Returns: (nuovo record ApiKey, messaggio per l'utente)
    Raises: ValueError con messaggio leggibile
    """
    email = email.lower().strip()

    # Controlla stato corrente
    existing = session.query(ApiKey).filter(ApiKey.email == email).all()
    pending  = [k for k in existing if k.status == "pending"]
    active   = [k for k in existing if k.status == "active"]

    if pending:
        raise ValueError("pending_exists")

    # Se ha una key attiva → il frontend deve chiedere conferma prima di arrivare qui.
    # Se arriva qui con force=False (default), solleva active_key_exists.
    # Se arriva qui con force=True, supersede silenziosamente.
    if active and not _force_replace:
        raise ValueError("active_key_exists")

    # Supersede la key attiva (solo se force=True o non c'era key attiva)
    for old_key in active:
        old_key.status       = "superseded"
        old_key.api_key_hash = None
        old_key.revoked_at   = datetime.now(timezone.utc)
        session.add(old_key)

    # Genera placeholder — la key vera viene generata solo all'approvazione
    raw_key, key_hash, key_prefix = _generate_key()
    # NOTA: il raw_key viene generato ora ma NON viene inviato —
    # viene inviato solo quando l'admin approva. Qui lo storamo temporaneamente
    # nell'hash per poterlo recuperare all'approvazione.
    # Alternativa sicura: generare la key solo al momento dell'approvazione.
    # Scegliamo questa: la key viene generata SOLO all'approvazione admin.

    new_record = ApiKey(
        full_name      = full_name.strip(),
        email          = email,
        company        = company.strip() if company else None,
        vat_number     = vat_number.strip() if vat_number else None,
        country        = country.upper().strip(),
        api_key_hash   = None,          # popolato all'approvazione
        key_prefix     = "",            # popolato all'approvazione
        status         = "pending",
        preferred_lang = preferred_lang,
        rate_limit_day = DEFAULT_RATE_LIMIT_DAY,
    )
    session.add(new_record)
    session.commit()
    session.refresh(new_record)

    replaced = len(active) > 0
    msg = "replacement_pending" if replaced else "registration_pending"
    return new_record, msg


# ── Approvazione admin ────────────────────────────────────────────────────────

def approve_key(session: Session, key_id: str) -> tuple[ApiKey, str]:
    """
    Approva una registrazione pending.
    Genera la key, la stoca come hash, ritorna il raw_key per invio email.

    Returns: (record aggiornato, raw_key da inviare via email UNA SOLA VOLTA)
    Raises: ValueError se non trovata o non in stato pending
    """
    record = session.get(ApiKey, key_id)
    if not record:
        raise ValueError("not_found")
    if record.status != "pending":
        raise ValueError(f"wrong_status:{record.status}")

    raw_key, key_hash, key_prefix = _generate_key()

    record.api_key_hash = key_hash
    record.key_prefix   = key_prefix
    record.status       = "active"
    record.approved_at  = datetime.now(timezone.utc)
    record.last_reset_date = _today_ch().isoformat()
    session.add(record)
    session.commit()
    session.refresh(record)

    log.info(f"[approve] Key approvata: {record.email} → {key_prefix}")
    return record, raw_key


# ── Rifiuto admin ─────────────────────────────────────────────────────────────

def reject_key(session: Session, key_id: str, reason: str = "") -> ApiKey:
    """
    Rifiuta una registrazione pending.
    La elimina dal DB (nessuna traccia sensibile rimane).
    """
    record = session.get(ApiKey, key_id)
    if not record:
        raise ValueError("not_found")
    if record.status != "pending":
        raise ValueError(f"wrong_status:{record.status}")

    # Salva i dati necessari per l'email prima di cancellare
    email     = record.email
    full_name = record.full_name
    lang      = record.preferred_lang

    session.delete(record)
    session.commit()

    log.info(f"[reject] Registrazione rifiutata: {email}")
    return email, full_name, lang


# ── Revoca / Sospensione / Riattivazione ──────────────────────────────────────

def revoke_key(session: Session, key_id: str) -> ApiKey:
    """Revoca permanente — non riattivabile, hash azzerato."""
    record = session.get(ApiKey, key_id)
    if not record:
        raise ValueError("not_found")
    if record.status == "revoked":
        raise ValueError("already_revoked")

    record.status       = "revoked"
    record.api_key_hash = None      # distruggi subito
    record.revoked_at   = datetime.now(timezone.utc)
    session.add(record)
    session.commit()
    session.refresh(record)
    log.info(f"[revoke] Key revocata permanentemente: {record.email}")
    return record


def suspend_key(session: Session, key_id: str, reason: str = "") -> ApiKey:
    """Sospensione temporanea — riattivabile."""
    record = session.get(ApiKey, key_id)
    if not record:
        raise ValueError("not_found")
    if record.status != "active":
        raise ValueError(f"wrong_status:{record.status}")

    record.status       = "suspended"
    record.suspended_at = datetime.now(timezone.utc)
    if reason:
        record.notes = (record.notes or "") + f"\n[{datetime.now().date()}] Sospeso: {reason}"
    session.add(record)
    session.commit()
    session.refresh(record)
    log.info(f"[suspend] Key sospesa: {record.email}")
    return record


def reactivate_key(session: Session, key_id: str) -> ApiKey:
    """Riattiva una key sospesa."""
    record = session.get(ApiKey, key_id)
    if not record:
        raise ValueError("not_found")
    if record.status != "suspended":
        raise ValueError(f"wrong_status:{record.status}")

    record.status       = "active"
    record.suspended_at = None
    session.add(record)
    session.commit()
    session.refresh(record)
    log.info(f"[reactivate] Key riattivata: {record.email}")
    return record


def update_key(
    session: Session,
    key_id: str,
    rate_limit_day: Optional[int] = None,
    notes: Optional[str] = None,
) -> ApiKey:
    """Aggiorna rate limit e/o note di una key."""
    record = session.get(ApiKey, key_id)
    if not record:
        raise ValueError("not_found")

    if rate_limit_day is not None:
        record.rate_limit_day = rate_limit_day
    if notes is not None:
        record.notes = notes

    session.add(record)
    session.commit()
    session.refresh(record)
    return record


# ── Verifica key (middleware) ─────────────────────────────────────────────────

def verify_api_key(
    session: Session,
    raw_key: str,
) -> tuple[Optional[ApiKey], str]:
    """
    Verifica una API key nelle richieste.
    Aggiorna i contatori, controlla il rate limit, gestisce le notifiche.

    Returns: (record, esito)
      esito: "ok" | "not_found" | "revoked" | "suspended" | "rate_limit"
    """
    if not raw_key or not raw_key.startswith(KEY_PREFIX):
        return None, "not_found"

    key_hash = _hash_key(raw_key)
    record   = session.query(ApiKey).filter(
        ApiKey.api_key_hash == key_hash
    ).first()

    if not record:
        return None, "not_found"

    if record.status == "revoked":
        return record, "revoked"

    if record.status == "suspended":
        return record, "suspended"

    if record.status != "active":
        return record, "not_found"

    # Reset contatore giornaliero se è un nuovo giorno (ora svizzera)
    today_ch = _today_ch().isoformat()
    if record.last_reset_date != today_ch:
        record.requests_today   = 0
        record.last_reset_date  = today_ch
        record.warning_80_sent  = False
        record.warning_100_sent = False

    # Controlla rate limit PRIMA di incrementare
    if record.requests_today >= record.rate_limit_day:
        # Invia notifica 100% se non già inviata
        if not record.warning_100_sent:
            record.warning_100_sent = True
            session.add(record)
            session.commit()
            _send_rate_limit_warning(record, 100)
        return record, "rate_limit"

    # Incrementa contatori
    record.requests_today  += 1
    record.requests_total  += 1
    record.last_used_at     = datetime.now(timezone.utc)
    session.add(record)
    session.commit()

    # Notifica 80% (dopo incremento, prima del return ok)
    pct = record.requests_today / record.rate_limit_day
    if pct >= WARNING_THRESHOLD_80 and not record.warning_80_sent:
        record.warning_80_sent = True
        session.add(record)
        session.commit()
        _send_rate_limit_warning(record, 80)

    return record, "ok"


# ── Notifiche rate limit ──────────────────────────────────────────────────────

def _send_rate_limit_warning(record: ApiKey, pct: int) -> None:
    """Invia email di warning rate limit all'utente in background."""
    try:
        from api_keys.email_templates import send_rate_limit_warning
        send_rate_limit_warning(record, pct)
    except Exception as e:
        log.warning(f"[rate_limit_warning] Email fallita per {record.email}: {e}")


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_all_keys(
    session: Session,
    status: Optional[str] = None,
) -> list[ApiKey]:
    """Lista tutte le key, opzionalmente filtrate per status."""
    q = session.query(ApiKey)
    if status:
        q = q.filter(ApiKey.status == status)
    return q.order_by(ApiKey.created_at.desc()).all()


def get_pending_count(session: Session) -> int:
    return session.query(ApiKey).filter(ApiKey.status == "pending").count()


def get_stats(session: Session) -> dict:
    """Statistiche globali per la dashboard admin."""
    from sqlalchemy import func
    total    = session.query(func.count(ApiKey.id)).scalar() or 0
    active   = session.query(func.count(ApiKey.id)).filter(ApiKey.status == "active").scalar() or 0
    pending  = session.query(func.count(ApiKey.id)).filter(ApiKey.status == "pending").scalar() or 0
    suspended= session.query(func.count(ApiKey.id)).filter(ApiKey.status == "suspended").scalar() or 0
    revoked  = session.query(func.count(ApiKey.id)).filter(ApiKey.status == "revoked").scalar() or 0

    today_requests = session.query(func.sum(ApiKey.requests_today)).scalar() or 0
    total_requests = session.query(func.sum(ApiKey.requests_total)).scalar() or 0

    # Top 5 per utilizzo oggi
    top5 = (
        session.query(ApiKey)
        .filter(ApiKey.status == "active")
        .order_by(ApiKey.requests_today.desc())
        .limit(5)
        .all()
    )

    return {
        "total":          total,
        "active":         active,
        "pending":        pending,
        "suspended":      suspended,
        "revoked":        revoked,
        "today_requests": today_requests,
        "total_requests": total_requests,
        "top5_today": [
            {
                "email":          k.email,
                "key_prefix":     k.key_prefix,
                "requests_today": k.requests_today,
                "rate_limit_day": k.rate_limit_day,
            }
            for k in top5
        ],
    }