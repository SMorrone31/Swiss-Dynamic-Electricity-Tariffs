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

import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("admin_routes")

from fastapi import Cookie, Depends, HTTPException, Query, Request, Response
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
    "/api/v1/confirm-login",
    "/api/v1/upgrade-to-pro",   # autenticato via api_key nel body JSON
    "/api/v1/me/tariff",        # autenticato via api_key nel body JSON
    "/api/v1/chat",             # chatbot rule-based — pubblico, no API key
    "/docs",
    "/redoc",
    "/openapi.json",
    "/admin",
    "/admin/login",
    "/admin/logout",
    "/admin/setup-totp",
}

# Endpoint interni: usati SOLO dalla dashboard, protetti da X-Dashboard-Secret
# Non contano come richieste API utente, non richiedono API key
INTERNAL_PREFIXES = ("/internal/",)

# Endpoint dati pubblici per API key utenti (ora richiedono key obbligatoria)
DATA_PATHS = {
    "/api/v1/tariffs",
    "/api/v1/smart",
    "/api/v1/summary/daily",
    "/api/v1/summary/monthly",
    "/api/v1/latest",
    "/api/v1/metrics",
    "/api/v1/prices/all",
    "/api/v1/prices",
}

DATA_PREFIXES = (
    "/api/v1/prices",
    "/api/v1/summary/",
)

# Prefissi pubblici (es. /admin/... gestito internamente)
PUBLIC_PREFIXES = ("/admin",)


def _get_dashboard_secret() -> str:
    return os.getenv("DASHBOARD_SECRET", "")


def is_internal(path: str) -> bool:
    return path.startswith("/internal/")


def is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path.startswith("/admin"):
        return True
    if path.startswith("/api/v1/admin/"):
        return True
    if path.startswith("/api/v1/summary/"):
        return True
    if path.startswith("/api/v1/latest"):
        return True
    if path.startswith("/api/v1/health"):
        return True
    return False


def is_data_path(path: str) -> bool:
    """Endpoint dati esterni: richiedono API key."""
    if path in DATA_PATHS:
        return True
    for prefix in DATA_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


# ── Middleware ────────────────────────────────────────────────────────────────

async def api_key_middleware(request: Request, call_next):
    """
    Middleware aggiornato — tre livelli:

    1. /internal/* → richiede X-Dashboard-Secret (usato dalla dashboard stessa)
       Non conta come richiesta API utente.

    2. Percorsi pubblici (/, /admin/*, /api/v1/health, zip-map, ecc.) → liberi.

    3. /api/v1/* dati → richiedono API key obbligatoria (X-API-Key).
       Il piano free/pro viene verificato nei singoli endpoint.
    """
    path = request.url.path

    # ── 1. Endpoint interni (dashboard) ──────────────────────────────────────
    if is_internal(path):
        secret = _get_dashboard_secret()
        incoming = request.headers.get("X-Dashboard-Secret", "")
        # Admin key bypassa anche gli internal (per debug)
        admin_key = os.getenv("ADMIN_KEY", "")
        if (secret and incoming == secret) or (admin_key and incoming == admin_key):
            return await call_next(request)
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # ── 2. Percorsi sempre pubblici ───────────────────────────────────────────
    if is_public(path):
        return await call_next(request)

    raw_key = request.headers.get("X-API-Key", "").strip()

    # ADMIN_KEY bypassa tutto
    admin_key = os.getenv("ADMIN_KEY", "")
    if admin_key and raw_key == admin_key:
        return await call_next(request)

    # ── 3. Endpoint dati esterni → API key obbligatoria ───────────────────────
    if is_data_path(path):
        if not raw_key:
            return JSONResponse(
                {"error": "Missing API key", "hint": "Add X-API-Key header"},
                status_code=401,
            )
        from database import init_db
        from api_keys.registration import verify_api_key
        SessionLocal = init_db()
        with SessionLocal() as session:
            record, outcome = verify_api_key(session, raw_key)
        if outcome == "ok":
            # Inietta piano e free_tariff_id nello stato della request
            request.state.plan           = getattr(record, "plan", "free") or "free"
            request.state.free_tariff_id = getattr(record, "free_tariff_id", None)
            request.state.api_key_record = record
            return await call_next(request)
        if outcome == "rate_limit":
            return JSONResponse(
                {"error": "Rate limit exceeded",
                 "limit": record.rate_limit_day if record else 500,
                 "used":  record.requests_today if record else 0,
                 "resets": "midnight CET/CEST",
                 "hint":   "Upgrade to Pro for higher limits"},
                status_code=429,
            )
        if outcome == "suspended":
            return JSONResponse({"error": "API key suspended", "hint": "Contact support"}, status_code=403)
        if outcome in ("revoked", "not_found"):
            return JSONResponse({"error": "Invalid or revoked API key"}, status_code=401)
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # ── 4. Tutto il resto → API key obbligatoria ──────────────────────────────
    if not raw_key:
        return JSONResponse(
            {"error": "Missing API key", "hint": "Add X-API-Key header"},
            status_code=401,
        )

    from database import init_db
    from api_keys.registration import verify_api_key
    SessionLocal = init_db()
    with SessionLocal() as session:
        record, outcome = verify_api_key(session, raw_key)

    if outcome == "ok":
        request.state.plan           = getattr(record, "plan", "free") or "free"
        request.state.free_tariff_id = getattr(record, "free_tariff_id", None)
        request.state.api_key_record = record
        return await call_next(request)
    if outcome == "rate_limit":
        return JSONResponse(
            {"error": "Rate limit exceeded",
             "limit": record.rate_limit_day if record else 500,
             "used":  record.requests_today if record else 0,
             "resets": "midnight CET/CEST",
             "hint":   "Upgrade to Pro for higher limits"},
            status_code=429,
        )
    if outcome == "suspended":
        return JSONResponse({"error": "API key suspended", "hint": "Contact support"}, status_code=403)
    if outcome in ("revoked", "not_found"):
        return JSONResponse({"error": "Invalid or revoked API key"}, status_code=401)

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
    session_token: Optional[str] = None


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
    plan:           Optional[str] = None   # "free" | "pro"


# ── Helper: require admin session ─────────────────────────────────────────────

def _require_admin(request: Request, admin_session: Optional[str] = Cookie(default=None)):
    from api_keys.admin_auth import validate_session, get_client_ip
    ip = get_client_ip(request)
    if not validate_session(admin_session, ip):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return True


# ── Chatbot rule-based engine ─────────────────────────────────────────────────

class _ChatMsg(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: list[_ChatMsg]
    lang: Optional[str] = None   # hint lingua dal frontend (opzionale)


def _detect_lang(text: str, hint: Optional[str]) -> str:
    """Rileva la lingua dal testo o usa l'hint."""
    if hint and hint in ("it", "de", "fr", "en"):
        return hint
    t = text.lower()
    # Segnali italiani
    it_signals = ["che cos", "cosa", "come funziona", "come si", "vorrei", "puoi",
                  "dimmi", "grazie", "ciao", "aiuto", "spiegami", "quando",
                  "quanto costa", "perché", "quali", "mostra", "hai", "ho"]
    de_signals = ["was ist", "wie", "kannst", "bitte", "danke", "hallo", "zeig",
                  "welche", "kostet", "warum", "können", "ich"]
    fr_signals = ["qu'est", "comment", "pouvez", "merci", "bonjour", "montrez",
                  "quels", "coûte", "pourquoi", "quel", "une", "des", "les"]
    it_count = sum(1 for s in it_signals if s in t)
    de_count = sum(1 for s in de_signals if s in t)
    fr_count = sum(1 for s in fr_signals if s in t)
    if it_count >= de_count and it_count >= fr_count and it_count > 0:
        return "it"
    if de_count > fr_count and de_count > 0:
        return "de"
    if fr_count > 0:
        return "fr"
    return "en"


# ── Knowledge base ────────────────────────────────────────────────────────────

_PROVIDERS = {
    "ckw": {
        "name": "CKW (Zentralschweiz)",
        "tariff_ids": ["ckw_home_dynamic", "ckw_business_dynamic"],
        "api_url": "https://e-ckw-public-data.de-c1.eu1.cloudhub.io/api/v1/netzinformationen/energie/dynamische-preise",
        "region": {"it": "Svizzera Centrale", "de": "Zentralschweiz", "fr": "Suisse centrale", "en": "Central Switzerland"},
    },
    "ekz": {
        "name": "EKZ",
        "tariff_ids": ["ekz_energie_dynamisch_netz_400d"],
        "api_url": "https://api.tariffs.ekz.ch",
        "region": {"it": "Zurigo", "de": "Zürich", "fr": "Zurich", "en": "Zurich"},
    },
    "ekz_einsiedeln": {
        "name": "EKZ Einsiedeln",
        "tariff_ids": ["ekz_einsiedeln_energie_dynamisch_netz_400d"],
        "api_url": "https://api.tariffs.ekz.ch",
        "region": {"it": "Einsiedeln (Svitto)", "de": "Einsiedeln (Schwyz)", "fr": "Einsiedeln (Schwyz)", "en": "Einsiedeln (Schwyz)"},
    },
    "aem": {
        "name": "AEM (Azienda Elettrica di Massagno)",
        "tariff_ids": ["aem_tariffa_dinamica"],
        "api_url": "https://servizi.aemsa.ch/tariffe",
        "region": {"it": "Massagno, Ticino", "de": "Massagno, Tessin", "fr": "Massagno, Tessin", "en": "Massagno, Ticino"},
    },
    "groupe_e": {
        "name": "Groupe E",
        "tariff_ids": ["groupe_e_vario"],
        "api_url": "https://api.tariffs.groupe-e.ch/v2/tariffs",
        "region": {"it": "Friborgo / Romandia", "de": "Freiburg / Romandie", "fr": "Fribourg / Romandie", "en": "Fribourg / Romandy"},
    },
    "primeo": {
        "name": "Primeo Energie",
        "tariff_ids": ["primeo_netzdynamisch"],
        "api_url": "https://tarife.primeo-energie.ch/api/v1/tariffs",
        "region": {"it": "Basilea / nord-ovest", "de": "Basel / Nordwestschweiz", "fr": "Bâle / nord-ouest", "en": "Basel / NW Switzerland"},
    },
    "avag": {
        "name": "AVAG (Aare Versorgungs AG)",
        "tariff_ids": ["avag_primeo_netzdynamisch"],
        "api_url": "https://tarife.primeo-energie.ch/api/v1/tariffs",
        "region": {"it": "Canton Berna", "de": "Kanton Bern", "fr": "Canton de Berne", "en": "Canton Bern"},
    },
    "elag": {
        "name": "ELAG (Elektra Gretzenbach AG)",
        "tariff_ids": ["elag_primeo_netzdynamisch"],
        "api_url": "https://tarife.primeo-energie.ch/api/v1/tariffs",
        "region": {"it": "Gretzenbach, Soletta", "de": "Gretzenbach, Solothurn", "fr": "Gretzenbach, Soleure", "en": "Gretzenbach, Solothurn"},
    },
    "ail": {
        "name": "AIL (Aziende industriali di Lugano)",
        "tariff_ids": ["ail_tariffa_dinamica"],
        "api_url": None,
        "web_url": "https://www.ail.ch/privati/elettricita/prodotti/Tariffa-dinamica/tariffa-dinamica.html",
        "region": {"it": "Lugano, Ticino", "de": "Lugano, Tessin", "fr": "Lugano, Tessin", "en": "Lugano, Ticino"},
    },
    "ega": {
        "name": "EGA (Elektra Aettenschwil)",
        "tariff_ids": ["ega_einheitstarif_netz_dinamisch"],
        "api_url": None,
        "web_url": "https://esit.code-fabrik.ch/doc_scalar",
        "region": {"it": "Aettenschwil, Argovia", "de": "Aettenschwil, Aargau", "fr": "Aettenschwil, Argovie", "en": "Aettenschwil, Aargau"},
    },
}

_ENDPOINTS = [
    {"path": "GET /api/v1/prices/today?tariff_id=X",    "desc": {"it": "96 slot prezzi del giorno corrente", "de": "96 Preisslots für heute", "fr": "96 créneaux de prix pour aujourd'hui", "en": "96 price slots for today"}},
    {"path": "GET /api/v1/prices/tomorrow?tariff_id=X", "desc": {"it": "Prezzi domani (disponibili dopo ~17:30 UTC)", "de": "Preise morgen (ab ~17:30 UTC)", "fr": "Prix demain (disponibles après ~17:30 UTC)", "en": "Tomorrow's prices (available after ~17:30 UTC)"}},
    {"path": "GET /api/v1/prices/all",                   "desc": {"it": "Tutte le tariffe, tutti gli slot (solo Pro)", "de": "Alle Tarife, alle Slots (nur Pro)", "fr": "Tous les tarifs, tous les créneaux (Pro uniquement)", "en": "All tariffs, all slots (Pro only)"}},
    {"path": "GET /api/v1/tariffs",                      "desc": {"it": "Lista di tutte le tariffe disponibili", "de": "Liste aller verfügbaren Tarife", "fr": "Liste de tous les tarifs disponibles", "en": "List of all available tariffs"}},
    {"path": "GET /api/v1/health",                       "desc": {"it": "Stato del sistema e uptime", "de": "Systemstatus und Uptime", "fr": "État du système et uptime", "en": "System health and uptime"}},
]

_T = {
    "it": {
        "unknown":       "Non ho trovato una risposta precisa a questa domanda. Per assistenza dettagliata scrivi a **support@tariffhub.ch** — risponderemo entro 24 ore! 📧",
        "greeting":      "👋 Ciao! Sono l'assistente di **Swiss Tariff Hub**.\nPosso aiutarti con informazioni su tariffe dinamiche svizzere, piani, endpoint API e provider. Come posso aiutarti?",
        "about":         "**Swiss Tariff Hub** è un servizio B2B che aggrega le tariffe elettriche dinamiche dei principali fornitori svizzeri (EVU) in un'unica API REST normalizzata.\n\n📊 **Formato dati**: slot da 15 minuti in UTC con tre componenti di prezzo: `energy_price_utc`, `grid_price_utc`, `residual_price_utc` (CHF/kWh)\n\n🗺️ **Dashboard**: mappa interattiva della Svizzera, visualizzatore tariffe Smart, console di test API\n\n🌍 **Lingue supportate**: IT, DE, FR, EN",
        "plans":         "## Piani disponibili\n\n**Piano Free** — gratuito\n- 500 richieste/giorno\n- Accesso a 1 tariffa a scelta\n- Endpoint base\n- Storico limitato\n\n**Piano Pro** — CHF 19/mese\n- 2.000 richieste/giorno\n- Tutte le tariffe svizzere\n- Storico illimitato\n- Tutti gli endpoint\n- Fatturazione mensile, disdici quando vuoi\n\n👉 Registrati gratuitamente su `/register`, poi passa a Pro dalla dashboard.",
        "providers":     "## Provider supportati ({n} totali)\n",
        "provider_row":  "- **{name}** — {region}\n  Tariff ID: `{ids}`\n  API reale: {url}\n",
        "provider_no_api": "  ⚠️ Nessuna API pubblica disponibile\n",
        "provider_web_link": "  🌐 Sito ufficiale / documentazione: {url}\n",
        "endpoints":     "## Endpoint STH disponibili\n",
        "endpoint_row":  "- `{path}` — {desc}\n",
        "update_times":  "⏰ **Orari aggiornamento dati:**\n- CKW: ogni giorno alle **11:05 UTC**\n- Tutti gli altri provider: ogni giorno alle **17:30 UTC**\n\nI dati del giorno successivo (domani) sono tipicamente disponibili dopo le 17:30 UTC.",
        "dst":           "🕐 **Gestione cambio ora (DST):**\nIl sistema usa `Europe/Zurich` per calcolare gli slot correttamente:\n- Giorno normale: **96 slot** (24h × 4)\n- Domenica di primavera (−1h): **92 slot**\n- Domenica d'autunno (+1h): **100 slot**",
        "data_format":   "📐 **Formato dati API:**\nOgni risposta contiene slot da **15 minuti in UTC** con:\n```\nenergy_price_utc   → prezzo energia (CHF/kWh)\ngrid_price_utc     → prezzo rete (CHF/kWh)\nresidual_price_utc → componenti residue (CHF/kWh)\n```\nI timestamp sono in formato ISO 8601 UTC.",
        "register":      "**Registrazione gratuita** in pochi secondi:\n1. Vai su `/register` nella dashboard\n2. Inserisci nome, email e azienda\n3. Ricevi la tua API key via email dopo l'approvazione\n\nNessuna carta di credito richiesta per il piano Free. Il piano Pro (CHF 19/mese) si attiva direttamente dalla dashboard.",
        "contact":       "📧 **Contatti Swiss Tariff Hub:**\nEmail: **support@tariffhub.ch**\n\nRispondiamo entro 24 ore nei giorni lavorativi.",
        "pricing_detail":"💰 **Dettaglio prezzi:**\n- Piano Free: **gratuito** (500 req/giorno)\n- Piano Pro: **CHF 19/mese** — fatturazione mensile, nessun vincolo\n- Nessuna commissione per slot, nessun costo a consumo\n- Puoi disdire in qualsiasi momento prima della prossima scadenza",
        "security":      "🔒 **Sicurezza & Autenticazione:**\nL'accesso alle API avviene tramite **API key** nell'header HTTP:\n```\nAuthorization: Bearer stk_xxxxxxxxxxxx\n```\nLe chiavi sono hashate (SHA-256) nel database. Nessun dato sensibile è memorizzato in chiaro.",
    },
    "de": {
        "unknown":       "Ich konnte auf diese Frage keine genaue Antwort finden. Für detaillierte Hilfe schreiben Sie an **support@tariffhub.ch** — wir antworten innerhalb von 24 Stunden! 📧",
        "greeting":      "👋 Hallo! Ich bin der Assistent von **Swiss Tariff Hub**.\nIch helfe Ihnen mit Informationen zu dynamischen Schweizer Stromtarifen, Plänen, API-Endpunkten und Anbietern.",
        "about":         "**Swiss Tariff Hub** ist ein B2B-Dienst, der dynamische Stromtarife der wichtigsten Schweizer Energieversorger (EVU) in einer einheitlichen normalisierten REST-API aggregiert.\n\n📊 **Datenformat**: 15-Minuten-Slots in UTC mit drei Preiskomponenten: `energy_price_utc`, `grid_price_utc`, `residual_price_utc` (CHF/kWh)\n\n🗺️ **Dashboard**: Interaktive Schweizer Karte, Smart-Tarifanzeige, API-Testkonsole\n\n🌍 **Sprachen**: IT, DE, FR, EN",
        "plans":         "## Verfügbare Pläne\n\n**Free-Plan** — kostenlos\n- 500 Anfragen/Tag\n- Zugang zu 1 Tarif nach Wahl\n- Basis-Endpunkte\n\n**Pro-Plan** — CHF 19/Monat\n- 2.000 Anfragen/Tag\n- Alle Schweizer Tarife\n- Unbegrenzte Historie\n- Alle Endpunkte\n- Monatliche Abrechnung, jederzeit kündbar\n\n👉 Registrierung unter `/register`, dann Pro-Upgrade im Dashboard.",
        "providers":     "## Unterstützte Anbieter ({n} gesamt)\n",
        "provider_row":  "- **{name}** — {region}\n  Tariff-ID: `{ids}`\n  Echtzeit-API: {url}\n",
        "provider_no_api": "  ⚠️ Keine öffentliche API verfügbar\n",
        "provider_web_link": "  🌐 Offizielle Website / Dokumentation: {url}\n",
        "endpoints":     "## Verfügbare STH-Endpunkte\n",
        "endpoint_row":  "- `{path}` — {desc}\n",
        "update_times":  "⏰ **Datenaktualisierungszeiten:**\n- CKW: täglich um **11:05 UTC**\n- Alle anderen Anbieter: täglich um **17:30 UTC**",
        "dst":           "🕐 **Sommerzeit-Behandlung (DST):**\nDas System verwendet `Europe/Zurich`:\n- Normaler Tag: **96 Slots**\n- Frühjahr (−1h): **92 Slots**\n- Herbst (+1h): **100 Slots**",
        "data_format":   "📐 **API-Datenformat:**\nJede Antwort enthält **15-Minuten-Slots in UTC** mit:\n```\nenergy_price_utc   → Energiepreis (CHF/kWh)\ngrid_price_utc     → Netzpreis (CHF/kWh)\nresidual_price_utc → Restkomponenten (CHF/kWh)\n```",
        "register":      "**Kostenlose Registrierung** in wenigen Sekunden:\n1. Gehen Sie zu `/register` im Dashboard\n2. Name, E-Mail und Unternehmen eingeben\n3. API-Key per E-Mail nach Genehmigung erhalten\n\nKeine Kreditkarte für den Free-Plan erforderlich.",
        "contact":       "📧 **Kontakt Swiss Tariff Hub:**\nE-Mail: **support@tariffhub.ch**\n\nWir antworten innerhalb von 24 Stunden an Werktagen.",
        "pricing_detail":"💰 **Preisdetails:**\n- Free-Plan: **kostenlos** (500 Anfragen/Tag)\n- Pro-Plan: **CHF 19/Monat** — monatliche Abrechnung, keine Bindung\n- Jederzeit vor dem nächsten Abrechnungsdatum kündbar",
        "security":      "🔒 **Sicherheit & Authentifizierung:**\nAPI-Zugang über **API-Key** im HTTP-Header:\n```\nAuthorization: Bearer stk_xxxxxxxxxxxx\n```\nKeys werden SHA-256-gehasht gespeichert.",
    },
    "fr": {
        "unknown":       "Je n'ai pas trouvé de réponse précise à cette question. Pour une aide détaillée, écrivez à **support@tariffhub.ch** — nous répondrons dans les 24 heures ! 📧",
        "greeting":      "👋 Bonjour ! Je suis l'assistant de **Swiss Tariff Hub**.\nJe peux vous aider avec les tarifs dynamiques suisses, les plans, les endpoints API et les fournisseurs.",
        "about":         "**Swiss Tariff Hub** est un service B2B qui agrège les tarifs d'électricité dynamiques des principaux fournisseurs suisses (EVU) dans une API REST unifiée et normalisée.\n\n📊 **Format des données** : créneaux de 15 minutes en UTC avec trois composantes : `energy_price_utc`, `grid_price_utc`, `residual_price_utc` (CHF/kWh)\n\n🗺️ **Dashboard** : carte interactive de la Suisse, visualiseur Smart, console de test API\n\n🌍 **Langues** : IT, DE, FR, EN",
        "plans":         "## Plans disponibles\n\n**Plan Free** — gratuit\n- 500 requêtes/jour\n- Accès à 1 tarif au choix\n- Endpoints de base\n\n**Plan Pro** — CHF 19/mois\n- 2 000 requêtes/jour\n- Tous les tarifs suisses\n- Historique illimité\n- Tous les endpoints\n- Facturation mensuelle, résiliable à tout moment\n\n👉 Inscription gratuite sur `/register`, puis upgrade Pro depuis le dashboard.",
        "providers":     "## Fournisseurs supportés ({n} au total)\n",
        "provider_row":  "- **{name}** — {region}\n  Tariff ID : `{ids}`\n  API réelle : {url}\n",
        "provider_no_api": "  ⚠️ Pas d'API publique disponible\n",
        "provider_web_link": "  🌐 Site officiel / documentation : {url}\n",
        "endpoints":     "## Endpoints STH disponibles\n",
        "endpoint_row":  "- `{path}` — {desc}\n",
        "update_times":  "⏰ **Horaires de mise à jour :**\n- CKW : tous les jours à **11:05 UTC**\n- Tous les autres fournisseurs : tous les jours à **17:30 UTC**",
        "dst":           "🕐 **Gestion du changement d'heure (DST) :**\nLe système utilise `Europe/Zurich` :\n- Jour normal : **96 créneaux**\n- Printemps (−1h) : **92 créneaux**\n- Automne (+1h) : **100 créneaux**",
        "data_format":   "📐 **Format des données API :**\nChaque réponse contient des **créneaux de 15 minutes en UTC** avec :\n```\nenergy_price_utc   → prix énergie (CHF/kWh)\ngrid_price_utc     → prix réseau (CHF/kWh)\nresidual_price_utc → composantes résiduelles (CHF/kWh)\n```",
        "register":      "**Inscription gratuite** en quelques secondes :\n1. Allez sur `/register` dans le dashboard\n2. Saisissez nom, e-mail et entreprise\n3. Recevez votre clé API par e-mail après approbation\n\nAucune carte de crédit requise pour le plan Free.",
        "contact":       "📧 **Contact Swiss Tariff Hub :**\nE-mail : **support@tariffhub.ch**\n\nNous répondons dans les 24 heures les jours ouvrables.",
        "pricing_detail":"💰 **Détail des tarifs :**\n- Plan Free : **gratuit** (500 req/jour)\n- Plan Pro : **CHF 19/mois** — facturation mensuelle, sans engagement\n- Résiliable à tout moment avant la prochaine date de facturation",
        "security":      "🔒 **Sécurité & Authentification :**\nAccès API via **clé API** dans l'en-tête HTTP :\n```\nAuthorization: Bearer stk_xxxxxxxxxxxx\n```\nLes clés sont stockées hashées (SHA-256).",
    },
    "en": {
        "unknown":       "I couldn't find a precise answer to this question. For detailed assistance, write to **support@tariffhub.ch** — we'll reply within 24 hours! 📧",
        "greeting":      "👋 Hi! I'm the Swiss Tariff Hub assistant.\nI can help you with Swiss dynamic electricity tariffs, plans, API endpoints and providers. How can I help?",
        "about":         "**Swiss Tariff Hub** is a B2B service that aggregates dynamic electricity tariffs from major Swiss energy utilities (EVUs) into a single normalized REST API.\n\n📊 **Data format**: 15-minute UTC slots with three price components: `energy_price_utc`, `grid_price_utc`, `residual_price_utc` (CHF/kWh)\n\n🗺️ **Dashboard**: interactive Swiss map, Smart tariff viewer, API test console\n\n🌍 **Languages**: IT, DE, FR, EN",
        "plans":         "## Available Plans\n\n**Free Plan** — free\n- 500 requests/day\n- Access to 1 tariff of your choice\n- Basic endpoints\n\n**Pro Plan** — CHF 19/month\n- 2,000 requests/day\n- All Swiss tariffs\n- Unlimited history\n- All endpoints\n- Monthly billing, cancel anytime\n\n👉 Register for free at `/register`, then upgrade to Pro from the dashboard.",
        "providers":     "## Supported Providers ({n} total)\n",
        "provider_row":  "- **{name}** — {region}\n  Tariff ID: `{ids}`\n  Real API: {url}\n",
        "provider_no_api": "  ⚠️ No public API available\n",
        "provider_web_link": "  🌐 Official website / documentation: {url}\n",
        "endpoints":     "## Available STH Endpoints\n",
        "endpoint_row":  "- `{path}` — {desc}\n",
        "update_times":  "⏰ **Data update schedule:**\n- CKW: daily at **11:05 UTC**\n- All other providers: daily at **17:30 UTC**\n\nNext-day prices are typically available after 17:30 UTC.",
        "dst":           "🕐 **Daylight Saving Time (DST) handling:**\nThe system uses `Europe/Zurich` timezone:\n- Normal day: **96 slots**\n- Spring forward (−1h): **92 slots**\n- Autumn back (+1h): **100 slots**",
        "data_format":   "📐 **API data format:**\nEach response contains **15-minute UTC slots** with:\n```\nenergy_price_utc   → energy price (CHF/kWh)\ngrid_price_utc     → grid price (CHF/kWh)\nresidual_price_utc → residual components (CHF/kWh)\n```\nTimestamps are ISO 8601 UTC.",
        "register":      "**Free registration** in seconds:\n1. Go to `/register` in the dashboard\n2. Enter name, email and company\n3. Receive your API key by email after approval\n\nNo credit card required for the Free plan. The Pro plan (CHF 19/month) is activated directly from the dashboard.",
        "contact":       "📧 **Contact Swiss Tariff Hub:**\nEmail: **support@tariffhub.ch**\n\nWe reply within 24 hours on business days.",
        "pricing_detail":"💰 **Pricing details:**\n- Free plan: **free** (500 req/day)\n- Pro plan: **CHF 19/month** — monthly billing, no lock-in\n- Cancel anytime before the next billing date",
        "security":      "🔒 **Security & Authentication:**\nAPI access via **API key** in HTTP header:\n```\nAuthorization: Bearer stk_xxxxxxxxxxxx\n```\nKeys are SHA-256 hashed in the database.",
    },
}


def _cb_t(lang: str, key: str) -> str:
    return _T.get(lang, _T["en"]).get(key, _T["en"].get(key, ""))


def _build_providers_response(lang: str, filter_key: Optional[str] = None) -> str:
    items = _PROVIDERS.items() if not filter_key else [(filter_key, _PROVIDERS[filter_key])]
    n = len(_PROVIDERS) if not filter_key else 1
    out = _cb_t(lang, "providers").format(n=n)
    for key, p in items:
        region = p["region"].get(lang, p["region"]["en"])
        ids = ", ".join(p["tariff_ids"])
        if p["api_url"]:
            row = _cb_t(lang, "provider_row").format(
                name=p["name"], region=region, ids=ids, url=p["api_url"]
            )
        else:
            web_url = p.get("web_url")
            row = f"- **{p['name']}** — {region}\n  Tariff ID: `{ids}`\n"
            if web_url:
                row += _cb_t(lang, "provider_web_link").format(url=web_url)
            else:
                row += _cb_t(lang, "provider_no_api")
        out += row
    return out.strip()


def _build_endpoints_response(lang: str) -> str:
    out = _cb_t(lang, "endpoints")
    for ep in _ENDPOINTS:
        desc = ep["desc"].get(lang, ep["desc"]["en"])
        out += _cb_t(lang, "endpoint_row").format(path=ep["path"], desc=desc)
    return out.strip()


def _chatbot_reply(messages: list[dict], hint_lang: Optional[str]) -> str:
    """Engine rule-based: analizza l'ultimo messaggio utente e risponde."""
    # Prendi tutti i messaggi utente per contesto lingua
    user_msgs = [m["content"] for m in messages if m["role"] == "user"]
    if not user_msgs:
        return _cb_t("it", "greeting")

    last = user_msgs[-1].strip()
    lang = _detect_lang(last, hint_lang)
    t = last.lower()

    # ── Saluti ────────────────────────────────────────────────────────────────
    greet_words = ["ciao", "salve", "buongiorno", "buonasera", "hello", "hi",
                   "hallo", "bonjour", "hey", "salut", "guten tag", "good morning"]
    if any(t.startswith(w) or t == w for w in greet_words) and len(t) < 30:
        return _cb_t(lang, "greeting")

    # ── Chi siete / cosa è STH ────────────────────────────────────────────────
    about_kw = ["cos'è", "cosa è", "che cos", "che cosa", "cos e", "what is",
                "was ist", "qu'est", "qu est", "swiss tariff hub", "sth",
                "cosa fa", "what does", "was macht", "à quoi", "a cosa serve",
                "descri", "presentati", "presentate", "tell me about", "erzähl",
                "parlez"]
    if any(k in t for k in about_kw):
        return _cb_t(lang, "about")

    # ── Piani e prezzi ────────────────────────────────────────────────────────
    plan_kw = ["piano", "piani", "plan", "pläne", "plans", "free", "pro",
               "costo", "costa", "kostet", "coûte", "price", "preis", "prix",
               "quanto", "how much", "wie viel", "combien", "abbonamento",
               "subscription", "abonnement", "upgrade", "differenza", "difference",
               "unterschied", "différence", "19 chf", "chf 19"]
    if any(k in t for k in plan_kw):
        # Se chiede specificamente il dettaglio prezzi
        if any(k in t for k in ["quanto", "how much", "wie viel", "combien", "costo", "kostet", "coûte", "19"]):
            return _cb_t(lang, "pricing_detail")
        return _cb_t(lang, "plans")

    # ── Provider specifici ─────────────────────────────────────────────────────
    provider_map = {
        "ckw":          "ckw",
        "ekz einsiedeln": "ekz_einsiedeln",
        "ekz":          "ekz",
        "aem":          "aem",
        "massagno":     "aem",
        "groupe e":     "groupe_e",
        "groupe-e":     "groupe_e",
        "groupe_e":     "groupe_e",
        "primeo":       "primeo",
        "avag":         "avag",
        "aare":         "avag",
        "elag":         "elag",
        "gretzenbach":  "elag",
        "ail":          "ail",
        "lugano":       "ail",
        "ega":          "ega",
        "aettenschwil": "ega",
    }
    matched_provider = None
    for kw, pk in provider_map.items():
        if kw in t:
            matched_provider = pk
            break

    # ── API / URL / endpoint ──────────────────────────────────────────────────
    api_kw = ["api", "url", "endpoint", "link", "indirizzo", "address",
              "adresse", "adres", "curl", "request", "richiesta", "anfrage",
              "requête", "http", "https", "tariff_id", "mostra api", "show api",
              "zeig api", "montrer api"]
    wants_api = any(k in t for k in api_kw)

    if matched_provider and wants_api:
        return _build_providers_response(lang, matched_provider)

    if matched_provider:
        return _build_providers_response(lang, matched_provider)

    # ── Tutti i provider ──────────────────────────────────────────────────────
    all_prov_kw = ["provider", "fornitori", "fornitore", "anbieter", "fournisseur",
                   "evu", "tutti", "all", "alle", "tous", "lista", "list", "elenco",
                   "welche", "quali", "quels", "which", "supported"]
    if any(k in t for k in all_prov_kw):
        return _build_providers_response(lang)

    # ── Endpoint API ──────────────────────────────────────────────────────────
    if wants_api and not matched_provider:
        return _build_endpoints_response(lang)

    # ── Registrazione ─────────────────────────────────────────────────────────
    reg_kw = ["registra", "register", "registrierung", "inscription", "accesso",
              "access", "zugang", "accès", "chiave", "key", "api key", "come si accede",
              "how to", "wie kann", "comment", "iniziare", "start", "anfangen",
              "commencer", "inizia", "begin"]
    if any(k in t for k in reg_kw):
        return _cb_t(lang, "register")

    # ── Sicurezza ─────────────────────────────────────────────────────────────
    sec_kw = ["sicurezza", "security", "sicherheit", "sécurité", "auth",
              "autenticazione", "authentication", "header", "bearer", "token",
              "hash", "sha", "protetto", "protect"]
    if any(k in t for k in sec_kw):
        return _cb_t(lang, "security")

    # ── Orari aggiornamento ────────────────────────────────────────────────────
    update_kw = ["aggiornamento", "aggiorna", "update", "aktualisierung", "mise à jour",
                 "quando", "when", "wann", "quand", "orario", "schedule", "zeitplan",
                 "horaire", "11:05", "17:30", "utc", "fetch", "domani", "tomorrow",
                 "morgen", "demain"]
    if any(k in t for k in update_kw):
        return _cb_t(lang, "update_times")

    # ── Formato dati / DST ────────────────────────────────────────────────────
    fmt_kw = ["formato", "format", "slot", "15 min", "15min", "kwh", "chf/kwh",
              "energy_price", "grid_price", "residual", "struttura", "structure",
              "aufbau", "json", "response", "risposta"]
    if any(k in t for k in fmt_kw):
        return _cb_t(lang, "data_format")

    dst_kw = ["dst", "ora legale", "sommerzeit", "heure d'été", "daylight",
              "92 slot", "100 slot", "96 slot", "transizione", "transition",
              "primavera", "autunno", "spring", "autumn", "frühling", "herbst"]
    if any(k in t for k in dst_kw):
        return _cb_t(lang, "dst")

    # ── Contatti ──────────────────────────────────────────────────────────────
    contact_kw = ["contatto", "contatti", "contact", "kontakt", "email", "mail",
                  "support", "assistenza", "hilfe", "aide", "help", "scrivere",
                  "write", "schreiben", "écrire"]
    if any(k in t for k in contact_kw):
        return _cb_t(lang, "contact")

    # ── Fallback ──────────────────────────────────────────────────────────────
    return _cb_t(lang, "unknown")


# ── Funzione per registrare tutte le route su un'app FastAPI ─────────────────

def _resolve_user(request: Request):
    """
    Legge X-API-Key dalla richiesta e restituisce un dict con:
      plan, free_tariff_id, email
    Se la chiave manca o è invalida, restituisce piano 'guest'.
    Usato dagli endpoint /internal/* per applicare restrizioni server-side.
    """
    import hashlib as _hashlib
    raw_key = request.headers.get("X-API-Key", "").strip()
    if not raw_key:
        return {"plan": "guest", "free_tariff_id": None, "email": None}
    try:
        from api_keys.registration import ApiKey, _hash_key
        from database import init_db
        key_hash = _hash_key(raw_key)
        SessionLocal = init_db()
        with SessionLocal() as session:
            record = session.query(ApiKey).filter(
                ApiKey.api_key_hash == key_hash,
                ApiKey.status == "active"
            ).first()
            if not record:
                return {"plan": "guest", "free_tariff_id": None, "email": None}
            return {
                "plan":           getattr(record, "plan", "free") or "free",
                "free_tariff_id": getattr(record, "free_tariff_id", None),
                "email":          record.email,
            }
    except Exception:
        return {"plan": "guest", "free_tariff_id": None, "email": None}


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

    # ── POST /api/v1/chat — Chatbot rule-based ────────────────────────────────
    @app.post("/api/v1/chat")
    async def chat_endpoint(req: ChatRequest):
        """
        Chatbot TF-IDF locale (chatbot_engine.py).
        Risponde in IT/DE/FR/EN. Nessuna API key esterna richiesta.
        """
        import sys as _sys, os as _os
        _here = _os.path.dirname(_os.path.abspath(__file__))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        from chatbot_engine import smart_reply
        messages = [{"role": m.role, "content": m.content} for m in req.messages]
        reply = smart_reply(messages, req.lang)
        return JSONResponse({"reply": reply})

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

            # Genera un pending token — non sovrascrive ancora il DB.
            # Se c'era già una sessione attiva, il client mostra un avviso di conferma.
            # Solo dopo conferma (POST /confirm-login) il token viene salvato nel DB.
            pending_token        = _secrets.token_urlsafe(32)
            had_previous_session = bool(getattr(record, "session_token", None))

            return {
                "valid":                True,
                "session_token":        pending_token,
                "had_previous_session": had_previous_session,
                "user": {
                    "full_name":        record.full_name,
                    "email":            record.email,
                    "company":          record.company,
                    "country":          record.country,
                    "key_prefix":       record.key_prefix,
                    "requests_today":   record.requests_today,
                    "rate_limit_day":   record.rate_limit_day,
                    "requests_total":   record.requests_total,
                    "plan":             getattr(record, "plan", "free"),
                    "rate_limit_min":   getattr(record, "rate_limit_min", 4),
                    "free_tariff_id":        getattr(record, "free_tariff_id", None),
                    "tariff_choice_locked":  bool(getattr(record, "tariff_choice_locked", False)),
                },
            }


    # ── POST /api/v1/confirm-login ───────────────────────────────────────────
    @app.post("/api/v1/confirm-login")
    async def confirm_login(req: ValidateKeyRequest):
        """
        Finalizza il login sovrascrivendo il session_token nel DB.
        Chiamato dal frontend dopo che l'utente ha confermato il login
        su un nuovo dispositivo (era già loggato altrove).
        Richiede: {api_key: "stk_...", session_token: "il pending token"}
        """
        import hashlib as _hashlib
        from database import init_db
        from api_keys.registration import ApiKey, _hash_key

        if not req.api_key or not req.session_token:
            raise HTTPException(400, "api_key and session_token required")

        SessionLocal = init_db()
        with SessionLocal() as session:
            key_hash = _hash_key(req.api_key.strip())
            record   = session.query(ApiKey).filter(
                ApiKey.api_key_hash == key_hash
            ).first()
            if not record or record.status != "active":
                raise HTTPException(401, "Invalid key")
            record.session_token = req.session_token
            session.commit()
        return {"confirmed": True}

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

            # Se il client manda un session_token, verifica che corrisponda al DB.
            # Token diverso = altro dispositivo ha fatto login → slogga questo.
            if req.session_token:
                stored_token = getattr(record, "session_token", None)
                if stored_token and stored_token != req.session_token:
                    return {"valid": False, "status": "session_invalid"}

            return {
                "valid":                 record.status == "active",
                "status":                record.status,
                "requests_today":        record.requests_today,
                "rate_limit_day":        record.rate_limit_day,
                "requests_total":        record.requests_total,
                "plan":                  getattr(record, "plan", "free"),
                "free_tariff_id":        getattr(record, "free_tariff_id", None),
                "tariff_choice_locked":  bool(getattr(record, "tariff_choice_locked", False)),
            }

    # ── POST /api/v1/upgrade-to-pro ───────────────────────────────────────────
    @app.post("/api/v1/upgrade-to-pro")
    async def upgrade_to_pro(req: ValidateKeyRequest):
        """
        Endpoint chiamato dal form di pagamento nel frontend (loggato).
        Simula pagamento riuscito, upgrada piano a Pro, invia receipt + invoice PDF.

        Richiede: {api_key: "stk_..."}
        Returns: {success: bool, plan: "pro", message: str}
        """
        from database import init_db
        from api_keys.registration import ApiKey, _hash_key, update_key

        PRO_RATE_LIMIT = 2000   # richieste/giorno piano Pro

        if not req.api_key.strip():
            raise HTTPException(400, "api_key required")

        SessionLocal = init_db()
        with SessionLocal() as session:
            key_hash = _hash_key(req.api_key.strip())
            record   = session.query(ApiKey).filter(
                ApiKey.api_key_hash == key_hash
            ).first()

            if not record:
                raise HTTPException(404, "Key not found")
            if record.status != "active":
                raise HTTPException(403, f"Key status: {record.status}")

            current_plan = getattr(record, "plan", "free") or "free"
            if current_plan == "pro":
                return {"success": True, "plan": "pro", "already_pro": True,
                        "message": "Account already on Pro plan."}

            # Esegui upgrade — imposta anche rate_limit_day Pro
            record, upgraded, _downgraded = update_key(
                session, record.id,
                plan="pro",
                rate_limit_day=PRO_RATE_LIMIT,
            )

        # Invia email con receipt + fattura PDF in background (non blocca la risposta)
        if upgraded:
            import threading
            def _send_email():
                try:
                    from api_keys.email_templates import send_pro_upgrade
                    from datetime import datetime, timezone as tz
                    send_pro_upgrade(
                        email          = record.email,
                        full_name      = record.full_name,
                        key_prefix     = record.key_prefix,
                        rate_limit     = record.rate_limit_day,
                        pro_since      = record.approved_at or datetime.now(tz.utc),
                        company        = record.company,
                        vat_number     = getattr(record, "vat_number", None),
                        address_line1  = None,
                        address_line2  = None,
                        country        = getattr(record, "country", "CH") or "CH",
                        payment_method = "Card",
                        lang           = record.preferred_lang or "en",
                    )
                    log.info(f"[upgrade-to-pro] Email Pro inviata a {record.email}")
                except Exception as e:
                    import traceback
                    log.error(f"[upgrade-to-pro] Email non inviata per {record.email}: {e}\n{traceback.format_exc()}")
            threading.Thread(target=_send_email, daemon=True).start()

        return {
            "success":        True,
            "plan":           "pro",
            "already_pro":    False,
            "rate_limit_day": record.rate_limit_day,
            "message":        "Upgrade successful.",
        }

    # ── POST /api/v1/me/tariff ────────────────────────────────────────────────
    @app.post("/api/v1/me/tariff")
    async def set_free_tariff(req: ValidateKeyRequest, tariff_id: str = Query(...)):
        """
        Salva la tariffa default scelta dall'utente free.
        Può essere chiamato una sola volta — successivamente può essere
        cambiato solo da admin o se l'utente upgrada a Pro.
        Richiede: header X-API-Key + ?tariff_id=...
        """
        from database import init_db, get_active_tariffs
        from api_keys.registration import ApiKey, _hash_key, update_key

        if not req.api_key.strip():
            raise HTTPException(400, "api_key required")

        SessionLocal = init_db()
        with SessionLocal() as session:
            key_hash = _hash_key(req.api_key.strip())
            record   = session.query(ApiKey).filter(
                ApiKey.api_key_hash == key_hash
            ).first()
            if not record or record.status != "active":
                raise HTTPException(401, "Invalid or inactive key")

            # Blocca se già bloccata (scelta irrevocabile per utenti free)
            if getattr(record, "tariff_choice_locked", False) and record.plan == "free":
                raise HTTPException(403, "Tariff choice is locked for free plan. Upgrade to Pro to change.")

            # Verifica che il tariff_id esista
            active_ids = {t.tariff_id for t in get_active_tariffs(session)}
            if tariff_id not in active_ids:
                raise HTTPException(404, f"Tariff '{tariff_id}' not found or inactive")

            # Salva e blocca
            record.free_tariff_id = tariff_id
            record.tariff_choice_locked = True
            session.commit()

        return {"success": True, "free_tariff_id": tariff_id, "tariff_choice_locked": True}

    # ═══════════════════════════════════════════════════════════════════════════
    # ENDPOINT INTERNI — /internal/*
    # Protetti da X-Dashboard-Secret. Usati SOLO dalla dashboard.
    # Non richiedono API key utente. Non contano nel rate limit.
    # ═══════════════════════════════════════════════════════════════════════════

    @app.get("/internal/verify-pro")
    def internal_verify_pro(request: Request):
        """Gate server-side Pro per la ricerca mappa."""
        u = _resolve_user(request)
        return {"pro": u["plan"] == "pro", "plan": u["plan"]}

    @app.get("/internal/tariffs")
    def internal_tariffs():
        """Lista completa tariffe — usata dalla dashboard per mappa e grafici."""
        import json as _json
        from database import init_db, get_active_tariffs

        def expand_zip_ranges(raw: list) -> list:
            result = []
            for z in raw:
                if isinstance(z, int):
                    result.append(z)
                elif isinstance(z, list) and len(z) == 2:
                    result.extend(range(int(z[0]), int(z[1]) + 1))
                elif isinstance(z, str):
                    z = z.strip()
                    if "-" in z:
                        parts = z.split("-")
                        try: result.extend(range(int(parts[0]), int(parts[1]) + 1))
                        except ValueError: pass
                    else:
                        try: result.append(int(z))
                        except ValueError: pass
            return sorted(set(result))

        SessionLocal = init_db()
        with SessionLocal() as session:
            tariffs = get_active_tariffs(session)
        return [
            {
                "tariff_id":                   t.tariff_id,
                "tariff_name":                 t.tariff_name,
                "provider_name":               t.provider_name,
                "zip_ranges":                  expand_zip_ranges(
                    _json.loads(t.full_config_json).get("zip_ranges", [])
                ),
                "daily_update_time_utc":       t.daily_update_time_utc,
                "datetime_available_from_utc": (
                    t.datetime_available_from.isoformat() if t.datetime_available_from else None
                ),
                "valid_until_utc":             (t.valid_until.isoformat() if t.valid_until else None),
                "sgr_compliant":               t.sgr_compliant,
                "dynamic_elements":            _json.loads(t.dynamic_elements_json),
                "time_resolution_minutes":     t.time_resolution_minutes,
            }
            for t in tariffs
        ]

    @app.get("/internal/prices/all")
    def internal_prices_all(
        request:    Request,
        start_time: str = Query(...),
        end_time:   Optional[str] = Query(None),
        tariff_id:  Optional[str] = Query(None),
    ):
        """
        Prezzi delle tariffe — usato dalla dashboard per grafici e mappa.
        Restrizioni server-side:
          - tariff_id specificato (grafico singolo Data page):
              guest → solo prime 2 tariffe + solo oggi
              free  → solo la propria free_tariff_id + solo oggi
              pro   → qualsiasi tariffa, qualsiasi data
          - tariff_id assente (tutte le tariffe insieme / mappa): solo Pro
        """
        from datetime import datetime, timedelta, timezone
        from database import init_db, get_active_tariffs, get_prices

        from schemas import today_ch as _today_ch
        user = _resolve_user(request)
        plan = user["plan"]

        # Richiesta "tutte le tariffe" → solo Pro
        if not tariff_id and plan != "pro":
            raise HTTPException(403, "Pro plan required to fetch all tariffs at once")

        try:
            import zoneinfo; tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
        except ImportError:
            import pytz; tz_ch = pytz.timezone("Europe/Zurich")

        try:
            start_utc = datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            raise HTTPException(400, f"Invalid start_time: {start_time!r}")

        if end_time:
            try:
                end_utc = datetime.fromisoformat(end_time.replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                raise HTTPException(400, f"Invalid end_time: {end_time!r}")
        else:
            end_utc = start_utc + timedelta(days=1)

        start_ld = start_utc.astimezone(tz_ch).date()
        end_ld   = end_utc.astimezone(tz_ch).date()
        start_db = datetime(start_ld.year, start_ld.month, start_ld.day, tzinfo=tz_ch).astimezone(timezone.utc)
        end_db   = datetime(end_ld.year, end_ld.month, end_ld.day, tzinfo=tz_ch).astimezone(timezone.utc)
        if end_db <= start_db:
            end_db = (datetime(end_ld.year, end_ld.month, end_ld.day, tzinfo=tz_ch) + timedelta(days=1)).astimezone(timezone.utc)
        end_ld_filter = start_ld + timedelta(days=1) if end_ld <= start_ld else end_ld

        # Protezione server-side per non-pro: blocca giorni passati su tariffe non accessibili
        _today_date = _today_ch()
        is_past     = start_ld < _today_date
        if plan != "pro" and is_past and tariff_id:
            free_id = user["free_tariff_id"] if plan == "free" else None
            if plan == "free" and tariff_id != free_id:
                raise HTTPException(403, "Past data for this tariff requires Pro plan")
            if plan == "guest":
                SessionLocal0 = init_db()
                with SessionLocal0() as _s:
                    _all_ids = [t.tariff_id for t in get_active_tariffs(_s)]
                if tariff_id not in set(_all_ids[:2]):
                    raise HTTPException(403, "Past data requires login")

        result = {}
        SessionLocal = init_db()
        with SessionLocal() as session:
            for tariff in get_active_tariffs(session):
                # Se è richiesta una sola tariffa, salta le altre
                if tariff_id and tariff.tariff_id != tariff_id:
                    continue
                # Guest: solo prime 2 tariffe
                if plan == "guest" and not tariff_id:
                    pass  # "tutte" già bloccato sopra per non-pro
                slots = get_prices(session, tariff.tariff_id, start_db, end_db)
                slots = [s for s in slots if start_ld <= s.slot_start_utc.astimezone(tz_ch).date() < end_ld_filter]
                if not slots: continue
                energy_s, grid_s, residual_s = [], [], []
                for s in slots:
                    dt = s.slot_start_utc
                    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                    ts = dt.isoformat()
                    if s.energy_price   is not None: energy_s.append({ts: s.energy_price})
                    if s.grid_price     is not None: grid_s.append({ts: s.grid_price})
                    if s.residual_price is not None: residual_s.append({ts: s.residual_price})
                td: dict = {"slot_count": len(slots)}
                if energy_s:   td["energy_price_utc"]   = energy_s
                if grid_s:     td["grid_price_utc"]     = grid_s
                if residual_s: td["residual_price_utc"] = residual_s
                result[tariff.tariff_id] = td

        if not result:
            raise HTTPException(404, "No data for this range.")
        return {"start_time": start_db.isoformat(), "end_time": end_db.isoformat(), "tariffs": result}

    @app.get("/internal/smart")
    def internal_smart(request: Request, date_str: Optional[str] = Query(None),
                       tariff_id: Optional[str] = Query(None)):
        """
        Smart summary per la dashboard.
        Se tariff_id specificato: restituisce solo quella tariffa con detail completo
        (usato per cambio rapido lato client senza ricaricare tutto).
        """
        import time as _time
        from datetime import date, datetime, timedelta, timezone
        from database import init_db, get_active_tariffs, get_prices, PriceSlotDB

        try:
            import zoneinfo; tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
        except ImportError:
            import pytz; tz_ch = pytz.timezone("Europe/Zurich")

        from schemas import today_ch as _today_ch
        target_date = date.fromisoformat(date_str) if date_str else _today_ch()

        now_utc = datetime.now(timezone.utc)
        local_midnight = datetime(target_date.year, target_date.month, target_date.day, tzinfo=tz_ch)
        start_utc  = local_midnight.astimezone(timezone.utc)
        end_utc    = (local_midnight + timedelta(days=1)).astimezone(timezone.utc)
        month_start = datetime(target_date.year, target_date.month, 1, tzinfo=timezone.utc)

        SessionLocal = init_db()
        with SessionLocal() as session:
            tariffs = get_active_tariffs(session)
            from adapters import list_available_adapters
            available = set(list_available_adapters())
            result_tariffs = []
            for t in tariffs:
                # Se richiesta singola tariffa, salta le altre
                if tariff_id and t.tariff_id != tariff_id:
                    continue
                day_slots = get_prices(session, t.tariff_id, start_utc, end_utc)
                if not day_slots:
                    result_tariffs.append({
                        "tariff_id": t.tariff_id, "provider_name": t.provider_name,
                        "tariff_name": t.tariff_name, "has_data": False,
                        "adapter_ready": t.adapter_class in available,
                    }); continue

                slots_data = []
                for s in day_slots:
                    dt = s.slot_start_utc
                    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                    total = sum(p for p in [s.energy_price, s.grid_price, s.residual_price] if p)
                    if total > 0:
                        slots_data.append({"ts": dt, "total": total,
                            "grid": s.grid_price or 0, "energy": s.energy_price or 0,
                            "residual": s.residual_price or 0})
                if not slots_data: continue

                day_avg = sum(s["total"] for s in slots_data) / len(slots_data)
                month_slots = (
                    session.query(PriceSlotDB.energy_price, PriceSlotDB.grid_price, PriceSlotDB.residual_price)
                    .filter(PriceSlotDB.tariff_id == t.tariff_id,
                            PriceSlotDB.slot_start_utc >= month_start,
                            PriceSlotDB.slot_start_utc < end_utc).all()
                )
                month_totals = [sum(p for p in [e, g, r] if p) for e, g, r in month_slots
                                if sum(p for p in [e, g, r] if p) > 0]
                month_avg = sum(month_totals) / len(month_totals) if month_totals else day_avg
                current_slot = next((s for s in reversed(slots_data) if s["ts"] <= now_utc), None)
                if not current_slot: current_slot = slots_data[0]
                ratio  = current_slot["total"] / month_avg if month_avg > 0 else 1
                signal = "green" if ratio < 0.85 else "red" if ratio >= 1.15 else "yellow"
                sorted_asc  = sorted(slots_data, key=lambda x: x["total"])
                sorted_desc = sorted(slots_data, key=lambda x: x["total"], reverse=True)

                def fmt_slot(s):
                    local_t = s["ts"].astimezone(tz_ch)
                    return {"time": local_t.strftime("%H:%M"),
                            "total_rp": round(s["total"]*100, 2),
                            "grid_rp":  round(s["grid"]*100, 2),
                            "energy_rp": round(s["energy"]*100, 2),
                            "residual_rp": round(s["residual"]*100, 2)}

                hourly = {}
                for s in slots_data:
                    h = s["ts"].astimezone(tz_ch).hour
                    hourly.setdefault(h, []).append(s["total"])
                hourly_avg = [
                    round(sum(hourly[h])/len(hourly[h])*100, 2) if h in hourly else None
                    for h in range(24)
                ]
                # Dati di base sempre visibili (usati nel confronto pubblico)
                base_entry = {
                    "tariff_id": t.tariff_id, "provider_name": t.provider_name,
                    "tariff_name": t.tariff_name, "has_data": True,
                    "adapter_ready": t.adapter_class in available,
                    "day_avg_rp":   round(day_avg*100, 2),
                    "month_avg_rp": round(month_avg*100, 2),
                }
                # Campi sensibili: solo per la tariffa accessibile all'utente
                user_smart = _resolve_user(request)
                plan_smart = user_smart["plan"]
                # Free senza tariffa scelta: nessun detail per nessuna tariffa
                _free_tariff = user_smart.get("free_tariff_id")
                is_accessible = (
                    plan_smart == "pro" or
                    (plan_smart == "free" and _free_tariff and t.tariff_id == _free_tariff)
                )
                if is_accessible:
                    base_entry.update({
                        "signal": signal,
                        "current_price_rp": round(current_slot["total"]*100, 2),
                        "best_slots":  [fmt_slot(s) for s in sorted_asc[:3]],
                        "worst_slots": [fmt_slot(s) for s in sorted_desc[:2]],
                        "hourly_rp":   hourly_avg,
                    })
                result_tariffs.append(base_entry)

        return {"date": target_date.isoformat(), "tariffs": result_tariffs}

    @app.get("/internal/summary/daily")
    def internal_summary_daily(
        request:   Request,
        tariff_id: str = Query(...),
        days:      int = Query(5, ge=1, le=30),
    ):
        """Daily summary per dashboard — protetto per piano."""
        user = _resolve_user(request)
        plan = user["plan"]
        # guest: solo prime 2 tariffe, solo oggi (days=1 forzato lato query, qui blocchiamo tariffe)
        # free: solo la propria free_tariff_id, solo oggi
        # pro: nessuna restrizione
        if plan == "pro":
            pass  # tutto libero
        elif plan == "free":
            pass  # free vede i pill di tutti i giorni (dati bloccati lato client/prices-all)
        else:  # guest
            pass  # guest vede i pill di tutti i giorni (solo oggi sbloccato lato prices-all)
        from datetime import datetime, timedelta, timezone
        from database import init_db, get_prices, PriceSlotDB
        from collections import defaultdict

        try:
            import zoneinfo; tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
        except ImportError:
            import pytz; tz_ch = pytz.timezone("Europe/Zurich")

        from schemas import today_ch as _today_ch
        today_local = _today_ch()

        SessionLocal = init_db()
        with SessionLocal() as session:
            from database import Tariff
            if not session.get(Tariff, tariff_id):
                raise HTTPException(404, f"Tariff '{tariff_id}' not found")
            cutoff = datetime.now(timezone.utc) - timedelta(days=days + 1)
            rows = (
                session.query(PriceSlotDB.slot_start_utc, PriceSlotDB.energy_price,
                              PriceSlotDB.grid_price, PriceSlotDB.residual_price)
                .filter(PriceSlotDB.tariff_id == tariff_id, PriceSlotDB.slot_start_utc >= cutoff)
                .order_by(PriceSlotDB.slot_start_utc).all()
            )
        if not rows:
            raise HTTPException(404, "No data available")

        by_date: dict = defaultdict(list)
        for (dt, energy, grid, residual) in rows:
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            local_date = dt.astimezone(tz_ch).date()
            if local_date > today_local: continue
            total = sum(p for p in [energy, grid, residual] if p)
            if total > 0: by_date[local_date.isoformat()].append((total, dt))

        result = []
        for d in sorted(sorted(by_date.keys(), reverse=True)[:days]):
            slots = by_date[d]
            if not slots: continue
            avg = sum(t for t, _ in slots) / len(slots)
            min_val, min_dt = min(slots, key=lambda x: x[0])
            max_val, max_dt = max(slots, key=lambda x: x[0])
            result.append({
                "date": d, "slot_count": len(slots),
                "avg_rp": round(avg * 100, 2),
                "min_rp": round(min_val * 100, 2), "min_time": min_dt.strftime("%H:%M"),
                "max_rp": round(max_val * 100, 2), "max_time": max_dt.strftime("%H:%M"),
            })
        return result

    @app.get("/internal/summary/monthly")
    def internal_summary_monthly(
        request:   Request,
        tariff_id: str = Query(...),
        months:    int = Query(3, ge=1, le=6),
    ):
        """Monthly summary per dashboard — protetto per piano."""
        user = _resolve_user(request)
        plan = user["plan"]
        if plan == "pro":
            pass
        elif plan == "free":
            pass  # monthly overlay gestito client-side per tariffe non proprie
        else:  # guest
            pass  # monthly overlay gestito client-side
        import time
        from datetime import datetime, timedelta, timezone
        from database import init_db, PriceSlotDB
        from collections import defaultdict

        cache_key = f"internal_monthly:{tariff_id}:{months}"
        _internal_cache = getattr(internal_summary_monthly, "_cache", {})
        cached = _internal_cache.get(cache_key)
        if cached and time.time() - cached["ts"] < 300:
            return cached["data"]

        SessionLocal = init_db()
        with SessionLocal() as session:
            from database import Tariff
            if not session.get(Tariff, tariff_id):
                raise HTTPException(404, f"Tariff '{tariff_id}' not found")
            cutoff = datetime.now(timezone.utc) - timedelta(days=months * 32)
            rows = (
                session.query(PriceSlotDB.slot_start_utc, PriceSlotDB.energy_price,
                              PriceSlotDB.grid_price, PriceSlotDB.residual_price)
                .filter(PriceSlotDB.tariff_id == tariff_id, PriceSlotDB.slot_start_utc >= cutoff)
                .order_by(PriceSlotDB.slot_start_utc).all()
            )
        if not rows:
            raise HTTPException(404, "No data available")

        by_month: dict = defaultdict(list)
        for (dt, energy, grid, residual) in rows:
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            month_key = dt.strftime("%Y-%m")
            total = sum(p for p in [energy, grid, residual] if p)
            if total > 0: by_month[month_key].append((total, dt))

        now = datetime.now(timezone.utc)
        result = []
        for m in sorted(sorted(by_month.keys(), reverse=True)[:months]):
            slots = by_month[m]
            if not slots: continue
            avg = sum(t for t, _ in slots) / len(slots)
            min_val, min_dt = min(slots, key=lambda x: x[0])
            max_val, max_dt = max(slots, key=lambda x: x[0])
            result.append({
                "month": m, "month_label": datetime.strptime(m, "%Y-%m").strftime("%B %Y"),
                "is_current": m == now.strftime("%Y-%m"),
                "days_with_data": len({dt.date() for _, dt in slots}),
                "slot_count": len(slots),
                "avg_rp": round(avg * 100, 2),
                "min_rp": round(min_val * 100, 2), "min_date": min_dt.strftime("%d/%m %H:%M"),
                "max_rp": round(max_val * 100, 2), "max_date": max_dt.strftime("%d/%m %H:%M"),
            })
        if not hasattr(internal_summary_monthly, "_cache"):
            internal_summary_monthly._cache = {}
        internal_summary_monthly._cache[cache_key] = {"ts": time.time(), "data": result}
        return result

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
                record, upgraded_to_pro, downgraded_to_free = update_key(
                    session, key_id, req.rate_limit_day, req.notes, req.plan
                )

            # Email upgrade a Pro — in thread background
            if upgraded_to_pro:
                import threading
                def _send_upgrade():
                    try:
                        from api_keys.email_templates import send_pro_upgrade
                        send_pro_upgrade(
                            email          = record.email,
                            full_name      = record.full_name,
                            key_prefix     = record.key_prefix,
                            rate_limit     = record.rate_limit_day,
                            pro_since      = record.approved_at,
                            company        = record.company,
                            vat_number     = getattr(record, "vat_number", None),
                            country        = getattr(record, "country", "CH"),
                            lang           = record.preferred_lang or "en",
                        )
                    except Exception as e:
                        import traceback
                        log.error(f"[admin_update] Email Pro non inviata per {record.email}: {e}\n{traceback.format_exc()}")
                threading.Thread(target=_send_upgrade, daemon=True).start()

            # Email downgrade a Free — in thread background
            if downgraded_to_free:
                import threading
                def _send_downgrade():
                    try:
                        from api_keys.email_templates import send_pro_downgrade
                        send_pro_downgrade(
                            email      = record.email,
                            full_name  = record.full_name,
                            rate_limit = record.rate_limit_day,
                            lang       = record.preferred_lang or "en",
                        )
                    except Exception as e:
                        import traceback
                        log.error(f"[admin_update] Email downgrade non inviata per {record.email}: {e}\n{traceback.format_exc()}")
                threading.Thread(target=_send_downgrade, daemon=True).start()

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
        "plan":             getattr(k, "plan", "free") or "free",
        "pro_since":        k.approved_at.isoformat() if k.approved_at and getattr(k, "plan", "free") == "pro" else None,
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
  /* Piano badges admin */
  .plan-free{{font-size:10px;padding:2px 8px;border-radius:99px;font-weight:600;
              background:#2a2a20;color:#888780}}
  .plan-pro{{font-size:10px;padding:2px 8px;border-radius:99px;font-weight:600;
             background:#0a1929;color:#185fa5}}
  /* Plan toggle inline */
  .plan-toggle{{font-size:11px;padding:3px 8px;border-radius:5px;border:0.5px solid #333;
                background:#0f1117;color:#888;cursor:pointer;transition:all .15s}}
  .plan-toggle:hover{{border-color:#185fa5;color:#185fa5}}

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
          <th>Piano</th>
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

    const actions  = _buildActions(k);
    const plan     = k.plan || 'free';
    const planCls  = plan === 'pro' ? 'plan-pro' : 'plan-free';
    const planLbl  = plan === 'pro' ? 'Pro' : 'Free';
    const planToggleLbl = plan === 'pro' ? 'Downgrade' : 'Upgrade';

    return `<tr id="row-${{k.id}}">
      <td><strong style="color:white">${{k.full_name}}</strong>${{company}}</td>
      <td style="color:#888">${{k.email}}</td>
      <td style="color:#555">${{k.country}}</td>
      <td>${{keyDisp}}</td>
      <td><span class="${{sCls}}">${{k.status}}</span></td>
      <td>
        <span class="${{planCls}}">${{planLbl}}</span>
        ${{k.status === 'active' ? `<button class="plan-toggle" style="margin-top:4px;display:block" onclick="togglePlan('${{k.id}}','${{plan}}')">${{planToggleLbl}}</button>` : ''}}
      </td>
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

async function togglePlan(id, currentPlan) {{
  const newPlan = currentPlan === 'pro' ? 'free' : 'pro';
  const label   = newPlan === 'pro' ? 'upgrade to Pro' : 'downgrade to Free';
  if (!confirm(`Change plan to ${{newPlan.toUpperCase()}} for this user? This will also update the rate limit.`)) return;
  const r = await fetch(`/api/v1/admin/keys/${{id}}`, {{
    method: 'PATCH', credentials: 'include',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{plan: newPlan}}),
  }});
  if (r.ok) {{
    showToast(`✅ Piano aggiornato a ${{newPlan.toUpperCase()}}`);
    loadKeys(currentTab);
  }} else {{
    const d = await r.json().catch(()=>({{}}));
    showToast('❌ ' + (d.detail||'Errore'), true);
  }}
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