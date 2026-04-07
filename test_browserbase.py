#!/usr/bin/env python3
"""Standalone end-to-end test: Browserbase → Gemini → schedule.

Run from the repo root:
    pip install browserbase playwright google-genai pillow
    BB_API_KEY=... BB_PROJECT_ID=... GEMINI_API_KEY=... python test_browserbase.py
"""

import io
import logging
import os
import re
import sys
import urllib.error as _urlerr
import urllib.request as _urlreq

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
_LOGGER = logging.getLogger("test_browserbase")

# ---------------------------------------------------------------------------
# Config — set via environment variables
# ---------------------------------------------------------------------------
BB_API_KEY = os.environ.get("BB_API_KEY", "")
BB_PROJECT_ID = os.environ.get("BB_PROJECT_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# The slug to test (current newsletter slug from Flipsnack URL)
TEST_SLUG = os.environ.get("TEST_SLUG", "pesach-2026-_-5786")
BJC_FLIPSNACK_ACCOUNT = "7BBDB688B7A"

GEMINI_PROMPT = """\
You are processing a weekly synagogue newsletter PDF for Boca Jewish Center (BJC).

CRITICAL INSTRUCTION: You MUST extract EVERY SINGLE DAY listed in the schedule section \
of this newsletter. Do not stop early. Do not truncate. Output every day completely, \
from the first day to the last day shown in the PDF.

Your task: find the SCHEDULE section of the newsletter (it may be labeled "Schedule", \
"Weekly Schedule", "Zmanim", or similar). This section contains a day-by-day list of \
prayer services and events with specific times. Extract ALL of it — every day, \
every event, every time listed.

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

Today's date for reference: 2026-04-06
The newsletter covers the week of: April 5, 2026
Current year: 2026
"""


# ---------------------------------------------------------------------------
# Step 1: Browserbase fetch (copied from coordinator._browserbase_fetch_sync)
# ---------------------------------------------------------------------------

def browserbase_fetch(slug: str, api_key: str, project_id: str):
    full_view_url = (
        f"https://www.flipsnack.com/{BJC_FLIPSNACK_ACCOUNT}/{slug}/full-view.html"
    )
    _LOGGER.info("Browserbase: starting session for slug '%s'", slug)

    try:
        from browserbase import Browserbase
    except ImportError:
        print("ERROR: browserbase not installed. Run: pip install browserbase")
        sys.exit(1)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed. Run: pip install playwright")
        sys.exit(1)

    bb = Browserbase(api_key=api_key)
    session = bb.sessions.create(project_id=project_id)
    _LOGGER.info("Browserbase: session created: %s", session.id)

    captured: dict = {}

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(session.connect_url)
        context = browser.contexts[0]
        page = context.new_page()

        def on_response(response):
            if "data.json" in response.url and response.status == 200:
                try:
                    captured["url"] = response.url
                    captured["data"] = response.json()
                    _LOGGER.info("Captured data.json from: %s", response.url)
                except Exception as e:
                    _LOGGER.warning("Failed to parse data.json: %s", e)

        page.on("response", on_response)
        _LOGGER.info("Navigating to: %s", full_view_url)
        page.goto(full_view_url, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(3000)
        browser.close()

    if "data" not in captured:
        print("FAIL: data.json was not captured")
        return None

    data: dict = captured["data"]

    # Page count
    toc = data.get("toc", [])
    toc_pages = toc[0].get("sub", []) if toc else []
    page_count = len(toc_pages) if toc_pages else 0
    pages_info = data.get("pages", {})
    if page_count == 0:
        page_count = len(pages_info.get("order", []))

    # Item hash
    item_hash = toc[0].get("originalHash") if toc else None
    if not item_hash:
        pages_data_map = pages_info.get("data", {})
        order = pages_info.get("order", [])
        if order and order[0] in pages_data_map:
            item_hash = pages_data_map[order[0]].get("source", {}).get("hash")

    _LOGGER.info("page_count=%d, item_hash=%s", page_count, item_hash)

    # Download page images and assemble PDF
    if not item_hash or page_count == 0:
        print("FAIL: could not determine item_hash or page_count — cannot proceed")
        return None

    qs_match = re.search(r"\?(.+)$", captured["url"])
    if not qs_match:
        print("FAIL: no signed query string in data.json URL")
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

    _LOGGER.info("Image fallback: downloading %d pages...", page_count)
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
            except Exception:
                continue
        if img_data:
            page_images.append(img_data)
        else:
            _LOGGER.debug("Could not download page %d", page_num)

    if not page_images:
        print("FAIL: no page images downloaded")
        return None

    from PIL import Image as _PilImage
    pil_images = [_PilImage.open(io.BytesIO(b)).convert("RGB") for b in page_images]
    out = io.BytesIO()
    pil_images[0].save(out, format="PDF", save_all=True, append_images=pil_images[1:], resolution=150)
    _LOGGER.info("Image fallback: assembled %d-page PDF", len(page_images))
    return out.getvalue()


# ---------------------------------------------------------------------------
# Step 2: Gemini
# ---------------------------------------------------------------------------

def call_gemini(content: bytes, gemini_key: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=gemini_key)
    model = "gemini-2.5-flash"

    # Always use PDF path — upload via Files API
    _LOGGER.info("Uploading %d-byte PDF to Gemini Files API", len(content))
    uploaded = client.files.upload(
        file=io.BytesIO(content),
        config=types.UploadFileConfig(mime_type="application/pdf", display_name="newsletter.pdf"),
    )
    # Wait for ACTIVE
    for _ in range(30):
        import time
        f = client.files.get(name=uploaded.name)
        if f.state and f.state.name == "ACTIVE":
            break
        time.sleep(2)

    resp = client.models.generate_content(
        model=model,
        contents=[uploaded, GEMINI_PROMPT],
        config=types.GenerateContentConfig(temperature=0.1),
    )
    client.files.delete(name=uploaded.name)
    return resp.text or ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    missing = [v for v in ("BB_API_KEY", "BB_PROJECT_ID", "GEMINI_API_KEY") if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing environment variables: {', '.join(missing)}")
        print("Usage: BB_API_KEY=... BB_PROJECT_ID=... GEMINI_API_KEY=... python test_browserbase.py")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"STEP 1: Browserbase fetch for slug '{TEST_SLUG}'")
    print(f"{'='*60}")
    content = browserbase_fetch(TEST_SLUG, BB_API_KEY, BB_PROJECT_ID)

    if not content:
        print("\nFAIL: Browserbase returned nothing")
        sys.exit(1)

    print(f"\nOK: Got {len(content)} bytes (PDF)")

    print(f"\n{'='*60}")
    print("STEP 2: Gemini schedule extraction")
    print(f"{'='*60}")
    schedule_md = call_gemini(content, GEMINI_API_KEY)

    if not schedule_md:
        print("\nFAIL: Gemini returned empty response")
        sys.exit(1)

    print(f"\nOK: Gemini returned {len(schedule_md)} chars\n")
    print("--- Schedule ---")
    print(schedule_md)

    # Save full output
    out_path = "/Users/daniellamm/Documents/GitHub/ha-bjc-newsletter/test_output.md"
    with open(out_path, "w") as f:
        f.write(schedule_md)
    print(f"\nFull output saved to: {out_path}")


if __name__ == "__main__":
    main()
