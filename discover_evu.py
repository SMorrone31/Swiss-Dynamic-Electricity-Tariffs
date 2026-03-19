"""
discover_evu.py
===============
Aggiorna automaticamente config/evu_list.json raccogliendo gli EVU svizzeri
con tariffe dinamiche da tre fonti:

  1. evcc GitHub templates   — CKW, EKZ con URL API già verificati
  2. Shiny scraper completo  — Playwright naviga tutti gli EVU e legge i dettagli
     Per ogni EVU: clicca l'EVU nel dropdown → clicca ogni card tariffa →
     legge il pannello d-panel (API URL, auth, orario, documentazione)
  3. Fallback hardcodato     — se Playwright non disponibile

Requisiti:
    pip install httpx playwright pyyaml beautifulsoup4
    playwright install chromium

Uso:
    python discover_evu.py              # aggiorna config/evu_list.json
    python discover_evu.py --dry-run    # stampa senza salvare
    python discover_evu.py --no-browser # solo evcc + fallback
"""

import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

OUTPUT_PATH   = Path(__file__).parent / "config" / "evu_list.json"
SHINY_URL     = "https://weisskopf.shinyapps.io/DynamischeTarife/"
EVCC_RAW_BASE = "https://raw.githubusercontent.com/evcc-io/evcc/master/templates/definition/tariff"
EVCC_CH_FILES = ["ckw.yaml", "ekz.yaml"]

EVCC_PROVIDER_MAP = {
    "CKW": "ckw_default",
    "EKZ": "ekz_default",
}

SHINY_EVU_FALLBACK = [
    {"label": "Aare Versorgungs AG (AVAG)",             "value": "1"},
    {"label": "Azienda Elettrica di Massagno (AEM) SA", "value": "2"},
    {"label": "Aziende industriali di Lugano AIL SA",   "value": "3"},
    {"label": "CKW",                                    "value": "4"},
    {"label": "EKZ",                                    "value": "5"},
    {"label": "EKZ Einsiedeln",                         "value": "6"},
    {"label": "Elektra Aettenschwil EGA",               "value": "7"},
    {"label": "Elektra Gretzenbach AG (ELAG)",          "value": "8"},
    {"label": "Groupe E",                               "value": "9"},
    {"label": "Primeo Energie",                         "value": "10"},
]
FALLBACK_DATE = "2026-03-15"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Fonte 1: evcc GitHub ──────────────────────────────────────────────────────

async def fetch_evcc_templates() -> list[dict]:
    results = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for filename in EVCC_CH_FILES:
            url = f"{EVCC_RAW_BASE}/{filename}"
            try:
                r = await client.get(url, timeout=15)
                r.raise_for_status()
                entries = _parse_evcc_template(filename, yaml.safe_load(r.text))
                results.extend(entries)
                log.info(f"[evcc] {filename}: {[e['tariff_id'] for e in entries]}")
            except Exception as e:
                log.warning(f"[evcc] Errore su {filename}: {e}")
    return results


def _parse_evcc_template(filename: str, data: dict) -> list[dict]:
    if not data:
        return []
    template  = filename.replace(".yaml", "")
    provider  = (data.get("product") or data.get("brand") or template).upper()
    api_url   = _evcc_api_url(data)
    auth_type = _evcc_auth_type(data)
    tariff_options = ["default"]
    for p in data.get("params", []):
        if p.get("name") in ("tariff", "tariff_name"):
            tariff_options = p.get("validValues", ["default"])
            break
    return [
        {
            "tariff_id":             f"{template}_{opt}".lower(),
            "tariff_name":           opt,
            "provider_name":         provider,
            "adapter_class":         f"{provider.capitalize()}Adapter",
            "api_base_url":          api_url,
            "auth_type":             auth_type,
            "auth_config":           {},
            "response_format":       "json",
            "daily_update_time_utc": "17:00",
            "zip_ranges":            [],
            "notes":                 f"Template evcc: {filename}",
            "source":                "evcc_github",
            "last_discovered_utc":   _now_utc(),
        }
        for opt in tariff_options
    ]


def _evcc_api_url(data: dict) -> str:
    for p in data.get("params", []):
        if p.get("name") in ("uri", "url"):
            return p.get("default", "")
    render = str(data.get("render", ""))
    urls = re.findall(r"https?://[^\s\"'<>{}|\\^\[\]]+", render)
    for u in urls:
        if any(k in u for k in ("api", "tariff", "preis", "dynamic")):
            return u
    return urls[0] if urls else ""


def _evcc_auth_type(data: dict) -> str:
    render = str(data.get("render", "")).lower()
    if any(k in render for k in ("oauth", "azure", "jwt")):        return "jwt_azure"
    if any(k in render for k in ("token", "apikey", "api_key")):   return "api_key"
    if any(k in render for k in ("username", "password")):         return "basic"
    return "none"


# ── Fonte 2: Shiny scraper completo ──────────────────────────────────────────

async def scrape_shiny_full() -> list[dict]:
    """
    Playwright naviga tutta la Shiny app:
      1. Intercetta f-vnb per la lista EVU
      2. Per ogni EVU: clicca nel dropdown
      3. Aspetta il pannello DETAILS con le card tariffe
      4. Per ogni card: clicca e legge i dettagli (API URL, auth, orario, ecc.)
      5. Ritorna lista completa con tutti i metadati
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("[Playwright] Non installato")
        return []

    all_entries: list[dict] = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()

            # Intercetta f-vnb
            fnvb_future: asyncio.Future = asyncio.get_event_loop().create_future()

            async def on_response(response):
                if "f-vnb" in response.url and not fnvb_future.done():
                    try:
                        data = json.loads(await response.text())
                        if isinstance(data, list) and data:
                            fnvb_future.set_result(data)
                    except Exception:
                        pass

            page.on("response", on_response)

            log.info(f"[Playwright] GET {SHINY_URL}")
            await page.goto(SHINY_URL, wait_until="domcontentloaded", timeout=20_000)

            # Accetta cookie se presenti
            for sel in ["button:has-text('Akzeptieren')", "button:has-text('Accept')"]:
                try:
                    if await page.locator(sel).first.is_visible(timeout=1000):
                        await page.locator(sel).first.click()
                        break
                except Exception:
                    pass

            # Aspetta lista EVU
            try:
                evu_list = await asyncio.wait_for(asyncio.shield(fnvb_future), timeout=20)
                log.info(f"[Playwright] {len(evu_list)} EVU trovati")
            except asyncio.TimeoutError:
                log.error("[Playwright] f-vnb timeout")
                await browser.close()
                return []

            # Per ogni EVU — naviga e scrapa i dettagli
            for evu in evu_list:
                name  = evu.get("label", "").strip()
                value = str(evu.get("value", ""))
                if not name:
                    continue

                log.info(f"[Playwright] Scraping: {name}")
                entries = await _scrape_evu(page, name, value)
                all_entries.extend(entries)

                # Piccola pausa per non stressare il server
                await asyncio.sleep(1)

            await browser.close()

    except Exception as e:
        log.error(f"[Playwright] Errore generale: {e}")

    log.info(f"[Playwright] Totale tariffe scrapate: {len(all_entries)}")
    return all_entries


async def _scrape_evu(page, evu_name: str, evu_value: str) -> list[dict]:
    """
    Per un singolo EVU:
      1. Seleziona nel dropdown
      2. Aspetta il pannello con le card tariffe
      3. Per ogni card: clicca e legge i dettagli
    """
    entries = []

    try:
        # Clicca sul campo di ricerca e seleziona l'EVU
        await page.click("#f-vnb-selectized")
        await asyncio.sleep(0.3)

        # Clicca sull'opzione con il data-value corretto
        option_sel = f".selectize-dropdown-content .option[data-value='{evu_value}']"
        await page.wait_for_selector(option_sel, timeout=5000)
        await page.click(option_sel)
        await asyncio.sleep(0.5)

        # Aspetta che appaia il pannello DETAILS con le card
        await page.wait_for_selector("#d-panel .plz-card-link, #d-panel .section", timeout=8000)

        # Conta le card disponibili (d-go_1, d-go_2, ...)
        cards = await page.query_selector_all("#d-panel .plz-card-link")
        n_cards = len(cards)
        log.info(f"  {evu_name}: {n_cards} tariffa/e trovata/e")

        if n_cards == 0:
            # Nessuna card — pannello già mostra i dettagli diretti
            html = await page.inner_html("#d-panel")
            details = _parse_detail_panel(html, evu_name)
            if details.get("api_base_url"):
                entries.append(_build_scraped_entry(evu_name, evu_value, details))
            return entries

        # Per ogni card, clicca e leggi i dettagli
        for i in range(1, n_cards + 1):
            card_id = f"#d-go_{i}"
            try:
                await page.click(card_id)
                await asyncio.sleep(0.5)

                # Aspetta che il pannello si aggiorni con i dettagli (sezione API-INFOS)
                try:
                    await page.wait_for_selector("#d-panel .section", timeout=6000)
                except Exception:
                    pass

                html = await page.inner_html("#d-panel")
                details = _parse_detail_panel(html, evu_name)
                log.info(f"  Card {i}: {details.get('tariff_name', '?')} — API: {details.get('api_base_url', '(vuota)')[:60]}")
                entries.append(_build_scraped_entry(evu_name, evu_value, details, card_index=i))

                # Torna alla lista card (click sul browser back o ri-seleziona l'EVU)
                await page.click("#f-vnb-selectized")
                await asyncio.sleep(0.3)
                await page.click(f".selectize-dropdown-content .option[data-value='{evu_value}']")
                await asyncio.sleep(0.5)
                await page.wait_for_selector("#d-panel .plz-card-link", timeout=6000)

            except Exception as e:
                log.warning(f"  Errore card {i} di {evu_name}: {e}")
                continue

    except Exception as e:
        log.warning(f"[Playwright] Errore EVU {evu_name}: {e}")
        # Entry minima senza dettagli
        entries.append(_build_shiny_entry(evu_name, evu_value))

    return entries if entries else [_build_shiny_entry(evu_name, evu_value)]


def _parse_detail_panel(html: str, evu_name: str) -> dict:
    """
    Parsa il pannello d-panel e estrae:
    - tariff_name (da firm-under-logo)
    - api_base_url (da code.copycode o dt API-URL)
    - auth_type (da dd Authentifizierung)
    - daily_update_time_utc (da dd Publikationszeitpunkt)
    - doc_url (da a Dokumentation)
    - tags (SmartGridready, Wahltarif, Pflichttarif, ...)
    - valid_from (da Gültigkeit)
    - tariff_type (da Dynamische Elemente)
    - for_whom (da Für wen)
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict = {}

    # Tariff name
    name_el = soup.select_one(".firm-under-logo")
    if name_el:
        result["tariff_name"] = name_el.get_text(strip=True)

    # Tags
    tags = [t.get_text(strip=True) for t in soup.select(".labeltag")]
    result["tags"] = tags

    # Legge tutte le coppie dt/dd dal pannello
    kv: dict[str, str] = {}
    for dl in soup.select("dl.kv2"):
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            key = dt.get_text(strip=True).lower()
            val = dd.get_text(strip=True)
            kv[key] = val

            # API URL — cerca anche il tag <code class="copycode">
            code = dd.select_one("code.copycode")
            if code:
                kv[key + "_raw"] = code.get_text(strip=True)

    # API URL
    api_url = ""
    for k in ("api-url", "api-url_raw", "api url", "url"):
        if k in kv and kv[k].startswith("http"):
            api_url = kv[k]
            break
    # Cerca anche data-copy sui bottoni (più affidabile)
    copy_btn = soup.select_one(".copybtn[data-copy]")
    if copy_btn and copy_btn.get("data-copy", "").startswith("http"):
        api_url = copy_btn["data-copy"]
    result["api_base_url"] = api_url

    # Auth type
    auth_raw = kv.get("authentifizierung", "").lower()
    if not auth_raw or auth_raw == "—" or "öffentlich" in auth_raw or "public" in auth_raw:
        result["auth_type"] = "none"
    elif "token" in auth_raw or "api key" in auth_raw:
        result["auth_type"] = "api_key"
    elif "oauth" in auth_raw or "azure" in auth_raw:
        result["auth_type"] = "jwt_azure"
    else:
        result["auth_type"] = "unknown"

    # Orario pubblicazione → converti in UTC
    pub_raw = kv.get("publikationszeitpunkt", "")
    result["publication_time_raw"] = pub_raw
    result["daily_update_time_utc"] = _parse_publication_time(pub_raw)

    # Documentazione URL
    doc_link = soup.select_one("a.alink[href*='pdf'], a.alink[href*='doc']")
    result["doc_url"] = doc_link["href"] if doc_link else ""

    # Valido da
    result["valid_from"] = kv.get("gültigkeit", "")

    # Für wen
    result["for_whom"] = kv.get("für wen ist der tarif?", "")

    # Elementi dinamici
    result["dynamic_elements"] = kv.get("dynamische elemente", "")

    # Zugriffsverfahren
    result["access_method"] = kv.get("zugriffsverfahren", "")

    return result


def _parse_publication_time(raw: str) -> str:
    """
    Converte 'bis 18:00 Uhr am Vortag' → '17:00' (UTC, assumendo CET/CEST).
    In estate (CEST = UTC+2): 18:00 → 16:00 UTC
    In inverno (CET = UTC+1):  18:00 → 17:00 UTC
    Usiamo 17:00 UTC come valore conservativo (orario invernale).
    """
    if not raw:
        return "17:00"
    m = re.search(r"(\d{1,2})[:\.](\d{2})", raw)
    if not m:
        return "17:00"
    hour = int(m.group(1))
    # Sottrai 1 ora (CET = UTC+1, orario invernale — più conservativo)
    utc_hour = max(0, hour - 1)
    return f"{utc_hour:02d}:00"


def _build_scraped_entry(
    evu_name: str,
    shiny_value: str,
    details: dict,
    card_index: int = 1,
) -> dict:
    """Costruisce una entry completa dai dettagli scrapati."""
    evcc_tid = EVCC_PROVIDER_MAP.get(evu_name.upper())
    if evcc_tid and card_index == 1:
        tid = evcc_tid
    else:
        tariff_slug = re.sub(r"[^a-z0-9]", "_", (details.get("tariff_name") or evu_name).lower()).strip("_")
        tid = f"sgr_{re.sub(r'[^a-z0-9]', '_', evu_name.lower()).strip('_')}_{tariff_slug}"
        tid = re.sub(r"_+", "_", tid).strip("_")

    # Note ricche con tutti i metadati trovati
    notes_parts = []
    if details.get("for_whom"):
        notes_parts.append(f"Per: {details['for_whom']}")
    if details.get("valid_from"):
        notes_parts.append(f"Valido: {details['valid_from']}")
    if details.get("dynamic_elements"):
        notes_parts.append(f"Elementi dinamici: {details['dynamic_elements']}")
    if details.get("publication_time_raw"):
        notes_parts.append(f"Pubblicazione: {details['publication_time_raw']}")
    if details.get("doc_url"):
        notes_parts.append(f"Docs: {details['doc_url']}")
    if details.get("tags"):
        notes_parts.append(f"Tags: {', '.join(details['tags'])}")

    return {
        "tariff_id":             tid,
        "tariff_name":           details.get("tariff_name", "dynamic"),
        "provider_name":         evu_name,
        "shiny_data_value":      int(shiny_value) if shiny_value.isdigit() else shiny_value,
        "adapter_class":         _adapter_class(evu_name),
        "api_base_url":          details.get("api_base_url", ""),
        "auth_type":             details.get("auth_type", "unknown"),
        "auth_config":           {},
        "response_format":       "json",
        "daily_update_time_utc": details.get("daily_update_time_utc", "17:00"),
        "zip_ranges":            [],
        "valid_from":            details.get("valid_from", ""),
        "for_whom":              details.get("for_whom", ""),
        "dynamic_elements":      details.get("dynamic_elements", ""),
        "doc_url":               details.get("doc_url", ""),
        "tags":                  details.get("tags", []),
        "notes":                 " | ".join(notes_parts) if notes_parts else "Scrapato da weisskopf.shinyapps.io",
        "source":                "shiny_scraped",
        "last_discovered_utc":   _now_utc(),
    }


def _build_shiny_entry(name: str, shiny_value: str) -> dict:
    evcc_tid = EVCC_PROVIDER_MAP.get(name.upper())
    tid = evcc_tid if evcc_tid else f"sgr_{re.sub(r'[^a-z0-9]', '_', name.lower()).strip('_')}"
    return {
        "tariff_id":             tid,
        "tariff_name":           "dynamic",
        "provider_name":         name,
        "shiny_data_value":      int(shiny_value) if shiny_value.isdigit() else shiny_value,
        "adapter_class":         _adapter_class(name),
        "api_base_url":          "",
        "auth_type":             "unknown",
        "auth_config":           {},
        "response_format":       "unknown",
        "daily_update_time_utc": "17:00",
        "zip_ranges":            [],
        "notes":                 "Dettagli non scrapati — riprovare manualmente",
        "source":                "shiny_weisskopf",
        "last_discovered_utc":   _now_utc(),
    }


# ── Fonte 3: Fallback ─────────────────────────────────────────────────────────

def get_fallback_entries() -> list[dict]:
    log.info(f"[Fallback] Uso lista hardcodata (verificata il {FALLBACK_DATE})")
    return [_build_shiny_entry(o["label"], o["value"]) for o in SHINY_EVU_FALLBACK]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _adapter_class(name: str) -> str:
    return (
        "".join(w.capitalize() for w in re.sub(r"[^a-zA-Z0-9]", " ", name).split())
        + "Adapter"
    )


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Merge ─────────────────────────────────────────────────────────────────────

def merge(existing: list[dict], new_entries: list[dict]) -> list[dict]:
    by_id = {e["tariff_id"]: e for e in existing}
    added = updated = 0
    for entry in new_entries:
        tid = entry["tariff_id"]
        if tid in by_id:
            ex = by_id[tid]
            ex["last_discovered_utc"] = entry["last_discovered_utc"]
            # Aggiorna i campi che potrebbero essere migliorati dallo scraping
            for field in ("api_base_url", "auth_type", "daily_update_time_utc",
                          "doc_url", "valid_from", "for_whom", "dynamic_elements",
                          "tags", "zip_ranges"):
                # Aggiorna solo se il nuovo valore è migliore (non vuoto)
                new_val = entry.get(field)
                old_val = ex.get(field)
                if new_val and not old_val:
                    ex[field] = new_val
                # api_base_url: aggiorna anche se era vuoto
                if field == "api_base_url" and new_val and new_val != old_val:
                    ex[field] = new_val
            # Aggiungi shiny_data_value se mancante
            if "shiny_data_value" in entry and "shiny_data_value" not in ex:
                ex["shiny_data_value"] = entry["shiny_data_value"]
            # Source: se era shiny_weisskopf e ora è shiny_scraped, aggiorna
            if entry.get("source") == "shiny_scraped":
                ex["source"] = "shiny_scraped"
            updated += 1
        else:
            by_id[tid] = entry
            log.info(f"  + Nuovo: {entry['provider_name']} / {entry['tariff_id']}")
            added += 1
    log.info(f"Merge: +{added} nuovi, ~{updated} aggiornati, {len(by_id)} totale")
    return sorted(by_id.values(), key=lambda x: (x["provider_name"], x["tariff_id"]))


def load(path: Path) -> list[dict]:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        log.info(f"Caricati {len(data)} EVU da {path}")
        return data
    return []


def save(path: Path, data: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"Salvati {len(data)} EVU in {path}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    dry_run    = "--dry-run"    in sys.argv
    no_browser = "--no-browser" in sys.argv

    log.info("=" * 55)
    log.info("  Swiss EVU Discovery  |  " + _now_utc())
    log.info("=" * 55)

    existing = load(OUTPUT_PATH)
    all_new: list[dict] = []

    log.info("--- Fonte 1: evcc GitHub templates ---")
    all_new.extend(await fetch_evcc_templates())

    if not no_browser:
        log.info("--- Fonte 2: Shiny scraper completo ---")
        scraped = await scrape_shiny_full()
        if scraped:
            all_new.extend(scraped)
        else:
            log.warning("[Shiny] Scraping fallito — uso fallback")
            all_new.extend(get_fallback_entries())
    else:
        log.info("--- Fonte 2: Fallback hardcodato ---")
        all_new.extend(get_fallback_entries())

    merged = merge(existing, all_new)

    if dry_run:
        print(json.dumps(merged, indent=2, ensure_ascii=False))
    else:
        save(OUTPUT_PATH, merged)

    log.info(f"  Fine. Totale tariffe: {len(merged)}")


if __name__ == "__main__":
    asyncio.run(main())