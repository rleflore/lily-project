"""
Perusall to PDF — Web App

Usage:
    python app.py --login    # First time: opens browser to log in
    python app.py            # Starts the web server on port 5000
"""

import sys
import os
from flask import Flask, render_template_string, request, send_file, redirect, url_for
from io import BytesIO
from perusall_to_pdf import login_interactive, is_logged_in, fetch_pdf, SESSION_DIR

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Perusall to PDF</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f5f5f7;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .container {
            background: white;
            border-radius: 16px;
            padding: 40px;
            max-width: 500px;
            width: 100%;
            box-shadow: 0 4px 24px rgba(0,0,0,0.08);
        }
        h1 {
            font-size: 24px;
            margin-bottom: 8px;
            color: #1d1d1f;
        }
        .subtitle {
            color: #86868b;
            margin-bottom: 24px;
            font-size: 14px;
        }
        label {
            display: block;
            font-weight: 500;
            margin-bottom: 6px;
            color: #1d1d1f;
        }
        input[type="url"] {
            width: 100%;
            padding: 12px 16px;
            border: 1px solid #d2d2d7;
            border-radius: 8px;
            font-size: 16px;
            margin-bottom: 20px;
            transition: border-color 0.2s;
        }
        input[type="url"]:focus {
            outline: none;
            border-color: #0071e3;
        }
        button {
            width: 100%;
            padding: 14px;
            background: #0071e3;
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 500;
            cursor: pointer;
            transition: background 0.2s;
        }
        button:hover { background: #0077ED; }
        button:disabled {
            background: #86868b;
            cursor: not-allowed;
        }
        .error {
            background: #fff2f2;
            border: 1px solid #ff3b30;
            color: #ff3b30;
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-size: 14px;
        }
        .success {
            background: #f0fff4;
            border: 1px solid #34c759;
            color: #1d7a34;
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-size: 14px;
        }
        .loading {
            display: none;
            text-align: center;
            padding: 20px;
            color: #86868b;
        }
        .loading.active { display: block; }
        .spinner {
            border: 3px solid #f3f3f3;
            border-top: 3px solid #0071e3;
            border-radius: 50%;
            width: 24px;
            height: 24px;
            animation: spin 1s linear infinite;
            margin: 0 auto 12px;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>📄 Perusall to PDF</h1>
        <p class="subtitle">Paste a Perusall article URL to download it as a PDF.</p>

        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}

        {% if success %}
        <div class="success">{{ success }}</div>
        {% endif %}

        <form method="POST" action="/convert" id="convertForm">
            <label for="url">Perusall Article URL</label>
            <input type="url" id="url" name="url"
                   placeholder="https://app.perusall.com/courses/..."
                   required value="{{ last_url or '' }}">
            <button type="submit" id="submitBtn">Convert to PDF</button>
        </form>

        <div class="loading" id="loading">
            <div class="spinner"></div>
            <p>Fetching article... this may take 30-60 seconds.</p>
        </div>
    </div>

    <script>
        document.getElementById('convertForm').addEventListener('submit', function() {
            document.getElementById('submitBtn').disabled = true;
            document.getElementById('submitBtn').textContent = 'Converting...';
            document.getElementById('loading').classList.add('active');
        });
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    if not is_logged_in():
        return render_template_string(
            HTML_TEMPLATE,
            error="Not logged in! Run 'python app.py --login' on the server first.",
        )
    return render_template_string(HTML_TEMPLATE, error=None, success=None, last_url=None)


@app.route("/convert", methods=["POST"])
def convert():
    url = request.form.get("url", "").strip()

    if not url:
        return render_template_string(HTML_TEMPLATE, error="Please enter a URL.", last_url=url)

    if "perusall" not in url:
        return render_template_string(
            HTML_TEMPLATE, error="That doesn't look like a Perusall URL.", last_url=url
        )

    print(f"[*] Converting: {url[:80]}", flush=True)
    print(f"[*] Session dir: {SESSION_DIR}", flush=True)
    print(f"[*] Session exists: {is_logged_in()}", flush=True)

    try:
        pdf_data, filename = fetch_pdf(url, debug=True)
        print(f"[+] Success! {len(pdf_data)} bytes → {filename}", flush=True)
        return send_file(
            BytesIO(pdf_data),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        error_text = str(e)
        # Show just the first line in the UI, full detail in terminal
        ui_error = error_text.split("\n")[0]
        print(f"[!] Error: {error_text}", flush=True)
        return render_template_string(HTML_TEMPLATE, error=ui_error, last_url=url)


if __name__ == "__main__":
    if "--login" in sys.argv:
        login_interactive()
    else:
        if not is_logged_in():
            print("=" * 50)
            print("  No login session found!")
            print(f"  Session dir: {SESSION_DIR}")
            print("  Run: python app.py --login")
            print("=" * 50)
            sys.exit(1)

        print("=" * 50)
        print("  Perusall to PDF Web App")
        print(f"  Session dir: {SESSION_DIR}")
        print("  Open: http://localhost:5000")
        print("  Share via ngrok: ngrok http 5000")
        print("=" * 50)
        app.run(host="0.0.0.0", port=5000, debug=False)
