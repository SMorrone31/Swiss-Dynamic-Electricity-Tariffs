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

def months_in_window(today: date = None, window: int = 3) -> list[tuple[int, int]]:
    """
    Ritorna la lista di (year, month) nella finestra corrente.
    window=3 → mese corrente + 2 precedenti.
    """
    if today is None:
        today = date.today()
    result = []
    y, m = today.year, today.month
    for _ in range(window):
        result.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(result))  # ordine cronologico


def days_in_month(year: int, month: int) -> list[date]:
    """Tutti i giorni di un mese, escludendo i giorni futuri."""
    today = date.today()
    _, last_day = monthrange(year, month)
    return [
        date(year, month, d)
        for d in range(1, last_day + 1)
        if date(year, month, d) <= today
    ]


def cutoff_date(window: int = 3) -> date:
    """Data prima della quale i dati vengono cancellati."""
    today = date.today()
    y, m = today.year, today.month
    for _ in range(window):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    # Primo giorno del mese più vecchio nella finestra
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
    """
    from database import init_db, get_active_tariffs, seed_tariffs, PriceSlotDB
    from adapters import get_adapter, list_available_adapters, AdapterError, AdapterEmptyError

    SessionLocal = init_db()

    # Seed tariffe se DB vuoto
    with SessionLocal() as session:
        tariffs = get_active_tariffs(session)
        if not tariffs:
            log.info("DB vuoto — seed tariffe da evu_list.json")
            seed_tariffs(session)
            tariffs = get_active_tariffs(session)

    available = set(list_available_adapters())
    active_tariffs = [t for t in tariffs if t.adapter_class in available]

    if not active_tariffs:
        log.warning("Nessuna tariffa con adapter implementato — aggiungi gli adapter prima")
        return {}

    window = months_in_window(window=3)
    log.info(f"Finestra: {window[0][0]}-{window[0][1]:02d} → {window[-1][0]}-{window[-1][1]:02d}")
    log.info(f"Tariffe da backfillare: {[t.tariff_id for t in active_tariffs]}")

    stats = {"ok": 0, "skip": 0, "skip_no_backfill": 0, "fail": 0, "total": 0}

    for year, month in window:
        days = days_in_month(year, month)
        is_current = (year, month) == (date.today().year, date.today().month)
        log.info(f"\n--- {year}-{month:02d} {'(corrente)' if is_current else '(storico)'} ---")

        for target_date in days:
            # Salta solo i giorni futuri (non oggi)
            if target_date > date.today():
                continue

            stats["total"] += 1

            for tariff in active_tariffs:
                # Verifica se i dati esistono già
                # Verifica se i dati esistono già — conta gli slot
                # usando una finestra UTC leggermente allargata per catturare
                # tutti gli slot del giorno in ora locale svizzera
                # (es. 1 gen 00:00 CET = 31 dic 23:00 UTC)
                if not force:
                    with SessionLocal() as session:
                        # Usa strftime spazio per matchare il formato salvato nel DB SQLite.
                        # datetime aware → SQLAlchemy → isoformat T → no match → existing=0 sempre.
                        # Fix: stringa spazio diretta, identica al formato nel DB.
                        start_naive = (datetime(target_date.year, target_date.month,
                                               target_date.day, tzinfo=timezone.utc)
                                       - timedelta(hours=2)).replace(tzinfo=None)
                        end_naive   = (start_naive + timedelta(hours=28))
                        start_str   = start_naive.strftime("%Y-%m-%d %H:%M:%S")
                        end_str     = end_naive.strftime("%Y-%m-%d %H:%M:%S")
                        existing = session.query(PriceSlotDB).filter(
                            PriceSlotDB.tariff_id == tariff.tariff_id,
                            PriceSlotDB.slot_start_utc >= start_str,
                            PriceSlotDB.slot_start_utc < end_str,
                        ).count()

                        if existing >= 88:  # abbastanza slot (92 nei giorni DST)
                            stats["skip"] += 1
                            continue

                # Salta se l'adapter non supporta backfill storico
                # (es. Primeo: API senza memoria storica, solo day-ahead)
                import json as _json
                config = _json.loads(tariff.full_config_json)
                if not config.get("api_params", {}).get("backfill_supported", True):
                    log.debug(
                        f"  {Y}↷{D} {tariff.tariff_id} / {target_date}: "
                        f"backfill storico non supportato (API day-ahead only)"
                    )
                    stats["skip_no_backfill"] += 1
                    stats["skip"] += 1
                    continue

                if dry_run:
                    log.info(f"  [DRY RUN] {tariff.tariff_id} / {target_date}")
                    stats["ok"] += 1
                    continue

                # Fetch e salva
                import json
                config = json.loads(tariff.full_config_json)
                try:
                    adapter = get_adapter(config)
                    result  = await adapter.fetch(target_date)

                    from database import upsert_prices
                    with SessionLocal() as session:
                        saved = upsert_prices(session, result)

                    log.info(
                        f"  {G}✓{D} {tariff.tariff_id} / {target_date} "
                        f"→ {saved} slot | avg: {result.avg_total_price*100:.1f} Rp"
                        if result.avg_total_price else
                        f"  {G}✓{D} {tariff.tariff_id} / {target_date} → {saved} slot"
                    )
                    stats["ok"] += 1

                    # Piccola pausa per non sovraccaricare l'API
                    await asyncio.sleep(0.5)

                except AdapterEmptyError as e:
                    log.warning(f"  {Y}!{D} {tariff.tariff_id} / {target_date}: {e}")
                    stats["skip"] += 1
                except AdapterError as e:
                    log.error(f"  {R}✗{D} {tariff.tariff_id} / {target_date}: {e}")
                    stats["fail"] += 1
                except Exception as e:
                    log.error(f"  {R}✗{D} {tariff.tariff_id} / {target_date}: {type(e).__name__}: {e}")
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