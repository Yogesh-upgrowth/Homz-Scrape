"""Re-fetch only the listings still showing "images coming soon" and record their photos.

Targets come from `data/checkpoints/rescrape_targets.json` (`pids`: property
ids whose feed gallery is the placeholder). Each listing's detail page is
fetched live through the normal scraper, so robots.txt, the per-host rate and
block detection all apply. If the fresh parse finds real photos, only the
`images` field is updated; the existing ingest then downloads them, removes
the portal watermark, adds the homzrealtor mark and stores them:

    python scripts/rescrape_missing_images.py --source magicbricks
    python scripts/ingest_property_images.py --all-types --reset
    python scripts/sync_blob_urls.py

Why a live fetch: MagicBricks builds its gallery client-side, so the archived
server HTML often has no photos while a fresh fetch does. Replaying archived
HTML found none in a 60-listing sample; a live fetch found photos for 6 of 10.

A fresh parse that finds no photos never overwrites what is stored. Progress
is checkpointed per listing id, so an interrupted run resumes where it stopped.

Usage:
    python scripts/rescrape_missing_images.py --source magicbricks --limit 20
    python scripts/rescrape_missing_images.py --source magicbricks --rps 0.5
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
from homz.common.schema import PropertyRecord  # noqa: E402
from homz.db import documents as D  # noqa: E402
from homz.db.mongo import close_client, get_database  # noqa: E402
from homz.images.urls import is_photo  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ROOT / "data" / "checkpoints" / "rescrape_targets.json"


def _checkpoint(source: str) -> Path:
    return ROOT / "data" / "checkpoints" / f"rescrape_missing_{source}.json"


def _done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8-sig")).get("done", []))
    except (OSError, ValueError):
        return set()


def _scraper(source: str, rps: float | None):
    if source == "magicbricks":
        from homz.scrapers.magicbricks.scraper import MagicBricksScraper

        scraper = MagicBricksScraper()
    else:
        from homz.scrapers.squareyards.scraper import SquareYardsScraper

        scraper = SquareYardsScraper()
    if rps:
        for host in getattr(scraper, "hosts", ()) or ():
            scraper.limiter.set_host_rate(host, rps)
    return scraper


async def main(source: str, rps: float | None, limit: int | None, dry_run: bool) -> None:
    ids = [p for p in json.loads(TARGETS.read_text())["pids"] if p.startswith(f"{source}:")]
    ckpt = _checkpoint(source)
    done = _done(ckpt)
    todo = [p for p in ids if p not in done]
    print(f"{source}: {len(ids)} targets, {len(done)} already done, {len(todo)} to go")
    if limit:
        todo = todo[:limit]

    db = get_database()
    scraper = _scraper(source, rps)
    job = ScrapeJob(name="rescrape-missing-images", city="gurgaon",
                    listing_type="sale", max_items=10**9)
    await scraper.__aenter__()
    found = empty = failed = 0
    started = time.monotonic()
    try:
        for n, pid in enumerate(todo, start=1):
            doc = await db[D.PROPERTIES].find_one(
                {"_id": pid}, {"listing_url": 1, "images": 1})
            if not doc or not doc.get("listing_url"):
                done.add(pid)
                continue
            try:
                fetched = await scraper.fetch_detail(doc["listing_url"], job)
                records = await scraper.parse_detail(fetched, job)
                rec = next((r for r in records if isinstance(r, PropertyRecord)), None)
            except Exception as exc:  # noqa: BLE001 - one bad page is not fatal
                failed += 1
                print(f"[fetch-fail] {pid}: {type(exc).__name__}: {str(exc)[:100]}")
                if any(code in str(exc) for code in ("404", "410")):
                    done.add(pid)   # removed from the portal; retrying cannot help
                continue            # anything else is not marked done, so a rerun retries it
            photos = [i for i in (rec.images if rec else []) if is_photo(i.url)]
            if photos:
                found += 1
                if not dry_run:
                    kept = [i for i in (doc.get("images") or []) if i.get("blob_url")]
                    fresh = [i.model_dump(mode="python") for i in photos]
                    have = {i["url"] for i in fresh}
                    await db[D.PROPERTIES].update_one(
                        {"_id": pid},
                        {"$set": {"images": fresh + [k for k in kept if k["url"] not in have]}})
            else:
                empty += 1
            done.add(pid)
            if n % 20 == 0:
                if not dry_run:
                    ckpt.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")
                el = (time.monotonic() - started) / 60
                print(f"[{n}/{len(todo)}] photos_found={found} still_none={empty} "
                      f"failed={failed} | {el:.1f}m ({n / max(el, 1e-9):.1f}/min)")
    finally:
        if not dry_run:
            ckpt.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")
        await scraper.__aexit__(None, None, None)
        await close_client()
    print(f"\ndone: photos_found={found} still_none={empty} failed={failed}"
          f"{' (dry run)' if dry_run else ''}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, choices=["magicbricks", "squareyards"])
    ap.add_argument("--rps", type=float, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.source, a.rps, a.limit, a.dry_run))
