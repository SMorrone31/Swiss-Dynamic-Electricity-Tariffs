#!/usr/bin/env python3
"""
test_primeo.py
==============
Script diagnostico per l'API Primeo Energie / AVAG / ELAG.

Da eseguire LOCALMENTE (non nel container Anthropic — il dominio è bloccato lì).
Testa l'endpoint e scopre quali parametri funzionano davvero.

Uso:
    cd progetto/
    python test_primeo.py                    # test completo
    python test_primeo.py --date 2026-04-03  # data specifica
    python test_primeo.py --netzgebiet AVAG  # prova un netzgebiet
    python test_primeo.py --quick            # solo risposta base

Requisiti:
    pip install httpx
"""

import asyncio
import json
import sys
import argparse
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

try:
    import httpx
except ImportError:
    print("ERRORE: httpx non trovato. Installa con: pip install httpx")
    sys.exit(1)

# ── Colori terminal ───────────────────────────────────────────────────────────
G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"
B = "\033[94m"; D = "\033[0m"; BOLD = "\033[1m"

BASE_URL = "https://tarife.primeo-energie.ch/api/v1/tariffs"

COMPANIES = [
    {"name": "Primeo Energie",         "tariff_id": "primeo_netzdynamisch",    "netzgebiet": None},
    {"name": "Aare Versorgungs (AVAG)", "tariff_id": "avag_primeo_netzdynamisch", "netzgebiet": "AVAG"},
    {"name": "Elektra Gretzenbach (ELAG)", "tariff_id": "elag_primeo_netzdynamisch", "netzgebiet": "ELAG"},
]


async def probe_url(client: httpx.AsyncClient, url: str, label: str) -> dict | None:
    """Chiama un URL e restituisce la risposta parsata."""
    print(f"\n  {B}→ GET{D} {url}")
    try:
        r = await client.get(url, timeout=20)
        print(f"  Status: {G}{r.status_code}{D}" if r.status_code == 200 else f"  Status: {R}{r.status_code}{D}")

        if r.status_code != 200:
            print(f"  Body: {r.text[:300]}")
            return None

        data = r.json()
        return data

    except httpx.TimeoutException:
        print(f"  {R}TIMEOUT dopo 20s{D}")
        return None
    except Exception as e:
        print(f"  {R}ERRORE: {e}{D}")
        return None


def analyze_response(data: dict, label: str) -> dict:
    """Analizza la risposta e stampa statistiche."""
    if not isinstance(data, dict):
        print(f"  {R}Risposta non è un dict: {type(data)}{D}")
        return {}

    print(f"\n  {BOLD}Analisi risposta — {label}{D}")
    print(f"  Chiavi top-level: {list(data.keys())}")

    pub_ts = data.get("publication_timestamp")
    if pub_ts:
        print(f"  publication_timestamp: {pub_ts}")

    tariff_name = data.get("tariff_name")
    if tariff_name:
        print(f"  tariff_name: {tariff_name}")

    prices = data.get("prices", [])
    print(f"  Numero slot: {G}{len(prices)}{D}")

    if not prices:
        print(f"  {R}Lista 'prices' VUOTA!{D}")
        return data

    # Analisi primo slot
    first = prices[0]
    last  = prices[-1]
    print(f"\n  Primo slot: {first.get('start_timestamp')} → {first.get('end_timestamp')}")
    print(f"  Ultimo slot: {last.get('start_timestamp')} → {last.get('end_timestamp')}")

    # Analisi componenti
    components = {}
    for key in ("grid", "grid_usage", "electricity", "integrated", "feed_in"):
        val = first.get(key)
        if val is not None:
            if isinstance(val, list) and val:
                v = val[0]
                components[key] = f"{v.get('value')} {v.get('unit')}"
            else:
                components[key] = str(val)

    print(f"\n  Componenti nel primo slot:")
    for k, v in components.items():
        indicator = G if v != "None" and v != "null" else Y
        print(f"    {indicator}{k:<15}{D} → {v}")

    # Verifica se i prezzi variano (API dinamica vs statica)
    grid_vals = set()
    for item in prices:
        raw = item.get("grid")
        if isinstance(raw, list) and raw:
            grid_vals.add(raw[0].get("value"))

    if len(grid_vals) > 1:
        print(f"\n  {G}✓ Prezzi VARIABILI nel giorno ({len(grid_vals)} valori distinti per 'grid'){D}")
        print(f"  Range: {min(grid_vals):.4f} — {max(grid_vals):.4f} CHF/kWh")
    else:
        print(f"\n  {Y}⚠ Prezzi FISSI nel giorno (tutti gli slot hanno lo stesso valore 'grid'){D}")

    return data


async def test_date_params(client: httpx.AsyncClient, target: date):
    """Testa le diverse forme del parametro data."""
    print(f"\n{'='*60}")
    print(f"{BOLD}TEST 1 — Parametri data per {target}{D}")
    print('='*60)

    variants = [
        ("senza parametri (default)",      BASE_URL),
        ("?date=YYYY-MM-DD",               f"{BASE_URL}?date={target.isoformat()}"),
        ("?start_timestamp / end_timestamp (vecchio formato)",
         f"{BASE_URL}?start_timestamp={target.isoformat()}T00%3A00%3A00%2B02%3A00"
         f"&end_timestamp={target.isoformat()}T23%3A59%3A59%2B02%3A00"),
    ]

    results = {}
    for label, url in variants:
        data = await probe_url(client, url, label)
        if data:
            prices = data.get("prices", [])
            results[label] = len(prices)
            print(f"  → {len(prices)} slot")
        else:
            results[label] = 0

    print(f"\n{BOLD}Riepilogo parametri data:{D}")
    for label, count in results.items():
        icon = G+"✓"+D if count >= 88 else (Y+"⚠"+D if count > 0 else R+"✗"+D)
        print(f"  {icon} {label}: {count} slot")

    return results


async def test_netzgebiet(client: httpx.AsyncClient, target: date):
    """Testa il parametro netzgebiet per distinguere AVAG / ELAG / Primeo."""
    print(f"\n{'='*60}")
    print(f"{BOLD}TEST 2 — Parametro netzgebiet{D}")
    print('='*60)
    print("Verifica se AVAG e ELAG richiedono un netzgebiet diverso da Primeo.\n")

    netzgebiet_values = [
        None,
        "AVAG", "avag",
        "ELAG", "elag",
        "primeo", "PRIMEO",
        "1", "2", "3",  # valori numerici possibili
    ]

    found = {}
    for ng in netzgebiet_values:
        params = {"date": target.isoformat()}
        if ng:
            params["netzgebiet"] = ng
        url = f"{BASE_URL}?{urlencode(params)}"
        label = f"netzgebiet={ng!r}" if ng else "senza netzgebiet"

        data = await probe_url(client, url, label)
        if data:
            prices = data.get("prices", [])
            if prices:
                # Calcola la media del grid price per vedere se è diverso
                grids = [
                    p.get("grid", [{}])[0].get("value", 0)
                    for p in prices
                    if isinstance(p.get("grid"), list) and p.get("grid")
                ]
                avg = sum(grids) / len(grids) if grids else 0
                found[label] = {"slots": len(prices), "avg_grid": round(avg, 5)}
                print(f"  → {len(prices)} slot | avg grid: {avg:.5f} CHF/kWh")
            else:
                found[label] = {"slots": 0, "avg_grid": None}
                print(f"  → risposta vuota")

    print(f"\n{BOLD}Analisi netzgebiet:{D}")
    unique_avgs = set(v["avg_grid"] for v in found.values() if v["avg_grid"] is not None)
    if len(unique_avgs) > 1:
        print(f"{G}  ✓ Il parametro netzgebiet cambia i prezzi — differenziazione geografica attiva!{D}")
        for label, info in found.items():
            print(f"    {label}: {info['slots']} slot, avg={info['avg_grid']}")
    else:
        print(f"{Y}  ⚠ Tutti i netzgebiet restituiscono gli stessi prezzi.{D}")
        print(f"  → Le tre aziende (Primeo/AVAG/ELAG) condividono lo stesso tariffario.")
        print(f"  → Il parametro netzgebiet non è necessario (o non differenzia i prezzi).")

    return found


async def test_historical(client: httpx.AsyncClient):
    """Verifica se l'API supporta date passate (backfill)."""
    print(f"\n{'='*60}")
    print(f"{BOLD}TEST 3 — Date storiche (verifica backfill){D}")
    print('='*60)

    today = date.today()
    test_dates = [
        ("Ieri",          today - timedelta(days=1)),
        ("7 giorni fa",   today - timedelta(days=7)),
        ("30 giorni fa",  today - timedelta(days=30)),
        ("60 giorni fa",  today - timedelta(days=60)),
    ]

    backfill_possible = False
    for label, d in test_dates:
        url = f"{BASE_URL}?date={d.isoformat()}"
        data = await probe_url(client, url, f"{label} ({d})")
        if data:
            prices = data.get("prices", [])
            if len(prices) >= 88:
                print(f"  → {G}{len(prices)} slot — BACKFILL POSSIBILE per {label}!{D}")
                backfill_possible = True
            elif prices:
                print(f"  → {Y}{len(prices)} slot (parziali){D}")
            else:
                print(f"  → {R}0 slot (risposta vuota){D}")
        else:
            print(f"  → {R}Nessuna risposta{D}")

    print(f"\n{BOLD}Risultato backfill:{D}")
    if backfill_possible:
        print(f"{G}  ✓ L'API supporta date storiche! Il backfill è possibile.{D}")
        print(f"  → Rimuovi 'backfill_supported: false' dall'evu_list.json per queste tariffe.")
    else:
        print(f"{R}  ✗ L'API NON supporta date storiche (day-ahead only).{D}")
        print(f"  → Confermato: usa 'backfill_supported: false' nell'evu_list.json.")
        print(f"  → I dati storici sono disponibili SOLO se lo scheduler era già attivo.")

    return backfill_possible


async def test_all_companies(client: httpx.AsyncClient, target: date):
    """Testa tutte e tre le aziende con la loro config attuale."""
    print(f"\n{'='*60}")
    print(f"{BOLD}TEST 4 — Fetch completo per le tre aziende{D}")
    print('='*60)

    for company in COMPANIES:
        params = {"date": target.isoformat()}
        if company["netzgebiet"]:
            params["netzgebiet"] = company["netzgebiet"]
        url = f"{BASE_URL}?{urlencode(params)}"

        print(f"\n  {BOLD}{company['name']}{D} ({company['tariff_id']})")
        data = await probe_url(client, url, company['name'])
        if data:
            analyze_response(data, company['name'])


async def full_test_simulation():
    """
    Simula esattamente quello che fa PrimeoEnergieAdapter.fetch()
    per vedere se produce il risultato corretto.
    """
    print(f"\n{'='*60}")
    print(f"{BOLD}TEST 5 — Simulazione adapter fetch{D}")
    print('='*60)

    # Usa il percorso del progetto per importare l'adapter
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    try:
        from adapters.primeo_energie import PrimeoEnergieAdapter
        from database import init_db, seed_tariffs, get_active_tariffs
        import json as _json

        SessionLocal = init_db()
        with SessionLocal() as s:
            seed_tariffs(s)
            tariffs = get_active_tariffs(s)
            primeo_tariffs = [t for t in tariffs if t.adapter_class == "PrimeoEnergieAdapter"]

        if not primeo_tariffs:
            print(f"{R}  Nessuna tariffa PrimeoEnergieAdapter trovata nel DB.{D}")
            print(f"  → Verifica evu_list.json e riavvia: rm tariffs.db && uvicorn main:app")
            return

        target = date.today()  # fetch di oggi (potrebbe essere domani se già pubblicato)
        print(f"  Target date: {target}")
        print(f"  Tariffe Primeo trovate: {[t.tariff_id for t in primeo_tariffs]}\n")

        for tariff in primeo_tariffs:
            config = _json.loads(tariff.full_config_json)
            adapter = PrimeoEnergieAdapter(config)
            print(f"\n  {BOLD}→ {tariff.tariff_id}{D}")
            try:
                result = await adapter.fetch(target)
                print(f"  {G}✓ OK: {result.slot_count} slot{D}")
                print(f"  Media totale: {result.avg_total_price*100:.2f} Rp/kWh" if result.avg_total_price else "  Media: N/A")
                if result.min_slot:
                    print(f"  Min: {result.min_slot}")
                if result.max_slot:
                    print(f"  Max: {result.max_slot}")
                if result.warnings:
                    for w in result.warnings:
                        print(f"  {Y}⚠ {w}{D}")
            except Exception as e:
                print(f"  {R}✗ ERRORE: {type(e).__name__}: {e}{D}")

    except ImportError as e:
        print(f"{Y}  Impossibile importare adapter (non nel path progetto): {e}{D}")
        print(f"  → Esegui da dentro la cartella 'progetto/'")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Test diagnostico API Primeo")
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="Data target YYYY-MM-DD (default: oggi)")
    parser.add_argument("--netzgebiet", default=None,
                        help="Testa un singolo valore netzgebiet")
    parser.add_argument("--quick", action="store_true",
                        help="Solo test base (più veloce)")
    args = parser.parse_args()

    target = date.fromisoformat(args.date)

    print(f"\n{BOLD}{'='*60}{D}")
    print(f"{BOLD}  Primeo API Diagnostic Tool{D}")
    print(f"{BOLD}  Endpoint: {BASE_URL}{D}")
    print(f"{BOLD}  Data target: {target}{D}")
    print(f"{BOLD}{'='*60}{D}")

    async with httpx.AsyncClient(
        headers={"Accept": "application/json", "User-Agent": "swiss-tariff-hub/1.0"},
        follow_redirects=True,
    ) as client:
        # Test connettività base
        print(f"\n{BOLD}Verifica connettività...{D}")
        basic = await probe_url(client, BASE_URL, "Base (senza parametri)")
        if basic is None:
            print(f"\n{R}ERRORE CRITICO: Impossibile raggiungere l'API.{D}")
            print("  Verifica la connessione internet e che il dominio sia raggiungibile.")
            return

        # Analisi risposta base
        analyze_response(basic, "Senza parametri")

        if args.quick:
            print(f"\n{Y}Modalità --quick: stop dopo il test base.{D}")
            return

        if args.netzgebiet:
            # Test singolo netzgebiet
            url = f"{BASE_URL}?date={target.isoformat()}&netzgebiet={args.netzgebiet}"
            data = await probe_url(client, url, f"netzgebiet={args.netzgebiet}")
            if data:
                analyze_response(data, f"netzgebiet={args.netzgebiet}")
            return

        # Suite completa
        await test_date_params(client, target)
        await test_netzgebiet(client, target)
        backfill_possible = await test_historical(client)
        await test_all_companies(client, target)

    # Test adapter vero (solo se siamo nella dir del progetto)
    await full_test_simulation()

    # Riepilogo finale
    print(f"\n{'='*60}")
    print(f"{BOLD}RIEPILOGO DIAGNOSTICA{D}")
    print('='*60)
    print(f"""
Risultati:
  • Backfill storico: {'✓ POSSIBILE' if backfill_possible else '✗ NON POSSIBILE (day-ahead only)'}
  • Configurazione consigliata per evu_list.json: vedi sezione evu_list

Prossimi passi:
  1. Se il test netzgebiet mostra prezzi IDENTICI tra Primeo/AVAG/ELAG:
     → Le tre tariffe restituiscono gli stessi dati (stesso Netzgebiet)
     → Mantieni ugualmente le tre entry per trasparenza verso i clienti

  2. Se il test netzgebiet mostra prezzi DIVERSI:
     → Aggiungi il campo "netzgebiet" corretto nell'api_params di evu_list.json

  3. Se backfill_possible è False:
     → Assicurati che "backfill_supported": false sia in api_params per tutte e 3 le tariffe

Esegui: python test_primeo.py 2>&1 | tee primeo_diagnostic.txt
per salvare il report completo.
""")

    print(f"{G}Diagnostica completata.{D}\n")


if __name__ == "__main__":
    asyncio.run(main())