# Compliance & Data Ethics

This document is part of the deliverable, not boilerplate. The engineering
decisions it describes are enforced in code, and several of them cost
throughput on purpose.

Nothing here is legal advice. Have counsel review your position before running
this in production against third-party sites.

---

## 1. The position this system takes

| Principle | How it is enforced |
|---|---|
| Respect `robots.txt` | `common/robots.py` — checked before **every** request. A disallowed URL is never fetched. On by default (`HOMZ_RESPECT_ROBOTS=true`). |
| Respect `Crawl-delay` | Parsed from robots.txt and applied to that host's token bucket, tightening the global rate if the site asks for slower. |
| Crawl slowly | `HOMZ_PER_HOST_RPS=0.5` default — one request per two seconds per host. SquareYards runs at 0.33. |
| Public data only | No login, no session cookies, no paywall circumvention. Everything collected is served to an anonymous visitor. |
| Detect blocks, never defeat them | `common/captcha.py` **identifies** captcha/WAF walls so the crawler can stop. There is no solver, no token farm, no fingerprint spoofing. |
| Stop when told to stop | `HOMZ_ABORT_ON_BLOCK=true` aborts the job on a captcha/WAF/403 wall rather than retrying into it. |
| Identify honestly | Real, current browser User-Agents. Reddit gets the descriptive UA its API terms require. |
| Prefer sanctioned channels | Sitemaps first, official APIs where they exist (Reddit), HTML last. |

### What this system deliberately does **not** do

- Solve or outsource CAPTCHAs
- Rotate residential proxies to evade a rate limit or an IP ban
- Route requests through website visitors' IP addresses
- Spoof TLS/JA3 fingerprints or patch browser automation flags
- Log in, reuse a user's cookies, or access anything behind auth
- Scrape personal data of private individuals
- Ignore `robots.txt` "because the data is public anyway"

`common/proxy.py` exists to spread legitimate load across egress IPs and to
survive flaky networks — not to evade a site that has told us to stop. When a
proxy is hard-blocked it is benched, not rotated around the block.

**On using visitor IPs:** distributing fetches across your website visitors'
IP addresses (the Hola/Luminati residential-proxy pattern) is the same evasion
in a different costume. It exposes visitors to being blocked or flagged by
sites they never chose to contact, and it is out of scope for this codebase.
The compliant version of that idea is an **opt-in browser extension that
extracts data from pages the user is already viewing** — zero extra requests to
the portal, explicit consent at install. That is a different product; it is not
implemented here.

---

## 2. Per-source status and fallbacks

Terms of service change. **Re-check each source's ToS and robots.txt before
deploying**, and treat the table below as a starting point, not a clearance.

### Reddit — sanctioned ✅

Uses the **official Reddit API** (`oauth.reddit.com`) with a registered script
app and the `client_credentials` grant. This is the compliant path and needs no
fallback.

- Register at <https://www.reddit.com/prefs/apps>
- Reddit requires a descriptive, contactable User-Agent — set
  `HOMZ_REDDIT_USER_AGENT` to a real contact, not the placeholder.
- Rate limits are read from `X-Ratelimit-Remaining` / `-Reset` and obeyed.
- Free tier is ~100 queries/minute. Commercial/high-volume use requires a paid
  agreement with Reddit — check current terms if you scale up.

### MagicBricks, Housing.com, SquareYards — HTML, use with care ⚠️

None of these publish a public data API. Their ToS generally restrict automated
collection. The crawler therefore:

1. reads `robots.txt` and honours it;
2. prefers the **sitemaps** they advertise there;
3. crawls slowly with a single connection per host;
4. **stops** on any anti-bot wall instead of working around it.

**If a source blocks you, escalate in this order — never by evasion:**

| Step | Action |
|---|---|
| 1 | **Slow down.** Drop `HOMZ_PER_HOST_RPS` to `0.1` (1 req/10s) and re-run. Most blocks are rate-based. |
| 2 | **Narrow scope.** Fewer cities, fewer pages, longer interval between runs. |
| 3 | **Use the sitemap only.** Skip paginated search entirely; sitemaps are what the site asks crawlers to use. |
| 4 | **Ask for a licence.** All three run B2B data/API partnerships. A commercial feed is cheaper than an arms race and it is contractually safe. |
| 5 | **Substitute an open source.** RERA portals (below) are government-published and carry no such restriction. |
| 6 | **Stop crawling that source.** Disable it in `homz/scrapers/__init__.py` and note it in the runbook. |

**Never**: buy a captcha-solving service, add stealth patches, or rotate IPs
(yours or your visitors') to get past a block. If a site has erected a wall,
that is the answer.

### Recommended additional sources (open by design)

These are government or open-data publishers with no anti-scraping posture, and
they carry the authoritative version of data the portals only echo:

| Source | What it gives you | URL |
|---|---|---|
| HARERA (Haryana) | Project registrations, promoter details, QPRs, complaints | `haryanarera.gov.in` |
| UP-RERA | Same, for Noida / Greater Noida / Ghaziabad | `up-rera.in` |
| Delhi RERA | Same, for Delhi | `rera.delhi.gov.in` |
| MCA21 | Builder corporate filings, directors, charges | `mca.gov.in` |
| data.gov.in | Census, infrastructure, municipal datasets | `data.gov.in` |
| Haryana / UP registration dept | Circle rates, registered transaction values | state revenue portals |

RERA in particular is worth wiring in early: it is the ground truth for the
`rera_number`, possession-date and promoter fields that the risk score depends
on, and a listing whose claimed possession date contradicts its RERA QPR is
exactly the signal the platform should be surfacing.

---

## 3. Personal data

Indian **DPDP Act 2023** applies to personal data of identifiable individuals.

What this system stores:

- **Business contact details published on a listing** (agent name, agency,
  business phone) — collected because they are published as business contact
  points. `contact_phone` is normalized but never enriched, cross-referenced,
  or used to build a profile.
- **Reddit usernames** — pseudonymous handles attached to public posts.
  `AutoModerator` and `[deleted]` are dropped.

What it does not store: private individuals' details, anything behind a login,
scraped emails for marketing, or any attempt to de-anonymise a Reddit account.

**Before production, decide and document:**

- [ ] Retention period for `contact_phone` / `contact_email` — set one and
      enforce it with a scheduled `UPDATE ... SET contact_phone = NULL`.
- [ ] Whether you need agent contacts at all. If the product does not use them,
      stop collecting them — the cheapest compliance posture is not holding the
      data.
- [ ] A deletion path for a Reddit user who asks to be removed.
- [ ] Whether DPDP consent/notice obligations apply to your use.
- [ ] Do **not** feed scraped phone numbers into outbound marketing — that
      engages TRAI DND regulations independently of DPDP.

---

## 4. Copyright

Facts are not copyrightable; **expression is**. Listing descriptions and
photographs are the portal's or the lister's copyrighted material.

- ✅ Safe to store and republish: price, area, configuration, sector, possession
  status, RERA number — these are facts.
- ⚠️ `description` is stored for AI enrichment and debugging. **Do not
  republish it verbatim.** Show the generated `ai_summary` instead.
- ⚠️ `property_images` stores image **URLs**, not the bytes. The web widget
  hot-links them. Hot-linking has its own problems (rotation, blocking); if you
  display images at scale, licence them or generate your own.
- ⚠️ Wholesale republication of a portal's listing set may constitute database
  infringement even where individual facts do not.

---

## 5. Operational safeguards in the code

| Safeguard | Location |
|---|---|
| robots.txt gate + Crawl-delay | `common/robots.py`, wired into `common/http.py` |
| Per-host token bucket, global concurrency cap | `common/ratelimit.py` |
| Exponential backoff with full jitter; `Retry-After` honoured | `common/retry.py` |
| Block classification, no bypass | `common/captcha.py` |
| Abort-on-block | `common/base.py::run_job`, `HOMZ_ABORT_ON_BLOCK` |
| Raw payload archive (re-parse instead of re-crawl) | `common/rawstore.py` |
| Incremental state (don't re-fetch what hasn't changed) | `common/state.py` |
| Read-only API — no endpoint can trigger a crawl | `search/api.py` |

The raw archive and incremental state are compliance features as much as
engineering ones: both exist so that a parser bug or a schema change does not
turn into another full crawl of someone else's site.

---

## 6. Pre-production checklist

- [ ] Legal review of each source's current ToS
- [ ] `HOMZ_RESPECT_ROBOTS=true` in production (verify — do not assume)
- [ ] `HOMZ_ABORT_ON_BLOCK=true` in production
- [ ] `HOMZ_PER_HOST_RPS` ≤ 0.5; start lower and raise only if clean
- [ ] Real contact details in `HOMZ_REDDIT_USER_AGENT`
- [ ] Retention policy set for contact fields and raw HTML
- [ ] Alerting on `scrape_runs.status = 'blocked'` — a block must page a human,
      not silently retry tomorrow
- [ ] Commercial licence conversations opened with any source you depend on
- [ ] RERA ingestion planned as the authoritative cross-check
- [ ] Decided: what happens if a source sends a takedown request (who responds,
      how fast, how the data is purged)
