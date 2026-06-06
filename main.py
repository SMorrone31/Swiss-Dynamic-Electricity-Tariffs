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

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from database import (
    init_db, seed_tariffs, get_active_tariffs,
    get_prices, get_last_fetch, PriceSlotDB
)
from fastapi.templating import Jinja2Templates as _Jinja2Templates
from fastapi.staticfiles  import StaticFiles as _StaticFiles
import os as _os

_templates = _Jinja2Templates(
    directory=_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "templates")
)

from schemas import make_day_range_utc


# ── Limiti piano free/pro ─────────────────────────────────────────────────────

def _plan_info(request: Request) -> tuple[str, str | None]:
    """
    Ritorna (plan, free_tariff_id) dallo stato della request.
    Impostato dal middleware dopo verifica API key.
    Se non presente (chiamata interna) → ("pro", None) per non bloccare.
    """
    plan           = getattr(request.state, "plan",           "pro")
    free_tariff_id = getattr(request.state, "free_tariff_id", None)
    return plan, free_tariff_id


def _require_free_tariff_chosen(free_tariff_id: str | None) -> None:
    """Solleva 400 se l'utente free non ha ancora scelto la sua tariffa."""
    if not free_tariff_id:
        raise HTTPException(
            400,
            {
                "error":   "no_default_tariff",
                "message": "Free plan: select your default tariff first via POST /api/v1/me/tariff",
                "hint":    "Login to the dashboard and choose your tariff, or upgrade to Pro for full access.",
            },
        )


def _enforce_free_tariff(request_tariff: str | None, free_tariff_id: str | None) -> str:
    """
    Per piano free: se il tariff_id richiesto non è quello scelto → 403.
    Ritorna il tariff_id valido.
    """
    _require_free_tariff_chosen(free_tariff_id)
    if request_tariff and request_tariff != free_tariff_id:
        raise HTTPException(
            403,
            {
                "error":   "plan_restriction",
                "message": f"Free plan: you can only access tariff '{free_tariff_id}'. Upgrade to Pro for all tariffs.",
                "your_tariff": free_tariff_id,
                "requested":   request_tariff,
            },
        )
    return free_tariff_id


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
    print("  23:05 UTC (00:05 CET) → fetch TODAY mezzanotte     [alert immediato se ✗]")
    print("  06:30 UTC (07:30 CET) → recovery TODAY mattutino   [no-op se ok]")
    print("  09:00 UTC (10:00 CET) → health check mattutino     [alert se mancano]")
    print("  11:15 UTC (12:15 CET) → fetch CKW primario")
    print("  12:30 UTC (13:30 CET) → recovery CKW               [no-op se ok]")
    print("  17:15 UTC (18:15 CET) → fetch altri EVU primario")
    print("  19:00 UTC (20:00 CET) → recovery tutti             [no-op se ok]")
    print("  22:15 UTC (23:15 CET) → last resort                [alert se vuoto]")
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

import os as _os
_env = _os.getenv("ENVIRONMENT", "development")
app = FastAPI(
    title="Swiss Dynamic Tariffs API",
    description="Unified API for Swiss Tariff Hub",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _env == "development" else None,
    redoc_url="/redoc" if _env == "development" else None,
    openapi_url="/openapi.json" if _env == "development" else None,
)
app.mount("/static", _StaticFiles(directory=_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "static")), name="static")


# In main.py, dopo la creazione dell'app:
from fastapi import Request
import os
 
# Registra routes admin e di registrazione
from admin_routes import register_routes, api_key_middleware
 
# Sostituisce il vecchio middleware VALID_API_KEYS statico
app.middleware("http")(api_key_middleware)
 
# Registra tutti gli endpoint admin + /api/v1/register + /api/v1/validate-key
register_routes(app)


# ── API 1: lista tariffe ──────────────────────────────────────────────────────

@app.get("/api/v1/tariffs")
def get_tariffs(request: Request):
    plan, free_tariff_id = _plan_info(request)
    def expand_zip_ranges(raw: list) -> list[int]:
        """
        Normalizza zip_ranges in lista di interi, qualunque sia il formato nel JSON:
          - int       →  [6810]
          - [lo, hi]  →  range espanso
          - "4000-4199" → range espanso
          - "4242"    →  [4242]
        """
        result = []
        for z in raw:
            if isinstance(z, int):
                result.append(z)
            elif isinstance(z, list) and len(z) == 2:
                result.extend(range(int(z[0]), int(z[1]) + 1))
            elif isinstance(z, str):
                z = z.strip()
                if "-" in z:
                    parts = z.split("-")
                    try:
                        result.extend(range(int(parts[0]), int(parts[1]) + 1))
                    except ValueError:
                        pass
                else:
                    try:
                        result.append(int(z))
                    except ValueError:
                        pass
        return sorted(set(result))

    with SessionLocal() as session:
        tariffs = get_active_tariffs(session)

    # Piano free: restituisce solo la tariffa scelta
    if plan == "free":
        _require_free_tariff_chosen(free_tariff_id)
        tariffs = [t for t in tariffs if t.tariff_id == free_tariff_id]

    return [
        {
            "tariff_id":                  t.tariff_id,
            "tariff_name":                t.tariff_name,
            "provider_name":              t.provider_name,
            "zip_ranges":                 expand_zip_ranges(
                                              json.loads(t.full_config_json).get("zip_ranges", [])
                                          ),
            "daily_update_time_utc":      t.daily_update_time_utc,
            "datetime_available_from_utc": (
                t.datetime_available_from.isoformat() if t.datetime_available_from else None
            ),
            "valid_until_utc":            (t.valid_until.isoformat() if t.valid_until else None),
            "sgr_compliant":              t.sgr_compliant,
            "dynamic_elements":           json.loads(t.dynamic_elements_json),
            "time_resolution_minutes":    t.time_resolution_minutes,
        }
        for t in tariffs
    ]


# ── API 2: prezzi timeseries ──────────────────────────────────────────────────

@app.get("/api/v1/prices")
def get_prices_endpoint(
    request:    Request,
    tariff_id:  str = Query(..., description="Tariff ID"),
    start_time: str = Query(..., description="Date/time start UTC (es. 2026-03-16T00:00:00Z)"),
    end_time:   Optional[str] = Query(None, description="Date/time end UTC (optional, default: +1 day)"),
):
    plan, free_tariff_id = _plan_info(request)

    # Piano free: solo la tariffa scelta + solo oggi
    if plan == "free":
        tariff_id = _enforce_free_tariff(tariff_id, free_tariff_id)
        from schemas import today_ch as _today_ch
        today_iso = _today_ch().isoformat()
        start_time = f"{today_iso}T00:00:00Z"
        end_time   = None  # default +1 day = solo oggi
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
    request:   Request,
    zip_code:  Optional[int] = Query(None, description="CAP svizzero, es. 6810"),
    tariff_id: Optional[str] = Query(None, description="Tariff ID diretto, es. aem_tariffa_dinamica"),
):
    """
    Prezzi della giornata corrente (ora locale svizzera).
    Usa zip_code OPPURE tariff_id — non entrambi.
    """
    from schemas import today_ch as _today_ch
    plan, free_tariff_id = _plan_info(request)

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

    # Piano free: solo la tariffa scelta
    if plan == "free":
        tariff_id = _enforce_free_tariff(tariff_id, free_tariff_id)

    return _prices_for_date(tariff_id, _today_ch())
  
  
  
# ── API 2b: tutti i prezzi in un colpo ───────────────────────────────────────

@app.get("/api/v1/prices/all")
def get_all_prices_endpoint(
    request:    Request,
    start_time: str = Query(..., description="Date/time start UTC (es. 2026-03-17T00:00:00Z)"),
    end_time:   Optional[str] = Query(None, description="Date/time end UTC (optional, default: +1 day)"),
):
    """
    Restituisce i prezzi di TUTTE le tariffe attive in un unico JSON.
    Richiede piano Pro.
    """
    plan, _ = _plan_info(request)
    if plan != "pro":
        raise HTTPException(
            403,
            {"error": "pro_required",
             "message": "GET /api/v1/prices/all requires a Pro plan. Upgrade at swiss-tariff-hub.ch.",
             "upgrade_url": "https://swiss-tariff-hub.ch/#upgrade"},
        )
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
def get_daily_summary(request: Request, tariff_id: str = Query(...), days: int = Query(5, ge=1, le=30)):
    plan, free_tariff_id = _plan_info(request)
    # Piano free: solo tariffa scelta, solo oggi (days=1)
    if plan == "free":
        tariff_id = _enforce_free_tariff(tariff_id, free_tariff_id)
        days = 1
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
def get_monthly_summary(request: Request, tariff_id: str = Query(...), months: int = Query(3, ge=1, le=6)):
    plan, _ = _plan_info(request)
    if plan != "pro":
        raise HTTPException(
            403,
            {"error": "pro_required",
             "message": "GET /api/v1/summary/monthly requires a Pro plan.",
             "upgrade_url": "https://swiss-tariff-hub.ch/#upgrade"},
        )
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

@app.get("/api/v1/zip-map")
def get_zip_map():
    """
    ZIP display string per tariff_id, calcolato da evu_list.json.
    Usato dalla mappa per mostrare il CAP nel tooltip.
    """
    import pathlib
    cfg_path = pathlib.Path(__file__).parent / "config" / "evu_list.json"
    try:
        with open(cfg_path, encoding="utf-8") as f:
            evus = json.load(f)
    except Exception:
        return JSONResponse(content={})
    result = {}
    for e in evus:
        tid      = e.get("tariff_id", "")
        zips_raw = e.get("zip_ranges", [])
        zips_all = []
        for z in zips_raw:
            if isinstance(z, int):
                zips_all.append(z)
            elif isinstance(z, list) and len(z) == 2:
                zips_all.extend(range(int(z[0]), int(z[1]) + 1))
            elif isinstance(z, str):
                # Formato "4000-4199" oppure singolo "4242"
                z = z.strip()
                if "-" in z:
                    parts = z.split("-")
                    try:
                        zips_all.extend(range(int(parts[0]), int(parts[1]) + 1))
                    except ValueError:
                        pass
                else:
                    try:
                        zips_all.append(int(z))
                    except ValueError:
                        pass
        zips_all = sorted(set(zips_all))
        if len(zips_all) == 0:
            display = ""
        elif len(zips_all) <= 6:
            display = ", ".join(str(z) for z in zips_all)
        else:
            display = str(zips_all[0]) + "–" + str(zips_all[-1])
        result[tid] = {"zip_display": display, "zip_count": len(zips_all)}
    return JSONResponse(content=result)


@app.get("/api/v1/municipalities")
def get_municipalities():
    """
    Mapping BFS-ID -> nome comune svizzero ufficiale (2024).
    Usato dalla mappa per il tooltip. Fonte: BFS registro comuni 2024.
    """
    bfs_names = {
        # Canton Zurigo (ZH)
        "1":"Aeugst am Albis","2":"Affoltern am Albis","3":"Bonstetten","4":"Hausen am Albis","5":"Hedingen",
        "6":"Knonau","7":"Maschwanden","8":"Mettmenstetten","9":"Obfelden","10":"Ottikon (Knonau)",
        "11":"Rifferswil","12":"Stallikon","13":"Wettswil am Albis","14":"Adliswil","15":"Dübendorf",
        "16":"Fällanden","17":"Greifensee","18":"Kilchberg (ZH)","19":"Küsnacht (ZH)",
        "21":"Langnau am Albis","22":"Maur","23":"Männedorf","24":"Meilen","25":"Neftenbach",
        "26":"Oetwil am See","27":"Oetwil an der Limmat","28":"Opfikon","29":"Regensdorf","30":"Richterswil",
        "31":"Rüschlikon","32":"Schlieren","33":"Schönenberg (ZH)","34":"Staefa","35":"Thalwil",
        "36":"Uetikon am See","37":"Uitikon","38":"Uster","39":"Volketswil","40":"Wädenswil",
        "41":"Wallisellen","42":"Wangen-Brüttisellen","43":"Zollikon","44":"Zumikon","45":"Zürich",
        "51":"Bachenbülach","52":"Bassersdorf","53":"Bülach","54":"Dietlikon","55":"Eglisau",
        "56":"Embrach","57":"Freienstein-Teufen","58":"Glattfelden","59":"Hochfelden","60":"Höri",
        "61":"Hüntwangen","62":"Kloten","63":"Lufingen","64":"Nürensdorf","65":"Oberembrach",
        "66":"Rafz","67":"Rorbas","68":"Winkel","70":"Wasterkingen",
        "71":"Wil (ZH)","72":"Weiach","81":"Andelfingen","82":"Benken (ZH)",
        "83":"Berg am Irchel","84":"Buch am Irchel","85":"Dachsen","86":"Dorf","87":"Ellikon am Rhein",
        "88":"Flaach","89":"Flurlingen","90":"Henggart","91":"Humlikon","92":"Kleinandelfingen",
        "93":"Laufen-Uhwiesen","95":"Ossingen","96":"Rheinau","98":"Thalheim an der Thur",
        "99":"Truttikon","100":"Volken","101":"Waltalingen",
        "111":"Adlikon","112":"Bertschikon","113":"Brütten","114":"Dättlikon","115":"Dinhard",
        "117":"Elsau","119":"Hagenbuch","121":"Illnau-Effretikon","126":"Seuzach","129":"Winterthur",
        "131":"Zell (ZH)","135":"Bauma","136":"Bäretswil","137":"Fischenthal","138":"Hinwil",
        "141":"Grüningen","146":"Rüti (ZH)","147":"Seegräben","148":"Wald (ZH)","149":"Wetzikon (ZH)",
        "153":"Adlikon","157":"Andelfingen","160":"Zürich","173":"Embrach","178":"Glattfelden",
        "180":"Hinwil","181":"Hombrechtikon","182":"Horgen","192":"Kilchberg (ZH)","194":"Küsnacht (ZH)",
        "195":"Langnau am Albis","196":"Männedorf","197":"Maur","199":"Neftenbach","200":"Obfelden",
        "211":"Opfikon","213":"Rafz","214":"Regensdorf","215":"Richterswil","216":"Rorbas",
        "218":"Rüschlikon","219":"Schlieren","220":"Schönenberg (ZH)","221":"Seegräben",
        "223":"Stadel","224":"Staefa","225":"Thalwil","226":"Uetikon am See","227":"Uitikon",
        "228":"Uster","231":"Volketswil","241":"Wädenswil","242":"Wangen-Brüttisellen",
        "243":"Wasterkingen","244":"Weiach","245":"Wettswil am Albis","246":"Wil (ZH)",
        "247":"Wiesendangen","248":"Winkel","249":"Winterthur","250":"Zollikon","251":"Zumikon",
        "291":"Pfäffikon","292":"Pfäffikon","293":"Pfäffikon","294":"Pfäffikon","295":"Pfäffikon",
        "296":"Pfäffikon","297":"Pfäffikon","298":"Pfäffikon","1301":"Einsiedeln","1704":"Zürich",
        # Canton Berna (BE)
        "2422":"Aarberg","2471":"Arlesheim","2472":"Birsfelden","2473":"Bottmingen","2474":"Bettlach",
        "2475":"Dornach","2476":"Ettingen","2477":"Gempen","2478":"Hochwald","2479":"Hofstetten-Flüh",
        "2480":"Inzlingen","2481":"Metzerlen-Mariastein","2491":"Aefligen","2493":"Bätterkinden",
        "2495":"Burgdorf","2497":"Ersigen","2499":"Hindelbank","2500":"Jegenstorf","2501":"Kirchberg (BE)",
        "2502":"Koppigen","2572":"Bätterkinden","2573":"Burgdorf","2576":"Gretzenbach",
        "2582":"Aefligen","2583":"Alchenstorf","2584":"Bätterkinden","2585":"Burgdorf","2586":"Ersigen",
        "2611":"Allschwil","2612":"Arlesheim","2613":"Bettlach","2614":"Binningen",
        "2615":"Birsfelden","2616":"Bottmingen","2617":"Dornach","2618":"Ettingen","2619":"Gempen",
        "2620":"Hochwald","2621":"Hofstetten-Flüh","2622":"Metzerlen-Mariastein",
        "2761":"Aetigkofen","2762":"Aetingen","2763":"Bätterkinden","2764":"Biberist","2765":"Bolken",
        "2766":"Burgdorf","2767":"Derendingen","2768":"Etziken","2769":"Fehren","2770":"Gerlafingen",
        "2771":"Gunzgen","2772":"Härkingen","2773":"Hersiwil","2774":"Horriwil","2775":"Hüniken",
        "2782":"Balm bei Messen","2783":"Bibern (SO)","2785":"Messen","2786":"Niederwil (SO)",
        "2787":"Nennigkofen","2788":"Oberdorf (SO)","2830":"Lüterkofen-Ichertswil",
        "2831":"Lüterswil-Gächliwil","2883":"Rüttenen","2889":"Selzach",
        # Canton Lucerna (LU)
        "1001":"Adligenswil","1002":"Buchrain","1004":"Dierikon","1005":"Ebikon","1007":"Eschenbach (LU)",
        "1008":"Gisikon","1009":"Greppen","1010":"Honau","1021":"Horw","1023":"Inwil","1024":"Littau",
        "1025":"Luzern","1026":"Malters","1030":"Meggen","1032":"Meierskappel","1033":"Neuenkirch",
        "1037":"Rain","1039":"Root","1040":"Rothenburg","1041":"Ruswil","1051":"Schwarzenberg",
        "1052":"Sempach","1053":"Sursee","1054":"Triengen","1055":"Ufhusen","1058":"Vitznau",
        "1059":"Weggis","1061":"Wolhusen","1063":"Udligenswil","1064":"Wauwil","1065":"Wikon",
        "1067":"Willisau","1081":"Ballwil","1082":"Beromünster","1083":"Büron","1084":"Dierikon",
        "1085":"Ermensee","1086":"Eschenbach (LU)","1088":"Geuensee","1089":"Grosswangen",
        "1091":"Hellbühl","1093":"Hildisrieden","1094":"Hochdorf","1095":"Hohenrain",
        "1097":"Hüswil","1098":"Inwil","1099":"Knutwil","1100":"Luzern","1102":"Luthern",
        "1103":"Meierskappel","1104":"Menznau","1107":"Muri bei Bern","1121":"Nottwil",
        "1122":"Oberkirch (LU)","1123":"Pfaffnau","1125":"Rain","1127":"Rickenbach (LU)",
        "1128":"Römerswil","1129":"Ruswil","1131":"Schlierbach","1136":"Sempach","1137":"Sursee",
        "1139":"Triengen","1140":"Ufhusen","1142":"Wauwil","1143":"Wikon","1146":"Willisau",
        "1147":"Winikon","1150":"Wolhusen","1151":"Zell (LU)",
        # Canton Ticino (TI)
        "5009":"Massagno","5141":"Agno","5143":"Aranno","5144":"Arogno","5148":"Balerna",
        "5151":"Bedano","5154":"Besazio","5160":"Brusino Arsizio","5161":"Cadenazzo",
        "5162":"Camorino","5167":"Carona","5171":"Castel San Pietro","5176":"Caslano",
        "5180":"Chiasso","5186":"Coldrerio","5187":"Collina d'Oro","5189":"Comano",
        "5192":"Croglio","5193":"Cugnasco-Gerra","5194":"Cureglia","5196":"Lugano",
        "5198":"Lumino","5199":"Magliaso","5203":"Maroggia","5205":"Massagno",
        "5206":"Melide","5208":"Mendrisio","5210":"Mezzovico-Vira","5212":"Minusio",
        "5214":"Montagnola","5216":"Monteceneri","5221":"Morcote","5225":"Muzzano",
        "5226":"Breganzona","5227":"Neggio","5230":"Novazzano","5231":"Novazzano",
        "5233":"Origlio","5236":"Paradiso","5237":"Porza","5238":"Pura","5239":"Riva San Vitale",
        "5240":"Rovio","5249":"Savosa","5251":"Sessa","5260":"Sorengo","5263":"Stabio",
        "5269":"Vernate",
        # Canton Argovia (AG)
        "4239":"Aarau",
        # Canton Friburgo (FR)
        "669":"Gryon","2008":"Attalens","2011":"Broc","2022":"Châtel-Saint-Denis","2025":"Charmey",
        "2029":"Corpataux-Magnedens","2035":"Courgevaux","2041":"Düdingen","2043":"Ecuvillens",
        "2044":"Ependes (FR)","2045":"Estavayer","2050":"Fribourg","2051":"Giffers",
        "2053":"Givisiez","2054":"Grolley","2055":"Gruyères","2063":"Hauteville","2067":"Jaun",
        "2068":"La Brillaz","2079":"Le Mouret","2086":"Liebistorf","2087":"Lossy",
        "2096":"Marly","2097":"Marsens","2099":"Matran","2102":"Ménières","2113":"Morlon",
        "2114":"Murist","2115":"Nuvilly","2117":"Orsonnens","2121":"Pont-la-Ville",
        "2122":"Portalban-Dessous","2124":"Posieux","2125":"Prez","2134":"Romont (FR)",
        "2135":"Rossens (FR)","2137":"Saint-Aubin (FR)","2138":"Sâles","2140":"Salvagny",
        "2145":"Siviriez","2147":"Sorens","2149":"Surpierre","2152":"Treyvaux","2153":"Ursy",
        "2155":"Villars-sur-Glâne","2160":"Vuadens","2162":"Vuisternens-devant-Romont",
        "2173":"Bussy (FR)","2174":"Cressier (FR)","2175":"Bas-Vully","2177":"Estavayer",
        "2183":"Fribourg","2186":"Givisiez","2194":"Murten","2196":"Ried bei Kerzers",
        "2197":"Murten","2198":"Meyriez","2206":"Muntelier","2208":"Muntelier",
        "2211":"Münchenwiler","2216":"Nant","2220":"Praz","2226":"Haut-Vully",
        "2228":"Bas-Vully","2230":"Bas-Vully","2233":"Staufen","2234":"Sugiez",
        "2235":"Ulmiz","2236":"Vallon","2238":"Murten","2239":"Murten",
        "2250":"Bottens","2254":"Autigny","2257":"Belfaux","2258":"Corserey",
        "2261":"Courtepin","2262":"Cressier (FR)","2265":"Formangueires","2266":"Fribourg",
        "2272":"Givisiez","2275":"Granges-Paccot","2276":"Grolley","2284":"Hauterive (FR)",
        "2292":"Corminboeuf","2293":"Moncor","2294":"Fribourg","2295":"Noréaz","2296":"Onnens (FR)",
        "2299":"Pierrafortscha","2300":"Praroman","2301":"Rechthalten","2303":"Romont (FR)",
        "2304":"Rossens (FR)","2305":"Rueyres-les-Prés","2306":"Saint-Aubin (FR)","2307":"Sâles",
        "2308":"Salvagny","2309":"Seedorf (FR)","2321":"Barberêche","2323":"Bussy (FR)",
        "2325":"Courtepin","2328":"Cressier (FR)","2333":"Fribourg","2335":"Fribourg",
        "2336":"Grolley","2337":"Hauterive (FR)","2338":"Murten",
        # Canton Svitto (SZ)
        "5451":"Gersau","5456":"Illgau","5458":"Ingenbohl","5464":"Lauerz",
        "5790":"Sattel","5813":"Schwyz","5816":"Seewen","5817":"Ingenbohl",
        "5821":"Steinen","5822":"Steinerberg","5827":"Unteriberg","5841":"Vorderthal",
        "5842":"Wald (SZ)","5843":"Wollerau",
        # Canton Glarona (GL)
        "6413":"Betschwanden","6416":"Braunwald","6417":"Elm","6423":"Glarus","6432":"Luchsingen",
        "6433":"Mitlödi","6434":"Mollis","6435":"Mühlehorn","6437":"Näfels","6451":"Niederurnen",
        "6452":"Netstal","6456":"Oberurnen","6458":"Riedern","6487":"Schwanden","6504":"Sool",
        "6511":"Ennenda","6512":"Glarus","6513":"Riedern",
    }
    return JSONResponse(content=bfs_names)


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
def get_smart_summary(request: Request, date_str: Optional[str] = Query(None)):
    import time as _time
    from schemas import today_ch as _today_ch
    plan, free_tariff_id = _plan_info(request)
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

    # Piano free: filtra solo la tariffa scelta
    if plan == "free":
        _require_free_tariff_chosen(free_tariff_id)
        data["tariffs"] = [t for t in data["tariffs"] if t["tariff_id"] == free_tariff_id]

    _summary_cache[cache_key] = {"ts": _time.time(), "data": data}
    return data


# ── Dashboard HTML ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    import os
    from datetime import date
    return _templates.TemplateResponse("dashboard.html", {
        "request":          request,
        "today":            date.today().isoformat(),
        "dashboard_secret": os.getenv("DASHBOARD_SECRET", ""),
    })