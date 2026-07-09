#!/usr/bin/env python3
"""
Quarterly Electrical Industry Report Digest

Collects the industry research reports that middle-market investment banks
publish on the electrical industry (electrical contracting, distribution,
products, utility/T&D infrastructure), digests each with Claude, synthesizes
an analyst-style HTML email, and sends it via Gmail.

Sibling automation to briefing/generate_briefing.py — same Google OAuth
secrets, same Claude call pattern, same Gmail-dark-mode-safe email design.
State (which report editions were already digested) lives in a hidden
manifest comment inside the previously sent digest email, so no repo
commits or external storage are needed.

Run locally without secrets to test collection only:
    python digest/generate_digest.py --collect-only
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parsedate_to_datetime

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ──────────────────────────────── Config ───────────────────────────────────

# Recipients: start with Gus only; add Tom + boss later via the repo variable
# DIGEST_RECIPIENTS (comma-separated) — no code change needed.
DEFAULT_RECIPIENTS = ["gvonmetzsch@gmail.com"]
SENDER = "gvonmetzsch@gmail.com"

SUBJECT_PREFIX = "Electrical Industry Digest"

# HARDCODED quarterly send dates (month, day). The digest goes out on the
# first successful run ON or AFTER each date; the grace window lets the daily
# cron retry if GitHub drops ticks. These are fixed calendar dates — test
# sends and force_send runs never shift them.
#
# The dates sit ~2 months after each quarter closes because banks publish
# their quarterly reports 3-10 weeks after quarter end:
#   Mar 1 → Q4/H2 crop,  Jun 1 → Q1,  Sep 1 → Q2/H1,  Dec 1 → Q3.
# (To send right after quarter close instead, change to (1,x),(4,x),(7,x),(10,x)
#  — nothing is ever missed, but slow-publishing reports shift to the next
#  digest. Keep the cron in report_digest.yml in sync with these dates.)
DIGEST_SEND_DATES = [(3, 1), (6, 1), (9, 1), (12, 1)]
SEND_GRACE_DAYS = 20
# Don't re-send if a digest already went out within this window. Must cover
# the 21-day cron window; kept short of a full quarter so a mid-quarter
# force_send test doesn't suppress the next scheduled digest entirely.
DEDUP_WINDOW_DAYS = 25

# Skip editions published more than this many days ago (first-run sanity cap;
# afterwards the manifest handles dedup). Editions without a detectable date
# are kept and deduped by URL.
MAX_EDITION_AGE_DAYS = 270

# Cost guard: at most this many reports digested per run (newest first).
MAX_REPORTS_PER_RUN = 18

MODEL = "claude-sonnet-5"

# PDFs above these limits are text-extracted with pypdf instead of being sent
# to Claude as native documents (API request cap is 32MB post-base64; page cap
# is 600 for 1M-context models).
PDF_NATIVE_MAX_BYTES = 20 * 1024 * 1024
PDF_NATIVE_MAX_PAGES = 550
EXTRACTED_TEXT_MAX_CHARS = 200_000
HTML_REPORT_MAX_CHARS = 60_000

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Bank domains scanned in Gmail once Gus subscribes to their research lists
# (the delivery channel for form-gated reports: FMI Energy & Power Overview,
# BaseRock market updates). Harmless no-op until subscriptions exist.
BANK_EMAIL_DOMAINS = [
    "fmicorp.com", "cascade-partners.com", "baserockpartners.com",
    "capstonepartners.com", "pmcf.com", "sdrventures.com", "rlhulett.com",
]

# ─── EMAIL THEME — identical palette/system to briefing/generate_briefing.py ─
PALETTE = {
    "page_bg": "#eef0f4",
    "card_bg": "#f9fafc",
    "text":    "#33384a",
    "muted":   "#6b7280",
    "accent":  "#1664c0",
    "accent2": "#0e8aa8",
    "chip_bg": "#e6eef9",
    "border":  "#dde1e8",
}


# ──────────────────────────── HTTP helpers ─────────────────────────────────

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})


MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


def http_get(url: str, timeout: int = 40) -> requests.Response | None:
    """GET with browser UA; returns None on any failure (fail-soft).
    Streams with a hard byte cap so one misbehaving source can't balloon
    the runner's memory."""
    try:
        r = _session.get(url, timeout=timeout, allow_redirects=True, stream=True)
        if r.status_code != 200:
            log.info("GET %s -> %s", url, r.status_code)
            r.close()
            return None
        chunks, total = [], 0
        for chunk in r.iter_content(chunk_size=1 << 20):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                log.info("GET %s exceeded %dMB — aborting.", url, MAX_DOWNLOAD_BYTES >> 20)
                r.close()
                return None
        r._content = b"".join(chunks)  # make .content/.text/.json() work as usual
        return r
    except Exception as exc:
        log.info("GET %s failed: %s", url, exc)
    return None


def probe_pdf(url: str) -> tuple[bool, str | None]:
    """HEAD-probe a candidate PDF URL. Returns (exists, last_modified_iso).
    Requires an actual application/pdf content type — a 200 HTML page
    (bot-challenge, soft-404) does not count."""
    try:
        r = _session.head(url, timeout=20, allow_redirects=True)
        if r.status_code != 200:
            return False, None
        ctype = r.headers.get("Content-Type", "")
        if "pdf" not in ctype.lower():
            return False, None
        lm = r.headers.get("Last-Modified")
        if lm:
            try:
                return True, parsedate_to_datetime(lm).date().isoformat()
            except Exception:
                pass
        return True, None
    except Exception as exc:
        log.info("HEAD %s failed: %s", url, exc)
        return False, None


def strip_html(html: str) -> str:
    """Crude but adequate HTML → text for report pages."""
    html = re.sub(r"(?is)<(script|style|nav|header|footer|form)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def parse_date(s: str | None):
    """Parse an RFC-822 or ISO-ish date string → date, or None."""
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date()
    except Exception:
        pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            return None
    return None


def rss_items(url: str) -> list[dict]:
    """Fetch an RSS feed and return [{title, link, pubdate}] (fail-soft)."""
    r = http_get(url)
    if r is None:
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as exc:
        log.info("RSS parse failed for %s: %s", url, exc)
        return []
    items = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        # Strip feed tracking params so manifest keys stay stable.
        link = re.sub(r"\?utm_[^#]*$", "", link)
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": link,
            "pubdate": (item.findtext("pubDate") or "").strip(),
        })
    return items


# ───────────────────────────── Edition model ───────────────────────────────
# An "edition" is one report to digest:
# {
#   "key":       manifest key — stable unique ID for dedup (usually the PDF URL)
#   "bank":      publisher name
#   "series":    publication series name
#   "title":     edition title
#   "page_url":  human landing page (for the email link)
#   "pdf_url":   direct PDF (None for HTML-only reports)
#   "html_text": extracted text for HTML-only reports (None if pdf_url set)
#   "published": ISO date string or None
# }

def make_edition(key, bank, series, title, page_url=None, pdf_url=None,
                 html_text=None, published=None):
    return {"key": key, "bank": bank, "series": series, "title": title,
            "page_url": page_url or pdf_url, "pdf_url": pdf_url,
            "html_text": html_text, "published": published}


def fresh_enough(published: str | None) -> bool:
    d = parse_date(published)
    if d is None:
        return True
    return (datetime.now(timezone.utc).date() - d).days <= MAX_EDITION_AGE_DAYS


def infer_period_date(text: str) -> str | None:
    """Derive an approximate publication date from a 'Q2 2026' / 'q2-2026' /
    '1H 2026' label in a slug or title (reports publish around the end of
    their labeled period). Used when a source exposes no real date."""
    m = re.search(r"q([1-4])[-_ ]?(\d{4})", text, re.I)
    if m:
        month = int(m.group(1)) * 3
        return f"{m.group(2)}-{month:02d}-28"
    m = re.search(r"([12])h[-_ ]?(\d{4})", text, re.I)
    if m:
        return f"{m.group(2)}-{'06' if m.group(1) == '1' else '12'}-28"
    return None


# ───────────────────────────── Collectors ──────────────────────────────────
# One function per source. Every collector is fail-soft: any error returns []
# so a single broken site never blocks the digest. URLs and strategies come
# from a per-source verification pass (see digest/README.md).

def collect_cascade() -> list[dict]:
    """Cascade Partners — open WordPress media API lists every electrical
    report PDF with a direct ungated source_url (the Gravity-Forms gate on the
    landing pages never protects the files)."""
    eds = []
    url = ("https://cascade-partners.com/wp-json/wp/v2/media"
           "?search=electrical&per_page=20&_fields=date,source_url,title")
    r = http_get(url)
    if r is not None:
        try:
            for item in r.json():
                src = item.get("source_url", "")
                if not src.lower().endswith(".pdf"):
                    continue
                title = strip_html(item.get("title", {}).get("rendered", "")) or "Electrical report"
                eds.append(make_edition(
                    key=src, bank="Cascade Partners",
                    series="Electrical Contracting & Utility Infrastructure M&A Update",
                    title=title, pdf_url=src, published=item.get("date")))
        except Exception as exc:
            log.info("Cascade media API parse failed: %s", exc)
    # Second signal: the flagship landing page embeds the current edition's
    # PDF href and is sometimes updated in place rather than re-uploaded.
    r = http_get("https://cascade-partners.com/industry-report/"
                 "electrical-contracting-utility-infrastructure-ma-marketing-update/")
    if r is not None:
        for m in re.finditer(r'https://cascade-partners\.com/wp-content/uploads/[^"\']+\.pdf', r.text):
            pdf = m.group(0)
            if not any(e["key"] == pdf for e in eds):
                eds.append(make_edition(
                    key=pdf, bank="Cascade Partners",
                    series="Electrical Contracting & Utility Infrastructure M&A Update",
                    title=pdf.rsplit("/", 1)[-1].replace("-", " ").removesuffix(".pdf"),
                    page_url=r.url, pdf_url=pdf))
    return eds


def collect_fmi() -> list[dict]:
    """FMI — HTML pages are Cloudflare-403 to non-browsers, but PDFs under
    /uploads/media/ are open and the Outlook filenames are semi-predictable.
    Probe the last 3 quarters. (The form-gated Energy & Power Overview arrives
    via the Gmail channel once subscribed — see collect_gmail_scan.)"""
    eds = []
    today = datetime.now(timezone.utc).date()
    quarters = []
    y, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(3):
        quarters.append((q, y))
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    for q, y in quarters:
        if q == 1:
            candidates = [f"Overview_{y}_Final_Web.pdf", f"Overview_{y}_Final.pdf", f"Overview_{y}.pdf"]
            label = f"Q1 {y} Overview"
        else:
            candidates = [f"Q{q}_Outlook_{y}.pdf", f"Q{q}_Outlook_{y}_Final.pdf"]
            label = f"Q{q} {y} Outlook"
        for name in candidates:
            url = f"https://fmicorp.com/uploads/media/{name}"
            ok, lm = probe_pdf(url)
            if ok:
                eds.append(make_edition(
                    key=url, bank="FMI",
                    series="North American Engineering & Construction Outlook",
                    title=f"NA Engineering & Construction {label}",
                    page_url="https://fmicorp.com/insights/construction-outlook",
                    pdf_url=url, published=lm))
                break
    return eds


def collect_baird() -> list[dict]:
    """Baird — Electrical Power Solutions sector update; fully predictable
    ungated PDF URL keyed on the lowercase month name. Probe last 8 months."""
    eds = []
    d = datetime.now(timezone.utc).date().replace(day=1)
    for _ in range(8):
        month = d.strftime("%B").lower()
        url = (f"https://www.rwbaird.com/siteassets/gib/pdf/"
               f"electrical-power-solutions-sector-update-{month}-{d.year}.pdf")
        ok, lm = probe_pdf(url)
        if ok:
            eds.append(make_edition(
                key=url, bank="Robert W. Baird",
                series="Electrical Power Solutions Sector Update",
                title=f"Electrical Power Solutions — {month.title()} {d.year}",
                page_url="https://www.rwbaird.com/corporations-and-institutions/"
                         "investment-banking/industries-of-focus/industrial/electrical-power-solutions/",
                pdf_url=url, published=lm or d.isoformat()))
        d = (d - timedelta(days=1)).replace(day=1)
    return eds


def collect_harris_williams() -> list[dict]:
    """Harris Williams — scrape the server-rendered EPI industry index for
    /our-insights/epi-* links; each edition page embeds an ungated
    assets.ctfassets.net PDF."""
    eds = []
    idx = http_get("https://www.harriswilliams.com/our-industries/energy-power-and-infrastructure")
    if idx is None:
        return eds
    # hrefs come HTML-escaped (m&amp;a) — unescape before fetching.
    links = {p.replace("&amp;", "&") for p in
             re.findall(r'href="(/our-insights/epi-[^"]+)"', idx.text)}
    # The quarterly Utility Services & Power Products brief is the priority;
    # other epi-* links are ad-hoc deep dives — keep only electrical-relevant
    # ones (the EPI group also posts environmental-services etc. content).
    RELEVANT = re.compile(r"(utility|power|electric|transformer|t-and-d|renewabl|grid|outlook)", re.I)
    for path in sorted(links):
        if not RELEVANT.search(path):
            continue
        page_url = "https://www.harriswilliams.com" + path
        r = http_get(page_url)
        if r is None:
            continue
        m = re.search(r'https://assets\.ctfassets\.net/[^"\'\s]+\.pdf', r.text)
        if not m:
            continue
        pdf = m.group(0)
        title = path.rsplit("/", 1)[-1].replace("-", " ").replace("m&a", "M&A").title()
        eds.append(make_edition(
            key=pdf, bank="Harris Williams",
            series="Energy, Power & Infrastructure insights",
            title=title, page_url=page_url, pdf_url=pdf))
    return eds


def collect_houlihan() -> list[dict]:
    """Houlihan Lokey — index pages are behind Imperva, but cdn.hl.com serves
    PDFs openly and recent slugs are probeable. A quarter's PDF may sit under
    the publication year's path (e.g. /pdf/2026/...q4-2025.pdf)."""
    eds = []
    today = datetime.now(timezone.utc).date()
    quarters = []
    y, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(4):
        quarters.append((q, y))
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    series = [
        ("power-utilities-renewables-market-update", "Power, Utilities & Renewables Market Snapshot"),
        ("engineering-industrial-infrastructure-market-update", "Engineering, Industrial & Infrastructure Services Update"),
    ]
    for slug, name in series:
        found = False
        for q, y in quarters:
            if found:
                break
            for path_year in (y, y + 1):
                url = f"https://cdn.hl.com/pdf/{path_year}/{slug}-q{q}-{y}.pdf"
                ok, lm = probe_pdf(url)
                if ok:
                    eds.append(make_edition(
                        key=url, bank="Houlihan Lokey", series=name,
                        title=f"{name} — Q{q} {y}",
                        page_url="https://hl.com/insights/", pdf_url=url, published=lm))
                    found = True
                    break
    return eds


def collect_rlhulett() -> list[dict]:
    """R.L. Hulett — custom-post-type RSS feed lists every quarterly update;
    edition pages carry an ungated Download PDF href."""
    eds = []
    for item in rss_items("https://rlhulett.com/feed/?post_type=quarterly-updates"):
        if not re.search(r"(MEP Services|Industrial Services) M&A Update", item["title"], re.I):
            continue
        if not fresh_enough(item["pubdate"]):
            continue
        r = http_get(item["link"])
        if r is None:
            continue
        m = re.search(r'https://rlhulett\.com/app/uploads/[^"\']+\.pdf', r.text)
        if not m:
            continue
        eds.append(make_edition(
            key=m.group(0), bank="R.L. Hulett", series="Quarterly M&A Updates",
            title=item["title"], page_url=item["link"], pdf_url=m.group(0),
            published=item["pubdate"]))
    return eds


def collect_lincoln() -> list[dict]:
    """Lincoln International — publications RSS; the /publications/ item links
    serve the PDF bytes directly. Facilities Services is the active electrical-
    relevant quarterly; Distribution Quarterly Review is watched for revival.
    The feed only shows recent posts, so also probe the predictable
    facilities-services slug for the last few quarters."""
    eds = []
    for item in rss_items("https://www.lincolninternational.com/category/publications/feed/"):
        if not re.search(r"(Facilities Services Market Update|Distribution Quarterly Review)",
                         item["title"], re.I):
            continue
        if not fresh_enough(item["pubdate"]):
            continue
        eds.append(make_edition(
            key=item["link"], bank="Lincoln International", series="Publications",
            title=item["title"], page_url=item["link"], pdf_url=item["link"],
            published=item["pubdate"]))
    today = datetime.now(timezone.utc).date()
    y, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(4):
        url = (f"https://www.lincolninternational.com/publications/"
               f"facilities-services-market-update-q{q}-{y}/")
        if not any(e["key"] == url for e in eds):
            ok, lm = probe_pdf(url)
            if ok:
                eds.append(make_edition(
                    key=url, bank="Lincoln International",
                    series="Facilities Services Market Update",
                    title=f"Facilities Services Market Update Q{q} {y}",
                    page_url=url, pdf_url=url, published=lm))
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return eds


def collect_pmcf() -> list[dict]:
    """PMCF — scrape the industry-updates index for Pulse edition pages, then
    extract the ungated /download/...pdf href from each."""
    eds = []
    idx = http_get("https://pmcf.com/industry-updates/")
    if idx is None:
        return eds
    slugs = set(re.findall(
        r'https://pmcf\.com/industry-updates/((?:distribution|construction-building-products)'
        r'-ma-pulse-q\d-\d{4})/', idx.text))
    for slug in sorted(slugs, reverse=True)[:4]:
        page_url = f"https://pmcf.com/industry-updates/{slug}/"
        r = http_get(page_url)
        if r is None:
            continue
        m = re.search(r'https://pmcf\.com/download/[^"\'\s]+\.pdf', r.text)
        if not m:
            continue
        title = slug.replace("-", " ").title().replace(" Ma Pulse", " M&A Pulse")
        eds.append(make_edition(
            key=m.group(0), bank="PMCF (P&M Corporate Finance)", series="M&A Pulse",
            title=title, page_url=page_url, pdf_url=m.group(0)))
    return eds


def collect_pce() -> list[dict]:
    """PCE — open HTML reports on canonical URLs that are overwritten each
    quarter; the RSS feed's per-item pubDate marks each refresh. The manifest
    key embeds the pubDate because the URL never changes."""
    eds = []
    wanted = {
        "power-energy-ma-update": "Power & Energy M&A Update",
        "diversified-industrials-ma-update": "Diversified Industrials M&A Update",
    }
    for item in rss_items("https://www.pcecompanies.com/resources/industry-reports/rss.xml"):
        slug = item["link"].rstrip("/").rsplit("/", 1)[-1]
        if slug not in wanted:
            continue
        if not fresh_enough(item["pubdate"]):
            continue
        d = parse_date(item["pubdate"])
        r = http_get(item["link"])
        if r is None:
            continue
        eds.append(make_edition(
            key=f"{item['link']}#{d.isoformat() if d else 'undated'}",
            bank="PCE Investment Bankers", series=wanted[slug],
            title=f"{wanted[slug]} ({d.isoformat() if d else 'latest'})",
            page_url=item["link"],
            html_text=strip_html(r.text)[:HTML_REPORT_MAX_CHARS],
            published=item["pubdate"]))
    return eds


def collect_capstone() -> list[dict]:
    """Capstone Partners — category RSS announces new editions; report
    editions expose an ungated wp-content PDF, article editions are web-only
    full text."""
    eds = []
    feed = ("https://www.capstonepartners.com/insights/industries/"
            "building-products-and-construction-services/feed/")
    for item in rss_items(feed):
        if not re.search(r"(Construction Services|AEC Services)", item["title"], re.I):
            continue
        if not fresh_enough(item["pubdate"]):
            continue
        r = http_get(item["link"])
        if r is None:
            continue
        # Their report PDFs are all named Capstone-Partners-*; the page also
        # carries unrelated image assets with .pdf inside srcset blobs.
        m = re.search(r'https://www\.capstonepartners\.com/wp-content/uploads/'
                      r'[^"\'\s]*Capstone[^"\'\s]*\.pdf', r.text)
        if m:
            eds.append(make_edition(
                key=m.group(0), bank="Capstone Partners", series="Construction / AEC Services",
                title=item["title"], page_url=item["link"], pdf_url=m.group(0),
                published=item["pubdate"]))
        else:
            d = parse_date(item["pubdate"])
            eds.append(make_edition(
                key=f"{item['link']}#{d.isoformat() if d else 'undated'}",
                bank="Capstone Partners", series="Construction / AEC Services",
                title=item["title"], page_url=item["link"],
                html_text=strip_html(r.text)[:HTML_REPORT_MAX_CHARS],
                published=item["pubdate"]))
    return eds


def collect_anchor_peabody() -> list[dict]:
    """Anchor Peabody — open WP REST API, category 15 = HVAC/Plumbing/
    Electrical. Recent editions link straight to a PDF; older ones are HTML."""
    eds = []
    r = http_get("https://anchorpeabody.com/wp-json/wp/v2/posts?categories=15&per_page=5")
    if r is None:
        return eds
    try:
        posts = r.json()
    except Exception:
        return eds
    for post in posts:
        link = post.get("link", "")
        title = strip_html(post.get("title", {}).get("rendered", "")) or "HPE Services Market Report"
        date = post.get("date")
        if not fresh_enough(date):
            continue
        if link.lower().endswith(".pdf"):
            eds.append(make_edition(
                key=link, bank="Anchor Peabody",
                series="HVAC, Plumbing & Electrical Services Market Report",
                title=title, pdf_url=link, published=date))
        else:
            page = http_get(link)
            if page is None:
                continue
            m = re.search(r'https://anchorpeabody\.com/wp-content/uploads/[^"\']+\.pdf', page.text)
            if m:
                eds.append(make_edition(
                    key=m.group(0), bank="Anchor Peabody",
                    series="HVAC, Plumbing & Electrical Services Market Report",
                    title=title, page_url=link, pdf_url=m.group(0), published=date))
            else:
                eds.append(make_edition(
                    key=link, bank="Anchor Peabody",
                    series="HVAC, Plumbing & Electrical Services Market Report",
                    title=title, page_url=link,
                    html_text=strip_html(page.text)[:HTML_REPORT_MAX_CHARS],
                    published=date))
    return eds


def collect_sdr() -> list[dict]:
    """SDR Ventures — static report hub; edition pages embed a public Sanity
    CDN PDF. (These run ~26MB, so they take the pypdf text-extraction path.)"""
    eds = []
    idx = http_get("https://reports.sdrventures.com/")
    if idx is None:
        return eds
    slugs = set(re.findall(r'href="(/reports/[^"]*industrial[^"]*infrastructure[^"]*)"', idx.text))
    for slug in sorted(slugs, reverse=True)[:2]:
        page_url = "https://reports.sdrventures.com" + slug
        r = http_get(page_url)
        if r is None:
            continue
        m = re.search(r'https://cdn\.sanity\.io/files/[^"\']+\.pdf', r.text)
        if not m:
            continue
        eds.append(make_edition(
            key=m.group(0), bank="SDR Ventures",
            series="Industrial & Infrastructure Services Report",
            title=slug.rsplit("/", 1)[-1].replace("-", " ").title(),
            page_url=page_url, pdf_url=m.group(0)))
    return eds


def collect_baserock() -> list[dict]:
    """BaseRock Partners — annual electrical/utility services updates. The PDF
    is HubSpot-gated (delivered by email once subscribed), but each edition has
    an ungated summary post announced on the RSS feed; digest that."""
    eds = []
    for item in rss_items("https://baserockpartners.com/feed/"):
        if not re.search(r"(electrical|utility)[- ]services[- ]market[- ]update", item["link"], re.I):
            continue
        if not fresh_enough(item["pubdate"]):
            continue
        r = http_get(item["link"])
        if r is None:
            continue
        eds.append(make_edition(
            key=item["link"], bank="BaseRock Partners",
            series="Electrical / Utility Services Market Update (summary)",
            title=item["title"], page_url=item["link"],
            html_text=strip_html(r.text)[:HTML_REPORT_MAX_CHARS],
            published=item["pubdate"]))
    return eds


def collect_northeastern() -> list[dict]:
    """Northeastern Advisors — annual, ungated HTML report on a predictable
    year-keyed URL (typically drops late January)."""
    eds = []
    year = datetime.now(timezone.utc).year
    for y in (year, year - 1):
        url = f"https://northeasternadvisors.com/{y}-u-s-electrical-contracting-industry-report/"
        r = http_get(url)
        if r is None:
            continue
        eds.append(make_edition(
            key=url, bank="Northeastern Advisors",
            series="U.S. Electrical Contracting Industry Report",
            title=f"{y} U.S. Electrical Contracting Industry Report",
            page_url=url, html_text=strip_html(r.text)[:HTML_REPORT_MAX_CHARS],
            published=f"{y}-01-28"))
        break  # only the newest available year
    return eds


def collect_nema() -> list[dict]:
    """NEMA EBCI — monthly electroindustry confidence index; the landing page
    carries a rolling window of ungated PDF hrefs. Take the newest 3 months as
    market-conditions context."""
    eds = []
    r = http_get("https://www.makeitelectric.org/industry-impact/"
                 "nema-business-intelligence/electroindustry-business-confidence-index/")
    if r is None:
        return eds
    pdfs = re.findall(r'https://www\.makeitelectric\.org/wp-content/uploads/Documents/EBCI/[^"\'\s]+\.pdf', r.text)
    # Filenames mix hyphen/underscore conventions, so sort by the embedded
    # YYYYMM, not by raw URL string.
    dated = []
    for url in dict.fromkeys(pdfs):
        m = re.search(r"(\d{4})(\d{2})", url)
        if m:
            dated.append((m.group(1) + m.group(2), url))
    for yyyymm, url in sorted(dated, reverse=True)[:3]:
        label = f"{yyyymm[:4]}-{yyyymm[4:]}"
        eds.append(make_edition(
            key=url, bank="NEMA", series="Electroindustry Business Confidence Index (EBCI)",
            title=f"EBCI {label}", page_url=r.url, pdf_url=url,
            published=f"{label}-01"))
    return eds


def collect_gmail_scan(gmail_service) -> list[dict]:
    """Scan Gmail for research emails from subscribed bank domains and pull
    any PDF links out of them. Inactive (returns nothing) until Gus subscribes
    to the banks' distribution lists; then it becomes the channel for the
    form-gated reports."""
    eds = []
    if gmail_service is None:
        return eds
    try:
        query = "(" + " OR ".join(f"from:{d}" for d in BANK_EMAIL_DOMAINS) + ") newer_than:120d"
        result = gmail_service.users().messages().list(userId="me", q=query, maxResults=20).execute()
        for m in result.get("messages", []):
            detail = gmail_service.users().messages().get(userId="me", id=m["id"], format="full").execute()
            headers = {h["name"].lower(): h["value"]
                       for h in detail.get("payload", {}).get("headers", [])}
            subject = headers.get("subject", "Bank research email")
            sender = headers.get("from", "")
            body = _walk_message_text(detail.get("payload", {}))
            for pdf in set(re.findall(r'https?://[^\s"<>\)\]]+\.pdf', body)):
                eds.append(make_edition(
                    key=pdf, bank=sender.split("<")[0].strip() or "Bank email",
                    series="Received by email", title=subject, pdf_url=pdf))
    except Exception as exc:
        log.warning("Gmail scan failed (non-fatal): %s", exc)
    return eds


def _walk_message_text(payload: dict) -> str:
    """Concatenate all decoded text parts of a Gmail message payload."""
    chunks = []
    def walk(part):
        data = part.get("body", {}).get("data")
        if data and part.get("mimeType", "").startswith("text/"):
            try:
                # "==" pad: Gmail returns unpadded base64url (same fix as the
                # briefing script; extra padding is ignored by the decoder).
                chunks.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace"))
            except Exception:
                pass
        for sub in part.get("parts", []) or []:
            walk(sub)
    walk(payload)
    return "\n".join(chunks)


COLLECTORS = [
    ("Cascade Partners", collect_cascade),
    ("FMI", collect_fmi),
    ("Baird", collect_baird),
    ("Harris Williams", collect_harris_williams),
    ("Houlihan Lokey", collect_houlihan),
    ("R.L. Hulett", collect_rlhulett),
    ("Lincoln International", collect_lincoln),
    ("PMCF", collect_pmcf),
    ("PCE", collect_pce),
    ("Capstone Partners", collect_capstone),
    ("Anchor Peabody", collect_anchor_peabody),
    ("SDR Ventures", collect_sdr),
    ("BaseRock Partners", collect_baserock),
    ("Northeastern Advisors", collect_northeastern),
    ("NEMA EBCI", collect_nema),
]


def collect_all(gmail_service=None) -> list[dict]:
    editions = []
    for name, fn in COLLECTORS:
        try:
            found = fn()
        except Exception as exc:
            log.warning("Collector %s crashed (non-fatal): %s", name, exc)
            found = []
        log.info("Collector %-24s -> %d edition(s)", name, len(found))
        editions.extend(found)
    editions.extend(collect_gmail_scan(gmail_service))
    # De-dup by manifest key (a PDF can surface via two signals).
    seen, unique = set(), []
    for ed in editions:
        if ed["key"] in seen:
            continue
        seen.add(ed["key"])
        if ed["published"] is None:
            ed["published"] = infer_period_date(f"{ed['key']} {ed['title']}")
        if fresh_enough(ed["published"]):
            unique.append(ed)
    return unique


# ──────────────────────────── Google / Gmail ────────────────────────────────

def build_google_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleRequest
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


def _subject_is_digest(detail: dict) -> bool:
    """True only for a digest the script generated — a forwarded or replied
    copy gets a 'Fwd: '/'Re: ' prefix and must not count."""
    for h in detail.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == "subject":
            return h.get("value", "").startswith(SUBJECT_PREFIX)
    return False


def already_sent_recently(gmail_service) -> bool:
    """Whether a digest went out within the dedup window (one send/quarter)."""
    coarse = (datetime.now(timezone.utc) - timedelta(days=DEDUP_WINDOW_DAYS)).strftime("%Y/%m/%d")
    result = gmail_service.users().messages().list(
        userId="me",
        q=f'from:me subject:"{SUBJECT_PREFIX}" after:{coarse}',
        maxResults=5,
    ).execute()
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUP_WINDOW_DAYS)
    for m in result.get("messages", []):
        detail = gmail_service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["Subject"]).execute()
        if not _subject_is_digest(detail):
            continue
        sent = datetime.fromtimestamp(int(detail.get("internalDate", "0")) / 1000, tz=timezone.utc)
        if sent >= cutoff:
            return True
    return False


def fetch_last_manifest(gmail_service) -> set[str]:
    """Read the previously digested edition keys from the hidden manifest
    comment in the most recent digest email (state lives in the Sent folder)."""
    try:
        result = gmail_service.users().messages().list(
            userId="me", q=f'from:me subject:"{SUBJECT_PREFIX}"', maxResults=5).execute()
        for m in result.get("messages", []):
            detail = gmail_service.users().messages().get(
                userId="me", id=m["id"], format="full").execute()
            if not _subject_is_digest(detail):
                continue  # a forward/reply could carry a STALE manifest
            body = _walk_message_text(detail.get("payload", {}))
            match = re.search(r"DIGEST-MANIFEST-B64:\s*([A-Za-z0-9+/=]+)", body)
            if match:
                keys = json.loads(base64.b64decode(match.group(1)).decode())
                log.info("Loaded manifest with %d previously digested editions.", len(keys))
                return set(keys)
    except Exception as exc:
        log.warning("Manifest fetch failed (treating all editions as new): %s", exc)
    return set()


def send_email(gmail_service, subject: str, html_body: str, recipients: list[str]):
    page_bg = PALETTE["page_bg"]
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
    msg["From"] = SENDER
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(full_html, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()
        log.info("Digest sent: %s -> %s", subject, ", ".join(recipients))
    except Exception as exc:
        log.warning("Send failed (%s) — attempting draft fallback.", exc)
        try:
            # NOTE: drafts.create needs a compose/modify scope the token may
            # not carry (it has gmail.send) — treat this as best-effort only.
            gmail_service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
            log.info("Digest saved as draft.")
        except Exception as exc2:
            log.error("Draft fallback also failed (%s). Digest NOT delivered this run; "
                      "the next cron tick will retry from scratch.", exc2)


# ─────────────────────────── Claude digestion ───────────────────────────────

REPORT_PROMPT = """You are an equity/M&A research analyst covering the ELECTRICAL industry \
(electrical contracting & services, electrical distribution, electrical products/equipment, \
utility & T&D infrastructure, electrification/data-center power demand).

Summarize the attached report for a quarterly internal digest read by investment professionals. \
Report metadata:
- Publisher: {bank}
- Series: {series}
- Edition: {title}

Write a tight, factual summary with EXACTLY these labeled sections (plain text, no markdown headers):
TAKEAWAYS: 3-5 bullet points of the most decision-relevant findings.
DATA: the hardest numbers in the report (valuation multiples, deal counts, indices, growth rates, backlog figures), each attributed clearly. If the report contains none, write "None reported."
DEALS: notable M&A transactions named in the report (target/acquirer, sector, multiple if given). If none, "None reported."
OUTLOOK: 1-3 sentences on the publisher's forward view.
RELEVANCE: one sentence on how directly this bears on the electrical industry (some reports cover adjacent sectors).

Be specific: names, numbers, multiples. Never invent a figure not in the report. Keep the whole summary under 350 words."""

DIGEST_SYSTEM_PROMPT = """You are the editor of a quarterly "Electrical Industry Digest" email for investment \
professionals at a firm active in the electrical industry. You receive per-report analyst summaries of the \
industry research that middle-market investment banks published this quarter, and you produce ONE polished \
HTML email.

Structure (in order):
1. EXECUTIVE SYNTHESIS — the lead section. 2-4 paragraphs that read ACROSS the reports: the state of \
electrical-industry M&A and market conditions this quarter, where valuations sit, the themes multiple banks \
agree on (and any disagreements), and what changed vs. the prior quarter if evident. This is the part the \
readers will actually remember; make it sharp and specific.
2. BY THE NUMBERS — a compact list of the most important cross-report data points (multiples, deal counts, \
indices), each attributed to its source bank.
3. REPORT-BY-REPORT — one subsection per report: bank + report title (hyperlinked to the report URL given), \
then the takeaways/data/deals condensed to their best material. Order reports by relevance to the electrical \
industry (core electrical M&A first, adjacent sector reports later, market indices last). Keep each tight.
4. SOURCES — a one-line-per-report list: bank, report title, link.

Rules:
- Use ONLY facts from the summaries provided. Never invent a number, deal, or claim. Attribute figures to banks.
- Hyperlink report titles using the exact URLs provided; never construct or guess a URL.
- FORMATTING: output ONLY the HTML email body (no DOCTYPE/html/head; just the visible content starting with a wrapper table).
  - Table-based layout only (nested <table>; no <div> for structure). ALL CSS inline. No <style> block, no JS, no external resources, no web fonts. Arial/Helvetica for everything, Courier New ONLY for small uppercase section labels.
  - CRITICAL for Gmail mobile dark mode (which partially color-INVERTS the email): put an explicit bgcolor="..." attribute AND an inline background-color on EVERY <table>, <tr>, and <td> — including spacer/padding cells and the outer wrapper. No cell may be left without a background. NEVER use pure #000000 or #ffffff for any background or text. Use ONLY the exact palette hex values given in the user message.
  - Set explicit inline font-size (px), font-weight, line-height (px), and color on EVERY text element; margin:0 on heading tags. Use the exact type scale given in the user message.
- LENGTH: 1200-3000 words of visible text depending on how many reports there are. Substantive, not padded.
- VOICE — write like a sharp sell-side research editor, NOT like an AI assistant:
  - NEVER use em dashes (—) or en dashes (–). Use commas, periods, colons, or parentheses instead.
  - No AI-tell phrasing: no "delve", "it's worth noting", "underscores", "boasts", "a testament to", "navigating the landscape", no reflexive rule-of-three lists, no hedging throat-clearing.
  - Concrete and specific (names, numbers, multiples) over vague and smooth. Confident, direct, professional."""


def _first_text_block(message) -> str:
    return next((b.text for b in message.content if b.type == "text"), "")


def summarize_report(client, ed: dict, usage_tally: dict) -> str | None:
    """One Claude call per report. PDFs go in as native document blocks;
    oversized PDFs and HTML reports go in as extracted text."""
    prompt = REPORT_PROMPT.format(bank=ed["bank"], series=ed["series"], title=ed["title"])
    content: list[dict] = []

    if ed.get("pdf_url"):
        r = http_get(ed["pdf_url"], timeout=120)
        if r is None or not r.content:
            log.warning("Could not download %s — skipping.", ed["pdf_url"])
            return None
        pdf_bytes = r.content
        if not pdf_bytes.startswith(b"%PDF"):
            # A .pdf URL that now serves HTML (consent page, soft-404, bot wall).
            log.warning("%s did not return a real PDF (magic bytes: %r) — skipping.",
                        ed["pdf_url"], pdf_bytes[:8])
            return None
        pages = _pdf_page_count(pdf_bytes)
        if len(pdf_bytes) <= PDF_NATIVE_MAX_BYTES and (pages is None or pages <= PDF_NATIVE_MAX_PAGES):
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(pdf_bytes).decode(),
                },
            })
            content.append({"type": "text", "text": prompt})
        else:
            log.info("%s is large (%.1fMB / %s pages) — extracting text instead.",
                     ed["title"], len(pdf_bytes) / 1e6, pages)
            text = _pdf_extract_text(pdf_bytes)
            if not text:
                log.warning("Text extraction failed for %s — skipping.", ed["title"])
                return None
            content.append({"type": "text",
                            "text": f"{prompt}\n\nREPORT TEXT (extracted from PDF):\n{text}"})
    elif ed.get("html_text"):
        content.append({"type": "text",
                        "text": f"{prompt}\n\nREPORT TEXT (extracted from web page):\n{ed['html_text']}"})
    else:
        return None

    try:
        # Sonnet 5 runs adaptive thinking by default when `thinking` is omitted;
        # that helps on dense report analysis. max_tokens covers thinking + text,
        # so leave generous headroom or a long thinking phase truncates the answer.
        message = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        log.warning("Claude summarization failed for %s: %s", ed["title"], exc)
        return None
    usage_tally["in"] += message.usage.input_tokens
    usage_tally["out"] += message.usage.output_tokens
    if message.stop_reason == "max_tokens":
        log.warning("Summary for %s hit max_tokens — may be truncated.", ed["title"])
    text = _first_text_block(message).strip()
    if not text:
        log.warning("No text in Claude response for %s (stop_reason=%s) — skipping.",
                    ed["title"], message.stop_reason)
        return None
    return text


def _pdf_page_count(pdf_bytes: bytes) -> int | None:
    try:
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception:
        return None


def _pdf_extract_text(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        chunks = []
        total = 0
        for page in reader.pages:
            t = page.extract_text() or ""
            chunks.append(t)
            total += len(t)
            if total > EXTRACTED_TEXT_MAX_CHARS:
                break
        return "\n".join(chunks)[:EXTRACTED_TEXT_MAX_CHARS]
    except Exception as exc:
        log.warning("pypdf extraction failed: %s", exc)
        return ""


def in_send_window(now_utc: datetime) -> bool:
    """True when today falls on or within SEND_GRACE_DAYS after one of the
    hardcoded DIGEST_SEND_DATES."""
    today = now_utc.date()
    for month, day in DIGEST_SEND_DATES:
        target = date(today.year, month, day)
        if 0 <= (today - target).days <= SEND_GRACE_DAYS:
            return True
    return False


def quarter_label(now_utc: datetime) -> str:
    """The digest covers the PREVIOUS calendar quarter's report crop."""
    q = (now_utc.month - 1) // 3 + 1
    if q == 1:
        return f"Q4 {now_utc.year - 1}"
    return f"Q{q - 1} {now_utc.year}"


def build_synthesis_prompt(summaries: list[dict], label: str) -> str:
    p = PALETTE
    parts = [
        f"# SUBJECT LINE\n{SUBJECT_PREFIX} - {label}\n",
        f"\n# REPORT SUMMARIES ({len(summaries)} reports collected this quarter)\n",
    ]
    for i, s in enumerate(summaries, 1):
        parts.append(
            f"\n## REPORT {i}\n"
            f"Bank: {s['bank']}\n"
            f"Report: {s['title']}\n"
            f"Series: {s['series']}\n"
            f"URL: {s['page_url']}\n"
            f"Published: {s['published'] or 'unknown'}\n"
            f"ANALYST SUMMARY:\n{s['summary']}\n")
    parts.append(f"""
# OUTPUT INSTRUCTIONS
Generate the complete HTML email body (no DOCTYPE/html/head — just the visible content, starting with a wrapper table).
First line must be a comment: <!-- SUBJECT: {SUBJECT_PREFIX} - {label} -->

PALETTE — use ONLY these hex values (never pure #000000 or #ffffff anywhere):
  page background:   {p['page_bg']}
  card background:   {p['card_bg']}
  body text:         {p['text']}
  muted/meta:        {p['muted']}
  accent (primary):  {p['accent']}
  accent2 (pops):    {p['accent2']}
  chip/tint bg:      {p['chip_bg']}
  hairline border:   {p['border']}

LAYOUT: a single outer 100%-width wrapper <table> with bgcolor="{p['page_bg']}", containing a centered max-width 640px content <table> with bgcolor="{p['card_bg']}". Explicit bgcolor AND inline background-color on EVERY table/tr/td (including spacer cells).

TYPE SCALE (inline font-size px, explicit line-height px, margin:0 on headings; Arial/Helvetica everywhere; Courier New only for the tiny eyebrow label):
  Email title (H1):     26px / weight 800 / line-height 32px / color {p['text']}
  Section header (H2):  20px / weight 800 / line-height 26px / color {p['accent']}  (with a 3-4px left accent bar in {p['accent']})
  Subsection (H3):      16px / weight 700 / line-height 22px / color {p['text']}  (bank + report name; give it a small {p['accent2']} leading marker)
  Tiny eyebrow label:   11px / weight 700 / line-height 15px / letter-spacing 2px / uppercase / Courier New / color {p['accent2']}
  Body:                 15px / weight 400 / line-height 22px / color {p['text']}
  Small / meta:         12px / weight 400 / line-height 17px / color {p['muted']}
Links in {p['accent']}. Divider lines 1px in {p['border']}. Key stats may use a chip: padding 2-6px, background {p['chip_bg']}, bold {p['accent']} text.
""")
    return "".join(parts)


def generate_digest_html(client, user_prompt: str, label: str, usage_tally: dict) -> tuple[str, str]:
    # Thinking disabled for the HTML-writing pass, matching the proven daily
    # briefing behavior (fuller, direct writing; no thinking spend).
    with client.messages.stream(
        model=MODEL,
        max_tokens=32000,
        thinking={"type": "disabled"},
        system=DIGEST_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "max_tokens":
        log.warning("Digest hit max_tokens (32000) — output may be truncated.")
    usage_tally["in"] += message.usage.input_tokens
    usage_tally["out"] += message.usage.output_tokens
    html = _first_text_block(message)
    m = re.search(r"<!--\s*SUBJECT:\s*(.+?)\s*-->", html)
    subject = m.group(1).strip() if m else f"{SUBJECT_PREFIX} - {label}"
    return subject, html


def embed_manifest(html: str, keys: list[str]) -> str:
    """Append the state manifest as a hidden comment. Base64 so URLs with
    '--' can never break the HTML comment."""
    payload = base64.b64encode(json.dumps(sorted(keys)).encode()).decode()
    return html + f"\n<!-- DIGEST-MANIFEST-B64: {payload} -->\n"


# ──────────────────────────────────── Main ─────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect-only", action="store_true",
                        help="Run collectors and print what was found; no secrets needed.")
    args = parser.parse_args()

    if args.collect_only:
        editions = collect_all(gmail_service=None)
        print(f"\n{len(editions)} edition(s) found:\n")
        for ed in sorted(editions, key=lambda e: (e["bank"], e["title"])):
            kind = "PDF " if ed["pdf_url"] else "HTML"
            print(f"  [{kind}] {ed['bank']:28s} | {ed['title'][:70]:70s} | {ed['published'] or '?'}")
            print(f"         {ed['pdf_url'] or ed['page_url']}")
        return

    log.info("Starting electrical industry digest job...")
    import anthropic
    from googleapiclient.discovery import build

    creds = build_google_credentials()
    gmail_svc = build("gmail", "v1", credentials=creds)

    force = os.environ.get("FORCE_SEND", "").strip().lower() == "true"
    now = datetime.now(timezone.utc)

    if not force and not in_send_window(now):
        log.info("Outside the hardcoded quarterly send windows (%s + %d-day grace). Exiting.",
                 ", ".join(f"{m}/{d}" for m, d in DIGEST_SEND_DATES), SEND_GRACE_DAYS)
        sys.exit(0)

    if not force and already_sent_recently(gmail_svc):
        log.info("Digest already sent this quarter. Exiting.")
        sys.exit(0)

    if force:
        log.info("FORCE_SEND enabled — bypassing month/dedup guards for a test run.")

    # 1. What have we already digested? (State rides in the last sent email.)
    old_keys = fetch_last_manifest(gmail_svc)

    # 2. Collect the current report landscape.
    editions = collect_all(gmail_service=gmail_svc)
    new_editions = [ed for ed in editions if ed["key"] not in old_keys]
    log.info("Collected %d editions; %d are new since the last digest.",
             len(editions), len(new_editions))

    if not new_editions:
        log.info("No new reports this quarter — not sending a digest.")
        sys.exit(0)

    # Newest first, cap for cost.
    def sort_key(ed):
        d = parse_date(ed["published"])
        return d.isoformat() if d else "0000-00-00"
    new_editions.sort(key=sort_key, reverse=True)
    if len(new_editions) > MAX_REPORTS_PER_RUN:
        log.info("Capping digestion at %d of %d new editions.",
                 MAX_REPORTS_PER_RUN, len(new_editions))
        new_editions = new_editions[:MAX_REPORTS_PER_RUN]

    # 3. Digest each report with Claude. Bound per-call stalls so 18 sequential
    #    reports can't blow past the workflow timeout (SDK default is 10 min x3).
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"],
                                 timeout=240.0, max_retries=1)
    usage = {"in": 0, "out": 0}
    summaries = []
    for ed in new_editions:
        log.info("Digesting: %s — %s", ed["bank"], ed["title"])
        summary = summarize_report(client, ed, usage)
        if summary:
            summaries.append({**ed, "summary": summary})

    if not summaries:
        log.info("No reports could be digested — not sending.")
        sys.exit(0)

    # 4. Synthesize the email.
    label = quarter_label(now)
    prompt = build_synthesis_prompt(summaries, label)
    subject, html = generate_digest_html(client, prompt, label, usage)

    est = (usage["in"] * 2 + usage["out"] * 10) / 1_000_000
    log.info("Token usage: %d in / %d out — est. $%.4f (Sonnet 5 intro rates).",
             usage["in"], usage["out"], est)

    # 5. Manifest = everything previously digested + what was successfully
    #    summarized today. Editions that failed to download/summarize or were
    #    cut by the per-run cap stay eligible and retry next quarter (until
    #    the age filter retires them).
    sent_keys = set(old_keys) | {s["key"] for s in summaries}
    html = embed_manifest(html, list(sent_keys))

    recipients = [r.strip() for r in
                  os.environ.get("DIGEST_RECIPIENTS", "").split(",") if r.strip()] or DEFAULT_RECIPIENTS
    send_email(gmail_svc, subject, html, recipients)
    log.info("Done — digested %d report(s).", len(summaries))


if __name__ == "__main__":
    # Same fail-soft top level as the daily briefing: the cron fires daily
    # through each digest month, so a crash must not email a failure on every
    # tick. A genuinely broken pipeline surfaces as a MISSING digest.
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        log.exception("Digest run failed; exiting 0 to avoid failure-email spam.")
