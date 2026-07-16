# Electrical Industry Digest (quarterly)

Quarterly automation that gathers the research reports middle-market investment banks publish
on the **electrical industry** (contracting & services, distribution, products/equipment,
utility & T&D infrastructure), digests each with Claude, and emails one analyst-style
synthesis. Requested by Gus's boss (originally attempted manually); mirrors the daily
Morning Briefing architecture.

- Script: `digest/generate_digest.py` (single file, same style as `briefing/generate_briefing.py`)
- Workflow: `.github/workflows/report_digest.yml`
- Model: `claude-sonnet-5` (per-report summaries with adaptive thinking; final HTML synthesis
  with thinking disabled, matching the proven briefing behavior)
- Est. cost: roughly $1-3/quarter at Sonnet 5 rates

## How it works

1. **Guards** (skipped by `force_send`): send dates are **hardcoded** in
   `DIGEST_SEND_DATES` — Mar 1, Jun 1, Sep 1, Dec 1, each with a 20-day grace window.
   Fixed calendar dates: test runs never shift them. The digest goes out on the first
   successful run on/after each date; a 25-day Gmail dedup guarantees one send per window.
   **Two redundant triggers** fire daily through each window: GitHub's own cron
   (14:00 UTC) and a Google Cloud Scheduler job (`report-digest` in project
   `morning-briefing-499117`, us-central1, `10 14 1-21 3,6,9,12 *` UTC) that POSTs to the
   workflow_dispatch API with the same PAT as the briefing's `morning-briefing` job. Either
   path alone delivers; the guards + the workflow's concurrency group make duplicate sends
   impossible. The Cloud Scheduler path also survives GitHub's 60-day-inactivity
   auto-disable of `schedule:` triggers (dispatch triggers are never disabled).
   The dates sit ~2 months after quarter close because the report crop lands 3-10 weeks
   after quarter end; to send right after quarter close instead, change the constant to
   (1,1),(4,1),(7,1),(10,1) and move the cron months — nothing is missed either way,
   slow-publishing reports just shift one digest later.
2. **State**: the set of already-digested report editions rides inside the previously sent
   digest email as a hidden `<!-- DIGEST-MANIFEST-B64: ... -->` comment. No repo commits,
   no external storage. Anything collected that isn't in the manifest is "new".
3. **Collect**: one fail-soft collector per source (below). A broken site never blocks the
   digest; it just contributes nothing that quarter.
4. **Digest**: one Claude call per new report. PDFs go in natively (base64 document block);
   PDFs over 20MB/550 pages and HTML-only reports go in as extracted text (pypdf).
5. **Synthesize**: one streaming Claude call turns the per-report summaries into the HTML
   email: executive synthesis, cross-report numbers, report-by-report sections, source list.
   Same Gmail-dark-mode-safe palette/type system as the briefing.
6. **Send** via the Gmail API (same Google OAuth secrets as the briefing; draft fallback on
   send failure). Top-level try/except exits 0 so daily cron ticks never spam failure emails;
   a broken pipeline = a missing digest.

## Sources (verified 2026-07-09)

| Source | Publication | Cadence | Access path |
|---|---|---|---|
| Cascade Partners | Electrical Contracting & Utility Infra M&A Update | semiannual (H1 ~Jul, H2 ~Feb) | open WP media API + landing-page PDF href (gate is cosmetic) |
| FMI | NA Engineering & Construction Outlook | quarterly | PDF filename probe (`Q{n}_Outlook_{YYYY}[_Final].pdf`) — site HTML is Cloudflare-403 to scripts |
| Robert W. Baird | Electrical Power Solutions Sector Update | ~3x/yr | fully predictable ungated PDF URL (month-name keyed) |
| Harris Williams | Utility Services & Power Products Brief (+ EPI deep dives) | quarterly | index scrape → ctfassets PDF |
| Houlihan Lokey | Power/Utilities/Renewables + EII Services updates | quarterly/semiannual | cdn.hl.com PDF probe (HTML is Imperva-blocked) |
| R.L. Hulett | MEP Services + Industrial Services M&A Updates | quarterly | RSS (`?post_type=quarterly-updates`) → page → PDF |
| Lincoln International | Facilities Services Market Update (Distribution QR watched, dormant) | quarterly | publications RSS; item link serves the PDF |
| PMCF | Distribution + Construction/Building Products M&A Pulse | quarterly | index scrape → `/download/...pdf` |
| PCE | Power & Energy + Diversified Industrials M&A Updates | quarterly | HTML report, canonical URL refreshed in place; RSS pubDate keys the manifest |
| Capstone Partners | Construction Services / AEC Services updates | semiannual | category RSS → PDF or web article |
| Anchor Peabody | HVAC/Plumbing/Electrical Services Market Report | ~2x/yr | open WP REST API (category 15) |
| SDR Ventures | Industrial & Infrastructure Services Report | semiannual | report-hub scrape → Sanity CDN PDF (~26MB → pypdf path) |
| BaseRock Partners | Electrical + Utility Services Market Updates | annual (spring) | PDF is HubSpot-gated → digest the ungated summary post; full PDF arrives via email channel once subscribed |
| Northeastern Advisors | U.S. Electrical Contracting Industry Report | annual (~Jan) | ungated HTML page, year-keyed URL |
| NEMA | Electroindustry Business Confidence Index (EBCI) | monthly (newest 3 taken) | landing-page PDF hrefs |
| (Gmail channel) | anything the banks email after subscribing | — | scans mail from bank domains for PDF links |

Deliberately excluded: Electrical Marketing's newsletter PDF (paid $99/yr membership; do not
circumvent), BMI Mergers (annual recap, unreliable cadence — revisit if it resumes).
Candidates for future addition: MDM Quarterly Distribution M&A Report, ElectricalTrends
(Channel Marketing Group), Raymond James / BGL / D.A. Davidson industrials notes,
Electrical Marketing's free monthly EPI article + Excel.

## One-time setup checklist (Gus)

- [ ] **Subscribe to bank research lists** with gvonmetzsch@gmail.com — this activates the
  Gmail channel and is the only way to get the form-gated reports (FMI Energy & Power
  Overview, BaseRock PDFs):
  - https://fmicorp.com/subscribe
  - https://cascade-partners.com/enewsletter-sign-up/ (check Industrials & Manufacturing + Infrastructure)
  - https://baserockpartners.com/insights/ ("Get Our News & Insights")
  - https://www.capstonepartners.com/insights-subscribe/ (Building Products & Construction Services + Energy, Power & Infrastructure)
  - https://pmcf.com/subscribe/ (Industrial Distribution + Construction & Building Products)
  - https://sdrventures.com/industry-report-request/ (Industrial & Infrastructure Services)
- [x] **Recipients** (set 2026-07-16): configured in the `DIGEST_RECIPIENTS` repo variable
  (not listed here — this repo is public). Change anytime with
  `gh variable set DIGEST_RECIPIENTS -R gvonmetzsch/claude --body "..."`

## Operations

- **Test run any time**: Actions → Electrical Industry Digest → Run workflow → check
  `force_send`. Bypasses the month + dedup guards. Note it digests everything not in the
  manifest, so a forced run right before a scheduled one just means the scheduled one finds
  nothing new (safe).
- **Local collection test (no secrets)**: `python digest/generate_digest.py --collect-only`
- **Diagnosing**: like the briefing, a "success" run may not have sent — grep run logs for
  `Digest sent` vs `Not a digest month` / `already sent` / `No new reports`.
- **Secrets**: reuses `ANTHROPIC_API_KEY`, `GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN`. No new
  secrets. If Google auth dies with `invalid_grant`, see the briefing's fix (publish the
  OAuth consent screen, re-mint the token via OAuth Playground).
- **Source drift**: bank sites change slugs/patterns. Each collector is fail-soft, so drift
  shows up as a source silently contributing nothing — skim the run log's per-collector
  counts each quarter and re-verify any that dropped to 0.
- **Known runner-IP blocks** (first live run, 2026-07-09): cascade-partners.com and
  makeitelectric.org (NEMA) answer GitHub's datacenter IPs with HTTP 202 JS challenges,
  though they serve residential IPs fine. Until Gus's email subscriptions are active (the
  Cascade newsletter announces new editions, which the Gmail-scan collector then catches),
  those two sources won't contribute from scheduled runs. 13 of 15 collectors work from
  the runner; the first digest carried 18 reports without them.
