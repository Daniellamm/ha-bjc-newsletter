"""Local test script for BJC Newsletter integration.

Runs the core pipeline (scrape → PDF fetch → compress → Gemini) standalone,
without needing a running Home Assistant instance.

Usage:
    cd /path/to/ha-bjc-newsletter
    pip install google-genai beautifulsoup4 pikepdf aiohttp python-dateutil pillow
    export GEMINI_API_KEY="your-key-here"
    python test_local.py
    # Or pass a local PDF directly:
    python test_local.py "/path/to/newsletter.pdf"
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Inline copies of the key helpers (so we don't need HA installed)
# ---------------------------------------------------------------------------

BJC_HOMEPAGE_URL = "https://www.bocajewishcenter.org/"
BJC_FLIPSNACK_ACCOUNT = "7BBDB688B7A"
FLIPSNACK_PDF_PATTERN = "https://www.flipsnack.com/{account}/{slug}.pdf"
FLIPSNACK_FULLVIEW_PATTERN = "https://www.flipsnack.com/{account}/{slug}/full-view.html"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_FALLBACK_MODELS = ["models/gemini-2.5-flash"]

PDF_IMAGE_QUALITY = 40

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


def slug_from_url(url: str) -> str:
    url = url.rstrip("/")
    if url.endswith("/full-view.html"):
        url = url[: -len("/full-view.html")]
    return url.rsplit("/", 1)[-1]


def week_label_from_slug(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


def compress_pdf(pdf_bytes: bytes) -> tuple[bytes, str]:
    """Compress PDF images with pikepdf, preserving visual layout for Gemini.

    Text extraction is intentionally NOT used — the visual layout is required
    so Gemini correctly reads which events belong to which day columns.
    """
    try:
        import pikepdf
        from PIL import Image

        src = pikepdf.open(io.BytesIO(pdf_bytes))
        out_buf = io.BytesIO()
        images_processed = 0

        for page in src.pages:
            for name, raw_obj in page.images.items():
                try:
                    pil_img = pikepdf.PdfImage(raw_obj).as_pil_image()
                    jpeg_buf = io.BytesIO()
                    if pil_img.mode in ("RGBA", "P", "L"):
                        pil_img = pil_img.convert("RGB")
                    pil_img.save(jpeg_buf, format="JPEG", quality=PDF_IMAGE_QUALITY, optimize=True)
                    raw_obj.write(jpeg_buf.getvalue(), filter=pikepdf.Name("/DCTDecode"))
                    images_processed += 1
                except Exception as e:
                    pass

        src.save(out_buf)
        compressed = out_buf.getvalue()
        pct = 100 * (1 - len(compressed) / len(pdf_bytes))
        print(f"  ✓ pikepdf: {len(pdf_bytes)//1_000_000}MB → {len(compressed)//1_000_000}MB "
              f"({pct:.0f}% reduction, {images_processed} images recompressed)")
        return compressed, "pikepdf"
    except Exception as e:
        print(f"  ✗ pikepdf failed: {e} — using original PDF")

    print(f"  → using original PDF ({len(pdf_bytes)//1_000_000}MB)")
    return pdf_bytes, "original"


def parse_schedule(markdown: str) -> dict[str, str]:
    from dateutil import parser as dateutil_parser
    from datetime import datetime

    current_year = date.today().year
    year_default = datetime(current_year, 1, 1)

    schedule: dict[str, str] = {}
    sections = re.split(r"(?=^## )", markdown.strip(), flags=re.MULTILINE)

    for section in sections:
        section = section.strip()
        if not section:
            continue
        first_line = section.split("\n", 1)[0]
        heading_text = first_line.lstrip("#").strip()
        try:
            date_text = heading_text.split(":")[0].strip()
            parsed = dateutil_parser.parse(date_text, fuzzy=True, default=year_default)
            if abs(parsed.year - current_year) > 1:
                raise ValueError(f"Year {parsed.year} too far from {current_year}")
            key = parsed.date().isoformat()
            schedule[key] = section
        except (ValueError, OverflowError):
            existing = schedule.get("weekly", "")
            schedule["weekly"] = (existing + "\n\n" + section).strip() if existing else section

    return schedule


def call_gemini_with_fallback(pdf_bytes: bytes, method: str, week_label: str) -> str:
    """Upload compressed PDF to Gemini and extract schedule. Tries fallback models on quota errors."""
    from google import genai
    from google.genai import types as genai_types

    today = date.today()
    prompt = GEMINI_PROMPT.format(today=today.isoformat(), week_label=week_label, year=today.year)

    def _try_model(model: str) -> str:
        client = genai.Client(api_key=GEMINI_API_KEY)
        print(f"  Uploading {len(pdf_bytes)//1_000_000}MB PDF to Gemini Files API (model: {model})...")
        file_obj = client.files.upload(
            file=io.BytesIO(pdf_bytes),
            config=genai_types.UploadFileConfig(
                mime_type="application/pdf",
                display_name=f"bjc_{week_label}.pdf",
            ),
        )
        print(f"  File uploaded: {file_obj.name} — waiting for ACTIVE...")
        for i in range(30):
            file_obj = client.files.get(name=file_obj.name)
            if file_obj.state.name == "ACTIVE":
                print(f"  ACTIVE after {(i+1)*2}s")
                break
            time.sleep(2)
        else:
            client.files.delete(name=file_obj.name)
            raise RuntimeError("File never became ACTIVE")

        response = client.models.generate_content(
            model=model,
            contents=[file_obj, prompt],
            config=genai_types.GenerateContentConfig(temperature=0.1, max_output_tokens=16384),
        )
        try:
            client.files.delete(name=file_obj.name)
        except Exception:
            pass

        if not response.text:
            raise RuntimeError("Gemini returned empty response")
        return response.text

    models_to_try = [GEMINI_MODEL] + GEMINI_FALLBACK_MODELS
    last_err = None
    for model in models_to_try:
        try:
            return _try_model(model)
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e) or "quota" in str(e).lower():
                print(f"  ✗ {model} quota exceeded — trying next model")
                last_err = e
                continue
            raise
    raise RuntimeError(f"All Gemini models exhausted. Last error: {last_err}")


async def run_test(use_local_pdf: str | None = None):
    import aiohttp

    print("=" * 60)
    print("BJC Newsletter Integration — Local Test")
    print("=" * 60)

    # ---- Step 1: Scrape BJC homepage ----
    print("\n[1] Scraping BJC homepage for newsletter URL...")
    async with aiohttp.ClientSession() as session:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        async with session.get(BJC_HOMEPAGE_URL, timeout=aiohttp.ClientTimeout(total=30), headers=headers) as resp:
            html = await resp.text()

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    account = BJC_FLIPSNACK_ACCOUNT
    newsletter_url = None

    for h2 in soup.find_all("h2"):
        if "bjc insider" in h2.get_text(strip=True).lower():
            parent = h2.parent
            if parent:
                for a in parent.find_all("a", href=True):
                    href = a["href"]
                    if account in href and "flipsnack.com" in href:
                        newsletter_url = href.split("?")[0].rstrip("/")
                        break
            if newsletter_url:
                break

    if not newsletter_url:
        EXCLUDE = {"calendar", "annual-report", "yizkor", "haggadah", "bulletin"}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "flipsnack.com" in href and account in href:
                slug_check = slug_from_url(href)
                if not any(e in slug_check.lower() for e in EXCLUDE):
                    newsletter_url = href.split("?")[0].rstrip("/")
                    break

    if newsletter_url:
        print(f"  ✓ Found newsletter: {newsletter_url}")
    else:
        print("  ✗ Could not find newsletter URL on homepage")
        newsletter_url = f"https://www.flipsnack.com/{account}/pesach-2026-_-5786"
        print(f"  → Using fallback URL: {newsletter_url}")

    slug = slug_from_url(newsletter_url)
    week_label = week_label_from_slug(slug)
    print(f"  Slug: {slug}")
    print(f"  Week label: {week_label}")

    # ---- Step 2: Fetch or use local PDF ----
    pdf_bytes = None

    if use_local_pdf:
        print(f"\n[2] Loading local PDF: {use_local_pdf}")
        pdf_bytes = Path(use_local_pdf).read_bytes()
        print(f"  ✓ Loaded {len(pdf_bytes)//1_000_000}MB")
    else:
        print(f"\n[2] Attempting to fetch PDF for slug '{slug}'...")
        async with aiohttp.ClientSession() as session:
            pdf_url = FLIPSNACK_PDF_PATTERN.format(account=account, slug=slug)
            print(f"  Trying: {pdf_url}")
            try:
                async with session.get(
                    pdf_url,
                    timeout=aiohttp.ClientTimeout(total=60),
                    headers=headers,
                ) as resp:
                    print(f"  HTTP {resp.status} — Content-Type: {resp.headers.get('Content-Type', 'unknown')}")
                    if resp.status == 200 and "pdf" in resp.headers.get("Content-Type", "").lower():
                        pdf_bytes = await resp.read()
                        print(f"  ✓ PDF fetched: {len(pdf_bytes)//1_000_000}MB")
                    else:
                        print(f"  ✗ Not a PDF response")
            except Exception as e:
                print(f"  ✗ Fetch failed: {e}")

        if not pdf_bytes:
            print("\n  PDF could not be fetched from Flipsnack.")
            local_options = [
                "/Users/daniellamm/Downloads/Pesach 2026 _ 5786.pdf",
                "/Users/daniellamm/Downloads/March 27-28.pdf",
            ]
            for opt in local_options:
                if Path(opt).exists():
                    print(f"  → Using local PDF: {opt}")
                    pdf_bytes = Path(opt).read_bytes()
                    print(f"  ✓ Loaded {len(pdf_bytes)//1_000_000}MB")
                    break

    if not pdf_bytes:
        print("\n  ✗ No PDF available. Cannot continue.")
        return

    # ---- Step 3: Compress ----
    print("\n[3] Compressing PDF...")
    content, method = compress_pdf(pdf_bytes)

    # ---- Step 4: Call Gemini ----
    print(f"\n[4] Sending to Gemini via method '{method}'...")
    try:
        markdown = call_gemini_with_fallback(content, method, week_label)  # content is bytes (pikepdf or original)
        print(f"  ✓ Gemini returned {len(markdown):,} chars")
    except Exception as e:
        print(f"  ✗ Gemini failed: {e}")
        return

    # ---- Step 5: Parse schedule ----
    print("\n[5] Parsing schedule from markdown...")
    schedule = parse_schedule(markdown)
    date_keys = [k for k in schedule if k != "weekly"]
    print(f"  ✓ Parsed {len(date_keys)} date-keyed sections + {'weekly' if 'weekly' in schedule else 'no weekly'}")
    for k in sorted(date_keys):
        snippet = schedule[k].split("\n")[0]
        print(f"     {k}: {snippet[:60]}")

    # ---- Step 6: Show today/tomorrow ----
    today = date.today().isoformat()
    tomorrow = (date.today().replace(day=date.today().day + 1)).isoformat()

    print(f"\n[6] Today ({today}):")
    today_sched = schedule.get(today) or schedule.get("weekly") or "(no schedule)"
    print(today_sched[:500] + ("..." if len(today_sched) > 500 else ""))

    print(f"\n[7] Tomorrow ({tomorrow}):")
    tomorrow_sched = schedule.get(tomorrow) or schedule.get("weekly") or "(no schedule)"
    print(tomorrow_sched[:500] + ("..." if len(tomorrow_sched) > 500 else ""))

    # ---- Step 7: Save output ----
    output_path = Path("/Users/daniellamm/Documents/GitHub/ha-bjc-newsletter/test_output.json")
    output = {
        "newsletter_url": newsletter_url,
        "week_label": week_label,
        "compression_method": method,
        "schedule": schedule,
        "full_markdown": markdown,
    }
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n✓ Full output saved to: {output_path}")
    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    if not GEMINI_API_KEY:
        print("ERROR: Set the GEMINI_API_KEY environment variable before running.")
        print("  export GEMINI_API_KEY='your-key-here'")
        sys.exit(1)
    local_pdf = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run_test(use_local_pdf=local_pdf))
