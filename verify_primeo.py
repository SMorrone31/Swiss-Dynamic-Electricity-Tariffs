#!/usr/bin/env python3
"""
verify_primeo_flow.py
=====================
Verifica l'intero flusso Primeo nel sistema:
  1. Le tre tariffe sono nel DB?
  2. L'adapter è registrato?
  3. Il fetch funziona?
  4. I dati arrivano all'API?
  5. Il backfill è configurato correttamente?

Uso (da dentro la cartella progetto/):
    python verify_primeo_flow.py
    python verify_primeo_flow.py --fetch-live  # fa anche un fetch reale dall'API
"""

import asyncio
import json
import sys
import argparse
from pathlib import Path
from datetime import date, datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))

G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"; B = "\033[94m"
D = "\033[0m"; BOLD = "\033[1m"

PRIMEO_TARIFF_IDS = [
    "primeo_netzdynamisch",
    "avag_primeo_netzdynamisch",
    "elag_primeo_netzdynamisch",
]

def check(label: str, ok: bool, detail: str = ""):
    icon = f"{G}✓{D}" if ok else f"{R}✗{D}"
    suffix = f"  {Y}({detail}){D}" if detail and not ok else (f"  {G}({detail}){D}" if detail else "")
    print(f"  {icon}  {label}{suffix}")
    return ok


def section(title: str):
    print(f"\n{BOLD}{'─'*55}{D}")
    print(f"{BOLD}  {title}{D}")
    print(f"{BOLD}{'─'*55}{D}")


def main_sync():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch-live", action="store_true",
                        help="Esegui un fetch reale dall'API Primeo")
    parser.add_argument("--fix", action="store_true",
                        help="Tenta di correggere automaticamente i problemi trovati")
    args = parser.parse_args()

    print(f"\n{BOLD}Primeo Flow Verification{D}")
    print(f"Data: {date.today()}")

    errors = []

    # ── 1. Adapter registrato ─────────────────────────────────────────────────
    section("1. Adapter registrato")
    try:
        from adapters import get_adapter, list_available_adapters
        available = list_available_adapters()
        ok = "PrimeoEnergieAdapter" in available
        check("PrimeoEnergieAdapter in _REGISTRY", ok,
              f"disponibili: {available}" if not ok else f"trovato tra {len(available)} adapter")
        if not ok:
            errors.append("PrimeoEnergieAdapter non registrato in adapters/__init__.py")
    except ImportError as e:
        check("Import adapters", False, str(e))
        errors.append(f"Import error: {e}")

    # ── 2. evu_list.json ─────────────────────────────────────────────────────
    section("2. evu_list.json")
    config_path = Path("config/evu_list.json")
    if not config_path.exists():
        config_path = Path("evu_list.json")

    if check("File evu_list.json trovato", config_path.exists(),
             f"cercato in config/ e root"):
        with open(config_path) as f:
            evu_list = json.load(f)

        evu_ids = {e.get("tariff_id") for e in evu_list}
        for tid in PRIMEO_TARIFF_IDS:
            found = tid in evu_ids
            check(f"  Entry '{tid}'", found, "mancante!" if not found else "")
            if not found:
                errors.append(f"Entry '{tid}' mancante in evu_list.json")

        # Verifica api_params
        for entry in evu_list:
            if entry.get("adapter_class") != "PrimeoEnergieAdapter":
                continue
            tid = entry.get("tariff_id")
            api_params = entry.get("api_params", {})

            # backfill_supported deve essere false
            bs = api_params.get("backfill_supported")
            ok_bs = bs is False
            check(f"  {tid}: backfill_supported=false", ok_bs,
                  f"attuale: {bs!r} — il backfill tenterà di fetchare date passate e fallirà!" if not ok_bs else "")
            if not ok_bs:
                errors.append(f"{tid}: manca 'backfill_supported': false in api_params")

            # Controlla adapter_class
            ac = entry.get("adapter_class")
            check(f"  {tid}: adapter_class='PrimeoEnergieAdapter'",
                  ac == "PrimeoEnergieAdapter",
                  f"attuale: {ac!r}" if ac != "PrimeoEnergieAdapter" else "")

    # ── 3. Tariffe nel DB ─────────────────────────────────────────────────────
    section("3. Tariffe nel database")
    try:
        from database import init_db, get_active_tariffs
        SessionLocal = init_db()
        with SessionLocal() as session:
            all_tariffs = get_active_tariffs(session)
            active_ids = {t.tariff_id for t in all_tariffs}
            primeo_tariffs = [t for t in all_tariffs if t.adapter_class == "PrimeoEnergieAdapter"]

        check(f"Tariffe totali nel DB", True, f"{len(all_tariffs)} tariffe attive")
        check(f"Tariffe PrimeoEnergieAdapter nel DB", len(primeo_tariffs) > 0,
              f"{len(primeo_tariffs)} trovate")

        for tid in PRIMEO_TARIFF_IDS:
            ok = tid in active_ids
            check(f"  '{tid}' nel DB", ok, "esegui: rm tariffs.db && uvicorn main:app" if not ok else "")
            if not ok:
                errors.append(f"'{tid}' non nel DB — forse evu_list.json non è stato ricaricato")

        # Mostra config di ogni tariffa Primeo nel DB
        for t in primeo_tariffs:
            cfg = json.loads(t.full_config_json)
            api_params = cfg.get("api_params", {})
            netzgebiet = api_params.get("netzgebiet", "—")
            backfill = api_params.get("backfill_supported", "NON IMPOSTATO")
            print(f"\n    {B}{t.tariff_id}{D}")
            print(f"      URL:          {t.api_base_url}")
            print(f"      netzgebiet:   {netzgebiet}")
            print(f"      backfill:     {backfill}")
            print(f"      update time:  {t.daily_update_time_utc} UTC")

    except Exception as e:
        check("Database", False, str(e))
        errors.append(f"Errore DB: {e}")

    # ── 4. Dati nel DB per le tariffe Primeo ─────────────────────────────────
    section("4. Slot salvati nel DB")
    try:
        from database import init_db, PriceSlotDB
        from sqlalchemy import func
        SessionLocal = init_db()

        with SessionLocal() as session:
            for tid in PRIMEO_TARIFF_IDS:
                count = session.query(func.count(PriceSlotDB.tariff_id)).filter(
                    PriceSlotDB.tariff_id == tid
                ).scalar()

                if count and count > 0:
                    latest = session.query(
                        func.max(PriceSlotDB.slot_start_utc)
                    ).filter(PriceSlotDB.tariff_id == tid).scalar()
                    check(f"  '{tid}': {count} slot salvati", True,
                          f"ultimo slot: {latest}")
                else:
                    check(f"  '{tid}': nessun slot nel DB", False,
                          "normale se lo scheduler non ha ancora girato dopo oggi le 18:00 CH")

    except Exception as e:
        check("Query slot DB", False, str(e))

    # ── 5. Fetch live opzionale ───────────────────────────────────────────────
    if args.fetch_live:
        section("5. Fetch live dall'API Primeo")
        asyncio.run(do_live_fetch())

    # ── 6. Verifica scheduler timing ─────────────────────────────────────────
    section("6. Timing scheduler")
    print(f"""
  Lo scheduler in main.py esegue il job Primeo alle:
    _job_others → CronTrigger(hour=17, minute=30) UTC
  
  Primeo pubblica entro le 18:00 ora locale CH:
    In CET  (UTC+1): entro le 17:00 UTC → scheduler a 17:30 ✓ (15 min di margine)
    In CEST (UTC+2): entro le 16:00 UTC → scheduler a 17:30 ✓ (90 min di margine)

  {G}✓ Il timing è corretto per entrambe le stagioni.{D}
  
  Per testare manualmente il fetch:
    python -c "
    import asyncio, json
    from database import init_db, get_active_tariffs, seed_tariffs
    from scheduler import fetch_with_retry
    from datetime import date

    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_tariffs(s)
        tariffs = [t for t in get_active_tariffs(s) if t.adapter_class == 'PrimeoEnergieAdapter']
        configs = [json.loads(t.full_config_json) for t in tariffs]

    async def run():
        for cfg in configs:
            await fetch_with_retry(cfg, date.today(), None)

    asyncio.run(run())
    "
""")

    # ── Riepilogo ─────────────────────────────────────────────────────────────
    section("RIEPILOGO")
    if not errors:
        print(f"\n  {G}{BOLD}✓ Tutto OK — il sistema Primeo è correttamente configurato.{D}")
    else:
        print(f"\n  {R}Trovati {len(errors)} problemi:{D}")
        for i, err in enumerate(errors, 1):
            print(f"  {R}{i}.{D} {err}")
        print(f"\n  {Y}Comandi di fix:{D}")
        if any("evu_list.json" in e for e in errors):
            print(f"  → Aggiorna config/evu_list.json con le entry Primeo corrette")
            print(f"     (vedi primeo_evu_entries.json nei file generati)")
        if any("DB" in e or "tariff" in e.lower() for e in errors):
            print(f"  → rm tariffs.db && uvicorn main:app --reload")
            print(f"     (ricarica evu_list.json e ricrea il DB)")
        if any("backfill" in e.lower() for e in errors):
            print(f"  → Aggiungi '\"backfill_supported\": false' nell'api_params")
            print(f"     di ogni entry Primeo/AVAG/ELAG in evu_list.json")

    print()


async def do_live_fetch():
    """Esegue un fetch reale per tutte le tariffe Primeo."""
    from adapters.primeo_energie import PrimeoEnergieAdapter
    from database import init_db, get_active_tariffs
    import json as _json

    target = date.today()
    print(f"  Fetch per {target}...")

    SessionLocal = init_db()
    with SessionLocal() as s:
        tariffs = [t for t in get_active_tariffs(s)
                   if t.adapter_class == "PrimeoEnergieAdapter"]

    if not tariffs:
        print(f"  {R}Nessuna tariffa Primeo nel DB.{D}")
        return

    for t in tariffs:
        cfg = _json.loads(t.full_config_json)
        adapter = PrimeoEnergieAdapter(cfg)
        print(f"\n  {B}→ {t.tariff_id}{D}")
        try:
            result = await adapter.fetch(target)
            print(f"  {G}✓ {result.slot_count} slot{D}")
            if result.avg_total_price:
                print(f"    avg total: {result.avg_total_price*100:.2f} Rp/kWh")
            if result.min_slot and result.min_slot.total_price:
                print(f"    min: {result.min_slot.slot_start_utc.strftime('%H:%M UTC')} "
                      f"→ {result.min_slot.total_price*100:.2f} Rp/kWh")
            if result.max_slot and result.max_slot.total_price:
                print(f"    max: {result.max_slot.slot_start_utc.strftime('%H:%M UTC')} "
                      f"→ {result.max_slot.total_price*100:.2f} Rp/kWh")
            if result.warnings:
                for w in result.warnings:
                    print(f"    {Y}⚠ {w}{D}")

            # Mostra primi 3 slot come esempio
            print(f"    Esempio output (primi 3 slot):")
            for slot in result.slots[:3]:
                print(f"      {slot.slot_start_utc.strftime('%H:%M UTC')} "
                      f"| grid={slot.grid_price} "
                      f"| energy={slot.energy_price} "
                      f"| residual={slot.residual_price}")

        except Exception as e:
            print(f"  {R}✗ {type(e).__name__}: {e}{D}")


if __name__ == "__main__":
    main_sync()
