"""
migrate_add_plan_columns.py
===========================
Aggiunge le colonne 'plan' e 'free_tariff_id' alla tabella api_keys
se non esistono già. Sicuro da rieseguire più volte (idempotente).

Uso:
    python migrate_add_plan_columns.py
    # oppure con DB custom:
    python migrate_add_plan_columns.py --db /path/to/tariffs.db
"""
import sqlite3
import sys
import os

def migrate(db_path: str = "./tariffs.db"):
    if not os.path.exists(db_path):
        print(f"[ERROR] DB non trovato: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    cur  = conn.cursor()

    # Leggi colonne esistenti
    cur.execute("PRAGMA table_info(api_keys)")
    existing = {row[1] for row in cur.fetchall()}
    print(f"[info] Colonne esistenti in api_keys: {sorted(existing)}")

    added = []

    if "plan" not in existing:
        cur.execute("ALTER TABLE api_keys ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
        added.append("plan")
        print("[OK] Aggiunta colonna: plan (default='free')")
    else:
        print("[skip] Colonna 'plan' già presente")

    if "free_tariff_id" not in existing:
        cur.execute("ALTER TABLE api_keys ADD COLUMN free_tariff_id TEXT")
        added.append("free_tariff_id")
        print("[OK] Aggiunta colonna: free_tariff_id (nullable)")
    else:
        print("[skip] Colonna 'free_tariff_id' già presente")

    conn.commit()
    conn.close()

    if added:
        print(f"\n✅ Migration completata — colonne aggiunte: {added}")
    else:
        print("\n✅ Nessuna modifica necessaria — DB già aggiornato")

if __name__ == "__main__":
    db = "./tariffs.db"
    if "--db" in sys.argv:
        idx = sys.argv.index("--db")
        db  = sys.argv[idx + 1]
    migrate(db)