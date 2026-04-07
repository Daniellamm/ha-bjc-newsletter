"""Browser-based Flipsnack PDF fetcher using Playwright.

How it works:
  1. Opens the Flipsnack page in a real headless Chromium browser
  2. Intercepts the signed data.json CloudFront URL (contains a time-limited
     Signature token that unlocks all page images)
  3. Downloads every page as a high-res JPEG using the signed token
  4. Assembles all pages into a multi-page PDF via Pillow

This bypasses all Flipsnack 403 blocks because a real browser visiting the
page generates a valid signed URL automatically.

Usage:
    pip install playwright pillow
    python3 -m playwright install chromium
    python3 test_pdf_fetch.py
    python3 test_pdf_fetch.py "https://www.flipsnack.com/7BBDB688B7A/pesach-2026-_-5786"

Output is saved to ~/bjc_newsletter_pdfs/<slug>.pdf
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

OUTPUT_DIR = Path.home() / "bjc_newsletter_pdfs"
DEFAULT_URL = "https://www.flipsnack.com/7BBDB688B7A/pesach-2026-_-5786"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.flipsnack.com/",
}


async def get_signed_data(flipsnack_url: str) -> dict | None:
    """Open the Flipsnack page and capture the signed data.json URL + content.

    Returns a dict with:
        cdn_base      — CloudFront CDN base path for this collection
        signed_qs     — signed query string (Signature=...&Key-Pair-Id=...&Policy=...)
        item_hash     — the original item hash used in image paths
        page_count    — total number of pages
    """
    from playwright.async_api import async_playwright

    captured: dict = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )

        async def on_response(resp):
            if "data.json" in resp.url and resp.status == 200:
                try:
                    body = await resp.body()
                    captured["data_json_url"] = resp.url
                    captured["data_json"] = json.loads(body)
                except Exception:
                    pass

        page = await context.new_page()
        page.on("response", on_response)

        # Use full-view URL — loads the embedded flipbook widget which fetches data.json
        full_view_url = flipsnack_url.rstrip("/") + "/full-view.html"
        if "full-view.html" in flipsnack_url:
            full_view_url = flipsnack_url

        print(f"  Opening: {full_view_url}")
        try:
            await page.goto(full_view_url, wait_until="networkidle", timeout=30_000)
        except Exception as nav_err:
            print(f"  Nav warning (continuing): {nav_err}")

        # Give lazy-loaded content a moment to fetch data.json
        await asyncio.sleep(3)
        await browser.close()

    if "data_json_url" not in captured:
        print("  ✗ data.json was not captured — page may not have loaded correctly")
        return None

    data_url = captured["data_json_url"]
    data = captured["data_json"]

    # Parse the signed query string from the data.json URL
    qs_match = re.search(r"\?(.+)$", data_url)
    signed_qs = qs_match.group(1) if qs_match else ""
    cdn_base = data_url.split("/data.json")[0]

    # --- Page count ---
    # Strategy 1: toc[0].sub list (older flipbooks)
    toc = data.get("toc", [])
    toc_pages = toc[0].get("sub", []) if toc else []
    page_count = len(toc_pages) if toc_pages else 0
    # Strategy 2: pages.order array (newer flipbooks)
    pages_info = data.get("pages", {})
    if page_count == 0:
        page_count = len(pages_info.get("order", []))

    # --- Item hash ---
    # Strategy 1: toc[0].originalHash (older flipbooks)
    item_hash = toc[0].get("originalHash") if toc else None
    # Strategy 2: pages.data[first_id].source.hash (newer flipbooks)
    if not item_hash:
        pages_data = pages_info.get("data", {})
        order = pages_info.get("order", [])
        if order and order[0] in pages_data:
            item_hash = pages_data[order[0]].get("source", {}).get("hash")

    # --- Extracted text (newer flipbooks embed page text in data.json) ---
    extracted_text: str | None = None
    pages_data = pages_info.get("data", {})
    order = pages_info.get("order", [])
    if pages_data and order:
        chunks = []
        for pid in order:
            txt = pages_data.get(pid, {}).get("extractedText", "")
            if txt:
                chunks.append(txt.strip())
        if chunks:
            extracted_text = "\n\n".join(chunks)

    print(f"  CDN base:   {cdn_base}")
    print(f"  Item hash:  {item_hash}")
    print(f"  Pages:      {page_count}")
    print(f"  Token len:  {len(signed_qs)} chars")
    if extracted_text:
        print(f"  Extracted text: {len(extracted_text)} chars (no image download needed)")

    return {
        "cdn_base": cdn_base,
        "signed_qs": signed_qs,
        "item_hash": item_hash,
        "page_count": page_count,
        "extracted_text": extracted_text,
    }


def download_page_image(cdn_base: str, item_hash: str, page_num: int, signed_qs: str) -> bytes | None:
    """Download a single page image. Tries 'large' then 'medium' sizes."""
    for size in ("large", "medium", "small"):
        url = f"{cdn_base}/items/{item_hash}/covers/page_{page_num}/{size}?{signed_qs}"
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError:
            continue
        except Exception as e:
            print(f"    page {page_num} ({size}) error: {e}")
            continue
    return None


def images_to_pdf(image_bytes_list: list[bytes]) -> bytes:
    """Convert a list of JPEG image bytes into a single multi-page PDF."""
    from PIL import Image

    pil_images = []
    for raw in image_bytes_list:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        pil_images.append(img)

    if not pil_images:
        raise ValueError("No images to convert to PDF")

    out = io.BytesIO()
    pil_images[0].save(
        out,
        format="PDF",
        save_all=True,
        append_images=pil_images[1:],
        resolution=150,
    )
    return out.getvalue()


async def fetch_pdf_browser(flipsnack_url: str) -> bytes | None:
    """Full pipeline: browser → signed token → download pages → assemble PDF.

    If data.json contains embedded extractedText, returns that as UTF-8 bytes
    (no image download or PDF assembly needed — much faster).
    """

    # Step 1: Get signed token via browser
    print("\n[1] Launching headless browser to capture signed token...")
    signed = await get_signed_data(flipsnack_url)
    if not signed:
        return None

    # Fast path: text already embedded in data.json (newer PDF-based flipbooks)
    if signed.get("extracted_text"):
        print("\n[2] Using embedded extractedText — skipping image download")
        return signed["extracted_text"].encode("utf-8")

    cdn_base = signed["cdn_base"]
    signed_qs = signed["signed_qs"]
    item_hash = signed["item_hash"]
    page_count = signed["page_count"]

    if not item_hash or page_count == 0:
        print("  ✗ Could not determine item hash or page count from data.json")
        return None

    # Step 2: Download all page images
    print(f"\n[2] Downloading {page_count} page images...")
    page_images: list[bytes] = []
    failed = 0
    for n in range(1, page_count + 1):
        img = download_page_image(cdn_base, item_hash, n, signed_qs)
        if img:
            size_kb = len(img) // 1024
            print(f"  page {n:>2}/{page_count}: {size_kb}KB ✓")
            page_images.append(img)
        else:
            print(f"  page {n:>2}/{page_count}: ✗ FAILED")
            failed += 1

    if not page_images:
        print("  ✗ No pages downloaded")
        return None

    if failed > 0:
        print(f"  ⚠ {failed} pages failed — PDF will have {len(page_images)}/{page_count} pages")

    # Step 3: Assemble PDF
    print(f"\n[3] Assembling {len(page_images)} pages into PDF...")
    try:
        pdf_bytes = images_to_pdf(page_images)
        print(f"  ✓ PDF assembled: {len(pdf_bytes)//1_000_000}MB ({len(pdf_bytes):,} bytes)")
        return pdf_bytes
    except Exception as e:
        print(f"  ✗ PDF assembly failed: {e}")
        return None


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    print("=" * 60)
    print("Browser-based Flipsnack PDF Fetcher")
    print("=" * 60)
    print(f"Target URL: {url}")
    print(f"Runs:       {runs}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = url.rstrip("/").rsplit("/", 1)[-1].replace("/full-view.html", "")

    results = []
    for run_num in range(1, runs + 1):
        if runs > 1:
            print(f"\n{'='*40} RUN {run_num}/{runs} {'='*40}")
        t0 = time.time()
        pdf_bytes = await fetch_pdf_browser(url)
        elapsed = time.time() - t0

        if pdf_bytes:
            out_path = OUTPUT_DIR / f"{slug}.pdf"
            out_path.write_bytes(pdf_bytes)
            valid = pdf_bytes[:4] == b"%PDF"
            print(f"\n✓ RUN {run_num} SUCCESS in {elapsed:.1f}s")
            print(f"  Size:  {len(pdf_bytes)//1_000_000}MB ({len(pdf_bytes):,} bytes)")
            print(f"  Saved: {out_path}")
            print(f"  Valid: {'YES' if valid else 'NO — does not start with %PDF'}")
            results.append({"run": run_num, "ok": True, "size": len(pdf_bytes), "elapsed": elapsed})
        else:
            print(f"\n✗ RUN {run_num} FAILED in {elapsed:.1f}s")
            results.append({"run": run_num, "ok": False, "elapsed": elapsed})

        if run_num < runs:
            print("  Waiting 5s before next run...")
            await asyncio.sleep(5)

    if runs > 1:
        print(f"\n{'='*60}")
        print(f"SUMMARY: {sum(1 for r in results if r['ok'])}/{runs} successful")
        for r in results:
            status = "✓" if r["ok"] else "✗"
            size_str = f" {r['size']//1_000_000}MB" if r.get("size") else ""
            print(f"  Run {r['run']}: {status}{size_str} in {r['elapsed']:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
