"""
adapters/base.py
================
Classe base che tutti gli adapter devono implementare.

Ogni EVU ha la sua API con formato diverso. Il BaseAdapter definisce
un contratto unico: qualunque sia il formato grezzo dell'EVU, l'adapter
deve produrre un NormalizedPrices.

Struttura di un adapter concreto:
  1. Costruisce l'URL e i parametri della chiamata
  2. Chiama l'API (gestisce auth, retry, timeout)
  3. Parsa la risposta grezza
  4. Converte ogni valore in un PriceSlot (UTC, CHF/kWh)
  5. Ritorna NormalizedPrices

L'adapter NON deve:
  - Salvare nel DB (lo fa lo scheduler)
  - Mandare alert (lo fa lo scheduler)
  - Conoscere altri EVU
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from typing import Optional

import httpx

from schemas import NormalizedPrices, PriceSlot, make_day_range_utc, utc_now

log = logging.getLogger(__name__)


# ── Eccezioni ─────────────────────────────────────────────────────────────────

class AdapterError(Exception):
    """Errore generico dell'adapter."""

class AdapterNetworkError(AdapterError):
    """L'API non è raggiungibile o ha risposto con errore HTTP."""

class AdapterParseError(AdapterError):
    """La risposta è stata ricevuta ma non è parsabile."""

class AdapterEmptyError(AdapterError):
    """La risposta è valida ma non contiene dati per il giorno richiesto."""


# ── BaseAdapter ───────────────────────────────────────────────────────────────

class BaseAdapter(ABC):
    """
    Classe base per tutti gli adapter EVU.

    Ogni sottoclasse deve implementare solo due metodi:
      - _build_url()    → costruisce l'URL con i parametri
      - _parse()        → parsa la risposta e ritorna lista di PriceSlot

    Il metodo fetch() gestisce la chiamata HTTP, il retry e gli errori.
    """

    # Timeout di default per le chiamate HTTP
    TIMEOUT_SECONDS: int = 20

    # Numero di retry in caso di errore temporaneo (5xx, timeout)
    MAX_RETRIES: int = 2

    def __init__(self, tariff_config: dict) -> None:
        """
        tariff_config: la entry corrispondente da evu_list.json
        Es:
          {
            "tariff_id": "ckw_home_dynamic",
            "api_base_url": "https://...",
            "auth_type": "none",
            "auth_config": {},
            ...
          }
        """
        self.tariff_id   = tariff_config["tariff_id"]
        self.tariff_name = tariff_config.get("tariff_name", "")
        self.provider    = tariff_config.get("provider_name", "")
        self.base_url    = tariff_config["api_base_url"]
        self.auth_type   = tariff_config.get("auth_type", "none")
        self.auth_config = tariff_config.get("auth_config", {})
        self.config      = tariff_config

        self._log = logging.getLogger(
            f"{self.__class__.__module__}.{self.__class__.__name__}"
            f"[{self.tariff_id}]"
        )

    # ── Metodi astratti da implementare ───────────────────────────────────────

    @abstractmethod
    def _build_url(self, start_utc: datetime, end_utc: datetime) -> str:
        """
        Costruisce l'URL completo con i parametri per la chiamata API.

        Args:
            start_utc: inizio del periodo richiesto (UTC)
            end_utc:   fine del periodo richiesto (UTC)

        Returns:
            URL completo pronto per la chiamata GET
        """

    @abstractmethod
    def _parse(
        self,
        response_data: dict | list,
        target_date: date,
    ) -> list[PriceSlot]:
        """
        Parsa la risposta grezza dell'API e ritorna i PriceSlot.

        Args:
            response_data: JSON già parsato (dict o list)
            target_date:   data del giorno richiesto

        Returns:
            Lista di PriceSlot in UTC, prezzi in CHF/kWh

        Raises:
            AdapterParseError: se la risposta non è nel formato atteso
            AdapterEmptyError: se non ci sono dati per il giorno richiesto
        """

    # ── Metodi opzionali da sovrascrivere ─────────────────────────────────────

    def _build_headers(self) -> dict[str, str]:
        """
        Headers HTTP per la chiamata.
        Sovrascrivere per auth token, API key, ecc.
        """
        headers = {
            "Accept":     "application/json",
            "User-Agent": "swiss-tariff-hub/1.0",
        }

        if self.auth_type == "api_key":
            key   = self.auth_config.get("api_key", "")
            hname = self.auth_config.get("header_name", "X-API-Key")
            headers[hname] = key

        elif self.auth_type == "bearer":
            token = self.auth_config.get("token", "")
            headers["Authorization"] = f"Bearer {token}"

        elif self.auth_type == "messpunkt_token":
            # EGA / ESIT: numero punto di misura + token
            mp    = self.auth_config.get("messpunkt_nummer", "")
            token = self.auth_config.get("auth_token", "")
            headers["X-Messpunkt"]  = mp
            headers["Authorization"] = f"Bearer {token}"

        return headers

    def _preprocess_response(self, raw: bytes) -> dict | list:
        """
        Converte la risposta grezza in dict/list Python.
        Sovrascrivere se la risposta non è JSON standard
        (es. Groupe E che ha timestamp con +0200 invece di +02:00).
        """
        import json
        return json.loads(raw)

    # ── fetch() — metodo principale ───────────────────────────────────────────

    async def fetch(self, target_date: date) -> NormalizedPrices:
        """
        Chiama l'API dell'EVU e ritorna i prezzi normalizzati per target_date.

        Args:
            target_date: giorno per cui richiedere i prezzi (es. domani)

        Returns:
            NormalizedPrices con tutti i PriceSlot in UTC

        Raises:
            AdapterNetworkError: errore di rete o HTTP
            AdapterParseError:   risposta non parsabile
            AdapterEmptyError:   nessun dato per il giorno richiesto
        """
        start_utc, end_utc = make_day_range_utc(target_date)
        url = self._build_url(start_utc, end_utc)
        headers = self._build_headers()
        fetched_at = utc_now()

        self._log.info(f"Fetch {target_date} → {url[:80]}...")

        raw_response = await self._http_get_with_retry(url, headers)
        response_data = self._preprocess_response(raw_response)

        slots = self._parse(response_data, target_date)

        if not slots:
            raise AdapterEmptyError(
                f"{self.tariff_id}: nessun slot ricevuto per {target_date}"
            )

        result = NormalizedPrices(
            tariff_id    = self.tariff_id,
            source_date  = target_date,
            fetched_at   = fetched_at,
            slots        = sorted(slots, key=lambda s: s.slot_start_utc),
            adapter_name = self.__class__.__name__,
            raw_url      = url,
        )

        if not result.is_complete:
            result.warnings.append(
                f"Slot count inatteso: {result.slot_count} "
                f"(attesi ~96 per un giorno normale)"
            )
            self._log.warning(result.warnings[-1])

        self._log.info(result.summary())
        return result

    # ── HTTP con retry ────────────────────────────────────────────────────────

    async def _http_get_with_retry(
        self,
        url: str,
        headers: dict[str, str],
    ) -> bytes:
        """GET con retry automatico su errori 5xx e timeout."""
        last_error: Optional[Exception] = None

        for attempt in range(1, self.MAX_RETRIES + 2):
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=self.TIMEOUT_SECONDS,
                ) as client:
                    r = await client.get(url, headers=headers)

                if r.status_code == 200:
                    return r.content

                if r.status_code in (401, 403):
                    raise AdapterNetworkError(
                        f"{self.tariff_id}: auth fallita ({r.status_code}) — "
                        f"verificare auth_config in evu_list.json"
                    )

                if r.status_code == 404:
                    raise AdapterNetworkError(
                        f"{self.tariff_id}: endpoint non trovato (404) — "
                        f"URL: {url}"
                    )

                if r.status_code >= 500:
                    last_error = AdapterNetworkError(
                        f"{self.tariff_id}: server error {r.status_code}"
                    )
                    self._log.warning(
                        f"Tentativo {attempt}/{self.MAX_RETRIES+1}: {r.status_code}"
                    )
                    if attempt <= self.MAX_RETRIES:
                        import asyncio
                        await asyncio.sleep(5 * attempt)
                    continue

                raise AdapterNetworkError(
                    f"{self.tariff_id}: HTTP {r.status_code}"
                )

            except httpx.TimeoutException as e:
                last_error = AdapterNetworkError(
                    f"{self.tariff_id}: timeout dopo {self.TIMEOUT_SECONDS}s"
                )
                self._log.warning(f"Tentativo {attempt}: timeout")
                if attempt <= self.MAX_RETRIES:
                    import asyncio
                    await asyncio.sleep(5 * attempt)

            except httpx.RequestError as e:
                raise AdapterNetworkError(
                    f"{self.tariff_id}: errore di rete — {e}"
                ) from e

        raise last_error or AdapterNetworkError(
            f"{self.tariff_id}: tutti i tentativi falliti"
        )

    # ── Helpers per i parser ──────────────────────────────────────────────────

    @staticmethod
    def rappen_to_chf(rappen: float) -> float:
        """Converte Rp/kWh → CHF/kWh (divide per 100)."""
        return round(rappen / 100, 8)

    @staticmethod
    def parse_utc_datetime(s: str) -> datetime:
        """
        Parsa una stringa datetime in UTC.
        Gestisce:
          - Groupe E: +0200 senza i due punti → +02:00
          - ISO 8601 "24:00" (fine giornata) → 00:00 del giorno successivo
          - Timestamp con Z (UTC esplicito)
        """
        import re

        s = s.strip()

        # Fix ISO 8601 "24:00" — rappresenta mezzanotte del giorno successivo
        # Es. "2026-10-25T24:00Z" → "2026-10-26T00:00Z"
        hour24_match = re.match(
            r'^(\d{4}-\d{2}-\d{2})T24:(\d{2})(.*)', s
        )
        if hour24_match:
            date_part = hour24_match.group(1)
            min_part  = hour24_match.group(2)
            tz_part   = hour24_match.group(3)
            # Avanza di un giorno
            from datetime import timedelta
            next_day = (datetime.fromisoformat(date_part) + timedelta(days=1)).date()
            s = f"{next_day}T00:{min_part}{tz_part}"

        # Fix Groupe E: +0200 → +02:00
        s_fixed = re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', s)

        try:
            dt = datetime.fromisoformat(s_fixed)
        except ValueError:
            try:
                from dateutil import parser as duparser
                dt = duparser.parse(s)
            except ImportError:
                raise AdapterParseError(
                    f"Impossibile parsare datetime: {s!r}"
                )

        return dt.astimezone(timezone.utc)

    @staticmethod
    def make_slots_from_hourly(
        hourly_prices: list[tuple[datetime, float | None, float | None, float | None]],
    ) -> list[PriceSlot]:
        """
        Espande prezzi orari in slot da 15 minuti.
        Usato per Primeo e CKW che hanno output 15min ma prezzi statici per ora.

        Args:
            hourly_prices: lista di (start_utc, energy, grid, residual)
                           con granularità oraria

        Returns:
            Lista di PriceSlot da 15 minuti (ogni ora → 4 slot identici)
        """
        from datetime import timedelta
        slots = []
        for start, energy, grid, residual in hourly_prices:
            for i in range(4):
                slots.append(PriceSlot(
                    slot_start_utc = start + timedelta(minutes=15 * i),
                    energy_price   = energy,
                    grid_price     = grid,
                    residual_price = residual,
                ))
        return slots

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.tariff_id})"