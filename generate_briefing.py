#!/usr/bin/env python3
"""
Morning Briefing Generator for Gus von Metzsch
Runs daily via GitHub Actions, delivers to gvonmetzsch@gmail.com at 6:45 AM local time.
Timezone is inferred from the most recent flight destination in Google Calendar.
"""

import os
import sys
import json
import base64
import re
import logging
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import pytz
import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ──────────────────────────────── Config ───────────────────────────────────

RECIPIENT = "gvonmetzsch@gmail.com"
TARGET_HOUR = 6
TARGET_MINUTE = 45
# Send if local time is in [6:45 AM, MORNING_CUTOFF). Tolerates GitHub schedule
# delays and lets the first morning run win; dedup prevents repeats.
MORNING_CUTOFF_HOUR = 11

# IATA city codes → pytz timezone strings
AIRPORT_TZ = {
    # North America
    "MEX": "America/Mexico_City", "OAX": "America/Mexico_City",
    "JFK": "America/New_York",    "EWR": "America/New_York",
    "LGA": "America/New_York",    "BOS": "America/New_York",
    "ORD": "America/Chicago",     "MDW": "America/Chicago",
    "LAX": "America/Los_Angeles", "SFO": "America/Los_Angeles",
    "OAK": "America/Los_Angeles", "SJC": "America/Los_Angeles",
    "DEN": "America/Denver",      "PHX": "America/Phoenix",
    "MIA": "America/New_York",    "ATL": "America/New_York",
    "SEA": "America/Los_Angeles", "YYZ": "America/Toronto",
    "YVR": "America/Vancouver",   "CUN": "America/Cancun",
    "GDL": "America/Mexico_City", "MTY": "America/Monterrey",
    # Europe
    "LHR": "Europe/London",       "LGW": "Europe/London",
    "LTN": "Europe/London",       "STN": "Europe/London",
    "CDG": "Europe/Paris",        "ORY": "Europe/Paris",
    "AMS": "Europe/Amsterdam",    "FRA": "Europe/Berlin",
    "MUC": "Europe/Berlin",       "ZRH": "Europe/Zurich",
    "GVA": "Europe/Zurich",       "FCO": "Europe/Rome",
    "MXP": "Europe/Rome",         "MAD": "Europe/Madrid",
    "BCN": "Europe/Madrid",       "IBZ": "Europe/Madrid",
    "LIS": "Europe/Lisbon",       "DUB": "Europe/Dublin",
    "BRU": "Europe/Brussels",     "VIE": "Europe/Vienna",
    "PRG": "Europe/Prague",       "WAW": "Europe/Warsaw",
    "ATH": "Europe/Athens",       "IST": "Europe/Istanbul",
    # Asia / Pacific
    "NRT": "Asia/Tokyo",          "HND": "Asia/Tokyo",
    "ICN": "Asia/Seoul",          "HKG": "Asia/Hong_Kong",
    "SIN": "Asia/Singapore",      "BKK": "Asia/Bangkok",
    "DXB": "Asia/Dubai",          "SYD": "Australia/Sydney",
    "AKL": "Pacific/Auckland",
}

AIRPORT_RE = re.compile(r'\b([A-Z]{3})\b')
FLIGHT_KEYWORDS = re.compile(
    r'\b(flight|fly|flies|airline|airways|air\s+\w+|FR\s*\d|BA\s*\d|AM\s*\d|UA\s*\d|DL\s*\d|AA\s*\d|'
    r'aeromexico|british airways|ryanair|easyjet|lufthansa|delta|united|american|southwest|'
    r'virgin|air france|klm|iberia|finnair)\b',
    re.IGNORECASE
)


# ────────────────────────────── Google Auth ────────────────────────────────

def build_google_credentials() -> Credentials:
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        ],
    )
    creds.refresh(GoogleRequest())
    return creds


# ─────────────────────────── Timezone Inference ─────────────────────────────

def infer_timezone(calendar_service) -> str:
    """
    Infer current timezone from the destination of the most recent past flight
    in Google Calendar. Falls back to calendar's default timezone.
    """
    now_utc = datetime.now(timezone.utc)
    two_months_ago = (now_utc - timedelta(days=60)).isoformat()

    try:
        result = calendar_service.events().list(
            calendarId="primary",
            timeMin=two_months_ago,
            timeMax=now_utc.isoformat(),
            maxResults=50,
            singleEvents=True,
            orderBy="startTime",
            q="flight",
        ).execute()

        events = result.get("items", [])
        for event in reversed(events):
            title = event.get("summary", "")
            desc  = event.get("description", "")
            text  = f"{title} {desc}"
            if not FLIGHT_KEYWORDS.search(text):
                continue
            codes = AIRPORT_RE.findall(text)
            for code in reversed(codes):
                if code in AIRPORT_TZ:
                    log.info("Inferred timezone from flight to %s: %s", code, AIRPORT_TZ[code])
                    return AIRPORT_TZ[code]

        cal_meta = calendar_service.calendars().get(calendarId="primary").execute()
        tz = cal_meta.get("timeZone", "America/New_York")
        log.info("No flight found — using calendar default timezone: %s", tz)
        return tz

    except Exception as exc:
        log.warning("Timezone inference failed: %s — defaulting to America/New_York", exc)
        return "America/New_York"


def is_briefing_window(tz_str: str) -> tuple[bool, datetime]:
    """True if local time is between 6:45 AM and the morning cutoff."""
    tz = pytz.timezone(tz_str)
    local_now = datetime.now(tz)
    after_start = (local_now.hour, local_now.minute) >= (TARGET_HOUR, TARGET_MINUTE)
    before_cutoff = local_now.hour < MORNING_CUTOFF_HOUR
    return (after_start and before_cutoff), local_now


# ─────────────────────────────── Gmail helpers ─────────────────────────────

def already_sent_today(gmail_service, tz_str: str) -> bool:
    """Check if a briefing was already sent to self today (avoid double-send).
    Uses the inferred local date so the 'day' matches the user's wall clock."""
    tz = pytz.timezone(tz_str)
    today_str = datetime.now(tz).strftime("%Y/%m/%d")
    result = gmail_service.users().messages().list(
        userId="me",
        q=f'from:me to:{RECIPIENT} subject:"Morning Briefing" after:{today_str}',
        maxResults=1,
    ).execute()
    return bool(result.get("messages"))


def compute_lookback_hours(gmail_service, tz_str: str) -> float:
    """
    Determine how many hours back to pull inbox/news so each item is captured
    exactly once — even when the timezone changed since the last briefing.

    Window = time since the last briefing was sent (capped to a sane range).
    If no prior briefing is found, default to 24 hours. When the user has
    flown east (day got shorter) this naturally shrinks; flown west (day got
    longer) it naturally grows, so nothing is missed or double-counted.
    """
    try:
        result = gmail_service.users().messages().list(
            userId="me",
            q=f'from:me to:{RECIPIENT} subject:"Morning Briefing"',
            maxResults=1,
        ).execute()
        msgs = result.get("messages", [])
        if not msgs:
            return 24.0
        detail = gmail_service.users().messages().get(
            userId="me", id=msgs[0]["id"], format="metadata",
            metadataHeaders=["Date"],
        ).execute()
        internal_ms = int(detail.get("internalDate", "0"))
        if internal_ms <= 0:
            return 24.0
        last_sent = datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc)
        hours = (datetime.now(timezone.utc) - last_sent).total_seconds() / 3600.0
        # Clamp: never less than 6h (avoid gaps from a same-day re-run) or more than 48h.
        return max(6.0, min(hours + 0.5, 48.0))
    except Exception as exc:
        log.warning("Lookback computation failed: %s — defaulting to 24h", exc)
        return 24.0


def fetch_inbox(gmail_service, since_utc: datetime) -> list[dict]:
    since_str = since_utc.strftime("%Y/%m/%d")
    result = gmail_service.users().threads().list(
        userId="me",
        q=f"in:inbox after:{since_str} -category:promotions",
        maxResults=50,
    ).execute()
    threads = result.get("threads", [])
    enriched = []
    for t in threads:
        detail = gmail_service.users().threads().get(
            userId="me", id=t["id"], format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        ).execute()
        msgs = detail.get("messages", [])
        if not msgs:
            continue
        m = msgs[-1]
        # Filter precisely by timestamp (Gmail 'after:' is date-granular only).
        internal_ms = int(m.get("internalDate", "0"))
        if internal_ms and datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc) < since_utc:
            continue
        headers = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
        enriched.append({
            "subject": headers.get("Subject", ""),
            "from":    headers.get("From", ""),
            "date":    headers.get("Date", ""),
            "snippet": m.get("snippet", "")[:300],
        })
    return enriched


def fetch_newsletter_body(gmail_service, thread_id: str) -> str:
    detail = gmail_service.users().threads().get(
        userId="me", id=thread_id, format="full"
    ).execute()
    msgs = detail.get("messages", [])
    if not msgs:
        return ""

    def extract(payload):
        mime = payload.get("mimeType", "")
        if mime == "text/plain":
            data = payload.get("body", {}).get("data", "")
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
        if mime == "text/html":
            data = payload.get("body", {}).get("data", "")
            html = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
            return re.sub(r'<[^>]+>', ' ', html)
        for part in payload.get("parts", []):
            result = extract(part)
            if result:
                return result
        return ""

    text = extract(msgs[0].get("payload", {}))
    return re.sub(r'\s+', ' ', text).strip()[:6000]


def find_newsletter_threads(gmail_service, since_utc: datetime) -> dict[str, str]:
    since_str = since_utc.strftime("%Y/%m/%d")
    targets = {
        "DealBook":     ("nytdirect@nytimes.com", "DealBook"),
        "Term Sheet":   ("termsheet@mail.fortune.com", ""),
        "WSJ 10-Point": ("access@interactive.wsj.com", "10-Point"),
        "Morning Brew": ("crew@morningbrew.com", ""),
        "NYT Morning":  ("nytdirect@nytimes.com", "The Morning"),
    }
    found = {}
    for name, (sender, subj_kw) in targets.items():
        q = f"from:{sender} after:{since_str}"
        if subj_kw:
            q += f' subject:"{subj_kw}"'
        result = gmail_service.users().threads().list(
            userId="me", q=q, maxResults=3
        ).execute()
        threads = result.get("threads", [])
        if threads:
            found[name] = threads[0]["id"]
    return found


# ────────────────────────────── Calendar helpers ───────────────────────────

def fetch_calendar_events(calendar_service, tz_str: str) -> dict:
    tz = pytz.timezone(tz_str)
    local_now = datetime.now(tz)
    start_of_day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day   = local_now.replace(hour=23, minute=59, second=59, microsecond=0)
    end_of_month = local_now + timedelta(days=30)

    def list_events(time_min, time_max, max_results=50):
        return calendar_service.events().list(
            calendarId="primary",
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_results,
        ).execute().get("items", [])

    return {
        "timezone": tz_str,
        "local_date": local_now.strftime("%A, %B %-d, %Y"),
        "today": list_events(start_of_day, end_of_day),
        "upcoming": list_events(end_of_day, end_of_month)[:30],
    }


# ─────────────────────────────── News search ───────────────────────────────

def web_search(query: str, num: int = 5) -> list[dict]:
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
    if not api_key:
        return []
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/news/search",
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            params={"q": query, "count": num, "freshness": "pd"},
            timeout=10,
        )
        resp.raise_for_status()
        return [{"title": r.get("title"), "url": r.get("url"), "description": r.get("description", "")}
                for r in resp.json().get("results", [])]
    except Exception as exc:
        log.warning("Brave search failed for '%s': %s", query, exc)
        return []


# ────────────────────────────── Main generation ────────────────────────────

SYSTEM_PROMPT = """You are a sharp, concise AI assistant generating a personalized morning briefing email for August (Gus) von Metzsch.

About Gus:
- Rising sophomore at Princeton (ORFE), D1 men's lacrosse player, student VC club
- Targeting high finance (IB, PE, HF, quant)
- Affluent background, avid traveler — skiing, wakesurfing, rafting, backpacking, nightlife, history, culture
- Bay Area native, also follows Boston teams and Princeton athletics

Briefing rules:
1. CALENDAR: Bullet points with brief prose within each. Include events with times, tasks with due dates. If the calendar is empty today, say so in ONE line. After today, add a very brief prose overview of the day, next week, and next month (1-2 sentences each, weighted by how much is happening).
2. INBOX: Lead with time-sensitive items (deadlines, things awaiting a reply). One line per item with sender. SKIP newsletters here. Skip promotional/automated noise unless it has a clear action item (package/flight status, etc.). Sort by priority/action type, with a short 'potentially relevant' group at the end. No commentary on who emails are addressed to. If nothing substantial, say nothing about there being nothing.
3. NEWS: Start from newsletter content, fill gaps with search results, preferring newsletter content. Priority order: world headlines (major items + maybe 1 niche cool one) -> 3 WSJ links (one macro, one company/industry, one op-ed; FT/Economist/NYT acceptable fallback) -> markets & macro -> M&A/PE/dealflow -> tech/AI -> athletics (lacrosse NCAA D1 + pro, football, golf, snow sports, hockey, basketball, baseball; bias Princeton, Bay Area, Boston) -> 3 unique niche cool things -> 3 learning points (>=1 on actionable tech/AI skill or workflow). Skip empty categories rather than padding. Tight, mostly bullets. Only include links you are confident are live.
4. If any section/subsection is empty, skip it entirely with no mention (Calendar's 'empty today' one-liner is the only exception).
5. Output ONLY the HTML email body. Table-based layout, all inline CSS, no JS, no external fonts, web-safe fonts only. Sleek futuristic/tech but minimal aesthetic. MUST be clearly readable on Gmail mobile and web in BOTH light and dark mode. Dark card on near-black background, cyan accent.
6. Target 1000-2500 words of visible text.
"""


def build_user_prompt(calendar_data, inbox_items, newsletters, news_results, tz_str, local_now):
    day = local_now.day
    suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    date_str = local_now.strftime(f"%B %-d{suffix}, %Y")
    subject = f"Gus's Morning Briefing - {date_str}"

    parts = [
        f"# SUBJECT LINE\n{subject}\n",
        f"# CURRENT TIMEZONE\n{tz_str} (it is {local_now.strftime('%I:%M %p %Z')} locally)\n",
        f"\n# CALENDAR DATA\n{json.dumps(calendar_data, indent=2, default=str)}\n",
        f"\n# INBOX ITEMS (since last briefing)\n{json.dumps(inbox_items, indent=2)}\n",
        "\n# NEWSLETTER CONTENT (plain text, already stripped)\n",
    ]
    for name, text in newsletters.items():
        parts.append(f"\n## {name}\n{text[:4000]}\n")
    parts.append("\n# WEB SEARCH NEWS RESULTS (use to fill gaps only)\n")
    for topic, results in news_results.items():
        parts.append(f"\n## {topic}\n{json.dumps(results, indent=2)}\n")
    parts.append("""
# OUTPUT INSTRUCTIONS
Generate the complete HTML email body (no DOCTYPE/html/head — just the visible content in a wrapper table).
First line must be a comment: <!-- SUBJECT: the subject line here -->
Sleek futuristic minimal aesthetic: near-black background (#08080f), dark card (#0f0f1c), cyan accent (#00d4ff), light text (#c8c8e0), muted labels (#4a5070). Use monospace (Courier New) only for small section labels; Arial/Helvetica for body.
Table-based layout. All CSS inline. No external resources, no web fonts, no JS. Must read well on Gmail mobile + web, light + dark mode.
""")
    return "".join(parts)


def generate_html_briefing(user_prompt: str) -> tuple[str, str]:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    html = message.content[0].text
    m = re.search(r'<!--\s*SUBJECT:\s*(.+?)\s*-->', html)
    subject = m.group(1).strip() if m else f"Gus's Morning Briefing - {datetime.utcnow().strftime('%B %-d, %Y')}"
    return subject, html


# ─────────────────────────────── Gmail sending ─────────────────────────────

def send_email(gmail_service, subject: str, html_body: str):
    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<title>{subject}</title>
</head>
<body style="margin:0;padding:0;background-color:#08080f;" bgcolor="#08080f">
{html_body}
</body>
</html>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = RECIPIENT
    msg["To"]      = RECIPIENT
    msg.attach(MIMEText(full_html, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()
        log.info("Briefing sent: %s", subject)
    except Exception as exc:
        log.warning("Send failed (%s) — saving draft instead.", exc)
        gmail_service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        log.info("Briefing saved as draft.")


# ──────────────────────────────────── Main ─────────────────────────────────

def main():
    log.info("Starting morning briefing job...")
    creds        = build_google_credentials()
    calendar_svc = build("calendar", "v3", credentials=creds)
    gmail_svc    = build("gmail",    "v1", credentials=creds)

    tz_str = infer_timezone(calendar_svc)
    in_window, local_now = is_briefing_window(tz_str)

    if not in_window:
        log.info("Outside morning send window for %s (local %s). Exiting.", tz_str, local_now.strftime("%H:%M"))
        sys.exit(0)

    if already_sent_today(gmail_svc, tz_str):
        log.info("Already sent today. Exiting.")
        sys.exit(0)

    calendar_data = fetch_calendar_events(calendar_svc, tz_str)

    # Window the inbox/news pull so every item is captured exactly once,
    # adjusting for any timezone change since the last briefing.
    lookback_h = compute_lookback_hours(gmail_svc, tz_str)
    since_utc  = datetime.now(timezone.utc) - timedelta(hours=lookback_h)
    log.info("Inbox lookback window: %.1f hours", lookback_h)

    inbox_items = fetch_inbox(gmail_svc, since_utc)
    nl_ids      = find_newsletter_threads(gmail_svc, since_utc)
    newsletters = {name: fetch_newsletter_body(gmail_svc, tid) for name, tid in nl_ids.items()}

    news_queries = {
        "World headlines": "major world news today",
        "Markets & macro": "stock market inflation interest rates today",
        "M&A PE":          "M&A private equity deals fundraising",
        "Tech AI":         "AI OpenAI Anthropic technology news today",
        "Lacrosse":        "NCAA lacrosse PLL Premier Lacrosse League",
        "NBA":             "NBA Finals results",
        "World Cup":       "FIFA World Cup results scores",
    }
    news_results = {topic: web_search(q) for topic, q in news_queries.items()}

    user_prompt   = build_user_prompt(calendar_data, inbox_items, newsletters, news_results, tz_str, local_now)
    subject, html = generate_html_briefing(user_prompt)
    send_email(gmail_svc, subject, html)
    log.info("Done.")


if __name__ == "__main__":
    main()
