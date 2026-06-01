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

                # Log PDF and JSON API responses
                elif "application/json" in content_type and "cloudfront" in url:
                    log(f"  📦 JSON data: {url[:120]}")

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

            # First pass: scroll slowly to trigger loading
            for i, elem in enumerate(page_elements):
                try:
                    elem.scroll_into_view_if_needed(timeout=5000)
                    time.sleep(1.5)
                except Exception:
                    pass

            # Wait for network to settle
            time.sleep(5)
            log(f"Text content JSONs after first pass: {len(text_content_jsons)}")

            # Second pass: scroll again to catch any pages that didn't load
            if len(text_content_jsons) < len(page_elements):
                log("Doing second scroll pass for missed pages...")
                for i, elem in enumerate(page_elements):
                    try:
                        elem.scroll_into_view_if_needed(timeout=5000)
                        time.sleep(1)
                    except Exception:
                        pass
                time.sleep(5)
                log(f"Text content JSONs after second pass: {len(text_content_jsons)}")

            # Strategy: Directly fetch all text-content URLs if we found the pattern
            # Extract the doc ID and base URL from captured text-content URLs
            if text_content_jsons:
                pdf_data = _fetch_all_text_content(page, text_content_jsons, all_responses, log)

            # Fallback: Build from whatever text-content we captured
            if not pdf_data:
                log(f"Total text content JSONs captured: {len(text_content_jsons)}")
                # Strategy 1: Check for direct PDF in responses
                pdf_data = _check_responses_for_pdf(all_responses, log)

            # Strategy 2: Build text PDF from text-content JSONs
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


def _fetch_all_text_content(page, text_content_jsons, all_responses, log):
    """
    Use captured text-content URLs to determine the doc ID and base URL pattern,
    then find all page IDs from the documents API and fetch text for every page.
    """
    import json
    import re

    # Extract pattern from captured URLs
    # Format: https://d12klv9dmumy6j.cloudfront.net/text-content/{docId}/{pageId}.json?Expires=...
    sample_url = text_content_jsons[0][0]
    match = re.search(r'(https://[^/]+/text-content/([^/]+)/)([^.?]+)\.json(\?[^"]+)', sample_url)
    if not match:
        log("Could not parse text-content URL pattern.")
        return None

    base_url = match.group(1)
    doc_id = match.group(2)
    query_params = match.group(4)
    log(f"Document ID: {doc_id}")
    log(f"Base URL: {base_url}")

    # Find the documents API response to get all page IDs
    page_ids = []

    # Look in API responses for page list
    for url, content_type, resp in all_responses:
        if "application/json" in content_type and ("documents" in url or "backend" in url):
            try:
                body = resp.body()
                data = json.loads(body)
                # Look for page arrays in the response
                pages_found = _extract_page_ids(data, doc_id)
                if pages_found:
                    page_ids = pages_found
                    log(f"Found {len(page_ids)} page IDs from API")
                    break
            except Exception:
                continue

    # If we couldn't find page IDs from API, extract from captured URLs
    if not page_ids:
        for url, resp in text_content_jsons:
            m = re.search(r'/text-content/[^/]+/([^.?]+)\.json', url)
            if m:
                page_ids.append(m.group(1))
        log(f"Extracted {len(page_ids)} page IDs from captured URLs only")

    # Now fetch ALL text-content JSONs (including ones we may have missed)
    all_text_jsons = []

    # First, add what we already have
    captured_page_ids = set()
    for url, resp in text_content_jsons:
        m = re.search(r'/text-content/[^/]+/([^.?]+)\.json', url)
        if m:
            captured_page_ids.add(m.group(1))
            all_text_jsons.append((url, resp))

    # Try to fetch any missing pages
    for pid in page_ids:
        if pid not in captured_page_ids:
            fetch_url = f"{base_url}{pid}.json{query_params}"
            log(f"  Fetching missing page: {pid}")
            try:
                resp = page.request.get(fetch_url)
                if resp.ok:
                    all_text_jsons.append((fetch_url, resp))
                    log(f"  ✓ Got text for page {pid}")
            except Exception as e:
                log(f"  ✗ Failed: {e}")

    log(f"Total text-content JSONs (with fetched): {len(all_text_jsons)}")

    if all_text_jsons:
        return _build_text_pdf(all_text_jsons, log)
    return None


def _extract_page_ids(data, doc_id):
    """Recursively search API response for page IDs related to this document."""
    page_ids = []

    if isinstance(data, dict):
        # Look for common patterns in Perusall's API
        for key in ["pages", "pageIds", "pageList"]:
            if key in data and isinstance(data[key], list):
                for item in data[key]:
                    if isinstance(item, str):
                        page_ids.append(item)
                    elif isinstance(item, dict) and "_id" in item:
                        page_ids.append(item["_id"])
                    elif isinstance(item, dict) and "id" in item:
                        page_ids.append(item["id"])

        # Check if this is a document with an _id matching our doc
        if data.get("_id") == doc_id or data.get("id") == doc_id:
            if "pages" in data:
                return _extract_page_ids(data, doc_id)

        # Recurse into nested objects
        if not page_ids:
            for value in data.values():
                if isinstance(value, (dict, list)):
                    found = _extract_page_ids(value, doc_id)
                    if found:
                        return found

    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                found = _extract_page_ids(item, doc_id)
                if found:
                    return found

    return page_ids


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

    # Standard PDF page is 612x792 points (US Letter)
    PAGE_WIDTH_PT = 612
    PAGE_HEIGHT_PT = 792

    # Create PDF with fpdf2 and Unicode support
    pdf = FPDF(unit="pt", format="letter")
    pdf.set_auto_page_break(auto=False)

    # Add a Unicode font (DejaVu Sans ships with fpdf2)
    font_dir = Path(__file__).parent
    try:
        pdf.add_font("DejaVu", "", fname="DejaVuSans.ttf")
        pdf.add_font("DejaVu", "B", fname="DejaVuSans-Bold.ttf")
        pdf.add_font("DejaVu", "I", fname="DejaVuSans-Oblique.ttf")
        pdf.add_font("DejaVu", "BI", fname="DejaVuSans-BoldOblique.ttf")
        use_dejavu = True
    except Exception:
        try:
            # Try without italic variants
            pdf.add_font("DejaVu", "", fname="DejaVuSans.ttf")
            pdf.add_font("DejaVu", "B", fname="DejaVuSans-Bold.ttf")
            use_dejavu = True
        except Exception:
            use_dejavu = False
            log("DejaVu font not found, using Helvetica with character replacement")

    # Log unique font names from first page to help debug
    if pages_data:
        font_names = set()
        for item in pages_data[0]:
            if item.get("fontName"):
                font_names.add(item["fontName"])
        log(f"Font names found: {sorted(font_names)}")

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

        # Scale to fit within page margins (20pt each side)
        content_height = max_y - min_y if max_y > min_y else PAGE_HEIGHT_PT
        content_width = max_x if max_x > 0 else PAGE_WIDTH_PT
        usable_width = PAGE_WIDTH_PT - 40
        usable_height = PAGE_HEIGHT_PT - 60
        scale_x = usable_width / content_width if content_width > usable_width else 1.0
        scale_y = usable_height / content_height if content_height > usable_height else 1.0
        scale = min(scale_x, scale_y)

        for item in items:
            text = item.get("str", "")
            if not text or text.isspace():
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

            # Determine if bold/italic based on font name
            font_name = item.get("fontName", "")
            style = ""
            font_lower = font_name.lower()
            if "bold" in font_lower and "italic" in font_lower:
                style = "BI"
            elif "bold" in font_lower:
                style = "B"
            elif "italic" in font_lower or "oblique" in font_lower:
                style = "I"
            # For Perusall's generic font names like g_d2896_f1, g_d2896_f2
            elif font_name:
                parts = font_name.split("_f")
                if len(parts) == 2 and parts[1].isdigit():
                    variant = int(parts[1])
                    # f1 = bold/headings, f2 = regular body
                    # f3 = italic, f4 = bold-italic (if they exist)
                    if variant == 1:
                        style = "B"
                    elif variant == 3:
                        style = "I"
                    elif variant == 4:
                        style = "BI"

            # Sanitize text for non-Unicode fonts
            if not use_dejavu:
                text = _sanitize_latin1(text)

            try:
                font_style = style
                if use_dejavu:
                    try:
                        pdf.set_font("DejaVu", style=font_style, size=max(6, min(font_size_scaled, 24)))
                    except Exception:
                        pdf.set_font("DejaVu", style="", size=max(6, min(font_size_scaled, 24)))
                else:
                    pdf.set_font("Helvetica", style=font_style, size=max(6, min(font_size_scaled, 24)))
                pdf.text(x=x_scaled, y=y_scaled + font_size_scaled, text=text)
            except Exception as e:
                # Last resort: try with sanitized text and no style
                try:
                    sanitized = _sanitize_latin1(text)
                    pdf.set_font("Helvetica", style="", size=max(6, min(font_size_scaled, 24)))
                    pdf.text(x=x_scaled, y=y_scaled + font_size_scaled, text=sanitized)
                except Exception:
                    log(f"  ⚠ Could not render: {text[:50]}")

    pdf_bytes = pdf.output()
    log(f"Text PDF built: {len(pdf_bytes)} bytes, {len(pages_data)} pages")
    return bytes(pdf_bytes)


def _sanitize_latin1(text):
    """Replace common Unicode characters with Latin-1 equivalents."""
    replacements = {
        '\u2018': "'",   # left single quote
        '\u2019': "'",   # right single quote
        '\u201c': '"',   # left double quote
        '\u201d': '"',   # right double quote
        '\u2013': '-',   # en dash
        '\u2014': '--',  # em dash
        '\u2026': '...', # ellipsis
        '\u00a0': ' ',   # non-breaking space
        '\ufb01': 'fi',  # fi ligature
        '\ufb02': 'fl',  # fl ligature
        '\u2022': '*',   # bullet
        '\u2032': "'",   # prime
        '\u2033': '"',   # double prime
    }
    for orig, repl in replacements.items():
        text = text.replace(orig, repl)
    # Replace any remaining non-Latin-1 chars
    return text.encode('latin-1', errors='replace').decode('latin-1')


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

