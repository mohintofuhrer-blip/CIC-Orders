"""
cic_orders.py - collect Central Information Commission (CIC) decisions.

Setup (once):
    pip install playwright
    playwright install chromium

Usage:
    python cic_orders.py discover   # first run: shows how the portal loads its data
    python cic_orders.py run        # collect orders, download PDFs, flag new ones

Output (folder ./cic_output):
    orders.csv          every order seen so far (one row per order)
    new_orders.csv      orders found in the latest run only
    pdfs/               downloaded order PDFs
    seen.json           memory of what was already collected
    discover/           screenshot, HTML and network log from `discover`

NOTE: written without access to the live portal, so the page selectors
are best guesses. Run `discover` first and adjust the CONFIG block if needed.
"""
import asyncio
import csv
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright

# ---------------- CONFIG ----------------
URL = "https://dss.cic.gov.in/citizen-service?tab=decision"
OUT = Path("cic_output")
PDF_DIR = OUT / "pdfs"
DISCOVER_DIR = OUT / "discover"
SEEN_FILE = OUT / "seen.json"
ALL_CSV = OUT / "orders.csv"
NEW_CSV = OUT / "new_orders.csv"
MAX_PAGES = 5          # result pages to walk per run (newest first)
HEADLESS = True        # set False to watch the browser (useful when debugging)
NEXT_SELECTORS = [     # tried in order to go to the next results page
    'button:has-text("Next")',
    'a:has-text("Next")',
    '[aria-label="Next"]',
    'li.next a',
]
# ----------------------------------------

FIELDS = ["found_at", "key", "case_info", "link", "pdf_file", "row_text"]

JS_EXTRACT_ROWS = """
() => {
  const rows = [];
  document.querySelectorAll('tr').forEach(tr => {
    const text = tr.innerText.replace(/\\s+/g, ' ').trim();
    if (!text) return;
    const a = tr.querySelector('a[href]');
    rows.push({text, href: a ? a.href : ''});
  });
  if (rows.length === 0) {   // fallback for card/list layouts: any link that looks like an order
    document.querySelectorAll('a[href]').forEach(a => {
      if (/pdf|decision|download|view/i.test(a.href + a.innerText)) {
        const box = a.closest('li, div') || a;
        rows.push({text: box.innerText.replace(/\\s+/g, ' ').trim(), href: a.href});
      }
    });
  }
  return rows;
}
"""


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def make_key(row):
    base = row["href"] or row["text"]
    return hashlib.sha1(base.encode()).hexdigest()[:16]


async def open_page(p):
    browser = await p.chromium.launch(headless=HEADLESS)
    context = await browser.new_context(accept_downloads=True)
    page = await context.new_page()
    await page.goto(URL, wait_until="networkidle", timeout=90000)
    await page.wait_for_timeout(3000)  # let the JS app render
    return browser, context, page


async def discover():
    """Save a screenshot, the rendered HTML and every JSON call the page makes."""
    DISCOVER_DIR.mkdir(parents=True, exist_ok=True)
    calls = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context()
        page = await context.new_page()

        async def on_response(resp):
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                try:
                    body = await resp.text()
                except Exception:
                    body = ""
                calls.append({"url": resp.url, "method": resp.request.method,
                              "post_data": resp.request.post_data, "sample": body[:1500]})

        page.on("response", on_response)
        await page.goto(URL, wait_until="networkidle", timeout=90000)
        await page.wait_for_timeout(5000)
        await page.screenshot(path=str(DISCOVER_DIR / "page.png"), full_page=True)
        (DISCOVER_DIR / "page.html").write_text(await page.content(), encoding="utf-8")
        await browser.close()
    (DISCOVER_DIR / "network_calls.json").write_text(json.dumps(calls, indent=2), encoding="utf-8")
    print(f"Saved to {DISCOVER_DIR}/. JSON calls found: {len(calls)}")
    print("If the page asks for a search or captcha first, set HEADLESS=False and look at it.")


async def run():
    OUT.mkdir(exist_ok=True)
    PDF_DIR.mkdir(exist_ok=True)
    seen = load_seen()
    new_rows = []

    async with async_playwright() as p:
        browser, context, page = await open_page(p)

        for page_no in range(1, MAX_PAGES + 1):
            rows = await page.evaluate(JS_EXTRACT_ROWS)
            fresh_on_page = 0
            for r in rows:
                key = make_key(r)
                if key in seen:
                    continue
                fresh_on_page += 1
                pdf_file = ""
                link = r["href"]
                if link:
                    link = urljoin(URL, link)
                    try:
                        resp = await context.request.get(link, timeout=60000)
                        if resp.ok and "pdf" in resp.headers.get("content-type", "").lower():
                            pdf_file = f"{key}.pdf"
                            (PDF_DIR / pdf_file).write_bytes(await resp.body())
                    except Exception as e:
                        print(f"  could not fetch {link}: {e}")
                entry = {
                    "found_at": datetime.now().isoformat(timespec="seconds"),
                    "key": key,
                    "case_info": re.sub(r"\s+", " ", r["text"])[:120],
                    "link": link,
                    "pdf_file": pdf_file,
                    "row_text": r["text"],
                }
                seen[key] = entry
                new_rows.append(entry)

            print(f"Page {page_no}: {len(rows)} rows, {fresh_on_page} new")
            if fresh_on_page == 0:
                break  # reached orders we already have

            # go to the next results page
            clicked = False
            for sel in NEXT_SELECTORS:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_enabled():
                    await btn.click()
                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(1500)
                    clicked = True
                    break
            if not clicked:
                break

        await browser.close()

    SEEN_FILE.write_text(json.dumps(seen, indent=2), encoding="utf-8")

    # all orders
    with ALL_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(seen.values())
    # new orders only
    with NEW_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(new_rows)

    print(f"Done. {len(new_rows)} new orders. Total tracked: {len(seen)}.")
    if not new_rows and not seen:
        print("Nothing found. Run `python cic_orders.py discover` and check the output.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    asyncio.run(discover() if mode == "discover" else run())
