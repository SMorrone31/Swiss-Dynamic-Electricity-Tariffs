"""
scheduler.py
============
Scheduler giornaliero automatico per Swiss Tariff Hub.

Flusso giornaliero (ora locale CET/CEST):
  12:05 CET → Fetch CKW (home + business) per DOMANI
               Retry: 12:35, poi 13:35
               Se tutti falliti: EMAIL ❌
  18:30 CET → Fetch tutti gli altri EVU per DOMANI
               Retry: 19:00, poi 20:00
               Se tutti falliti: EMAIL ❌
  10:00 CET → Health check: i dati di OGGI sono nel DB?
               Se mancano: EMAIL ⚠️
               (cattura il caso "server era spento ieri")

Validazione post-fetch:
  Dopo ogni fetch controlla slot count, prezzi a zero/negativi,
  tutti prezzi identici (possibile errore API).
  Se anomalie: EMAIL ⚠️

Configurazione (.env nella cartella del progetto):
  ALERT_EMAIL=simomorro24@gmail.com   # destinatario alert
  EMAIL_LANG=it                       # lingua email: en / de / fr / it
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=simomorro24@gmail.com
  SMTP_PASSWORD=xxxxxxxxxxxxxxxx      # App Password Gmail (16 caratteri, no spazi)

Uso CLI:
  python scheduler.py                                      # avvia scheduler
  python scheduler.py --now                                # fetch tutte le tariffe oggi
  python scheduler.py --tariff ckw_home_dynamic            # fetch singola tariffa
  python scheduler.py --tariff ckw_home_dynamic --date 2026-03-18
  python scheduler.py --health                             # health check manuale
  python scheduler.py --test-email                         # invia email di test
"""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

# ── Carica .env automaticamente ───────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scheduler")


# ── Config ────────────────────────────────────────────────────────────────────

RETRY_DELAYS       = [0, 30 * 60, 60 * 60]   # subito, +30 min, +60 min
EXPECTED_SLOTS_MIN = 90
EXPECTED_SLOTS_MAX = 102


# ── Traduzioni email ──────────────────────────────────────────────────────────

_T = {
    "en": {
        "fetch_failed_subject": "[Swiss Tariff Hub] ❌ Fetch failed — {tariff_id} ({date})",
        "fetch_failed_body": """\
Swiss Tariff Hub — Fetch Failed
================================================

Tariff:      {tariff_id}
Provider:    {provider}
Target date: {target_date}
Alert time:  {now_utc}

All {retries} fetch attempts failed.
{error_line}
What to do:
  1. Check that {provider} API is reachable:
     {api_url}
  2. Check the system logs for details
  3. Retry manually:
     python scheduler.py --tariff {tariff_id} --date {target_date}
""",
        "missing_subject": "[Swiss Tariff Hub] ⚠️  Missing data — {count} tariff(s) ({date})",
        "missing_body": """\
Swiss Tariff Hub — Missing Data
================================================

Check date: {check_date}
Alert time: {now_utc}

The following tariff(s) have NO data for today:
{tariff_lines}

Expected fetch schedule (CET):
  CKW:       12:05 (retry 12:35, 13:35)
  Other EVU: 18:30 (retry 19:00, 20:00)

What to do:
  1. Check the system logs
  2. Fetch all manually:
     python scheduler.py --now
  3. Or fetch a single tariff:
     python scheduler.py --tariff <tariff_id>
""",
        "anomaly_subject": "[Swiss Tariff Hub] ⚠️  Data anomaly — {tariff_id} ({date})",
        "anomaly_body": """\
Swiss Tariff Hub — Data Anomaly Detected
================================================

Tariff:      {tariff_id}
Provider:    {provider}
Target date: {target_date}
Alert time:  {now_utc}

Issues found:
{issue_lines}

The data has been saved but may be incomplete or incorrect.

What to do:
  1. Check the raw API response manually
  2. Refetch if needed:
     python scheduler.py --tariff {tariff_id} --date {target_date}
""",
        "test_subject": "[Swiss Tariff Hub] ✅ Email configuration test",
        "test_body": """\
Swiss Tariff Hub — Email Test
================================================

If you receive this email, your alert configuration is working correctly.

Alerts will be sent automatically when:
  ❌  A fetch fails after all retries (~13:35 CET for CKW)
  ⚠️   Data is missing during the morning health check (10:00 CET)
  ⚠️   Fetched data has anomalies (wrong slot count, zero prices, etc.)

Current schedule (CET):
  12:05 → Fetch CKW (retry 12:35, 13:35)
  18:30 → Fetch other EVU (retry 19:00, 20:00)
  10:00 → Health check
""",
    },
    "de": {
        "fetch_failed_subject": "[Swiss Tariff Hub] ❌ Abruf fehlgeschlagen — {tariff_id} ({date})",
        "fetch_failed_body": """\
Swiss Tariff Hub — Abruf fehlgeschlagen
================================================

Tarif:       {tariff_id}
Anbieter:    {provider}
Zieldatum:   {target_date}
Alarmzeit:   {now_utc}

Alle {retries} Abrufversuche sind fehlgeschlagen.
{error_line}
Was tun:
  1. Prüfen Sie, ob die {provider}-API erreichbar ist:
     {api_url}
  2. Systemprotokolle auf Details prüfen
  3. Manuell erneut versuchen:
     python scheduler.py --tariff {tariff_id} --date {target_date}
""",
        "missing_subject": "[Swiss Tariff Hub] ⚠️  Fehlende Daten — {count} Tarif(e) ({date})",
        "missing_body": """\
Swiss Tariff Hub — Fehlende Daten
================================================

Prüfdatum:  {check_date}
Alarmzeit:  {now_utc}

Folgende Tarife haben KEINE Daten für heute:
{tariff_lines}

Erwarteter Abrufplan (CET):
  CKW:        12:05 (Retry 12:35, 13:35)
  Andere EVU: 18:30 (Retry 19:00, 20:00)

Was tun:
  1. Systemprotokolle prüfen
  2. Alle manuell abrufen:
     python scheduler.py --now
  3. Einzelnen Tarif abrufen:
     python scheduler.py --tariff <tariff_id>
""",
        "anomaly_subject": "[Swiss Tariff Hub] ⚠️  Datenanomalie — {tariff_id} ({date})",
        "anomaly_body": """\
Swiss Tariff Hub — Datenanomalie erkannt
================================================

Tarif:       {tariff_id}
Anbieter:    {provider}
Zieldatum:   {target_date}
Alarmzeit:   {now_utc}

Gefundene Probleme:
{issue_lines}

Die Daten wurden gespeichert, könnten aber unvollständig oder falsch sein.

Was tun:
  1. API-Antwort manuell prüfen
  2. Bei Bedarf neu abrufen:
     python scheduler.py --tariff {tariff_id} --date {target_date}
""",
        "test_subject": "[Swiss Tariff Hub] ✅ E-Mail-Konfigurationstest",
        "test_body": """\
Swiss Tariff Hub — E-Mail-Test
================================================

Wenn Sie diese E-Mail erhalten, funktioniert die Alarmkonfiguration korrekt.

Alarme werden automatisch gesendet bei:
  ❌  Fehlgeschlagenem Abruf nach allen Versuchen (~13:35 CET bei CKW)
  ⚠️   Fehlenden Daten beim Gesundheitscheck (10:00 CET)
  ⚠️   Anomalien in abgerufenen Daten

Aktueller Zeitplan (CET):
  12:05 → CKW abrufen (Retry 12:35, 13:35)
  18:30 → Andere EVU (Retry 19:00, 20:00)
  10:00 → Gesundheitscheck
""",
    },
    "fr": {
        "fetch_failed_subject": "[Swiss Tariff Hub] ❌ Échec de collecte — {tariff_id} ({date})",
        "fetch_failed_body": """\
Swiss Tariff Hub — Échec de collecte
================================================

Tarif:        {tariff_id}
Fournisseur:  {provider}
Date cible:   {target_date}
Heure alerte: {now_utc}

Les {retries} tentatives de collecte ont échoué.
{error_line}
Que faire:
  1. Vérifier que l'API {provider} est accessible:
     {api_url}
  2. Consulter les journaux système
  3. Réessayer manuellement:
     python scheduler.py --tariff {tariff_id} --date {target_date}
""",
        "missing_subject": "[Swiss Tariff Hub] ⚠️  Données manquantes — {count} tarif(s) ({date})",
        "missing_body": """\
Swiss Tariff Hub — Données manquantes
================================================

Date de vérif.: {check_date}
Heure alerte:   {now_utc}

Les tarifs suivants n'ont PAS de données pour aujourd'hui:
{tariff_lines}

Planning de collecte prévu (CET):
  CKW:         12h05 (retry 12h35, 13h35)
  Autres EVU:  18h30 (retry 19h00, 20h00)

Que faire:
  1. Consulter les journaux système
  2. Tout récupérer manuellement:
     python scheduler.py --now
  3. Ou récupérer un seul tarif:
     python scheduler.py --tariff <tariff_id>
""",
        "anomaly_subject": "[Swiss Tariff Hub] ⚠️  Anomalie de données — {tariff_id} ({date})",
        "anomaly_body": """\
Swiss Tariff Hub — Anomalie de données détectée
================================================

Tarif:        {tariff_id}
Fournisseur:  {provider}
Date cible:   {target_date}
Heure alerte: {now_utc}

Problèmes détectés:
{issue_lines}

Les données ont été sauvegardées mais peuvent être incomplètes ou incorrectes.

Que faire:
  1. Vérifier manuellement la réponse API brute
  2. Relancer si nécessaire:
     python scheduler.py --tariff {tariff_id} --date {target_date}
""",
        "test_subject": "[Swiss Tariff Hub] ✅ Test de configuration email",
        "test_body": """\
Swiss Tariff Hub — Test Email
================================================

Si vous recevez cet email, la configuration des alertes fonctionne correctement.

Des alertes seront envoyées automatiquement quand:
  ❌  Une collecte échoue après toutes les tentatives (~13h35 CET pour CKW)
  ⚠️   Des données manquent lors du contrôle matinal (10h00 CET)
  ⚠️   Les données collectées présentent des anomalies

Planning actuel (CET):
  12h05 → Collecte CKW (retry 12h35, 13h35)
  18h30 → Collecte autres EVU (retry 19h00, 20h00)
  10h00 → Contrôle de santé
""",
    },
    "it": {
        "fetch_failed_subject": "[Swiss Tariff Hub] ❌ Fetch fallito — {tariff_id} ({date})",
        "fetch_failed_body": """\
Swiss Tariff Hub — Fetch Fallito
================================================

Tariffa:     {tariff_id}
Provider:    {provider}
Data target: {target_date}
Ora alert:   {now_utc}

Tutti i {retries} tentativi di fetch sono falliti.
{error_line}
Cosa fare:
  1. Verifica che l'API di {provider} sia raggiungibile:
     {api_url}
  2. Controlla i log del sistema
  3. Riprova manualmente:
     python scheduler.py --tariff {tariff_id} --date {target_date}
""",
        "missing_subject": "[Swiss Tariff Hub] ⚠️  Dati mancanti — {count} tariffa/e ({date})",
        "missing_body": """\
Swiss Tariff Hub — Dati Mancanti
================================================

Data controllo: {check_date}
Ora alert:      {now_utc}

Le seguenti tariffe NON hanno dati per oggi:
{tariff_lines}

Orari fetch previsti (CET):
  CKW:        12:05 (retry 12:35, 13:35)
  Altri EVU:  18:30 (retry 19:00, 20:00)

Cosa fare:
  1. Controlla i log del sistema
  2. Rifai il fetch di tutte le tariffe:
     python scheduler.py --now
  3. Oppure di una sola:
     python scheduler.py --tariff <tariff_id>
""",
        "anomaly_subject": "[Swiss Tariff Hub] ⚠️  Anomalia dati — {tariff_id} ({date})",
        "anomaly_body": """\
Swiss Tariff Hub — Anomalia Dati Rilevata
================================================

Tariffa:     {tariff_id}
Provider:    {provider}
Data target: {target_date}
Ora alert:   {now_utc}

Problemi rilevati:
{issue_lines}

I dati sono stati salvati ma potrebbero essere incompleti o errati.

Cosa fare:
  1. Controlla la risposta API grezza manualmente
  2. Rifai il fetch se necessario:
     python scheduler.py --tariff {tariff_id} --date {target_date}
""",
        "test_subject": "[Swiss Tariff Hub] ✅ Test configurazione email",
        "test_body": """\
Swiss Tariff Hub — Test Email
================================================

Se ricevi questa email, la configurazione degli alert è corretta.

Gli alert arriveranno automaticamente quando:
  ❌  Un fetch fallisce dopo tutti i retry (~13:35 CET per CKW)
  ⚠️   Mancano dati all'health check delle 10:00 CET
  ⚠️   I dati fetchati hanno anomalie (slot errati, prezzi a zero, ecc.)

Orari correnti (CET):
  12:05 → Fetch CKW (retry 12:35, 13:35)
  18:30 → Fetch altri EVU (retry 19:00, 20:00)
  10:00 → Health check
""",
    },
}


def _lang() -> str:
    lang = os.getenv("EMAIL_LANG", "en").lower().strip()
    return lang if lang in _T else "en"


def _t(key: str, **kwargs) -> str:
    tmpl = _T[_lang()].get(key, _T["en"][key])
    return tmpl.format(**kwargs) if kwargs else tmpl


# ── Invio email ───────────────────────────────────────────────────────────────

async def send_email(subject: str, body: str) -> None:
    """Invia email al destinatario configurato in .env (ALERT_EMAIL)."""
    to_email = os.getenv("ALERT_EMAIL", "")
    if not to_email:
        log.warning("ALERT_EMAIL non configurata in .env — email non inviata")
        log.warning(f"  Soggetto saltato: {subject}")
        return

    from_email = os.getenv("SMTP_USER", "tariff-hub@noreply.local")
    smtp_host  = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port  = int(os.getenv("SMTP_PORT", "587"))
    smtp_user  = os.getenv("SMTP_USER", "")
    smtp_pass  = os.getenv("SMTP_PASSWORD", "")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_email
    msg["To"]      = to_email
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            if smtp_port != 25:
                server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        log.info(f"Email inviata a {to_email}: {subject}")
    except Exception as e:
        log.error(f"Invio email fallito: {e}")


# ── Email specifiche ──────────────────────────────────────────────────────────

async def send_fetch_alert(tariff_config: dict, target_date: date, error_detail: str = "") -> None:
    tariff_id  = tariff_config["tariff_id"]
    provider   = tariff_config.get("provider_name", "?")
    now_utc    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    error_line = f"Error detail: {error_detail}\n" if error_detail else ""
    await send_email(
        _t("fetch_failed_subject", tariff_id=tariff_id, date=target_date),
        _t("fetch_failed_body", tariff_id=tariff_id, provider=provider,
           target_date=target_date, now_utc=now_utc, retries=len(RETRY_DELAYS),
           error_line=error_line, api_url=tariff_config.get("api_base_url", "N/A")),
    )


async def send_health_alert(missing: list[tuple[str, str]], check_date: date) -> None:
    now_utc      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tariff_lines = "\n".join(f"  • {tid}  ({provider})" for tid, provider in missing)
    await send_email(
        _t("missing_subject", count=len(missing), date=check_date),
        _t("missing_body", check_date=check_date, now_utc=now_utc, tariff_lines=tariff_lines),
    )

async def fetch(self, target_date: date) -> NormalizedPrices:
    """
    Override del fetch base: corregge source_date con la data reale
    degli slot restituiti dall'API (che è sempre oggi, non target_date).
    """
    from schemas import NormalizedPrices, make_day_range_utc, utc_now

    start_utc, end_utc = make_day_range_utc(target_date)
    url        = self._build_url(start_utc, end_utc)
    headers    = self._build_headers()
    fetched_at = utc_now()

    self._log.info(f"Fetch {target_date} → {url[:80]}...")

    raw_response  = await self._http_get_with_retry(url, headers)
    response_data = self._preprocess_response(raw_response)
    slots         = self._parse(response_data, target_date)

    if not slots:
        from adapters.base import AdapterEmptyError
        raise AdapterEmptyError(
            f"{self.tariff_id}: nessun slot ricevuto"
        )

    # ← DIFFERENZA CHIAVE rispetto al base:
    # Usa la data reale del primo slot, non target_date
    try:
        import zoneinfo
        tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz
        tz_ch = pytz.timezone("Europe/Zurich")

    actual_date = slots[0].slot_start_utc.astimezone(tz_ch).date()

    result = NormalizedPrices(
        tariff_id    = self.tariff_id,
        source_date  = actual_date,   # ← data reale, non target_date
        fetched_at   = fetched_at,
        slots        = sorted(slots, key=lambda s: s.slot_start_utc),
        adapter_name = self.__class__.__name__,
        raw_url      = url,
    )

    if not result.is_complete:
        result.warnings.append(
            f"Slot count inatteso: {result.slot_count} (attesi ~96)"
        )
        self._log.warning(result.warnings[-1])

    self._log.info(result.summary())
    return result

async def send_anomaly_alert(result, target_date: date, issues: list[str], tariff_config: dict) -> None:
    tariff_id   = result.tariff_id
    provider    = tariff_config.get("provider_name", "?")
    now_utc     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    issue_lines = "\n".join(f"  • {i}" for i in issues)
    await send_email(
        _t("anomaly_subject", tariff_id=tariff_id, date=target_date),
        _t("anomaly_body", tariff_id=tariff_id, provider=provider,
           target_date=target_date, now_utc=now_utc, issue_lines=issue_lines),
    )


# ── Validazione post-fetch ────────────────────────────────────────────────────

def validate_result(result) -> list[str]:
    """Ritorna lista di problemi trovati. Lista vuota = tutto ok."""
    issues = []
    sc = result.slot_count

    if sc < EXPECTED_SLOTS_MIN:
        issues.append(f"Too few slots: {sc} (expected ~96, min {EXPECTED_SLOTS_MIN})")
    elif sc > EXPECTED_SLOTS_MAX:
        issues.append(f"Too many slots: {sc} (expected ~96, max {EXPECTED_SLOTS_MAX})")

    zero_slots     = [s.slot_start_utc.strftime("%H:%M") for s in result.slots if s.total_price == 0.0]
    negative_slots = [s.slot_start_utc.strftime("%H:%M") for s in result.slots
                      if s.total_price is not None and s.total_price < 0]

    if zero_slots:
        sample = zero_slots[:5]
        more   = f" (+{len(zero_slots)-5} more)" if len(zero_slots) > 5 else ""
        issues.append(f"Zero-price slots: {', '.join(sample)}{more}")

    if negative_slots:
        sample = negative_slots[:5]
        more   = f" (+{len(negative_slots)-5} more)" if len(negative_slots) > 5 else ""
        issues.append(f"Negative-price slots: {', '.join(sample)}{more}")

    totals = [s.total_price for s in result.slots if s.total_price is not None]
    if len(totals) > 4 and len(set(round(t, 6) for t in totals)) == 1:
        issues.append(
            f"All {len(totals)} slots have identical price "
            f"({totals[0]*100:.2f} Rp/kWh) — possible API error"
        )

    return issues


# ── Fetch singola tariffa con retry ───────────────────────────────────────────

async def fetch_with_retry(tariff_config: dict, target_date: date, _unused) -> bool:
    from adapters import get_adapter, AdapterError, AdapterEmptyError, list_available_adapters
    from database import upsert_prices, init_db

    tariff_id     = tariff_config["tariff_id"]
    adapter_class = tariff_config.get("adapter_class", "")

    if adapter_class not in list_available_adapters():
        log.warning(f"[{tariff_id}] Adapter '{adapter_class}' non implementato — skip")
        return False

    last_error = ""
    for attempt, delay in enumerate(RETRY_DELAYS, 1):
        if delay > 0:
            log.info(f"[{tariff_id}] Retry {attempt}/{len(RETRY_DELAYS)} tra {delay//60} min")
            await asyncio.sleep(delay)

        try:
            log.info(f"[{tariff_id}] Tentativo {attempt}/{len(RETRY_DELAYS)} — {target_date}")
            adapter = get_adapter(tariff_config)
            result  = await adapter.fetch(target_date)

            SessionLocal = init_db()
            with SessionLocal() as session:
                saved = upsert_prices(session, result)

            avg_str = f" | avg: {result.avg_total_price*100:.2f} Rp/kWh" if result.avg_total_price else ""
            log.info(f"[{tariff_id}] ✓ {saved} slot salvati{avg_str}")

            issues = validate_result(result)
            if issues:
                log.warning(f"[{tariff_id}] Anomalie: {issues}")
                await send_anomaly_alert(result, target_date, issues, tariff_config)
            else:
                log.info(f"[{tariff_id}] ✓ Validazione OK ({result.slot_count} slot)")

            return True

        except AdapterEmptyError as e:
            log.warning(f"[{tariff_id}] Dati non disponibili: {e}")
            return False
        except AdapterError as e:
            last_error = str(e)
            log.warning(f"[{tariff_id}] Tentativo {attempt} fallito: {e}")
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            log.error(f"[{tariff_id}] Errore inatteso: {last_error}")
            import traceback; traceback.print_exc()

    log.error(f"[{tariff_id}] TUTTI I TENTATIVI FALLITI per {target_date}")
    await send_fetch_alert(tariff_config, target_date, last_error)
    return False


# ── Health check ─────────────────────────────────────────────────────────────

async def run_health_check() -> None:
    """
    Alle 10:00 CET: verifica che i dati di OGGI siano nel DB.

    I fetch del giorno prima (12:05 e 18:30 CET) salvano i prezzi per OGGI.
    Se questa mattina i dati mancano → qualcosa è andato storto ieri
    (server spento, errore silenzioso senza email) → manda email.
    """
    from database import init_db, get_active_tariffs, get_last_fetch
    from adapters import list_available_adapters

    log.info("=== HEALTH CHECK ===")

    try:
        import zoneinfo; tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz; tz_ch = pytz.timezone("Europe/Zurich")

    today     = datetime.now(timezone.utc).astimezone(tz_ch).date()
    yesterday = today - timedelta(days=1)

    SessionLocal = init_db()
    available    = set(list_available_adapters())
    missing      = []

    with SessionLocal() as session:
        for t in get_active_tariffs(session):
            if t.adapter_class not in available:
                continue
            last = get_last_fetch(session, t.tariff_id)
            if last is None:
                log.warning(f"[{t.tariff_id}] ❌ Nessun dato nel DB")
                missing.append((t.tariff_id, t.provider_name))
            elif last.date() < yesterday:
                log.warning(f"[{t.tariff_id}] ❌ Ultimo dato: {last.date()} (atteso >= {yesterday})")
                missing.append((t.tariff_id, t.provider_name))
            else:
                log.info(f"[{t.tariff_id}] ✓ Ultimo dato: {last.date()}")

    if missing:
        log.error(f"Health check: {len(missing)} tariffe con dati mancanti")
        await send_health_alert(missing, today)
    else:
        log.info("Health check: ✓ tutte le tariffe hanno dati recenti")


# ── Fetch tutte le tariffe ────────────────────────────────────────────────────

async def fetch_all(target_date: Optional[date] = None) -> dict[str, bool]:
    from database import init_db, get_active_tariffs, seed_tariffs
    from schemas import tomorrow_ch

    if target_date is None:
        target_date = tomorrow_ch()

    log.info(f"Fetch di {target_date}")
    SessionLocal = init_db()
    with SessionLocal() as session:
        tariffs = get_active_tariffs(session)
        if not tariffs:
            seed_tariffs(session)
            tariffs = get_active_tariffs(session)
        configs = [json.loads(t.full_config_json) for t in tariffs]

    results   = {}
    semaphore = asyncio.Semaphore(3)

    async def bounded(config):
        async with semaphore:
            ok = await fetch_with_retry(config, target_date, None)
            results[config["tariff_id"]] = ok

    await asyncio.gather(*[bounded(c) for c in configs])

    from adapters import list_available_adapters
    available = set(list_available_adapters())
    success   = sum(1 for v in results.values() if v)
    skipped   = sum(1 for c in configs if c.get("adapter_class") not in available)
    failed    = sum(1 for v in results.values() if not v) - skipped
    log.info(f"Riepilogo: {success} OK, {failed} falliti, {skipped} skip (adapter mancanti)")
    return results


# ── APScheduler ───────────────────────────────────────────────────────────────

def start_scheduler() -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    import json as _json

    scheduler = AsyncIOScheduler(timezone="UTC")

    async def job_ckw():
        log.info("=== JOB CKW (11:05 UTC = 12:05 CET) ===")
        from database import init_db, get_active_tariffs
        from schemas import tomorrow_ch
        target = tomorrow_ch()
        SessionLocal = init_db()
        with SessionLocal() as session:
            ckw_configs = [
                _json.loads(t.full_config_json)
                for t in get_active_tariffs(session)
                if t.adapter_class == "CkwAdapter"
            ]
        for config in ckw_configs:
            await fetch_with_retry(config, target, None)

    async def job_others():
        log.info("=== JOB ALTRI EVU (17:30 UTC = 18:30 CET) ===")
        from database import init_db, get_active_tariffs
        from schemas import tomorrow_ch
        target = tomorrow_ch()
        SessionLocal = init_db()
        with SessionLocal() as session:
            other_configs = [
                _json.loads(t.full_config_json)
                for t in get_active_tariffs(session)
                if t.adapter_class != "CkwAdapter"
            ]
        semaphore = asyncio.Semaphore(3)
        async def bounded(config):
            async with semaphore:
                await fetch_with_retry(config, target, None)
        await asyncio.gather(*[bounded(c) for c in other_configs])

    async def job_health_check():
        await run_health_check()

    async def job_monthly():
        from backfill import monthly_maintenance
        await monthly_maintenance()

    scheduler.add_job(job_ckw,          CronTrigger(hour=11, minute=5),         id="ckw_fetch")
    scheduler.add_job(job_others,        CronTrigger(hour=17, minute=30),        id="others_fetch")
    scheduler.add_job(job_health_check,  CronTrigger(hour=9,  minute=0),         id="health_check")
    scheduler.add_job(job_monthly,       CronTrigger(day=1, hour=3, minute=0),   id="monthly")

    log.info("╔══════════════════════════════════════════╗")
    log.info("║      Swiss Tariff Hub — Scheduler        ║")
    log.info("╠══════════════════════════════════════════╣")
    log.info("║  11:05 UTC (12:05 CET) → fetch CKW       ║")
    log.info("║  17:30 UTC (18:30 CET) → fetch altri EVU ║")
    log.info("║  09:00 UTC (10:00 CET) → health check    ║")
    log.info("║  1° mese  03:00 UTC    → manutenzione    ║")
    log.info("╚══════════════════════════════════════════╝")

    email = os.getenv("ALERT_EMAIL", "")
    lang  = _lang()
    if email:
        log.info(f"Alert email: {email}  |  lingua: {lang}")
    else:
        log.warning("⚠️  ALERT_EMAIL non configurata — crea il file .env")

    async def _run():
        scheduler.start()
        log.info("Scheduler avviato — premi Ctrl+C per fermare")
        try:
            while True:
                await asyncio.sleep(60)
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown()
            log.info("Scheduler fermato")

    asyncio.run(_run())


# ── CLI ───────────────────────────────────────────────────────────────────────

import json


def _parse_target_date() -> date:
    try:
        import zoneinfo; tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz; tz_ch = pytz.timezone("Europe/Zurich")

    if "--date" in sys.argv:
        idx = sys.argv.index("--date") + 1
        if idx < len(sys.argv):
            try:
                return date.fromisoformat(sys.argv[idx])
            except ValueError:
                print(f"Data non valida: {sys.argv[idx]}. Formato: YYYY-MM-DD")
                sys.exit(1)

    if "--now" in sys.argv or "--tariff" in sys.argv:
        return datetime.now(timezone.utc).astimezone(
            __import__("zoneinfo").ZoneInfo("Europe/Zurich")
        ).date()

    from schemas import tomorrow_ch
    return tomorrow_ch()


async def cli():
    if "--health" in sys.argv:
        await run_health_check()
        return

    if "--test-email" in sys.argv:
        await send_email(_t("test_subject"), _t("test_body"))
        return

    target = _parse_target_date()

    if "--tariff" in sys.argv:
        idx = sys.argv.index("--tariff") + 1
        tariff_id = sys.argv[idx] if idx < len(sys.argv) else None
        if not tariff_id:
            print("Uso: python scheduler.py --tariff <tariff_id> [--date YYYY-MM-DD]")
            return
        from database import init_db, get_active_tariffs
        SessionLocal = init_db()
        with SessionLocal() as session:
            config = next(
                (json.loads(t.full_config_json) for t in get_active_tariffs(session)
                 if t.tariff_id == tariff_id), None
            )
        if not config:
            print(f"Tariffa '{tariff_id}' non trovata")
            return
        log.info(f"Fetch manuale: {tariff_id} per {target}")
        ok = await fetch_with_retry(config, target, None)
        print("✓ OK" if ok else "✗ FALLITO")

    elif "--now" in sys.argv:
        results = await fetch_all(target)
        print()
        for tid, ok in results.items():
            print(f"  {'✓' if ok else '✗'} {tid}")

    else:
        start_scheduler()


if __name__ == "__main__":
    if any(f in sys.argv for f in ["--now", "--tariff", "--health", "--test-email"]):
        asyncio.run(cli())
    else:
        start_scheduler()