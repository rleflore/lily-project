"""
Direct test script — run this from the command line to see full debug output.

Usage:
    python test_fetch.py
"""

import sys
import json
sys.stdout.reconfigure(line_buffering=True)

from perusall_to_pdf import fetch_pdf, is_logged_in, SESSION_DIR
from pathlib import Path
from playwright.sync_api import sync_playwright
import time

URL = "https://app.perusall.com/courses/amst203-wb12-popular-culture-in-america/1freccero-218278326?assignmentId=ggmcBWFcPTqP3qQbw&part=1&panel=assignmentInformation&filter=all"

print("=" * 60)
print("  Perusall Text Content — Debug Test")
print("=" * 60)
print()

print(f"[*] Session exists: {is_logged_in()}")
print(f"[*] Target URL: {URL[:80]}...")
print()

# Open browser and capture text-content JSON files
with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        user_data_dir=str(SESSION_DIR),
        headless=False,
        viewport={"width": 1400, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )

    page = browser.pages[0] if browser.pages else browser.new_page()
    text_jsons = []

    def capture_response(response):
        url = response.url
        if "text-content" in url and ".json" in url:
            text_jsons.append((url, response))
            print(f"[*] Captured text-content: {url[:100]}", flush=True)

    page.on("response", capture_response)

    print("[*] Navigating...", flush=True)
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    time.sleep(5)

    # Re-navigate if redirected
    if "1freccero" not in page.url:
        print("[*] Redirected, going back to article...", flush=True)
        page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(5)

    print("[*] Waiting for document to load...", flush=True)
    time.sleep(10)

    # Scroll through all pages to trigger text-content loading
    print("[*] Scrolling through pages to load all text...", flush=True)
    page_elements = page.query_selector_all('[class*="page"]')
    print(f"[*] Found {len(page_elements)} page elements", flush=True)

    for i, elem in enumerate(page_elements):
        try:
            elem.scroll_into_view_if_needed(timeout=5000)
            time.sleep(0.5)
        except Exception:
            pass

    # Wait for remaining network requests
    time.sleep(5)

    print(f"\n[*] Total text-content JSONs captured: {len(text_jsons)}")

    # Dump the first JSON to see the format
    if text_jsons:
        print("\n[*] Sample text-content JSON structure:")
        print("-" * 40)
        try:
            body = text_jsons[0][1].body()
            data = json.loads(body)
            # Pretty print first 2000 chars
            formatted = json.dumps(data, indent=2)
            print(formatted[:3000])
            if len(formatted) > 3000:
                print(f"\n... ({len(formatted)} total chars)")
        except Exception as e:
            print(f"Error reading JSON: {e}")

        # Save all text content for inspection
        output_dir = Path(__file__).parent / "debug_output"
        output_dir.mkdir(exist_ok=True)
        for i, (url, resp) in enumerate(text_jsons):
            try:
                body = resp.body()
                with open(output_dir / f"text_page_{i}.json", "wb") as f:
                    f.write(body)
            except Exception:
                pass
        print(f"\n[*] Saved {len(text_jsons)} JSON files to debug_output/")

    browser.close()

print("\n[*] Done!")
