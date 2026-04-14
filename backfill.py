"""
backfill.py
===========
Carica i dati storici per la finestra mobile di 2 mesi.

Logica della finestra:
  - Mese corrente (live):          dati aggiornati ogni sera dallo scheduler
  - Mese precedente (completato):  dati completi
  - 2 mesi fa (completato):        dati completi
  - Dati più vecchi:               cancellati automaticamente

Esempio a marzo 2026:
  Finestra attiva: gennaio, febbraio, marzo
  Aprile 1° → cancella gennaio, mantieni febbraio + marzo, aggiungi aprile live

Uso:
  python backfill.py                    # carica i 2 mesi precedenti mancanti
  python backfill.py --force            # ricarica tutto anche se già presente
  python backfill.py --prune-only       # solo cancella i dati fuori finestra
  python backfill.py --status           # mostra lo stato del DB senza modifiche
"""

import asyncio
import logging
import sys
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from schemas import today_ch as today_ch

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill")

G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"
B = "\033[94m"; D = "\033[0m"; BOLD = "\033[1m"


# ── Helpers data ──────────────────────────────────────────────────────────────

def months_in_window(today=None, window: int = 3):
    """
    Ritorna la lista di (year, month) nella finestra corrente.
    window=3 → mese corrente + 2 precedenti.
 
    FIX: usa today_ch() invece di date.today() — corretto per i calcoli
    tra le 22:00-00:00 UTC quando la data UTC è già il giorno successivo
    ma in Svizzera è ancora il giorno precedente.
    """
    if today is None:
        try:
            from schemas import today_ch as _today_ch
            today = _today_ch()
        except ImportError:
            from datetime import date
            today = date.today()
 
    result = []
    y, m = today.year, today.month
    for _ in range(window):
        result.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(result))

def days_in_month(year: int, month: int):
    """
    Tutti i giorni di un mese, escludendo i giorni futuri.
 
    FIX: usa today_ch() invece di date.today().
    """
    from calendar import monthrange
    from datetime import date
    try:
        from schemas import today_ch as _today_ch
        today = _today_ch()
    except ImportError:
        today = date.today()
 
    _, last_day = monthrange(year, month)
    return [
        date(year, month, d)
        for d in range(1, last_day + 1)
        if date(year, month, d) <= today
    ]


def cutoff_date(window: int = 3):
    """
    Data prima della quale i dati vengono cancellati.
 
    FIX: usa today_ch() invece di date.today().
    """
    try:
        from schemas import today_ch as _today_ch
        today = _today_ch()
    except ImportError:
        from datetime import date
        today = date.today()
 
    y, m = today.year, today.month
    for _ in range(window):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    from datetime import date
    return date(y, m, 1)


# ── Status ────────────────────────────────────────────────────────────────────

def show_status():
    """Mostra lo stato attuale del DB per ogni mese nella finestra."""
    from database import init_db, get_active_tariffs, PriceSlotDB
    from sqlalchemy import func

    SessionLocal = init_db()
    window = months_in_window()

    print(f"\n{BOLD}Finestra attiva: {window[0][0]}-{window[0][1]:02d} → {window[-1][0]}-{window[-1][1]:02d}{D}")
    print(f"Data cutoff: {cutoff_date()} (dati prima di questa data verranno cancellati)\n")

    with SessionLocal() as session:
        tariffs = get_active_tariffs(session)
        from adapters import list_available_adapters
        available = set(list_available_adapters())
        active_tariffs = [t for t in tariffs if t.adapter_class in available]

        if not active_tariffs:
            print(f"{Y}Nessuna tariffa con adapter implementato{D}")
            return

        for year, month in window:
            days = days_in_month(year, month)
            is_current = (year, month) == (date.today().year, date.today().month)
            tag = f"{G}● live{D}" if is_current else "  completato"
            print(f"  {BOLD}{year}-{month:02d}{D} {tag}  ({len(days)} giorni disponibili)")

            for tariff in active_tariffs:
                # Recupera tutti gli slot del mese per questa tariffa
                start = datetime(year, month, 1, tzinfo=timezone.utc)
                if month == 12:
                    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
                else:
                    end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

                slots = session.query(
                    PriceSlotDB.slot_start_utc
                ).filter(
                    PriceSlotDB.tariff_id == tariff.tariff_id,
                    PriceSlotDB.slot_start_utc >= start,
                    PriceSlotDB.slot_start_utc < end,
                ).all()

                # Conta i giorni distinti in ora locale svizzera (CET/CEST)
                # per evitare di contare giorni di confine UTC che appartengono
                # al mese precedente/successivo in ora locale
                try:
                    import zoneinfo
                    tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
                except ImportError:
                    import pytz
                    tz_ch = pytz.timezone("Europe/Zurich")

                local_dates = set()
                for (slot_dt,) in slots:
                    if slot_dt.tzinfo is None:
                        slot_dt = slot_dt.replace(tzinfo=timezone.utc)
                    local_date = slot_dt.astimezone(tz_ch).date()
                    # Conta solo i giorni che appartengono a questo mese
                    if local_date.year == year and local_date.month == month:
                        local_dates.add(local_date)

                count = len(local_dates)

                pct = count / len(days) * 100 if days else 0
                bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
                color = G if pct >= 90 else Y if pct >= 50 else R
                print(f"    {tariff.tariff_id:<30} {color}{bar}{D} {count}/{len(days)} giorni")

        print()


# ── Prune ─────────────────────────────────────────────────────────────────────

def prune_old_data(dry_run: bool = False) -> int:
    """
    Cancella i dati più vecchi della finestra (> 2 mesi fa).
    Ritorna il numero di slot cancellati.
    """
    from database import init_db, PriceSlotDB
    from sqlalchemy import text

    cutoff = cutoff_date(window=3)
    cutoff_dt = datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=timezone.utc)

    SessionLocal = init_db()
    with SessionLocal() as session:
        # Conta prima
        count = session.query(PriceSlotDB).filter(
            PriceSlotDB.slot_start_utc < cutoff_dt
        ).count()

        if count == 0:
            log.info(f"Nessun dato da cancellare (cutoff: {cutoff})")
            return 0

        if dry_run:
            log.info(f"[DRY RUN] Verrebbero cancellati {count} slot prima del {cutoff}")
            return count

        session.query(PriceSlotDB).filter(
            PriceSlotDB.slot_start_utc < cutoff_dt
        ).delete(synchronize_session=False)
        session.commit()
        log.info(f"{G}Cancellati {count} slot prima del {cutoff}{D}")
        return count


# ── Backfill ──────────────────────────────────────────────────────────────────

async def backfill(force: bool = False, dry_run: bool = False) -> dict:
    """
    Carica i dati storici mancanti per la finestra di 2 mesi.
    Salta i giorni già presenti nel DB (a meno che force=True).
 
    FIX SessionLocal:
      - init_db() è ora un singleton: chiamarlo qui ritorna la factory
        già creata all'avvio di FastAPI, senza ricreare il connection pool.
      - La session per l'exists-check viene aperta e chiusa correttamente
        con context manager per ogni check (non tenuta aperta per tutto il loop,
        che causerebbe idle connection troppo lunga su PostgreSQL).
    """
    import asyncio
    import json as _json
    from datetime import date, datetime, timedelta, timezone
    from database import init_db, get_active_tariffs, seed_tariffs, PriceSlotDB, has_data_for_date
    from adapters import get_adapter, list_available_adapters, AdapterError, AdapterEmptyError
 
    try:
        from schemas import today_ch as _today_ch
        _today = _today_ch()
    except ImportError:
        _today = date.today()
 
    # FIX: init_db() è singleton — non ricrea il pool, ritorna quello esistente
    SessionLocal = init_db()
 
    with SessionLocal() as session:
        tariffs = get_active_tariffs(session)
        if not tariffs:
            log.info("DB vuoto — seed tariffe da evu_list.json")
            seed_tariffs(session)
            tariffs = get_active_tariffs(session)
        # Materializziamo la lista fuori dalla session (expire_on_commit=False)
        active_tariffs = [
            t for t in tariffs
            if t.adapter_class in list_available_adapters()
        ]
        # Pre-carica le config JSON fuori dalla session
        tariff_configs = {
            t.tariff_id: _json.loads(t.full_config_json)
            for t in active_tariffs
        }
 
    if not active_tariffs:
        log.warning("Nessuna tariffa con adapter implementato")
        return {}
 
    try:
        import zoneinfo
        tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz
        tz_ch = pytz.timezone("Europe/Zurich")
 
    window = months_in_window(window=3)
    log.info(f"Finestra: {window[0][0]}-{window[0][1]:02d} → {window[-1][0]}-{window[-1][1]:02d}")
    log.info(f"Tariffe: {[t.tariff_id for t in active_tariffs]}")
 
    stats = {"ok": 0, "skip": 0, "skip_no_backfill": 0, "fail": 0, "total": 0}
 
    for year, month in window:
        days = days_in_month(year, month)
        is_current = (year, month) == (_today.year, _today.month)
        log.info(f"\n--- {year}-{month:02d} {'(corrente)' if is_current else '(storico)'} ---")
 
        for target_date in days:
            if target_date > _today:
                continue
 
            stats["total"] += 1
 
            for tariff in active_tariffs:
                config = tariff_configs[tariff.tariff_id]
 
                # Salta se non supporta backfill storico
                if not config.get("api_params", {}).get("backfill_supported", True):
                    log.debug(
                        f"  ↷ {tariff.tariff_id} / {target_date}: "
                        f"day-ahead only — skip"
                    )
                    stats["skip_no_backfill"] += 1
                    stats["skip"] += 1
                    continue
 
                # Verifica se i dati esistono già — session aperta solo per la query
                if not force:
                    with SessionLocal() as session:
                        already = has_data_for_date(session, tariff.tariff_id, target_date, tz_ch)
                    if already:
                        stats["skip"] += 1
                        continue
 
                if dry_run:
                    log.info(f"  [DRY RUN] {tariff.tariff_id} / {target_date}")
                    stats["ok"] += 1
                    continue
 
                # Fetch e salva
                try:
                    adapter = get_adapter(config)
                    result  = await adapter.fetch(target_date)
 
                    from database import upsert_prices
                    with SessionLocal() as session:
                        saved = upsert_prices(session, result)
 
                    avg_str = (
                        f" | avg: {result.avg_total_price*100:.1f} Rp"
                        if result.avg_total_price else ""
                    )
                    log.info(f"  ✓ {tariff.tariff_id} / {target_date} → {saved} slot{avg_str}")
                    stats["ok"] += 1
 
                    await asyncio.sleep(0.5)   # throttle per non martellare l'API
 
                except AdapterEmptyError as e:
                    log.warning(f"  ! {tariff.tariff_id} / {target_date}: {e}")
                    stats["skip"] += 1
                except AdapterError as e:
                    log.error(f"  ✗ {tariff.tariff_id} / {target_date}: {e}")
                    stats["fail"] += 1
                except Exception as e:
                    log.error(f"  ✗ {tariff.tariff_id} / {target_date}: {type(e).__name__}: {e}")
                    stats["fail"] += 1
 
    return stats
 

# ── Job mensile (chiamato dallo scheduler) ────────────────────────────────────

async def monthly_maintenance():
    """
    Eseguito automaticamente il 1° di ogni mese dallo scheduler.
    1. Cancella i dati fuori finestra
    2. Avvia backfill per eventuali giorni mancanti
    """
    log.info("=== MANUTENZIONE MENSILE ===")
    today = date.today()
    log.info(f"Data: {today} — mese appena iniziato: {today.month}/{today.year}")

    # 1. Prune
    pruned = prune_old_data(dry_run=False)
    log.info(f"Prune: {pruned} slot rimossi")

    # 2. Backfill (solo dati mancanti, non force)
    stats = await backfill(force=False)
    log.info(f"Backfill: {stats}")

    log.info("=== MANUTENZIONE MENSILE COMPLETATA ===")


# ── CLI ───────────────────────────────────────────────────────────────────────

async def main():
    force      = "--force"      in sys.argv
    prune_only = "--prune-only" in sys.argv
    status     = "--status"     in sys.argv
    dry_run    = "--dry-run"    in sys.argv

    print(f"{BOLD}Swiss Tariff Hub — Backfill{D}")
    print(f"Data corrente: {date.today()}")
    print(f"Finestra mesi: {[f'{y}-{m:02d}' for y, m in months_in_window()]}")

    if status:
        show_status()
        return

    if prune_only:
        pruned = prune_old_data(dry_run=dry_run)
        print(f"\nSlot {'da cancellare' if dry_run else 'cancellati'}: {pruned}")
        return

    # Prima prune, poi backfill
    if not dry_run:
        pruned = prune_old_data()
        if pruned:
            log.info(f"Rimossi {pruned} slot fuori finestra")

    stats = await backfill(force=force, dry_run=dry_run)

    print(f"\n{BOLD}Riepilogo:{D}")
    print(f"  {G}✓ OK:{D}    {stats.get('ok', 0)}")
    print(f"  {Y}→ Skip:{D}  {stats.get('skip', 0)} (già presenti)")
    nb = stats.get('skip_no_backfill', 0)
    if nb:
        print(f"  {Y}↷ No-backfill:{D} {nb} (API day-ahead only — Primeo/AVAG/ELAG)")
    print(f"  {R}✗ Fail:{D}  {stats.get('fail', 0)}")
    print(f"\nProssimo step: avvia uvicorn e controlla la dashboard.")


if __name__ == "__main__":
    asyncio.run(main())