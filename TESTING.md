# Testing Guide

Four levels, cheapest first. Levels 1–2 need no database and no network.

---

## 0. Where the Mongo URL goes

**File: `.env`, line 15.** It has already been created for you from
`.env.example`, with an ingest token generated. Replace only the URI:

```bash
HOMZ_MONGODB_URI=mongodb+srv://YOUR_USER:YOUR_PASS@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
```

Get that string from **Atlas → your cluster → Connect → Drivers → Python**.

Three things bite people here:

| Problem | Symptom | Fix |
|---|---|---|
| IP not allowlisted | connection times out | Atlas → **Network Access** → Add IP Address → "Add Current IP" |
| Password has `@ : / ? #` | auth fails, or URI parses wrong | Percent-encode it: `p@ss` → `p%40ss` |
| Using the shell/Compass string | starts `mongodb://` with `/test?` | Use the **Drivers** string, not the shell one |

`.env` is gitignored (`git check-ignore .env` confirms) — your credentials
will not be committed.

---

## 1. Unit tests — no database, no network

```bash
make install          # one time; creates .venv and installs deps
make test
```

Expect **170 passed** in well under a second. This covers price/area parsing,
dedupe, the scoring formulas, all four parsers, query fingerprinting and
ingest auth. If this fails, nothing else is worth trying.

---

## 2. Check the wiring without a cluster

```bash
.venv/bin/homz --help
.venv/bin/homz scrape list          # sources and their default jobs
.venv/bin/homz ops config           # effective config, secrets redacted
```

---

## 3. Connect to Atlas

```bash
.venv/bin/homz db ping
```

This is the command to run first when anything looks wrong. It connects and
translates the failure — Atlas surfaces every problem as the same opaque
timeout, so this separates *IP not allowlisted* from *bad password* from
*SRV not resolving*, which need completely different fixes.

Success looks like:

```json
{ "ok": true, "server_version": "7.0.x", "backend": "atlas", "transactions": true }
```

`"backend": "atlas"` means Atlas Search is available (fuzzy/typo-tolerant
search). `"backend": "text"` means it fell back to `$text` — weighted, but a
typo finds nothing.

Then create the schema:

```bash
.venv/bin/homz db init          # collections + 66 indexes + Atlas Search indexes
.venv/bin/homz db search-status # Atlas Search builds async — wait for queryable=true
.venv/bin/homz db check         # document counts
```

> Atlas Search indexes take 30–60s to build. Until `queryable: true`, text
> search returns nothing — which looks exactly like a broken search.

---

## 4. Put real data in

### Option A — scrape a source (start with a dry run)

```bash
# Parse but write nothing — check the output looks sane first.
.venv/bin/homz scrape source magicbricks --city gurgaon --max-items 5 --dry-run

# Then for real
.venv/bin/homz scrape source magicbricks --city gurgaon --max-items 20
```

Reddit needs credentials in `.env` (`HOMZ_REDDIT_CLIENT_ID` / `_SECRET` from
<https://www.reddit.com/prefs/apps>, "script" app).

SquareYards and Housing need a browser once: `make install-browsers`.

**Selectors for MagicBricks and Housing were written from their documented
structure, not a live crawl.** Run the `--dry-run` first and check you get
real prices and areas, not nulls. If a parser is off, the raw HTML is already
archived — `homz ops raw <key>` replays it, no re-crawl needed.

### Option B — push a page in by hand (no scraping at all)

Useful for testing the pipeline in isolation:

```bash
curl -s -X POST http://localhost:8000/ingest/page \
  -H "authorization: Bearer $(grep HOMZ_INGEST_TOKEN .env | cut -d= -f2)" \
  -H 'content-type: application/json' \
  -d '{"source":"magicbricks","url":"https://www.magicbricks.com/propertyDetails/x-pdpid-123","html":"<PASTE PAGE HTML>"}'
```

### Then process it

```bash
.venv/bin/homz etl run          # rollups, delist stale, locality aggregates
.venv/bin/homz enrich scores    # investment/risk/location scores — free, no LLM
.venv/bin/homz ops status       # what ran, what failed
```

---

## 5. Test the search → miss → scrape → hit loop

Start the API:

```bash
make api          # http://localhost:8000/docs
```

Open <http://localhost:8000/web/> for the search widget.

Then walk the loop with curl (`TOKEN` from your `.env`):

```bash
TOKEN=$(grep HOMZ_INGEST_TOKEN .env | cut -d= -f2)
B=http://localhost:8000

# 1. A search that misses → a fill task is queued
curl -s "$B/properties?q=Godrej%20Aristocrat&city=gurgaon" | jq '{total, backfill}'
#    → {"total": 0, "backfill": {"queued": true, "task_id": "9975f413…"}}

# 2. Ingest is closed without a token
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$B/ingest/page" \
     -H 'content-type: application/json' -d '{}'
#    → 401

# 3. Your client claims the work
curl -s "$B/ingest/tasks?worker=my-extension&limit=1" \
     -H "authorization: Bearer $TOKEN" | jq
#    → {"tasks":[{"task_id":"9975f413…","query":{"q":"Godrej Aristocrat","city":"gurgaon"}}]}

# 4. Your client scrapes, then POSTs the HTML back with the task_id
curl -s -X POST "$B/ingest/page" -H "authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' \
     -d '{"source":"magicbricks","url":"…","html":"…","task_id":"9975f413…"}'
#    → {"accepted":1,"inserted":1}

# 5. The same search is now a hit
curl -s "$B/properties?q=Godrej%20Aristocrat&city=gurgaon" | jq '.total'

# queue depth and remaining daily crawl budget
curl -s "$B/ingest/stats" | jq
```

This exact sequence was verified end-to-end against MongoDB 7 during
development.

---

## 6. What your client-side scraper has to do

Only three things — it never needs to know a portal's markup, because the
server-side parsers do the extraction:

```
loop:
  GET  /ingest/tasks?worker=<id>       →  {task_id, query:{q, city, …}}
  (scrape a matching page yourself)
  POST /ingest/page {source, url, html, task_id}
```

Both calls need `Authorization: Bearer <HOMZ_INGEST_TOKEN>`.

If a task can't be satisfied, say so rather than dropping it — it becomes
retryable after 30 minutes instead of blocking for 6 hours:

```bash
curl -X POST "$B/ingest/tasks/<task_id>/complete" \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"records_written":0,"error":"page was a search page, not a listing"}'
```

A task claimed but never completed returns to the pool after 30 minutes, so a
crashed client doesn't strand work.

---

## 7. Guardrails you should know about

Search can now cause outbound traffic, so three limits apply — all in `.env`:

| Setting | Default | What it stops |
|---|---|---|
| `HOMZ_ONDEMAND_COOLDOWN_MINUTES` | 360 | Someone hammering refresh on an empty query becoming a scrape loop |
| `HOMZ_ONDEMAND_DAILY_BUDGET` | 500 | A bot crawling *your* search amplifying into thousands of portal requests |
| `HOMZ_ONDEMAND_MIN_RESULTS` | 5 | Queueing work for searches that already have enough results |

Set `HOMZ_ONDEMAND_ENABLED=false` to turn the loop off entirely.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `db ping` times out | IP not allowlisted, or cluster paused | Atlas → Network Access; check the cluster is running |
| `db ping` says auth failed | Wrong password, or unencoded special characters | Atlas → Database Access; percent-encode the password |
| Search returns nothing after `db init` | Atlas Search index still building | `homz db search-status` until `queryable: true` |
| `backend: "text"` on an Atlas cluster | Search index missing or user lacks permission | Re-run `homz db init`, check the user has `readWrite` |
| Scrape parses rows but prices are null | Portal markup changed | `homz ops raw <key>` to replay the archived HTML; fix the parser |
| `status='blocked'` in `ops status` | Anti-bot wall hit | Lower `HOMZ_PER_HOST_RPS` to `0.1`. Do **not** add evasion — see COMPLIANCE.md |
| Ingest returns 401 | Token missing/wrong, or `HOMZ_INGEST_TOKEN` unset | Check the header is `Authorization: Bearer <token>` |
| Ingest returns 503 | `HOMZ_INGEST_TOKEN` is empty | Set it — an empty token disables ingest by design |
