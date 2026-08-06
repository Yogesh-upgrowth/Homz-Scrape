"""Resumable full-catalog crawl driver.

The CLI's `homz scrape source` path re-discovers from scratch every run and
its `stop_after_known` heuristic (25 consecutive already-seen pages) is built
for *daily incremental refresh*, not for filling a multi-thousand-item
backlog: discovery yields URLs in a stable order, so a second run immediately
re-hits the same already-known URLs at the front of that order and gives up
after 25 of them, long before ever reaching the unfetched tail.

This script instead treats MongoDB itself as the resume point — it fetches
the set of `source_id`s already stored, skips those during discovery, and
commits every ~30 new records immediately (not just at job end) — so a kill
at any point loses at most the current small batch, never the whole run.
Discovery results are cached to `data/checkpoints/` since MagicBricks'
listing-page discovery alone costs ~100 rate-limited requests.

Usage:
    python scripts/full_crawl.py squareyards --city gurgaon --chunk-size 150 --budget-seconds 480
    python scripts/full_crawl.py magicbricks --city gurgaon --listing-type sale --chunk-size 150 --budget-seconds 480
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from homz.common.base import ScrapeJob  # noqa: E402
from homz.common.state import ScrapeState, StateStore  # noqa: E402
from homz.db.mongo import get_database  # noqa: E402
from homz.etl.pipeline import load_records  # noqa: E402

CHECKPOINT_DIR = Path(__file__).resolve().parent.parent / "data" / "checkpoints"
COMMIT_EVERY = 30
MAGICBRICKS_MAX_PAGE = 100  # confirmed live: page 101+ loops back to page 1


def _scraper_and_extractor(source: str):
    if source == "squareyards":
        from homz.scrapers.squareyards import parser
        from homz.scrapers.squareyards.scraper import SquareYardsScraper

        return SquareYardsScraper, (lambda url: parser.extract_project_id(url))
    if source == "magicbricks":
        from homz.scrapers.magicbricks import parser
        from homz.scrapers.magicbricks.scraper import MagicBricksScraper

        return MagicBricksScraper, (lambda url: parser.extract_listing_id(url))
    raise ValueError(f"unsupported source: {source}")


def _normalize_known_id(source: str, raw_id: str) -> str:
    if source == "squareyards" and raw_id.startswith("project:"):
        return raw_id.split(":", 1)[1]
    return raw_id


async def _known_ids(db, source: str) -> set[str]:
    ids: set[str] = set()
    for coll in ("projects", "properties"):
        async for doc in db[coll].find({"source": source}, {"source_id": 1}):
            sid = doc.get("source_id")
            if sid:
                ids.add(_normalize_known_id(source, sid))
    return ids


def _cache_path(source: str, city: str, listing_type: str | None) -> Path:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{source}_{city}_{listing_type or 'na'}_urls.json"
    return CHECKPOINT_DIR / name


async def _discover_all(scraper, job, source: str, city: str, listing_type: str | None) -> list[str]:
    cache = _cache_path(source, city, listing_type)
    if cache.exists():
        urls = json.loads(cache.read_text(encoding="utf-8"))
        print(f"[discover] loaded {len(urls)} URLs from cache {cache}", flush=True)
        return urls

    state = ScrapeState(source=source, job=job.key)
    urls: list[str] = []
    async for url in scraper.discover(job, state):
        urls.append(url)
    cache.write_text(json.dumps(urls), encoding="utf-8")
    print(f"[discover] found {len(urls)} URLs total, cached to {cache}", flush=True)
    return urls


async def run(
    source: str,
    city: str,
    listing_type: str | None,
    chunk_size: int,
    budget_seconds: float,
) -> None:
    scraper_cls, extract_id = _scraper_and_extractor(source)
    db = get_database()
    known = await _known_ids(db, source)
    print(f"[start] {source}/{city}: {len(known)} already known", flush=True)

    if source == "squareyards":
        job = ScrapeJob(name="fullcrawl", city=city, max_pages=3, max_items=100_000)
    else:
        job = ScrapeJob(
            name="fullcrawl", city=city, listing_type=listing_type,
            max_pages=MAGICBRICKS_MAX_PAGE, max_items=100_000, incremental=False,
        )

    async with scraper_cls(state_store=StateStore(None)) as scraper:
        all_urls = await _discover_all(scraper, job, source, city, listing_type)

        remaining = [u for u in all_urls if extract_id(u) not in known]
        print(f"[plan] {len(all_urls)} total, {len(remaining)} not yet scraped", flush=True)

        buffer = []
        processed = 0
        fetch_failed = 0
        parse_failed = 0
        started = time.monotonic()

        for url in remaining:
            if processed >= chunk_size or (time.monotonic() - started) > budget_seconds:
                break

            sid = extract_id(url)
            if sid and sid in known:
                continue

            try:
                result = await scraper.fetch_detail(url, job)
            except Exception as exc:  # noqa: BLE001
                fetch_failed += 1
                print(f"[fetch-fail] {url}: {type(exc).__name__}: {str(exc)[:150]}", flush=True)
                continue

            try:
                records = await scraper.parse_detail(result, job)
            except Exception as exc:  # noqa: BLE001
                parse_failed += 1
                print(f"[parse-fail] {url}: {type(exc).__name__}: {str(exc)[:150]}", flush=True)
                continue

            if not records:
                parse_failed += 1
                continue

            buffer.extend(records)
            if sid:
                known.add(sid)
            processed += 1

            if len(buffer) >= COMMIT_EVERY:
                res = await load_records(buffer)
                print(
                    f"[commit] {len(buffer)} records -> +{res.inserted} inserted, "
                    f"~{res.updated} updated ({processed}/{min(chunk_size, len(remaining))} this chunk)",
                    flush=True,
                )
                buffer = []

        if buffer:
            res = await load_records(buffer)
            print(f"[commit] {len(buffer)} records -> +{res.inserted} inserted, ~{res.updated} updated", flush=True)

    left = len(remaining) - processed
    print(
        f"[done] processed={processed} fetch_failed={fetch_failed} parse_failed={parse_failed} "
        f"elapsed_s={round(time.monotonic() - started, 1)} remaining_after_chunk={max(left, 0)}",
        flush=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", choices=["squareyards", "magicbricks"])
    ap.add_argument("--city", required=True)
    ap.add_argument("--listing-type", default="sale")
    ap.add_argument("--chunk-size", type=int, default=150)
    ap.add_argument("--budget-seconds", type=float, default=480)
    args = ap.parse_args()

    asyncio.run(
        run(args.source, args.city, args.listing_type, args.chunk_size, args.budget_seconds)
    )


if __name__ == "__main__":
    main()
