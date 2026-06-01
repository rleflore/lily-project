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
            headless=False,  # Use visible browser to avoid session issues
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = browser.pages[0] if browser.pages else browser.new_page()
        pdf_data = None
        debug_log = []

        def log(msg):
            debug_log.append(msg)
            if debug:
                print(f"[DEBUG] {msg}")

        try:
            # Navigate and collect ALL network responses
            all_responses = []

            def capture_response(response):
                content_type = response.headers.get("content-type", "")
                url = response.url
                all_responses.append((url, content_type, response))
                # Log interesting responses
                if any(kw in url.lower() for kw in ["pdf", "document", "file", "asset", "page", "image", "render"]):
                    log(f"  → {response.status} | {content_type[:40]} | {url[:120]}")

            page.on("response", capture_response)

            log(f"Navigating to: {article_url}")
            page.goto(article_url, wait_until="domcontentloaded", timeout=45000)
            time.sleep(3)

            # Check if we're actually logged in
            current_url = page.url
            log(f"Current URL after nav: {current_url}")

            if "login" in current_url or "accounts.google" in current_url or "signin" in current_url:
                browser.close()
                raise RuntimeError(
                    "Session expired — the browser was redirected to login. "
                    "Run 'python app.py --login' again."
                )

            # Wait for the document viewer to load
            log("Waiting for document to render...")
            time.sleep(10)

            # Log page title and some DOM info
            title = page.title()
            log(f"Page title: {title}")

            # Strategy 1: Look for PDF in network responses
            log(f"Total network responses captured: {len(all_responses)}")
            pdf_data = _check_responses_for_pdf(all_responses, log)

            # Strategy 2: Look for document images/pages from Perusall's API
            if not pdf_data:
                pdf_data = _extract_perusall_pages(page, all_responses, log)

            # Strategy 3: Extract PDF URL from page JavaScript
            if not pdf_data:
                pdf_data = _extract_pdf_url(page, log)

            # Strategy 4: Screenshot the rendered pages
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
                "/pdf/" in url or
                "document-file" in url or
                "original-file" in url):
            try:
                body = resp.body()
                if body[:4] == b"%PDF":
                    log(f"Found PDF in response: {url[:100]}")
                    return body
            except Exception:
                continue
    log("No PDF found in network responses.")
    return None


def _extract_perusall_pages(page, all_responses, log):
    """
    Perusall often loads documents as individual page images from S3/CloudFront.
    Collect those images and combine into a PDF.
    """
    from PIL import Image
    from fpdf import FPDF

    log("Looking for page images in network responses...")

    # Look for image responses that could be document pages
    image_urls = []
    for url, content_type, resp in all_responses:
        if ("image/" in content_type and
                resp.status == 200 and
                any(kw in url for kw in ["page", "document", "asset", "cloudfront", "s3.amazonaws"])):
            image_urls.append((url, resp))

    # Also look for Perusall's specific API patterns
    for url, content_type, resp in all_responses:
        if (resp.status == 200 and
                ("image/png" in content_type or "image/jpeg" in content_type) and
                len(url) > 50):  # Long URLs are typically CDN/asset URLs
            if (url, resp) not in image_urls:
                image_urls.append((url, resp))

    log(f"Found {len(image_urls)} potential page images")

    if not image_urls:
        return None

    # Download and sort images by URL (often numbered)
    images = []
    for url, resp in image_urls:
        try:
            body = resp.body()
            if len(body) > 5000:  # Skip tiny images (icons, etc.)
                img = Image.open(BytesIO(body))
                if img.width > 200 and img.height > 200:  # Skip small images
                    images.append((url, img))
                    log(f"  Page image: {img.width}x{img.height} from {url[:80]}")
        except Exception:
            continue

    if len(images) < 1:
        log("No usable page images found.")
        return None

    log(f"Building PDF from {len(images)} page images...")
    pdf = FPDF()
    for _, img in images:
        # Save to temp buffer
        img_buffer = BytesIO()
        img.save(img_buffer, format="PNG")
        img_buffer.seek(0)

        width_mm = img.width * 0.264583
        height_mm = img.height * 0.264583
        if width_mm > 200:
            scale = 200 / width_mm
            width_mm *= scale
            height_mm *= scale

        pdf.add_page(orientation="P" if height_mm > width_mm else "L")
        # fpdf2 supports reading from BytesIO
        tmp_path = Path(__file__).parent / ".tmp_page_img.png"
        img.save(str(tmp_path))
        pdf.image(str(tmp_path), x=5, y=5, w=width_mm)
        os.remove(tmp_path)

    return bytes(pdf.output())


def _extract_pdf_url(page, log):
    """Try to find a PDF URL in Perusall's JavaScript state."""
    log("Searching page JavaScript for PDF URLs...")
    try:
        doc_info = page.evaluate("""
            () => {
                const results = [];

                // Check __NEXT_DATA__
                if (window.__NEXT_DATA__) {
                    results.push(JSON.stringify(window.__NEXT_DATA__).substring(0, 10000));
                }

                // Check all script tags
                const scripts = document.querySelectorAll('script');
                for (const s of scripts) {
                    const text = s.textContent;
                    if (text.includes('pdf') || text.includes('document') || text.includes('fileUrl')) {
                        results.push(text.substring(0, 5000));
                    }
                }

                // Check for React fiber / state with document data
                const root = document.getElementById('__next') || document.getElementById('root');
                if (root && root._reactRootContainer) {
                    try {
                        const state = root._reactRootContainer._internalRoot.current.memoizedState;
                        results.push(JSON.stringify(state).substring(0, 5000));
                    } catch(e) {}
                }

                return results.join('\\n---\\n');
            }
        """)

        if doc_info:
            log(f"Found {len(doc_info)} chars of JS state")
            # Look for various URL patterns
            patterns = [
                r'(https?://[^"\'\s\\]+\.pdf[^"\'\s\\]*)',
                r'"(https?://[^"\\]+cloudfront[^"\\]+)"',
                r'"(https?://[^"\\]+s3[^"\\]+)"',
                r'"fileUrl"\s*:\s*"(https?://[^"\\]+)"',
                r'"url"\s*:\s*"(https?://[^"\\]+(?:pdf|document|file)[^"\\]*)"',
            ]
            for pattern in patterns:
                matches = re.findall(pattern, doc_info)
                for match in matches:
                    log(f"  Trying URL: {match[:100]}")
                    try:
                        resp = page.request.get(match)
                        if resp.ok:
                            body = resp.body()
                            if body[:4] == b"%PDF":
                                log("Found working PDF URL!")
                                return body
                    except Exception:
                        continue
    except Exception as e:
        log(f"JS extraction error: {e}")

    return None


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
        os.remove(img_path)

    pdf_bytes = pdf.output()
    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    return bytes(pdf_bytes)

