"""
database.py
===========
Schema del database e funzioni di accesso.

Tabelle:
  tariffs     → una riga per ogni tariffa (da evu_list.json)
  price_slots → i 96 slot da 15 min per ogni tariffa ogni giorno

Fix applicati:
  1. PostgreSQL production-ready — engine con pool configurato,
     niente workaround strftime, niente timezone strip.
  2. Indice su (tariff_id, fetched_at_utc DESC) per get_last_fetch().
  3. seed_tariffs() disattiva (active=False) le tariffe rimosse da evu_list.json.
  4. has_data_for_date() funziona correttamente sia su SQLite che PostgreSQL.

Uso:
  from database import init_db, upsert_prices, seed_tariffs, has_data_for_date
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Index, Integer,
    String, Text, create_engine, text
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from schemas import NormalizedPrices


# ── Timezone helper ───────────────────────────────────────────────────────────

def _tz_ch():
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz
        return pytz.timezone("Europe/Zurich")


# ── Engine ────────────────────────────────────────────────────────────────────

def get_engine(database_url: Optional[str] = None):
    """
    Crea l'engine SQLAlchemy.

    PostgreSQL (produzione):
      DATABASE_URL=postgresql://user:pass@host:5432/tariffs
      - pool_size=5: 5 connessioni persistenti (adeguato per FastAPI + scheduler)
      - max_overflow=10: fino a 10 connessioni extra in picco
      - pool_pre_ping=True: verifica che la connessione sia viva prima di usarla
        (essenziale dopo restart del DB o disconnessioni temporanee)
      - pool_recycle=1800: ricicla le connessioni ogni 30 min per evitare
        "SSL connection has been closed unexpectedly" su connessioni idle

    SQLite (sviluppo locale):
      DATABASE_URL=sqlite:///./tariffs.db  (o non impostare nulla)
      - check_same_thread=False: necessario perché FastAPI usa thread multipli
      - connect_args timeout: riduce i tempi di attesa su lock
    """
    import os
    url = database_url or os.getenv(
        "DATABASE_URL",
        "sqlite:///./tariffs.db"
    )

    if url.startswith("sqlite"):
        engine = create_engine(
            url,
            connect_args={
                "check_same_thread": False,
                "timeout": 15,           # secondi di attesa sul lock SQLite
            },
            echo=False,
        )
    else:
        # PostgreSQL — gestione pool robusta per produzione
        # Render free tier: usa DATABASE_URL dalla variabile d'ambiente
        engine = create_engine(
            url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,    # evita "connection closed unexpectedly"
            pool_recycle=1800,     # 30 min — previene SSL timeout su idle
            echo=False,
        )

    return engine


# ── Dialetto corrente ─────────────────────────────────────────────────────────

def _is_sqlite(session: Session) -> bool:
    """True se il DB sottostante è SQLite."""
    return session.bind.dialect.name == "sqlite"


# ── Models ────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class Tariff(Base):
    """
    Una riga per ogni tariffa in evu_list.json.
    Questa tabella è la fonte di verità per lo scheduler.

    Il campo evu_list_hash permette di rilevare se evu_list.json è stato
    modificato senza rieseguire seed_tariffs() — usato in init_db() per
    loggare un avviso.
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
        status = "active" if self.active else "inactive"
        return f"Tariff({self.tariff_id} | {self.provider_name} | {status})"


class PriceSlotDB(Base):
    """
    Un slot da 15 minuti per una tariffa.
    Unique constraint su (tariff_id, slot_start_utc) — gestito tramite
    ON CONFLICT DO UPDATE nell'upsert.
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
        # Indice primario: unicità + lookup per data/tariffa
        Index("ix_price_slots_tariff_start",
              "tariff_id", "slot_start_utc", unique=True),

        # Indice per range query su slot_start_utc senza tariff_id
        Index("ix_price_slots_start",
              "slot_start_utc"),

        # FIX: indice per get_last_fetch() — ORDER BY fetched_at_utc DESC
        # Senza questo indice la query fa full scan su milioni di righe.
        # DESC è importante: PostgreSQL usa l'indice direttamente per il MAX.
        Index("ix_price_slots_tariff_fetched",
              "tariff_id", "fetched_at_utc"),
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

# Engine singleton — creato una volta sola al primo init_db()
_engine = None
_SessionLocal = None


def init_db(database_url: Optional[str] = None) -> sessionmaker:
    """
    Crea le tabelle (se non esistono) e ritorna una session factory.

    È un singleton: chiamarlo più volte ritorna sempre la stessa factory
    senza ricreare il connection pool. Questo è importante perché
    SQLAlchemy gestisce il pool internamente — ricrearlo ad ogni chiamata
    spreca connessioni e può causare "too many clients" su PostgreSQL.

    Chiamare una volta all'avvio dell'applicazione e riusare la factory.
    """
    global _engine, _SessionLocal

    if _engine is None:
        _engine = get_engine(database_url)
        Base.metadata.create_all(_engine)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)

        import logging
        log = logging.getLogger("database")
        dialect = _engine.dialect.name
        log.info(f"DB inizializzato — dialect: {dialect}")

        if dialect == "sqlite":
            log.warning(
                "Usando SQLite — adeguato per sviluppo locale. "
                "In produzione imposta DATABASE_URL=postgresql://..."
            )

    return _SessionLocal


# ── Hash evu_list.json ────────────────────────────────────────────────────────

def _evu_list_hash(path: Path) -> str:
    """SHA256 dei primi 4KB di evu_list.json — usato per rilevare modifiche."""
    try:
        content = path.read_bytes()[:4096]
        return hashlib.sha256(content).hexdigest()[:16]
    except Exception:
        return ""


# ── Seed tariffe ──────────────────────────────────────────────────────────────

def seed_tariffs(
    session: Session,
    evu_list_path: Optional[Path] = None,
) -> int:
    """
    Carica/aggiorna le tariffe dalla evu_list.json nel DB.

    Comportamento:
      - Upsert: aggiorna se esiste, inserisce se nuova.
      - FIX: Disattiva (active=False) le tariffe nel DB che NON compaiono
        più in evu_list.json. Senza questo fix, una tariffa rimossa dal file
        continua ad essere fetchata dallo scheduler per sempre.

    Ritorna il numero di tariffe processate (attive + disattivate).
    """
    import logging
    log = logging.getLogger("database.seed")

    path = evu_list_path or Path(__file__).parent / "config" / "evu_list.json"
    with open(path, encoding="utf-8") as f:
        evu_list = json.load(f)

    # IDs presenti nel file corrente
    file_ids: set[str] = set()

    def parse_dt(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    count = 0
    for entry in evu_list:
        tariff_id = entry.get("tariff_id")
        if not tariff_id:
            continue

        file_ids.add(tariff_id)
        existing = session.get(Tariff, tariff_id)

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
        # Attiva solo se ha un URL API configurato
        row.active                  = bool(entry.get("api_base_url"))
        row.full_config_json        = json.dumps(entry)

        if not existing:
            session.add(row)
            log.info(f"seed: inserita nuova tariffa '{tariff_id}'")
        count += 1

    # FIX: disattiva le tariffe presenti nel DB ma rimosse da evu_list.json
    all_db_tariffs = session.query(Tariff).all()
    deactivated = 0
    for db_row in all_db_tariffs:
        if db_row.tariff_id not in file_ids:
            if db_row.active:
                db_row.active = False
                deactivated += 1
                log.warning(
                    f"seed: tariffa '{db_row.tariff_id}' rimossa da evu_list.json "
                    f"→ disattivata nel DB (i dati storici vengono mantenuti)"
                )

    session.commit()

    if deactivated:
        log.warning(f"seed: {deactivated} tariffa/e disattivata/e — non più in evu_list.json")

    return count


# ── Upsert prezzi ─────────────────────────────────────────────────────────────

def upsert_prices(session: Session, result: NormalizedPrices) -> int:
    """
    Salva i prezzi nel DB con upsert reale (insert-or-update).

    Comportamento per dialetto:
      SQLite:     ON CONFLICT(tariff_id, slot_start_utc) DO UPDATE SET ...
                  I timestamp vengono serializzati come stringhe con spazio
                  ("YYYY-MM-DD HH:MM:SS") per matchare il formato che SQLite
                  salva internamente — necessario per riconoscere il conflict.

      PostgreSQL: ON CONFLICT (tariff_id, slot_start_utc) DO UPDATE SET ...
                  I timestamp sono passati come oggetti datetime aware — nessun
                  workaround necessario, PostgreSQL gestisce TIMESTAMPTZ nativamente.
    """
    rows = result.to_db_rows()
    if not rows:
        return 0

    is_sqlite = _is_sqlite(session)

    for row in rows:
        slot_dt    = row["slot_start_utc"]
        fetched_dt = row["fetched_at_utc"]

        if is_sqlite:
            # SQLite salva i datetime come stringhe naive con spazio.
            # Se passiamo un datetime aware, SQLAlchemy usa isoformat() con T e +00:00
            # che non matcha la stringa già salvata → ON CONFLICT non scatta → duplicati.
            # Fix: serializzare esplicitamente con strftime spazio.
            if hasattr(slot_dt, "tzinfo") and slot_dt.tzinfo is not None:
                slot_dt = slot_dt.replace(tzinfo=None)
            if hasattr(fetched_dt, "tzinfo") and fetched_dt.tzinfo is not None:
                fetched_dt = fetched_dt.replace(tzinfo=None)

            slot_str    = slot_dt.strftime("%Y-%m-%d %H:%M:%S")
            fetched_str = fetched_dt.strftime("%Y-%m-%d %H:%M:%S")

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
                    "tariff_id":  row["tariff_id"],
                    "slot_start": slot_str,
                    "energy":     row["energy_price"],
                    "grid":       row["grid_price"],
                    "residual":   row["residual_price"],
                    "fetched":    fetched_str,
                }
            )

        else:
            # PostgreSQL — datetime aware passati direttamente, zero workaround.
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
                    "tariff_id":  row["tariff_id"],
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
    """Tutte le tariffe attive (con API URL configurato e non rimosse)."""
    return session.query(Tariff).filter(Tariff.active == True).all()


def get_prices(
    session: Session,
    tariff_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> list[PriceSlotDB]:
    """
    Slot di prezzo per una tariffa in un range temporale.

    Compatibilità SQLite/PostgreSQL:
      SQLite salva i timestamp come stringhe naive ("YYYY-MM-DD HH:MM:SS").
      Se confrontiamo con datetime aware, SQLAlchemy serializza con isoformat T+tz
      che ordina lessicograficamente diverso dalla stringa spazio → slot esclusi.
      Fix: strip tzinfo e usa strftime spazio per SQLite; per PostgreSQL
      il confronto con datetime aware funziona nativamente.
    """
    if _is_sqlite(session):
        start_str = start_utc.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        end_str   = end_utc.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        rows = (
            session.query(PriceSlotDB)
            .filter(
                PriceSlotDB.tariff_id == tariff_id,
                PriceSlotDB.slot_start_utc >= start_str,
                PriceSlotDB.slot_start_utc <  end_str,
            )
            .order_by(PriceSlotDB.slot_start_utc)
            .all()
        )
        # SQLite perde il timezone — ripristiniamo UTC sui risultati
        for row in rows:
            if row.slot_start_utc is not None and row.slot_start_utc.tzinfo is None:
                row.slot_start_utc = row.slot_start_utc.replace(tzinfo=timezone.utc)
            if row.fetched_at_utc is not None and row.fetched_at_utc.tzinfo is None:
                row.fetched_at_utc = row.fetched_at_utc.replace(tzinfo=timezone.utc)
    else:
        # PostgreSQL — datetime aware, nessun workaround
        rows = (
            session.query(PriceSlotDB)
            .filter(
                PriceSlotDB.tariff_id == tariff_id,
                PriceSlotDB.slot_start_utc >= start_utc,
                PriceSlotDB.slot_start_utc <  end_utc,
            )
            .order_by(PriceSlotDB.slot_start_utc)
            .all()
        )

    return rows


def get_last_fetch(session: Session, tariff_id: str) -> Optional[datetime]:
    """
    Timestamp dell'ultimo fetch per una tariffa (quando è stato fetchato,
    non per quale data). Usato solo per il display nel /api/v1/health.

    Nota: usa l'indice ix_price_slots_tariff_fetched per evitare full scan.
    """
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


def has_data_for_date(
    session: Session,
    tariff_id: str,
    target_date: date,
    tz_ch=None,
) -> bool:
    """
    True se esistono slot nel DB per la data locale svizzera target_date.

    Questa è la funzione semanticamente corretta da usare per sapere se
    "abbiamo i dati di oggi/domani". A differenza di get_last_fetch()
    che dice QUANDO hai fetchato, questa dice PER QUALE DATA hai i prezzi.

    Funziona correttamente sia con SQLite che con PostgreSQL:
      - Converte target_date in range UTC (mezzanotte locale CH → UTC)
      - SQLite: usa strftime spazio per il confronto
      - PostgreSQL: usa datetime aware

    tz_ch: opzionale, passa il timezone se già istanziato per evitare
    di ricrearlo ad ogni chiamata in loop.
    """
    if tz_ch is None:
        tz_ch = _tz_ch()

    midnight     = datetime(target_date.year, target_date.month, target_date.day, tzinfo=tz_ch)
    start_utc    = midnight.astimezone(timezone.utc)
    end_utc      = (midnight + timedelta(days=1)).astimezone(timezone.utc)

    if _is_sqlite(session):
        start_str = start_utc.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        end_str   = end_utc.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        count = (
            session.query(PriceSlotDB.id)
            .filter(
                PriceSlotDB.tariff_id == tariff_id,
                PriceSlotDB.slot_start_utc >= start_str,
                PriceSlotDB.slot_start_utc <  end_str,
            )
            .limit(1)
            .count()
        )
    else:
        count = (
            session.query(PriceSlotDB.id)
            .filter(
                PriceSlotDB.tariff_id == tariff_id,
                PriceSlotDB.slot_start_utc >= start_utc,
                PriceSlotDB.slot_start_utc <  end_utc,
            )
            .limit(1)
            .count()
        )

    return count > 0