#!/usr/bin/env python3
"""
Morning Briefing Generator for Gus von Metzsch
Runs daily via GitHub Actions, delivers to gvonmetzsch@gmail.com at 6:45 AM local time.
Timezone is inferred from the most recent flight destination in Google Calendar.
"""

from __future__ import annotations

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

# ─── EMAIL THEME (edit me) ──────────────────────────────────────────────────
# We ship a SOFT LIGHT palette engineered for Gmail's mobile dark-mode flip:
# Gmail's app partially color-INVERTS emails it reads in dark mode (it ignores
# prefers-color-scheme / color-scheme meta on mobile), so a comfortable, medium-
# contrast light design inverts into a clean dark one — while the un-flipped
# browser view stays easy on the eyes. Never pure #fff / #000 (those invert
# harshly). To switch back to a native dark look later, set EMAIL_THEME = "dark".
EMAIL_THEME = "light"
THEMES = {
    "light": {
        "page_bg": "#eef0f4",   # soft light gray (never #ffffff)
        "card_bg": "#f9fafc",   # off-white card
        "text":    "#33384a",   # dark slate (never #000) — medium contrast
        "muted":   "#6b7280",   # muted labels / meta
        "accent":  "#1664c0",   # primary accent — clear blue
        "accent2": "#0e8aa8",   # secondary accent — teal/cyan, for small pops
        "chip_bg": "#e6eef9",   # tinted background for chips/eyebrows/bars
        "border":  "#dde1e8",   # hairline borders
    },
    "dark": {
        "page_bg": "#0d0d16",
        "card_bg": "#15151f",
        "text":    "#e8e8f0",
        "muted":   "#9aa0b8",
        "accent":  "#00d4ff",
        "accent2": "#a78bfa",
        "chip_bg": "#1d2233",
        "border":  "#262636",
    },
}
PALETTE = THEMES[EMAIL_THEME]

# ─── GOOGLE KEEP TODOS (edit me) ────────────────────────────────────────────
# Todos live in Google Keep (no official personal API) and are read via the
# unofficial gkeepapi library using a master token on a SECONDARY account
# (GOOGLE_KEEP_EMAIL / GOOGLE_KEEP_MASTER_TOKEN). Keep sharing is per-note, so
# each note to be read must be shared into that account. Notes are plain text
# (not checklists). The note below is the primary focus; other notes are skimmed.
KEEP_PRIMARY_NOTE_TITLE = "Tasks"
KEEP_SKIM_OTHER_NOTES   = True   # also lightly skim non-primary notes for todos
KEEP_MAX_OTHER_NOTES    = 12     # cap how many other notes get skimmed
KEEP_MAX_LINES_PER_NOTE = 40     # truncate long notes

# ─── WHOOP (official OAuth2 v2 API) ─────────────────────────────────────────
WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
WHOOP_API_BASE  = "https://api.prod.whoop.com/developer/v2"
WHOOP_LOOKBACK_DAYS = 7   # retrospective window (user is usually still asleep at send time)

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


def _message_plain_text(detail: dict) -> str:
    """Plain text of a single Gmail message (prefers text/plain, else strips HTML)."""
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
            r = extract(part)
            if r:
                return r
        return ""
    return re.sub(r'\s+', ' ', extract(detail.get("payload", {}))).strip()


def fetch_recent_briefings_text(gmail_service, n: int = 2) -> list[str]:
    """Plain text of the last n briefings already sent, so the model can avoid
    repeating the same scores/headlines/storylines day over day. Fail-soft → []."""
    try:
        result = gmail_service.users().messages().list(
            userId="me",
            q=f'from:me to:{RECIPIENT} subject:"Morning Briefing"',
            maxResults=n,
        ).execute()
        out = []
        for m in result.get("messages", []):
            detail = gmail_service.users().messages().get(
                userId="me", id=m["id"], format="full"
            ).execute()
            txt = _message_plain_text(detail)
            if txt:
                out.append(txt[:6000])
        log.info("Dedupe: loaded %d recent briefing(s).", len(out))
        return out
    except Exception as exc:
        log.warning("Recent-briefings (dedupe) fetch failed: %s", exc)
        return []


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
    """Search via the Brave Web Search endpoint (included in the free plan).
    Pulls any fresh news cluster first, then general web results.
    Fails soft to [] so the briefing still builds if Brave is unavailable."""
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
    if not api_key:
        return []
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            params={"q": query, "count": num, "freshness": "pd"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        out = []
        for item in data.get("news", {}).get("results", []):
            out.append({"title": item.get("title"), "url": item.get("url"),
                        "description": item.get("description", "")})
        for item in data.get("web", {}).get("results", []):
            out.append({"title": item.get("title"), "url": item.get("url"),
                        "description": item.get("description", "")})
        return out[:num]
    except Exception as exc:
        log.warning("Brave search failed for '%s': %s", query, exc)
        return []


# ── Authoritative sports scores (ESPN public scoreboard API, no key) ─────────
# Brave snippets produced confidently-wrong scores (e.g. a fabricated upset), so
# scores now come from ESPN's structured feed and the model is told to use them
# verbatim and never invent a result. Scoped to yesterday→today (local) so each
# briefing covers fresh games rather than restating the same ones.
ESPN_SCOREBOARDS = {
    "NBA":               "basketball/nba",
    "NHL":               "hockey/nhl",
    "MLB":               "baseball/mlb",
    "World Cup":         "soccer/fifa.world",
    "NCAA Lacrosse (M)": "lacrosse/mens-college-lacrosse",
}


def _parse_espn_event(ev: dict) -> dict:
    comp  = (ev.get("competitions") or [{}])[0]
    stype = (ev.get("status") or {}).get("type", {})
    sides = {}
    for c in comp.get("competitors", []):
        team = c.get("team") or {}
        sides[c.get("homeAway", "?")] = {
            "team":   team.get("displayName") or team.get("abbreviation"),
            "score":  c.get("score"),
            "winner": c.get("winner"),
        }
    return {
        "matchup": ev.get("shortName") or ev.get("name"),
        "date":    ev.get("date"),
        "state":   stype.get("state"),                       # pre | in | post
        "status":  stype.get("shortDetail") or stype.get("description"),
        "home":    sides.get("home"),
        "away":    sides.get("away"),
    }


def fetch_espn_scores(local_now) -> dict:
    """Real scores (final + in-progress) from ESPN, scoped to yesterday→today
    local. Per-league fail-soft; returns {} if nothing/everything errors."""
    yday  = (local_now - timedelta(days=1)).strftime("%Y%m%d")
    today = local_now.strftime("%Y%m%d")
    dates = f"{yday}-{today}"
    out = {}
    for label, path in ESPN_SCOREBOARDS.items():
        try:
            resp = requests.get(
                f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard",
                params={"dates": dates}, timeout=10,
            )
            resp.raise_for_status()
            games = [_parse_espn_event(e) for e in resp.json().get("events", [])]
            games = [g for g in games if g.get("state") in ("in", "post")]  # drop not-yet-played
            if games:
                out[label] = games
        except Exception as exc:
            log.warning("ESPN scores failed for %s: %s", label, exc)
    log.info("ESPN scores: %s", {k: len(v) for k, v in out.items()})
    return out


# ──────────────────────────────────── WHOOP ─────────────────────────────────
# WHOOP uses OAuth2 with ROTATING refresh tokens: every refresh returns a NEW
# refresh token and invalidates the old one. Because the token lives in a GitHub
# Actions secret, we must write the rotated token back (via the GitHub REST API +
# libsodium sealed box) or the next run can't authenticate. All of this is
# fail-soft: any WHOOP error returns None so the briefing still builds.

def persist_whoop_refresh_token(new_token: str) -> None:
    """Write the rotated WHOOP refresh token back into the WHOOP_REFRESH_TOKEN
    GitHub Actions secret. No-op if unchanged or if no PAT is configured (so
    local/manual runs still work — the token just won't rotate there)."""
    current = os.environ.get("WHOOP_REFRESH_TOKEN", "")
    if not new_token or new_token == current:
        return
    pat = os.environ.get("GH_SECRETS_PAT", "")
    if not pat:
        log.warning("WHOOP refresh token rotated but GH_SECRETS_PAT is unset — "
                    "cannot persist; next scheduled run may fail to authenticate.")
        return
    try:
        from nacl import encoding, public  # lazy import (only needed here)

        repo = os.environ.get("GITHUB_REPOSITORY", "gvonmetzsch/claude")
        api = f"https://api.github.com/repos/{repo}/actions/secrets"
        headers = {
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        key_resp = requests.get(f"{api}/public-key", headers=headers, timeout=10)
        key_resp.raise_for_status()
        key_data = key_resp.json()
        pk = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
        sealed = public.SealedBox(pk).encrypt(new_token.encode())
        encrypted_value = base64.b64encode(sealed).decode()
        put_resp = requests.put(
            f"{api}/WHOOP_REFRESH_TOKEN",
            headers=headers,
            json={"encrypted_value": encrypted_value, "key_id": key_data["key_id"]},
            timeout=10,
        )
        put_resp.raise_for_status()
        log.info("Persisted rotated WHOOP refresh token to GitHub secret.")
    except Exception as exc:
        log.warning("Failed to persist rotated WHOOP refresh token: %s — "
                    "next scheduled run may fail to authenticate.", exc)


def whoop_refresh_access_token() -> str:
    """Exchange the stored refresh token for an access token. WHOOP returns a new
    refresh token each time (with the 'offline' scope), which we persist
    immediately so it is never lost. Returns the access token."""
    resp = requests.post(
        WHOOP_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": os.environ["WHOOP_REFRESH_TOKEN"],
            "client_id": os.environ["WHOOP_CLIENT_ID"],
            "client_secret": os.environ["WHOOP_CLIENT_SECRET"],
            "scope": "offline",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    resp.raise_for_status()
    tok = resp.json()
    new_refresh = tok.get("refresh_token")
    if new_refresh:
        # Persist immediately — even if the data calls below fail, the freshly
        # issued refresh token must survive for the next run.
        persist_whoop_refresh_token(new_refresh)
        os.environ["WHOOP_REFRESH_TOKEN"] = new_refresh
    return tok["access_token"]


def _whoop_get(path: str, headers: dict, params: dict | None = None) -> dict:
    resp = requests.get(f"{WHOOP_API_BASE}{path}", headers=headers,
                        params=params or {}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def fetch_whoop_data() -> dict | None:
    """Pull a retrospective ~7-day WHOOP snapshot (recovery trend, recent sleep,
    daily strain, notable workouts). The briefing usually fires before the user
    has woken up, so last night's sleep may not exist yet — we pull history and
    treat last night as 'include if present'. Fail-soft: returns None on error."""
    if not all(os.environ.get(k) for k in
               ("WHOOP_CLIENT_ID", "WHOOP_CLIENT_SECRET", "WHOOP_REFRESH_TOKEN")):
        return None
    try:
        access = whoop_refresh_access_token()
        headers = {"Authorization": f"Bearer {access}"}
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=WHOOP_LOOKBACK_DAYS)).isoformat()
        end = now.isoformat()
        window = {"start": start, "end": end, "limit": 25}

        data: dict = {}

        def safe(label, fn):
            try:
                return fn()
            except Exception as exc:
                log.warning("WHOOP %s fetch failed: %s", label, exc)
                return None

        # Recovery trend (newest first)
        rec = safe("recovery", lambda: _whoop_get("/recovery", headers, window))
        if rec:
            recs = []
            for r in rec.get("records", []):
                s = r.get("score") or {}
                recs.append({
                    "date": r.get("created_at", "")[:10],
                    "recovery_score": s.get("recovery_score"),
                    "hrv_ms": s.get("hrv_rmssd_milli"),
                    "resting_hr": s.get("resting_heart_rate"),
                })
            data["recovery_week"] = recs
            data["latest_recovery"] = recs[0] if recs else None

        # Recent sleep (last few nights; last night only if recorded)
        slp = safe("sleep", lambda: _whoop_get("/activity/sleep", headers, window))
        if slp:
            sleeps = []
            for r in slp.get("records", [])[:5]:
                s = r.get("score") or {}
                stage = s.get("stage_summary") or {}
                in_bed_ms = (stage.get("total_in_bed_time_milli") or 0)
                awake_ms = (stage.get("total_awake_time_milli") or 0)
                hours = round(max(in_bed_ms - awake_ms, 0) / 3_600_000, 1) or None
                sleeps.append({
                    "date": r.get("end", "")[:10],
                    "performance_pct": s.get("sleep_performance_percentage"),
                    "efficiency_pct": s.get("sleep_efficiency_percentage"),
                    "hours": hours,
                })
            data["sleep_recent"] = sleeps

        # Daily strain trend
        cyc = safe("cycle", lambda: _whoop_get("/cycle", headers, window))
        if cyc:
            strains = []
            for r in cyc.get("records", []):
                s = r.get("score") or {}
                strains.append({"date": r.get("start", "")[:10],
                                "strain": s.get("strain")})
            data["strain_week"] = strains

        # Notable workouts in the window (so "major things you've done" can surface)
        wko = safe("workout", lambda: _whoop_get("/activity/workout", headers, window))
        if wko:
            workouts = []
            for r in wko.get("records", []):
                s = r.get("score") or {}
                workouts.append({
                    "date": r.get("start", "")[:10],
                    "sport": r.get("sport_name") or r.get("sport_id"),
                    "strain": s.get("strain"),
                    "kilojoule": s.get("kilojoule"),
                })
            data["workouts_week"] = workouts

        return data or None
    except Exception as exc:
        log.warning("WHOOP fetch failed: %s", exc)
        return None


# ──────────────────────────────── Google Keep ───────────────────────────────
# Todos live in Google Keep, which has no official personal API. We read them via
# the unofficial gkeepapi library (master-token auth) on a SECONDARY account.
# This is against Google's ToS and can break on Google-side changes, so it's kept
# fully fail-soft and runs last. Each note must be shared into the secondary
# account (Keep sharing is per-note). Notes are plain text, not checklists.

def fetch_keep_todos() -> dict | None:
    """Return the primary 'Tasks' note plus a light skim of other notes, as plain
    text lines. Fail-soft: returns None if secrets are missing or anything errors."""
    email = os.environ.get("GOOGLE_KEEP_EMAIL", "")
    master_token = os.environ.get("GOOGLE_KEEP_MASTER_TOKEN", "")
    if not email or not master_token:
        return None
    try:
        import gkeepapi  # lazy import so a missing/broken lib can't crash module load

        keep = gkeepapi.Keep()
        keep.authenticate(email, master_token)
        keep.sync()

        def note_lines(note):
            text = getattr(note, "text", "") or ""
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            return lines[:KEEP_MAX_LINES_PER_NOTE]

        primary = None
        others = []
        for note in keep.all():
            if getattr(note, "trashed", False) or getattr(note, "archived", False):
                continue
            title = (getattr(note, "title", "") or "").strip()
            lines = note_lines(note)
            if not title and not lines:
                continue
            if title.lower() == KEEP_PRIMARY_NOTE_TITLE.lower():
                primary = {"title": title or KEEP_PRIMARY_NOTE_TITLE, "lines": lines}
            elif KEEP_SKIM_OTHER_NOTES and lines:
                others.append({"title": title, "lines": lines})

        others = others[:KEEP_MAX_OTHER_NOTES]
        if not primary and not others:
            return None
        return {"primary": primary, "other": others}
    except Exception as exc:
        log.warning("Google Keep fetch failed: %s", exc)
        return None


# ── Upcoming events/tickets scraped from email ──────────────────────────────
# Tickets, travel, and reservations often arrive months before the event date,
# so scan up to a year back, then let Claude keep only those dated within the
# next-week horizon. (Gmail can filter by RECEIVED date, not EVENT date, so the
# wide receive-window is necessary — the event-date filtering happens in-prompt.)
EVENT_EMAIL_LOOKBACK = "12m"   # Gmail newer_than: window
EVENT_HORIZON_DAYS   = 7       # only surface events dated today .. +N days
EVENT_MAX_CANDIDATES = 300     # cap messages fetched (bounds latency + tokens)

# High-precision sources: ticketing, airlines/rail, lodging/travel, dining.
_EVENT_SENDERS = [
    "ticketmaster.com", "livenation.com", "seatgeek.com", "axs.com",
    "stubhub.com", "vividseats.com", "eventbrite.com", "dice.fm",
    "ticketweb.com", "gametime.co", "etix.com",
    "united.com", "delta.com", "aa.com", "southwest.com", "jetblue.com",
    "alaskaair.com", "aeromexico.com", "volaris.com", "lufthansa.com",
    "britishairways.com", "amtrak.com",
    "expedia.com", "booking.com", "hotels.com", "airbnb.com", "vrbo.com",
    "marriott.com", "hilton.com", "hyatt.com", "tripit.com", "kayak.com",
    "opentable.com", "resy.com", "exploretock.com", "sevenrooms.com",
]
# High-signal phrases for ticket/travel/reservation mail from senders we didn't enumerate.
_EVENT_KEYWORDS = [
    '"your tickets"', '"e-ticket"', '"mobile ticket"', '"boarding pass"',
    '"flight confirmation"', "itinerary", '"reservation confirmed"',
    '"your reservation"', '"booking confirmation"', '"order confirmed"',
    '"doors open"', '"you\'re going"',
]


def _build_event_query() -> str:
    senders = " ".join(f"from:{d}" for d in _EVENT_SENDERS)
    keywords = " ".join(_EVENT_KEYWORDS)
    # {a b c} is Gmail's OR group. Promotions are kept IN — real ticket/order
    # confirmations are frequently filed under that category.
    return f"newer_than:{EVENT_EMAIL_LOOKBACK} {{{senders} {keywords}}}"


def fetch_email_events(gmail_service) -> list[dict]:
    """Scan up to ~12 months of ticket/travel/reservation email and return
    lightweight candidates (subject/from/received/snippet). Claude extracts the
    actual EVENT date and keeps only those within the next-week horizon. The
    event date usually sits in the subject/snippet, so metadata format is enough
    and we avoid pulling full bodies. Fail-soft → [] on any error."""
    try:
        q = _build_event_query()
        ids, page_token = [], None
        while len(ids) < EVENT_MAX_CANDIDATES:
            resp = gmail_service.users().messages().list(
                userId="me", q=q, maxResults=100, pageToken=page_token,
            ).execute()
            ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        out = []
        for mid in ids[:EVENT_MAX_CANDIDATES]:
            try:
                m = gmail_service.users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                ).execute()
            except Exception:
                continue
            headers = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
            out.append({
                "subject":  headers.get("Subject", ""),
                "from":     headers.get("From", ""),
                "received": headers.get("Date", ""),
                "snippet":  m.get("snippet", "")[:300],
            })
        log.info("Email-events scan: %d candidate messages.", len(out))
        if len(out) >= EVENT_MAX_CANDIDATES:
            log.warning("Email-events scan hit the %d candidate cap — older "
                        "advance-purchase confirmations may be excluded.", EVENT_MAX_CANDIDATES)
        return out
    except Exception as exc:
        log.warning("Email-events scan failed: %s", exc)
        return []


# ────────────────────────────── Main generation ────────────────────────────

SYSTEM_PROMPT = """You are a sharp, concise AI assistant generating a personalized morning briefing email for August (Gus) von Metzsch.

About Gus:
- Rising sophomore at Princeton (ORFE), D1 men's lacrosse player, student VC club
- Targeting high finance (IB, PE, HF, quant)
- Affluent background, avid traveler — skiing, wakesurfing, rafting, backpacking, nightlife, history, culture
- Bay Area native, also follows Boston teams and Princeton athletics

Briefing rules:
1. OPENING (lead the email with this, in order):
   a) A brief PROSE PARAGRAPH (a few sentences, warm personal-assistant tone) that weaves together: a SHORT retrospective WHOOP read (how recovery/strain have trended over yesterday and the past week, plus any notable workouts — keep it brief, and only mention last night's sleep if sleep data for last night is actually present), the day's todos at a high level (heavily weight the note titled "Tasks"; lightly fold in anything clearly todo-like skimmed from the other notes), and what today's calendar holds. If something from the rest of the briefing body is genuinely MAJOR, it may earn one sentence here too. Omit WHOOP or todos silently if their data is absent — never mention missing data.
   b) QUICK KEY TASKS: a tight bulleted list of the few most important/actionable todos (drawn mainly from "Tasks"). Skip if there are none.
   c) CALENDAR & UPCOMING: bullet points with brief prose within each — today's events with times, tasks with due dates. If the calendar is empty today, say so in ONE line. After today, add a very brief prose overview of the day, next week, and next month (1-2 sentences each, weighted by how much is happening). Also fold in any EVENTS/TICKETS FROM EMAIL dated within the next week (concerts, games, flights, trips, hotel/restaurant reservations) that are NOT already on the calendar — list them like calendar items with their date/time and venue. A genuinely major one (a concert tonight, a flight today) may instead/also earn a mention in the opening paragraph (a). Only include an email-derived event if its date is clearly within the next week; never invent or guess a date.
2. INBOX: Lead with time-sensitive items (deadlines, things awaiting a reply). One line per item with sender. SKIP newsletters here. Skip promotional/automated noise unless it has a clear action item (package/flight status, etc.). Sort by priority/action type, with a short 'potentially relevant' group at the end. No commentary on who emails are addressed to. If nothing substantial, say nothing about there being nothing.
3. NEWS: Start from newsletter content, fill gaps with search results, preferring newsletter content. DEDUPE (critical): do NOT repeat any score, headline, or storyline that already appeared in the ALREADY REPORTED IN RECENT BRIEFINGS section — surface only NEW developments; ongoing items (series standings, tournaments in progress) may be updated with what changed but never restated unchanged. Priority order: world headlines (major items + maybe 1 niche cool one) -> 3 WSJ links (one macro, one company/industry, one op-ed; FT/Economist/NYT acceptable fallback) -> markets & macro -> M&A/PE/dealflow -> tech/AI -> athletics (lacrosse NCAA D1 + pro, football, golf, snow sports, hockey, basketball, baseball; bias Princeton, Bay Area, Boston): keep the brief prose coverage of notable storylines/news, THEN a real SCOREBOARD grouped BY SPORT. Under each sport (e.g. NHL, NBA, MLB, Lacrosse, Golf, Soccer), a tight text list of games with the ACTUAL final score — "matchup — final score" (e.g. "Oilers vs Panthers (Stanley Cup Final G5) — EDM 3-1", "Red Sox @ Yankees — BOS 6-3", "Giants @ Dodgers — LAD 4-2"). Lead each sport with Gus's teams (Princeton; Bay Area: Warriors/Giants/49ers/Sharks/A's; Boston: Celtics/Bruins/Red Sox), then include other notable/marquee results — finals & championship games (e.g. the Stanley Cup Final, NBA Finals), big upsets, and tournament leaders/standings. Scores MUST be taken verbatim from the REAL SPORTS SCORES (ESPN) section — never from news snippets, prior knowledge, or inference. If a game is NOT in that data, do not report a score for it (you may mention the matchup as upcoming or result-pending from the news, but NEVER fabricate the outcome or winner). ALWAYS give the exact real score; NEVER vague summaries like "Germany dominant" or "close game". Per sport, include AT MOST 3 scores — lead with Gus's teams, then only genuinely notable/marquee results (finals, championship/playoff games, big upsets, rivalry games). Fewer than 3 is good; do NOT pad with routine regular-season games Gus has no connection to, and skip a sport entirely if it has nothing new or noteworthy (he is not a casual fan of every sport — quality over quantity) -> 3 unique niche cool things -> 3 learning points (>=1 on actionable tech/AI skill or workflow). Skip empty categories rather than padding. Tight, mostly bullets. Only include links you are confident are live.
4. If any section/subsection is empty, skip it entirely with no mention (Calendar's 'empty today' one-liner is the only exception).
5. FORMATTING — output ONLY the HTML email body (no DOCTYPE/html/head; just the visible content starting with a wrapper table). Hard requirements:
   - Table-based layout only (nested <table>; no <div> for structure). ALL CSS inline. No <style> block, no JS, no external resources, no web fonts. Web-safe fonts only: Arial/Helvetica for everything, Courier New ONLY for small uppercase section labels.
   - CRITICAL for Gmail mobile dark mode (which partially color-INVERTS the email): put an explicit bgcolor="..." attribute AND an inline background-color on EVERY <table>, <tr>, and <td> — including spacer/padding cells and the outer wrapper. No cell may be left without a background. NEVER use pure #000000 or #ffffff for any background or text. Use ONLY the exact palette hex values given in the user message. This is what makes the inversion render cleanly.
   - Set explicit inline font-size (in px), font-weight, line-height (in px), and color on EVERY text element; put margin:0 on any heading tags. Never rely on a tag's default sizing (Gmail mobile re-scales it). Use the exact type scale given in the user message.
   - Aesthetic: clean, minimal, modern. Must read well on Gmail mobile (in dark mode, post-inversion), Gmail web (light), and Apple Mail — comfortable medium contrast, not harsh.
6. Target 1000-2500 words of visible text.
"""


def build_user_prompt(calendar_data, inbox_items, newsletters, news_results, tz_str,
                      local_now, whoop_data=None, keep_todos=None, email_events=None,
                      espn_scores=None, recent_briefings=None):
    day = local_now.day
    suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    date_str = local_now.strftime(f"%B %-d{suffix}, %Y")
    subject = f"Gus's Morning Briefing - {date_str}"

    parts = [
        f"# SUBJECT LINE\n{subject}\n",
        f"# CURRENT TIMEZONE\n{tz_str} (it is {local_now.strftime('%I:%M %p %Z')} locally)\n",
    ]

    # OPENING INPUTS — feed WHOOP + todos so the email can lead with them.
    if whoop_data or keep_todos:
        parts.append("\n# OPENING INPUTS (for the opening paragraph + key tasks)\n")
        if whoop_data:
            parts.append(
                "\n## WHOOP (retrospective ~7 days; usually no last-night sleep yet — "
                "summarize trends, mention last night only if present)\n"
                f"{json.dumps(whoop_data, indent=2, default=str)}\n")
        if keep_todos:
            parts.append(
                '\n## TODOS (from Google Keep; the "Tasks" note under "primary" is the '
                'main focus, "other" notes are for a light skim)\n'
                f"{json.dumps(keep_todos, indent=2, default=str)}\n")

    parts.append(f"\n# CALENDAR DATA\n{json.dumps(calendar_data, indent=2, default=str)}\n")

    # Events/tickets scraped from up to ~12 months of email. Claude must extract
    # the actual EVENT date and keep only those within the next-week horizon,
    # deduping against the calendar above.
    if email_events:
        parts.append(
            f"\n# EVENTS/TICKETS FROM EMAIL (scanned ~{EVENT_EMAIL_LOOKBACK} of mail; tickets, travel, reservations)\n"
            "# Each item is a confirmation email. EXTRACT the actual EVENT / travel / reservation date from the "
            f"subject + snippet. USE ONLY items whose event date falls within TODAY..+{EVENT_HORIZON_DAYS} days. "
            "Discard anything past, far in the future, undated, cancelled, or merely promotional. DEDUP against "
            "CALENDAR DATA above — do not repeat events already on the calendar. Never invent a date; if you cannot "
            "tell the event date confidently, drop the item.\n"
            f"{json.dumps(email_events, indent=2)}\n")

    parts += [
        f"\n# INBOX ITEMS (since last briefing)\n{json.dumps(inbox_items, indent=2)}\n",
        "\n# NEWSLETTER CONTENT (plain text, already stripped)\n",
    ]
    for name, text in newsletters.items():
        parts.append(f"\n## {name}\n{text[:4000]}\n")
    parts.append("\n# WEB SEARCH NEWS RESULTS (use to fill gaps only; NOT a source of sports scores)\n")
    for topic, results in news_results.items():
        parts.append(f"\n## {topic}\n{json.dumps(results, indent=2)}\n")

    # Authoritative scores — the model must use these verbatim and never invent one.
    if espn_scores:
        parts.append(
            "\n# REAL SPORTS SCORES (AUTHORITATIVE — ESPN; yesterday→today)\n"
            "# This is the ONLY valid source for the scoreboard. 'state': post=final, in=live. Report each score "
            "EXACTLY as given (home vs away with their numeric scores). NEVER state a score or winner that is not "
            "in this data; if a game from the news isn't here, do NOT invent its result.\n"
            f"{json.dumps(espn_scores, indent=2, default=str)}\n")

    # Dedupe — what was already sent, so today surfaces only new developments.
    if recent_briefings:
        joined = "\n\n--- PRIOR BRIEFING ---\n".join(recent_briefings)
        parts.append(
            "\n# ALREADY REPORTED IN RECENT BRIEFINGS (do NOT repeat)\n"
            "# Your last briefings, verbatim text. Do NOT restate the same game scores, headlines, or storylines "
            "that already appear here — surface only NEW results and NEW news. Ongoing items (a playoff series, a "
            "tournament in progress) may be UPDATED with new developments but never repeated unchanged.\n"
            f"{joined}\n")

    p = PALETTE
    parts.append(f"""
# OUTPUT INSTRUCTIONS
Generate the complete HTML email body (no DOCTYPE/html/head — just the visible content, starting with a wrapper table).
First line must be a comment: <!-- SUBJECT: the subject line here -->

PALETTE — use ONLY these hex values (never pure #000000 or #ffffff anywhere):
  page background:   {p['page_bg']}
  card background:   {p['card_bg']}
  body text:         {p['text']}
  muted/meta:        {p['muted']}
  accent (primary):  {p['accent']}
  accent2 (pops):    {p['accent2']}
  chip/tint bg:      {p['chip_bg']}
  hairline border:   {p['border']}

LAYOUT: a single outer 100%-width wrapper <table> with bgcolor="{p['page_bg']}", containing a centered max-width 600px content <table> with bgcolor="{p['card_bg']}". Put an explicit bgcolor AND inline background-color on EVERY table/tr/td (including spacer/padding cells) — this is required so Gmail mobile dark mode inverts the email cleanly.

TYPE SCALE (inline font-size in px, explicit line-height in px, margin:0 on headings; Arial/Helvetica for ALL headings and body; Courier New ONLY for the tiny eyebrow label):
  Email title (H1):     28px / weight 800 / line-height 34px / color {p['text']}
  Section header (H2):  21px / weight 800 / line-height 27px / color {p['accent']}
  Subsection (H3):      17px / weight 700 / line-height 23px / color {p['text']}  (clearly readable and standing out — NOT muted, NOT Courier New, NOT tiny. Give it a distinctive, non-generic visual TREATMENT rather than plain bold text — see VISUAL POP. Keep the natural category title; just style it well.)
  Tiny eyebrow label:   11px / weight 700 / line-height 15px / letter-spacing 2px / uppercase / Courier New / color {p['accent2']}  (optional accent kicker ABOVE a section — never use this size for an actual heading)
  Body:                 15px / weight 400 / line-height 22px / color {p['text']}  (never below 14px)
  Small / meta:         12px / weight 400 / line-height 17px / color {p['muted']}

VISUAL POP — make it feel sleek and modern with small, tasteful accent touches (use sparingly so it stays clean and readable in both light and inverted-dark):
  - Give each H2 section header a short accent bar to its left: a small cell or left-border (3-4px) in {p['accent']} or {p['accent2']}.
  - Give H3 subsection headings a distinctive, non-generic treatment (not plain bold text), applied consistently across all subsections — e.g. a small {p['accent2']} leading marker (▸ or a filled square) before the title, or a short {p['accent2']} underline/bottom-border beneath it. The title text stays the natural category name.
  - Optionally place a tiny uppercase eyebrow label (the Courier style above, in {p['accent2']}) above major sections.
  - Use {p['accent']} for links and {p['accent2']} sparingly for secondary highlights (a key metric, a bullet marker, a divider).
  - Bullet markers can be a small colored "▸" or "•" in {p['accent']} instead of default list dots.
  - Separate major sections with a thin 1px divider in {p['border']}.
  - For a small standout stat or tag (e.g. a WHOOP recovery %), use a chip: padding ~2-6px, background {p['chip_bg']}, text in {p['accent']} (or {p['text']}), bold.
  - Keep it minimal — a few accents per section, not everywhere. Text must stay high-contrast and easy to read.

All CSS inline. No <style> block, no external resources, no web fonts, no JS. Comfortable medium contrast — must read well on Gmail mobile (dark mode, after inversion), Gmail web (light), and Apple Mail.
""")
    return "".join(parts)


def generate_html_briefing(user_prompt: str) -> tuple[str, str]:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    # Stream a large max_tokens: the inline-CSS table HTML is token-heavy, so an
    # 8K cap truncated the email mid-tag. claude-opus-4-8 allows up to 128K output
    # but the SDK requires streaming above ~16K (non-streaming would hit the
    # ~10-min HTTP timeout). 32K is ample headroom for a full briefing.
    with client.messages.stream(
        model="claude-opus-4-8",
        max_tokens=32000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "max_tokens":
        log.warning("Briefing hit max_tokens (32000) — output may be truncated.")
    html = message.content[0].text
    m = re.search(r'<!--\s*SUBJECT:\s*(.+?)\s*-->', html)
    subject = m.group(1).strip() if m else f"Gus's Morning Briefing - {datetime.utcnow().strftime('%B %-d, %Y')}"
    return subject, html


# ─────────────────────────────── Gmail sending ─────────────────────────────

def send_email(gmail_service, subject: str, html_body: str):
    page_bg = PALETTE["page_bg"]
    # No color-scheme meta: we do NOT want Gmail to negotiate a scheme (it ignores
    # it on mobile anyway). Instead we wrap the body in a real full-bleed background
    # table, since Gmail strips <body> styling and renders into its own div.
    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{subject}</title>
</head>
<body style="margin:0;padding:0;background-color:{page_bg};" bgcolor="{page_bg}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{page_bg}" style="background-color:{page_bg};">
<tr bgcolor="{page_bg}"><td bgcolor="{page_bg}" style="background-color:{page_bg};">
{html_body}
</td></tr>
</table>
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

    # FORCE_SEND=true (set only by a manual run with the force_send box checked)
    # bypasses the morning window and the once-per-day guard so you can test
    # any time. Scheduled runs never set it, so normal timing is unaffected.
    force = os.environ.get("FORCE_SEND", "").strip().lower() == "true"

    tz_str = infer_timezone(calendar_svc)
    in_window, local_now = is_briefing_window(tz_str)

    if not force and not in_window:
        log.info("Outside morning send window for %s (local %s). Exiting.", tz_str, local_now.strftime("%H:%M"))
        sys.exit(0)

    if not force and already_sent_today(gmail_svc, tz_str):
        log.info("Already sent today. Exiting.")
        sys.exit(0)

    if force:
        log.info("FORCE_SEND enabled — bypassing window/dedup for a test run.")

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
        # Athletics — pull ACTUAL scores/results across sports (Gus's teams + marquee).
        "Lacrosse":        "lacrosse PLL Premier Lacrosse League NCAA score result",
        "NBA Finals":      "NBA Finals score result last night",
        "NHL Stanley Cup": "NHL Stanley Cup Final score result last night",
        "MLB":             "MLB scores last night Red Sox Giants Athletics",
        "Golf":            "PGA Tour US Open golf leaderboard results today",
        "Soccer/WC":       "FIFA World Cup 2026 results scores yesterday",
        "Princeton":       "Princeton Tigers athletics score result",
        "Boston teams":    "Celtics Bruins Red Sox score last night",
        "Bay Area teams":  "Warriors Giants 49ers Sharks Athletics score last night",
    }
    news_results = {topic: web_search(q) for topic, q in news_queries.items()}

    # Opening-section data (both fail-soft → None on any error). WHOOP refreshes a
    # rotating token, so it runs here — past the window/dedup guards — i.e. only on
    # send-eligible runs, ~once/day. Keep is last (slow full sync, most fragile).
    whoop_data = fetch_whoop_data()
    keep_todos = fetch_keep_todos()

    # Scan up to ~12 months of email for tickets/travel/reservations dated in the
    # next week (fail-soft → []). Runs only on send-eligible runs, like the above.
    email_events = fetch_email_events(gmail_svc)

    # Authoritative scores (real, date-scoped) + last 2 briefings for dedupe.
    espn_scores      = fetch_espn_scores(local_now)
    recent_briefings = fetch_recent_briefings_text(gmail_svc, n=2)

    user_prompt   = build_user_prompt(calendar_data, inbox_items, newsletters,
                                      news_results, tz_str, local_now,
                                      whoop_data=whoop_data, keep_todos=keep_todos,
                                      email_events=email_events,
                                      espn_scores=espn_scores,
                                      recent_briefings=recent_briefings)
    subject, html = generate_html_briefing(user_prompt)
    send_email(gmail_svc, subject, html)
    log.info("Done.")


if __name__ == "__main__":
    main()
