# Setup Guide

## 1. Enable GitHub Pages

Go to **Settings → Pages** in this repo, set source to `main` branch, root `/`.
Your site will be live at `https://maryleenamaizu.github.io/ML-AI-Scholarships/`.

## 2. Add Secrets for the Auto-Update Workflow

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Where to get it |
|--------|----------------|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com |
| `TAVILY_API_KEY` | https://tavily.com (free tier, 1000 searches/month) |
| `GMAIL_TOKEN_JSON` | See Gmail setup below |

If you skip `TAVILY_API_KEY` the scraper falls back to DuckDuckGo (no key needed).
If you skip `GMAIL_TOKEN_JSON` it still runs web-only searches.

## 3. Gmail API Setup (one-time)

1. Go to https://console.cloud.google.com → create a project
2. Enable the **Gmail API**
3. Create **OAuth 2.0 credentials** (Desktop app type) → download `credentials.json`
4. Run this locally once to generate a token:

```bash
pip install google-auth-oauthlib google-api-python-client
python - <<'EOF'
from google_auth_oauthlib.flow import InstalledAppFlow
import json

flow = InstalledAppFlow.from_client_secrets_file(
    'credentials.json',
    scopes=['https://www.googleapis.com/auth/gmail.readonly']
)
creds = flow.run_local_server(port=0)
print(creds.to_json())
EOF
```

5. Copy the printed JSON and paste it as the `GMAIL_TOKEN_JSON` secret.

## 4. How the Update Flow Works

```
Every Monday 08:00 UTC (or manually triggered)
  └─> scripts/scrape_and_parse.py runs
       ├─ Searches web for new ML/AI opportunities
       ├─ Reads Gmail inbox (women-in-ml, ml-news lists)
       ├─ Parses results with Claude → structured JSON
       ├─ Deduplicates against existing data/
       └─ If new entries found → opens a Pull Request

You review the PR → merge → GitHub Pages auto-updates
```

## 5. Manual Trigger

Go to **Actions → Update Opportunities → Run workflow** to trigger it immediately.
