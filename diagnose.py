#!/usr/bin/env python3
"""
diagnose.py — esegui nella stessa cartella di tariffs.db
python diagnose.py
oppure con una data specifica:
python diagnose.py --date 2026-03-17
"""
import sqlite3, sys, argparse
from datetime import datetime, date, timezone, timedelta

try:
    import zoneinfo; tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
except ImportError:
    import pytz; tz_ch = pytz.timezone("Europe/Zurich")

parser = argparse.ArgumentParser()
parser.add_argument("--date", default=date.today().isoformat(), help="Data locale CH (YYYY-MM-DD)")
parser.add_argument("--db", default="tariffs.db")
parser.add_argument("--tariff", default="ckw_home_dynamic")
args = parser.parse_args()

target = date.fromisoformat(args.date)
print(f"\n{'='*60}")
print(f"  Diagnosi slot — {target} — {args.tariff}")
print(f"{'='*60}\n")

con = sqlite3.connect(args.db)
cur = con.cursor()

# 1. Range naive corretto (come deve fare database.py con il fix)
midnight_local = datetime(target.year, target.month, target.day, tzinfo=tz_ch)
start_naive = midnight_local.astimezone(timezone.utc).replace(tzinfo=None)
end_naive   = (midnight_local + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)

print(f"Range query naive: {start_naive}  →  {end_naive}")
print()

r = cur.execute("""
    SELECT COUNT(*), MIN(slot_start_utc), MAX(slot_start_utc)
    FROM price_slots
    WHERE tariff_id=?
    AND slot_start_utc >= ? AND slot_start_utc < ?
""", (args.tariff, str(start_naive), str(end_naive))).fetchone()

slot_count = r[0]
print(f"Slot nel DB:   {slot_count}  (attesi 96 normali, 92 cambio ora primavera, 100 autunno)")
print(f"Primo slot:    {r[1]}")
print(f"Ultimo slot:   {r[2]}")

# 2. Confronto con range UTC puro (buggy, senza fix)
r2 = cur.execute("""
    SELECT COUNT(*), MIN(slot_start_utc), MAX(slot_start_utc)
    FROM price_slots
    WHERE tariff_id=?
    AND slot_start_utc >= ? AND slot_start_utc < ?
""", (args.tariff, f"{target}T00:00:00", f"{target + timedelta(days=1)}T00:00:00")).fetchone()

print(f"\nRange UTC puro (vecchio): {r2[0]} slot  [{r2[1]} → {r2[2]}]")

# 3. Diagnostica
print(f"\n{'='*60}")
if slot_count == r2[0]:
    print("⚠  I due range danno lo stesso risultato.")
    print("   Possibili cause:")
    print("   a) database.py NON ha il fix (start_q naive) → riavvia uvicorn")
    print("   b) I dati non sono ancora stati refetchati con il fix attivo")
    print(f"   c) Lo slot delle {start_naive} non è presente nel DB")
    
    # Controlla se lo slot mancante esiste
    missing = cur.execute("""
        SELECT slot_start_utc FROM price_slots
        WHERE tariff_id=? AND slot_start_utc = ?
    """, (args.tariff, str(start_naive))).fetchone()
    
    if missing:
        print(f"\n✓ Lo slot {start_naive} ESISTE nel DB ma non viene restituito dall'API")
        print("  → Il problema è nel codice Python (database.py o main.py non aggiornati)")
    else:
        print(f"\n✗ Lo slot {start_naive} NON ESISTE nel DB")
        print("  → I dati devono essere re-fetchati: python scheduler.py --now")
else:
    diff = slot_count - r2[0]
    print(f"✓ Fix funziona: range corretto recupera {diff} slot in più")

# 4. Verifica versione database.py in uso
print(f"\n{'='*60}")
print("  Verifica file in uso:")
import os
for fname in ["database.py", "main.py"]:
    if os.path.exists(fname):
        with open(fname) as f:
            content = f.read()
        has_fix_db   = "start_q = start_utc.replace(tzinfo=None)" in content or "start_q" in content
        has_fix_main = "start_ld" in content or "start_local_date" in content
        if fname == "database.py":
            print(f"  database.py: fix SQLite naive {'✓ PRESENTE' if has_fix_db else '✗ MANCANTE'}")
        else:
            print(f"  main.py:     fix DST          {'✓ PRESENTE' if has_fix_main else '✗ MANCANTE'}")
    else:
        print(f"  {fname}: non trovato nella directory corrente")

con.close()
print()