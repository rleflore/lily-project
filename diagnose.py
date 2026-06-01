"""
Diagnostic: dump all captured text content as plain text so we can see gaps.
"""
import sys
import json
sys.stdout.reconfigure(line_buffering=True)

from perusall_to_pdf import is_logged_in, SESSION_DIR
from pathlib import Path
from playwright.sync_api import sync_playwright
import time

URL = "https://app.perusall.com/courses/amst203-wb12-popular-culture-in-america/1freccero-218278326?assignmentId=ggmcBWFcPTqP3qQbw&part=1&panel=assignmentInformation&filter=all"

print("=" * 60)
print("  Text Content Diagnostic")
print("=" * 60)

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        user_data_dir=str(SESSION_DIR),
        headless=False,
        viewport={"width": 1400, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )

    page = browser.pages[0] if browser.pages else browser.new_page()
    text_jsons = []
    all_urls = []

    def capture_response(response):
        url = response.url
        content_type = response.headers.get("content-type", "")
        if "text-content" in url and ".json" in url:
            text_jsons.append((url, response))
            print(f"  [text-content] {url.split('/')[-1].split('?')[0]}", flush=True)
        # Log ALL cloudfront responses
        if "cloudfront" in url:
            all_urls.append((url, content_type, response))

    page.on("response", capture_response)

    print("[*] Navigating...", flush=True)
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    time.sleep(5)

    if URL.split("perusall.com")[-1].split("?")[0] not in page.url:
        print("[*] Redirected, going back...", flush=True)
        page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(5)

    print("[*] Waiting for viewer...", flush=True)
    time.sleep(10)

    # Scroll through all pages slowly
    page_elements = page.query_selector_all('[class*="page"]')
    print(f"[*] Scrolling through {len(page_elements)} pages...", flush=True)

    for i, elem in enumerate(page_elements):
        try:
            elem.scroll_into_view_if_needed(timeout=5000)
            time.sleep(2)  # Extra slow
        except Exception:
            pass

    time.sleep(5)

    # Second pass even slower
    print(f"[*] Second pass...", flush=True)
    for i, elem in enumerate(page_elements):
        try:
            elem.scroll_into_view_if_needed(timeout=5000)
            time.sleep(1.5)
        except Exception:
            pass

    time.sleep(5)

    print(f"\n[*] Captured {len(text_jsons)} text-content JSONs")
    print(f"[*] Total CloudFront responses: {len(all_urls)}")

    # Show all cloudfront URL types
    print("\n[*] CloudFront URL patterns:")
    patterns = set()
    for url, ct, _ in all_urls:
        # Extract path pattern
        path = url.split("cloudfront.net/")[1].split("?")[0] if "cloudfront.net/" in url else url
        pattern = path.split("/")[0]  # First path segment
        patterns.add(f"  {pattern}/ ({ct})")
    for p in sorted(patterns):
        print(p)

    # Dump text from first 3 pages
    print("\n" + "=" * 60)
    print("TEXT DUMP - First 2 pages (check for gaps)")
    print("=" * 60)

    for page_idx, (url, resp) in enumerate(text_jsons[:2]):
        try:
            body = resp.body()
            data = json.loads(body)
            items = data.get("items", [])

            print(f"\n{'='*40}")
            print(f"PAGE {page_idx + 1} ({len(items)} text items)")
            print(f"{'='*40}")

            # Sort items by Y position (top to bottom)
            positioned = []
            for item in items:
                if item.get("transform") and len(item["transform"]) >= 6:
                    positioned.append(item)

            # Sort by y descending (top of page = highest y in PDF coords)
            positioned.sort(key=lambda i: -i["transform"][5])

            last_y = None
            for item in positioned:
                text = item.get("str", "")
                y = item["transform"][5]
                x = item["transform"][4]

                # Add blank line if there's a big y gap
                if last_y is not None and (last_y - y) > 20:
                    print()

                if text:
                    print(f"  [{y:6.0f},{x:4.0f}] {text}")
                last_y = y

        except Exception as e:
            print(f"  Error: {e}")

    browser.close()

print("\n[*] Done!")
