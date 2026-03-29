"""
ML/AI Opportunities Scraper
============================
Searches the web and Gmail for new ML/AI PhD and summer school opportunities,
parses them with Claude, deduplicates against existing JSON, and writes updates.

Required secrets (GitHub Actions / .env):
  ANTHROPIC_API_KEY   — Claude API key (https://console.anthropic.com)
  TAVILY_API_KEY      — Tavily web search (https://tavily.com) [optional]
  GMAIL_TOKEN_JSON    — Gmail OAuth token JSON string (see README for setup)

Run locally:  python scripts/scrape_and_parse.py
"""

import json
import os
import re
import base64
import datetime
from pathlib import Path

# ── optional imports (graceful degradation) ─────────────────────────────────
try:
    import anthropic
    _has_anthropic = True
except ImportError:
    _has_anthropic = False
    print("WARNING: anthropic package not installed. Skipping Claude parsing.")

try:
    import requests
    _has_requests = True
except ImportError:
    _has_requests = False

try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    _has_gmail = True
except ImportError:
    _has_gmail = False
    print("WARNING: google-api-python-client not installed. Skipping Gmail.")

# ── paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
SCHOLARSHIPS_JSON = ROOT / "data" / "scholarships.json"
SUMMER_JSON = ROOT / "data" / "summer_schools.json"

# ── config ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TAVILY_API_KEY    = os.environ.get("TAVILY_API_KEY", "")
GMAIL_TOKEN_JSON  = os.environ.get("GMAIL_TOKEN_JSON", "")

WEB_QUERIES = [
    "funded PhD machine learning artificial intelligence 2026 deadline apply",
    "fully funded PhD AI NLP computer vision open positions 2026",
    "ML AI summer school 2026 applications open deadline",
    "women in machine learning PhD scholarship 2026",
    "MSCA doctoral network AI machine learning 2026",
]

GMAIL_SEARCH_QUERY = (
    "from:(women-in-machine-learning@googlegroups.com OR ml-news@googlegroups.com) "
    "newer_than:14d"
)

SYSTEM_PROMPT = """You are an assistant that extracts structured information about
academic opportunities from raw text.

For each PhD/MSc opportunity extract:
  institution, location (country), research_area, type (PhD | MSc | MSc & PhD),
  deadline (ISO YYYY-MM-DD or "Open till filled"), deadline_display (human-readable),
  link (URL)

For each summer school extract:
  name, location (City, Country), deadline (ISO YYYY-MM-DD or "TBD"),
  deadline_display, dates (e.g. "14–18 Jul 2026"), funded (true/false/null), link

Return ONLY valid JSON with two keys:
{
  "scholarships": [ ... ],
  "summer_schools": [ ... ]
}
If no opportunities are found, return {"scholarships":[], "summer_schools":[]}.
Do not include markdown fences."""


# ── helpers ──────────────────────────────────────────────────────────────────

def load_existing():
    scholarships = json.loads(SCHOLARSHIPS_JSON.read_text()) if SCHOLARSHIPS_JSON.exists() else []
    summer = json.loads(SUMMER_JSON.read_text()) if SUMMER_JSON.exists() else []
    return scholarships, summer


def existing_keys(scholarships, summer):
    """Build a set of (institution.lower, deadline) tuples for dedup."""
    s_keys = {(r.get("institution","").lower(), r.get("deadline","")) for r in scholarships}
    ss_keys = {(r.get("name","").lower(), r.get("deadline","")) for r in summer}
    return s_keys, ss_keys


def next_id(items):
    if not items:
        return 1
    return max(r.get("id", 0) for r in items) + 1


def web_search_tavily(query: str) -> str:
    """Fetch search results via Tavily API."""
    if not TAVILY_API_KEY or not _has_requests:
        return ""
    resp = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": TAVILY_API_KEY, "query": query, "max_results": 8,
              "search_depth": "basic", "include_answer": True},
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"Tavily error {resp.status_code}: {resp.text[:200]}")
        return ""
    data = resp.json()
    snippets = [r.get("content","") for r in data.get("results",[])]
    return "\n\n".join(snippets[:6])


def web_search_fallback(query: str) -> str:
    """Fallback: DuckDuckGo Lite HTML scrape (no API key required)."""
    if not _has_requests:
        return ""
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; MLScholarshipsBot/1.0)"},
            timeout=15,
        )
        # Very basic extraction of result snippets
        text = re.sub(r"<[^>]+>", " ", resp.text)
        text = re.sub(r"\s+", " ", text)
        return text[:4000]
    except Exception as e:
        print(f"Fallback search error: {e}")
        return ""


def search_web(query: str) -> str:
    result = web_search_tavily(query)
    if not result:
        result = web_search_fallback(query)
    return result


def fetch_gmail_messages() -> list[str]:
    """Fetch recent emails from the target mailing lists via Gmail API."""
    if not _has_gmail or not GMAIL_TOKEN_JSON:
        print("Gmail not configured — skipping email scan.")
        return []
    try:
        token_data = json.loads(GMAIL_TOKEN_JSON)
        creds = Credentials.from_authorized_user_info(token_data)
        service = build("gmail", "v1", credentials=creds)

        results = service.users().messages().list(
            userId="me", q=GMAIL_SEARCH_QUERY, maxResults=30
        ).execute()

        messages = []
        for msg in results.get("messages", []):
            m = service.users().messages().get(
                userId="me", id=msg["id"], format="full"
            ).execute()
            body = _extract_body(m["payload"])
            if body:
                messages.append(body[:3000])
        print(f"Fetched {len(messages)} Gmail messages.")
        return messages
    except Exception as e:
        print(f"Gmail error: {e}")
        return []


def _extract_body(payload) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
    for part in payload.get("parts", []):
        result = _extract_body(part)
        if result:
            return result
    return ""


def parse_with_claude(raw_text: str) -> dict:
    """Send raw text to Claude and extract structured opportunities."""
    if not _has_anthropic or not ANTHROPIC_API_KEY:
        print("Claude not available — skipping parsing.")
        return {"scholarships": [], "summer_schools": []}
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Extract all ML/AI opportunities from this text:\n\n{raw_text}"}],
    )
    raw = message.content[0].text.strip()
    # Strip any accidental markdown fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}\nRaw:\n{raw[:500]}")
        return {"scholarships": [], "summer_schools": []}


def merge_new(existing, new_items, existing_keys_set, name_field, id_start):
    added = 0
    next_id_val = id_start
    for item in new_items:
        key = (item.get(name_field, "").lower(), item.get("deadline", ""))
        if key in existing_keys_set:
            continue
        item["id"] = next_id_val
        item.setdefault("section", "auto-" + datetime.date.today().strftime("%b %Y"))
        existing.append(item)
        existing_keys_set.add(key)
        next_id_val += 1
        added += 1
    return added


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    print("Loading existing data...")
    scholarships, summer = load_existing()
    s_keys, ss_keys = existing_keys(scholarships, summer)

    all_text_parts = []

    # 1. Web search
    print("Searching the web...")
    for q in WEB_QUERIES:
        text = search_web(q)
        if text:
            all_text_parts.append(text)

    # 2. Gmail
    print("Checking Gmail...")
    email_bodies = fetch_gmail_messages()
    all_text_parts.extend(email_bodies)

    if not all_text_parts:
        print("No source text gathered. Exiting.")
        return

    # 3. Parse in batches of ~6000 chars to stay within context limits
    batch_size = 6000
    combined = "\n\n---\n\n".join(all_text_parts)
    batches = [combined[i:i+batch_size] for i in range(0, len(combined), batch_size)]

    total_new_s = 0
    total_new_ss = 0

    for i, batch in enumerate(batches):
        print(f"Parsing batch {i+1}/{len(batches)} with Claude...")
        parsed = parse_with_claude(batch)
        total_new_s += merge_new(
            scholarships, parsed.get("scholarships", []),
            s_keys, "institution", next_id(scholarships)
        )
        total_new_ss += merge_new(
            summer, parsed.get("summer_schools", []),
            ss_keys, "name", next_id(summer)
        )

    print(f"New scholarships: {total_new_s} | New summer schools: {total_new_ss}")

    if total_new_s > 0:
        SCHOLARSHIPS_JSON.write_text(json.dumps(scholarships, indent=2, ensure_ascii=False))
        print(f"Wrote {SCHOLARSHIPS_JSON}")
    if total_new_ss > 0:
        SUMMER_JSON.write_text(json.dumps(summer, indent=2, ensure_ascii=False))
        print(f"Wrote {SUMMER_JSON}")

    if total_new_s == 0 and total_new_ss == 0:
        print("No new opportunities found. Nothing to commit.")


if __name__ == "__main__":
    main()
