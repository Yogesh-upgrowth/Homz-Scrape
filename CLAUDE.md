# Homz-Scrape – notes for Claude

See COMPLIANCE.md for scraping rules (robots.txt, ~0.5 rps per host, SquareYards 0.33, no evasion, stop on block).

## Listing images: "images coming soon" placeholder

### How the feed gets images
- Mongo warehouse (`properties`, `projects`, `property_images`) -> `homz export feed` writes `data/feed/*.json` -> committed -> Vercel build runs `node scripts/split-feed.js` into static CDN parts (1.4 MB each). Auto-deploys from `main` (Vercel project `homz-scrape`, team `yogesh4`; GitHub `Yogesh-upgrowth/Homz-Scrape`).
- `src/homz/services/feed.py`: `_public_url` returns only `blob_url` (never the portal URL, so no competitor watermark ships). If a listing has no `blob_url` images, `_split_images` falls back to `_placeholder_gallery()` = the "images coming soon" image (blob path `listings/3d/29/3d29ea7b...877d.webp`). Only `ggn` is published (`feed_cities`).
- A listing shows the placeholder when none of its images has been downloaded, de-watermarked, branded and uploaded to Blob.

### Pipeline to remove placeholders (run in this order)
1. `python scripts/rescrape_missing_images.py --source magicbricks` (live re-fetch of listings in `data/checkpoints/rescrape_targets.json`; records photo URLs only; resumable via `data/checkpoints/rescrape_missing_<source>.json`). Flags: `--source {magicbricks,squareyards} --rps --limit --dry-run`. SquareYards path is untested.
   - Why live: MagicBricks builds its gallery client-side, so archived HTML often has no photos.
2. `python scripts/ingest_property_images.py --all-types --reset` (then without `--reset`, in chunks via `--limit 250-800`) – downloads, removes the portal watermark (calibration/LaMa inpaint), adds the homzrealtor mark, WebP, uploads to Vercel Blob, writes `property_images` + `properties.images[].blob_url`. Checkpoint: `data/checkpoints/property_images_<source>.json`.
   - `--all-types` must stay wired: it drops the `property_type`/`listing_type` filters of `BASE_FILTER` (it was once a silent no-op).
   - Junk is filtered by `src/homz/images/urls.py` (agent headshots `/topagent/`, `/user/`, badges, gifs); min edge 200 px. Listings with only junk correctly stay on the placeholder.
3. `python scripts/sync_blob_urls.py`, then `python scripts/sync_blob_urls.py --projects` (links archive images and project images to already-processed URLs).
4. Recount: regenerate to a temp dir (`python -m homz.cli export feed --out "$TEMP/feed_now"`) and compare with `data/feed` by `id` (properties) / `projectUrl` (projects), counting images containing the placeholder hash.
5. Publish: `python -m homz.cli export feed` into `data/feed`, commit, push to `main`, confirm the Vercel deployment is READY (CDN parts cache `s-maxage=604800`).

### Status (as of 2026-10-20)
- Placeholders went from 10,280 to 4,171 listings of 36,549 (commit 6ae2411 published).
- Remaining 4,171: MagicBricks no photos scraped 2,427; SquareYards photos not processed 1,133 (+7 junk); MagicBricks headshots-only 144; MagicBricks unprocessed 49; 411 projects (no project image script yet).
- Re-scrape of the MagicBricks targets: 2,080 of 2,620 done, about 930 listings now have new photo URLs not yet downloaded/branded. Remaining 540. Hit rate has risen each run.
- 100% is unreachable: some listings have no photos on the portal, and some are removed (404/410).

### Operational rules
- Claude Code kills long background commands on low system memory (~20-30 min). Everything is checkpointed. After a memory stop, do NOT restart unprompted; wait for the user to say "run again".
- Use `PYTHONIOENCODING=utf-8` on Windows (the rupee sign breaks CP1252).
- Untracked scratch files `_home*.html`, `_localhome*.html`, `_oldapi.json`, `_rentlive.html` are intentionally not committed.
- Logs in `data/logs/`.
