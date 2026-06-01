"""
Direct test script — run this from the command line to see full debug output.

Usage:
    python test_fetch.py
"""

import sys
sys.stdout.reconfigure(line_buffering=True)  # Force unbuffered output

from perusall_to_pdf import fetch_pdf, is_logged_in

URL = "https://app.perusall.com/courses/amst203-wb12-popular-culture-in-america/1freccero-218278326?assignmentId=ggmcBWFcPTqP3qQbw&part=1&panel=assignmentInformation&filter=all"

print("=" * 60)
print("  Perusall PDF Fetch — Debug Test")
print("=" * 60)
print()

print(f"[*] Session exists: {is_logged_in()}")
print(f"[*] Target URL: {URL[:80]}...")
print()

try:
    pdf_data, filename = fetch_pdf(URL, debug=True)
    print(f"\n[+] SUCCESS! Got {len(pdf_data)} bytes")
    print(f"[+] Saving as: {filename}")
    with open(filename, "wb") as f:
        f.write(pdf_data)
    print(f"[+] Saved!")
except Exception as e:
    print(f"\n[!] FAILED: {e}")
