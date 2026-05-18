# Socio Scanner

Standalone CLI tool that extracts LinkedIn post analytics by clicking LinkedIn's own **Export** button and parsing the downloaded XLSX file — no DOM scraping, no API abuse. Everything runs locally.

## What it does

1. **Discovers** your LinkedIn posts by scrolling through your activity feed (authenticated)
2. **Captures** analytics for each post via LinkedIn's Export → XLSX, then parses both sheets (Performance + Demographics)
3. **Outputs** structured JSON with impressions, reactions, comments, reposts, demographics, and more
4. **Schedules** follow-up scans at 1h, 6h, 24h, 72h, and 7d intervals
5. **All local** — no backend, no cloud, no data leaves your machine

## Requirements

- **Windows** (10 or later)
- **Python 3.11+** ([python.org/downloads](https://www.python.org/downloads/))
- **A LinkedIn account**

## Step-by-step setup

### 1. Download and extract

Download the latest `socio-scanner-v*.zip` from [Releases](../../releases) and extract it to a folder on your PC.

### 2. Run setup

Double-click **`setup.bat`**. This will:

- Create a Python virtual environment (`.venv`)
- Install all dependencies (`playwright`, `openpyxl`)
- Install Chromium browser (needed by Playwright)
- Initialize the local scanner database

Wait for "**Setup complete!**" before proceeding.

### 3. Log into LinkedIn

Double-click **`login.bat`**. A Chrome browser window will open. Sign into your LinkedIn account, then close the browser. The scanner will auto-detect your profile URL and save it.

> Your login session (cookies) is saved locally in `%LOCALAPPDATA%\Socio\LocalScanner\browser-profile\` and reused across runs — you only need to log in once.

### 4. Use the scanner

Double-click **`scan.bat`** to open the interactive menu:

```
============================================
  Socio Scanner
============================================

  1. Discover + Scan (fresh import)
  2. Scan pending (process due queue)
  3. Status
  4. Pause / Resume

Choose (1-4):
```

**First time:** Choose **1** to discover all your posts and scan their analytics immediately.

**Ongoing:** Choose **2** to process any scans that are due (the scanner schedules follow-ups at 1h, 6h, 24h, 72h, and 7d).

**Check progress:** Choose **3** to see how many scans are pending and when the next one is due.

## CLI reference

If you prefer the command line over the interactive menu:

| Command | Description |
|---------|------------|
| `socio-scanner init` | Create local SQLite database |
| `socio-scanner login` | Open browser to authenticate with LinkedIn |
| `socio-scanner login --check` | Verify existing LinkedIn session is valid |
| `socio-scanner discover --limit 25` | Find posts on your profile, store in local DB |
| `socio-scanner import --scan-now` | Discover + scan analytics immediately |
| `socio-scanner scan` | Process due analytics captures |
| `socio-scanner scan --force` | Process ALL pending scans regardless of schedule |
| `socio-scanner status` | Show queue state and last scan time |
| `socio-scanner pause` / `resume` | Toggle scanning on/off |
| `socio-scanner reset --force --reinit` | Wipe everything and start fresh |

## Output format (v2 capture payload)

```json
{
  "schema_version": "socio_linkedin_snapshot_v2",
  "capture": {
    "capture_mode": "export",
    "capture_timestamp": "2026-05-14T22:00:00Z",
    "snapshot_window": "1h",
    "confidence": 0.98
  },
  "post": {
    "canonical_urn": "urn:li:activity:...",
    "top_job_title": "Frontend Developer",
    "top_location": "Karachi Division",
    "top_industry": "IT Services and IT Consulting"
  },
  "metrics": {
    "impressions": 105,
    "reactions": 3,
    "comments": 0,
    "reposts": 0,
    "members_reached": 29,
    "saves": 0,
    "sends": 0,
    "engagement_rate": 0.0286
  },
  "demographics": [
    {"category": "Company size", "value": "1-10 employees", "percentage": 41.4},
    {"category": "Job title", "value": "Software Engineer", "percentage": 28.6}
  ]
}
```

## Where data is stored

Everything lives in `%LOCALAPPDATA%\Socio\LocalScanner\`:

| Path | Purpose |
|------|---------|
| `scanner.db` | SQLite database (posts, scan queue, snapshots) |
| `config.json` | Your LinkedIn profile URL and scanner settings |
| `browser-profile/` | Chromium profile with your LinkedIn session |

Run `socio-scanner reset --force --reinit` to delete all of it.

## Integration

Pipe scanner output into any backend or script:

```bash
socio-scanner scan --force | your-ingestion-script
```

Or call it from Python:

```python
import subprocess, json
result = json.loads(subprocess.check_output(["socio-scanner", "scan", "--force"]))
```

## Risk assessment

**This scanner carries minimal risk for the following reasons:**

- **No API abuse.** It clicks LinkedIn's own "Export" button — the same one you'd click manually. It does not hit any LinkedIn API endpoint, does not scrape the DOM with regex, and does not send unauthorized requests. Every action maps to a real user interaction.
- **No credentials stored in code.** Your LinkedIn session lives in a local Chromium profile (`%LOCALAPPDATA%\Socio\LocalScanner\browser-profile\`), the same way Chrome remembers your logins. No passwords or tokens appear in the source code, config files, or database.
- **All data stays local.** Analytics data is written to a SQLite database on your machine. Nothing is sent to any server, cloud, or third party. There is no telemetry, no analytics, no phoning home.
- **Rate limiting built in.** The `AdaptiveLimiter` enforces random 3–5 second delays between actions, with automatic backoff if LinkedIn signals rate limiting. Scans of the same post are scheduled at least 1 hour apart.
- **Anti-detection measures.** The browser context uses randomized viewports, standard Chrome UA strings, and stealth scripts to avoid triggering automation detection. However, like any browser automation tool, LinkedIn could theoretically update their detection — this is an inherent risk of any Playwright/Puppeteer-based tool, mitigated but not eliminated.
- **Not officially endorsed by LinkedIn.** This is an independent tool. Use it responsibly and at your own discretion.
