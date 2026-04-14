"""
database.py
===========
Schema del database e funzioni di accesso.

Tabelle:
  tariffs     → una riga per ogni tariffa (da evu_list.json)
  price_slots → i 96 slot da 15 min per ogni tariffa ogni giorno

Uso:
  from database import init_db, get_session, upsert_prices, seed_tariffs
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Index, Integer,
    String, Text, create_engine, text
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from schemas import NormalizedPrices


# ── Engine ────────────────────────────────────────────────────────────────────

def get_engine(database_url: Optional[str] = None):
    """
    Crea l'engine SQLAlchemy.
    Se database_url è None, usa la variabile d'ambiente DATABASE_URL.
    Default locale per sviluppo: SQLite (zero config).
    """
    import os
    url = database_url or os.getenv(
        "DATABASE_URL",
        "sqlite:///./tariffs.db"  # SQLite per sviluppo locale
    )
    # PostgreSQL: "postgresql://user:pass@localhost:5432/tariffs"
    # SQLite:     "sqlite:///./tariffs.db"
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, echo=False)


# ── Models ────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class Tariff(Base):
    """
    Una riga per ogni tariffa in evu_list.json.
    Questa tabella è la fonte di verità per lo scheduler.
    """
    __tablename__ = "tariffs"

    tariff_id                 = Column(String(100), primary_key=True)
    tariff_name               = Column(String(200), nullable=False)
    provider_name             = Column(String(200), nullable=False)
    adapter_class             = Column(String(100), nullable=False)
    api_base_url              = Column(Text, nullable=False, default="")
    auth_type                 = Column(String(50), default="none")
    auth_config_json          = Column(Text, default="{}")
    response_format           = Column(String(20), default="json")
    time_resolution_minutes   = Column(Integer, default=15)
    daily_update_time_utc     = Column(String(5), default="17:00")  # "HH:MM"
    datetime_available_from   = Column(DateTime(timezone=True), nullable=True)
    valid_until               = Column(DateTime(timezone=True), nullable=True)
    sgr_compliant             = Column(Boolean, default=False)
    for_whom                  = Column(Text, default="")
    dynamic_elements_json     = Column(Text, default="[]")
    doc_url                   = Column(Text, default="")
    notes                     = Column(Text, default="")
    active                    = Column(Boolean, default=True)
    last_updated              = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )

    # Config completa dell'adapter (tutto evu_list.json entry serializzato)
    full_config_json          = Column(Text, default="{}")

    def config_dict(self) -> dict:
        """Ritorna la config completa come dict (per passarla all'adapter)."""
        return json.loads(self.full_config_json or "{}")

    def __repr__(self) -> str:
        return f"Tariff({self.tariff_id} | {self.provider_name})"


class PriceSlotDB(Base):
    """
    Un slot da 15 minuti per una tariffa.
    Chiave primaria composta: (tariff_id, slot_start_utc).
    """
    __tablename__ = "price_slots"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    tariff_id       = Column(String(100), nullable=False, index=True)
    slot_start_utc  = Column(DateTime(timezone=True), nullable=False)
    energy_price    = Column(Float, nullable=True)   # CHF/kWh
    grid_price      = Column(Float, nullable=True)   # CHF/kWh
    residual_price  = Column(Float, nullable=True)   # CHF/kWh
    fetched_at_utc  = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_price_slots_tariff_start",
              "tariff_id", "slot_start_utc", unique=True),
        Index("ix_price_slots_start",
              "slot_start_utc"),
    )

    def __repr__(self) -> str:
        total = sum(
            p for p in (self.energy_price, self.grid_price, self.residual_price)
            if p is not None
        )
        return (
            f"PriceSlot({self.tariff_id} | "
            f"{self.slot_start_utc.strftime('%Y-%m-%d %H:%M UTC')} | "
            f"{total*100:.2f} Rp/kWh)"
        )


# ── Setup ─────────────────────────────────────────────────────────────────────

def init_db(database_url: Optional[str] = None) -> sessionmaker:
    """
    Crea le tabelle (se non esistono) e ritorna una session factory.
    Chiamare una volta all'avvio dell'applicazione.
    """
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


# ── Seed tariffe ──────────────────────────────────────────────────────────────

def seed_tariffs(
    session: Session,
    evu_list_path: Optional[Path] = None,
) -> int:
    """
    Carica/aggiorna le tariffe dalla evu_list.json nel DB.
    Usa upsert: aggiorna se esiste, inserisce se nuovo.
    Ritorna il numero di tariffe processate.
    """
    path = evu_list_path or Path(__file__).parent / "config" / "evu_list.json"
    with open(path, encoding="utf-8") as f:
        evu_list = json.load(f)

    count = 0
    for entry in evu_list:
        tariff_id = entry.get("tariff_id")
        if not tariff_id:
            continue

        # Cerca entry esistente
        existing = session.get(Tariff, tariff_id)

        # Parsa datetime
        def parse_dt(s):
            if not s:
                return None
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except Exception:
                return None

        row = existing or Tariff(tariff_id=tariff_id)
        row.tariff_name             = entry.get("tariff_name", "")
        row.provider_name           = entry.get("provider_name", "")
        row.adapter_class           = entry.get("adapter_class", "")
        row.api_base_url            = entry.get("api_base_url", "")
        row.auth_type               = entry.get("auth_type", "none")
        row.auth_config_json        = json.dumps(entry.get("auth_config", {}))
        row.response_format         = entry.get("response_format", "json")
        row.time_resolution_minutes = entry.get("time_resolution_minutes", 15)
        row.daily_update_time_utc   = entry.get("daily_update_time_utc", "17:00")
        row.datetime_available_from = parse_dt(entry.get("datetime_available_from_utc"))
        row.valid_until             = parse_dt(entry.get("valid_until_utc"))
        row.sgr_compliant           = entry.get("sgr_compliant", False)
        row.for_whom                = entry.get("for_whom", "")
        row.dynamic_elements_json   = json.dumps(entry.get("dynamic_elements", []))
        row.doc_url                 = entry.get("doc_url", "")
        row.notes                   = entry.get("notes", "")
        row.active                  = bool(entry.get("api_base_url"))  # attivo solo se ha URL
        row.full_config_json        = json.dumps(entry)

        if not existing:
            session.add(row)
        count += 1

    session.commit()
    return count


# ── Upsert prezzi ─────────────────────────────────────────────────────────────

def upsert_prices(session: Session, result: NormalizedPrices) -> int:
    """
    Salva i prezzi nel DB con upsert reale.
    Usa INSERT OR REPLACE per SQLite, ON CONFLICT DO UPDATE per PostgreSQL.
    """
    rows = result.to_db_rows()
    if not rows:
        return 0

    from sqlalchemy import inspect
    dialect = session.bind.dialect.name  # "sqlite" o "postgresql"

    for row in rows:
        # Normalizza il timestamp: rimuovi timezone per SQLite
        # (SQLite non supporta TIMESTAMPTZ, salva come stringa naive)
        slot_dt = row["slot_start_utc"]
        if hasattr(slot_dt, "tzinfo") and slot_dt.tzinfo is not None:
            slot_dt_naive = slot_dt.replace(tzinfo=None)
        else:
            slot_dt_naive = slot_dt

        fetched_dt = row["fetched_at_utc"]
        if hasattr(fetched_dt, "tzinfo") and fetched_dt.tzinfo is not None:
            fetched_dt_naive = fetched_dt.replace(tzinfo=None)
        else:
            fetched_dt_naive = fetched_dt

        if dialect == "sqlite":
            # Usa strftime per forzare il formato spazio "YYYY-MM-DD HH:MM:SS".
            # SQLAlchemy in text() converte datetime con isoformat() → formato T,
            # che non matcha con i valori già salvati in formato spazio → INSERT
            # duplicato invece di ON CONFLICT UPDATE → dati sovrapposti.
            slot_str    = slot_dt_naive.strftime("%Y-%m-%d %H:%M:%S")
            fetched_str = fetched_dt_naive.strftime("%Y-%m-%d %H:%M:%S")
            session.execute(
                text("""
                    INSERT INTO price_slots
                        (tariff_id, slot_start_utc, energy_price, grid_price,
                         residual_price, fetched_at_utc)
                    VALUES
                        (:tariff_id, :slot_start, :energy, :grid, :residual, :fetched)
                    ON CONFLICT(tariff_id, slot_start_utc) DO UPDATE SET
                        energy_price   = excluded.energy_price,
                        grid_price     = excluded.grid_price,
                        residual_price = excluded.residual_price,
                        fetched_at_utc = excluded.fetched_at_utc
                """),
                {
                    "tariff_id": row["tariff_id"],
                    "slot_start": slot_str,
                    "energy":     row["energy_price"],
                    "grid":       row["grid_price"],
                    "residual":   row["residual_price"],
                    "fetched":    fetched_str,
                }
            )
        else:
            # PostgreSQL
            session.execute(
                text("""
                    INSERT INTO price_slots
                        (tariff_id, slot_start_utc, energy_price, grid_price,
                         residual_price, fetched_at_utc)
                    VALUES
                        (:tariff_id, :slot_start, :energy, :grid, :residual, :fetched)
                    ON CONFLICT (tariff_id, slot_start_utc) DO UPDATE SET
                        energy_price   = EXCLUDED.energy_price,
                        grid_price     = EXCLUDED.grid_price,
                        residual_price = EXCLUDED.residual_price,
                        fetched_at_utc = EXCLUDED.fetched_at_utc
                """),
                {
                    "tariff_id": row["tariff_id"],
                    "slot_start": slot_dt,
                    "energy":     row["energy_price"],
                    "grid":       row["grid_price"],
                    "residual":   row["residual_price"],
                    "fetched":    fetched_dt,
                }
            )

    session.commit()
    return len(rows)


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_active_tariffs(session: Session) -> list[Tariff]:
    """Tutte le tariffe attive (con API URL configurato)."""
    return session.query(Tariff).filter(Tariff.active == True).all()


def get_prices(
    session: Session,
    tariff_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[PriceSlotDB]:
    """
    Slot di prezzo per una tariffa in un range temporale.

    SQLite salva i timestamp come stringhe naive (senza timezone), quindi
    confrontare con datetime aware produce risultati sbagliati: SQLite ordina
    le stringhe lessicograficamente e '2026-03-16 23:00:00+00:00' > '2026-03-16 23:00:00',
    facendo escludere il primo slot della giornata.
    Fix: strip tzinfo prima di passare i limiti alla query.
    I timezone vengono ripristinati sui risultati dopo la query.
    """
    # SQLite salva i datetime come stringhe con lo spazio: "2026-03-16 23:00:00".
    # SQLAlchemy serializza i parametri datetime con isoformat() → "2026-03-16T23:00:00".
    # Confronto lessicografico: spazio (ASCII 32) < T (ASCII 84) → slot delle 23:00
    # risulta minore di start_str → escluso dal filtro >= → 95 slot invece di 96.
    # Fix: strftime con spazio, identico al formato salvato.
    start_str = start_utc.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    end_str   = end_utc.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")

    rows = (
        session.query(PriceSlotDB)
        .filter(
            PriceSlotDB.tariff_id == tariff_id,
            PriceSlotDB.slot_start_utc >= start_str,
            PriceSlotDB.slot_start_utc < end_str,
        )
        .order_by(PriceSlotDB.slot_start_utc)
        .all()
    )
    # SQLite perde il timezone — lo ripristiniamo sui risultati
    for row in rows:
        if row.slot_start_utc is not None and row.slot_start_utc.tzinfo is None:
            row.slot_start_utc = row.slot_start_utc.replace(tzinfo=timezone.utc)
        if row.fetched_at_utc is not None and row.fetched_at_utc.tzinfo is None:
            row.fetched_at_utc = row.fetched_at_utc.replace(tzinfo=timezone.utc)
    return rows


def get_last_fetch(session: Session, tariff_id: str) -> Optional[datetime]:
    """Timestamp dell'ultimo fetch per una tariffa."""
    row = (
        session.query(PriceSlotDB.fetched_at_utc)
        .filter(PriceSlotDB.tariff_id == tariff_id)
        .order_by(PriceSlotDB.fetched_at_utc.desc())
        .first()
    )
    if not row:
        return None
    dt = row[0]
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def has_data_for_date(session: Session, tariff_id: str, target_date, tz_ch) -> bool:
    """
    Controlla se esistono slot per una specifica data locale svizzera.

    A differenza di get_last_fetch (che dice QUANDO hai fetchato),
    questa funzione dice PER QUALE DATA hai i prezzi nel DB.

    Usata da /api/v1/health per distinguere:
      - "ho dati di oggi"   → has_data_for_date(session, id, today_ch, tz_ch)
      - "ho dati di domani" → has_data_for_date(session, id, tomorrow_ch, tz_ch)
    """
    from datetime import datetime, timedelta, timezone as _tz

    midnight = datetime(target_date.year, target_date.month, target_date.day,
                        tzinfo=tz_ch)
    start_utc = midnight.astimezone(_tz.utc)
    end_utc   = (midnight + timedelta(days=1)).astimezone(_tz.utc)

    # Stesso trick strftime per SQLite (evita mismatch formato spazio vs T)
    start_str = start_utc.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    end_str   = end_utc.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")

    count = (
        session.query(PriceSlotDB.id)
        .filter(
            PriceSlotDB.tariff_id == tariff_id,
            PriceSlotDB.slot_start_utc >= start_str,
            PriceSlotDB.slot_start_utc < end_str,
        )
        .limit(1)
        .count()
    )
    return count > 0