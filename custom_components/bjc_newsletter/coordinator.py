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
    OPT_CURRENT_NEWSLETTER_URL,
    OPT_LAST_CHECKED,
    OPT_LAST_PROCESSED,
    PDF_IMAGE_QUALITY,
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

        # Pre-populate from disk cache so sensors have data immediately on restart
        self.data = self._load_cache()

    # ------------------------------------------------------------------
    # Cache I/O
    # ------------------------------------------------------------------

    def _load_cache(self) -> dict[str, Any]:
        """Load persisted schedule from disk. Returns safe default on failure."""
        default: dict[str, Any] = {
            DATA_SCHEDULE: {},
            DATA_STATUS: STATUS_IDLE,
            DATA_NEWSLETTER_URL: self._entry.options.get(OPT_CURRENT_NEWSLETTER_URL, ""),
            DATA_LAST_PROCESSED: self._entry.options.get(OPT_LAST_PROCESSED, ""),
            DATA_LAST_CHECKED: self._entry.options.get(OPT_LAST_CHECKED, ""),
            DATA_LAST_ERROR: "",
        }
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

        # Step 2: change detection
        stored_url = self._entry.options.get(OPT_CURRENT_NEWSLETTER_URL, "")
        if current_url and current_url == stored_url:
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

        # Step 3b: fetch PDF
        pdf_bytes = await self._try_fetch_pdf(slug)

        # Step 3c: send to Gemini
        try:
            markdown = await self._process_with_gemini(pdf_bytes, week_label, current_url)
        except Exception as err:
            _LOGGER.error("Gemini processing failed for %s: %s", slug, err)
            existing_data[DATA_STATUS] = STATUS_ERROR
            existing_data[DATA_LAST_ERROR] = str(err)
            existing_data[DATA_LAST_CHECKED] = now_iso
            await self.hass.async_add_executor_job(self._save_cache, existing_data)
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

        # Strategy 1: find <h2> containing "BJC Insider", then find
        # the nearest <a href> pointing to the BJC Flipsnack account
        for h2 in soup.find_all("h2"):
            if "bjc insider" in h2.get_text(strip=True).lower():
                parent = h2.parent
                if parent:
                    for a in parent.find_all("a", href=True):
                        href = a["href"]
                        if account in href and "flipsnack.com" in href:
                            return href.split("?")[0].rstrip("/")
                # Also check if the heading itself is wrapped in an anchor
                a = h2.find("a", href=True)
                if a and account in a["href"]:
                    return a["href"].split("?")[0].rstrip("/")

        # Strategy 2: scan all BJC Flipsnack links, exclude non-newsletter slugs
        EXCLUDE = {"calendar", "annual-report", "yizkor", "haggadah", "bulletin"}
        candidates = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "flipsnack.com" in href and account in href:
                slug = _slug_from_url(href)
                if not any(excl in slug.lower() for excl in EXCLUDE):
                    candidates.append(href.split("?")[0].rstrip("/"))

        if candidates:
            return candidates[0]

        raise UpdateFailed("Could not find BJC Insider newsletter link on homepage")

    # ------------------------------------------------------------------
    # PDF acquisition
    # ------------------------------------------------------------------

    async def _try_fetch_pdf(self, slug: str) -> bytes | None:
        """Attempt to fetch the newsletter PDF. Returns bytes or None."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": f"https://www.flipsnack.com/{BJC_FLIPSNACK_ACCOUNT}/{slug}",
        }

        # Attempt 1: direct PDF URL
        pdf_url = FLIPSNACK_PDF_PATTERN.format(account=BJC_FLIPSNACK_ACCOUNT, slug=slug)
        try:
            async with self._session.get(
                pdf_url,
                timeout=aiohttp.ClientTimeout(total=120),
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    ct = resp.headers.get("Content-Type", "")
                    if "pdf" in ct or "octet-stream" in ct:
                        data = await resp.read()
                        _LOGGER.info("PDF fetched directly: %d MB", len(data) // 1_000_000)
                        return data
        except Exception as err:
            _LOGGER.debug("Direct PDF URL failed for %s: %s", slug, err)

        # Attempt 2: scrape the full-view page for a PDF link
        full_view_url = FLIPSNACK_FULLVIEW_PATTERN.format(
            account=BJC_FLIPSNACK_ACCOUNT, slug=slug
        )
        try:
            from bs4 import BeautifulSoup

            async with self._session.get(
                full_view_url,
                timeout=aiohttp.ClientTimeout(total=30),
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")
                    for a in soup.find_all("a", href=True):
                        href = a["href"]
                        if ".pdf" in href.lower() or "download" in href.lower():
                            async with self._session.get(
                                href,
                                timeout=aiohttp.ClientTimeout(total=120),
                                headers=headers,
                            ) as pdf_resp:
                                if pdf_resp.status == 200:
                                    data = await pdf_resp.read()
                                    _LOGGER.info(
                                        "PDF fetched from full-view page: %d MB",
                                        len(data) // 1_000_000,
                                    )
                                    return data
        except Exception as err:
            _LOGGER.debug("Full-view PDF scrape failed for %s: %s", slug, err)

        _LOGGER.warning(
            "Could not fetch PDF for slug '%s' directly — will pass URL to Gemini instead",
            slug,
        )
        return None  # caller will fall back to URL-based Gemini processing

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
            # Direct PDF download was blocked — ask Gemini to fetch the PDF URL itself
            slug = _slug_from_url(newsletter_url) if newsletter_url else "unknown"
            pdf_url = FLIPSNACK_PDF_PATTERN.format(
                account=BJC_FLIPSNACK_ACCOUNT, slug=slug
            )
            _LOGGER.info(
                "PDF fetch was blocked; passing PDF URL directly to Gemini: %s", pdf_url
            )
            return await self.hass.async_add_executor_job(
                self._gemini_url_fallback, api_key, model_name, pdf_url, prompt
            )

        # Compress/extract in executor
        content, method = await self.hass.async_add_executor_job(
            _compress_pdf, pdf_bytes
        )
        _LOGGER.info(
            "PDF prepared for Gemini using method '%s' (%s)",
            method,
            f"{len(content):,} chars" if isinstance(content, str) else f"{len(content) // 1_000_000} MB",
        )

        return await self.hass.async_add_executor_job(
            self._gemini_upload_pdf, api_key, model_name, content, prompt, week_label
        )

    @staticmethod
    def _gemini_url_fallback(
        api_key: str,
        model_name: str,
        pdf_url: str,
        prompt: str,
    ) -> str:
        """Ask Gemini to fetch and process the PDF directly from its URL.

        Used when the HA instance cannot download the PDF (e.g. Flipsnack blocks
        server-side requests). Gemini's servers can often fetch the URL independently.
        """
        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(api_key=api_key)

        response = client.models.generate_content(
            model=model_name,
            contents=[
                genai_types.Part(
                    file_data=genai_types.FileData(
                        file_uri=pdf_url,
                        mime_type="application/pdf",
                    )
                ),
                prompt,
            ],
            config=genai_types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=16384,
            ),
        )

        if not response.text:
            raise RuntimeError(
                f"Gemini returned an empty response when fetching URL: {pdf_url}"
            )

        return response.text

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
