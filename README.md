# Perusall to PDF Converter

Convert Perusall articles to PDF — just paste the URL.

---

## First-Time Setup (do this once)

### 1. Install Python
If you don't have Python installed, download it from https://www.python.org/downloads/  
**Check "Add Python to PATH"** during installation.

### 2. Open a terminal
- On Windows: search for "Command Prompt" or "PowerShell"
- On Mac: open "Terminal"

### 3. Clone this repo and install dependencies
```bash
git clone https://github.com/rleflore/lily-project.git
cd lily-project
pip install -r requirements.txt
playwright install chromium
```

### 4. Log in (one time only)
```bash
python app.py --login
```
A browser will open — sign in with your university Google account.  
Once you see your Perusall dashboard, the session is saved and you can close the browser.

---

## Usage (every time you want a PDF)

### Start the app:
```bash
cd lily-project
python app.py
```

### Then:
1. Open **http://localhost:5000** in your browser
2. Paste the Perusall article URL
3. Click **Convert to PDF**
4. Your PDF downloads automatically!

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Not logged in" error | Run `python app.py --login` again |
| PDF doesn't download | Try again — Perusall can be slow |
| Session expired | Delete the `.auth_session` folder, then run `python app.py --login` |

---

## How It Works

The app uses a headless browser (Playwright) to:
1. Load the Perusall article using your saved login session
2. Intercept the PDF that Perusall fetches internally
3. If that fails, it screenshots each page and combines them into a PDF
