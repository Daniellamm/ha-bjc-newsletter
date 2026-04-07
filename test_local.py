"""Local test script for BJC Newsletter integration.

Runs the full pipeline (scrape → PDF fetch → compress → Gemini) standalone,
without needing a running Home Assistant instance.

Fetch priority (mirrors production coordinator):
  1. Flipsnack direct PDF URL
  2. Flipsnack full-view page scrape
  3. Flipsnack viewer page CDN scrape
  4. Local PDF folder  (~/bjc_newsletter_pdfs/ or argv[2])
  5. Explicit path passed as argv[1]

Usage:
    pip install google-genai beautifulsoup4 pikepdf aiohttp python-dateutil pillow
    export GEMINI_API_KEY="your-key-here"

    # Auto-discover newsletter and try all fetch methods:
    python test_local.py

    # Use a specific local PDF:
    python test_local.py "/path/to/newsletter.pdf"

    # Run multiple passes to verify consistency:
    python test_local.py --runs 3
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BJC_HOMEPAGE_URL = "https://www.bocajewishcenter.org/"
BJC_FLIPSNACK_ACCOUNT = "7BBDB688B7A"
FLIPSNACK_PDF_PATTERN = "https://www.flipsnack.com/{account}/{slug}.pdf"
FLIPSNACK_FULLVIEW_PATTERN = "https://www.flipsnack.com/{account}/{slug}/full-view.html"
FLIPSNACK_CDN = "https://d160aj0mj3npgx.cloudfront.net"

# Default folder to look for manually-downloaded newsletter PDFs
LOCAL_PDF_FOLDER = Path.home() / "bjc_newsletter_pdfs"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slug_from_url(url: str) -> str:
    url = url.rstrip("/")
    if url.endswith("/full-view.html"):
        url = url[: -len("/full-view.html")]
    return url.rsplit("/", 1)[-1]


def week_label_from_slug(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


def extract_pdf_url_from_flipsnack_page(html: str) -> str | None:
    """Extract CDN PDF URL embedded in Flipsnack viewer page JavaScript."""
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

    cdn_pattern = r'https://(?:[^"\'<\s]*(?:amazonaws|cloudfront|flipsnack|s3)[^"\'<\s]*\.pdf[^"\'<\s]*)'
    m = re.search(cdn_pattern, html, re.IGNORECASE)
    if m:
        return m.group(0).rstrip("\\")
    return None


def compress_pdf(pdf_bytes: bytes) -> tuple[bytes, str]:
    """Compress PDF images with pikepdf, preserving visual layout."""
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
                except Exception:
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


def call_gemini(pdf_bytes: bytes, week_label: str) -> str:
    """Send content to Gemini — uploads PDF via Files API, or sends plain text inline."""
    from google import genai
    from google.genai import types as genai_types

    today = date.today()
    prompt = GEMINI_PROMPT.format(today=today.isoformat(), week_label=week_label, year=today.year)
    client = genai.Client(api_key=GEMINI_API_KEY)

    # Plain text path: newer PDF-based flipbooks embed extractedText in data.json
    if pdf_bytes[:4] != b"%PDF":
        text_content = pdf_bytes.decode("utf-8", errors="replace")
        print(f"  Sending {len(text_content):,} chars as inline text to Gemini...")
        combined = f"{prompt}\n\n---\n\nNEWSLETTER TEXT:\n{text_content}"
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=combined,
            config=genai_types.GenerateContentConfig(temperature=0.1, max_output_tokens=16384),
        )
        if not response.text:
            raise RuntimeError("Gemini returned empty response (text inline path)")
        return response.text

    # PDF path: upload via Files API
    print(f"  Uploading {len(pdf_bytes)//1_000_000}MB to Gemini Files API...")
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
        raise RuntimeError("Gemini file never became ACTIVE")

    response = client.models.generate_content(
        model=GEMINI_MODEL,
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


# ---------------------------------------------------------------------------
# PDF Fetch — mirrors coordinator._try_fetch_pdf + watch folder
# ---------------------------------------------------------------------------

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


async def try_fetch_pdf(session, slug: str) -> tuple[bytes | None, str]:
    """Try all methods to fetch the newsletter PDF. Returns (bytes, method_name)."""
    import aiohttp

    referer_headers = {
        **BROWSER_HEADERS,
        "Referer": f"https://www.flipsnack.com/{BJC_FLIPSNACK_ACCOUNT}/{slug}",
    }

    # Method 1: Browser-based fetch (Playwright) — mirrors production coordinator
    print(f"  [1] Browser-based fetch (Playwright)...")
    try:
        from test_pdf_fetch import fetch_pdf_browser
        img_pdf = await fetch_pdf_browser(
            f"https://www.flipsnack.com/{BJC_FLIPSNACK_ACCOUNT}/{slug}"
        )
        if img_pdf and img_pdf[:4] == b"%PDF":
            print(f"      ✓ Got PDF via browser ({len(img_pdf)//1_000_000}MB)")
            return img_pdf, "playwright_browser"
        elif img_pdf:
            # May be plain text (extractedText path for newer PDF-based flipbooks)
            try:
                decoded = img_pdf.decode("utf-8")
                if len(decoded.strip()) >= 200:
                    print(f"      ✓ Got plain text via browser ({len(decoded):,} chars)")
                    return img_pdf, "playwright_browser_text"
            except Exception:
                pass
            print(f"      ✗ Browser fetch returned no valid PDF or text")
        else:
            print(f"      ✗ Browser fetch returned no valid PDF")
    except Exception as e:
        print(f"      ✗ {e}")

    # Method 2: Direct PDF URL
    pdf_url = FLIPSNACK_PDF_PATTERN.format(account=BJC_FLIPSNACK_ACCOUNT, slug=slug)
    print(f"  [2] Direct PDF URL: {pdf_url}")
    try:
        async with session.get(
            pdf_url, timeout=aiohttp.ClientTimeout(total=60), headers=referer_headers
        ) as resp:
            print(f"      → HTTP {resp.status} | CT: {resp.headers.get('Content-Type', '?')}")
            if resp.status == 200:
                ct = resp.headers.get("Content-Type", "")
                if "pdf" in ct or "octet-stream" in ct:
                    data = await resp.read()
                    print(f"      ✓ Got PDF ({len(data)//1_000_000}MB)")
                    return data, "flipsnack_direct"
                else:
                    print(f"      ✗ Not a PDF content-type")
    except Exception as e:
        print(f"      ✗ {e}")

    # Method 2: Full-view page link scrape
    full_view_url = FLIPSNACK_FULLVIEW_PATTERN.format(account=BJC_FLIPSNACK_ACCOUNT, slug=slug)
    print(f"  [3] Full-view page scrape: {full_view_url}")
    try:
        from bs4 import BeautifulSoup
        async with session.get(
            full_view_url, timeout=aiohttp.ClientTimeout(total=30), headers=referer_headers
        ) as resp:
            print(f"      → HTTP {resp.status}")
            if resp.status == 200:
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if ".pdf" in href.lower() or "download" in href.lower():
                        print(f"      Found PDF link: {href[:80]}")
                        async with session.get(
                            href, timeout=aiohttp.ClientTimeout(total=120), headers=referer_headers
                        ) as pdf_resp:
                            if pdf_resp.status == 200:
                                data = await pdf_resp.read()
                                print(f"      ✓ Got PDF ({len(data)//1_000_000}MB)")
                                return data, "flipsnack_fullview"
                print(f"      ✗ No PDF links found")
    except Exception as e:
        print(f"      ✗ {e}")

    # Method 3: Viewer page CDN scrape (extract JSON-embedded PDF URL)
    viewer_url = f"https://www.flipsnack.com/{BJC_FLIPSNACK_ACCOUNT}/{slug}"
    print(f"  [4] Viewer page CDN scrape: {viewer_url}")
    try:
        async with session.get(
            viewer_url, timeout=aiohttp.ClientTimeout(total=30), headers=referer_headers
        ) as resp:
            print(f"      → HTTP {resp.status}")
            if resp.status == 200:
                html = await resp.text()
                pdf_cdn_url = extract_pdf_url_from_flipsnack_page(html)
                if pdf_cdn_url:
                    print(f"      Found CDN URL: {pdf_cdn_url[:80]}")
                    async with session.get(
                        pdf_cdn_url, timeout=aiohttp.ClientTimeout(total=120), headers=referer_headers
                    ) as pdf_resp:
                        print(f"      → HTTP {pdf_resp.status}")
                        if pdf_resp.status == 200:
                            data = await pdf_resp.read()
                            print(f"      ✓ Got PDF ({len(data)//1_000_000}MB)")
                            return data, "flipsnack_cdn_scrape"
                        else:
                            print(f"      ✗ CDN blocked ({pdf_resp.status})")
                else:
                    print(f"      ✗ No PDF URL found in page JavaScript")
    except Exception as e:
        print(f"      ✗ {e}")

    # Method 4: Local PDF watch folder
    print(f"  [5] Local PDF folder: {LOCAL_PDF_FOLDER}")
    if LOCAL_PDF_FOLDER.exists():
        pdfs = sorted(LOCAL_PDF_FOLDER.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        if pdfs:
            newest = pdfs[0]
            print(f"      Found: {newest.name} ({newest.stat().st_size//1_000_000}MB)")
            data = newest.read_bytes()
            print(f"      ✓ Loaded from local folder")
            return data, f"local_folder:{newest.name}"
        else:
            print(f"      ✗ Folder exists but no PDF files")
    else:
        print(f"      ✗ Folder does not exist (create it and add a PDF)")

    return None, "none"


# ---------------------------------------------------------------------------
# Main test pipeline
# ---------------------------------------------------------------------------

async def run_single_pass(session, newsletter_url: str, use_local_pdf: str | None, pass_num: int) -> dict:
    """Run one complete pipeline pass. Returns result dict."""
    import aiohttp

    print(f"\n{'='*60}")
    print(f"PASS {pass_num}")
    print(f"{'='*60}")

    slug = slug_from_url(newsletter_url)
    week_label = week_label_from_slug(slug)
    print(f"Slug:       {slug}")
    print(f"Week label: {week_label}")
    print(f"URL:        {newsletter_url}")

    # Step 2: Get PDF
    print(f"\n[2] Fetching PDF...")
    if use_local_pdf:
        pdf_bytes = Path(use_local_pdf).read_bytes()
        fetch_method = f"explicit_arg:{Path(use_local_pdf).name}"
        print(f"  ✓ Using explicit PDF: {use_local_pdf} ({len(pdf_bytes)//1_000_000}MB)")
    else:
        pdf_bytes, fetch_method = await try_fetch_pdf(session, slug)

    if not pdf_bytes:
        print(f"\n  ✗ FAILED — no PDF available")
        print(f"  → To fix: download the newsletter PDF from {newsletter_url}")
        print(f"    and place it in: {LOCAL_PDF_FOLDER}/")
        return {"pass": pass_num, "success": False, "error": "no_pdf", "fetch_method": fetch_method}

    print(f"\n  PDF acquired via: {fetch_method}")

    # Step 3: Compress (skip for plain text content)
    if pdf_bytes[:4] == b"%PDF":
        print(f"\n[3] Compressing PDF...")
        compressed, compress_method = compress_pdf(pdf_bytes)
    else:
        print(f"\n[3] Skipping compression — content is plain text ({len(pdf_bytes):,} bytes)")
        compressed, compress_method = pdf_bytes, "text_inline"

    # Step 4: Gemini
    print(f"\n[4] Sending to Gemini ({GEMINI_MODEL})...")
    t0 = time.time()
    try:
        markdown = call_gemini(compressed, week_label)
        elapsed = time.time() - t0
        print(f"  ✓ Gemini returned {len(markdown):,} chars in {elapsed:.1f}s")
    except Exception as e:
        print(f"  ✗ Gemini failed: {e}")
        return {"pass": pass_num, "success": False, "error": str(e), "fetch_method": fetch_method}

    # Step 5: Parse
    print(f"\n[5] Parsing schedule...")
    schedule = parse_schedule(markdown)
    date_keys = sorted(k for k in schedule if k != "weekly")
    print(f"  ✓ {len(date_keys)} date-keyed sections + {'weekly' if 'weekly' in schedule else 'no weekly key'}")
    for k in date_keys:
        snippet = schedule[k].split("\n")[0]
        print(f"     {k}: {snippet[:70]}")

    # Step 6: Today / Tomorrow
    from datetime import timedelta
    today_iso = date.today().isoformat()
    tomorrow_iso = (date.today() + timedelta(days=1)).isoformat()

    print(f"\n[6] Today ({today_iso}):")
    today_content = schedule.get(today_iso) or schedule.get("weekly") or "(not found)"
    print(today_content[:800] + ("..." if len(today_content) > 800 else ""))

    print(f"\n[7] Tomorrow ({tomorrow_iso}):")
    tomorrow_content = schedule.get(tomorrow_iso) or schedule.get("weekly") or "(not found)"
    print(tomorrow_content[:800] + ("..." if len(tomorrow_content) > 800 else ""))

    return {
        "pass": pass_num,
        "success": True,
        "fetch_method": fetch_method,
        "compress_method": compress_method,
        "markdown_chars": len(markdown),
        "date_keys": date_keys,
        "has_today": today_iso in schedule,
        "has_tomorrow": tomorrow_iso in schedule,
        "schedule": schedule,
        "full_markdown": markdown,
    }


async def run_test(use_local_pdf: str | None = None, num_runs: int = 1):
    import aiohttp

    print("=" * 60)
    print("BJC Newsletter Integration — Local Test")
    print("=" * 60)

    if not GEMINI_API_KEY:
        print("ERROR: Set the GEMINI_API_KEY environment variable before running.")
        print("  export GEMINI_API_KEY='your-key-here'")
        sys.exit(1)

    # Step 1: Scrape BJC homepage
    print("\n[1] Scraping BJC homepage for newsletter URL...")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                BJC_HOMEPAGE_URL,
                timeout=aiohttp.ClientTimeout(total=30),
                headers=BROWSER_HEADERS,
            ) as resp:
                html = await resp.text()
        except Exception as e:
            print(f"  ✗ Could not reach BJC homepage: {e}")
            html = ""

    newsletter_url = None
    if html:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        account = BJC_FLIPSNACK_ACCOUNT

        # Slugs that are never the weekly newsletter
        EXCLUDE = {"calendar", "annual-report", "yizkor", "haggadah", "bulletin"}
        # Slugs that look like one-off supplements (lower priority)
        SUPPLEMENT_HINTS = {"schedule", "supplement", "special", "flyer", "announcement"}
        READ_NOW_PHRASES = {"read it now", "read now", "click here to read", "view now"}

        def _clean(href):
            return href.split("?")[0].rstrip("/")

        def _is_bjc(href):
            return "flipsnack.com" in href and account in href

        def _excluded(href):
            return any(e in slug_from_url(href).lower() for e in EXCLUDE)

        def _supplement(href):
            return any(h in slug_from_url(href).lower() for h in SUPPLEMENT_HINTS)

        all_links = soup.find_all("a", href=True)

        # Strategy 1: h2 "BJC Insider" section
        for h2 in soup.find_all("h2"):
            if "bjc insider" in h2.get_text(strip=True).lower():
                parent = h2.parent
                if parent:
                    for a in parent.find_all("a", href=True):
                        if _is_bjc(a["href"]) and not _excluded(a["href"]):
                            newsletter_url = _clean(a["href"])
                            break
                if newsletter_url:
                    break

        # Strategy 2: "READ IT NOW" / "READ NOW" CTA links
        if not newsletter_url:
            for a in all_links:
                if _is_bjc(a["href"]) and not _excluded(a["href"]):
                    if any(p in a.get_text(strip=True).lower() for p in READ_NOW_PHRASES):
                        newsletter_url = _clean(a["href"])
                        break

        # Strategy 3: any non-excluded link, prefer non-supplement slugs
        if not newsletter_url:
            main, supps = [], []
            for a in all_links:
                if _is_bjc(a["href"]) and not _excluded(a["href"]):
                    (_supplement(a["href"]) and supps or main).append(_clean(a["href"]))
            newsletter_url = (main or supps or [None])[0]

    if newsletter_url:
        print(f"  ✓ Found newsletter: {newsletter_url}")
    else:
        print(f"  ✗ Not found on homepage — no newsletter link detected")

    # Run passes
    results = []
    async with aiohttp.ClientSession() as session:
        for i in range(1, num_runs + 1):
            result = await run_single_pass(session, newsletter_url, use_local_pdf, i)
            results.append(result)
            if num_runs > 1 and i < num_runs:
                print(f"\nWaiting 5s before next pass...")
                await asyncio.sleep(5)

    # Summary
    print(f"\n{'='*60}")
    print(f"SUMMARY ({num_runs} pass{'es' if num_runs > 1 else ''})")
    print(f"{'='*60}")
    successes = [r for r in results if r["success"]]
    print(f"  Passed: {len(successes)}/{num_runs}")
    for r in results:
        status = "✓" if r["success"] else "✗"
        if r["success"]:
            print(f"  Pass {r['pass']}: {status} fetch={r['fetch_method']} | "
                  f"{r['markdown_chars']:,} chars | {len(r['date_keys'])} days | "
                  f"today={'yes' if r['has_today'] else 'NO'} | "
                  f"tomorrow={'yes' if r.get('has_tomorrow') else 'NO'}")
        else:
            print(f"  Pass {r['pass']}: {status} FAILED — {r.get('error', '?')} (fetch={r['fetch_method']})")

    # Save last successful result
    if successes:
        last = successes[-1]
        output_path = Path(__file__).parent / "test_output.json"
        output = {
            "newsletter_url": newsletter_url,
            "week_label": week_label_from_slug(slug_from_url(newsletter_url)),
            "fetch_method": last["fetch_method"],
            "compress_method": last["compress_method"],
            "schedule": last["schedule"],
            "full_markdown": last["full_markdown"],
        }
        output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n✓ Output saved to: {output_path}")

    print(f"\n{'='*60}")
    print("TEST COMPLETE")
    print(f"{'='*60}")


if __name__ == "__main__":
    args = sys.argv[1:]
    local_pdf = None
    num_runs = 1

    if "--runs" in args:
        idx = args.index("--runs")
        try:
            num_runs = int(args[idx + 1])
            args = args[:idx] + args[idx + 2:]
        except (IndexError, ValueError):
            pass

    if args and not args[0].startswith("--"):
        local_pdf = args[0]

    asyncio.run(run_test(use_local_pdf=local_pdf, num_runs=num_runs))
