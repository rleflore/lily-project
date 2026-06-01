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
            # Collect all network responses
            all_responses = []
            cloudfront_pages = []

            def capture_response(response):
                content_type = response.headers.get("content-type", "")
                url = response.url
                all_responses.append((url, content_type, response))

                # Track CloudFront page images specifically
                if "cloudfront.net/pages/" in url:
                    cloudfront_pages.append((url, response))
                    log(f"  📄 Page image: {url[:120]}")

                # Log other interesting responses
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

            # Step 3: Wait for the document viewer to fully load
            log("Waiting for document viewer to load...")
            time.sleep(15)

            log(f"CloudFront page images captured so far: {len(cloudfront_pages)}")
            log(f"Total network responses: {len(all_responses)}")

            # Strategy 1: Check for direct PDF in responses
            pdf_data = _check_responses_for_pdf(all_responses, log)

            # Strategy 2: Use CloudFront page images to build PDF
            if not pdf_data:
                pdf_data = _build_pdf_from_cloudfront(page, cloudfront_pages, log)

            # Strategy 3: Try fetching full-size pages from CloudFront
            if not pdf_data:
                pdf_data = _fetch_fullsize_pages(page, cloudfront_pages, log)

            # Strategy 4: Screenshot the rendered document
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


def _build_pdf_from_cloudfront(page, cloudfront_pages, log):
    """
    Build a PDF from CloudFront page images that were already loaded.
    Perusall loads thumbnails first; we'll use those if full-size aren't available.
    """
    from PIL import Image
    from fpdf import FPDF

    if not cloudfront_pages:
        log("No CloudFront page images captured.")
        return None

    log(f"Building PDF from {len(cloudfront_pages)} CloudFront page images...")

    images = []
    for url, resp in cloudfront_pages:
        try:
            body = resp.body()
            if len(body) > 1000:
                img = Image.open(BytesIO(body))
                images.append((url, img))
        except Exception as e:
            log(f"  Failed to read image: {e}")
            continue

    if not images:
        log("Could not read any page images.")
        return None

    # Sort by URL to maintain page order (they often have sequential IDs)
    log(f"Got {len(images)} readable page images")

    pdf = FPDF()
    for url, img in images:
        tmp_path = Path(__file__).parent / ".tmp_page_img.png"
        img.save(str(tmp_path))

        width_mm = img.width * 0.264583
        height_mm = img.height * 0.264583
        if width_mm > 200:
            scale = 200 / width_mm
            width_mm *= scale
            height_mm *= scale

        pdf.add_page(orientation="P" if height_mm > width_mm else "L")
        pdf.image(str(tmp_path), x=5, y=5, w=width_mm)
        os.remove(tmp_path)

    return bytes(pdf.output())


def _fetch_fullsize_pages(page, cloudfront_pages, log):
    """
    Perusall loads thumbnail versions of pages. Try to fetch full-size versions
    by removing '-thumbnail' from the URL.
    """
    from PIL import Image
    from fpdf import FPDF

    if not cloudfront_pages:
        return None

    log("Attempting to fetch full-size page images...")

    # Extract page IDs from thumbnail URLs and try full-size
    fullsize_images = []
    for url, _ in cloudfront_pages:
        # URL pattern: .../pages/{id}-thumbnail.png?Expires=...
        fullsize_url = url.replace("-thumbnail", "")
        log(f"  Trying full-size: {fullsize_url[:100]}")
        try:
            resp = page.request.get(fullsize_url)
            if resp.ok:
                body = resp.body()
                if len(body) > 5000:
                    img = Image.open(BytesIO(body))
                    fullsize_images.append((fullsize_url, img))
                    log(f"  ✓ Got full-size page: {img.width}x{img.height}")
        except Exception as e:
            log(f"  ✗ Failed: {e}")
            continue

    if not fullsize_images:
        log("Could not fetch any full-size pages.")
        return None

    log(f"Building PDF from {len(fullsize_images)} full-size pages...")
    pdf = FPDF()
    for url, img in fullsize_images:
        tmp_path = Path(__file__).parent / ".tmp_page_img.png"
        img.save(str(tmp_path))

        width_mm = img.width * 0.264583
        height_mm = img.height * 0.264583
        if width_mm > 200:
            scale = 200 / width_mm
            width_mm *= scale
            height_mm *= scale

        pdf.add_page(orientation="P" if height_mm > width_mm else "L")
        pdf.image(str(tmp_path), x=5, y=5, w=width_mm)
        os.remove(tmp_path)

    return bytes(pdf.output())


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

