# Homz Realtor — Real Estate Intelligence Platform

Production scraping, ETL, AI enrichment, search API and embeddable frontend for
the **Delhi NCR** property market (Gurgaon, Noida, Greater Noida, Delhi,
Faridabad, Ghaziabad).

Collects public listings, projects, builders and community discussion from
MagicBricks, Housing.com, SquareYards and Reddit; normalizes everything into one
schema; scores it; serves it through one API; and renders it with a drop-in web
component.

> **Read [COMPLIANCE.md](COMPLIANCE.md) before running this against live sites.**
> The crawler honours `robots.txt`, rate-limits hard, and **stops** at anti-bot
> walls rather than defeating them. That is deliberate and enforced in code.

---

## 1. Architecture

```
                    ┌──────────── SOURCES ────────────┐
                    │ MagicBricks  Housing            │
                    │ SquareYards  Reddit (official   │
                    │                     OAuth API)  │
                    └────────────────┬────────────────┘
                                     │
                    ┌────────────────▼────────────────┐
   common/          │  robots gate → rate limiter →   │   ← reusable, knows
   (source-agnostic)│  proxy → UA rotation → fetch →  │     nothing about any
                    │  block detect → retry → archive │     one portal
                    └────────────────┬────────────────┘
                                     │  FetchResult
                    ┌────────────────▼────────────────┐
   scrapers/<src>/  │  parser.py  (pure HTML→record)  │   ← the only place
   parser.py        │  JSON-LD → __NEXT_DATA__ →      │     portal-specific
                    │  OpenGraph → CSS  (fallbacks)   │     knowledge lives
                    └────────────────┬────────────────┘
                                     │  PropertyRecord | ProjectRecord |
                                     │  BuilderRecord  | RedditPostRecord
                    ┌────────────────▼────────────────┐
   etl/             │  dedupe → upsert → price history│
                    │  → delist stale → rollups       │
                    └────────────────┬────────────────┘
                                     │
                    ┌────────────────▼────────────────┐
   PostgreSQL       │ properties projects builders    │
                    │ reddit_posts reddit_comments    │
                    │ price_history property_images   │
                    │ locations market_insights       │
                    │ + 4 materialized rollups        │
                    └────────────────┬────────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
   enrichment/ (3 tiers)      search/ (FastAPI)     scheduler/ (cron)
   1 rules   free, 100%       full-text + facets    staggered jobs
   2 scores  free, formulas            │
   3 Claude  paid, selective           ▼
                              web/ <homz-search>
                              zero-dep web component
```

**The one rule that keeps this maintainable:** `common/` never imports from
`scrapers/`, and a `parser.py` never makes a network call. Adding a fifth portal
is two methods (`discover`, `parse_detail`) plus a parser module — not a new
pipeline.

### Layout

```
src/homz/
├── settings.py          env-driven config (pydantic-settings)
├── logging_setup.py     structlog: console in dev, JSON in prod
├── cli.py               `homz` command
├── common/              ── reusable scraping infrastructure ──
│   ├── schema.py          the normalized contract every scraper satisfies
│   ├── enums.py           controlled vocabularies
│   ├── parsing.py         ₹/Cr/Lac, sqft/gaj, BHK, possession, RERA
│   ├── geo.py             NCR gazetteer: city, sector, micro-market
│   ├── domx.py            JSON-LD / __NEXT_DATA__ / CSS extraction ladder
│   ├── http.py            the fetcher — where all middleware composes
│   ├── browser.py         Playwright pool (JS-rendered pages only)
│   ├── robots.py          robots.txt gate + Crawl-delay
│   ├── ratelimit.py       per-host token bucket + global concurrency
│   ├── retry.py           backoff + full jitter + failure classifier
│   ├── proxy.py           proxy pool with benching
│   ├── useragent.py       UA rotation with coherent header sets
│   ├── captcha.py         block detection (detect, never bypass)
│   ├── rawstore.py        gzipped raw-payload archive
│   ├── dedupe.py          simhash + blocked pairwise matching
│   ├── state.py           incremental cursors
│   └── base.py            BaseScraper: the run loop and reporting
├── scrapers/            ── one package per source ──
│   ├── magicbricks/{scraper,parser}.py
│   ├── housing/{scraper,parser}.py
│   ├── squareyards/{scraper,parser}.py
│   └── reddit/{scraper,parser}.py
├── db/                  models + idempotent upserts
├── etl/                 load, dedupe, aggregate, market trends
├── enrichment/          extractors, prompts, Claude client, scoring
├── search/              query builder + FastAPI service
└── scheduler/           APScheduler job definitions

web/                     embeddable frontend (see web/README.md)
├── homz-sdk.js          zero-dependency API client
├── homz-widget.js       <homz-search> web component
└── index.html           demo harness
```

---

## 2. Quick start

```bash
cp .env.example .env        # fill in Reddit creds + ANTHROPIC_API_KEY
docker compose up -d postgres        # schema auto-applies from sql/ on first boot
docker compose up -d api             # http://localhost:8000/docs
open http://localhost:8000/web/      # the search widget

# one scrape, no writes, to see it work
docker compose run --rm scraper-cli scrape source magicbricks --max-items 20 --dry-run
```

Local (no Docker):

```bash
make install && make install-browsers
make db-up && make db-migrate
homz scrape source magicbricks --city gurgaon --max-items 20 --dry-run
make api
```

Verify:

```bash
homz db check          # connectivity + row counts
homz ops status        # recent runs, errors, blocks
make test              # 147 unit tests, no network or DB needed
```

---

## 3. CLI

```bash
# scraping
homz scrape list                                    # sources + default jobs
homz scrape all [--dry-run] [--parallel]
homz scrape source magicbricks --city gurgaon --listing-type rent --max-items 50
homz scrape source reddit --city gurgaon --full     # --full ignores incremental state

# etl
homz etl run [--stale-days 21]                      # delist stale, refresh rollups, prune raw
homz etl insights --days 90                         # price/supply/yield trends
homz etl dedupe                                     # cross-source duplicate linking

# enrichment
homz enrich estimate                                # project LLM cost BEFORE spending
homz enrich scores                                  # deterministic only — free
homz enrich run [--no-llm] [--no-batch] [--limit N]

# database
homz db init                                        # apply sql/*.sql
homz db check
homz db refresh-views

# inspection
homz search "3 bhk sector 82 gurgaon" --city gurgaon
homz ops status
homz ops raw <archive-key>                          # replay a stored page
homz ops config                                     # effective config, secrets redacted
```

---

## 4. Data model

Nine core tables plus operational ones. Full DDL: [`sql/001_schema.sql`](sql/001_schema.sql).

| Table | Purpose |
|---|---|
| `properties` | The fact table. One row per listing, keyed `(source, source_id)`. |
| `projects` | Builder projects, distinct from individual units. |
| `builders` | Developers, with trust/risk scores. |
| `locations` | Dimension table — "all listings on Dwarka Expressway" is an index scan. |
| `property_images` | Image URLs (not bytes), deduped per owner. |
| `price_history` | **Append-only, written by a database trigger.** |
| `reddit_posts` / `reddit_comments` | Discussion + extracted entities and sentiment. |
| `market_insights` | Computed trend observations. |
| `scrape_state` / `scrape_runs` | Incremental cursors and run history. |
| `property_duplicates` | Cross-source duplicate links. |

**Design decisions worth knowing:**

- **Every write is `INSERT ... ON CONFLICT DO UPDATE` on `(source, source_id)`.**
  Re-running a scraper is safe and cheap.
- **`COALESCE(EXCLUDED.x, table.x)` on descriptive fields.** A thin re-scrape
  (search card only) must never blank a field a richer detail-page scrape
  already filled.
- **Price history is trigger-driven.** Application code cannot forget to record
  a price change — verified end-to-end.
- **Money is `NUMERIC`, never float.** Area is normalized to sqft with the
  original value and unit preserved.
- **Four materialized rollups** (`mv_locality_price_trends`, `mv_rental_yield`,
  `mv_builder_scorecard`, `mv_supply_demand`) back the scores and the market
  endpoints. Refreshed by `homz etl run`.

### Normalized schema

Every scraper emits `PropertyRecord`, `ProjectRecord`, `BuilderRecord`,
`RedditPostRecord` or `MarketInsightRecord` (`common/schema.py`). All fields are
optional except the identity triple `(source, source_id, listing_url)` — real
listings are messy and a half-filled record still has value.

Two derived fields drive the pipeline:

- `content_hash` — sha256 of volatile business fields. Unchanged hash ⇒ nothing
  worth writing changed ⇒ ETL skips the row. This is what makes incremental
  runs cheap.
- `dedupe_key` — project + config + area bucket + price bucket. The blocking key
  for cross-source duplicate detection.

---

## 5. Anti-fragility: why parsers survive redesigns

Portal CSS churns constantly; the structured data they emit for Google does not.
Every parser follows the same ladder (`common/domx.py`):

1. **JSON-LD** (`<script type="application/ld+json">`) — most stable
2. **Framework state** — `__NEXT_DATA__` (Housing is Next.js), `window.__INITIAL_STATE__`
3. **OpenGraph / meta tags**
4. **CSS selectors** — last resort, always with fallback selectors listed inline

`domx.find_first_key()` searches embedded JSON *by leaf key* rather than by
fixed path, because portals rename wrappers far more often than leaves.

When a parser does break: the raw payload is already archived, so you fix the
parser and replay — you do not re-crawl.

```bash
homz ops raw magicbricks/2026/07/22/ab/abc123.html.gz > /tmp/page.html
# fix parser, then add /tmp/page.html as a fixture in tests/test_scrapers.py
```

### Per-source notes

| Source | Transport | Notes |
|---|---|---|
| MagicBricks | HTTP | Server-rendered. Sitemap-first discovery, paginated search as fallback. 0.4 req/s. |
| Housing.com | HTTP → browser | Detail pages usually ship `__NEXT_DATA__` server-side; search is client-rendered, so discovery escalates to Playwright only when the cheap path yields nothing. |
| SquareYards | Browser | Fully JS-rendered; amenities sit behind a modal that a `page_actions` callable opens. Selectors ported from the validated Puppeteer scripts at the repo root. 0.33 req/s. |
| Reddit | Official API | OAuth `client_credentials`. `/new` for recency (cursor-based ⇒ incremental) plus 18 targeted topic searches. |

---

## 6. AI enrichment

Three tiers, each cheaper than the next is expensive:

| Tier | What | Cost | Coverage |
|---|---|---|---|
| 1. Rule extraction | Builders, projects, sectors, city, topics, lexicon sentiment | Free | 100%, at ingest |
| 2. Deterministic scores | Investment, risk, location, builder trust | Free | 100% |
| 3. Claude | Nuanced sentiment, summaries, entities outside the gazetteer | Paid | Selective |

**Scores are formulas, not model opinions.** That is a product requirement: a
score must be explainable ("risk 41 because possession slipped and no RERA
number is stated"), reproducible, and stable when the LLM tier is off. Every
score returns its component breakdown so the API can show its working. The LLM
contributes *inputs* (sentiment, claims); it never produces the final number.
`builder_trust_score` caps the LLM's influence at 30% for exactly this reason —
a generous model read cannot erase a hard-evidence penalty.

**Cost control:**

- Tier 3 runs only where rules genuinely cannot help (Reddit prose; listings
  whose builder/project the gazetteer missed).
- **Message Batches API** by default — 50% of standard price.
- `output_config.format` with a JSON schema, so shape is enforced server-side
  rather than parsed hopefully.
- Frozen system prompts with `cache_control`. `verify_cache_hits()` reports
  whether caching actually paid off rather than assuming it did — note that
  Opus 4.8 has a 4096-token minimum cacheable prefix, so short prompts will
  correctly report no benefit.
- `homz enrich estimate` projects the bill from real `count_tokens` calls before
  you spend anything.

Model: `claude-opus-4-8`, configurable via `HOMZ_LLM_MODEL`.

### Gazetteer

`enrichment/extractors.py` carries ~95 NCR developers with aliases and ~120
known projects. Adding a name there immediately improves recall across every
historical Reddit post on the next enrichment pass — the cheapest quality lever
in the system. It also drives builder inference: a listing that names only
"Godrej Aristocrat" still gets attributed to Godrej Properties, which is what
keeps the builder-trust feature from being empty.

---

## 7. Search API

`http://localhost:8000/docs` for interactive docs. Read-only: no endpoint can
trigger a crawl, so a traffic spike never becomes a spike against a source.

```
GET /properties          full-text + 20 filters + facets + 8 sort modes
GET /properties/{id}     detail + images + price history + duplicate listings
GET /properties/facets   filter counts honouring current filters
GET /autocomplete        typo-tolerant across projects/builders/localities
GET /builders            ranked by trust score
GET /builders/{id}       portfolio + public discussion
GET /projects
GET /reddit              by builder / project / sector / topic / sentiment
GET /market/trends       median ₹/sqft by locality and month
GET /market/yield        rental yield by locality and configuration
GET /market/supply-demand
GET /market/insights
GET /market/new-launches
```

Example:

```bash
curl "localhost:8000/properties?\
q=godrej&city=gurgaon&listing_type=sale&bedrooms_min=3&\
price_max=25000000&possession_status=ready_to_move&\
max_risk_score=40&sort=investment"
```

Ranking blends `ts_rank_cd` on a generated `tsvector`, trigram similarity for
typo tolerance, and freshness decay (~half weight at 60 days). All filtering is
parameterized — user input never reaches string concatenation.

---

## 8. Frontend

A zero-dependency web component. Full docs: [`web/README.md`](web/README.md).

```html
<script type="module" src="https://cdn.homzrealtor.com/homz-widget.js"></script>
<homz-search api="https://api.homzrealtor.com" city="gurgaon" sync-url></homz-search>
```

Renders in a Shadow DOM so host-page CSS can't collide in either direction;
themed via CSS custom properties. `sync-url` makes every search a shareable
link with a working back button. Emits `homz:select` so you route to your own
property detail page.

CORS is configured for `homzrealtor.com` and any subdomain via
`HOMZ_API_CORS_ORIGINS`.

---

## 9. Scheduling

Two options, same job definitions:

- **APScheduler** — `docker compose up -d scheduler` (one long-lived process)
- **cron** — `crontab deploy/crontab` (invokes the CLI)

| Job | Cadence | Why |
|---|---|---|
| Sale listings | daily 02:30 IST | Off-peak; never competing with the portals' own traffic peak. |
| Rentals | 06:00 and 18:00 | Rentals churn fastest. |
| SquareYards | every 2nd day 04:00 | Browser-driven and expensive. |
| Reddit | every 6h | Posts are short-lived on `/new`. |
| ETL + insights | daily 08:00 | After the scrapes have landed. |
| AI enrichment | daily 09:30 | Batch mode makes the latency irrelevant. |
| Deterministic rescoring | every 4h | Free. |
| Prune raw archive | weekly | Retention is `HOMZ_RAW_HTML_RETENTION_DAYS`. |

Jobs are staggered so two browser-heavy jobs never overlap. `coalesce=True` and
`max_instances=1` mean a missed run does not pile up and two copies never run.

---

## 10. Configuration

Everything is env-driven; see [`.env.example`](.env.example). The settings that
matter most:

| Variable | Default | Notes |
|---|---|---|
| `HOMZ_RESPECT_ROBOTS` | `true` | **Leave on.** See COMPLIANCE.md. |
| `HOMZ_ABORT_ON_BLOCK` | `true` | Stop the job at a captcha/WAF wall. |
| `HOMZ_PER_HOST_RPS` | `0.5` | 1 request / 2s per host. Lower it if blocked. |
| `HOMZ_MAX_CONCURRENCY` | `4` | Global in-flight cap. |
| `HOMZ_STORE_RAW_HTML` | `true` | Needed to fix parsers without re-crawling. |
| `HOMZ_LLM_USE_BATCH` | `true` | 50% cheaper. |
| `HOMZ_LLM_ENABLED` | `true` | `false` ⇒ tiers 1–2 only, zero LLM cost. |
| `HOMZ_API_CORS_ORIGINS` | homzrealtor.com + localhost | Browser origins allowed to call the API. |

---

## 11. Testing

```bash
make test        # 147 tests, no network and no database required
```

Coverage is concentrated where silent corruption is possible:

- **`test_parsing.py`** — money, area, configuration, possession, RERA, dates,
  geo. A regression here corrupts every downstream number without throwing.
- **`test_dedupe.py`** — fingerprints, simhash, similarity, canonical selection.
  Both failure directions are pinned: a missed duplicate inflates supply counts;
  a false merge hides a real listing.
- **`test_enrichment.py`** — extraction, topics, every scoring formula
  (monotonicity, bounds, hard evidence outweighing the LLM), builder inference.
- **`test_scrapers.py`** — each parser against synthetic fixtures mirroring real
  portal structure, plus block detection, the extraction ladder, and job-key
  isolation.

**Verified against live PostgreSQL 16 during development:** schema applies
cleanly and is idempotent (13 tables, 4 matviews, 92 indexes, 7 triggers, 9
enums); upserts are idempotent; the price-history trigger records deltas
correctly (`2.35 Cr → 2.10 Cr` produced `change_pct = -10.638`); full-text
search, facets, autocomplete, builder inference, scoring and the ETL rollups all
return correct results end-to-end. The frontend was verified against a running
API: static serving, CORS preflight for `homzrealtor.com` and the wildcard
subdomain regex, and all four widget endpoints.

---

## 12. Operations

```bash
homz ops status        # run history: parsed / errors / blocked per job
homz db check          # row counts
homz ops raw           # archive size and retention
```

**Alert on `scrape_runs.status = 'blocked'`.** A block must page a human, not
silently retry tomorrow — it is the signal to slow down or open a licensing
conversation, and the correct response is never to try harder.

Common situations:

| Symptom | Cause | Action |
|---|---|---|
| `status='blocked'` | Anti-bot wall | Drop `HOMZ_PER_HOST_RPS` to 0.1. Do **not** add evasion. See COMPLIANCE.md §2. |
| Parsed rows but null prices | Portal markup changed | `homz ops raw <key>`, fix the parser, add the payload as a fixture. |
| `skipped_known` very high | Working as intended | Incremental skip. Use `--full` to force. |
| Reddit 401 | Bad credentials or UA | Check `HOMZ_REDDIT_*`; Reddit requires a descriptive UA. |
| Slow matview refresh | Table growth | Raise `maintenance_work_mem`; consider partitioning `price_history` by month. |
| `cannot refresh ... concurrently` | Matview never populated | Handled automatically — the ETL retries non-concurrently. |
| Widget shows no results | CORS or wrong `api` attribute | Check the browser console; `/health` on the demo page shows API status. |

---

## 13. Known limitations

Stated plainly, because they affect how you should use this:

1. **Portal selectors are best-effort.** They are written defensively (four-level
   extraction ladder, fallback selectors) and the SquareYards ones are ported
   from validated Puppeteer scripts, but MagicBricks and Housing selectors are
   built from their documented structure rather than a live crawl. **Run each
   source once with `--dry-run --max-items 5` and inspect the output before
   trusting a full run.** The archived raw HTML makes the first correction cheap.
2. **No RERA ingestion yet.** It is the highest-value next source and is
   government-published, so it carries none of the ToS friction — see
   COMPLIANCE.md §2.
3. **Geocoding is bounding-box only.** Coordinates are taken where a portal
   provides them and validated against an NCR box; there is no geocoding service
   and no PostGIS. Add PostGIS if you need radius search.
4. **Micro-market weights are a market judgement**, reviewed quarterly, held as
   data in `scoring.py`. They are not derived from transactions.
5. **Cross-source dedupe is within-batch** at load time, plus a `dedupe_key`
   sweep via `homz etl dedupe`. A full historical re-match is not automatic.
6. **Rental yield needs ≥3 sale and ≥3 rent samples** per (city, sector,
   bedrooms) before it is emitted — thin localities will legitimately show no
   yield rather than a noisy one.

---

## 14. Legacy

The Puppeteer scripts at the repo root (`gurgaonPDPScraper.js`,
`noidaPDPScraperNew.js`, …) are the original SquareYards scrapers. Their
selectors are the validated basis for
`src/homz/scrapers/squareyards/parser.py`, which supersedes them with rate
limiting, retries, block detection, incremental state and the normalized schema.
They are kept for reference and can be removed once you have run the Python
scraper against live pages.

Note that the root `package.json` declares `"type": "commonjs"` for those
scripts, which affects Node tooling run against `web/*.js` — see
[`web/README.md`](web/README.md#using-the-sdk-on-its-own). Browsers are
unaffected.
