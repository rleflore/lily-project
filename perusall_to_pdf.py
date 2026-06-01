"""
Perusall Article to PDF Converter — Core Logic

This module handles browser automation to extract PDFs from Perusall.
Used by both the CLI login command and the Flask web app.
"""

import os
import time
import re
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


def fetch_pdf(article_url):
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
            headless=True,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = browser.pages[0] if browser.pages else browser.new_page()
        pdf_data = None

        try:
            # Strategy 1: Intercept PDF from network responses
            pdf_data = _intercept_pdf(page, article_url)

            # Strategy 2: Extract PDF URL from page JavaScript
            if not pdf_data:
                pdf_data = _extract_pdf_url(page)

            # Strategy 3: Screenshot fallback
            if not pdf_data:
                pdf_data = _screenshot_to_pdf(page)

        finally:
            browser.close()

        if not pdf_data:
            raise RuntimeError("Could not extract PDF. Session may have expired — try logging in again.")

        # Generate filename from URL
        slug = article_url.rstrip("/").split("/")[-1][:50]
        filename = re.sub(r"[^\w\-]", "_", slug) + ".pdf"

        return pdf_data, filename


def _intercept_pdf(page, url):
    """Navigate and intercept PDF responses from Perusall's network requests."""
    pdf_responses = []

    def handle_response(response):
        content_type = response.headers.get("content-type", "")
        req_url = response.url
        if ("application/pdf" in content_type or
                req_url.endswith(".pdf") or
                "/pdf/" in req_url or
                "document-file" in req_url or
                "original-file" in req_url):
            pdf_responses.append(response)

    page.on("response", handle_response)

    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
    except PlaywrightTimeout:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

    time.sleep(5)

    for resp in pdf_responses:
        try:
            body = resp.body()
            if body[:4] == b"%PDF":
                return body
        except Exception:
            continue

    return None


def _extract_pdf_url(page):
    """Try to find a PDF URL in Perusall's JavaScript state."""
    try:
        doc_info = page.evaluate("""
            () => {
                if (window.__NEXT_DATA__) return JSON.stringify(window.__NEXT_DATA__);
                const scripts = document.querySelectorAll('script');
                for (const s of scripts) {
                    if (s.textContent.includes('pdfUrl') || s.textContent.includes('documentUrl')) {
                        return s.textContent.substring(0, 5000);
                    }
                }
                return null;
            }
        """)

        if doc_info:
            match = re.search(r'(https?://[^"\'\s]+\.pdf[^"\'\s]*)', doc_info)
            if match:
                resp = page.request.get(match.group(1))
                if resp.ok and resp.body()[:4] == b"%PDF":
                    return resp.body()
    except Exception:
        pass

    return None


def _screenshot_to_pdf(page):
    """Fallback: screenshot each page and combine into a PDF."""
    from PIL import Image
    from fpdf import FPDF

    time.sleep(3)
    screenshots = []
    tmp_dir = Path(__file__).parent / ".tmp_screenshots"
    tmp_dir.mkdir(exist_ok=True)

    page_elements = page.query_selector_all(
        '[class*="page"], [data-page-number], canvas'
    )

    if page_elements:
        for i, elem in enumerate(page_elements):
            try:
                elem.scroll_into_view_if_needed(timeout=5000)
                time.sleep(0.5)
                path = tmp_dir / f"page_{i}.png"
                elem.screenshot(path=str(path))
                screenshots.append(path)
            except Exception:
                continue
    else:
        viewport_height = page.viewport_size["height"]
        total_height = page.evaluate("document.body.scrollHeight")
        scroll_pos = 0
        i = 0
        while scroll_pos < total_height:
            page.evaluate(f"window.scrollTo(0, {scroll_pos})")
            time.sleep(0.5)
            path = tmp_dir / f"page_{i}.png"
            page.screenshot(path=str(path))
            screenshots.append(path)
            scroll_pos += viewport_height
            i += 1

    if not screenshots:
        return None

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

