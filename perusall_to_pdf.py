"""
Perusall Article to PDF Converter — Core Logic

This module handles browser automation to extract PDFs from Perusall.
Used by both the CLI login command and the Flask web app.
"""

import os
import time
import re
from io import BytesIO
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

SESSION_DIR = Path(__file__).parent / ".auth_session"
OUTPUT_DIR = Path(__file__).parent / "downloads"


def ensure_dirs():
    SESSION_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)


def login_interactive():
    """Open a browser for the user to log in and save the session."""
    ensure_dirs()
    print("[*] Opening browser for login...")
    print("    Log in with your Google university account.")
    print("    Once you see your Perusall dashboard, you can close the browser.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=False,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto("https://app.perusall.com", wait_until="domcontentloaded")

        print("    Waiting for you to log in (up to 5 minutes)...")
        try:
            page.wait_for_url(
                lambda url: "perusall.com/courses" in url or "perusall.com/home" in url,
                timeout=300000,
            )
            print("\n[+] Login successful! Session saved.")
            print("    You can now start the web server with: python app.py")
        except PlaywrightTimeout:
            print("\n[!] Login timed out. Please try again.")
        finally:
            time.sleep(2)
            browser.close()


def is_logged_in():
    """Check if a saved session exists."""
    return SESSION_DIR.exists() and any(SESSION_DIR.iterdir())


def fetch_pdf(article_url, debug=False):
    """
    Fetch a PDF from Perusall using the saved session.
    Returns (pdf_bytes, filename) or raises an exception.
    """
    ensure_dirs()

    if not is_logged_in():
        raise RuntimeError("Not logged in. Run 'python app.py --login' first.")

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=False,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = browser.pages[0] if browser.pages else browser.new_page()
        pdf_data = None
        debug_log = []

        def log(msg):
            debug_log.append(msg)
            if debug:
                print(f"[DEBUG] {msg}", flush=True)

        try:
            # Collect network responses
            all_responses = []
            text_content_jsons = []

            def capture_response(response):
                content_type = response.headers.get("content-type", "")
                url = response.url
                all_responses.append((url, content_type, response))

                # Track text-content JSON files (our primary target)
                if "text-content" in url and ".json" in url:
                    text_content_jsons.append((url, response))
                    log(f"  📝 Text content: {url[:100]}")

                # Log PDF responses
                elif any(kw in url.lower() for kw in ["pdf", "document-file", "original-file"]):
                    log(f"  → {response.status} | {content_type[:40]} | {url[:120]}")

            page.on("response", capture_response)

            # Step 1: Navigate to the article
            log(f"Navigating to: {article_url}")
            page.goto(article_url, wait_until="domcontentloaded", timeout=45000)

            # Step 2: Wait for potential OAuth redirect to complete
            log("Waiting for auth redirect to settle...")
            time.sleep(5)

            current_url = page.url
            log(f"Current URL: {current_url}")

            # If we got redirected away from the article (OAuth flow), go back
            article_path = article_url.split("perusall.com")[-1].split("?")[0]
            if article_path not in current_url:
                if "perusall.com" in current_url:
                    log("Redirected after auth — navigating back to article...")
                    page.goto(article_url, wait_until="domcontentloaded", timeout=45000)
                    time.sleep(5)
                    current_url = page.url
                    log(f"URL after re-navigation: {current_url}")

            # Check if login failed entirely
            if "login" in current_url and "perusall" not in current_url:
                browser.close()
                raise RuntimeError(
                    "Session expired — redirected to login. "
                    "Run 'python app.py --login' again."
                )

            # Step 3: Wait for document viewer to load
            log("Waiting for document viewer to load...")
            time.sleep(10)

            # Step 4: Scroll through all pages to trigger text-content loading
            log("Scrolling through pages to load all text content...")
            page_elements = page.query_selector_all('[class*="page"]')
            log(f"Found {len(page_elements)} page elements")

            for i, elem in enumerate(page_elements):
                try:
                    elem.scroll_into_view_if_needed(timeout=5000)
                    time.sleep(0.8)
                except Exception:
                    pass

            # Wait for remaining network requests
            time.sleep(5)
            log(f"Text content JSONs captured: {len(text_content_jsons)}")

            # Strategy 1: Check for direct PDF in responses
            pdf_data = _check_responses_for_pdf(all_responses, log)

            # Strategy 2: Build text PDF from text-content JSONs (primary strategy)
            if not pdf_data:
                pdf_data = _build_text_pdf(text_content_jsons, log)

            # Strategy 3: Screenshot fallback
            if not pdf_data:
                pdf_data = _screenshot_to_pdf(page, log)

        finally:
            browser.close()

        if not pdf_data:
            error_msg = "Could not extract PDF.\n\nDebug info:\n" + "\n".join(debug_log[-20:])
            raise RuntimeError(error_msg)

        # Generate filename from URL
        slug = article_url.rstrip("/").split("/")[-1].split("?")[0][:50]
        filename = re.sub(r"[^\w\-]", "_", slug) + ".pdf"

        return pdf_data, filename


def _check_responses_for_pdf(all_responses, log):
    """Check captured network responses for PDF data."""
    log("Checking network responses for PDF content...")
    for url, content_type, resp in all_responses:
        if ("application/pdf" in content_type or
                url.endswith(".pdf") or
                "document-file" in url or
                "original-file" in url):
            try:
                body = resp.body()
                if body[:4] == b"%PDF":
                    log(f"Found PDF in response: {url[:100]}")
                    return body
            except Exception:
                continue
    log("No direct PDF found in network responses.")
    return None


def _build_text_pdf(text_content_jsons, log):
    """
    Build a text-based PDF from Perusall's text-content JSON files.
    Each JSON contains positioned text items for one page.
    
    Transform format: [fontSize, 0, 0, fontSize, x, y]
    - Index 0: font size
    - Index 4: x position (from left)
    - Index 5: y position (from bottom, PDF coordinate system)
    """
    import json
    from fpdf import FPDF

    if not text_content_jsons:
        log("No text-content JSONs available.")
        return None

    log(f"Building text PDF from {len(text_content_jsons)} pages...")

    # Parse all page JSONs
    pages_data = []
    for url, resp in text_content_jsons:
        try:
            body = resp.body()
            data = json.loads(body)
            if "items" in data and data["items"]:
                pages_data.append(data["items"])
        except Exception as e:
            log(f"  Failed to parse JSON: {e}")
            continue

    if not pages_data:
        log("No valid text data found in JSONs.")
        return None

    log(f"Parsed {len(pages_data)} pages of text content")

    # Determine page dimensions from the text positions
    # Standard PDF page is 612x792 points (US Letter)
    PAGE_WIDTH_PT = 612
    PAGE_HEIGHT_PT = 792

    # Create PDF with fpdf2
    pdf = FPDF(unit="pt", format="letter")
    pdf.set_auto_page_break(auto=False)

    for page_idx, items in enumerate(pages_data):
        pdf.add_page()

        # Find the bounding box of text on this page to determine scale
        max_y = 0
        min_y = float('inf')
        max_x = 0
        for item in items:
            if not item.get("str") or not item.get("transform"):
                continue
            t = item["transform"]
            if len(t) >= 6:
                y = t[5]
                x = t[4]
                if y > max_y:
                    max_y = y
                if y < min_y:
                    min_y = y
                if x + item.get("width", 0) > max_x:
                    max_x = x + item.get("width", 0)

        # Scale factor to fit content to page
        content_height = max_y - min_y if max_y > min_y else PAGE_HEIGHT_PT
        scale_x = PAGE_WIDTH_PT / max(max_x, PAGE_WIDTH_PT) if max_x > 0 else 1.0
        scale_y = (PAGE_HEIGHT_PT - 40) / content_height if content_height > 0 else 1.0
        scale = min(scale_x, scale_y, 1.3)  # Don't scale up too much

        for item in items:
            text = item.get("str", "")
            if not text:
                continue

            transform = item.get("transform", [10, 0, 0, 10, 0, 0])
            if len(transform) < 6:
                continue

            font_size = transform[0]
            x = transform[4]
            # Flip y-coordinate (PDF origin is bottom-left, fpdf is top-left)
            y = max_y - transform[5]

            # Apply scaling
            x_scaled = x * scale + 20  # 20pt left margin
            y_scaled = y * scale + 30  # 30pt top margin
            font_size_scaled = font_size * scale

            # Clamp to page bounds
            if y_scaled > PAGE_HEIGHT_PT - 20 or x_scaled > PAGE_WIDTH_PT - 20:
                continue

            # Determine if bold based on font name
            font_name = item.get("fontName", "")
            style = ""
            if "bold" in font_name.lower() or "f1" in font_name:
                style = "B"

            try:
                pdf.set_font("Helvetica", style=style, size=max(6, min(font_size_scaled, 24)))
                pdf.set_xy(x_scaled, y_scaled)
                pdf.cell(text=text)
            except Exception:
                pass

    pdf_bytes = pdf.output()
    log(f"Text PDF built: {len(pdf_bytes)} bytes, {len(pages_data)} pages")
    return bytes(pdf_bytes)


def _screenshot_to_pdf(page, log):
    """Fallback: screenshot the Perusall viewer pages."""
    from PIL import Image
    from fpdf import FPDF

    log("Falling back to screenshot mode...")
    time.sleep(3)
    screenshots = []
    tmp_dir = Path(__file__).parent / ".tmp_screenshots"
    tmp_dir.mkdir(exist_ok=True)

    # Perusall-specific selectors for the document viewer
    selectors = [
        'canvas',
        '[class*="Page"]',
        '[class*="page"]',
        '[data-page-number]',
        '[class*="document"] canvas',
        '.reader canvas',
        '[role="document"] canvas',
    ]

    page_elements = []
    for selector in selectors:
        elems = page.query_selector_all(selector)
        if elems:
            log(f"Found {len(elems)} elements with selector: {selector}")
            # Filter to only visible, reasonably-sized elements
            for elem in elems:
                try:
                    box = elem.bounding_box()
                    if box and box["width"] > 100 and box["height"] > 100:
                        page_elements.append(elem)
                except Exception:
                    continue
            if page_elements:
                break

    if page_elements:
        log(f"Capturing {len(page_elements)} page elements...")
        for i, elem in enumerate(page_elements):
            try:
                elem.scroll_into_view_if_needed(timeout=5000)
                time.sleep(1)
                path = tmp_dir / f"page_{i}.png"
                elem.screenshot(path=str(path))
                screenshots.append(path)
                log(f"  Captured page {i + 1}")
            except Exception as e:
                log(f"  Skip element {i}: {e}")
                continue
    else:
        # Full page scroll capture
        log("No page elements found, doing full-page scroll capture...")
        viewport_height = page.viewport_size["height"]
        total_height = page.evaluate("document.body.scrollHeight")
        scroll_pos = 0
        i = 0
        while scroll_pos < total_height and i < 50:  # Cap at 50 pages
            page.evaluate(f"window.scrollTo(0, {scroll_pos})")
            time.sleep(1)
            path = tmp_dir / f"page_{i}.png"
            page.screenshot(path=str(path))
            screenshots.append(path)
            scroll_pos += viewport_height
            i += 1

    if not screenshots:
        log("No screenshots captured!")
        return None

    log(f"Building PDF from {len(screenshots)} screenshots...")
    pdf = FPDF()
    for img_path in screenshots:
        img = Image.open(img_path)
        width_mm = img.width * 0.264583
        height_mm = img.height * 0.264583
        if width_mm > 200:
            scale = 200 / width_mm
            width_mm *= scale
            height_mm *= scale
        pdf.add_page(orientation="P" if height_mm > width_mm else "L")
        pdf.image(str(img_path), x=5, y=5, w=width_mm)
        img.close()

    pdf_bytes = pdf.output()

    # Clean up temp files after PDF is built
    for img_path in screenshots:
        try:
            os.remove(img_path)
        except OSError:
            pass
    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    return bytes(pdf_bytes)

