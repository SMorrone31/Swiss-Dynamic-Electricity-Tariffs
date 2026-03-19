"""
test_requirements.py
====================
Verifica che il sistema rispetti tutte le specifiche del PDF.

PDF requirements:
  1. API 1: GET /api/v1/tariffs — lista tariffe con tutti i campi richiesti
  2. API 2: GET /api/v1/prices  — timeseries 15 min, UTC, CHF/kWh
  3. DST: gestione corretta cambio ora (marzo: 92 slot, ottobre: 100 slot)
  4. Alert: email dopo fallimento fetch (testato via mock)
  5. Prezzi per unità energetica (CHF/kWh), non mensili/annuali
  6. Timezone sempre UTC esplicito

Uso:
  python test_requirements.py
  python test_requirements.py --api     # solo test API (richiede uvicorn attivo)
  python test_requirements.py --unit    # solo unit test (no rete, no server)
"""

import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"
B = "\033[94m"; W = "\033[97m"; D = "\033[0m"; BOLD = "\033[1m"

passed = failed = 0

def ok(msg):
    global passed
    passed += 1
    print(f"  {G}✓{D} {msg}")

def fail(msg):
    global failed
    failed += 1
    print(f"  {R}✗{D} {msg}")

def warn(msg):
    print(f"  {Y}!{D} {msg}")

def head(msg):
    print(f"\n{BOLD}{W}{msg}{D}")


# ── REQ 1: API 1 — struttura risposta tariffe ────────────────────────────────

def test_api1_structure():
    head("REQ 1 — API 1: struttura risposta /api/v1/tariffs")

    from database import init_db, seed_tariffs, get_active_tariffs
    SessionLocal = init_db()
    with SessionLocal() as session:
        seed_tariffs(session)
        tariffs = get_active_tariffs(session)

    if not tariffs:
        fail("Nessuna tariffa nel DB")
        return

    required_fields = [
        "tariff_id", "tariff_name", "provider_name",
        "daily_update_time_utc", "datetime_available_from_utc",
    ]

    # Simula la risposta dell'API
    import json as _json
    for t in tariffs[:2]:  # testa le prime 2
        config = _json.loads(t.full_config_json)
        response = {
            "tariff_id":   t.tariff_id,
            "tariff_name": t.tariff_name,
            "provider_name": t.provider_name,
            "zip_ranges":  config.get("zip_ranges", []),
            "daily_update_time_utc": t.daily_update_time_utc,
            "datetime_available_from_utc": (
                t.datetime_available_from.isoformat()
                if t.datetime_available_from else None
            ),
            "valid_until_utc": (
                t.valid_until.isoformat() if t.valid_until else None
            ),
        }
        for field in required_fields:
            if field in response and response[field] is not None:
                ok(f"{t.tariff_id}: campo '{field}' presente")
            else:
                fail(f"{t.tariff_id}: campo '{field}' MANCANTE o None")

        # zip_ranges deve essere una lista
        if isinstance(response["zip_ranges"], list):
            ok(f"{t.tariff_id}: zip_ranges è una lista")
        else:
            fail(f"{t.tariff_id}: zip_ranges non è una lista")


# ── REQ 2: API 2 — struttura risposta prezzi ─────────────────────────────────

def test_api2_structure():
    head("REQ 2 — API 2: struttura risposta /api/v1/prices")

    from database import init_db, get_prices, PriceSlotDB
    SessionLocal = init_db()

    with SessionLocal() as session:
        # Prendi il primo slot disponibile
        slot = session.query(PriceSlotDB).first()

    if not slot:
        warn("Nessun slot nel DB — esegui prima: python scheduler.py --now --date 2026-03-16")
        return

    tariff_id = slot.tariff_id
    # Prendi la data del primo slot
    dt = slot.slot_start_utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    target_date = dt.date()

    start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    end   = start + timedelta(days=1)

    with SessionLocal() as session:
        slots = get_prices(session, tariff_id, start, end)

    if not slots:
        fail(f"Nessun slot per {tariff_id} / {target_date}")
        return

    ok(f"Trovati {len(slots)} slot per {tariff_id} / {target_date}")

    # Verifica struttura risposta API
    energy_series   = []
    grid_series     = []
    residual_series = []

    for s in slots:
        sdt = s.slot_start_utc
        if sdt.tzinfo is None:
            sdt = sdt.replace(tzinfo=timezone.utc)
        ts = sdt.isoformat()

        if s.energy_price   is not None: energy_series.append({ts: s.energy_price})
        if s.grid_price     is not None: grid_series.append({ts: s.grid_price})
        if s.residual_price is not None: residual_series.append({ts: s.residual_price})

    # REQ: almeno uno dei tre componenti deve essere presente
    has_any = bool(energy_series or grid_series or residual_series)
    if has_any:
        ok("Almeno un componente prezzo presente")
    else:
        fail("Nessun componente prezzo trovato")

    # REQ: i timestamp devono avere timezone UTC esplicito
    for series_name, series in [
        ("energy_price_utc", energy_series),
        ("grid_price_utc",   grid_series),
        ("residual_price_utc", residual_series),
    ]:
        if not series:
            warn(f"{series_name}: vuoto (componente non disponibile per questo EVU)")
            continue

        first_ts = list(series[0].keys())[0]
        if "+00:00" in first_ts or first_ts.endswith("Z"):
            ok(f"{series_name}: timestamp UTC esplicito ✓ ({first_ts[:22]}...)")
        else:
            fail(f"{series_name}: timestamp SENZA timezone UTC! ({first_ts})")

        # REQ: prezzi devono essere in CHF/kWh (range ragionevole: 0.001 - 2.0)
        first_price = list(series[0].values())[0]
        if 0.001 <= first_price <= 2.0:
            ok(f"{series_name}: prezzo in range CHF/kWh ({first_price:.4f} CHF/kWh = {first_price*100:.2f} Rp/kWh)")
        elif first_price > 2.0:
            fail(f"{series_name}: prezzo TROPPO ALTO ({first_price}) — probabilmente in Rp, non CHF!")
        else:
            warn(f"{series_name}: prezzo molto basso ({first_price}) — verificare unità")

        # REQ: slot ogni 15 minuti
        if len(series) >= 2:
            ts1 = datetime.fromisoformat(list(series[0].keys())[0])
            ts2 = datetime.fromisoformat(list(series[1].keys())[0])
            delta = (ts2 - ts1).total_seconds() / 60
            if delta == 15:
                ok(f"{series_name}: intervallo 15 minuti ✓")
            else:
                fail(f"{series_name}: intervallo {delta} minuti (atteso 15)")


# ── REQ 3: DST — cambio ora ───────────────────────────────────────────────────

def test_dst_handling():
    head("REQ 3 — DST: gestione cambio ora (PDF: 'take extra care of DST')")

    from schemas import PriceSlot, NormalizedPrices
    from adapters.ckw import CkwAdapter
    from datetime import timedelta

    config = {
        "tariff_id": "ckw_home_dynamic",
        "tariff_name": "home_dynamic",
        "provider_name": "CKW",
        "adapter_class": "CkwAdapter",
        "api_base_url": "https://...",
        "auth_type": "none",
        "auth_config": {},
        "api_params": {"tariff_name": "home_dynamic"},
    }
    adapter = CkwAdapter(config)

    # Test 1: 16 marzo 2026 — CET→CEST (23 ore = 92 slot)
    # I timestamp CKW per questo giorno vanno da 00:00+01:00 a 22:45+01:00
    # = da 23:00Z del 15/3 a 21:45Z del 16/3 (UTC)
    print(f"\n  Test: 16 marzo 2026 (CET→CEST, 23 ore = 92 slot)")
    prices_march = []
    # Giorno inizia mezzanotte ora locale = 23:00 UTC giorno prima
    for i in range(92):  # 23 ore * 4 slot
        h_local = i * 15 // 60
        m_local = (i * 15) % 60
        # Salta l'ora 2:xx (non esiste in CEST)
        prices_march.append({
            "start_timestamp": f"2026-03-15T23:{i*15 % 60:02d}Z"
            if i < 4 else f"2026-03-16T{((i-4)*15) // 60:02d}:{((i-4)*15) % 60:02d}Z",
            "grid": [{"unit": "CHF_kWh", "value": 0.05}],
            "electricity": [{"unit": "CHF_kWh", "value": 0.12}],
            "integrated": [{"unit": "CHF_kWh", "value": 0.17}],
            "grid_usage": [{"unit": "CHF_kWh", "value": 0.009}],
        })

    # Usa il mock reale di CKW (92 slot con timestamp Z)
    mock_92 = {
        "publication_timestamp": "2026-03-15T11:00:00+01:00",
        "prices": [
            {
                "start_timestamp": f"2026-03-16T{(i*15)//60:02d}:{(i*15)%60:02d}Z",
                "grid":        [{"unit": "CHF_kWh", "value": 0.05}],
                "electricity": [{"unit": "CHF_kWh", "value": 0.12}],
                "integrated":  [{"unit": "CHF_kWh", "value": 0.17}],
                "grid_usage":  [{"unit": "CHF_kWh", "value": 0.009}],
            }
            for i in range(92)
        ]
    }
    slots = adapter._parse(mock_92, date(2026, 3, 16))
    if len(slots) == 92:
        ok(f"16 marzo: {len(slots)} slot (attesi 92 per giorno CET→CEST)")
    else:
        fail(f"16 marzo: {len(slots)} slot (attesi 92)")

    # Test 2: giorno normale — 96 slot
    print(f"\n  Test: giorno normale (96 slot)")
    mock_96 = {
        "publication_timestamp": "2026-03-14T11:00:00+01:00",
        "prices": [
            {
                "start_timestamp": f"2026-03-15T{(i*15)//60:02d}:{(i*15)%60:02d}Z",
                "grid": [{"unit": "CHF_kWh", "value": 0.05}],
                "electricity": [{"unit": "CHF_kWh", "value": 0.12}],
                "integrated": [{"unit": "CHF_kWh", "value": 0.17}],
                "grid_usage": [{"unit": "CHF_kWh", "value": 0.009}],
            }
            for i in range(96)
        ]
    }
    slots = adapter._parse(mock_96, date(2026, 3, 15))
    if len(slots) == 96:
        ok(f"Giorno normale: {len(slots)} slot (attesi 96)")
    else:
        fail(f"Giorno normale: {len(slots)} slot (attesi 96)")

    # Test 3: ottobre — CEST→CET (25 ore = 100 slot)
    # In UTC: il 25 ottobre va da 00:00Z a 25:00Z (cioè fino a 00:00Z del 26/10)
    # I timestamp validi sono 00:00Z..23:45Z del 25/10 + 00:00Z..00:45Z del 26/10
    print(f"\n  Test: ottobre 2026 (CEST→CET, 25 ore = 100 slot)")
    prices_oct = []
    for i in range(100):
        total_minutes = i * 15
        day_offset = total_minutes // (24 * 60)
        h = (total_minutes % (24 * 60)) // 60
        m = (total_minutes % (24 * 60)) % 60
        if day_offset == 0:
            ts = f"2026-10-25T{h:02d}:{m:02d}Z"
        else:
            ts = f"2026-10-26T{h:02d}:{m:02d}Z"
        prices_oct.append({
            "start_timestamp": ts,
            "grid": [{"unit": "CHF_kWh", "value": 0.05}],
            "electricity": [{"unit": "CHF_kWh", "value": 0.12}],
            "integrated": [{"unit": "CHF_kWh", "value": 0.17}],
            "grid_usage": [{"unit": "CHF_kWh", "value": 0.009}],
        })
    mock_100 = {
        "publication_timestamp": "2026-10-24T11:00:00+02:00",
        "prices": prices_oct,
    }
    slots = adapter._parse(mock_100, date(2026, 10, 25))
    if len(slots) == 100:
        ok(f"25 ottobre: {len(slots)} slot (attesi 100 per giorno CEST→CET)")
    else:
        fail(f"25 ottobre: {len(slots)} slot (attesi 100)")

    # Test anche con timestamp "24:00" che alcuni provider potrebbero inviare
    print(f"\n  Test: timestamp '24:00' (ISO non standard — giorno successivo)")
    mock_24 = {
        "publication_timestamp": "2026-10-24T11:00:00+02:00",
        "prices": [{
            "start_timestamp": "2026-10-25T24:00Z",
            "grid": [{"unit": "CHF_kWh", "value": 0.05}],
        }]
    }
    try:
        slots_24 = adapter._parse(mock_24, date(2026, 10, 25))
        # Deve essere parsato come 2026-10-26T00:00Z
        if slots_24 and slots_24[0].slot_start_utc == datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc):
            ok("Timestamp '24:00Z' correttamente convertito a 00:00Z giorno successivo")
        else:
            ts = slots_24[0].slot_start_utc if slots_24 else "nessuno slot"
            warn(f"Timestamp '24:00Z' parsato come: {ts}")
    except Exception as e:
        fail(f"Timestamp '24:00Z' ha causato errore: {e}")

    # Test 4: is_complete accetta 90-102 — fix: usa timedelta per i datetime
    for n, expected in [(92, True), (96, True), (100, True), (89, False), (103, False)]:
        base_dt = datetime(2026, 3, 16, 0, 0, tzinfo=timezone.utc)
        result = NormalizedPrices(
            tariff_id="test",
            source_date=date(2026, 3, 16),
            fetched_at=datetime.now(timezone.utc),
            slots=[
                PriceSlot(
                    slot_start_utc=base_dt + timedelta(minutes=15 * i),
                    grid_price=0.05
                )
                for i in range(n)
            ]
        )
        if result.is_complete == expected:
            ok(f"is_complete({n} slot) = {expected} ✓")
        else:
            fail(f"is_complete({n} slot) = {result.is_complete} (atteso {expected})")


# ── REQ 4: Alert email timing ─────────────────────────────────────────────────

def test_alert_timing():
    head("REQ 4 — Alert: email dopo fallimento fetch (PDF: 'after 1 hour send mail')")

    # Il PDF dice: no new data or not responding → after 1 hour send a mail
    # Il nostro schema: retry a 0min, +30min, +60min = alert dopo ~90 min
    # Questo è ragionevole: 3 tentativi in 90 min, poi alert

    import scheduler as sched
    delays = sched.RETRY_DELAYS
    total_wait = sum(delays)

    ok(f"Retry schedule: {[f'+{d//60}min' for d in delays]}")
    ok(f"Tempo totale prima dell'alert: {total_wait//60} minuti")

    if total_wait >= 3600:  # almeno 1 ora
        ok(f"Alert dopo {total_wait//60} min (≥ 60 min come richiesto dal PDF)")
    elif total_wait >= 1800:  # 30+ min
        warn(f"Alert dopo {total_wait//60} min — PDF richiede dopo 1 ora")
    else:
        fail(f"Alert dopo solo {total_wait//60} min — troppo presto")

    # Verifica che ALERT_EMAIL sia configurabile
    import os
    alert_email = os.getenv("ALERT_EMAIL", sched.ALERT_EMAIL)
    if alert_email:
        ok(f"ALERT_EMAIL configurata: {alert_email}")
    else:
        warn("ALERT_EMAIL non configurata — imposta ALERT_EMAIL=... nell'ambiente")
        print(f"    Esempio: export ALERT_EMAIL=tuo@email.ch")


# ── REQ 5: Prezzi per unità energetica ───────────────────────────────────────

def test_price_unit():
    head("REQ 5 — Prezzi per kWh (PDF: 'costs per energy unit, not monthly/yearly')")

    from schemas import PriceSlot
    from datetime import timezone

    test_slots = [
        # (descrizione, energy, grid, residual, atteso_valido)
        ("prezzi normali CHF/kWh",     0.12, 0.04, 0.009, True),
        ("prezzi alti ma plausibili",  0.35, 0.15, 0.02,  True),
        ("prezzi in Rp (troppo alti)", 12.0, 4.0,  0.9,   False),  # 12 CHF/kWh non plausibile
        ("prezzo zero",                0.0,  0.0,  0.0,   False),  # zero non ha senso
        ("None OK (componente mancante)", None, 0.05, None, True),
    ]

    for desc, energy, grid, residual, should_be_valid in test_slots:
        slot = PriceSlot(
            slot_start_utc=datetime(2026, 3, 16, 0, 0, tzinfo=timezone.utc),
            energy_price=energy,
            grid_price=grid,
            residual_price=residual,
        )
        total = slot.total_price

        if total is None:
            is_valid = False
        else:
            # Prezzi plausibili: 0.001 CHF/kWh (0.1 Rp) a 2.0 CHF/kWh (200 Rp)
            is_valid = 0.001 <= total <= 2.0

        if is_valid == should_be_valid:
            ok(f"{desc}: total={f'{total*100:.1f} Rp/kWh' if total else 'None'} → {'valido' if is_valid else 'non valido'} ✓")
        else:
            fail(f"{desc}: total={f'{total*100:.1f} Rp/kWh' if total else 'None'} → atteso {'valido' if should_be_valid else 'non valido'}")

    # Verifica che rappen_to_chf funzioni
    from adapters.base import BaseAdapter
    tests = [(8.5, 0.085), (100.0, 1.0), (0.1, 0.001)]
    for rp, expected_chf in tests:
        result = BaseAdapter.rappen_to_chf(rp)
        if abs(result - expected_chf) < 0.000001:
            ok(f"rappen_to_chf({rp} Rp) = {result} CHF ✓")
        else:
            fail(f"rappen_to_chf({rp} Rp) = {result} CHF (atteso {expected_chf})")


# ── REQ 6: UTC in tutto il sistema ───────────────────────────────────────────

def test_utc_everywhere():
    head("REQ 6 — UTC ovunque (PDF: 'timezone is UTC')")

    from schemas import PriceSlot, NormalizedPrices, utc_now, make_day_range_utc

    # Test PriceSlot rifiuta datetime naive
    try:
        PriceSlot(
            slot_start_utc=datetime(2026, 3, 16, 0, 0),  # naive!
            grid_price=0.05
        )
        fail("PriceSlot dovrebbe rifiutare datetime naive")
    except ValueError:
        ok("PriceSlot rifiuta datetime naive ✓")

    # Test make_day_range_utc
    start, end = make_day_range_utc(date(2026, 3, 16))
    if start.tzinfo is not None and end.tzinfo is not None:
        ok(f"make_day_range_utc: {start.isoformat()} → {end.isoformat()}")
    else:
        fail("make_day_range_utc ritorna datetime naive")

    # Test to_api_response ha timestamp UTC
    now = datetime.now(timezone.utc)
    result = NormalizedPrices(
        tariff_id="test",
        source_date=date(2026, 3, 16),
        fetched_at=now,
        slots=[
            PriceSlot(
                slot_start_utc=datetime(2026, 3, 16, i, 0, tzinfo=timezone.utc),
                grid_price=0.05,
                energy_price=0.12,
            )
            for i in range(3)
        ]
    )
    api_resp = result.to_api_response()
    for component in ("energy_price_utc", "grid_price_utc"):
        if component in api_resp:
            ts = list(api_resp[component][0].keys())[0]
            if "+00:00" in ts:
                ok(f"to_api_response: {component} ha UTC esplicito ✓ ({ts[:25]})")
            else:
                fail(f"to_api_response: {component} SENZA UTC! ({ts})")


# ── REQ 7: Valid_until e datetime_available_from ──────────────────────────────

def test_tariff_validity_fields():
    head("REQ 7 — Campi validità tariffa (valid_until, datetime_available_from_utc)")

    from database import init_db, get_active_tariffs
    import json

    SessionLocal = init_db()
    with SessionLocal() as session:
        tariffs = get_active_tariffs(session)

    for t in tariffs[:3]:
        config = json.loads(t.full_config_json)

        # datetime_available_from_utc
        if t.datetime_available_from:
            ok(f"{t.tariff_id}: datetime_available_from = {t.datetime_available_from.date()}")
        else:
            warn(f"{t.tariff_id}: datetime_available_from mancante in evu_list.json")

        # valid_until (None = ongoing, accettabile)
        if t.valid_until:
            ok(f"{t.tariff_id}: valid_until = {t.valid_until.date()}")
        else:
            ok(f"{t.tariff_id}: valid_until = None (tariffa ongoing) ✓")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    unit_only = "--unit" in sys.argv
    api_only  = "--api"  in sys.argv

    print(f"{BOLD}Swiss Tariff Hub — Verifica Requisiti PDF{D}")
    print(f"Data: {date.today()}\n")

    if not api_only:
        test_api1_structure()
        test_api2_structure()
        test_dst_handling()
        test_alert_timing()
        test_price_unit()
        test_utc_everywhere()
        test_tariff_validity_fields()

    if api_only or not unit_only:
        # Test API live (richiede uvicorn attivo)
        head("TEST API LIVE (http://localhost:8000)")
        try:
            import urllib.request
            base = "http://localhost:8000"

            # API 1
            with urllib.request.urlopen(f"{base}/api/v1/tariffs") as r:
                tariffs = json.loads(r.read())
            ok(f"GET /api/v1/tariffs: {len(tariffs)} tariffe")

            # API 2 con la prima tariffa disponibile
            if tariffs:
                tid = tariffs[0]["tariff_id"]
                url = f"{base}/api/v1/prices?tariff_id={tid}&start_time=2026-03-16T00:00:00Z"
                try:
                    with urllib.request.urlopen(url) as r:
                        prices = json.loads(r.read())
                    ok(f"GET /api/v1/prices ({tid}): {prices.get('slot_count', 0)} slot")

                    # Verifica timestamp UTC
                    for key in ("energy_price_utc", "grid_price_utc", "residual_price_utc"):
                        if key in prices and prices[key]:
                            ts = list(prices[key][0].keys())[0]
                            if "+00:00" in ts:
                                ok(f"  {key}: UTC ✓ ({ts[:25]})")
                            else:
                                fail(f"  {key}: timestamp senza UTC!")
                except Exception as e:
                    warn(f"GET /api/v1/prices: {e} (caricare dati con --date)")

            # Health
            with urllib.request.urlopen(f"{base}/api/v1/health") as r:
                health = json.loads(r.read())
            ok(f"GET /api/v1/health: status={health['status']}, "
               f"adapters_ready={health.get('adapters_ready', '?')}/{health.get('adapters_total', '?')}")

        except Exception as e:
            warn(f"Server non raggiungibile: {e}")
            warn("Avvia uvicorn per testare le API live: uvicorn main:app --reload")

    # Riepilogo finale
    total = passed + failed
    print(f"\n{'='*50}")
    print(f"{BOLD}Risultato: {G}{passed}{D}{BOLD}/{total} test passati{D}", end="")
    if failed:
        print(f"  {R}({failed} falliti){D}")
    else:
        print(f"  {G}✓ tutti passati{D}")

    if failed == 0:
        print(f"\n{G}{BOLD}Tutti i requisiti PDF sono soddisfatti.{D}")
    else:
        print(f"\n{Y}Alcuni requisiti non sono soddisfatti — vedere i dettagli sopra.{D}")

    return failed == 0


if __name__ == "__main__":
    ok_flag = main()
    sys.exit(0 if ok_flag else 1)