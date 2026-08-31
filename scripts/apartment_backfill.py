"""Exhaustive apartment backfill for Gurgaon — SquareYards + MagicBricks.

Both scrapers' *default* discovery is deliberately partial (a citywide
sample, not full coverage — see each scraper's own docstring). This script
instead targets "every apartment" specifically:

- SquareYards: pages the type-scoped `/rent/apartments-for-rent-in-gurgaon`
  and `/sale/apartments-for-sale-in-gurgaon` pages directly (own independent
  listing counts: 14,338+ / 16,232+ live) as deep as they go — verified live
  that, unlike MagicBricks, this pagination does NOT loop back after 100
  pages, so one deep paginated stream per listing-type is sufficient.
- MagicBricks: its "flats" category pagination DOES loop back after page
  100 (page 101+ returns page 1's content — confirmed live, same bug
  `scripts/full_crawl.py` already documented for the generic search).  One
  citywide stream tops out around ~3,000 listings, far short of the
  21,519+/9,783+ live totals — so discovery instead sweeps one locality at a
  time, using the ~470-690 Gurgaon localities MagicBricks itself publishes
  in `sitemap_index.xml` (`srp_flats*.xml.gz`, `srp_rent_Gurgaon*.xml.gz`),
  cached at `data/checkpoints/magicbricks_gurgaon_apartment_localities.json`.

Resumable the same way `full_crawl.py` is: known `source_id`s are read from
MongoDB at start (not from a discovery cache), every URL already known is
skipped before it's ever fetched, and parsed records are committed to Mongo
every `COMMIT_EVERY` — so a kill at any point loses at most one small
in-flight batch, never the whole run. Progress (which page/locality of which
listing-type it's on) is also checkpointed to `data/checkpoints/` so a
restart resumes near where it left off instead of re-paging from page 1.

Rate limit: temporarily raised for this process only, via
`scraper.limiter.set_host_rate(...)` *after* construction (never touching
`host_rps` on the scraper class) — a fresh process for the scheduled
incremental jobs still gets the unmodified 0.33/0.4 default. See --rps.

Usage:
    python scripts/apartment_backfill.py squareyards --rps 0.7
    python scripts/apartment_backfill.py magicbricks --rps 0.8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout.reconfigure(line_buffering=True)

from homz.common.base import ScrapeJob  # noqa: E402
from homz.common.ratelimit import RateLimiter  # noqa: E402
from homz.common.state import StateStore  # noqa: E402
from homz.db import documents as D  # noqa: E402
from homz.db.mongo import close_client, get_database  # noqa: E402
from homz.etl.pipeline import load_records  # noqa: E402

CHECKPOINT_DIR = Path(__file__).resolve().parent.parent / "data" / "checkpoints"
COMMIT_EVERY = 20
CITY = "gurgaon"

# MagicBricks' own pagination loops back to page 1 after this many pages —
# confirmed live (see scripts/full_crawl.py's identical finding for its
# generic search). One locality rarely has enough listings to hit this, but
# cap it as a safety net against ever looping on a single locality forever.
MB_MAX_PAGE_PER_LOCALITY = 100
# SquareYards' apartment pages verified NOT to loop (checked live through
# page 400). The first backfill run hit 700 without ever seeing 3 consecutive
# empty pages (the natural-exhaustion signal) on either rent or sale — i.e.
# real, non-repeating content was still coming back at the cap, so 700 was
# cutting the run short rather than reflecting an actual end. Raised well
# above that; the empty-page check still ends the run promptly once listings
# genuinely run out, so this only matters if there's real content beyond it.
SY_MAX_PAGE = 3000


def _progress_path(source: str, listing_type: str) -> Path:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    return CHECKPOINT_DIR / f"apartment_backfill_{source}_{listing_type}_progress.json"


def _load_progress(source: str, listing_type: str) -> dict:
    path = _progress_path(source, listing_type)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"page": 1, "locality_index": 0}


def _save_progress(source: str, listing_type: str, progress: dict) -> None:
    _progress_path(source, listing_type).write_text(json.dumps(progress), encoding="utf-8")


async def _known_ids(db, source: str) -> set[str]:
    ids: set[str] = set()
    async for doc in db[D.PROPERTIES].find({"source": source}, {"source_id": 1}):
        sid = doc.get("source_id")
        if sid:
            ids.add(sid)
    return ids


class Stats:
    def __init__(self) -> None:
        self.discovered = 0
        self.fetched = 0
        self.inserted = 0
        self.updated = 0
        self.fetch_failed = 0
        self.parse_failed = 0
        self.buffer: list = []
        self.started = time.monotonic()

    async def maybe_commit(self, force: bool = False) -> None:
        if len(self.buffer) >= COMMIT_EVERY or (force and self.buffer):
            res = await load_records(self.buffer)
            self.inserted += res.inserted
            self.updated += res.updated
            print(
                f"[commit] +{len(self.buffer)} -> inserted={self.inserted} "
                f"updated={self.updated} fetched={self.fetched} "
                f"fetch_failed={self.fetch_failed} parse_failed={self.parse_failed} "
                f"elapsed_h={round((time.monotonic() - self.started) / 3600, 2)}",
                flush=True,
            )
            self.buffer = []


async def _process_url(scraper, job, url: str, known: set[str], extract_id, stats: Stats) -> None:
    sid = extract_id(url)
    if sid and sid in known:
        return
    try:
        result = await scraper.fetch_detail(url, job)
    except Exception as exc:  # noqa: BLE001
        stats.fetch_failed += 1
        print(f"[fetch-fail] {url}: {type(exc).__name__}: {str(exc)[:150]}", flush=True)
        return
    stats.fetched += 1
    try:
        records = await scraper.parse_detail(result, job)
    except Exception as exc:  # noqa: BLE001
        stats.parse_failed += 1
        print(f"[parse-fail] {url}: {type(exc).__name__}: {str(exc)[:150]}", flush=True)
        return
    if not records:
        stats.parse_failed += 1
        return
    stats.buffer.extend(records)
    if sid:
        known.add(sid)
    await stats.maybe_commit()


async def run_squareyards(listing_type: str, rps: float | None, limit: int | None = None) -> None:
    from homz.scrapers.squareyards import parser
    from homz.scrapers.squareyards.scraper import SquareYardsScraper

    def extract_id(url: str) -> str | None:
        match = parser._LISTING_ID_RE.search(url)  # noqa: SLF001
        return match.group(1) if match else None

    db = get_database()
    known = await _known_ids(db, "squareyards")
    print(f"[start] squareyards/{listing_type}: {len(known)} already known", flush=True)

    build_url, parse_urls, category = (
        (parser.build_rent_search_url, parser.parse_jsonld_rental_urls, "apartments-for-rent")
        if listing_type == "rent"
        else (parser.build_sale_search_url, parser.parse_resale_urls, "apartments-for-sale")
    )

    job = ScrapeJob(name="apartment-backfill", city=CITY, listing_type=listing_type, max_items=10**9)
    progress = _load_progress("squareyards", listing_type)
    stats = Stats()

    async with SquareYardsScraper(state_store=StateStore(None)) as scraper:
        if rps:
            scraper.limiter.set_host_rate(RateLimiter.host_of(scraper.base_url), rps)

        empty_pages = 0
        page = progress["page"]
        while page <= SY_MAX_PAGE:
            if limit and stats.fetched >= limit:
                print(f"[limit] stopping after {stats.fetched} fetched (--limit {limit})", flush=True)
                break
            search_url = build_url(CITY, page=page, category=category)
            try:
                result = await scraper.fetcher.get(search_url)
            except Exception as exc:  # noqa: BLE001
                print(f"[page-fail] {search_url}: {type(exc).__name__}: {str(exc)[:150]}", flush=True)
                page += 1
                continue

            urls = parse_urls(result.text)
            # A page can be full of URLs and still contribute nothing: past
            # some point (verified live — 700+ pages deep with zero new
            # inserts) the site keeps returning pages, just full of listings
            # already in `known` (unstable sort order re-surfacing the same
            # ones). Checking raw url count here never sees that as "empty"
            # and the loop runs to the page cap without making progress —
            # count against *new* urls instead, same signal MagicBricks'
            # locality loop already uses.
            new_urls = [u for u in urls if not ((sid := extract_id(u)) and sid in known)]
            stats.discovered += len(new_urls)
            if not new_urls:
                empty_pages += 1
                if empty_pages >= 3:
                    print(f"[done] {empty_pages} consecutive pages with no new listings at page={page}", flush=True)
                    break
            else:
                empty_pages = 0

            for url in new_urls:
                await _process_url(scraper, job, url, known, extract_id, stats)

            page += 1
            progress["page"] = page
            _save_progress("squareyards", listing_type, progress)

    await stats.maybe_commit(force=True)
    print(
        f"[final] squareyards/{listing_type}: discovered={stats.discovered} "
        f"fetched={stats.fetched} inserted={stats.inserted} updated={stats.updated} "
        f"fetch_failed={stats.fetch_failed} parse_failed={stats.parse_failed} "
        f"elapsed_h={round((time.monotonic() - stats.started) / 3600, 2)}",
        flush=True,
    )


async def run_magicbricks(listing_type: str, rps: float | None, limit: int | None = None) -> None:
    from homz.scrapers.magicbricks import parser
    from homz.scrapers.magicbricks.scraper import MagicBricksScraper

    locality_file = CHECKPOINT_DIR / "magicbricks_gurgaon_apartment_localities.json"
    localities = json.loads(locality_file.read_text(encoding="utf-8-sig"))[listing_type]

    db = get_database()
    known = await _known_ids(db, "magicbricks")
    print(
        f"[start] magicbricks/{listing_type}: {len(known)} already known, "
        f"{len(localities)} localities to sweep",
        flush=True,
    )

    job = ScrapeJob(name="apartment-backfill", city=CITY, listing_type=listing_type, max_items=10**9)
    progress = _load_progress("magicbricks", listing_type)
    stats = Stats()

    async with MagicBricksScraper(state_store=StateStore(None)) as scraper:
        if rps:
            scraper.limiter.set_host_rate(RateLimiter.host_of(scraper.base_url), rps)

        for i in range(progress["locality_index"], len(localities)):
            if limit and stats.fetched >= limit:
                print(f"[limit] stopping after {stats.fetched} fetched (--limit {limit})", flush=True)
                break
            locality = localities[i]
            empty_pages = 0
            page = 1
            locality_total = 0
            # MagicBricks pagination loops back to page 1's content once it
            # runs out of real pages — confirmed live at the citywide level,
            # and hit live here too (a locality with a handful of real
            # listings still "found" 3,000 riding `has_next_page()`'s weak
            # "any card present" heuristic all the way to the page cap). A
            # page contributing zero URLs not already seen for this locality
            # is therefore treated the same as a genuinely empty page,
            # rather than trusting has_next_page() to know when to stop.
            locality_seen: set[str] = set()
            while page <= MB_MAX_PAGE_PER_LOCALITY:
                search_url = parser.build_apartment_search_url(
                    CITY, listing_type=listing_type, locality=locality, page=page
                )
                try:
                    result = await scraper.fetcher.get(search_url)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[page-fail] {search_url}: {type(exc).__name__}: {str(exc)[:150]}",
                        flush=True,
                    )
                    break

                urls = parser.parse_search_results(result.text)
                new_urls = [u for u in urls if u not in locality_seen]
                stats.discovered += len(new_urls)
                locality_total += len(new_urls)
                if not new_urls:
                    empty_pages += 1
                    if empty_pages >= 2:
                        break
                else:
                    empty_pages = 0
                    locality_seen.update(new_urls)

                for url in new_urls:
                    await _process_url(
                        scraper, job, url, known, parser.extract_listing_id, stats
                    )

                if not urls or not parser.has_next_page(result.text):
                    break
                page += 1

            print(
                f"[locality] {i + 1}/{len(localities)} {locality} -> {locality_total} found",
                flush=True,
            )
            progress["locality_index"] = i + 1
            _save_progress("magicbricks", listing_type, progress)

    await stats.maybe_commit(force=True)
    print(
        f"[final] magicbricks/{listing_type}: discovered={stats.discovered} "
        f"fetched={stats.fetched} inserted={stats.inserted} updated={stats.updated} "
        f"fetch_failed={stats.fetch_failed} parse_failed={stats.parse_failed} "
        f"elapsed_h={round((time.monotonic() - stats.started) / 3600, 2)}",
        flush=True,
    )


async def main(source: str, rps: float | None, limit: int | None) -> None:
    if source == "squareyards":
        await run_squareyards("rent", rps, limit)
        await run_squareyards("sale", rps, limit)
    else:
        await run_magicbricks("rent", rps, limit)
        await run_magicbricks("sale", rps, limit)
    await close_client()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("source", choices=["squareyards", "magicbricks"])
    ap.add_argument("--rps", type=float, default=None, help="Temporary per-host rate override")
    ap.add_argument(
        "--limit", type=int, default=None, help="Stop each listing-type after this many fetches (testing)"
    )
    args = ap.parse_args()
    asyncio.run(main(args.source, args.rps, args.limit))
