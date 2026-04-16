"""
scheduler.py
============
Scheduler giornaliero automatico per Swiss Tariff Hub.

Flusso giornaliero (ora locale CET/CEST):

  STARTUP (ogni riavvio del server)
  ──────────────────────────────────
  Avvio   → Startup recovery in background:
              fetch immediato di TODAY e TOMORROW se mancanti
              (copre "server era spento ieri sera")

  RECOVERY TODAY (fetch attivo per OGGI se mancante)
  ────────────────────────────────────────────────────
  07:30 CET → Recovery mattutino TODAY: fetcha chi manca oggi [no-op se ok]
  13:00 CET → Recovery pomeridiano TODAY: secondo tentativo    [no-op se ok]

  FETCH PRIMARIO (dati per DOMANI)
  ─────────────────────────────────
  12:05 CET → Fetch CKW (home + business) per DOMANI
               Retry: 12:35, poi 13:35
               Se tutti falliti: EMAIL ❌

  18:30 CET → Fetch tutti gli altri EVU per DOMANI
               Retry: 19:00, poi 20:00
               Se tutti falliti: EMAIL ❌

  RECOVERY TOMORROW (no-op se i dati ci sono già)
  ─────────────────────────────────────────────────
  17:30 CET → Recovery CKW: rifetcha solo se dati di domani mancano
  21:00 CET → Recovery TUTTI: rifetcha solo tariffe ancora mancanti

  LAST RESORT
  ───────────
  23:30 CET → Ultima verifica: rifetcha mancanti + EMAIL se ancora vuoto ⚠️

  HEALTH CHECK
  ────────────
  10:00 CET → Verifica che i dati di OGGI siano nel DB
               Se mancano: EMAIL ⚠️
               (cattura il caso "server era spento ieri sera")

  MANUTENZIONE
  ────────────
  02:00 CET → 1° di ogni mese: backfill manutenzione mensile

Validazione post-fetch (validate_result):
  - Slot count fuori range (< 90 o > 102)
  - Prezzi a zero o negativi
  - Tutti i prezzi identici — SALTATO se expect_uniform_prices=true in evu_list.json
    (EKZ 2026 ha prezzi fissi annuali: identici by design, non è un errore)
  - Gap temporali nella serie (slot non contigui a 15 min esatti)
  Se anomalie reali: EMAIL ⚠️

Circuit breaker (MAX_CONSECUTIVE_FAILURES = 3):
  Se una tariffa fallisce per 3 giorni consecutivi, il fetch automatico
  viene sospeso (log di avviso, nessuna email aggiuntiva — già inviata).
  Si resetta al primo fetch riuscito oppure con --tariff manuale.

Configurazione (.env nella cartella del progetto):
  ALERT_EMAIL=simomorro24@gmail.com
  EMAIL_LANG=it
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=simomorro24@gmail.com
  SMTP_PASSWORD=xxxxxxxxxxxxxxxx

Uso CLI:
  python scheduler.py                                      # avvia scheduler
  python scheduler.py --now                                # fetch tutte le tariffe per domani
  python scheduler.py --tariff ckw_home_dynamic            # fetch singola tariffa
  python scheduler.py --tariff ckw_home_dynamic --date 2026-03-18
  python scheduler.py --health                             # health check manuale
  python scheduler.py --recovery                           # recovery manuale (solo mancanti)
  python scheduler.py --test-email                         # invia email di test
"""

from __future__ import annotations

import asyncio
import random
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

RETRY_DELAYS             = [0, 30 * 60, 60 * 60]   # subito, +30 min, +60 min
EXPECTED_SLOTS_MIN       = 90
EXPECTED_SLOTS_MAX       = 102
MAX_CONSECUTIVE_FAILURES = 3   # giorni prima che il circuit breaker scatti

# ── Circuit breaker — stato in memoria ───────────────────────────────────────
# { tariff_id: contatore_fallimenti_consecutivi }
# Si azzera al primo fetch riuscito o con --tariff manuale. Reset al riavvio.
_failure_streak: dict[str, int] = {}


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
        "missing_subject": "[Swiss Tariff Hub] ⚠️  Missing data — {count} tariff(s) for {date}",
        "missing_body": """\
Swiss Tariff Hub — Missing Data
================================================

Check date: {check_date}
Alert time: {now_utc}
Context:    {context}

The following tariff(s) have NO data for {check_date}:
{tariff_lines}

Expected fetch schedule (CET):
  CKW:       12:05 (retry 12:35, 13:35) + recovery 17:30
  Other EVU: 18:30 (retry 19:00, 20:00) + recovery 21:00
  Last resort: 23:30

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
  ❌  A fetch fails after all retries (~13:35 CET for CKW, ~20:30 CET for others)
  ⚠️   Data is missing during the morning health check (10:00 CET)
  ⚠️   Data is still missing at the last resort check (23:30 CET)
  ⚠️   Fetched data has REAL anomalies (wrong slot count, gaps, zero prices)
       NOTE: uniform prices are NOT flagged if expect_uniform_prices=true (e.g. EKZ)

Current schedule (CET):
  12:05 → Fetch CKW        (retry 12:35, 13:35)
  17:30 → Recovery CKW     (only if data missing)
  18:30 → Fetch other EVU  (retry 19:00, 20:00)
  21:00 → Recovery all     (only if data missing)
  23:30 → Last resort all  (final check + alert if still missing)
  10:00 → Morning health check
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
        "missing_subject": "[Swiss Tariff Hub] ⚠️  Fehlende Daten — {count} Tarif(e) für {date}",
        "missing_body": """\
Swiss Tariff Hub — Fehlende Daten
================================================

Prüfdatum:  {check_date}
Alarmzeit:  {now_utc}
Kontext:    {context}

Folgende Tarife haben KEINE Daten für {check_date}:
{tariff_lines}

Erwarteter Abrufplan (CET):
  CKW:        12:05 (Retry 12:35, 13:35) + Recovery 17:30
  Andere EVU: 18:30 (Retry 19:00, 20:00) + Recovery 21:00
  Last Resort: 23:30

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
  ❌  Fehlgeschlagenem Abruf nach allen Versuchen
  ⚠️   Fehlenden Daten beim Gesundheitscheck (10:00 CET)
  ⚠️   Noch fehlenden Daten beim Last-Resort-Check (23:30 CET)
  ⚠️   ECHTEN Anomalien in Daten (falsche Slot-Anzahl, Lücken, Nullpreise)
       HINWEIS: einheitliche Preise werden NICHT gemeldet wenn expect_uniform_prices=true (z.B. EKZ)

Aktueller Zeitplan (CET):
  12:05 → CKW abrufen       (Retry 12:35, 13:35)
  17:30 → CKW Recovery      (nur bei fehlenden Daten)
  18:30 → Andere EVU        (Retry 19:00, 20:00)
  21:00 → Recovery alle     (nur bei fehlenden Daten)
  23:30 → Last Resort alle  (finale Prüfung + Alert)
  10:00 → Morgen-Gesundheitscheck
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
        "missing_subject": "[Swiss Tariff Hub] ⚠️  Données manquantes — {count} tarif(s) pour {date}",
        "missing_body": """\
Swiss Tariff Hub — Données manquantes
================================================

Date de vérif.: {check_date}
Heure alerte:   {now_utc}
Contexte:       {context}

Les tarifs suivants n'ont PAS de données pour {check_date}:
{tariff_lines}

Planning de collecte prévu (CET):
  CKW:         12h05 (retry 12h35, 13h35) + recovery 17h30
  Autres EVU:  18h30 (retry 19h00, 20h00) + recovery 21h00
  Last resort: 23h30

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

Planning actuel (CET):
  12h05 → Collecte CKW       (retry 12h35, 13h35)
  17h30 → Recovery CKW       (seulement si données manquantes)
  18h30 → Collecte autres EVU (retry 19h00, 20h00)
  21h00 → Recovery tous      (seulement si données manquantes)
  23h30 → Last resort tous   (vérification finale + alerte)
  10h00 → Contrôle de santé matinal
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
        "missing_subject": "[Swiss Tariff Hub] ⚠️  Dati mancanti — {count} tariffa/e per {date}",
        "missing_body": """\
Swiss Tariff Hub — Dati Mancanti
================================================

Data controllo: {check_date}
Ora alert:      {now_utc}
Contesto:       {context}

Le seguenti tariffe NON hanno dati per {check_date}:
{tariff_lines}

Orari fetch previsti (CET):
  CKW:        12:05 (retry 12:35, 13:35) + recovery 17:30
  Altri EVU:  18:30 (retry 19:00, 20:00) + recovery 21:00
  Last resort: 23:30

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
  ❌  Un fetch fallisce dopo tutti i retry
  ⚠️   Mancano dati all'health check delle 10:00 CET
  ⚠️   Mancano dati al last resort delle 23:30 CET
  ⚠️   I dati hanno ANOMALIE REALI (slot errati, gap temporali, prezzi zero)
       NOTA: prezzi uniformi NON vengono segnalati se expect_uniform_prices=true (es. EKZ)

Orari correnti (CET):
  12:05 → Fetch CKW        (retry 12:35, 13:35)
  17:30 → Recovery CKW     (solo se dati mancano)
  18:30 → Fetch altri EVU  (retry 19:00, 20:00)
  21:00 → Recovery tutti   (solo se dati mancano)
  23:30 → Last resort tutti (verifica finale + alert)
  10:00 → Health check mattutino
""",
    },
}


def _lang() -> str:
    lang = os.getenv("EMAIL_LANG", "en").lower().strip()
    return lang if lang in _T else "en"


def _t(key: str, **kwargs) -> str:
    tmpl = _T[_lang()].get(key, _T["en"][key])
    return tmpl.format(**kwargs) if kwargs else tmpl


# ── Timezone helper ───────────────────────────────────────────────────────────

def _tz_ch():
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz
        return pytz.timezone("Europe/Zurich")


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
        _t("fetch_failed_body",
           tariff_id=tariff_id, provider=provider,
           target_date=target_date, now_utc=now_utc,
           retries=len(RETRY_DELAYS),
           error_line=error_line,
           api_url=tariff_config.get("api_base_url", "N/A")),
    )


async def send_missing_alert(
    missing: list[tuple[str, str]],
    check_date: date,
    context: str = "health check",
) -> None:
    """Email generica 'dati mancanti', usata da health check e last resort."""
    now_utc      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tariff_lines = "\n".join(f"  • {tid}  ({provider})" for tid, provider in missing)
    await send_email(
        _t("missing_subject", count=len(missing), date=check_date),
        _t("missing_body",
           check_date=check_date, now_utc=now_utc,
           context=context, tariff_lines=tariff_lines),
    )


# Alias per compatibilità con codice esistente che chiama send_health_alert
async def send_health_alert(missing: list[tuple[str, str]], check_date: date) -> None:
    await send_missing_alert(missing, check_date, context="morning health check (10:00 CET)")


async def send_anomaly_alert(result, target_date: date, issues: list[str], tariff_config: dict) -> None:
    tariff_id   = result.tariff_id
    provider    = tariff_config.get("provider_name", "?")
    now_utc     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    issue_lines = "\n".join(f"  • {i}" for i in issues)
    await send_email(
        _t("anomaly_subject", tariff_id=tariff_id, date=target_date),
        _t("anomaly_body",
           tariff_id=tariff_id, provider=provider,
           target_date=target_date, now_utc=now_utc,
           issue_lines=issue_lines),
    )


# ── Validazione post-fetch ────────────────────────────────────────────────────

def validate_result(
    result,
    tariff_config: dict | None = None,
    session=None,
) -> list[str]:
    """
    Controlla la qualità dei dati fetchati.
    Ritorna lista di problemi trovati. Lista vuota = tutto ok.
 
    Se session è fornita, aggiunge un confronto con la mediana degli ultimi
    3 giorni — utile per capire se un'anomalia è nuova o strutturale.
 
    Checks:
      1. Slot count (< 90 o > 102)
      2. Prezzi a zero
      3. Prezzi negativi
      4. Prezzi identici — saltato se expect_uniform_prices=true (es. EKZ)
      5. Gap temporali nella serie
      6. [se session] Confronto con mediana ultimi 3 giorni
    """
    issues     = []
    config     = tariff_config or {}
    api_params = config.get("api_params", {})
 
    # 1. Slot count
    sc = result.slot_count
    if sc < EXPECTED_SLOTS_MIN:
        issues.append(f"Too few slots: {sc} (expected ~96, min {EXPECTED_SLOTS_MIN})")
    elif sc > EXPECTED_SLOTS_MAX:
        issues.append(f"Too many slots: {sc} (expected ~96, max {EXPECTED_SLOTS_MAX})")
 
    # 2. Prezzi a zero
    zero_slots = [
        s.slot_start_utc.strftime("%H:%M")
        for s in result.slots
        if s.total_price is not None and s.total_price == 0.0
    ]
    if zero_slots:
        sample = zero_slots[:5]
        more   = f" (+{len(zero_slots)-5} more)" if len(zero_slots) > 5 else ""
        issues.append(f"Zero-price slots: {', '.join(sample)}{more}")
 
    # 3. Prezzi negativi — saltato se allow_negative_prices=true (es. Groupe E)
    if not api_params.get("allow_negative_prices", False):
        negative_slots = [
            s.slot_start_utc.strftime("%H:%M")
            for s in result.slots
            if s.total_price is not None and s.total_price < 0
        ]
        if negative_slots:
            sample = negative_slots[:5]
            more   = f" (+{len(negative_slots)-5} more)" if len(negative_slots) > 5 else ""
            issues.append(f"Negative-price slots: {', '.join(sample)}{more}")
    else:
        log.debug(f"[{result.tariff_id}] allow_negative_prices=true — skip negative-price check")
 
    # 4. Prezzi identici — FIX EKZ
    if not api_params.get("expect_uniform_prices", False):
        totals = [s.total_price for s in result.slots if s.total_price is not None]
        if len(totals) > 4 and len(set(round(t, 6) for t in totals)) == 1:
            issues.append(
                f"All {len(totals)} slots have identical price "
                f"({totals[0]*100:.2f} Rp/kWh) — possible API error"
            )
    else:
        log.debug(f"[{result.tariff_id}] expect_uniform_prices=true — skip identical-price check")
 
    # 5. Gap temporali nella serie
    if len(result.slots) >= 2:
        gaps = []
        for i in range(len(result.slots) - 1):
            expected_next = result.slots[i].slot_start_utc + timedelta(minutes=15)
            actual_next   = result.slots[i + 1].slot_start_utc
            delta_min     = int((actual_next - expected_next).total_seconds() / 60)
            if abs(delta_min) > 0:
                gap_time = result.slots[i].slot_start_utc.strftime("%H:%M")
                gaps.append(f"{gap_time} (gap {delta_min:+d} min)")
        if gaps:
            sample = gaps[:3]
            more   = f" (+{len(gaps)-3} more)" if len(gaps) > 3 else ""
            issues.append(f"Temporal gaps in series: {', '.join(sample)}{more}")
 
    # 6. Confronto con mediana ultimi 3 giorni (solo se session disponibile)
    if session is not None and result.avg_total_price is not None:
        try:
            from database import PriceSlotDB
            from datetime import timezone
 
            # Recupera i prezzi degli ultimi 3 giorni (escluso oggi)
            cutoff = result.slots[0].slot_start_utc - timedelta(days=4)
            end    = result.slots[0].slot_start_utc
            hist_rows = (
                session.query(PriceSlotDB.energy_price, PriceSlotDB.grid_price,
                              PriceSlotDB.residual_price)
                .filter(
                    PriceSlotDB.tariff_id == result.tariff_id,
                    PriceSlotDB.slot_start_utc >= cutoff,
                    PriceSlotDB.slot_start_utc <  end,
                )
                .all()
            )
            hist_totals = [
                sum(p for p in [e, g, r] if p)
                for e, g, r in hist_rows
                if sum(p for p in [e, g, r] if p) > 0
            ]
            if len(hist_totals) >= 48:  # almeno mezzo giorno di storico
                # Mediana (più robusta della media agli outlier)
                hist_sorted = sorted(hist_totals)
                n = len(hist_sorted)
                median_hist = (
                    hist_sorted[n // 2]
                    if n % 2 == 1
                    else (hist_sorted[n // 2 - 1] + hist_sorted[n // 2]) / 2
                )
                today_avg = result.avg_total_price
                ratio = today_avg / median_hist if median_hist > 0 else 1.0
                diff_pct = (ratio - 1.0) * 100
 
                context_line = (
                    f"Historical context: today avg {today_avg*100:.2f} Rp/kWh vs "
                    f"3-day median {median_hist*100:.2f} Rp/kWh "
                    f"({diff_pct:+.1f}% — "
                    f"{'NEW anomaly' if abs(diff_pct) > 20 else 'within normal range'}"
                    f", based on {len(hist_totals)} historical slots)"
                )
                # Aggiunge il contesto come nota (non come issue) nella prima anomalia
                # oppure come issue separata se la variazione è estrema (>50%)
                if abs(diff_pct) > 50:
                    issues.append(
                        f"Price deviation: today avg {today_avg*100:.2f} Rp vs "
                        f"3-day median {median_hist*100:.2f} Rp ({diff_pct:+.1f}%)"
                    )
                elif issues:
                    # Appende il contesto storico alla prima issue esistente
                    issues[0] = issues[0] + f" [{context_line}]"
                else:
                    # Nessun issue ma aggiunge contesto se chiesto
                    log.info(f"[{result.tariff_id}] {context_line}")
        except Exception as e:
            log.debug(f"[{result.tariff_id}] Historical context unavailable: {e}")
 
    return issues


# ── Circuit breaker ───────────────────────────────────────────────────────────

def _circuit_open(tariff_id: str) -> bool:
    """
    True se il circuit breaker è aperto (troppi fallimenti consecutivi).
    Quando aperto: skip del fetch + log di avviso, senza email aggiuntive
    (l'email è già stata inviata al momento del fallimento).
    """
    streak = _failure_streak.get(tariff_id, 0)
    if streak >= MAX_CONSECUTIVE_FAILURES:
        log.warning(
            f"[{tariff_id}] Circuit breaker APERTO ({streak} fallimenti consecutivi) — "
            f"fetch skippato. Usa --tariff {tariff_id} per forzare."
        )
        return True
    return False


def _record_success(tariff_id: str) -> None:
    """Azzera il contatore di fallimenti per questa tariffa."""
    if tariff_id in _failure_streak:
        old = _failure_streak.pop(tariff_id)
        log.info(f"[{tariff_id}] Circuit breaker resettato (era a {old} fallimenti consecutivi)")


def _record_failure(tariff_id: str) -> int:
    """Incrementa il contatore di fallimenti. Ritorna il nuovo valore."""
    _failure_streak[tariff_id] = _failure_streak.get(tariff_id, 0) + 1
    return _failure_streak[tariff_id]


# ── Fetch singola tariffa con retry ───────────────────────────────────────────

async def fetch_with_retry(tariff_config: dict, target_date: date, _unused) -> bool:
    """
    Fetcha una tariffa con fino a 3 tentativi (0, +30min, +60min).
    Scrive un record in fetch_log dopo ogni esito (ok / error / empty / anomaly).
    """
    from adapters import get_adapter, AdapterError, AdapterEmptyError, list_available_adapters
    from database import upsert_prices, init_db, log_fetch
    import time as _time
 
    tariff_id     = tariff_config["tariff_id"]
    adapter_class = tariff_config.get("adapter_class", "")
 
    if adapter_class not in list_available_adapters():
        log.warning(f"[{tariff_id}] Adapter \'{adapter_class}\' non implementato — skip")
        return False
 
    if _circuit_open(tariff_id):
        return False
 
    last_error = ""
    for attempt, delay in enumerate(RETRY_DELAYS, 1):
        if delay > 0:
            log.info(f"[{tariff_id}] Retry {attempt}/{len(RETRY_DELAYS)} tra {delay//60} min")
            await asyncio.sleep(delay)
 
        t0 = _time.monotonic()
        try:
            log.info(f"[{tariff_id}] Tentativo {attempt}/{len(RETRY_DELAYS)} — {target_date}")
            adapter = get_adapter(tariff_config)
            result  = await adapter.fetch(target_date)
            duration_ms = int((_time.monotonic() - t0) * 1000)
 
            SessionLocal = init_db()
            with SessionLocal() as session:
                saved = upsert_prices(session, result)
 
            avg_str = f" | avg: {result.avg_total_price*100:.2f} Rp/kWh" if result.avg_total_price else ""
            log.info(f"[{tariff_id}] ✓ {saved} slot salvati{avg_str}")
 
            issues = validate_result(result, tariff_config)
            if issues:
                log.warning(f"[{tariff_id}] Anomalie: {issues}")
                await send_anomaly_alert(result, target_date, issues, tariff_config)
                # Log come "anomaly" — dati salvati ma con problemi
                SessionLocal2 = init_db()
                with SessionLocal2() as session:
                    log_fetch(session, tariff_id, target_date, "anomaly",
                              duration_ms=duration_ms,
                              slot_count=result.slot_count,
                              avg_price_rp=round(result.avg_total_price * 100, 2) if result.avg_total_price else None,
                              anomaly_flags=issues)
            else:
                log.info(f"[{tariff_id}] ✓ Validazione OK ({result.slot_count} slot)")
                # Log come "ok"
                SessionLocal2 = init_db()
                with SessionLocal2() as session:
                    log_fetch(session, tariff_id, target_date, "ok",
                              duration_ms=duration_ms,
                              slot_count=result.slot_count,
                              avg_price_rp=round(result.avg_total_price * 100, 2) if result.avg_total_price else None)
 
            _record_success(tariff_id)
            return True
 
        except AdapterEmptyError as e:
            duration_ms = int((_time.monotonic() - t0) * 1000)
            log.warning(f"[{tariff_id}] Dati non disponibili: {e}")
            SessionLocal = init_db()
            with SessionLocal() as session:
                log_fetch(session, tariff_id, target_date, "empty",
                          duration_ms=duration_ms, error_msg=str(e))
            return False
        except AdapterError as e:
            last_error = str(e)
            log.warning(f"[{tariff_id}] Tentativo {attempt} fallito: {e}")
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            log.error(f"[{tariff_id}] Errore inatteso: {last_error}")
            import traceback; traceback.print_exc()
 
    duration_ms = int((_time.monotonic() - t0) * 1000)
    log.error(f"[{tariff_id}] TUTTI I TENTATIVI FALLITI per {target_date}")
    await send_fetch_alert(tariff_config, target_date, last_error)
 
    # Log come "error"
    SessionLocal = init_db()
    with SessionLocal() as session:
        log_fetch(session, tariff_id, target_date, "error",
                  duration_ms=duration_ms, error_msg=last_error)
 
    streak = _record_failure(tariff_id)
    if streak >= MAX_CONSECUTIVE_FAILURES:
        log.warning(
            f"[{tariff_id}] Circuit breaker: {streak} fallimenti consecutivi. "
            f"Fetch automatico sospeso. Usa --tariff {tariff_id} per riprendere."
        )
 
    return False

# ── Recovery: fetch solo tariffe mancanti ─────────────────────────────────────

async def fetch_missing_for_date(
    target_date: date,
    adapter_class_filter: Optional[str] = None,
    label: str = "recovery",
) -> list[tuple[str, str]]:
    """
    Fetcha SOLO le tariffe che NON hanno già dati per target_date nel DB.
    È un no-op efficiente se i dati ci sono già (una COUNT query per tariffa).

    Ritorna la lista di (tariff_id, provider_name) ancora mancanti DOPO il tentativo.
    """
    from database import init_db, get_active_tariffs, has_data_for_date
    from adapters import list_available_adapters
    import json as _json

    tz_ch = _tz_ch()

    from datetime import timedelta as _td
    SessionLocal = init_db()
    available    = set(list_available_adapters())
    missing_configs = []

    with SessionLocal() as session:
        for t in get_active_tariffs(session):
            if adapter_class_filter and t.adapter_class != adapter_class_filter:
                continue
            if t.adapter_class not in available:
                continue
            config         = _json.loads(t.full_config_json)
            day_ahead_only = not config.get("api_params", {}).get("backfill_supported", True)
            prev_date      = target_date - _td(days=1)

            has_target = has_data_for_date(session, t.tariff_id, target_date, tz_ch)
            has_prev   = day_ahead_only and has_data_for_date(session, t.tariff_id, prev_date, tz_ch)

            if not has_target and not has_prev:
                missing_configs.append(config)

    if not missing_configs:
        log.info(f"[{label}] ✓ Nessuna tariffa mancante per {target_date}")
        return []

    missing_ids = [c["tariff_id"] for c in missing_configs]
    log.info(f"[{label}] {len(missing_configs)} tariffe mancanti per {target_date}: {missing_ids}")

    semaphore = asyncio.Semaphore(3)

    async def bounded(config):
        async with semaphore:
            await fetch_with_retry(config, target_date, None)

    await asyncio.gather(*[bounded(c) for c in missing_configs])

    # Verifica quali sono ancora mancanti dopo il tentativo.
    #
    # NOTA sulle API day-ahead (backfill_supported: false):
    # Queste API (AEM, EKZ, Groupe E, Primeo...) restituiscono sempre i dati
    # del giorno successivo rispetto a quello in cui vengono chiamate.
    # Quando le chiamiamo con target_date = domani, i loro slot UTC coprono
    # la data locale CH di "domani" ma iniziano alle 22:00 UTC di "oggi".
    # has_data_for_date(target_date) può quindi risultare False anche se il
    # fetch è riuscito, perché gli slot sono registrati sotto target_date - 1.
    # Per queste tariffe accettiamo i dati anche se sono sotto target_date - 1.
    from datetime import timedelta as _td
    still_missing = []
    SessionLocal2 = init_db()
    with SessionLocal2() as session:
        for config in missing_configs:
            tid              = config["tariff_id"]
            provider         = config.get("provider_name", "?")
            day_ahead_only   = not config.get("api_params", {}).get("backfill_supported", True)
            prev_date        = target_date - _td(days=1)

            has_target = has_data_for_date(session, tid, target_date, tz_ch)
            has_prev   = day_ahead_only and has_data_for_date(session, tid, prev_date, tz_ch)

            if not has_target and not has_prev:
                still_missing.append((tid, provider))
            elif not has_target and has_prev:
                log.info(
                    f"[{label}] [{tid}] Dati trovati sotto {prev_date} "
                    f"(API day-ahead — normale)"
                )

    if still_missing:
        log.warning(
            f"[{label}] Ancora {len(still_missing)} tariffe senza dati: "
            f"{[t for t, _ in still_missing]}"
        )
    else:
        log.info(f"[{label}] ✓ Tutti i dati recuperati per {target_date}")

    return still_missing


# ── Health check ─────────────────────────────────────────────────────────────

async def run_health_check() -> None:
    """
    Alle 10:00 CET: verifica che i dati di OGGI siano nel DB.

    Usa has_data_for_date (controlla per QUALE DATA ci sono slot, non QUANDO
    è stato fatto il fetch) — semantica corretta.
    """
    from database import init_db, get_active_tariffs, has_data_for_date
    from adapters import list_available_adapters
    from schemas import today_ch as _today_ch

    log.info("=== HEALTH CHECK (10:00 CET) ===")

    tz_ch = _tz_ch()
    today = _today_ch()

    SessionLocal = init_db()
    available    = set(list_available_adapters())
    missing      = []

    with SessionLocal() as session:
        for t in get_active_tariffs(session):
            if t.adapter_class not in available:
                log.info(f"[{t.tariff_id}] skip — adapter non implementato")
                continue
            has_data = has_data_for_date(session, t.tariff_id, today, tz_ch)
            if has_data:
                log.info(f"[{t.tariff_id}] ✓ Dati presenti per oggi ({today})")
            else:
                log.warning(f"[{t.tariff_id}] ❌ Nessun dato per oggi ({today})")
                missing.append((t.tariff_id, t.provider_name))

    if missing:
        log.error(f"Health check: {len(missing)} tariffe senza dati per oggi")
        await send_missing_alert(missing, today, context="morning health check (10:00 CET)")
    else:
        log.info("Health check: ✓ tutte le tariffe hanno dati per oggi")


# ── Fetch tutte le tariffe (CLI --now) ────────────────────────────────────────

async def fetch_all(target_date: Optional[date] = None) -> dict[str, bool]:
    from database import init_db, get_active_tariffs, seed_tariffs
    from schemas import tomorrow_ch
    import json

    if target_date is None:
        target_date = tomorrow_ch()

    log.info(f"=== FETCH ALL per {target_date} ===")
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

    # ── JOB 1: Fetch primario CKW (12:05 CET = 11:05 UTC) ────────────────────
    async def job_ckw():
        log.info("=== JOB CKW PRIMARIO (11:05 UTC = 12:05 CET) ===")
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

    # ── JOB 2: Recovery CKW (17:30 CET = 16:30 UTC) ──────────────────────────
    async def job_ckw_recovery():
        log.info("=== JOB CKW RECOVERY (16:30 UTC = 17:30 CET) ===")
        from schemas import tomorrow_ch
        target = tomorrow_ch()
        await fetch_missing_for_date(target, adapter_class_filter="CkwAdapter", label="ckw_recovery")

    # ── JOB 3: Fetch primario altri EVU (18:30 CET = 17:30 UTC) ──────────────
    async def job_others():
        log.info("=== JOB ALTRI EVU PRIMARIO (17:30 UTC = 18:30 CET) ===")
        asyncio.sleep(random.uniform(0, 30))
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
            await asyncio.sleep(random.uniform(0, 30))
            async with semaphore:
                await fetch_with_retry(config, target, None)

        await asyncio.gather(*[bounded(c) for c in other_configs])

    # ── JOB 4: Recovery tutti (21:00 CET = 20:00 UTC) ────────────────────────
    async def job_all_recovery():
        log.info("=== JOB ALL RECOVERY (20:00 UTC = 21:00 CET) ===")
        from schemas import tomorrow_ch
        target = tomorrow_ch()
        await fetch_missing_for_date(target, label="all_recovery")

    # ── JOB 5: Last resort (23:30 CET = 22:30 UTC) ───────────────────────────
    async def job_last_resort():
        log.info("=== LAST RESORT (22:30 UTC = 23:30 CET) ===")
        from schemas import tomorrow_ch
        target = tomorrow_ch()
        still_missing = await fetch_missing_for_date(target, label="last_resort")
        if still_missing:
            log.error(
                f"LAST RESORT: {len(still_missing)} tariffe ANCORA senza dati per {target} "
                f"alle 23:30 CET — invio alert email"
            )
            await send_missing_alert(
                still_missing,
                target,
                context="last resort check (23:30 CET) — all recovery attempts exhausted",
            )

    # ── JOB 6: Health check mattutino (10:00 CET = 09:00 UTC) ────────────────
    async def job_health_check():
        await run_health_check()

    # ── JOB 7: Manutenzione mensile (2:00 CET = 1:00 UTC, 1° del mese) ───────
    async def job_monthly():
        from backfill import monthly_maintenance
        await monthly_maintenance()

    # ── Registrazione job ─────────────────────────────────────────────────────
    scheduler.add_job(job_ckw,           CronTrigger(hour=11, minute=5),       id="ckw_fetch")
    scheduler.add_job(job_ckw_recovery,  CronTrigger(hour=16, minute=30),      id="ckw_recovery")
    scheduler.add_job(job_others,        CronTrigger(hour=17, minute=30),      id="others_fetch")
    scheduler.add_job(job_all_recovery,  CronTrigger(hour=20, minute=0),       id="all_recovery")
    scheduler.add_job(job_last_resort,   CronTrigger(hour=22, minute=30),      id="last_resort")
    scheduler.add_job(job_health_check,  CronTrigger(hour=9,  minute=0),       id="health_check")
    scheduler.add_job(job_monthly,       CronTrigger(day=1, hour=1, minute=0), id="monthly")

    log.info("╔══════════════════════════════════════════════════════════╗")
    log.info("║           Swiss Tariff Hub — Scheduler                   ║")
    log.info("╠══════════════════════════════════════════════════════════╣")
    log.info("║  11:05 UTC (12:05 CET) → fetch CKW primario             ║")
    log.info("║  16:30 UTC (17:30 CET) → recovery CKW  [no-op se ok]   ║")
    log.info("║  17:30 UTC (18:30 CET) → fetch altri EVU primario       ║")
    log.info("║  20:00 UTC (21:00 CET) → recovery tutti [no-op se ok]   ║")
    log.info("║  22:30 UTC (23:30 CET) → last resort   [alert se vuoto] ║")
    log.info("║  09:00 UTC (10:00 CET) → health check mattutino         ║")
    log.info("║  1° mese   01:00 UTC   → manutenzione mensile           ║")
    log.info("╚══════════════════════════════════════════════════════════╝")

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
    tz_ch = _tz_ch()

    if "--date" in sys.argv:
        idx = sys.argv.index("--date") + 1
        if idx < len(sys.argv):
            try:
                return date.fromisoformat(sys.argv[idx])
            except ValueError:
                print(f"Data non valida: {sys.argv[idx]}. Formato: YYYY-MM-DD")
                sys.exit(1)

    if "--now" in sys.argv or "--tariff" in sys.argv or "--recovery" in sys.argv:
        return datetime.now(timezone.utc).astimezone(tz_ch).date()

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

    if "--recovery" in sys.argv:
        log.info(f"Recovery manuale per {target}")
        still_missing = await fetch_missing_for_date(target, label="manual_recovery")
        print()
        if still_missing:
            print(f"  ⚠️  Ancora {len(still_missing)} tariffe senza dati:")
            for tid, provider in still_missing:
                print(f"     ✗ {tid}  ({provider})")
        else:
            print("  ✓ Tutti i dati presenti per", target)
        return

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
        # Fetch manuale resetta esplicitamente il circuit breaker
        _failure_streak.pop(tariff_id, None)
        log.info(f"Fetch manuale: {tariff_id} per {target} (circuit breaker resettato)")
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
    if any(f in sys.argv for f in ["--now", "--tariff", "--health", "--test-email", "--recovery"]):
        asyncio.run(cli())
    else:
        
        start_scheduler()