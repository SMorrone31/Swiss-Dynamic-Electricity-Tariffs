"""
main.py
=======
FastAPI application — exposes the APIs defined in the PDF + monitoring endpoints.

Endpoints:
  GET /api/v1/tariffs
      → list of tariffs (API 1 from the PDF)

  GET /api/v1/prices?tariff_id=...&start_time=...
      → time series prices (API 2 from the PDF)

  GET /api/v1/health
      → fetch and adapter status per EVU

  GET /
      → HTML dashboard

Startup:
  pip install fastapi uvicorn sqlalchemy apscheduler
  uvicorn main:app --reload

Production:
  uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from database import (
    init_db, seed_tariffs, get_active_tariffs,
    get_prices, get_last_fetch, PriceSlotDB
)
from schemas import make_day_range_utc


# ── Lifespan (avvio/spegnimento) ──────────────────────────────────────────────

SessionLocal = None

"""
SOSTITUZIONE DEL BLOCCO lifespan IN main.py
============================================
Sostituisci TUTTO il blocco @asynccontextmanager async def lifespan(...)
con questo codice. Il resto di main.py rimane identico.
"""

@asynccontextmanager
async def lifespan(app: FastAPI):
    global SessionLocal
    SessionLocal = init_db()
    with SessionLocal() as session:
        from database import check_evu_list_staleness
        if check_evu_list_staleness(session):
            n = seed_tariffs(session)
            print(f"[startup] DB aggiornato — {n} tariffe caricate da evu_list.json")
        else:
            from database import get_active_tariffs as _gat
            n = len(_gat(session))
            print(f"[startup] DB invariato — {n} tariffe attive (evu_list.json non cambiato)")

    import asyncio as _asyncio
    import json as _json
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    _scheduler = AsyncIOScheduler(timezone="UTC")

    # ── JOB 1: Fetch primario CKW (12:05 CET = 11:05 UTC) ────────────────────
    async def _job_ckw():
        from scheduler import fetch_with_retry
        from schemas import tomorrow_ch
        target = tomorrow_ch()
        with SessionLocal() as _s:
            _ckw = [
                _json.loads(t.full_config_json)
                for t in get_active_tariffs(_s)
                if t.adapter_class == "CkwAdapter"
            ]
        for _cfg in _ckw:
            await fetch_with_retry(_cfg, target, None)

    # ── JOB 2: Recovery CKW (17:30 CET = 16:30 UTC) ──────────────────────────
    async def _job_ckw_recovery():
        from scheduler import fetch_missing_for_date
        from schemas import tomorrow_ch
        await fetch_missing_for_date(
            tomorrow_ch(), adapter_class_filter="CkwAdapter", label="ckw_recovery"
        )

    # ── JOB 3: Fetch primario altri EVU (18:30 CET = 17:30 UTC) ──────────────
    async def _job_others():
        from scheduler import fetch_with_retry
        from schemas import tomorrow_ch
        target = tomorrow_ch()
        with SessionLocal() as _s:
            _others = [
                _json.loads(t.full_config_json)
                for t in get_active_tariffs(_s)
                if t.adapter_class != "CkwAdapter"
            ]
        _sem = _asyncio.Semaphore(3)

        async def _b(c):
            async with _sem:
                await fetch_with_retry(c, target, None)

        await _asyncio.gather(*[_b(c) for c in _others])

    # ── JOB 4: Recovery tutti (21:00 CET = 20:00 UTC) ────────────────────────
    async def _job_all_recovery():
        from scheduler import fetch_missing_for_date
        from schemas import tomorrow_ch
        await fetch_missing_for_date(tomorrow_ch(), label="all_recovery")

    # ── JOB 5: Last resort (23:30 CET = 22:30 UTC) ───────────────────────────
    async def _job_last_resort():
        from scheduler import fetch_missing_for_date, send_missing_alert
        from schemas import tomorrow_ch
        target = tomorrow_ch()
        still_missing = await fetch_missing_for_date(target, label="last_resort")
        if still_missing:
            await send_missing_alert(
                still_missing,
                target,
                context="last resort check (23:30 CET) — all recovery attempts exhausted",
            )

    # ── JOB 6: Health check mattutino (10:00 CET = 09:00 UTC) ────────────────
    async def _job_health():
        from scheduler import run_health_check
        await run_health_check()

    # ── JOB 8: Recovery mattutino TODAY (07:30 CET = 06:30 UTC) ──────────────
    # Fetcha attivamente le tariffe mancanti PER OGGI — copre il caso in cui
    # i fetch serali di ieri siano falliti. È un no-op se i dati ci sono.
    async def _job_morning_recovery_today():
        from scheduler import fetch_missing_for_date
        from schemas import today_ch
        target = today_ch()
        print(f"[morning_recovery_today] Verifica dati mancanti per oggi ({target})...")
        still_missing = await fetch_missing_for_date(target, label="morning_recovery_today")
        if still_missing:
            print(f"[morning_recovery_today] ⚠ Ancora mancanti dopo recovery: "
                  f"{[t for t, _ in still_missing]}")
        else:
            print(f"[morning_recovery_today] ✓ Tutti i dati presenti per oggi ({target})")

    # ── JOB 9: Recovery pomeridiano TODAY (13:00 CET = 12:00 UTC) ────────────
    # Secondo tentativo per TODAY in caso il mattutino non abbia risolto.
    # Utile per provider che aggiornano in mattinata (es. Primeo ~16:00 CET).
    # È un no-op efficiente se i dati ci sono già.
    async def _job_afternoon_recovery_today():
        from scheduler import fetch_missing_for_date
        from schemas import today_ch
        target = today_ch()
        still_missing = await fetch_missing_for_date(target, label="afternoon_recovery_today")
        if still_missing:
            print(f"[afternoon_recovery_today] ⚠ Ancora mancanti: "
                  f"{[t for t, _ in still_missing]}")

    # ── JOB 7: Manutenzione mensile (2:00 CET = 1:00 UTC, 1° del mese) ───────
    async def _job_monthly():
        from backfill import monthly_maintenance
        await monthly_maintenance()

    # ── Registrazione ─────────────────────────────────────────────────────────
    _scheduler.add_job(_job_ckw,                      CronTrigger(hour=11, minute=5),       id="ckw")
    _scheduler.add_job(_job_ckw_recovery,             CronTrigger(hour=16, minute=30),      id="ckw_recovery")
    _scheduler.add_job(_job_others,                   CronTrigger(hour=17, minute=30),      id="others")
    _scheduler.add_job(_job_all_recovery,             CronTrigger(hour=20, minute=0),       id="all_recovery")
    _scheduler.add_job(_job_last_resort,              CronTrigger(hour=22, minute=30),      id="last_resort")
    _scheduler.add_job(_job_health,                   CronTrigger(hour=9,  minute=0),       id="health")
    _scheduler.add_job(_job_morning_recovery_today,   CronTrigger(hour=6,  minute=30),      id="morning_recovery_today")
    _scheduler.add_job(_job_afternoon_recovery_today, CronTrigger(hour=12, minute=0),       id="afternoon_recovery_today")
    _scheduler.add_job(_job_monthly,                  CronTrigger(day=1, hour=1, minute=0), id="monthly")
    _scheduler.start()

    # ── Startup fetch immediato: recupera TODAY e TOMORROW se mancanti ────────
    # Lanciato in background subito dopo lo start dello scheduler, senza
    # bloccare il boot di FastAPI. Copre il caso "server era spento ieri sera".
    async def _startup_recovery():
        import asyncio as _aio
        from scheduler import fetch_missing_for_date
        from schemas import today_ch, tomorrow_ch

        today    = today_ch()
        tomorrow = tomorrow_ch()

        print(f"[startup_recovery] Avvio — verifica dati per oggi ({today}) e domani ({tomorrow})...")
        await _aio.sleep(5)  # lascia tempo al DB di stabilizzarsi

        missing_today = await fetch_missing_for_date(today, label="startup_today")
        if missing_today:
            print(f"[startup_recovery] ⚠ Ancora mancanti OGGI: {[t for t, _ in missing_today]}")
        else:
            print(f"[startup_recovery] ✓ Dati TODAY completi ({today})")

        missing_tomorrow = await fetch_missing_for_date(tomorrow, label="startup_tomorrow")
        if missing_tomorrow:
            print(f"[startup_recovery] ⚠ Ancora mancanti DOMANI: {[t for t, _ in missing_tomorrow]}")
        else:
            print(f"[startup_recovery] ✓ Dati TOMORROW completi ({tomorrow})")

    _asyncio.ensure_future(_startup_recovery())

    print("[startup] Scheduler avviato:")
    print("  06:30 UTC (07:30 CET) → recovery TODAY mattutino    [no-op se ok]")
    print("  09:00 UTC (10:00 CET) → health check mattutino      [alert se mancano]")
    print("  11:05 UTC (12:05 CET) → fetch CKW primario")
    print("  12:00 UTC (13:00 CET) → recovery TODAY pomeridiano  [no-op se ok]")
    print("  16:30 UTC (17:30 CET) → recovery CKW                [no-op se ok]")
    print("  17:30 UTC (18:30 CET) → fetch altri EVU primario")
    print("  20:00 UTC (21:00 CET) → recovery tutti              [no-op se ok]")
    print("  22:30 UTC (23:30 CET) → last resort                 [alert se vuoto]")
    print("  1° mese   01:00 UTC   → manutenzione mensile")
    print("[startup] Startup recovery lanciato in background (today + tomorrow)")

    yield
    
    try:
        _scheduler.shutdown(wait=True)
        print("[shutdown] Scheduler fermato — tutti i job completati.")
    except Exception as e:
        print(f"[shutdown] Scheduler shutdown con errore: {e}")
        try:
            _scheduler.shutdown(wait=False)  # forza se necessario
        except Exception:
            pass
    print("[shutdown] Processo terminato correttamente.")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Swiss Dynamic Tariffs API",
    description="Unified API for Swiss dynamic electricity tariffs",
    version="1.0.0",
    lifespan=lifespan,
)

# In main.py, dopo la creazione dell'app:
from fastapi import Request
import os

VALID_API_KEYS: set[str] = set(
    k.strip()
    for k in os.getenv("API_KEYS", "").split(",")
    if k.strip()
)

@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    # Escludi dashboard, health interno e docs
    public_paths = {"/", "/docs", "/redoc", "/openapi.json", "/api/v1/health"}
    if request.url.path in public_paths or not VALID_API_KEYS:
        return await call_next(request)
    key = request.headers.get("X-API-Key", "")
    if key not in VALID_API_KEYS:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Invalid or missing API key"}, status_code=401)
    return await call_next(request)


# ── API 1: lista tariffe ──────────────────────────────────────────────────────

@app.get("/api/v1/tariffs")
def get_tariffs():
    with SessionLocal() as session:
        tariffs = get_active_tariffs(session)
    return [
        {
            "tariff_id":               t.tariff_id,
            "tariff_name":             t.tariff_name,
            "provider_name":           t.provider_name,
            "zip_ranges":              json.loads(t.full_config_json).get("zip_ranges", []),
            "daily_update_time_utc":   t.daily_update_time_utc,
            "datetime_available_from_utc": (
                t.datetime_available_from.isoformat() if t.datetime_available_from else None
            ),
            "valid_until_utc":         (t.valid_until.isoformat() if t.valid_until else None),
            "sgr_compliant":           t.sgr_compliant,
            "dynamic_elements":        json.loads(t.dynamic_elements_json),
            "time_resolution_minutes": t.time_resolution_minutes,
        }
        for t in tariffs
    ]


# ── API 2: prezzi timeseries ──────────────────────────────────────────────────

@app.get("/api/v1/prices")
def get_prices_endpoint(
    tariff_id:  str = Query(..., description="Tariff ID"),
    start_time: str = Query(..., description="Date/time start UTC (es. 2026-03-16T00:00:00Z)"),
    end_time:   Optional[str] = Query(None, description="Date/time end UTC (optional, default: +1 day)"),
):
    # zoneinfo gestisce DST automaticamente per qualsiasi anno futuro
    try:
        import zoneinfo; tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz; tz_ch = pytz.timezone("Europe/Zurich")

    try:
        start_utc = datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        raise HTTPException(400, f"Invalid start_time: {start_time!r}. Format: 2026-03-16T00:00:00Z")

    if end_time:
        try:
            end_utc = datetime.fromisoformat(end_time.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            raise HTTPException(400, f"Invalid end_time: {end_time!r}")
    else:
        end_utc = start_utc + timedelta(days=1)

    # CKW usa timestamp locali svizzeri (+01:00/+02:00) che l'adapter converte in UTC.
    # Es: "2026-03-17T00:00+01:00" → salvato come 2026-03-16T23:00Z nel DB.
    # Una query con start=2026-03-17T00:00:00Z esclude quel primo slot → 95 invece di 96.
    # Fix: allineiamo i limiti alla mezzanotte locale svizzera → UTC.
    # Funziona per tutti i casi: CET (UTC+1), CEST (UTC+2), 23h/24h/25h.
    start_ld = start_utc.astimezone(tz_ch).date()
    end_ld   = end_utc.astimezone(tz_ch).date()
    start_db = datetime(start_ld.year, start_ld.month, start_ld.day, tzinfo=tz_ch).astimezone(timezone.utc)
    end_db   = datetime(end_ld.year,   end_ld.month,   end_ld.day,   tzinfo=tz_ch).astimezone(timezone.utc)
    if end_db <= start_db:  # stesso giorno locale (es. range intraday)
        end_db = (datetime(end_ld.year, end_ld.month, end_ld.day, tzinfo=tz_ch)
                  + timedelta(days=1)).astimezone(timezone.utc)

    with SessionLocal() as session:
        from database import Tariff
        tariff = session.get(Tariff, tariff_id)
        if not tariff:
            raise HTTPException(404, f"Tariff '{tariff_id}' not found")
        slots = get_prices(session, tariff_id, start_db, end_db)
        # Filtra per data locale richiesta (esclude slot di giorni adiacenti)
        slots = [s for s in slots
                 if start_ld <= s.slot_start_utc.astimezone(tz_ch).date() < end_ld]

    if not slots:
        raise HTTPException(404, f"No prices for '{tariff_id}' in the range {start_time} — {end_time or 'auto'}")

    response: dict = {
        "tariff_id":  tariff_id,
        "start_time": start_db.isoformat(),   # UTC start of the actual Swiss day returned
        "end_time":   end_db.isoformat(),     # UTC end of the actual Swiss day returned
        "slot_count": len(slots),
    }
    energy_series, grid_series, residual_series = [], [], []
    for s in slots:
        dt = s.slot_start_utc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = dt.isoformat()
        if s.energy_price   is not None: energy_series.append({ts: s.energy_price})
        if s.grid_price     is not None: grid_series.append({ts: s.grid_price})
        if s.residual_price is not None: residual_series.append({ts: s.residual_price})

    if energy_series:   response["energy_price_utc"]   = energy_series
    if grid_series:     response["grid_price_utc"]     = grid_series
    if residual_series: response["residual_price_utc"] = residual_series
    return response


def _find_tariff_for_zip(zip_code: int) -> Optional[str]:
    """Trova il tariff_id che copre questo CAP."""
    with SessionLocal() as session:
        tariffs = get_active_tariffs(session)
    for t in tariffs:
        config = json.loads(t.full_config_json)
        for r in config.get("zip_ranges", []):
            if isinstance(r, int) and r == zip_code:
                return t.tariff_id
            if isinstance(r, str):
                if "-" in r:
                    lo, hi = r.split("-")
                    if int(lo) <= zip_code <= int(hi):
                        return t.tariff_id
                elif int(r) == zip_code:
                    return t.tariff_id
    return None
  
def _prices_for_date(tariff_id: str, target_date) -> dict:
    """
    Logica comune per restituire i prezzi di un giorno specifico.
    Funzione Python pura — non un endpoint FastAPI, quindi chiamabile
    direttamente senza problemi con i Query objects.
    """
    try:
        import zoneinfo; tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz; tz_ch = pytz.timezone("Europe/Zurich")

    # Calcola il range UTC corretto per la data locale svizzera
    midnight    = datetime(target_date.year, target_date.month, target_date.day, tzinfo=tz_ch)
    start_db    = midnight.astimezone(timezone.utc)
    end_db      = (midnight + timedelta(days=1)).astimezone(timezone.utc)
    start_ld    = target_date
    end_ld      = target_date  # stesso giorno

    with SessionLocal() as session:
        from database import Tariff
        tariff = session.get(Tariff, tariff_id)
        if not tariff:
            raise HTTPException(404, f"Tariff '{tariff_id}' not found")
        slots = get_prices(session, tariff_id, start_db, end_db)
        slots = [s for s in slots
                 if s.slot_start_utc.astimezone(tz_ch).date() == target_date]

    if not slots:
        raise HTTPException(
            404,
            {
                "error":   "no_data",
                "message": f"No prices for '{tariff_id}' on {target_date}",
                "date":    target_date.isoformat(),
                "tariff_id": tariff_id,
            }
        )

    energy_series, grid_series, residual_series = [], [], []
    for s in slots:
        dt = s.slot_start_utc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = dt.isoformat()
        if s.energy_price   is not None: energy_series.append({ts: s.energy_price})
        if s.grid_price     is not None: grid_series.append({ts: s.grid_price})
        if s.residual_price is not None: residual_series.append({ts: s.residual_price})

    response: dict = {
        "tariff_id":  tariff_id,
        "date":       target_date.isoformat(),
        "start_time": start_db.isoformat(),
        "end_time":   end_db.isoformat(),
        "slot_count": len(slots),
    }
    if energy_series:   response["energy_price_utc"]   = energy_series
    if grid_series:     response["grid_price_utc"]     = grid_series
    if residual_series: response["residual_price_utc"] = residual_series
    return response


# ── /api/v1/prices/today ──────────────────────────────────────────────────────

@app.get("/api/v1/prices/today")
def get_today_prices(
    zip_code:  Optional[int] = Query(None, description="CAP svizzero, es. 6810"),
    tariff_id: Optional[str] = Query(None, description="Tariff ID diretto, es. aem_tariffa_dinamica"),
):
    """
    Prezzi della giornata corrente (ora locale svizzera).
    Usa zip_code OPPURE tariff_id — non entrambi.
    """
    from schemas import today_ch as _today_ch

    if zip_code and not tariff_id:
        tariff_id = _find_tariff_for_zip(zip_code)
        if not tariff_id:
            raise HTTPException(
                404,
                {"error": "zip_not_found",
                 "message": f"No tariff found for ZIP {zip_code}",
                 "zip_code": zip_code}
            )

    if not tariff_id:
        raise HTTPException(
            400,
            {"error": "missing_param",
             "message": "Provide either zip_code or tariff_id"}
        )

    return _prices_for_date(tariff_id, _today_ch())
  
  
  
# ── API 2b: tutti i prezzi in un colpo ───────────────────────────────────────

@app.get("/api/v1/prices/all")
def get_all_prices_endpoint(
    start_time: str = Query(..., description="Date/time start UTC (es. 2026-03-17T00:00:00Z)"),
    end_time:   Optional[str] = Query(None, description="Date/time end UTC (optional, default: +1 day)"),
):
    """
    Restituisce i prezzi di TUTTE le tariffe attive in un unico JSON.
    {tariff_id: {energy_price_utc: [...], grid_price_utc: [...], residual_price_utc: [...]}}
    """
    try:
        import zoneinfo; tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz; tz_ch = pytz.timezone("Europe/Zurich")

    try:
        start_utc = datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        raise HTTPException(400, f"Invalid start_time : {start_time!r}")
    if end_time:
        try:
            end_utc = datetime.fromisoformat(end_time.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            raise HTTPException(400, f"Invalid end_time : {end_time!r}")
    else:
        end_utc = start_utc + timedelta(days=1)

    start_ld = start_utc.astimezone(tz_ch).date()
    end_ld   = end_utc.astimezone(tz_ch).date()
    start_db = datetime(start_ld.year, start_ld.month, start_ld.day, tzinfo=tz_ch).astimezone(timezone.utc)
    end_db   = datetime(end_ld.year,   end_ld.month,   end_ld.day,   tzinfo=tz_ch).astimezone(timezone.utc)
    if end_db <= start_db:
        end_db = (datetime(end_ld.year, end_ld.month, end_ld.day, tzinfo=tz_ch) + timedelta(days=1)).astimezone(timezone.utc)
    end_ld_filter = start_ld + timedelta(days=1) if end_ld <= start_ld else end_ld

    result = {}
    with SessionLocal() as session:
        for tariff in get_active_tariffs(session):
            slots = get_prices(session, tariff.tariff_id, start_db, end_db)
            slots = [s for s in slots if start_ld <= s.slot_start_utc.astimezone(tz_ch).date() < end_ld_filter]
            if not slots:
                continue
            energy_s, grid_s, residual_s = [], [], []
            for s in slots:
                dt = s.slot_start_utc
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.isoformat()
                if s.energy_price   is not None: energy_s.append({ts: s.energy_price})
                if s.grid_price     is not None: grid_s.append({ts: s.grid_price})
                if s.residual_price is not None: residual_s.append({ts: s.residual_price})
            td: dict = {"slot_count": len(slots)}
            if energy_s:   td["energy_price_utc"]   = energy_s
            if grid_s:     td["grid_price_utc"]     = grid_s
            if residual_s: td["residual_price_utc"] = residual_s
            result[tariff.tariff_id] = td

    if not result:
        raise HTTPException(404, "No data for this range of days.")
    return {"start_time": start_db.isoformat(), "end_time": end_db.isoformat(), "tariffs": result}

# ── Summary / health endpoints ────────────────────────────────────────────────
@app.get("/api/v1/summary/daily")
def get_daily_summary(tariff_id: str = Query(...), days: int = Query(5, ge=1, le=30)):
    try:
        import zoneinfo; tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz; tz_ch = pytz.timezone("Europe/Zurich")

    with SessionLocal() as session:
        from database import Tariff
        if not session.get(Tariff, tariff_id):
            raise HTTPException(404, f"Tariff '{tariff_id}' not found")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days + 1)
        rows = (
            session.query(PriceSlotDB.slot_start_utc, PriceSlotDB.energy_price,
                          PriceSlotDB.grid_price, PriceSlotDB.residual_price)
            .filter(PriceSlotDB.tariff_id == tariff_id, PriceSlotDB.slot_start_utc >= cutoff)
            .order_by(PriceSlotDB.slot_start_utc).all()
        )
    if not rows:
        raise HTTPException(404, "NO data available")

    from schemas import today_ch as _today_ch
    today_local = _today_ch()   # data svizzera di OGGI — i dati di domani NON devono apparire

    from collections import defaultdict
    by_date: dict = defaultdict(list)
    for (dt, energy, grid, residual) in rows:
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        local_date = dt.astimezone(tz_ch).date()
        # ── FIX: esclude i dati pre-fetchati di domani dalla summary di oggi ──
        if local_date > today_local:
            continue
        total = sum(p for p in [energy, grid, residual] if p)
        if total > 0: by_date[local_date.isoformat()].append((total, dt))

    result = []
    for d in sorted(sorted(by_date.keys(), reverse=True)[:days]):
        slots = by_date[d]
        if not slots: continue
        avg = sum(t for t, _ in slots) / len(slots)
        min_val, min_dt = min(slots, key=lambda x: x[0])
        max_val, max_dt = max(slots, key=lambda x: x[0])
        result.append({
            "date": d, "slot_count": len(slots),
            "avg_rp": round(avg * 100, 2),
            "min_rp": round(min_val * 100, 2), "min_time": min_dt.strftime("%H:%M"),
            "max_rp": round(max_val * 100, 2), "max_time": max_dt.strftime("%H:%M"),
        })
    return result


_summary_cache: dict = {}

@app.get("/api/v1/summary/monthly")
def get_monthly_summary(tariff_id: str = Query(...), months: int = Query(3, ge=1, le=6)):
    import time
    cache_key = f"monthly:{tariff_id}:{months}"
    cached = _summary_cache.get(cache_key)
    if cached and time.time() - cached["ts"] < 300:
        return cached["data"]

    with SessionLocal() as session:
        from database import Tariff
        if not session.get(Tariff, tariff_id):
            raise HTTPException(404, f"Tariff '{tariff_id}' not found")
        cutoff = datetime.now(timezone.utc) - timedelta(days=months * 32)
        rows = (
            session.query(PriceSlotDB.slot_start_utc, PriceSlotDB.energy_price,
                          PriceSlotDB.grid_price, PriceSlotDB.residual_price)
            .filter(PriceSlotDB.tariff_id == tariff_id, PriceSlotDB.slot_start_utc >= cutoff)
            .order_by(PriceSlotDB.slot_start_utc).all()
        )
    if not rows:
        raise HTTPException(404, "NO data available")

    from collections import defaultdict
    by_month: dict = defaultdict(list)
    for (dt, energy, grid, residual) in rows:
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        month_key = dt.strftime("%Y-%m")
        total = sum(p for p in [energy, grid, residual] if p)
        if total > 0: by_month[month_key].append((total, dt))

    now = datetime.now(timezone.utc)
    result = []
    for m in sorted(sorted(by_month.keys(), reverse=True)[:months]):
        slots = by_month[m]
        if not slots: continue
        avg = sum(t for t, _ in slots) / len(slots)
        min_val, min_dt = min(slots, key=lambda x: x[0])
        max_val, max_dt = max(slots, key=lambda x: x[0])
        result.append({
            "month": m,
            "month_label": datetime.strptime(m, "%Y-%m").strftime("%B %Y"),
            "is_current": m == now.strftime("%Y-%m"),
            "days_with_data": len({dt.date() for _, dt in slots}),
            "slot_count": len(slots),
            "avg_rp": round(avg * 100, 2),
            "min_rp": round(min_val * 100, 2), "min_date": min_dt.strftime("%d/%m %H:%M"),
            "max_rp": round(max_val * 100, 2), "max_date": max_dt.strftime("%d/%m %H:%M"),
        })
    _summary_cache[cache_key] = {"ts": time.time(), "data": result}
    return result


@app.get("/api/v1/latest")
def get_latest(tariff_id: str = Query(...)):
    with SessionLocal() as session:
        from database import Tariff
        if not session.get(Tariff, tariff_id):
            raise HTTPException(404, f"Tariff '{tariff_id}' not found")
        row = (session.query(PriceSlotDB.slot_start_utc)
               .filter(PriceSlotDB.tariff_id == tariff_id)
               .order_by(PriceSlotDB.slot_start_utc.desc()).first())
    if not row:
        raise HTTPException(404, "NO data available")
    dt = row[0]
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return {"tariff_id": tariff_id, "latest_date": dt.date().isoformat()}

@app.get("/api/v1/metrics")
def get_metrics(
    tariff_id: Optional[str] = Query(None, description="Filtra per tariffa specifica"),
    days:      int           = Query(7,    ge=1, le=90, description="Finestra temporale in giorni"),
):
    """
    Statistiche strutturate sui fetch degli ultimi N giorni.
 
    Permette query tipo:
      - Quante volte ha fallito EKZ nell'ultima settimana?
      - Qual è la latenza media del fetch di Primeo?
      - Qual è il tasso di successo per tariffa?
 
    Risposta per ogni tariffa:
      total_fetches   → numero totale di tentativi
      success_rate    → percentuale di successi (status "ok" + "anomaly")
      avg_duration_ms → latenza media fetch in ms
      avg_slot_count  → slot medi ricevuti
      avg_price_rp    → prezzo medio in Rp/kWh (ultimi giorni con dati)
      last_status     → stato dell'ultimo fetch
      last_fetched_at → timestamp ultimo fetch
      errors_count    → numero di errori
      anomalies_count → numero di anomalie rilevate
      by_status       → breakdown per status: {"ok": N, "error": N, ...}
    """
    from database import FetchLog
    from datetime import timezone
 
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
 
    with SessionLocal() as session:
        q = session.query(FetchLog).filter(FetchLog.fetched_at >= cutoff)
        if tariff_id:
            q = q.filter(FetchLog.tariff_id == tariff_id)
        rows = q.order_by(FetchLog.fetched_at.desc()).all()
 
    if not rows:
        return {
            "window_days": days,
            "cutoff_utc":  cutoff.isoformat(),
            "tariffs":     {},
            "note":        "No fetch logs found. Logs are written starting from the next fetch cycle.",
        }
 
    from collections import defaultdict
    by_tariff: dict = defaultdict(list)
    for r in rows:
        by_tariff[r.tariff_id].append(r)
 
    result = {}
    for tid, entries in by_tariff.items():
        total    = len(entries)
        ok       = sum(1 for e in entries if e.status in ("ok", "anomaly"))
        errors   = sum(1 for e in entries if e.status == "error")
        anomalies= sum(1 for e in entries if e.status == "anomaly")
        empties  = sum(1 for e in entries if e.status == "empty")
 
        durations = [e.duration_ms for e in entries if e.duration_ms is not None]
        slots     = [e.slot_count  for e in entries if e.slot_count  is not None]
        prices    = [e.avg_price_rp for e in entries if e.avg_price_rp is not None]
 
        last = entries[0]  # già ordinati desc
        by_status = {}
        for e in entries:
            by_status[e.status] = by_status.get(e.status, 0) + 1
 
        result[tid] = {
            "total_fetches":   total,
            "success_rate":    round(ok / total * 100, 1) if total else 0,
            "errors_count":    errors,
            "anomalies_count": anomalies,
            "empty_count":     empties,
            "avg_duration_ms": round(sum(durations) / len(durations)) if durations else None,
            "avg_slot_count":  round(sum(slots) / len(slots), 1) if slots else None,
            "avg_price_rp":    round(sum(prices) / len(prices), 2) if prices else None,
            "last_status":     last.status,
            "last_fetched_at": last.fetched_at.isoformat() if last.fetched_at else None,
            "last_error":      last.error_msg if last.status == "error" else None,
            "by_status":       by_status,
        }
 
    return {
        "window_days": days,
        "cutoff_utc":  cutoff.isoformat(),
        "tariffs":     result,
    }

@app.get("/api/v1/health")
def health():
    try:
        import zoneinfo; tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz; tz_ch = pytz.timezone("Europe/Zurich")

    from schemas import today_ch as _today_ch
    from database import has_data_for_date

    now_utc      = datetime.now(timezone.utc)
    today_ch     = _today_ch()                        # data locale svizzera OGGI
    tomorrow_ch  = today_ch + timedelta(days=1)       # data locale svizzera DOMANI

    with SessionLocal() as session:
        tariffs = get_active_tariffs(session)
        statuses = []
        from adapters import list_available_adapters
        available_adapters = set(list_available_adapters())

        for t in tariffs:
            adapter_ready     = t.adapter_class in available_adapters
            last_fetch        = get_last_fetch(session, t.tariff_id)   # solo per display
            has_today_data    = has_data_for_date(session, t.tariff_id, today_ch, tz_ch)
            has_tomorrow_data = has_data_for_date(session, t.tariff_id, tomorrow_ch, tz_ch)

            # ── Status OGGI ──────────────────────────────────────────────────
            if not adapter_ready:
                status, meta = "pending", "Adapter not implemented yet"
            elif has_today_data:
                status, meta = "ok", None
            else:
                # Determina se è ancora troppo presto per aspettarsi i dati
                try:
                    upd_h, upd_m = map(int, t.daily_update_time_utc.split(":"))
                except Exception:
                    upd_h, upd_m = 17, 0
                # Il fetch di OGGI avviene la sera di IERI; se non c'è ancora
                # siamo in un edge case (server spento, primo avvio)
                if now_utc.hour < 10:
                    status, meta = "pending", f"Morning Health check — fetch expected last night"
                else:
                    status, meta = "missing", "NO data received for today"

            # ── Status DOMANI (pre-fetch) ─────────────────────────────────────
            if not adapter_ready:
                tomorrow_status = "adapter_missing"
            elif has_tomorrow_data:
                tomorrow_status = "prefetched"
            else:
                # Determina l'orario di fetch per questa tariffa
                try:
                    upd_h, upd_m = map(int, t.daily_update_time_utc.split(":"))
                except Exception:
                    upd_h, upd_m = 17, 0
                now_past_update = (now_utc.hour > upd_h or
                                   (now_utc.hour == upd_h and now_utc.minute >= upd_m + 30))
                if now_past_update:
                    tomorrow_status = "missing"   # avrebbe dovuto essere fetchato
                else:
                    tomorrow_status = "pending"   # non ancora il momento

            statuses.append({
                "tariff_id":          t.tariff_id,
                "provider_name":      t.provider_name,
                "tariff_name":        t.tariff_name,
                "adapter_class":      t.adapter_class,
                "adapter_ready":      adapter_ready,
                "last_fetch_utc":     last_fetch.isoformat() if last_fetch else None,
                # ── nuovi campi semanticamente corretti ──
                "has_today_data":     has_today_data,     # dati PER oggi (CH locale)
                "has_tomorrow_data":  has_tomorrow_data,  # pre-fetch PER domani
                "status":             status,             # ok / pending / missing
                "status_detail":      meta,
                "tomorrow_status":    tomorrow_status,    # prefetched / pending / missing
                "daily_update_utc":   t.daily_update_time_utc,
                # ── per compatibilità con il JS esistente ──
                "today_ch":           today_ch.isoformat(),
                "tomorrow_ch":        tomorrow_ch.isoformat(),
            })

    ready_count = sum(1 for s in statuses if s["adapter_ready"])
    ok_count    = sum(1 for s in statuses if s["status"] == "ok")
    return {
        "status":           "ok" if ok_count == ready_count and ready_count > 0 else "degraded",
        "timestamp":        now_utc.isoformat(),
        "today_ch":         today_ch.isoformat(),
        "tomorrow_ch":      tomorrow_ch.isoformat(),
        "adapters_ready":   ready_count,
        "adapters_total":   len(statuses),
        "ok_count":         ok_count,
        "prefetched_count": sum(1 for s in statuses if s["tomorrow_status"] == "prefetched"),
        "tariffs":          statuses,
    }

# ── Smart summary ─────────────────────────────────────────────────────────────

@app.get("/api/v1/smart")
def get_smart_summary(date_str: Optional[str] = Query(None)):
    import time as _time
    from schemas import today_ch as _today_ch
    target_date = date.fromisoformat(date_str) if date_str else _today_ch()
    cache_key = f"smart:{target_date}"
    cached = _summary_cache.get(cache_key)
    if cached and _time.time() - cached["ts"] < 120:
        return cached["data"]

    try:
        import zoneinfo; tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz; tz_ch = pytz.timezone("Europe/Zurich")

    now_utc = datetime.now(timezone.utc)
    local_midnight = datetime(target_date.year, target_date.month, target_date.day, tzinfo=tz_ch)
    start_utc = local_midnight.astimezone(timezone.utc)
    end_utc   = (local_midnight + timedelta(days=1)).astimezone(timezone.utc)
    month_start = datetime(target_date.year, target_date.month, 1, tzinfo=timezone.utc)

    with SessionLocal() as session:
        tariffs = get_active_tariffs(session)
        from adapters import list_available_adapters
        available = set(list_available_adapters())
        result_tariffs = []
        for t in tariffs:
            day_slots = get_prices(session, t.tariff_id, start_utc, end_utc)
            if not day_slots:
                result_tariffs.append({
                    "tariff_id": t.tariff_id, "provider_name": t.provider_name,
                    "tariff_name": t.tariff_name, "has_data": False,
                    "adapter_ready": t.adapter_class in available,
                }); continue

            slots_data = []
            for s in day_slots:
                dt = s.slot_start_utc
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                total = sum(p for p in [s.energy_price, s.grid_price, s.residual_price] if p)
                if total > 0:
                    slots_data.append({"ts": dt, "total": total,
                        "grid": s.grid_price or 0, "energy": s.energy_price or 0,
                        "residual": s.residual_price or 0})
            if not slots_data: continue

            day_avg = sum(s["total"] for s in slots_data) / len(slots_data)
            month_slots = (
                session.query(PriceSlotDB.energy_price, PriceSlotDB.grid_price, PriceSlotDB.residual_price)
                .filter(PriceSlotDB.tariff_id == t.tariff_id,
                        PriceSlotDB.slot_start_utc >= month_start,
                        PriceSlotDB.slot_start_utc < end_utc).all()
            )
            month_totals = [sum(p for p in [e, g, r] if p) for e, g, r in month_slots
                            if sum(p for p in [e, g, r] if p) > 0]
            month_avg = sum(month_totals) / len(month_totals) if month_totals else day_avg

            current_slot = next((s for s in reversed(slots_data) if s["ts"] <= now_utc), None)
            if not current_slot: current_slot = slots_data[0]

            ratio = current_slot["total"] / month_avg if month_avg > 0 else 1
            signal = "green" if ratio < 0.85 else "red" if ratio >= 1.15 else "yellow"

            sorted_asc  = sorted(slots_data, key=lambda x: x["total"])
            sorted_desc = sorted(slots_data, key=lambda x: x["total"], reverse=True)

            def fmt_slot(s):
                local_t = s["ts"].astimezone(tz_ch)
                return {"time": local_t.strftime("%H:%M"),
                        "total_rp":    round(s["total"] * 100, 2),
                        "grid_rp":     round(s["grid"] * 100, 2),
                        "energy_rp":   round(s["energy"] * 100, 2),
                        "residual_rp": round(s["residual"] * 100, 2)}

            hourly = {}
            for s in slots_data:
                h = s["ts"].astimezone(tz_ch).hour
                hourly.setdefault(h, []).append(s["total"])
            hourly_avg = [
                round(sum(hourly[h]) / len(hourly[h]) * 100, 2) if h in hourly else None
                for h in range(24)
            ]
            result_tariffs.append({
                "tariff_id":        t.tariff_id,
                "provider_name":    t.provider_name,
                "tariff_name":      t.tariff_name,
                "has_data":         True,
                "adapter_ready":    t.adapter_class in available,
                "signal":           signal,
                "current_price_rp": round(current_slot["total"] * 100, 2),
                "day_avg_rp":       round(day_avg * 100, 2),
                "month_avg_rp":     round(month_avg * 100, 2),
                "best_slots":       [fmt_slot(s) for s in sorted_asc[:3]],
                "worst_slots":      [fmt_slot(s) for s in sorted_desc[:2]],
                "hourly_rp":        hourly_avg,
            })

    data = {"date": target_date.isoformat(), "tariffs": result_tariffs}
    _summary_cache[cache_key] = {"ts": _time.time(), "data": data}
    return data


# ── Dashboard HTML ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard():
    from schemas import today_ch as _today_ch
    today = _today_ch().isoformat()
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Swiss Dynamic Tariffs</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:system-ui,sans-serif;background:#f4f5f7;color:#1a1a2e}}
    header{{background:#1a1a2e;color:white;padding:1rem 2rem;display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
    header h1{{font-size:1.1rem;font-weight:500;flex:1}}
    .badge{{font-size:11px;padding:3px 10px;border-radius:999px;font-weight:500}}
    .lang-btns{{display:flex;gap:4px}}
    .lang-btn{{font-size:11px;padding:3px 8px;border-radius:4px;border:1px solid rgba(255,255,255,0.3);
               background:transparent;color:rgba(255,255,255,0.7);cursor:pointer}}
    .lang-btn.active{{background:rgba(255,255,255,0.2);color:white;border-color:rgba(255,255,255,0.6)}}
    main{{max-width:1280px;margin:0 auto;padding:1.5rem;display:flex;flex-direction:column;gap:1.25rem}}
    .info-bar{{background:white;border:0.5px solid #e5e5e5;border-radius:10px;padding:10px 16px;
               display:flex;align-items:center;gap:16px;flex-wrap:wrap;font-size:12px;color:#555}}
    .unit-pill{{display:inline-flex;align-items:center;gap:6px;background:#f0f6ff;border-radius:6px;padding:4px 10px;font-size:11px}}
    .unit-pill b{{color:#185fa5}}
    .metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
    .metric{{background:white;border-radius:10px;padding:1rem 1.25rem;border:0.5px solid #e5e5e5}}
    .metric .label{{font-size:11px;color:#888;margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em}}
    .metric .value{{font-size:24px;font-weight:600;line-height:1.1}}
    .metric .sub{{font-size:11px;color:#aaa;margin-top:3px}}
    .row{{display:grid;grid-template-columns:1fr 340px;gap:1.25rem}}
    .card{{background:white;border-radius:12px;border:0.5px solid #e5e5e5;padding:1.25rem}}
    .card-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem}}
    .card-title{{font-size:13px;font-weight:600;color:#333}}
    .tariff-row{{display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap}}
    #tariff-select{{font-size:12px;padding:5px 8px;border:0.5px solid #ddd;border-radius:6px;flex:1;min-width:180px}}
    .day-btns{{display:flex;gap:4px;flex-wrap:wrap}}
    .day-btn{{font-size:11px;padding:4px 10px;border-radius:6px;border:0.5px solid #ddd;
              background:white;color:#555;cursor:pointer;transition:all .15s;white-space:nowrap}}
    .day-btn:hover{{border-color:#185fa5;color:#185fa5;background:#eef4fc}}
    .day-btn.active{{background:#185fa5;color:white;border-color:#185fa5;font-weight:500}}
    .chart-wrap{{position:relative;height:240px;width:100%;overflow:hidden}}
    .chart-wrap canvas{{position:absolute;inset:0;width:100%!important;height:100%!important}}
    .chart-placeholder{{height:240px;display:flex;align-items:center;justify-content:center;
                         color:#aaa;font-size:13px;text-align:center;line-height:1.6}}
    .month-grid{{display:flex;flex-direction:column;gap:10px}}
    .month-card{{border:0.5px solid #e5e5e5;border-radius:10px;padding:12px 14px}}
    .month-card.current{{border-color:#185fa5;background:#f0f6ff}}
    .month-label{{font-size:12px;font-weight:600;color:#333;margin-bottom:8px;display:flex;align-items:center;gap:6px}}
    .month-tag{{font-size:10px;padding:2px 7px;border-radius:999px;background:#185fa5;color:white}}
    .month-tag.live{{background:#1d9e75;animation:pulse 2s infinite}}
    @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.6}}}}
    .month-stats{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}}
    .mstat{{text-align:center}}
    .mstat .mv{{font-size:16px;font-weight:600}}
    .mstat .ml{{font-size:10px;color:#888;margin-top:1px}}
    .mstat .mt{{font-size:10px;color:#aaa}}
    .mv.avg{{color:#185fa5}}.mv.min{{color:#1d9e75}}.mv.max{{color:#e24b4a}}
    .month-meta{{font-size:10px;color:#bbb;margin-top:6px}}
    .slot-detail{{margin-top:auto;padding-top:12px;border-top:0.5px solid #e5e5e5}}
    .status-list{{display:flex;flex-direction:column;gap:6px;max-height:320px;overflow-y:auto}}
    .status-row{{display:flex;align-items:center;justify-content:space-between;
                 padding:7px 10px;background:#f8f9fa;border-radius:6px}}
    .status-name{{font-size:12px;font-weight:500}}
    .status-meta{{font-size:10px;color:#888;margin-top:1px}}
    .s-ok{{font-size:10px;padding:2px 8px;border-radius:999px;background:#d1fae5;color:#065f46;font-weight:500}}
    .s-warn{{font-size:10px;padding:2px 8px;border-radius:999px;background:#fef3c7;color:#92400e;font-weight:500}}
    .s-miss{{font-size:10px;padding:2px 8px;border-radius:999px;background:#fee2e2;color:#991b1b;font-weight:500}}
    .bottom-row{{display:grid;grid-template-columns:1fr 1fr;gap:1.25rem}}
    .tooltip-def{{position:relative;display:inline-block;cursor:help;border-bottom:1px dashed #aaa}}
    .tooltip-def:hover::after{{content:attr(data-tip);position:absolute;bottom:120%;left:50%;transform:translateX(-50%);
      background:#1a1a2e;color:white;padding:6px 10px;border-radius:6px;font-size:11px;white-space:nowrap;z-index:99;pointer-events:none}}
    @media(max-width:900px){{.metrics{{grid-template-columns:1fr 1fr}}.row,.bottom-row{{grid-template-columns:1fr}}}}
    .series-toggle{{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;
      border-radius:6px;border:1.5px solid;cursor:pointer;font-size:11px;font-weight:500;
      user-select:none;transition:opacity .15s,background .15s}}
    .series-toggle.off{{opacity:.4;background:white!important}}
    .series-toggle .chk{{width:14px;height:14px;border-radius:3px;border:1.5px solid currentColor;
      display:inline-flex;align-items:center;justify-content:center;font-size:10px;flex-shrink:0}}
    .view-tabs{{display:flex;gap:0;background:#f1efe8;border-radius:8px;padding:3px;margin-left:12px}}
    .view-tab{{font-size:11px;font-weight:500;padding:4px 12px;border-radius:6px;border:none;
      background:transparent;color:#888;cursor:pointer;transition:all .15s}}
    .view-tab.active{{background:white;color:#1a1a2e;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
    #consumer-view{{display:none}}
    #developer-view{{display:block}}
    .signal-card{{display:flex;align-items:center;gap:14px;padding:14px 16px;border-radius:10px;border:0.5px solid #e5e5e5;background:white}}
    .semaphore{{display:flex;gap:5px;align-items:center}}
    .sem-dot{{width:22px;height:22px;border-radius:50%;transition:opacity .3s}}
    .best-slots{{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}}
    .slot-pill{{font-size:11px;font-weight:500;padding:4px 10px;border-radius:6px}}
    .slot-pill.cheap{{background:#e1f5ee;color:#085041}}
    .slot-pill.exp{{background:#fcebeb;color:#791f1f}}
    .mini-bars{{display:flex;align-items:flex-end;gap:1.5px;height:44px;margin-top:8px}}
    .mini-bar{{flex:1;border-radius:2px 2px 0 0;min-height:3px;transition:background .3s}}
    .compare-row{{display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px}}
    .compare-bar-wrap{{flex:1;height:5px;background:#eee;border-radius:3px;overflow:hidden}}
    .compare-bar-fill{{height:100%;border-radius:3px;transition:width .4s}}
    .tariff-tabs{{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:12px}}
    .tariff-tab{{font-size:11px;padding:4px 10px;border-radius:6px;border:0.5px solid #ddd;
      background:white;color:#555;cursor:pointer;transition:all .15s}}
    .tariff-tab.active{{background:#1a1a2e;color:white;border-color:#1a1a2e}}
    .api-row{{background:#f8f9fa;border-radius:7px;padding:9px 12px;border:0.5px solid #eee;transition:border-color .15s}}
    .api-row:hover{{border-color:#c5d8f0}}
    .api-method{{font-size:10px;font-weight:700;color:white;background:#185fa5;padding:2px 6px;border-radius:4px;margin-right:6px;letter-spacing:.03em}}
    .api-path{{font-family:monospace;font-size:12px;font-weight:600;color:#1a1a2e}}
    .copy-btn{{font-size:10px;padding:3px 9px;border-radius:5px;border:0.5px solid #ddd;
      background:white;color:#555;cursor:pointer;transition:all .15s;white-space:nowrap}}
    .copy-btn:hover{{background:#185fa5;color:white;border-color:#185fa5}}
    .copy-btn.copied{{background:#1d9e75;color:white;border-color:#1d9e75}}
    .api-open-link{{font-size:10px;padding:3px 8px;border-radius:5px;border:0.5px solid #ddd;
      color:#555;text-decoration:none;transition:all .15s}}
    .api-open-link:hover{{background:#f0f4fa;border-color:#aaa}}
    #api-view{{display:none}}
    #map-view{{display:none}}
    #map-svg-container{{width:100%;background:#f8f9fa;border-radius:12px;
      border:0.5px solid #e5e5e5;overflow:hidden;position:relative}}
    #map-svg-container svg{{display:block;width:100%}}
    #map-loading{{padding:3rem;text-align:center;font-size:13px;color:#aaa}}
    #map-tooltip{{position:absolute;background:white;border:0.5px solid #ddd;
      border-radius:8px;padding:8px 12px;font-size:12px;pointer-events:none;
      display:none;z-index:20;max-width:240px;line-height:1.5;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
    #map-legend{{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:14px;padding:0 2px}}
    .map-legend-item{{display:flex;align-items:center;gap:7px;font-size:11px;color:#555}}
    .map-legend-dot{{width:11px;height:11px;border-radius:50%;flex-shrink:0}}
    #map-filter-row{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}}
    .map-filter-btn{{font-size:11px;padding:4px 12px;border-radius:6px;
      border:0.5px solid #ddd;background:white;color:#555;cursor:pointer;transition:all .15s}}
    .map-filter-btn:hover{{border-color:#185fa5;color:#185fa5}}
    .map-filter-btn.active{{background:#1a1a2e;color:white;border-color:#1a1a2e}}
    .api-section{{background:white;border:0.5px solid #e5e5e5;border-radius:12px;padding:1.25rem;margin-bottom:1.25rem}}
    .api-section-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:.6rem}}
    .api-section-title{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
    .api-desc{{font-size:12px;color:#666;margin:4px 0 12px 0;line-height:1.6}}
    .api-params-table{{width:100%;border-collapse:collapse;font-size:11px}}
    .api-params-table td{{padding:4px 8px;vertical-align:top;border-bottom:0.5px solid #f0f0f0}}
    .api-params-table tr:last-child td{{border-bottom:none}}
    .api-params-table td:first-child{{font-family:monospace;color:#185fa5;font-weight:600;white-space:nowrap;width:200px}}
    .api-params-wrap{{background:#f8f9fa;border-radius:8px;padding:8px 4px;margin:10px 0}}
    .api-params-head{{font-size:10px;color:#aaa;text-transform:uppercase;letter-spacing:.06em;padding:0 8px 6px}}
    .api-url-box{{background:#f0f4fa;border-radius:8px;padding:10px 14px;font-family:monospace;
      font-size:11px;color:#185fa5;margin-top:12px;display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}}
    .api-tariff-select{{font-size:11px;padding:5px 9px;border:0.5px solid #c5d8f0;border-radius:6px;
      background:#e8f1fa;color:#185fa5;font-weight:500;cursor:pointer;max-width:100%}}
    .api-badge-req{{font-size:9px;padding:1px 5px;border-radius:3px;background:#dbeafe;color:#1e40af;font-weight:600;vertical-align:middle;margin-left:4px}}
    .api-badge-opt{{font-size:9px;padding:1px 5px;border-radius:3px;background:#f3f4f6;color:#6b7280;font-weight:500;vertical-align:middle;margin-left:4px}}
    .api-links-row{{display:flex;gap:8px;flex-wrap:wrap;margin-top:1.5rem;padding-top:1rem;border-top:0.5px solid #e5e5e5}}
    .api-links-row a{{font-size:11px;padding:5px 14px;border-radius:6px}}
    .api-view-header{{margin-bottom:1.5rem}}
    .api-view-title{{font-size:15px;font-weight:600;color:#1a1a2e;margin-bottom:3px}}
    .api-view-sub{{font-size:12px;color:#888}}
    code{{font-family:monospace;font-size:10.5px;background:#f0f4fa;color:#185fa5;padding:1px 5px;border-radius:3px}}
  </style>
</head>
<body>
<header>
  <h1>Swiss Dynamic Electricity Tariffs</h1>
  <span class="badge" id="header-badge" style="background:#ba7517">...</span>
  <div class="view-tabs">
    <button class="view-tab" id="tab-smart" onclick="switchView('smart')">Smart</button>
    <button class="view-tab active" id="tab-data" onclick="switchView('data')">Data</button>
    <button class="view-tab" id="tab-map" onclick="switchView('map')" data-i18n="tab_map">Map</button>
    <button class="view-tab" id="tab-api" onclick="switchView('api')">API</button>
  </div>
  <div class="lang-btns">
    <button class="lang-btn active" onclick="setLang('en')">EN</button>
    <button class="lang-btn" onclick="setLang('de')">DE</button>
    <button class="lang-btn" onclick="setLang('fr')">FR</button>
    <button class="lang-btn" onclick="setLang('it')">IT</button>
  </div>
  <span style="margin-left:auto;font-size:11px;opacity:.6" id="header-time"></span>
</header>
<main>

  <!-- ── Consumer view ──────────────────────────────────────────────────── -->
  <div id="consumer-view">
    <div class="tariff-tabs" id="smart-tariff-tabs"></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1.25rem" id="smart-grid">
      <div class="signal-card" id="smart-signal-card">
        <div>
          <div class="semaphore" id="smart-semaphore">
            <div class="sem-dot" id="sem-g" style="background:#1d9e75"></div>
            <div class="sem-dot" id="sem-y" style="background:#ba7517"></div>
            <div class="sem-dot" id="sem-r" style="background:#e24b4a"></div>
          </div>
          <div style="font-size:13px;font-weight:500;margin-top:6px" id="smart-signal-label">—</div>
          <div style="font-size:11px;color:#888;margin-top:2px" id="smart-signal-sub">—</div>
        </div>
        <div style="margin-left:auto;text-align:right">
          <div style="font-size:32px;font-weight:700;line-height:1;color:#1a1a2e" id="smart-price">—</div>
          <div style="font-size:11px;color:#888;margin-top:3px">Rp/kWh</div>
        </div>
      </div>
      <div class="card">
        <div style="font-size:11px;font-weight:600;color:#333;margin-bottom:8px" data-i18n="smart_best_times"></div>
        <div class="best-slots" id="smart-best"></div>
        <div style="font-size:10px;color:#aaa;margin:8px 0 4px" data-i18n="smart_avoid"></div>
        <div class="best-slots" id="smart-worst"></div>
      </div>
    </div>
    <div class="card" style="margin-top:1.25rem">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <span style="font-size:11px;font-weight:600;color:#333" data-i18n="smart_today_profile"></span>
        <span style="font-size:10px;color:#aaa" data-i18n="smart_color_legend"></span>
      </div>
      <div class="mini-bars" id="smart-bars"></div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:#aaa;margin-top:3px">
        <span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span>
      </div>
    </div>
    <div class="card" style="margin-top:1.25rem">
      <div style="font-size:11px;font-weight:600;color:#333;margin-bottom:10px" data-i18n="smart_compare"></div>
      <div id="smart-compare-rows"></div>
    </div>
  </div>

  <!-- ── Developer view ────────────────────────────────────────────────── -->
  <div id="developer-view">
    <div class="info-bar">
      <span data-i18n="unit_explanation" style="color:#555;font-size:12px"></span>
      <span class="unit-pill"><b>Rp/kWh</b> = Rappen per kilowatt-hour = 1/100 CHF/kWh</span>
      <span class="unit-pill">0.10 CHF/kWh = <b>10 Rp/kWh</b></span>
      <span class="unit-pill" style="background:#f0faf5">
        Grid + Energy + Residual = <b data-i18n="total_price"></b>
      </span>
      <span style="display:inline-flex;align-items:center;gap:10px;margin-left:4px;padding:4px 10px;background:#fafafa;border:0.5px solid #e5e5e5;border-radius:6px;font-size:11px">
        <span style="display:flex;align-items:center;gap:4px">
          <span style="width:8px;height:8px;border-radius:2px;background:#185fa5;flex-shrink:0"></span>
          <span style="color:#555"><span class="tooltip-def" data-component="grid" data-tip="">Grid</span></span>
        </span>
        <span style="display:flex;align-items:center;gap:4px">
          <span style="width:8px;height:8px;border-radius:2px;background:#1d9e75;flex-shrink:0"></span>
          <span style="color:#555"><span class="tooltip-def" data-component="energy" data-tip="">Energy</span></span>
        </span>
        <span style="display:flex;align-items:center;gap:4px">
          <span style="width:8px;height:8px;border-radius:2px;background:#ba7517;flex-shrink:0"></span>
          <span style="color:#555"><span class="tooltip-def" data-component="residual" data-tip="">Residual</span></span>
        </span>
      </span>
    </div>

  <div class="metrics">
    <div class="metric">
      <div class="label" data-i18n="active_tariffs"></div>
      <div class="value" id="m-total">—</div>
      <div class="sub" id="m-total-sub">loading...</div>
    </div>
    <div class="metric">
      <div class="label" id="m-min-label" data-i18n="cheapest_hour"></div>
      <div class="value" style="color:#1d9e75" id="m-min">—</div>
      <div class="sub" id="m-min-sub"></div>
    </div>
    <div class="metric">
      <div class="label" id="m-max-label" data-i18n="most_expensive_hour"></div>
      <div class="value" style="color:#e24b4a" id="m-max">—</div>
      <div class="sub" id="m-max-sub"></div>
    </div>
    <div class="metric">
      <div class="label" data-i18n="viewing_day"></div>
      <div class="value" style="font-size:16px;padding-top:4px" id="m-date">—</div>
      <div class="sub" id="m-slots"></div>
    </div>
  </div>

  <div class="row">
    <div class="card">
      <div class="card-header">
        <span class="card-title" id="chart-title" data-i18n="daily_profile"></span>
      </div>
      <div class="tariff-row">
        <select id="tariff-select" onchange="onTariffChange()"></select>
      </div>
      <div class="day-btns" id="day-btns"></div>
      <div style="height:12px"></div>
      <div class="chart-wrap">
        <div class="chart-placeholder" id="chart-placeholder">
          <div data-i18n="select_tariff"></div>
        </div>
        <canvas id="priceChart" style="display:none"></canvas>
      </div>
      <div id="series-toggles" style="display:none;gap:8px;margin-top:10px;flex-wrap:wrap"></div>
      <div style="margin-top:10px;min-height:80px">
        <div id="slot-panel" style="display:none;background:#f8f9fa;border-radius:8px;padding:10px 12px">
          <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:6px">
            <div style="display:flex;align-items:baseline;gap:6px">
              <span style="font-size:22px;font-weight:700" id="sp-total">—</span>
              <span style="font-size:11px;color:#888">Rp/kWh</span>
            </div>
            <span style="font-size:11px;color:#888" id="sp-time">—</span>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;margin-bottom:8px">
            <div style="background:white;border-radius:6px;padding:5px 8px;text-align:center;border:0.5px solid #e0eeff">
              <div style="font-size:12px;font-weight:600;color:#185fa5" id="sp-grid">—</div>
              <div style="font-size:9px;color:#888;margin-top:1px">
                <span class="tooltip-def" data-tip="Network usage fee charged by grid operator">Grid</span>
              </div>
            </div>
            <div style="background:white;border-radius:6px;padding:5px 8px;text-align:center;border:0.5px solid #d0f0e0">
              <div style="font-size:12px;font-weight:600;color:#1d9e75" id="sp-energy">—</div>
              <div style="font-size:9px;color:#888;margin-top:1px">
                <span class="tooltip-def" data-tip="Electricity commodity price (spot market)">Energy</span>
              </div>
            </div>
            <div style="background:white;border-radius:6px;padding:5px 8px;text-align:center;border:0.5px solid #ffe8c0">
              <div style="font-size:12px;font-weight:600;color:#ba7517" id="sp-residual">—</div>
              <div style="font-size:9px;color:#888;margin-top:1px">
                <span class="tooltip-def" data-tip="Residual: SDL, Stromreserve, Netzzuschlag, levies">Residual</span>
              </div>
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <div style="flex:1;height:5px;background:#e5e5e5;border-radius:3px;overflow:hidden">
              <div id="sp-bar" style="height:100%;border-radius:3px;transition:width .15s,background .15s"></div>
            </div>
            <span style="font-size:11px;font-weight:600;min-width:54px;text-align:right" id="sp-vs">—</span>
          </div>
          <div style="font-size:10px;color:#aaa;margin-top:3px" id="sp-avg-ref"></div>
        </div>
        <div id="slot-panel-empty" style="font-size:12px;color:#bbb;text-align:center;padding:12px 0" data-i18n="hover_chart"></div>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><span class="card-title" data-i18n="monthly_summary"></span></div>
      <div class="month-grid" id="month-grid">
        <div style="color:#aaa;font-size:12px" data-i18n="select_tariff"></div>
      </div>
    </div>
  </div>

  <!-- Fetch status moved to API tab only — keep hidden element so JS doesn't throw -->
  <div style="display:none" id="status-list"></div>

  <!-- REMOVED API card – moved to dedicated API tab -->
  <div style="display:none" id="api-endpoint-list-legacy">
        <div class="api-row">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
            <div><span class="api-method">GET</span><span class="api-path">/api/v1/tariffs</span></div>
            <button class="copy-btn" onclick="copyEndpoint(this,'/api/v1/tariffs')">Copy</button>
          </div>
          <div style="font-size:11px;color:#666" data-i18n="api_tariffs_desc"></div>
        </div>
        <div class="api-row">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
            <div><span class="api-method">GET</span><span class="api-path">/api/v1/prices</span></div>
            <button class="copy-btn" id="copy-prices-btn" onclick="copyPricesEndpoint(this)">Copy</button>
          </div>
          <div style="font-size:10px;color:#666" data-i18n="api_prices_desc"></div>
          <div style="margin-top:6px;background:#f0f4fa;border-radius:5px;padding:7px 10px;font-family:monospace;font-size:10px;color:#185fa5;line-height:1.7" id="prices-example-url">
            /api/v1/prices?tariff_id=ckw_home_dynamic&amp;start_time={today}T00:00:00Z
          </div>
          <div style="font-size:10px;color:#aaa;margin-top:3px">↑ updates with selected tariff &amp; today's date</div>
        </div>
        <div class="api-row">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
            <div><span class="api-method">GET</span><span class="api-path">/api/v1/prices/all</span></div>
            <button class="copy-btn" onclick="copyEndpoint(this,'/api/v1/prices/all?start_time={today}T00:00:00Z')">Copy</button>
          </div>
          <div style="font-size:10px;color:#666">All tariffs at once — single JSON with tariff_id as key</div>
          <div style="margin-top:6px;background:#f0f4fa;border-radius:5px;padding:7px 10px;font-family:monospace;font-size:10px;color:#185fa5">
            /api/v1/prices/all?start_time={{today}}T00:00:00Z
          </div>
        </div>
        <div class="api-row">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
            <div><span class="api-method">GET</span><span class="api-path">/api/v1/health</span></div>
            <button class="copy-btn" onclick="copyEndpoint(this,'/api/v1/health')">Copy</button>
          </div>
          <div style="font-size:11px;color:#666" data-i18n="api_health_desc"></div>
        </div>
  </div><!-- end api-endpoint-list-legacy -->

  </div><!-- end developer-view -->


  <!-- ── Map view ─────────────────────────────────────────────────────────── -->
  <div id="map-view">

    <div class="api-view-header">
      <div class="api-view-title" data-i18n="map_title">Coverage map</div>
      <div class="api-view-sub" data-i18n="map_subtitle">Dynamic tariff providers by Swiss municipality · source: ElCom 2026</div>
    </div>

    <div id="map-filter-row">
      <button class="map-filter-btn active" data-filter="all" onclick="filterMap('all')" data-i18n="map_filter_all">All</button>
      <button class="map-filter-btn" data-filter="ckw"    onclick="filterMap('ckw')">CKW</button>
      <button class="map-filter-btn" data-filter="ekz"    onclick="filterMap('ekz')">EKZ</button>
      <button class="map-filter-btn" data-filter="groupe" onclick="filterMap('groupe')">Groupe E</button>
      <button class="map-filter-btn" data-filter="primeo" onclick="filterMap('primeo')">Primeo</button>
      <button class="map-filter-btn" data-filter="ail"    onclick="filterMap('ail')">AIL</button>
      <button class="map-filter-btn" data-filter="avag"   onclick="filterMap('avag')">AVAG</button>
      <button class="map-filter-btn" data-filter="aem"    onclick="filterMap('aem')">AEM</button>
      <button class="map-filter-btn" data-filter="ekze"   onclick="filterMap('ekze')">EKZ Einsiedeln</button>
      <button class="map-filter-btn" data-filter="ega"    onclick="filterMap('ega')">EGA</button>
      <button class="map-filter-btn" data-filter="elag"   onclick="filterMap('elag')">ELAG</button>
    </div>

    <div id="map-svg-container">
      <div id="map-loading" data-i18n="map_loading">Loading municipality boundaries…</div>
      <svg id="map-svg"></svg>
      <div id="map-tooltip"></div>
    </div>

    <div id="map-legend"></div>

    <div style="margin-top:12px;font-size:11px;color:#aaa;line-height:1.6" data-i18n="map_note">
      Grey municipalities have no dynamic tariff available in 2026.
      457 out of 2,148 Swiss municipalities covered. Source: ElCom open data.
    </div>

  </div><!-- end map-view -->

  <!-- ── API view ──────────────────────────────────────────────────────────── -->
  <div id="api-view">

    <div class="api-view-header">
      <div class="api-view-title">API Reference</div>
      <div class="api-view-sub" data-i18n="api_view_subtitle"></div>
    </div>

    <!-- ── Availability status ────────────────────────────────────────────── -->
    <div class="api-section" style="padding:1rem 1.25rem">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <span style="font-size:12px;font-weight:600;color:#333" data-i18n="api_avail_title"></span>
        <span style="font-size:10px;color:#aaa" data-i18n="api_avail_sub"></span>
      </div>
      <div id="api-status-list" style="display:flex;flex-direction:column;gap:5px;max-height:220px;overflow-y:auto">
        <div style="font-size:12px;color:#bbb">loading...</div>
      </div>
    </div>

    <!-- ── API 1: /tariffs ─────────────────────────────────────────────────── -->
    <div class="api-section">
      <div class="api-section-header">
        <div class="api-section-title">
          <span class="api-method">GET</span>
          <span class="api-path">/api/v1/tariffs</span>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <button class="copy-btn" onclick="copyEndpoint(this,'/api/v1/tariffs')">Copy</button>
          <a href="/api/v1/tariffs" target="_blank" class="api-open-link">Open ↗</a>
        </div>
      </div>
      <div class="api-desc" data-i18n="api_tariffs_section_desc"></div>
      <div class="api-params-wrap">
        <div class="api-params-head" data-i18n="api_returns"></div>
        <table class="api-params-table">
          <tr><td>tariff_id</td><td>Unique identifier, e.g. <code>ckw_home_dynamic</code></td></tr>
          <tr><td>tariff_name</td><td>Human-readable tariff name</td></tr>
          <tr><td>provider_name</td><td>Energy utility company</td></tr>
          <tr><td>daily_update_time_utc</td><td>Time prices are refreshed each day (UTC)</td></tr>
          <tr><td>datetime_available_from_utc</td><td>First date with available data</td></tr>
          <tr><td>valid_until_utc</td><td><code>null</code> if the tariff is ongoing</td></tr>
        </table>
      </div>
      <div class="api-url-box">
        <span>/api/v1/tariffs</span>
      </div>
    </div>

    <!-- ── API 2: /prices ──────────────────────────────────────────────────── -->
    <div class="api-section">
      <div class="api-section-header">
        <div class="api-section-title">
          <span class="api-method">GET</span>
          <span class="api-path">/api/v1/prices</span>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <button class="copy-btn" id="api-copy-prices-btn" onclick="copyApiPricesEndpoint(this)">Copy</button>
          <a id="api-prices-open" href="/api/v1/prices" target="_blank" class="api-open-link">Open ↗</a>
        </div>
      </div>
      <div class="api-desc" data-i18n="api_prices_section_desc"></div>
      <div class="api-params-wrap">
        <div class="api-params-head" data-i18n="api_parameters"></div>
        <table class="api-params-table">
          <tr>
            <td>tariff_id <span class="api-badge-req">required</span></td>
            <td>Tariff identifier — choose a live tariff below</td>
          </tr>
          <tr>
            <td>start_time <span class="api-badge-req">required</span></td>
            <td>UTC datetime, e.g. <code>{today}T00:00:00Z</code></td>
          </tr>
          <tr>
            <td>end_time <span class="api-badge-opt">optional</span></td>
            <td>UTC datetime — defaults to <code>start_time + 24h</code></td>
          </tr>
        </table>
        <div class="api-params-head" style="margin-top:8px" data-i18n="api_returns"></div>
        <table class="api-params-table">
          <tr><td>energy_price_utc</td><td>list of <code>&#123;start_timestamp: price_chf&#125;</code></td></tr>
          <tr><td>grid_price_utc</td><td>list of <code>&#123;start_timestamp: price_chf&#125;</code></td></tr>
          <tr><td>residual_price_utc</td><td>list of <code>&#123;start_timestamp: price_chf&#125;</code></td></tr>
          <tr><td>slot_count</td><td>Number of 15-min slots (96 = full day)</td></tr>
        </table>
      </div>
      <div style="margin:12px 0 4px 0;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span id="api-live-tariff-label" style="font-size:11px;color:#555;font-weight:500" data-i18n="api_live_tariff"></span>
        <select id="api-tariff-select" class="api-tariff-select" onchange="updateApiPricesUrl()">
          <option value="" data-i18n-opt="api_no_live_data">— no live data yet —</option>
        </select>
      </div>
      <div class="api-url-box">
        <span id="api-prices-url" style="word-break:break-all">/api/v1/prices?tariff_id=...&amp;start_time={today}T00:00:00Z</span>
      </div>
    </div>

    <!-- ── /prices/all ─────────────────────────────────────────────────────── -->
    <div class="api-section">
      <div class="api-section-header">
        <div class="api-section-title">
          <span class="api-method">GET</span>
          <span class="api-path">/api/v1/prices/all</span>
          <span style="font-size:10px;padding:2px 7px;border-radius:4px;background:#fef3c7;color:#92400e;font-weight:500">all live tariffs</span>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <button class="copy-btn" onclick="copyEndpoint(this,'/api/v1/prices/all?start_time={today}T00:00:00Z')">Copy</button>
          <a href="/api/v1/prices/all?start_time={today}T00:00:00Z" target="_blank" class="api-open-link">Open ↗</a>
        </div>
      </div>
      <div class="api-desc" data-i18n="api_all_section_desc"></div>
      <div class="api-url-box">
        <span>/api/v1/prices/all?start_time={today}T00:00:00Z</span>
      </div>
    </div>

    <!-- ── Links ──────────────────────────────────────────────────────────── -->
    <div class="api-links-row">
      <a href="/docs"  target="_blank" class="api-open-link" style="border-color:#185fa5;color:#185fa5">Swagger UI ↗</a>
      <a href="/redoc" target="_blank" class="api-open-link" style="border-color:#185fa5;color:#185fa5">ReDoc ↗</a>
      <a href="/api/v1/health" target="_blank" class="api-open-link">Health ↗</a>
    </div>

  </div><!-- end api-view -->

</main>
<script>
// ── Translations ──────────────────────────────────────────────────────────────
const T = {{
  en: {{
    unit_explanation: 'Prices in Rp/kWh (Rappen per kilowatt-hour). 100 Rp = 1 CHF.',
    total_price: 'Total price per kWh',
    active_tariffs: 'Active tariffs',
    cheapest_hour: 'Cheapest slot',
    most_expensive_hour: 'Most expensive slot',
    viewing_day: 'Viewing',
    no_data_today: 'No price data available for this day',
    no_data_tariff: 'No data available for this tariff yet',
    loading_data: 'Loading prices\u2026',
    daily_profile: 'Daily price profile (Rp/kWh)',
    monthly_summary: 'Monthly summary',
    select_tariff: 'Select a tariff to view prices',
    selected_slot: 'Selected 15-min slot',
    vs_daily_avg: 'Compared to daily average',
    hover_chart: 'Hover over the chart to inspect a slot',
    fetch_status: 'Fetch status',
    api_tariffs_desc: 'List all available tariffs with metadata',
    api_prices_desc: 'Returns energy_price_utc, grid_price_utc, residual_price_utc \u2014 all in CHF/kWh, UTC timestamps, 15-min intervals',
    api_health_desc: 'System status and last fetch time per tariff',
    today: 'Today', completed: 'completed', live: '\u25cf live',
    days: 'days', slots: 'slots', avg: 'Avg', min: 'Min', max: 'Max',
    adapter_pending: 'Integration in progress — data coming soon',
    never_updated: 'Never updated', updated_at: 'Updated at',
    above_avg: 'above average', below_avg: 'below average', daily_avg: 'Daily avg',
    grid_tip: 'Network usage fee (dynamic, set by grid operator)',
    energy_tip: 'Electricity commodity price (spot market based)',
    residual_tip: 'Residual costs: SDL, Stromreserve, Netzzuschlag, levies',
    api_view_subtitle: 'REST \u00b7 UTC timestamps \u00b7 15-min intervals \u00b7 CHF/kWh \u00b7 as per T-Swiss spec',
    api_tariffs_section_desc: 'Lists all available tariffs with metadata. No parameters required — returns the full catalogue.',
    api_prices_section_desc: 'Returns timeseries of net prices per kWh — energy, grid and residual components — in 15-min UTC slots.',
    api_all_section_desc: 'All live tariffs in a single response, keyed by tariff_id. Fixed endpoint — always reflects current available data.',
    api_live_tariff: 'Live tariff:',
    api_no_live_data: '\u2014 no live data yet \u2014',
    api_returns: 'Returns',
    api_parameters: 'Parameters',
    api_avail_title: 'Data availability',
    api_avail_sub: 'Only live tariffs appear in the dropdown above',
    chart_total: 'Total', chart_vs_avg: 'vs avg',
    smart_best_times: 'Best times to use energy today',
    smart_avoid: 'Avoid \u2014 peak prices',
    smart_today_profile: 'Price profile today',
    smart_color_legend: 'green = cheap \u00b7 amber = average \u00b7 red = expensive',
    smart_compare: 'Tariff comparison \u2014 average price today',
    signal_green: 'Good time to use energy',
    signal_yellow: 'Average price period',
    signal_red: 'Expensive \u2014 avoid if possible',
    signal_gray: 'No data for current slot',
    vs_month_avg: 'vs. monthly average',
    tab_map: 'Map',
    map_title: 'Coverage map',
    map_subtitle: 'Dynamic tariff providers by Swiss municipality · source: ElCom 2026',
    map_filter_all: 'All providers',
    map_loading: 'Loading municipality boundaries…',
    map_no_tariff: 'No dynamic tariff',
    map_note: 'Grey municipalities have no dynamic tariff available in 2026. 457 out of 2,148 Swiss municipalities covered. Source: ElCom open data.',
  }},
  de: {{
    unit_explanation: 'Preise in Rp/kWh (Rappen pro Kilowattstunde). 100 Rp = 1 CHF.',
    total_price: 'Gesamtpreis pro kWh',
    active_tariffs: 'Aktive Tarife',
    cheapest_hour: 'G\u00fcnstigster Slot',
    most_expensive_hour: 'Teuerster Slot',
    viewing_day: 'Tag',
    no_data_today: 'Keine Preisdaten f\u00fcr diesen Tag verf\u00fcgbar',
    no_data_tariff: 'Noch keine Daten f\u00fcr diesen Tarif',
    loading_data: 'Preise werden geladen\u2026',
    daily_profile: 'Tagespreisprofil (Rp/kWh)',
    monthly_summary: 'Monatszusammenfassung',
    select_tariff: 'Tarif ausw\u00e4hlen',
    selected_slot: 'Ausgew\u00e4hlter 15-Min-Slot',
    vs_daily_avg: 'Im Vergleich zum Tagesdurchschnitt',
    hover_chart: 'Maus \u00fcber Grafik bewegen',
    fetch_status: 'Abrufstatus',
    api_tariffs_desc: 'Alle verf\u00fcgbaren Tarife mit Metadaten',
    api_prices_desc: 'Gibt energy_price_utc, grid_price_utc, residual_price_utc zur\u00fcck \u2014 alle in CHF/kWh, UTC-Zeitstempel, 15-Min-Intervalle',
    api_health_desc: 'Systemstatus und letzter Abrufzeitpunkt pro Tarif',
    today: 'Heute', completed: 'abgeschlossen', live: '\u25cf live',
    days: 'Tage', slots: 'Slots', avg: '\u00d8', min: 'Min', max: 'Max',
    adapter_pending: 'Integration in Arbeit \u2014 Daten folgen',
    never_updated: 'Nie aktualisiert', updated_at: 'Aktualisiert um',
    above_avg: '\u00fcber Durchschnitt', below_avg: 'unter Durchschnitt', daily_avg: 'Tagesdurchschnitt',
    grid_tip: 'Netznutzungsentgelt (dynamisch, vom Netzbetreiber festgelegt)',
    energy_tip: 'Energiepreis (basierend auf Spotmarkt)',
    residual_tip: 'Restkosten: SDL, Stromreserve, Netzzuschlag, Abgaben',
    smart_best_times: 'Beste Zeiten f\u00fcr Stromverbrauch heute',
    smart_avoid: 'Vermeiden \u2014 Spitzenpreise',
    smart_today_profile: 'Preisprofil heute',
    smart_color_legend: 'gr\u00fcn = g\u00fcnstig \u00b7 amber = durchschnittlich \u00b7 rot = teuer',
    smart_compare: 'Tarifvergleich \u2014 Durchschnittspreis heute',
    signal_green: 'Guter Zeitpunkt f\u00fcr Stromverbrauch',
    signal_yellow: 'Durchschnittlicher Preis',
    signal_red: 'Teuer \u2014 wenn m\u00f6glich vermeiden',
    signal_gray: 'Keine Daten f\u00fcr aktuellen Slot',
    vs_month_avg: 'vs. Monatsdurchschnitt',
    tab_map: 'Karte',
    map_title: 'Versorgungskarte',
    map_subtitle: 'Dynamische Tarifanbieter nach Schweizer Gemeinde · Quelle: ElCom 2026',
    map_filter_all: 'Alle Anbieter',
    map_loading: 'Gemeindegrenzen werden geladen…',
    map_no_tariff: 'Kein dynamischer Tarif',
    map_note: 'Graue Gemeinden verfügen im Jahr 2026 über keinen dynamischen Tarif. 457 von 2.148 Gemeinden abgedeckt. Quelle: ElCom Open Data.',
    api_view_subtitle: 'REST \u00b7 UTC-Zeitstempel \u00b7 15-Min-Intervalle \u00b7 CHF/kWh \u00b7 gem\u00e4\u00df T-Swiss-Spezifikation',
    api_tariffs_section_desc: 'Listet alle verf\u00fcgbaren Tarife mit Metadaten auf. Keine Parameter erforderlich.',
    api_prices_section_desc: 'Gibt Zeitreihen der Nettopreise pro kWh zur\u00fcck \u2014 Energie, Netz und Rest \u2014 in 15-Min-UTC-Slots.',
    api_all_section_desc: 'Alle aktiven Tarife in einer einzigen Antwort, sortiert nach tariff_id. Gibt immer aktuelle Daten zur\u00fcck.',
    api_live_tariff: 'Aktiver Tarif:',
    api_no_live_data: '\u2014 noch keine Live-Daten \u2014',
    api_returns: 'R\u00fcckgabe',
    api_parameters: 'Parameter',
    api_avail_title: 'Datenverfügbarkeit',
    api_avail_sub: 'Im Dropdown oben erscheinen nur aktive Tarife',
    chart_total: 'Total', chart_vs_avg: 'vs. Avg',
  }},
  fr: {{
    unit_explanation: 'Prix en Rp/kWh (Rappen par kilowattheure). 100 Rp = 1 CHF.',
    total_price: 'Prix total par kWh',
    active_tariffs: 'Tarifs actifs',
    cheapest_hour: 'Cr\u00e9neau le moins cher',
    most_expensive_hour: 'Cr\u00e9neau le plus cher',
    viewing_day: 'Jour',
    no_data_today: 'Aucune donn\u00e9e de prix pour ce jour',
    no_data_tariff: 'Aucune donn\u00e9e disponible pour ce tarif',
    loading_data: 'Chargement des prix\u2026',
    daily_profile: 'Profil de prix journalier (Rp/kWh)',
    monthly_summary: 'R\u00e9sum\u00e9 mensuel',
    select_tariff: 'S\u00e9lectionner un tarif',
    selected_slot: 'Cr\u00e9neau de 15 min s\u00e9lectionn\u00e9',
    vs_daily_avg: 'Par rapport \u00e0 la moyenne journ\u00e9ali\u00e8re',
    hover_chart: 'Survolez le graphique pour inspecter un cr\u00e9neau',
    fetch_status: 'Statut de collecte',
    api_tariffs_desc: 'Liste tous les tarifs disponibles avec m\u00e9tadonn\u00e9es',
    api_prices_desc: 'Retourne energy_price_utc, grid_price_utc, residual_price_utc \u2014 en CHF/kWh, horodatages UTC, intervalles 15 min',
    api_health_desc: '\u00c9tat du syst\u00e8me et derni\u00e8re collecte par tarif',
    today: "Aujourd\u2019hui", completed: 'compl\u00e9t\u00e9', live: '\u25cf en cours',
    days: 'jours', slots: 'cr\u00e9neaux', avg: 'Moy', min: 'Min', max: 'Max',
    adapter_pending: 'Int\u00e9gration en cours \u2014 donn\u00e9es bient\u00f4t disponibles',
    never_updated: 'Jamais mis \u00e0 jour', updated_at: 'Mis \u00e0 jour \u00e0',
    above_avg: 'au-dessus de la moyenne', below_avg: 'en dessous de la moyenne',
    daily_avg: 'Moyenne journ\u00e9ali\u00e8re',
    grid_tip: "Tarif d\u2019utilisation du r\u00e9seau (dynamique)",
    energy_tip: "Prix de l\u2019\u00e9lectricit\u00e9 (bas\u00e9 sur le march\u00e9 spot)",
    residual_tip: 'Co\u00fbts r\u00e9siduels: SDL, r\u00e9serve, surtaxe r\u00e9seau, taxes',
    smart_best_times: "Meilleurs cr\u00e9neaux pour consommer aujourd\u2019hui",
    smart_avoid: '\u00c9viter \u2014 prix de pointe',
    smart_today_profile: "Profil de prix aujourd\u2019hui",
    smart_color_legend: 'vert = bon march\u00e9 \u00b7 ambr\u00e9 = moyen \u00b7 rouge = cher',
    smart_compare: "Comparaison de tarifs \u2014 prix moyen aujourd\u2019hui",
    signal_green: "Bon moment pour consommer",
    signal_yellow: 'Prix moyen',
    signal_red: 'Cher \u2014 \u00e9viter si possible',
    signal_gray: 'Aucune donn\u00e9e pour le cr\u00e9neau actuel',
    vs_month_avg: 'vs. moyenne mensuelle',
    tab_map: 'Carte',
    map_title: 'Carte de couverture',
    map_subtitle: 'Fournisseurs de tarifs dynamiques par commune suisse · source : ElCom 2026',
    map_filter_all: 'Tous les fournisseurs',
    map_loading: 'Chargement des limites communales…',
    map_no_tariff: 'Pas de tarif dynamique',
    map_note: 'Les communes grises ne disposent d’aucun tarif dynamique en 2026. 457 communes sur 2 148 couvertes. Source : ElCom open data.',
    api_view_subtitle: 'REST \u00b7 horodatages UTC \u00b7 intervalles 15 min \u00b7 CHF/kWh \u00b7 conform\u00e9ment \u00e0 la sp\u00e9c T-Swiss',
    api_tariffs_section_desc: 'Liste tous les tarifs disponibles avec m\u00e9tadonn\u00e9es. Aucun param\u00e8tre requis.',
    api_prices_section_desc: 'Retourne les s\u00e9ries temporelles de prix nets par kWh \u2014 \u00e9nergie, r\u00e9seau et r\u00e9sidu \u2014 en cr\u00e9neaux UTC de 15 min.',
    api_all_section_desc: 'Tous les tarifs actifs en une seule r\u00e9ponse, index\u00e9s par tariff_id. Refl\u00e8te toujours les donn\u00e9es disponibles.',
    api_live_tariff: 'Tarif en direct\u00a0:',
    api_no_live_data: '\u2014 pas encore de donn\u00e9es \u2014',
    api_returns: 'R\u00e9ponse',
    api_parameters: 'Param\u00e8tres',
    api_avail_title: 'Disponibilit\u00e9 des donn\u00e9es',
    api_avail_sub: 'Seuls les tarifs actifs apparaissent dans la liste d\u00e9roulante',
    chart_total: 'Total', chart_vs_avg: 'vs moy',
  }},
  it: {{
    unit_explanation: 'Prezzi in Rp/kWh (Rappen per kilowattora). 100 Rp = 1 CHF.',
    total_price: 'Prezzo totale per kWh',
    active_tariffs: 'Tariffe attive',
    cheapest_hour: 'Fascia pi\u00f9 economica',
    most_expensive_hour: 'Fascia pi\u00f9 cara',
    viewing_day: 'Visualizzando',
    no_data_today: 'Nessun dato disponibile per questo giorno',
    no_data_tariff: 'Nessun dato ancora disponibile per questa tariffa',
    loading_data: 'Caricamento prezzi\u2026',
    daily_profile: 'Profilo prezzi giornaliero (Rp/kWh)',
    monthly_summary: 'Riepilogo mensile',
    select_tariff: 'Seleziona una tariffa per vedere i prezzi',
    selected_slot: 'Slot di 15 min selezionato',
    vs_daily_avg: 'Rispetto alla media giornaliera',
    hover_chart: 'Passa il mouse sul grafico per ispezionare uno slot',
    fetch_status: 'Stato aggiornamento',
    api_tariffs_desc: 'Elenca tutte le tariffe disponibili con metadati',
    api_prices_desc: 'Restituisce energy_price_utc, grid_price_utc, residual_price_utc \u2014 in CHF/kWh, timestamp UTC, intervalli 15 min',
    api_health_desc: 'Stato del sistema e ultimo aggiornamento per tariffa',
    today: 'Oggi', completed: 'completato', live: '\u25cf in corso',
    days: 'giorni', slots: 'slot', avg: 'Media', min: 'Min', max: 'Max',
    adapter_pending: 'Integrazione in corso \u2014 dati disponibili a breve',
    never_updated: 'Mai aggiornato', updated_at: 'Aggiornato alle',
    above_avg: 'sopra la media', below_avg: 'sotto la media', daily_avg: 'Media giornaliera',
    grid_tip: 'Tariffa di utilizzo della rete (dinamica, fissata dal gestore di rete)',
    energy_tip: "Prezzo dell\u2019energia elettrica (basato sul mercato spot)",
    residual_tip: 'Costi residui: SDL, Stromreserve, Netzzuschlag, tasse e oneri',
    smart_best_times: 'Migliori orari per consumare energia oggi',
    smart_avoid: 'Evita \u2014 prezzi di punta',
    smart_today_profile: 'Profilo prezzi oggi',
    smart_color_legend: 'verde = economico \u00b7 ambra = nella media \u00b7 rosso = caro',
    smart_compare: 'Confronto tariffe \u2014 prezzo medio oggi',
    signal_green: 'Buon momento per consumare',
    signal_yellow: 'Periodo a prezzo medio',
    signal_red: 'Caro \u2014 evita se possibile',
    signal_gray: 'Nessun dato per lo slot corrente',
    vs_month_avg: 'vs. media mensile',
    tab_map: 'Mappa',
    map_title: 'Mappa di copertura',
    map_subtitle: 'Fornitori di tariffe dinamiche per comune svizzero · fonte: ElCom 2026',
    map_filter_all: 'Tutti i fornitori',
    map_loading: 'Caricamento confini comunali…',
    map_no_tariff: 'Nessuna tariffa dinamica',
    map_note: 'I comuni grigi non hanno tariffe dinamiche nel 2026. 457 su 2.148 comuni svizzeri coperti. Fonte: ElCom open data.',
    api_view_subtitle: 'REST \u00b7 timestamp UTC \u00b7 intervalli 15 min \u00b7 CHF/kWh \u00b7 conforme alla spec T-Swiss',
    api_tariffs_section_desc: 'Elenca tutte le tariffe disponibili con metadati. Nessun parametro richiesto.',
    api_prices_section_desc: 'Restituisce le serie temporali dei prezzi netti per kWh \u2014 energia, rete e residuo \u2014 in slot UTC da 15 min.',
    api_all_section_desc: 'Tutte le tariffe attive in un\u2019unica risposta, indicizzate per tariff_id. Riflette sempre i dati disponibili.',
    api_live_tariff: 'Tariffa live:',
    api_no_live_data: '\u2014 nessun dato live ancora \u2014',
    api_returns: 'Risposta',
    api_parameters: 'Parametri',
    api_avail_title: 'Disponibilit\u00e0 dati',
    api_avail_sub: 'Solo le tariffe attive appaiono nel men\u00f9 a tendina',
    chart_total: 'Totale', chart_vs_avg: 'vs media',
  }}
}};

let lang = 'en';

function t(key) {{ return T[lang][key] || T.en[key] || key; }}
function locale() {{ return lang==='de'?'de-CH':lang==='fr'?'fr-CH':lang==='it'?'it-CH':'en-GB'; }}

function setLang(l) {{
  lang = l;
  document.querySelectorAll('.lang-btn').forEach(b => b.classList.toggle('active', b.textContent === l.toUpperCase()));
  document.querySelectorAll('[data-i18n]').forEach(el => {{ el.textContent = t(el.getAttribute('data-i18n')); }});
  document.querySelectorAll('[data-component]').forEach(el => {{
    el.setAttribute('data-tip', t(el.getAttribute('data-component') + '_tip'));
  }});
  if (activeTariff) {{ renderMonthGrid(lastMonthData); renderStatusList(lastHealthData); }}
  updateSlotPanel(lastSlotIdx);
  updateChartLocale();
  renderApiStatusList(lastHealthData);
  // update "no live data" placeholder if select is empty
  const sel = document.getElementById('api-tariff-select');
  if (sel && sel.options.length === 1 && !sel.options[0].value) {{
    sel.options[0].textContent = t('api_no_live_data');
  }}
  const badge = document.getElementById('header-badge');
  if (badge) badge.title = t('unit_explanation');
}}

// ── State ─────────────────────────────────────────────────────────────────────
let chart = null, activeTariff = null, activeDate = null, dailySummary = [];
let chartData = {{ grid:[], energy:[], residual:[], totals:[], labels:[], avg:0 }};
let lastMonthData = [], lastHealthData = [], lastSlotIdx = null;

function fmt(v) {{ return v.toFixed(2) + ' Rp'; }}
function fmtShort(v) {{ return v.toFixed(1) + ' Rp'; }}

function extractSeries(seriesData) {{
  if (!seriesData?.length) return {{ labels: [], values: [] }};
  return {{
    labels: seriesData.map(s => {{
      const d = new Date(Object.keys(s)[0]);
      const local = d.toLocaleTimeString('en-GB', {{hour:'2-digit', minute:'2-digit', timeZone:'Europe/Zurich'}});
      const utc   = d.toLocaleTimeString('en-GB', {{hour:'2-digit', minute:'2-digit', timeZone:'UTC'}});
      return local + ' (' + utc + ' UTC)';
    }}),
    values: seriesData.map(s => +(Object.values(s)[0] * 100).toFixed(2))
  }};
}}

// ── Health ────────────────────────────────────────────────────────────────────
async function loadHealth() {{
  const r = await fetch('/api/v1/health');
  if (!r.ok) return;
  const data = await r.json();
  lastHealthData = data.tariffs || [];
  const okCount = lastHealthData.filter(t => t.has_today_data).length;
  const badge = document.getElementById('header-badge');
  badge.textContent = okCount + '/' + lastHealthData.length + ' with data';
  badge.style.background = okCount === (data.adapters_ready||0) && okCount > 0 ? '#1d9e75' : okCount > 0 ? '#ba7517' : '#555';
  document.getElementById('header-time').textContent = new Date().toLocaleTimeString('en-GB') + ' UTC';
  document.getElementById('m-total').textContent = lastHealthData.length;
  document.getElementById('m-total-sub').textContent = okCount + ' with prices today';
  renderStatusList(lastHealthData);
  populateApiTariffSelect();
}}

function renderStatusList(tariffs) {{
  document.getElementById('status-list').innerHTML = (tariffs||[]).map(t => {{
    const pendingMsg = lang==='de'?'Adapter noch nicht implementiert':lang==='fr'?'Adaptateur non impl\u00e9ment\u00e9':lang==='it'?'Adattatore non ancora implementato':'Adapter not yet implemented';
    const updatedPrefix = lang==='de'?'Aktualisiert ':lang==='fr'?'Mis \u00e0 jour \u00e0 ':lang==='it'?'Aggiornato alle ':'Updated ';
    const neverMsg = lang==='de'?'Nie aktualisiert':lang==='fr'?'Jamais mis \u00e0 jour':lang==='it'?'Mai aggiornato':'Never updated';
    const _fd = t.last_fetch_utc ? new Date(t.last_fetch_utc) : null;
    const _fmeta = _fd ? updatedPrefix + _fd.toLocaleDateString('en-GB') + ' ' + _fd.toLocaleTimeString('en-GB') + ' UTC' : neverMsg;
    const meta = t.status === 'pending' && !t.last_fetch_utc ? pendingMsg : _fmeta;
    return `<div class="status-row">
      <div>
        <div class="status-name">${{t.provider_name}} \u2014 ${{t.tariff_name||''}}</div>
        <div class="status-meta">${{meta}}</div>
      </div>
      <span class="${{t.status==='ok'?'s-ok':t.status==='pending'?'s-warn':'s-miss'}}">${{t.status}}</span>
    </div>`;
  }}).join('');
}}

// ── Tariffs ───────────────────────────────────────────────────────────────────
async function loadTariffs() {{
  const r = await fetch('/api/v1/tariffs');
  if (!r.ok) return [];
  const tariffs = await r.json();
  const sel = document.getElementById('tariff-select');
  const placeholder = lang==='de'?'\u2014 Tarif ausw\u00e4hlen \u2014':lang==='fr'?'\u2014 S\u00e9lectionner \u2014':lang==='it'?'\u2014 Seleziona tariffa \u2014':'\u2014 Select tariff \u2014';
  sel.innerHTML = `<option value="">${{placeholder}}</option>` +
    tariffs.map(t => `<option value="${{t.tariff_id}}">${{t.provider_name}} \u2014 ${{t.tariff_name}}</option>`).join('');
  if (tariffs.length) sel.value = tariffs[0].tariff_id;
  return tariffs;
}}

async function onTariffChange() {{
  const tariffId = document.getElementById('tariff-select').value;
  if (!tariffId) return;
  activeTariff = tariffId;
  activeDate = null;
  updatePricesExample(tariffId);
  if (chart) {{ chart.destroy(); chart = null; }}
  const canvas = document.getElementById('priceChart');
  const placeholder = document.getElementById('chart-placeholder');
  canvas.style.display = 'none';
  placeholder.style.display = 'flex';
  placeholder.innerHTML = `<div style="color:#bbb;font-size:13px">${{t('loading_data')}}</div>`;
  document.getElementById('series-toggles').style.display = 'none';
  document.getElementById('slot-panel').style.display = 'none';
  document.getElementById('slot-panel-empty').style.display = 'block';
  document.getElementById('day-btns').innerHTML = '';
  ['m-min','m-max','m-date'].forEach(id => document.getElementById(id).textContent = '\u2014');
  ['m-min-sub','m-max-sub','m-slots'].forEach(id => document.getElementById(id).textContent = '');
  await Promise.all([loadDaySummary(tariffId), loadMonthly(tariffId)]);
}}

// ── Day summary buttons ───────────────────────────────────────────────────────
async function loadDaySummary(tariffId) {{
  const r = await fetch(`/api/v1/summary/daily?tariff_id=${{tariffId}}&days=5`);
  if (!r.ok) {{
    document.getElementById('day-btns').innerHTML = '';
    document.getElementById('chart-placeholder').style.display = 'flex';
    document.getElementById('chart-placeholder').innerHTML = `
      <div style="text-align:center">
        <svg width="220" height="80" viewBox="0 0 220 80" style="opacity:.1;display:block;margin:0 auto 12px">
          <polyline points="0,70 20,55 40,60 60,30 80,45 100,20 120,35 140,50 160,25 180,40 200,30 220,45"
            fill="none" stroke="#888" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <div style="font-size:14px;font-weight:500;color:#999;margin-bottom:4px">${{t('no_data_tariff')}}</div>
      </div>`;
    document.getElementById('priceChart').style.display = 'none';
    return;
  }}
  dailySummary = await r.json();
  document.getElementById('day-btns').innerHTML = dailySummary.map((d, i) => {{
    const isToday = d.date === '{today}';
    const label = isToday ? t('today') : new Date(d.date+'T12:00:00Z').toLocaleDateString(locale(),{{weekday:'short',day:'numeric',month:'short'}});
    return `<button class="day-btn${{i===dailySummary.length-1?' active':''}}" onclick="selectDay('${{d.date}}',this)">${{label}}</button>`;
  }}).join('');
  if (dailySummary.length) await loadDayChart(tariffId, dailySummary[dailySummary.length-1].date);
}}

async function selectDay(dateStr, btn) {{
  document.querySelectorAll('.day-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  await loadDayChart(activeTariff, dateStr);
}}

// ── Slot panel ────────────────────────────────────────────────────────────────
function updateSlotPanel(idx) {{
  lastSlotIdx = idx;
  const panel = document.getElementById('slot-panel');
  const empty = document.getElementById('slot-panel-empty');
  if (idx === null || idx < 0 || idx >= chartData.totals.length) {{
    panel.style.display = 'none'; empty.style.display = 'block'; empty.textContent = t('hover_chart'); return;
  }}
  panel.style.display = 'block'; empty.style.display = 'none';
  const total = chartData.totals[idx], g = chartData.grid[idx]||0, e = chartData.energy[idx]||0;
  const res = chartData.residual[idx]||0, avg = chartData.avg, label = chartData.labels[idx]||'';
  const ratio = avg > 0 ? total / avg : 1;
  const totalColor = ratio < 0.85 ? '#1d9e75' : ratio > 1.15 ? '#e24b4a' : '#185fa5';
  document.getElementById('sp-total').textContent = fmt(total);
  document.getElementById('sp-total').style.color = totalColor;
  document.getElementById('sp-time').textContent = label + '  \u00b7  15 min';
  document.getElementById('sp-grid').textContent = g > 0 ? fmt(g) : '\u2014';
  document.getElementById('sp-energy').textContent = e > 0 ? fmt(e) : '\u2014';
  document.getElementById('sp-residual').textContent = res > 0 ? fmt(res) : '\u2014';
  const allPos = chartData.totals.filter(v => v > 0);
  const pct = Math.max(...chartData.totals) > Math.min(...allPos||[0])
    ? Math.round((total - Math.min(...allPos)) / (Math.max(...chartData.totals) - Math.min(...allPos)) * 100) : 50;
  const barClr = ratio < 0.85 ? '#1d9e75' : ratio > 1.15 ? '#e24b4a' : '#378add';
  document.getElementById('sp-bar').style.width = pct + '%';
  document.getElementById('sp-bar').style.background = barClr;
  const diff = total - avg;
  const vsEl = document.getElementById('sp-vs');
  vsEl.textContent = (diff >= 0 ? '+' : '') + diff.toFixed(2) + ' Rp';
  vsEl.style.color = diff <= 0 ? '#1d9e75' : '#e24b4a';
  document.getElementById('sp-avg-ref').textContent =
    t('daily_avg') + ': ' + avg.toFixed(2) + ' Rp/kWh  \u00b7  ' +
    (diff <= 0 ? diff.toFixed(2) + ' Rp ' + t('below_avg') : '+' + diff.toFixed(2) + ' Rp ' + t('above_avg'));
}}

// ── Series toggles ───────────────────────────────────────────────────────────
const SERIES_META = [
  {{ key:'grid',     label:'Grid',     color:'#185fa5', border:'#185fa5', bg:'#e6f0fb' }},
  {{ key:'energy',   label:'Energy',   color:'#1d9e75', border:'#1d9e75', bg:'#e6f7f0' }},
  {{ key:'residual', label:'Residual', color:'#ba7517', border:'#ba7517', bg:'#fef3e0' }},
];
let seriesVisible = {{ grid:true, energy:true, residual:true }};

function buildSeriesToggles(availableSeries) {{
  const wrap = document.getElementById('series-toggles');
  if (!availableSeries.length) {{ wrap.style.display = 'none'; return; }}
  wrap.style.display = 'flex';
  wrap.innerHTML = availableSeries.map(key => {{
    const m = SERIES_META.find(s => s.key === key); if (!m) return '';
    const on = seriesVisible[key];
    return `<span class="series-toggle${{on?'':' off'}}" style="background:${{on?m.bg:'white'}};border-color:${{m.border}};color:${{m.color}}"
      onclick="toggleSeries('${{key}}')" id="toggle-${{key}}">
      <span class="chk">${{on?'\u2713':''}}</span> ${{m.label}}</span>`;
  }}).join('');
}}

function toggleSeries(key) {{
  seriesVisible[key] = !seriesVisible[key];
  const m = SERIES_META.find(s => s.key === key);
  const btn = document.getElementById('toggle-' + key); const on = seriesVisible[key];
  btn.classList.toggle('off', !on); btn.style.background = on ? m.bg : 'white';
  btn.querySelector('.chk').textContent = on ? '\u2713' : '';
  if (!chart) return;
  chart.data.datasets.forEach((ds, i) => {{ if (ds._seriesKey === key) chart.setDatasetVisibility(i, on); }});
  chart.update(); rebuildTotals();
}}

function rebuildTotals() {{
  const totals = Array.from({{length:chartData.labels.length}}, (_,i) =>
    (seriesVisible.grid?chartData.grid[i]||0:0)+(seriesVisible.energy?chartData.energy[i]||0:0)+(seriesVisible.residual?chartData.residual[i]||0:0));
  const pos = totals.filter(v => v > 0);
  chartData.totals = totals; chartData.avg = pos.length ? pos.reduce((a,b)=>a+b,0)/pos.length : 0;
}}

// ── Chart ─────────────────────────────────────────────────────────────────────
async function loadDayChart(tariffId, dateStr) {{
  activeDate = dateStr;
  const placeholder = document.getElementById('chart-placeholder');
  const canvas = document.getElementById('priceChart');
  const r = await fetch(`/api/v1/prices?tariff_id=${{tariffId}}&start_time=${{dateStr}}T00:00:00Z`);
  if (!r.ok) {{
    canvas.style.display = 'none'; placeholder.style.display = 'flex';
    placeholder.innerHTML = `<div style="text-align:center">
      <svg width="220" height="80" viewBox="0 0 220 80" style="opacity:.12;display:block;margin:0 auto 12px">
        <polyline points="0,70 20,55 40,60 60,30 80,45 100,20 120,35 140,50 160,25 180,40 200,30 220,45"
          fill="none" stroke="#888" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <div style="font-size:14px;font-weight:500;color:#999;margin-bottom:4px">${{t('no_data_today')}}</div>
      <div style="font-size:11px;color:#bbb">${{dateStr}}</div></div>`;
    document.getElementById('slot-panel').style.display = 'none';
    document.getElementById('slot-panel-empty').textContent = t('hover_chart');
    document.getElementById('slot-panel-empty').style.display = 'block';
    document.getElementById('series-toggles').style.display = 'none';
    ['m-min','m-max','m-date'].forEach(id => document.getElementById(id).textContent = '\u2014');
    ['m-min-sub','m-max-sub','m-slots'].forEach(id => document.getElementById(id).textContent = '');
    return;
  }}
  const data = await r.json();
  const grid = extractSeries(data.grid_price_utc);
  const energy = extractSeries(data.energy_price_utc);
  const residual = extractSeries(data.residual_price_utc);
  const base = grid.labels.length ? grid : energy;
  const labels = base.labels; const nSlots = labels.length;
  const totals = Array.from({{length:nSlots}},(_,i)=>(grid.values[i]||0)+(energy.values[i]||0)+(residual.values[i]||0));
  const pos = totals.filter(v=>v>0);
  const avg = pos.length ? pos.reduce((a,b)=>a+b,0)/pos.length : 0;
  chartData = {{ grid:grid.values, energy:energy.values, residual:residual.values, totals, labels, avg }};

  if (totals.length) {{
    const minVal = pos.length ? Math.min(...pos) : 0; const maxVal = Math.max(...totals);
    const isToday = dateStr === '{today}';
    const todaySuffix = ' \u00b7 ' + t('today').toLowerCase();
    document.getElementById('m-min-label').textContent = isToday ? t('cheapest_hour') + todaySuffix : t('cheapest_hour');
    document.getElementById('m-max-label').textContent = isToday ? t('most_expensive_hour') + todaySuffix : t('most_expensive_hour');
    document.getElementById('m-min').textContent = fmtShort(minVal);
    document.getElementById('m-min-sub').textContent = labels[totals.indexOf(minVal)];
    document.getElementById('m-max').textContent = fmtShort(maxVal);
    document.getElementById('m-max-sub').textContent = labels[totals.indexOf(maxVal)];
    document.getElementById('m-date').textContent =
      new Date(dateStr+'T12:00:00Z').toLocaleDateString(locale(),{{day:'numeric',month:'long',year:'numeric'}});
    document.getElementById('m-slots').textContent = nSlots + ' \u00d7 15 min ' + t('slots');
  }}
  document.getElementById('chart-title').textContent =
    t('daily_profile') + ' \u2014 ' + new Date(dateStr+'T12:00:00Z').toLocaleDateString(locale(),{{weekday:'long',day:'numeric',month:'long'}});

  placeholder.style.display = 'none'; canvas.style.display = 'block';
  const availableSeries = []; const datasets = [];
  if (grid.values.length) {{ availableSeries.push('grid'); datasets.push({{
    _seriesKey:'grid',label:'Grid',data:grid.values,borderColor:'#185fa5',backgroundColor:'rgba(24,95,165,0.06)',
    borderWidth:1.5,pointRadius:0,pointHoverRadius:5,pointHoverBackgroundColor:'#185fa5',
    pointHoverBorderColor:'#fff',pointHoverBorderWidth:2,fill:true,tension:0.3,hidden:!seriesVisible.grid}}); }}
  if (energy.values.length) {{ availableSeries.push('energy'); datasets.push({{
    _seriesKey:'energy',label:'Energy',data:energy.values,borderColor:'#1d9e75',backgroundColor:'rgba(29,158,117,0.04)',
    borderWidth:1.5,pointRadius:0,pointHoverRadius:5,pointHoverBackgroundColor:'#1d9e75',
    pointHoverBorderColor:'#fff',pointHoverBorderWidth:2,fill:false,tension:0.3,hidden:!seriesVisible.energy}}); }}
  if (residual.values.length) {{ availableSeries.push('residual'); datasets.push({{
    _seriesKey:'residual',label:'Residual',data:residual.values,borderColor:'#ba7517',borderWidth:1,
    pointRadius:0,pointHoverRadius:4,pointHoverBackgroundColor:'#ba7517',fill:false,tension:0.3,
    borderDash:[4,3],hidden:!seriesVisible.residual}}); }}
  buildSeriesToggles(availableSeries);
  if (chart) chart.destroy();
  chart = new Chart(canvas, {{
    type:'line', data:{{ labels, datasets }},
    options:{{
      responsive:true,maintainAspectRatio:false,animation:false,normalized:true,
      interaction:{{mode:'index',intersect:false}},
      plugins:{{
        legend:{{display:false}},
        tooltip:{{
          enabled:true,mode:'index',intersect:false,
          backgroundColor:'rgba(26,26,46,0.95)',padding:10,cornerRadius:8,
          callbacks:{{
            title: items => items?.[0]?.label ?? '',
            label: ctx => ` ${{ctx.dataset.label}}: ${{ctx.parsed.y.toFixed(2)}} Rp/kWh`,
            afterBody: items => {{
              if (!items.length) return [];
              const tot = chartData.totals[items[0].dataIndex]; const diff = tot-avg;
              return ['',` ${{t('chart_total')}}: ${{tot.toFixed(2)}} Rp/kWh`,` ${{t('chart_vs_avg')}}: ${{diff>=0?'+':''}}${{diff.toFixed(2)}} Rp`];
            }}
          }}
        }}
      }},
      elements:{{point:{{radius:0,hoverRadius:5,hitRadius:15}},line:{{borderJoinStyle:'round'}}}},
      scales:{{
        x:{{grid:{{display:false}},ticks:{{maxTicksLimit:8,font:{{size:10}}}}}},
        y:{{grace:'5%',beginAtZero:false,ticks:{{callback:v=>v+' Rp',font:{{size:10}}}}}}
      }}
    }}
  }});
  canvas.onmousemove = e => {{ const pts = chart.getElementsAtEventForMode(e,'index',{{intersect:false}},true); if (pts.length) updateSlotPanel(pts[0].index); }};
  canvas.onmouseleave = () => updateSlotPanel(null);
}}

// ── Monthly ───────────────────────────────────────────────────────────────────
async function loadMonthly(tariffId) {{
  const r = await fetch(`/api/v1/summary/monthly?tariff_id=${{tariffId}}&months=3`);
  const grid = document.getElementById('month-grid');
  if (!r.ok) {{ grid.innerHTML = `<div style="color:#aaa;font-size:12px">${{t('select_tariff')}}</div>`; return; }}
  lastMonthData = await r.json(); renderMonthGrid(lastMonthData);
}}

function renderMonthGrid(months) {{
  const grid = document.getElementById('month-grid');
  if (!months?.length) {{ grid.innerHTML = `<div style="color:#aaa;font-size:12px">${{t('select_tariff')}}</div>`; return; }}
  grid.innerHTML = [...months].reverse().map(m => `
    <div class="month-card${{m.is_current?' current':''}}">
      <div class="month-label">
        ${{new Date(m.month+'-01').toLocaleDateString(locale(),{{month:'long',year:'numeric'}})}}
        ${{m.is_current?`<span class="month-tag live">${{t('live')}}</span>`:`<span class="month-tag">${{t('completed')}}</span>`}}
      </div>
      <div class="month-stats">
        <div class="mstat"><div class="mv avg">${{fmtShort(m.avg_rp)}}</div><div class="ml">${{t('avg')}}</div><div class="mt">\u2014</div></div>
        <div class="mstat"><div class="mv min">${{fmtShort(m.min_rp)}}</div><div class="ml">${{t('min')}}</div><div class="mt">${{m.min_date}}</div></div>
        <div class="mstat"><div class="mv max">${{fmtShort(m.max_rp)}}</div><div class="ml">${{t('max')}}</div><div class="mt">${{m.max_date}}</div></div>
      </div>
      <div class="month-meta">${{m.days_with_data}} ${{t('days')}} \u00b7 ${{m.slot_count}} ${{t('slots')}}</div>
    </div>`).join('');
}}

// ── Map view ──────────────────────────────────────────────────────────────────
let mapLoaded = false, activeFilter = 'all';

const MAP_BFS = {{"5009":{{"id":"aem","label":"AEM – Tariffa dinamica","color":"#3498db"}},"5196":{{"id":"aem","label":"AEM – Tariffa dinamica","color":"#3498db"}},"5226":{{"id":"aem","label":"AEM – Tariffa dinamica","color":"#3498db"}},"2422":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2491":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2493":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2495":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2497":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2499":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2500":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2501":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2502":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2572":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2573":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2582":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2583":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2584":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2585":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"2586":{{"id":"avag","label":"AVAG – Primeo NetzDynamisch","color":"#e74c3c"}},"5141":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5143":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5144":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5148":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5151":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5154":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5160":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5161":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5162":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5167":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5171":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5176":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5180":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5186":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5187":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5189":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5192":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5193":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5194":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5198":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5199":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5203":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5205":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5206":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5208":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5210":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5212":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5214":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5216":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5221":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5225":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5227":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5230":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5231":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5233":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5236":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5237":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5238":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5239":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5240":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5249":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5251":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5260":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5263":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"5269":{{"id":"ail","label":"AIL – Tariffa Dinamica","color":"#9b59b6"}},"1001":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1002":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1004":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1005":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1007":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1008":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1009":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1010":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1021":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1023":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1024":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1025":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1026":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1030":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1032":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1033":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1037":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1039":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1040":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1041":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1051":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1052":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1053":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1054":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1055":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1058":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1059":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1061":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1063":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1064":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1065":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1067":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1081":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1082":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1083":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1084":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1085":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1086":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1088":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1089":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1091":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1093":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1094":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1095":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1097":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1098":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1099":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1100":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1102":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1103":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1104":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1107":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1121":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1122":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1123":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1125":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1127":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1128":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1129":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1131":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1136":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1137":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1139":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1140":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1142":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1143":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1146":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1147":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1150":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1151":{{"id":"ckw","label":"CKW – Home/Business dynamic","color":"#e67e22"}},"1301":{{"id":"ekze","label":"EKZ Einsiedeln – Dynamisch","color":"#1abc9c"}},"2576":{{"id":"elag","label":"ELAG – Primeo NetzDynamisch","color":"#d35400"}},"4239":{{"id":"ega","label":"EGA – Netz dynamisch","color":"#f39c12"}},"1":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"2":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"3":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"4":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"5":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"6":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"7":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"8":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"9":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"10":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"11":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"12":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"13":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"14":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"23":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"24":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"25":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"26":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"27":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"28":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"29":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"31":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"33":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"34":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"37":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"38":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"39":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"40":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"41":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"43":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"51":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"52":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"53":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"55":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"56":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"57":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"58":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"59":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"60":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"61":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"63":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"64":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"65":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"67":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"68":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"70":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"71":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"72":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"81":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"82":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"83":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"84":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"85":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"86":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"87":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"88":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"89":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"90":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"91":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"92":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"93":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"95":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"96":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"98":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"99":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"100":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"101":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"111":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"112":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"113":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"114":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"115":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"117":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"119":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"131":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"135":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"136":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"137":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"138":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"139":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"141":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"153":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"157":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"160":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"173":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"178":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"180":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"181":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"182":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"192":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"194":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"195":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"196":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"197":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"199":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"200":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"211":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"213":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"214":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"215":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"216":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"218":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"219":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"220":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"221":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"223":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"224":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"225":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"226":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"227":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"228":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"231":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"241":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"242":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"243":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"244":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"245":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"246":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"247":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"248":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"249":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"250":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"251":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"291":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"292":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"293":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"294":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"295":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"296":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"297":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"298":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"1704":{{"id":"ekz","label":"EKZ – Energie Dynamisch+400D","color":"#27ae60"}},"669":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2008":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2011":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2022":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2025":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2029":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2035":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2041":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2043":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2044":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2045":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2050":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2051":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2053":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2054":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2055":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2063":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2067":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2068":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2079":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2086":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2087":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2096":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2097":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2099":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2102":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2113":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2114":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2115":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2117":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2121":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2122":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2124":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2125":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2134":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2135":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2137":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2138":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2140":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2145":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2147":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2149":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2152":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2153":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2155":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2160":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2162":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2173":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2174":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2175":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2177":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2183":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2186":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2194":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2196":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2197":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2198":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2206":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2208":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2211":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2216":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2220":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2226":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2228":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2230":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2233":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2234":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2235":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2236":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2237":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2238":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2239":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2250":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2254":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2257":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2258":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2261":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2262":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2265":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2266":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2272":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2275":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2276":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2284":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2292":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2293":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2294":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2295":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2296":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2299":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2300":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2301":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2303":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2304":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2305":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2306":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2307":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2308":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2309":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2321":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2323":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2325":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2328":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2333":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2335":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2336":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2337":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2338":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"5451":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"5456":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"5458":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"5464":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"5790":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"5813":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"5816":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"5817":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"5821":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"5822":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"5827":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"5841":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"5842":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"5843":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6413":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6416":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6417":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6423":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6432":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6433":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6434":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6435":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6437":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6451":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6452":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6456":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6458":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6487":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6504":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6511":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6512":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"6513":{{"id":"groupe","label":"Groupe E – VARIO","color":"#2980b9"}},"2471":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2472":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2473":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2474":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2475":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2476":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2477":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2478":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2479":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2480":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2481":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2611":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2612":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2613":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2614":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2615":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2616":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2617":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2618":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2619":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2620":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2621":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2622":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2761":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2762":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2763":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2764":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2765":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2766":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2767":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2768":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2769":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2770":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2771":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2772":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2773":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2774":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2775":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2782":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2783":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2785":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2786":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2787":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2788":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2830":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2831":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2883":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}},"2889":{{"id":"primeo","label":"Primeo – NetzDynamisch","color":"#8e44ad"}}}};

const MAP_PROVIDERS = [
  {{id:'ekz',    label:'EKZ – Energie Dynamisch+400D',  color:'#27ae60'}},
  {{id:'groupe', label:'Groupe E – VARIO',               color:'#2980b9'}},
  {{id:'ckw',    label:'CKW – Home/Business dynamic',   color:'#e67e22'}},
  {{id:'primeo', label:'Primeo – NetzDynamisch',         color:'#8e44ad'}},
  {{id:'ail',    label:'AIL – Tariffa Dinamica',         color:'#9b59b6'}},
  {{id:'avag',   label:'AVAG – Primeo NetzDynamisch',    color:'#e74c3c'}},
  {{id:'aem',    label:'AEM – Tariffa dinamica',         color:'#3498db'}},
  {{id:'ekze',   label:'EKZ Einsiedeln – Dynamisch',    color:'#1abc9c'}},
  {{id:'ega',    label:'EGA – Netz dynamisch',           color:'#f39c12'}},
  {{id:'elag',   label:'ELAG – Primeo NetzDynamisch',    color:'#d35400'}},
];

let mapPaths = [];

function buildMapLegend() {{
  const leg = document.getElementById('map-legend');
  if (!leg) return;
  leg.innerHTML = MAP_PROVIDERS.map(p =>
    `<div class="map-legend-item"><div class="map-legend-dot" style="background:${{p.color}}"></div>${{p.label}}</div>`
  ).join('') +
  `<div class="map-legend-item"><div class="map-legend-dot" style="background:#cccccc"></div>${{t('map_no_tariff')}}</div>`;
}}

function filterMap(filterId) {{
  activeFilter = filterId;
  document.querySelectorAll('.map-filter-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.filter === filterId)
  );
  mapPaths.forEach(path => {{
    const bfsId = path.dataset.bfs;
    const info  = MAP_BFS[bfsId];
    if (filterId === 'all') {{
      path.style.opacity = '1';
      path.setAttribute('fill', info ? info.color : '#cccccc');
    }} else if (info && info.id === filterId) {{
      path.style.opacity = '1';
      path.setAttribute('fill', info.color);
    }} else {{
      path.style.opacity = '0.12';
      path.setAttribute('fill', info ? info.color : '#cccccc');
    }}
  }});
}}

async function loadMap() {{
  if (!mapLoaded) {{ mapLoaded = true; }} else {{ return; }}
  buildMapLegend();
  const loadingEl = document.getElementById('map-loading');
  const svgEl     = document.getElementById('map-svg');
  const tooltip   = document.getElementById('map-tooltip');
  const container = document.getElementById('map-svg-container');
  function loadScript(src) {{
    return new Promise((res, rej) => {{
      if (document.querySelector(`script[src="${{src}}"]`)) {{ res(); return; }}
      const s = document.createElement('script');
      s.src = src; s.onload = res; s.onerror = rej;
      document.head.appendChild(s);
    }});
  }}
  try {{
    await loadScript('https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js');
    await loadScript('https://cdn.jsdelivr.net/npm/topojson-client@3/dist/topojson-client.min.js');
    const topo = await fetch('https://unpkg.com/swiss-maps@4/2021/ch-combined.json').then(r => r.json());
    loadingEl.style.display = 'none';
    const muni = topojson.feature(topo, topo.objects.municipalities);
    const W = container.clientWidth || 800;
    const H = Math.round(W * 0.56);
    svgEl.setAttribute('height', H);
    svgEl.style.height = H + 'px';
    const proj = d3.geoIdentity().reflectY(true).fitSize([W, H], muni);
    const path = d3.geoPath().projection(proj);
    const svg  = d3.select('#map-svg');
    svg.selectAll('path')
      .data(muni.features)
      .enter().append('path')
      .attr('d', path)
      .attr('fill', d => {{
        const id = String(d.id || d.properties?.id || '');
        return MAP_BFS[id] ? MAP_BFS[id].color : '#cccccc';
      }})
      .attr('stroke', '#fff')
      .attr('stroke-width', 0.5)
      .attr('cursor', 'pointer')
      .attr('data-bfs', d => String(d.id || d.properties?.id || ''))
      .on('mousemove', (ev, d) => {{
        const id   = String(d.id || d.properties?.id || '');
        const info = MAP_BFS[id];
        const name = d.properties?.NAME || d.properties?.name || d.properties?.GEM_NAME || id;
        tooltip.style.display = 'block';
        tooltip.style.left    = (ev.offsetX + 14) + 'px';
        tooltip.style.top     = (ev.offsetY - 10) + 'px';
        tooltip.innerHTML = info
          ? `<strong style="color:#1a1a2e">${{name}}</strong><br><span style="color:${{info.color}}">&#9679;</span> <span style="color:#555">${{info.label}}</span>`
          : `<strong style="color:#1a1a2e">${{name}}</strong><br><span style="color:#aaa;font-size:11px">${{t('map_no_tariff')}}</span>`;
      }})
      .on('mouseleave', () => {{ tooltip.style.display = 'none'; }});
    mapPaths = Array.from(svgEl.querySelectorAll('path'));
  }} catch(e) {{
    loadingEl.textContent = 'Could not load map — check network connection.';
    console.error('Map load error:', e);
  }}
}}

// ── View switching ────────────────────────────────────────────────────────────
let currentView = 'data', smartData = null, activeSmartTariff = null;

function switchView(view) {{
  currentView = view;
  document.getElementById('consumer-view').style.display  = view==='smart'?'block':'none';
  document.getElementById('developer-view').style.display = view==='data'?'block':'none';
  document.getElementById('map-view').style.display       = view==='map'?'block':'none';
  document.getElementById('api-view').style.display       = view==='api'?'block':'none';
  document.getElementById('tab-smart').classList.toggle('active', view==='smart');
  document.getElementById('tab-data').classList.toggle('active',  view==='data');
  document.getElementById('tab-map').classList.toggle('active',   view==='map');
  document.getElementById('tab-api').classList.toggle('active',   view==='api');
  if (view==='smart' && !smartData) loadSmart();
  if (view==='map'   && !mapLoaded) loadMap();
}}

// ── Consumer view ─────────────────────────────────────────────────────────────
async function loadSmart(dateStr) {{
  const r = await fetch(`/api/v1/smart?date_str=${{dateStr||'{today}'}}`);
  if (!r.ok) return;
  smartData = await r.json();
  const withData    = smartData.tariffs.filter(t => t.has_data);
  const withoutData = smartData.tariffs.filter(t => !t.has_data && t.adapter_ready);
  document.getElementById('smart-tariff-tabs').innerHTML =
    withData.map((t,i) =>
      `<button class="tariff-tab${{i===0?' active':''}}" onclick="selectSmartTariff('${{t.tariff_id}}',this)">
        ${{t.provider_name}} \u2014 ${{t.tariff_name}}</button>`).join('') +
    withoutData.map(t =>
      `<button class="tariff-tab" style="opacity:.4;cursor:default">${{t.provider_name}} \u2014 ${{t.tariff_name}}</button>`).join('');
  if (withData.length) {{ activeSmartTariff = withData[0].tariff_id; renderSmartTariff(withData[0]); }}
  renderSmartCompare(smartData.tariffs);
}}

function selectSmartTariff(tariffId, btn) {{
  document.querySelectorAll('.tariff-tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active'); activeSmartTariff = tariffId;
  const td = smartData.tariffs.find(t => t.tariff_id === tariffId);
  if (td) renderSmartTariff(td);
}}

function renderSmartTariff(td) {{
  const sig = td.signal || 'gray';
  ['g','y','r'].forEach((k,i) => {{
    document.getElementById('sem-'+k).style.opacity = sig===(['green','yellow','red'][i])?'1':'0.12';
  }});
  const sigColors = {{green:'#e1f5ee',yellow:'#faeeda',red:'#fcebeb',gray:'#f4f5f7'}};
  const sigBorders = {{green:'#1d9e75',yellow:'#ba7517',red:'#e24b4a',gray:'#aaa'}};
  const sigText   = {{green:'#085041',yellow:'#633806',red:'#791f1f',gray:'#888'}};
  const card = document.getElementById('smart-signal-card');
  card.style.background = sigColors[sig]||'#f4f5f7'; card.style.borderColor = sigBorders[sig]||'#ddd';
  const nowStr = new Date().toLocaleTimeString('en-GB',{{hour:'2-digit',minute:'2-digit'}});
  const base = t('signal_'+sig);
  document.getElementById('smart-signal-label').innerHTML = sig!=='gray'
    ? base+`<span style="font-size:10px;font-weight:500;opacity:.7;margin-left:6px;padding:1px 6px;border-radius:4px;background:rgba(0,0,0,.07)">${{nowStr}} now</span>`
    : base;
  document.getElementById('smart-signal-label').style.color = sigText[sig]||'#888';

  const price = td.current_price_rp;
  document.getElementById('smart-price').textContent = price!=null?price.toFixed(1):'\u2014';
  const diff = price!=null&&td.month_avg_rp?price-td.month_avg_rp:null;
  document.getElementById('smart-signal-sub').textContent =
    diff!=null?(diff>=0?'+':'')+diff.toFixed(1)+' Rp '+t('vs_month_avg'):t('vs_month_avg');

  document.getElementById('smart-best').innerHTML = (td.best_slots||[]).map(s=>
    `<span class="slot-pill cheap">${{s.time}} \u00b7 ${{s.total_rp.toFixed(1)}} Rp</span>`).join('');
  document.getElementById('smart-worst').innerHTML = (td.worst_slots||[]).map(s=>
    `<span class="slot-pill exp">${{s.time}} \u00b7 ${{s.total_rp.toFixed(1)}} Rp</span>`).join('');

  const hourly = td.hourly_rp||[], vals = hourly.filter(v=>v!=null);
  const minV = vals.length?Math.min(...vals):0, maxV = vals.length?Math.max(...vals):1;
  const mAvg = td.month_avg_rp||0;
  document.getElementById('smart-bars').innerHTML = hourly.map((v,h)=>{{
    if (v==null) return `<div class="mini-bar" style="background:#eee;height:3px"></div>`;
    const pct = maxV>minV?Math.round((v-minV)/(maxV-minV)*100):50;
    const color = (mAvg>0?v/mAvg:1)<0.85?'#1d9e75':(mAvg>0?v/mAvg:1)>1.15?'#e24b4a':'#ba7517';
    return `<div class="mini-bar" style="height:${{Math.max(8,pct)}}%;background:${{color}}" title="${{h}}:00 \u00b7 ${{v.toFixed(1)}} Rp/kWh"></div>`;
  }}).join('');
}}

function renderSmartCompare(tariffs) {{
  const withData = tariffs.filter(t=>t.has_data&&t.day_avg_rp);
  if (!withData.length) {{
    const noDataMsg = lang==='it'?'Nessun dato disponibile per il confronto':lang==='de'?'Keine Vergleichsdaten verf\u00fcgbar':lang==='fr'?'Aucune donn\u00e9e pour la comparaison':'No data available for comparison';
    document.getElementById('smart-compare-rows').innerHTML = `<div style="font-size:12px;color:#aaa">${{noDataMsg}}</div>`; return;
  }}
  const maxAvg = Math.max(...withData.map(t=>t.day_avg_rp));
  const sorted = [...withData].sort((a,b)=>a.day_avg_rp-b.day_avg_rp);
  const bestLabel = lang==='it'?'migliore':lang==='de'?'bester':lang==='fr'?'meilleur':'best';
  document.getElementById('smart-compare-rows').innerHTML = sorted.map((t,i)=>{{
    const pct = Math.round(t.day_avg_rp/maxAvg*100); const isBest = i===0;
    return `<div class="compare-row">
      <span style="min-width:140px;font-size:12px;color:#333;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"
            title="${{t.provider_name}} \u2014 ${{t.tariff_name}}">${{t.provider_name}} \u2014 ${{t.tariff_name}}</span>
      <div class="compare-bar-wrap"><div class="compare-bar-fill" style="width:${{pct}}%;background:${{isBest?'#1d9e75':'#185fa5'}}"></div></div>
      <span style="min-width:52px;text-align:right;font-size:12px;font-weight:${{isBest?'600':'400'}};color:${{isBest?'#1d9e75':'#333'}}">${{t.day_avg_rp.toFixed(1)}} Rp</span>
      ${{isBest?`<span style="font-size:10px;padding:2px 6px;border-radius:4px;background:#e1f5ee;color:#085041;white-space:nowrap">${{bestLabel}}</span>`:''}}
    </div>`;
  }}).join('');
}}

// ── API copy helpers ──────────────────────────────────────────────────────────
function copyEndpoint(btn, path) {{
  navigator.clipboard.writeText(window.location.origin + path).then(() => {{
    const msg = lang==='it'?'Copiato!':lang==='de'?'Kopiert!':lang==='fr'?'Copi\u00e9\u00a0!':'Copied!';
    btn.textContent = msg; btn.classList.add('copied');
    setTimeout(() => {{ btn.textContent = 'Copy'; btn.classList.remove('copied'); }}, 1800);
  }});
}}

function copyPricesEndpoint(btn) {{
  const url = document.getElementById('prices-example-url').textContent.trim();
  navigator.clipboard.writeText(window.location.origin + url).then(() => {{
    const msg = lang==='it'?'Copiato!':lang==='de'?'Kopiert!':lang==='fr'?'Copi\u00e9\u00a0!':'Copied!';
    btn.textContent = msg; btn.classList.add('copied');
    setTimeout(() => {{ btn.textContent = 'Copy'; btn.classList.remove('copied'); }}, 1800);
  }});
}}

function updatePricesExample(tariffId) {{
  const el = document.getElementById('prices-example-url');
  if (!el) return;
  el.textContent = `/api/v1/prices?tariff_id=${{tariffId||'ckw_home_dynamic'}}&start_time={today}T00:00:00Z`;
}}

// ── Chart locale update (in-place, no re-fetch) ───────────────────────────────
function updateChartLocale() {{
  if (!activeTariff || !activeDate) return;
  // Chart title
  const titleEl = document.getElementById('chart-title');
  if (titleEl) {{
    titleEl.textContent = t('daily_profile') + ' \u2014 ' +
      new Date(activeDate + 'T12:00:00Z').toLocaleDateString(locale(), {{weekday:'long', day:'numeric', month:'long'}});
  }}
  // Day buttons
  if (dailySummary.length) {{
    document.getElementById('day-btns').innerHTML = dailySummary.map(d => {{
      const isToday = d.date === '{today}';
      const label = isToday
        ? t('today')
        : new Date(d.date + 'T12:00:00Z').toLocaleDateString(locale(), {{weekday:'short', day:'numeric', month:'short'}});
      const isActive = d.date === activeDate;
      return `<button class="day-btn${{isActive ? ' active' : ''}}" onclick="selectDay('${{d.date}}', this)">${{label}}</button>`;
    }}).join('');
  }}
  // Metric labels
  const isToday = activeDate === '{today}';
  const todaySuffix = ' \u00b7 ' + t('today').toLowerCase();
  const minLbl = document.getElementById('m-min-label');
  const maxLbl = document.getElementById('m-max-label');
  if (minLbl) minLbl.textContent = isToday ? t('cheapest_hour') + todaySuffix : t('cheapest_hour');
  if (maxLbl) maxLbl.textContent = isToday ? t('most_expensive_hour') + todaySuffix : t('most_expensive_hour');
  // m-date
  const dateEl = document.getElementById('m-date');
  if (dateEl && activeDate) {{
    dateEl.textContent = new Date(activeDate + 'T12:00:00Z').toLocaleDateString(locale(), {{day:'numeric', month:'long', year:'numeric'}});
  }}
  // m-slots
  const slotsEl = document.getElementById('m-slots');
  if (slotsEl && chartData.labels.length) {{
    slotsEl.textContent = chartData.labels.length + ' \u00d7 15 min ' + t('slots');
  }}
}}

// ── API status list (rendered in the API tab) ─────────────────────────────────
function renderApiStatusList(tariffs) {{
  const el = document.getElementById('api-status-list');
  if (!el) return;
  if (!tariffs || !tariffs.length) {{
    el.innerHTML = '<div style="font-size:12px;color:#bbb">loading...</div>';
    return;
  }}
  el.innerHTML = tariffs.map(t => {{
    const isOk = t.has_today_data || t.status === 'ok';
    const _afd = t.last_fetch_utc ? new Date(t.last_fetch_utc) : null;
    const meta = isOk && _afd
      ? 'Updated ' + _afd.toLocaleDateString('en-GB') + ' ' + _afd.toLocaleTimeString('en-GB') + ' UTC'
      : !isOk && !t.last_fetch_utc ? '' : '';
    const badge = isOk
      ? `<span class="s-ok">live</span>`
      : `<span class="s-warn">pending</span>`;
    return `<div class="status-row">
      <div>
        <div class="status-name">${{t.provider_name}} \u2014 ${{t.tariff_name || ''}}</div>
        ${{meta ? `<div class="status-meta">${{meta}}</div>` : ''}}
      </div>
      ${{badge}}
    </div>`;
  }}).join('');
}}

// ── API tab helpers ───────────────────────────────────────────────────────────
function populateApiTariffSelect() {{
  const okTariffs = (lastHealthData || []).filter(t => t.has_today_data || t.status === 'ok');
  const sel = document.getElementById('api-tariff-select');
  if (!sel) return;
  if (!okTariffs.length) {{
    sel.innerHTML = `<option value="">${{t('api_no_live_data')}}</option>`;
  }} else {{
    sel.innerHTML = okTariffs.map(t =>
      `<option value="${{t.tariff_id}}">${{t.provider_name}} \u2014 ${{t.tariff_name}}</option>`
    ).join('');
  }}
  updateApiPricesUrl();
  renderApiStatusList(lastHealthData);
}}

function updateApiPricesUrl() {{
  const sel = document.getElementById('api-tariff-select');
  const urlEl = document.getElementById('api-prices-url');
  const openLink = document.getElementById('api-prices-open');
  if (!sel || !urlEl) return;
  const tid = sel.value;
  const url = tid
    ? `/api/v1/prices?tariff_id=${{tid}}&start_time={today}T00:00:00Z`
    : `/api/v1/prices?tariff_id=...&start_time={today}T00:00:00Z`;
  urlEl.textContent = url;
  if (openLink) openLink.href = tid ? url : '/api/v1/prices';
}}

function copyApiPricesEndpoint(btn) {{
  const urlEl = document.getElementById('api-prices-url');
  if (!urlEl) return;
  const url = urlEl.textContent.trim();
  navigator.clipboard.writeText(window.location.origin + url).then(() => {{
    const msg = lang==='it'?'Copiato!':lang==='de'?'Kopiert!':lang==='fr'?'Copi\u00e9\u00a0!':'Copied!';
    btn.textContent = msg; btn.classList.add('copied');
    setTimeout(() => {{ btn.textContent = 'Copy'; btn.classList.remove('copied'); }}, 1800);
  }});
}}

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {{
  setLang('en');
  try {{
    await Promise.all([loadHealth(), loadTariffs()]);
    await onTariffChange();
  }} catch(e) {{
    console.error('Init error:', e);
    document.getElementById('header-badge').textContent = 'Error: ' + e.message;
    document.getElementById('header-badge').style.background = '#e24b4a';
  }}
}}

init();
setInterval(() => loadHealth(), 60000);
setInterval(() => {{ if (activeTariff) loadMonthly(activeTariff); }}, 300000);
</script>
</body>
</html>"""
    return html