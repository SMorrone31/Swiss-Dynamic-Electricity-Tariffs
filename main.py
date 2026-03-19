"""
main.py
=======
FastAPI app — espone le API definite nel PDF + endpoint di monitoraggio.

Endpoints:
  GET /api/v1/tariffs                           → lista tariffe (API 1 del PDF)
  GET /api/v1/prices?tariff_id=...&start_time=...  → prezzi (API 2 del PDF)
  GET /api/v1/health                            → stato fetch per EVU
  GET /                                         → dashboard HTML

Avvio:
  pip install fastapi uvicorn sqlalchemy apscheduler
  uvicorn main:app --reload

In produzione:
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    global SessionLocal
    SessionLocal = init_db()
    with SessionLocal() as session:
        n = seed_tariffs(session)
    print(f"[startup] DB pronto — {n} tariffe caricate")

    # Scheduler integrato nel processo FastAPI (necessario per Render free)
    import asyncio as _asyncio
    import json as _json
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    _scheduler = AsyncIOScheduler(timezone="UTC")

    async def _job_ckw():
        from scheduler import fetch_with_retry
        from schemas import tomorrow_ch
        target = tomorrow_ch()
        with SessionLocal() as _s:
            _ckw = [_json.loads(t.full_config_json) for t in get_active_tariffs(_s) if t.adapter_class == "CkwAdapter"]
        for _cfg in _ckw:
            await fetch_with_retry(_cfg, target, None)

    async def _job_others():
        from scheduler import fetch_with_retry
        from schemas import tomorrow_ch
        target = tomorrow_ch()
        with SessionLocal() as _s:
            _others = [_json.loads(t.full_config_json) for t in get_active_tariffs(_s) if t.adapter_class != "CkwAdapter"]
        _sem = _asyncio.Semaphore(3)
        async def _b(c):
            async with _sem: await fetch_with_retry(c, target, None)
        await _asyncio.gather(*[_b(c) for c in _others])

    async def _job_health():
        from scheduler import run_health_check
        await run_health_check()

    async def _job_monthly():
        from backfill import monthly_maintenance
        await monthly_maintenance()

    _scheduler.add_job(_job_ckw,     CronTrigger(hour=11, minute=5),      id="ckw")
    _scheduler.add_job(_job_others,  CronTrigger(hour=17, minute=30),     id="others")
    _scheduler.add_job(_job_health,  CronTrigger(hour=9,  minute=0),      id="health")
    _scheduler.add_job(_job_monthly, CronTrigger(day=1, hour=3, minute=0),id="monthly")
    _scheduler.start()
    print("[startup] Scheduler avviato — 11:05 / 17:30 / 09:00 UTC")

    yield

    _scheduler.shutdown()
    print("[shutdown] Scheduler fermato")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Swiss Dynamic Tariffs API",
    description="API unificata per le tariffe elettriche dinamiche svizzere",
    version="1.0.0",
    lifespan=lifespan,
)


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
    tariff_id:  str = Query(..., description="ID della tariffa"),
    start_time: str = Query(..., description="Data/ora inizio UTC (es. 2026-03-16T00:00:00Z)"),
    end_time:   Optional[str] = Query(None, description="Data/ora fine UTC (opzionale, default: +1 giorno)"),
):
    # zoneinfo gestisce DST automaticamente per qualsiasi anno futuro
    try:
        import zoneinfo; tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz; tz_ch = pytz.timezone("Europe/Zurich")

    try:
        start_utc = datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        raise HTTPException(400, f"start_time non valido: {start_time!r}. Formato: 2026-03-16T00:00:00Z")

    if end_time:
        try:
            end_utc = datetime.fromisoformat(end_time.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            raise HTTPException(400, f"end_time non valido: {end_time!r}")
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
            raise HTTPException(404, f"Tariffa '{tariff_id}' non trovata")
        slots = get_prices(session, tariff_id, start_db, end_db)
        # Filtra per data locale richiesta (esclude slot di giorni adiacenti)
        slots = [s for s in slots
                 if start_ld <= s.slot_start_utc.astimezone(tz_ch).date() < end_ld]

    if not slots:
        raise HTTPException(404, f"Nessun prezzo disponibile per '{tariff_id}' nel range {start_time} — {end_time or 'auto'}")

    response: dict = {
        "tariff_id":  tariff_id,
        "start_time": start_utc.isoformat(),
        "end_time":   end_utc.isoformat(),
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




# ── API 2b: tutti i prezzi in un colpo ───────────────────────────────────────

@app.get("/api/v1/prices/all")
def get_all_prices_endpoint(
    start_time: str = Query(..., description="Data/ora inizio UTC (es. 2026-03-17T00:00:00Z)"),
    end_time:   Optional[str] = Query(None, description="Data/ora fine UTC (opzionale, default: +1 giorno)"),
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
        raise HTTPException(400, f"start_time non valido: {start_time!r}")
    if end_time:
        try:
            end_utc = datetime.fromisoformat(end_time.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            raise HTTPException(400, f"end_time non valido: {end_time!r}")
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
        raise HTTPException(404, "Nessun dato disponibile per il range richiesto")
    return {"start_time": start_utc.isoformat(), "end_time": end_utc.isoformat(), "tariffs": result}

# ── Summary / health endpoints ────────────────────────────────────────────────

@app.get("/api/v1/summary/daily")
def get_daily_summary(tariff_id: str = Query(...), days: int = Query(5, ge=1, le=30)):
    with SessionLocal() as session:
        from database import Tariff
        if not session.get(Tariff, tariff_id):
            raise HTTPException(404, f"Tariffa '{tariff_id}' non trovata")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days + 1)
        rows = (
            session.query(PriceSlotDB.slot_start_utc, PriceSlotDB.energy_price,
                          PriceSlotDB.grid_price, PriceSlotDB.residual_price)
            .filter(PriceSlotDB.tariff_id == tariff_id, PriceSlotDB.slot_start_utc >= cutoff)
            .order_by(PriceSlotDB.slot_start_utc).all()
        )
    if not rows:
        raise HTTPException(404, "Nessun dato disponibile")

    try:
        import zoneinfo; tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz; tz_ch = pytz.timezone("Europe/Zurich")

    from collections import defaultdict
    by_date: dict = defaultdict(list)
    for (dt, energy, grid, residual) in rows:
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        local_date = dt.astimezone(tz_ch).date().isoformat()
        total = sum(p for p in [energy, grid, residual] if p)
        if total > 0: by_date[local_date].append((total, dt))

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
            raise HTTPException(404, f"Tariffa '{tariff_id}' non trovata")
        cutoff = datetime.now(timezone.utc) - timedelta(days=months * 32)
        rows = (
            session.query(PriceSlotDB.slot_start_utc, PriceSlotDB.energy_price,
                          PriceSlotDB.grid_price, PriceSlotDB.residual_price)
            .filter(PriceSlotDB.tariff_id == tariff_id, PriceSlotDB.slot_start_utc >= cutoff)
            .order_by(PriceSlotDB.slot_start_utc).all()
        )
    if not rows:
        raise HTTPException(404, "Nessun dato disponibile")

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
            raise HTTPException(404, f"Tariffa '{tariff_id}' non trovata")
        row = (session.query(PriceSlotDB.slot_start_utc)
               .filter(PriceSlotDB.tariff_id == tariff_id)
               .order_by(PriceSlotDB.slot_start_utc.desc()).first())
    if not row:
        raise HTTPException(404, "Nessun dato disponibile")
    dt = row[0]
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return {"tariff_id": tariff_id, "latest_date": dt.date().isoformat()}


@app.get("/api/v1/health")
def health():
    now = datetime.now(timezone.utc)
    today = now.date()
    with SessionLocal() as session:
        tariffs = get_active_tariffs(session)
        statuses = []
        from adapters import list_available_adapters
        available_adapters = set(list_available_adapters())
        for t in tariffs:
            last = get_last_fetch(session, t.tariff_id)
            has_today = last is not None and last.date() >= today
            adapter_ready = t.adapter_class in available_adapters
            if not adapter_ready:
                status, meta = "pending", "Adapter non ancora implementato"
            elif has_today:
                status, meta = "ok", None
            elif now.hour < 18:
                status, meta = "pending", f"Atteso dopo le {t.daily_update_time_utc} UTC"
            else:
                status, meta = "missing", "Dati non ricevuti oggi"
            statuses.append({
                "tariff_id":        t.tariff_id,
                "provider_name":    t.provider_name,
                "tariff_name":      t.tariff_name,
                "adapter_class":    t.adapter_class,
                "adapter_ready":    adapter_ready,
                "last_fetch_utc":   last.isoformat() if last else None,
                "has_today_data":   has_today,
                "status":           status,
                "status_detail":    meta,
                "daily_update_utc": t.daily_update_time_utc,
            })
    ready_count = sum(1 for s in statuses if s["adapter_ready"])
    ok_count    = sum(1 for s in statuses if s["status"] == "ok")
    return {
        "status":         "ok" if ok_count == ready_count and ready_count > 0 else "degraded",
        "timestamp":      now.isoformat(),
        "adapters_ready": ready_count,
        "adapters_total": len(statuses),
        "ok_count":       ok_count,
        "tariffs":        statuses,
    }


# ── Smart summary ─────────────────────────────────────────────────────────────

@app.get("/api/v1/smart")
def get_smart_summary(date_str: Optional[str] = Query(None)):
    import time as _time
    target_date = date.fromisoformat(date_str) if date_str else date.today()
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
    today = date.today().isoformat()
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

  <div class="card">
    <div class="card-header"><span class="card-title" data-i18n="fetch_status"></span></div>
    <div class="status-list" id="status-list">loading...</div>
  </div>

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

  <!-- ── API view ──────────────────────────────────────────────────────────── -->
  <div id="api-view">

    <div class="api-view-header">
      <div class="api-view-title">API Reference</div>
      <div class="api-view-sub">REST · UTC timestamps · 15-min intervals · CHF/kWh · as per T-Swiss spec</div>
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
      <div class="api-desc">List all available tariffs with metadata. No parameters required — returns the full catalogue.</div>
      <div class="api-params-wrap">
        <div class="api-params-head">Returns</div>
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
      <div class="api-desc">Timeseries of net prices per kWh — energy, grid and residual components — in 15-min UTC slots.</div>
      <div class="api-params-wrap">
        <div class="api-params-head">Parameters</div>
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
        <div class="api-params-head" style="margin-top:8px">Returns</div>
        <table class="api-params-table">
          <tr><td>energy_price_utc</td><td>list of <code>&#123;start_timestamp: price_chf&#125;</code></td></tr>
          <tr><td>grid_price_utc</td><td>list of <code>&#123;start_timestamp: price_chf&#125;</code></td></tr>
          <tr><td>residual_price_utc</td><td>list of <code>&#123;start_timestamp: price_chf&#125;</code></td></tr>
          <tr><td>slot_count</td><td>Number of 15-min slots (96 = full day)</td></tr>
        </table>
      </div>
      <div style="margin:12px 0 4px 0;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="font-size:11px;color:#555;font-weight:500">Live tariff:</span>
        <select id="api-tariff-select" class="api-tariff-select" onchange="updateApiPricesUrl()">
          <option value="">— no live data yet —</option>
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
      <div class="api-desc">All currently live tariffs in a single response — JSON keyed by <code>tariff_id</code>. Fixed endpoint, no tariff selection needed. Always reflects real-time available data.</div>
      <div class="api-url-box">
        <span>/api/v1/prices/all?start_time={today}T00:00:00Z</span>
      </div>
    </div>

    <!-- ── Links ──────────────────────────────────────────────────────────── -->
    <div class="api-links-row">
      <a href="/docs"  target="_blank" class="api-open-link" style="border-color:#185fa5;color:#185fa5">Swagger UI ↗</a>
      <a href="/redoc" target="_blank" class="api-open-link" style="border-color:#185fa5;color:#185fa5">ReDoc ↗</a>
      <a href="/api/v1/health" target="_blank" class="api-open-link">Health status ↗</a>
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
    adapter_pending: 'Adapter not yet implemented',
    never_updated: 'Never updated', updated_at: 'Updated at',
    above_avg: 'above average', below_avg: 'below average', daily_avg: 'Daily avg',
    grid_tip: 'Network usage fee (dynamic, set by grid operator)',
    energy_tip: 'Electricity commodity price (spot market based)',
    residual_tip: 'Residual costs: SDL, Stromreserve, Netzzuschlag, levies',
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
    adapter_pending: 'Adapter noch nicht implementiert',
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
    adapter_pending: 'Adaptateur pas encore impl\u00e9ment\u00e9',
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
    adapter_pending: 'Adattatore non ancora implementato',
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
    const meta = t.status === 'pending' && !t.last_fetch_utc ? pendingMsg
      : t.last_fetch_utc ? updatedPrefix + new Date(t.last_fetch_utc).toLocaleTimeString('en-GB') + ' UTC'
      : neverMsg;
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
              return ['',` Total: ${{tot.toFixed(2)}} Rp/kWh`,` vs avg: ${{diff>=0?'+':''}}${{diff.toFixed(2)}} Rp`];
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

// ── View switching ────────────────────────────────────────────────────────────
let currentView = 'data', smartData = null, activeSmartTariff = null;

function switchView(view) {{
  currentView = view;
  document.getElementById('consumer-view').style.display  = view==='smart'?'block':'none';
  document.getElementById('developer-view').style.display = view==='data'?'block':'none';
  document.getElementById('api-view').style.display       = view==='api'?'block':'none';
  document.getElementById('tab-smart').classList.toggle('active', view==='smart');
  document.getElementById('tab-data').classList.toggle('active',  view==='data');
  document.getElementById('tab-api').classList.toggle('active',   view==='api');
  if (view==='smart' && !smartData) loadSmart();
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

// ── API tab helpers ───────────────────────────────────────────────────────────
function populateApiTariffSelect() {{
  const okTariffs = (lastHealthData || []).filter(t => t.has_today_data || t.status === 'ok');
  const sel = document.getElementById('api-tariff-select');
  if (!sel) return;
  if (!okTariffs.length) {{
    sel.innerHTML = '<option value="">— no live data yet —</option>';
  }} else {{
    sel.innerHTML = okTariffs.map(t =>
      `<option value="${{t.tariff_id}}">${{t.provider_name}} \u2014 ${{t.tariff_name}}</option>`
    ).join('');
  }}
  updateApiPricesUrl();
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