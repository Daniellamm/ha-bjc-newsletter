"""Coordinator for the BJC Newsletter integration.

Polls the BJC homepage hourly for a new newsletter, fetches and compresses
the PDF, then sends it to Gemini AI to extract the weekly schedule as markdown.
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BJC_FLIPSNACK_ACCOUNT,
    BJC_HOMEPAGE_URL,
    CACHE_FILENAME,
    CONF_GEMINI_API_KEY,
    CONF_GEMINI_MODEL,
    DATA_LAST_CHECKED,
    DATA_LAST_ERROR,
    DATA_LAST_PROCESSED,
    DATA_NEWSLETTER_URL,
    DATA_SCHEDULE,
    DATA_STATUS,
    DEFAULT_GEMINI_MODEL,
    DOMAIN,
    FLIPSNACK_FULLVIEW_PATTERN,
    FLIPSNACK_PDF_PATTERN,
    OPT_BROWSERBASE_API_KEY,
    OPT_BROWSERBASE_PROJECT_ID,
    OPT_CURRENT_NEWSLETTER_URL,
    OPT_LAST_ATTEMPTED_URL,
    OPT_LAST_CHECKED,
    OPT_LAST_PROCESSED,
    PDF_IMAGE_QUALITY,
    PDF_WATCH_FOLDER,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_PROCESSING,
    STATUS_READY,
    UPDATE_INTERVAL_HOURS,
)

_LOGGER = logging.getLogger(__name__)

GEMINI_PROMPT = """\
You are processing a weekly synagogue newsletter PDF for Boca Jewish Center (BJC).

CRITICAL INSTRUCTION: You MUST extract EVERY SINGLE DAY listed in the schedule section \
of this newsletter. Do not stop early. Do not truncate. Output every day completely, \
from the first day to the last day shown in the PDF.

Your task: find the SCHEDULE section of the newsletter (it may be labeled "Schedule", \
"Weekly Schedule", "Zmanim", or similar). This section contains a day-by-day list of \
prayer services and events with specific times. Extract ALL of it — every day, \
every event, every time listed.

Do NOT limit yourself to the announcements section. The schedule section has specific \
times like "Shacharis: 7:30am" — that is what you must extract for every single day.

Format rules:
- Use a level-2 heading (##) for each day, ALWAYS including the full 4-digit year: \
e.g., ## Sunday, April 6, 2026
- Include any holiday name or description after the date: e.g., ## Sunday, April 6, 2026: Chol Hamoed
- Use bullet points for each item under that day
- Include: event name, time (if shown), location (if shown), and any relevant notes
- If a section covers the entire week, put it under ## Weekly Announcements
- Do not add commentary, preamble, or closing remarks — output only the Markdown schedule

EXCLUDE the following — do not include them in the output:
- Yahrzeit (memorial) notices or lists
- Sponsorship announcements or dedications
- Advertisements or donor recognition

Today's date for reference: {today}
The newsletter covers the week of: {week_label}
Current year: {year}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug_from_url(url: str) -> str:
    """Extract the publication slug from a Flipsnack URL.

    e.g. https://www.flipsnack.com/7BBDB688B7A/pesach-2026-_-5786
    → 'pesach-2026-_-5786'
    """
    url = url.rstrip("/")
    if url.endswith("/full-view.html"):
        url = url[: -len("/full-view.html")]
    return url.rsplit("/", 1)[-1]


def _week_label_from_slug(slug: str) -> str:
    """Convert a Flipsnack slug to a human-readable week label.

    e.g. 'pesach-2026-_-5786' → 'Pesach 2026 5786'
    """
    label = re.sub(r"[-_]+", " ", slug).strip().title()
    return label


def _parse_schedule_from_markdown(markdown: str) -> dict[str, str]:
    """Split Gemini markdown output into {date_isoformat: section_markdown}.

    Falls back to 'weekly' key when headings can't be parsed as dates.
    If no date-keyed sections are found, seeds all 7 days of the current week.
    """
    from dateutil import parser as dateutil_parser

    # Use current year as the default so headings without a year parse correctly
    current_year = date.today().year
    year_default = datetime(current_year, 1, 1)

    schedule: dict[str, str] = {}
    # Split on ## headings, keeping the heading as part of its section
    sections = re.split(r"(?=^## )", markdown.strip(), flags=re.MULTILINE)

    for section in sections:
        section = section.strip()
        if not section:
            continue
        first_line = section.split("\n", 1)[0]
        heading_text = first_line.lstrip("#").strip()

        try:
            # Strip subtitle after colon (e.g. "Sunday, April 5, 2026: Chol Hamoed" → "Sunday, April 5, 2026")
            date_text = heading_text.split(":")[0].strip()
            parsed = dateutil_parser.parse(date_text, fuzzy=True, default=year_default)
            # Reject if the parsed year is more than 1 year off — means dateutil guessed badly
            if abs(parsed.year - current_year) > 1:
                raise ValueError(f"Year {parsed.year} too far from current year {current_year}")
            day_key = parsed.date().isoformat()
            schedule[day_key] = section
        except (ValueError, OverflowError):
            existing = schedule.get("weekly", "")
            schedule["weekly"] = (existing + "\n\n" + section).strip() if existing else section

    # If nothing was date-keyed, seed the entire week with the full content
    has_date_keys = any(k != "weekly" for k in schedule)
    if not has_date_keys:
        schedule["weekly"] = markdown
        today = date.today()
        # Seed Sunday–Saturday of the current week
        days_since_sunday = (today.weekday() + 1) % 7
        week_start = today - timedelta(days=days_since_sunday)
        for i in range(7):
            day = week_start + timedelta(days=i)
            schedule.setdefault(day.isoformat(), markdown)

    return schedule


def _browserbase_fetch_sync(
    slug: str, api_key: str, project_id: str
) -> bytes | None:
    """Fetch the Flipsnack newsletter text via Browserbase cloud browser (synchronous).

    Browserbase runs a real Chromium browser in their cloud. We connect to it via
    Chrome DevTools Protocol (CDP) using the Playwright sync API — no local browser
    binary is needed, so this works on HA Green and other restricted environments.

    The browser loads the Flipsnack full-view page, which triggers an authenticated
    CloudFront request for data.json. We intercept that response to extract
    ``extractedText`` (SEO-indexed page text) without downloading any images.

    Returns UTF-8 encoded plain text (not a PDF) on success, None on failure.
    Must be called from an executor thread.
    """
    import json as _json
    import urllib.request as _urlreq
    import urllib.error as _urlerr

    try:
        from browserbase import Browserbase
    except ImportError:
        _LOGGER.warning(
            "browserbase package not installed — cannot use cloud browser fetch. "
            "Ensure 'browserbase>=0.3.0' is in manifest.json requirements."
        )
        return None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _LOGGER.warning(
            "playwright package not installed — cannot use cloud browser fetch. "
            "Ensure 'playwright>=1.40.0' is in manifest.json requirements."
        )
        return None

    full_view_url = (
        f"https://www.flipsnack.com/{BJC_FLIPSNACK_ACCOUNT}/{slug}/full-view.html"
    )
    _LOGGER.debug("Browserbase: starting session for slug '%s'", slug)

    try:
        bb = Browserbase(api_key=api_key)
        session = bb.sessions.create(project_id=project_id)
    except Exception as err:
        _LOGGER.warning("Browserbase: could not create session: %s", err)
        return None

    captured: dict = {}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(session.connect_url)
            context = browser.contexts[0]
            page = context.new_page()

            def on_response(response):
                if "data.json" in response.url and response.status == 200:
                    try:
                        captured["url"] = response.url
                        captured["data"] = response.json()
                    except Exception:
                        pass

            page.on("response", on_response)
            page.goto(full_view_url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(3000)
            browser.close()
    except Exception as err:
        _LOGGER.warning("Browserbase: browser session error: %s", err)
        return None

    if "data" not in captured:
        _LOGGER.warning("Browserbase: data.json was not captured from %s", full_view_url)
        return None

    data: dict = captured["data"]

    # --- Page count ---
    toc = data.get("toc", [])
    toc_pages = toc[0].get("sub", []) if toc else []
    page_count = len(toc_pages) if toc_pages else 0
    pages_info = data.get("pages", {})
    if page_count == 0:
        page_count = len(pages_info.get("order", []))

    # --- Item hash ---
    item_hash = toc[0].get("originalHash") if toc else None
    if not item_hash:
        pages_data_map = pages_info.get("data", {})
        order = pages_info.get("order", [])
        if order and order[0] in pages_data_map:
            item_hash = pages_data_map[order[0]].get("source", {}).get("hash")

    # --- Download page images and assemble PDF ---
    if not item_hash or page_count == 0:
        _LOGGER.warning(
            "Browserbase: could not determine item hash or page count — giving up"
        )
        return None

    qs_match = re.search(r"\?(.+)$", captured["url"])
    if not qs_match:
        _LOGGER.warning("Browserbase: no signed query string found in data.json URL")
        return None

    signed_qs = qs_match.group(1)
    cdn_base = captured["url"].split("/data.json")[0]

    dl_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.flipsnack.com/",
    }

    page_images: list[bytes] = []
    for page_num in range(1, page_count + 1):
        img_data = None
        for size in ("large", "medium", "small"):
            img_url = f"{cdn_base}/items/{item_hash}/covers/page_{page_num}/{size}?{signed_qs}"
            try:
                req = _urlreq.Request(img_url, headers=dl_headers)
                with _urlreq.urlopen(req, timeout=30) as resp:
                    img_data = resp.read()
                break
            except _urlerr.HTTPError:
                continue
            except Exception:
                continue
        if img_data:
            page_images.append(img_data)
        else:
            _LOGGER.debug("Browserbase: could not download page %d of %d", page_num, page_count)

    if not page_images:
        _LOGGER.warning("Browserbase: no page images downloaded — giving up")
        return None

    try:
        import io as _io
        from PIL import Image as _PilImage

        pil_images = [_PilImage.open(_io.BytesIO(b)).convert("RGB") for b in page_images]
        out = _io.BytesIO()
        pil_images[0].save(
            out, format="PDF", save_all=True, append_images=pil_images[1:], resolution=150
        )
        _LOGGER.info(
            "Browserbase: assembled %d-page PDF for slug '%s'", len(page_images), slug
        )
        return out.getvalue()
    except Exception as err:
        _LOGGER.error("Browserbase: PDF assembly failed: %s", err)
        return None




def _extract_pdf_url_from_flipsnack_page(html: str) -> str | None:
    """Search a Flipsnack viewer page's source for the real CDN PDF URL.

    Flipsnack embeds book data as JSON in script tags. This function extracts
    the PDF download URL using a series of patterns that cover their various
    page formats (Nuxt, Next.js, legacy).
    Returns the first plausible PDF URL found, or None.
    """
    # JSON key patterns Flipsnack uses for the PDF URL
    json_key_patterns = [
        r'"pdfUrl"\s*:\s*"([^"]+)"',
        r'"pdfSrc"\s*:\s*"([^"]+)"',
        r'"pdf_url"\s*:\s*"([^"]+)"',
        r'"downloadUrl"\s*:\s*"([^"]+\.pdf[^"]*)"',
        r'"source"\s*:\s*"([^"]+\.pdf[^"]*)"',
        r'"fileUrl"\s*:\s*"([^"]+\.pdf[^"]*)"',
        r'"originalPdf"\s*:\s*"([^"]+)"',
    ]
    for pattern in json_key_patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            url = m.group(1).replace("\\/", "/")
            if url.startswith("http"):
                return url

    # Fallback: look for any HTTPS URL ending in .pdf from known CDN domains
    cdn_pattern = r'https://(?:[^"\'<\s]*(?:amazonaws|cloudfront|flipsnack|s3)[^"\'<\s]*\.pdf[^"\'<\s]*)'
    m = re.search(cdn_pattern, html, re.IGNORECASE)
    if m:
        return m.group(0).rstrip("\\")

    return None


def _compress_pdf(pdf_bytes: bytes) -> tuple[bytes, str]:
    """Compress the PDF for Gemini upload while preserving visual layout.

    Returns (compressed_bytes, method) where method is one of:
      'pikepdf'   — image-recompressed PDF (reduced size, layout intact)
      'original'  — original PDF bytes unchanged (pikepdf failed)

    Text extraction is intentionally NOT used: the visual page layout is
    required so Gemini can correctly associate events with their day columns.
    The function never raises; on any failure it falls back to 'original'.
    """
    # pikepdf image downsampling — preserves all text, layout, and structure
    try:
        import pikepdf
        from PIL import Image

        src = pikepdf.open(io.BytesIO(pdf_bytes))
        out_buf = io.BytesIO()
        images_processed = 0

        for page in src.pages:
            for name, raw_obj in page.images.items():
                try:
                    # Decode image preserving full resolution — resize is intentionally
                    # skipped because the schedule text requires full-size pixels to be
                    # readable by Gemini. Only JPEG quality is reduced (43MB → ~4MB).
                    pil_img = pikepdf.PdfImage(raw_obj).as_pil_image()

                    jpeg_buf = io.BytesIO()
                    if pil_img.mode in ("RGBA", "P", "L"):
                        pil_img = pil_img.convert("RGB")
                    pil_img.save(jpeg_buf, format="JPEG", quality=PDF_IMAGE_QUALITY, optimize=True)
                    raw_obj.write(jpeg_buf.getvalue(), filter=pikepdf.Name("/DCTDecode"))
                    images_processed += 1
                except Exception as img_err:
                    _LOGGER.debug("Skipping image %s in pikepdf: %s", name, img_err)

        src.save(out_buf)
        compressed = out_buf.getvalue()
        reduction_pct = 100 * (1 - len(compressed) / len(pdf_bytes))
        _LOGGER.info(
            "pikepdf compression: %d MB → %d MB (%.0f%% reduction, %d images)",
            len(pdf_bytes) // 1_000_000,
            len(compressed) // 1_000_000,
            reduction_pct,
            images_processed,
        )
        return compressed, "pikepdf"
    except Exception as err:
        _LOGGER.warning("pikepdf compression failed, using original PDF: %s", err)

    _LOGGER.info("Using original PDF (%d MB)", len(pdf_bytes) // 1_000_000)
    return pdf_bytes, "original"


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class BJCNewsletterCoordinator(DataUpdateCoordinator):
    """Polls BJC for newsletter changes and processes new editions with Gemini."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=UPDATE_INTERVAL_HOURS),
        )
        self._entry = entry
        self._session: aiohttp.ClientSession | None = None
        self._cache_path = Path(hass.config.path(CACHE_FILENAME))
        self._watch_folder = Path(hass.config.path(PDF_WATCH_FOLDER))

        # Initialize with empty data; cache is loaded asynchronously in async_setup_entry
        # via async_load_from_cache() to avoid blocking the event loop.
        self.data = self._empty_data()

    def _empty_data(self) -> dict:
        return {
            DATA_SCHEDULE: {},
            DATA_STATUS: STATUS_IDLE,
            DATA_NEWSLETTER_URL: self._entry.options.get(OPT_CURRENT_NEWSLETTER_URL, ""),
            DATA_LAST_PROCESSED: self._entry.options.get(OPT_LAST_PROCESSED, ""),
            DATA_LAST_CHECKED: self._entry.options.get(OPT_LAST_CHECKED, ""),
            DATA_LAST_ERROR: "",
        }

    async def async_load_from_cache(self) -> None:
        """Load persisted schedule from disk without blocking the event loop."""
        self.data = await self.hass.async_add_executor_job(self._load_cache)

    # ------------------------------------------------------------------
    # Cache I/O
    # ------------------------------------------------------------------

    def _load_cache(self) -> dict[str, Any]:
        """Load persisted schedule from disk. Returns safe default on failure.

        Must be called from an executor thread, not the event loop.
        """
        default = self._empty_data()
        if not self._cache_path.exists():
            return default
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            return {
                DATA_SCHEDULE: raw.get("schedule", {}),
                DATA_STATUS: STATUS_READY if raw.get("schedule") else STATUS_IDLE,
                DATA_NEWSLETTER_URL: raw.get("newsletter_url", default[DATA_NEWSLETTER_URL]),
                DATA_LAST_PROCESSED: raw.get("last_processed", default[DATA_LAST_PROCESSED]),
                DATA_LAST_CHECKED: raw.get("last_checked", default[DATA_LAST_CHECKED]),
                DATA_LAST_ERROR: "",
            }
        except Exception:
            _LOGGER.warning("Failed to load BJC newsletter cache; starting fresh")
            return default

    def _save_cache(self, data: dict[str, Any]) -> None:
        """Persist schedule cache to JSON (synchronous, called via executor)."""
        payload = {
            "schedule": data.get(DATA_SCHEDULE, {}),
            "newsletter_url": data.get(DATA_NEWSLETTER_URL, ""),
            "last_processed": data.get(DATA_LAST_PROCESSED, ""),
            "last_checked": data.get(DATA_LAST_CHECKED, ""),
        }
        try:
            self._cache_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as err:
            _LOGGER.error("Failed to write BJC newsletter cache: %s", err)

    # ------------------------------------------------------------------
    # Main update loop
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Called every hour by the coordinator framework."""
        if self._session is None:
            self._session = async_get_clientsession(self.hass)

        now_iso = datetime.now().isoformat(timespec="seconds")

        # Step 1: find the current newsletter URL on the BJC homepage
        try:
            current_url = await self._fetch_newsletter_url()
        except Exception as err:
            raise UpdateFailed(f"Cannot reach BJC homepage: {err}") from err

        existing_data = dict(self.data or {})
        existing_data[DATA_LAST_CHECKED] = now_iso

        # Step 2: change detection — check both successfully processed URL and last attempted URL
        stored_url = self._entry.options.get(OPT_CURRENT_NEWSLETTER_URL, "")
        last_attempted = self._entry.options.get(OPT_LAST_ATTEMPTED_URL, "")
        if current_url and (current_url == stored_url or current_url == last_attempted):
            _LOGGER.debug("BJC newsletter unchanged: %s", current_url)
            await self.hass.async_add_executor_job(self._save_cache, existing_data)
            self.hass.config_entries.async_update_entry(
                self._entry,
                options={**self._entry.options, OPT_LAST_CHECKED: now_iso},
            )
            return existing_data

        # Step 3: new newsletter detected
        _LOGGER.info("New BJC newsletter detected: %s", current_url)
        existing_data[DATA_STATUS] = STATUS_PROCESSING
        existing_data[DATA_NEWSLETTER_URL] = current_url
        self.async_set_updated_data(existing_data)  # notify sensors of processing state

        slug = _slug_from_url(current_url)
        week_label = _week_label_from_slug(slug)

        # Step 3b: fetch PDF — try remote first, then fall back to watch folder
        pdf_bytes = await self._try_fetch_pdf(slug)
        if pdf_bytes is None:
            pdf_bytes = await self.hass.async_add_executor_job(
                self._check_watch_folder, existing_data.get(DATA_LAST_PROCESSED, "")
            )

        # Sanity-check: must be a real PDF
        if pdf_bytes and pdf_bytes[:4] != b"%PDF":
            _LOGGER.warning(
                "Fetched file for '%s' does not start with %%PDF magic bytes — discarding, will retry next poll",
                slug,
            )
            pdf_bytes = None

        # Step 3c: send to Gemini
        try:
            markdown = await self._process_with_gemini(pdf_bytes, week_label, current_url)
        except Exception as err:
            _LOGGER.error("Gemini processing failed for %s: %s", slug, err)
            existing_data[DATA_STATUS] = STATUS_ERROR
            existing_data[DATA_LAST_ERROR] = str(err)
            existing_data[DATA_LAST_CHECKED] = now_iso
            await self.hass.async_add_executor_job(self._save_cache, existing_data)
            # Record the attempted URL so the next hourly poll doesn't retry immediately
            # and cause a 429 retry storm. The entry stays in ERROR state until the URL
            # changes (new newsletter) or the user manually reconfigures.
            self.hass.config_entries.async_update_entry(
                self._entry,
                options={
                    **self._entry.options,
                    OPT_LAST_ATTEMPTED_URL: current_url,
                    OPT_LAST_CHECKED: now_iso,
                },
            )
            return existing_data

        # Step 3d-e: parse and merge schedule
        new_schedule = _parse_schedule_from_markdown(markdown)
        merged = {**(existing_data.get(DATA_SCHEDULE) or {}), **new_schedule}

        # Step 3f-i: build final data and persist
        processed_iso = datetime.now().isoformat(timespec="seconds")
        final_data: dict[str, Any] = {
            DATA_SCHEDULE: merged,
            DATA_STATUS: STATUS_READY,
            DATA_NEWSLETTER_URL: current_url,
            DATA_LAST_PROCESSED: processed_iso,
            DATA_LAST_CHECKED: now_iso,
            DATA_LAST_ERROR: "",
        }

        await self.hass.async_add_executor_job(self._save_cache, final_data)
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={
                **self._entry.options,
                OPT_CURRENT_NEWSLETTER_URL: current_url,
                OPT_LAST_PROCESSED: processed_iso,
                OPT_LAST_CHECKED: now_iso,
            },
        )
        return final_data

    # ------------------------------------------------------------------
    # BJC homepage scraping
    # ------------------------------------------------------------------

    async def _fetch_newsletter_url(self) -> str:
        """Scrape the BJC homepage and return the current newsletter Flipsnack URL."""
        from bs4 import BeautifulSoup

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }

        async with self._session.get(
            BJC_HOMEPAGE_URL,
            timeout=aiohttp.ClientTimeout(total=30),
            headers=headers,
        ) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"BJC homepage returned HTTP {resp.status}")
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        account = BJC_FLIPSNACK_ACCOUNT

        # Slugs that indicate non-newsletter content — never pick these
        EXCLUDE = {"calendar", "annual-report", "yizkor", "haggadah", "bulletin"}

        # Slugs that indicate a supplemental one-off (lower priority than main newsletter)
        SUPPLEMENT_HINTS = {"schedule", "supplement", "special", "flyer", "announcement"}

        def _clean(href: str) -> str:
            return href.split("?")[0].rstrip("/")

        def _is_bjc_flipsnack(href: str) -> bool:
            return "flipsnack.com" in href and account in href

        def _is_excluded(href: str) -> bool:
            s = _slug_from_url(href).lower()
            return any(excl in s for excl in EXCLUDE)

        def _is_supplement(href: str) -> bool:
            s = _slug_from_url(href).lower()
            return any(hint in s for hint in SUPPLEMENT_HINTS)

        all_links = soup.find_all("a", href=True)

        # Strategy 1: find <h2> containing "BJC Insider" and get its flipsnack link
        for h2 in soup.find_all("h2"):
            if "bjc insider" in h2.get_text(strip=True).lower():
                parent = h2.parent
                if parent:
                    for a in parent.find_all("a", href=True):
                        href = a["href"]
                        if _is_bjc_flipsnack(href) and not _is_excluded(href):
                            _LOGGER.debug("Newsletter found via BJC Insider h2: %s", href)
                            return _clean(href)

        # Strategy 2: any link whose visible text is "READ IT NOW" or "READ NOW"
        READ_NOW_PHRASES = {"read it now", "read now", "click here to read", "view now"}
        for a in all_links:
            href = a["href"]
            if _is_bjc_flipsnack(href) and not _is_excluded(href):
                text = a.get_text(strip=True).lower()
                if any(phrase in text for phrase in READ_NOW_PHRASES):
                    _LOGGER.debug("Newsletter found via 'Read Now' CTA: %s", href)
                    return _clean(href)

        # Strategy 3: all non-excluded BJC flipsnack links — prefer main newsletters
        # over supplement-like slugs
        main_candidates = []
        supplement_candidates = []
        for a in all_links:
            href = a["href"]
            if _is_bjc_flipsnack(href) and not _is_excluded(href):
                url = _clean(href)
                if _is_supplement(href):
                    supplement_candidates.append(url)
                else:
                    main_candidates.append(url)

        if main_candidates:
            _LOGGER.debug("Newsletter found via main candidate scan: %s", main_candidates[0])
            return main_candidates[0]

        if supplement_candidates:
            _LOGGER.debug(
                "Newsletter found via supplement fallback (no main candidate): %s",
                supplement_candidates[0],
            )
            return supplement_candidates[0]

        raise UpdateFailed("Could not find BJC Insider newsletter link on homepage")

    # ------------------------------------------------------------------
    # PDF watch folder
    # ------------------------------------------------------------------

    def _check_watch_folder(self, last_processed_iso: str) -> bytes | None:
        """Scan the PDF watch folder for a PDF newer than the last processed time.

        The watch folder is at ``{HA config}/bjc_newsletter_pdfs/``.  Users
        download the newsletter PDF from Flipsnack via their browser and drop it
        there.  The integration picks up the newest file automatically.

        Must be called from an executor thread (uses blocking file I/O).
        Returns the PDF bytes if a suitable file is found, else None.
        """
        if not self._watch_folder.exists():
            try:
                self._watch_folder.mkdir(parents=True, exist_ok=True)
                _LOGGER.info(
                    "Created PDF watch folder: %s — place newsletter PDFs here "
                    "to process them automatically when remote download is unavailable.",
                    self._watch_folder,
                )
            except OSError as err:
                _LOGGER.warning("Could not create watch folder %s: %s", self._watch_folder, err)
            return None

        # Find all PDFs in the folder, sorted newest-first
        pdfs = sorted(
            self._watch_folder.glob("*.pdf"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        if not pdfs:
            _LOGGER.debug(
                "PDF watch folder '%s' is empty — no local PDF to process. "
                "Download the newsletter from Flipsnack and place the PDF here.",
                self._watch_folder,
            )
            return None

        newest = pdfs[0]

        # If we have a last-processed timestamp, skip files older than that.
        # Use timestamps (floats) to avoid timezone-aware vs naive datetime comparison.
        if last_processed_iso:
            try:
                last_processed_dt = datetime.fromisoformat(last_processed_iso)
                # Convert both sides to UTC timestamps to avoid tz-aware vs naive mismatch
                import calendar as _cal
                if last_processed_dt.tzinfo is not None:
                    last_processed_ts = last_processed_dt.timestamp()
                else:
                    last_processed_ts = _cal.timegm(last_processed_dt.timetuple())
                file_mtime_ts = newest.stat().st_mtime
                if file_mtime_ts <= last_processed_ts:
                    _LOGGER.debug(
                        "Watch folder PDF '%s' (modified %s) is not newer than "
                        "last processed time (%s) — skipping.",
                        newest.name,
                        file_mtime.isoformat(timespec="seconds"),
                        last_processed_iso,
                    )
                    return None
            except (ValueError, OSError):
                pass  # If timestamp parse fails, proceed with the file

        try:
            pdf_bytes = newest.read_bytes()
            if len(pdf_bytes) < 1000:
                _LOGGER.warning("Watch folder PDF '%s' is too small (%d bytes) — skipping.", newest.name, len(pdf_bytes))
                return None
            _LOGGER.info(
                "Using PDF from watch folder: '%s' (%d MB)",
                newest.name,
                len(pdf_bytes) // 1_000_000,
            )
            return pdf_bytes
        except OSError as err:
            _LOGGER.warning("Could not read watch folder PDF '%s': %s", newest, err)
            return None

    # ------------------------------------------------------------------
    # PDF acquisition
    # ------------------------------------------------------------------

    async def _try_fetch_pdf(self, slug: str) -> bytes | None:
        """Fetch the newsletter content via Browserbase cloud browser.

        Uses Browserbase to connect to a real Chromium browser in their cloud
        via CDP — no local browser binary needed, works on HA Green.

        Falls back to None (triggers watch-folder check) if Browserbase is not
        configured or if the session fails for any reason.
        """
        bb_key = self._entry.options.get(OPT_BROWSERBASE_API_KEY, "").strip()
        bb_project = self._entry.options.get(OPT_BROWSERBASE_PROJECT_ID, "").strip()

        if not bb_key or not bb_project:
            _LOGGER.debug(
                "Browserbase not configured — skipping cloud browser fetch for '%s'. "
                "Set Browserbase API key and Project ID in integration options to enable "
                "automatic newsletter fetching.",
                slug,
            )
            return None

        _LOGGER.debug("Starting Browserbase cloud browser fetch for slug '%s'", slug)
        try:
            content = await self.hass.async_add_executor_job(
                _browserbase_fetch_sync, slug, bb_key, bb_project
            )
            if content:
                _LOGGER.info(
                    "Browserbase fetch succeeded for '%s': %d bytes",
                    slug,
                    len(content),
                )
                return content
        except Exception as err:
            _LOGGER.warning(
                "Browserbase fetch failed for '%s': %s — will try watch folder",
                slug,
                err,
            )

        _LOGGER.warning(
            "Could not fetch content for slug '%s' via Browserbase — checking watch folder",
            slug,
        )
        return None

    # ------------------------------------------------------------------
    # Gemini processing
    # ------------------------------------------------------------------

    async def _process_with_gemini(
        self, pdf_bytes: bytes | None, week_label: str, newsletter_url: str = ""
    ) -> str:
        """Compress PDF and send to Gemini for schedule extraction.

        All Gemini calls run in an executor thread (library is synchronous).
        If pdf_bytes is None (PDF fetch was blocked), falls back to passing the
        Flipsnack PDF URL directly to Gemini so it can fetch it from Google's servers.
        """
        api_key = self._entry.data[CONF_GEMINI_API_KEY]
        model_name = self._entry.data.get(CONF_GEMINI_MODEL, DEFAULT_GEMINI_MODEL)
        today = date.today()
        prompt = GEMINI_PROMPT.format(
            today=today.isoformat(),
            week_label=week_label,
            year=today.year,
        )

        if pdf_bytes is None:
            raise RuntimeError(
                "Could not obtain the newsletter PDF. "
                f"To fix this: download the newsletter PDF from Flipsnack in your "
                f"browser, then copy it into the HA config folder at "
                f"'{PDF_WATCH_FOLDER}/' — the integration will pick it up automatically. "
                "Remote download will be retried when a new newsletter is detected."
            )

        # Check if this is plain text (extractedText path) rather than a real PDF.
        # If so, skip compression and use the inline text Gemini path.
        is_plain_text = pdf_bytes[:4] != b"%PDF"
        if is_plain_text:
            text_content = pdf_bytes.decode("utf-8", errors="replace")
            _LOGGER.info(
                "Sending plain text content to Gemini (%d chars, no file upload needed)",
                len(text_content),
            )
            return await self.hass.async_add_executor_job(
                self._gemini_text_inline, api_key, model_name, text_content, prompt
            )

        # Compress/extract in executor
        content, method = await self.hass.async_add_executor_job(
            _compress_pdf, pdf_bytes
        )
        _LOGGER.info(
            "PDF prepared for Gemini using method '%s' (%d MB)",
            method,
            len(content) // 1_000_000,
        )

        return await self.hass.async_add_executor_job(
            self._gemini_upload_pdf, api_key, model_name, content, prompt, week_label
        )

    @staticmethod
    def _gemini_upload_pdf(
        api_key: str,
        model_name: str,
        pdf_bytes: bytes,
        prompt: str,
        week_label: str,
    ) -> str:
        """Upload PDF to Gemini Files API and extract schedule."""
        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(api_key=api_key)

        # Upload PDF via Files API
        file_obj = client.files.upload(
            file=io.BytesIO(pdf_bytes),
            config=genai_types.UploadFileConfig(
                mime_type="application/pdf",
                display_name=f"bjc_newsletter_{week_label}.pdf",
            ),
        )
        _LOGGER.debug("Gemini file uploaded: %s", file_obj.name)

        # Wait for file to become ACTIVE (up to 60 seconds)
        for _ in range(30):
            file_obj = client.files.get(name=file_obj.name)
            if file_obj.state.name == "ACTIVE":
                break
            time.sleep(2)
        else:
            try:
                client.files.delete(name=file_obj.name)
            except Exception:
                pass
            raise RuntimeError(
                f"Gemini file {file_obj.name} did not become ACTIVE after 60s"
            )

        # Generate content
        response = client.models.generate_content(
            model=model_name,
            contents=[file_obj, prompt],
            config=genai_types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=16384,
            ),
        )

        # Clean up uploaded file
        try:
            client.files.delete(name=file_obj.name)
        except Exception:
            pass

        if not response.text:
            raise RuntimeError("Gemini returned an empty response (PDF path)")

        return response.text

    @staticmethod
    def _gemini_text_inline(
        api_key: str,
        model_name: str,
        text_content: str,
        prompt: str,
    ) -> str:
        """Send plain text content directly to Gemini (no file upload needed)."""
        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(api_key=api_key)

        combined = f"{prompt}\n\n---\n\nNEWSLETTER TEXT:\n{text_content}"
        response = client.models.generate_content(
            model=model_name,
            contents=combined,
            config=genai_types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=16384,
            ),
        )

        if not response.text:
            raise RuntimeError("Gemini returned an empty response (text inline path)")

        return response.text
