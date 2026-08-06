"""Command-line entry point.

    homz scrape all --dry-run
    homz scrape magicbricks --city gurgaon --listing-type sale --max-items 50
    homz etl run
    homz enrich run --no-llm
    homz db init
    homz search "3 bhk sector 82 gurgaon"
    homz ops status
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from homz.common.base import ScrapeJob
from homz.logging_setup import configure_logging, get_logger
from homz.settings import settings

app = typer.Typer(
    name="homz",
    help="Homz Realtor — real estate intelligence pipeline for Delhi NCR.",
    no_args_is_help=True,
    add_completion=False,
)
scrape_app = typer.Typer(help="Run scrapers.", no_args_is_help=True)
etl_app = typer.Typer(help="ETL and aggregation jobs.", no_args_is_help=True)
enrich_app = typer.Typer(help="AI enrichment pipeline.", no_args_is_help=True)
db_app = typer.Typer(help="Database maintenance.", no_args_is_help=True)
ops_app = typer.Typer(help="Operational inspection.", no_args_is_help=True)
export_app = typer.Typer(help="Publish the warehouse to downstream consumers.", no_args_is_help=True)

app.add_typer(scrape_app, name="scrape")
app.add_typer(etl_app, name="etl")
app.add_typer(enrich_app, name="enrich")
app.add_typer(db_app, name="db")
app.add_typer(ops_app, name="ops")
app.add_typer(export_app, name="export")

console = Console()
log = get_logger(__name__)


@app.callback()
def main(
    log_level: str = typer.Option(None, "--log-level", help="DEBUG | INFO | WARNING | ERROR"),
    json_logs: bool = typer.Option(False, "--json-logs", help="Emit structured JSON logs"),
) -> None:
    configure_logging(level=log_level, json_logs=json_logs or settings.log_json)


def _print_json(payload: Any) -> None:
    console.print_json(json.dumps(payload, default=str, indent=2))


# ===========================================================================
# scrape
# ===========================================================================


@scrape_app.command("all")
def scrape_all(
    dry_run: bool = typer.Option(False, "--dry-run", help="Scrape but do not write to the DB"),
    parallel: bool = typer.Option(False, "--parallel", help="Run sources concurrently"),
    sources: list[str] = typer.Option(None, "--source", "-s", help="Limit to these sources"),
) -> None:
    """Run every registered source once."""
    from homz.etl.pipeline import run_all_sources

    results = asyncio.run(
        run_all_sources(sources or None, dry_run=dry_run, sequential=not parallel)
    )
    _render_pipeline_results(results)


@scrape_app.command("source")
def scrape_source(
    source: str = typer.Argument(..., help="magicbricks | housing | squareyards | reddit"),
    city: str = typer.Option(None, "--city"),
    listing_type: str = typer.Option(None, "--listing-type", help="sale | rent"),
    property_type: str = typer.Option(None, "--property-type"),
    max_pages: int = typer.Option(3, "--max-pages"),
    max_items: int = typer.Option(100, "--max-items"),
    full: bool = typer.Option(False, "--full", help="Ignore incremental state"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Run a single source, optionally with an ad-hoc job."""
    from homz.etl.pipeline import run_source

    jobs = None
    if city or listing_type or property_type:
        jobs = [
            ScrapeJob(
                name=listing_type or "adhoc",
                city=city,
                listing_type=listing_type,
                property_type=property_type,
                max_pages=max_pages,
                max_items=max_items,
                incremental=not full,
                params={"subreddit": city} if source == "reddit" and city else {},
            )
        ]

    result = asyncio.run(run_source(source, jobs=jobs, dry_run=dry_run))
    _render_pipeline_results([result])


@scrape_app.command("list")
def scrape_list() -> None:
    """Show registered sources and their default jobs."""
    from homz.scrapers import SCRAPERS

    table = Table("source", "browser", "rps", "default jobs")
    for name, cls in SCRAPERS.items():
        jobs = cls.default_jobs if name != "reddit" else cls.build_jobs()
        table.add_row(
            name,
            "yes" if cls.needs_browser else "no",
            str(cls.host_rps or settings.per_host_rps),
            ", ".join(j.key for j in jobs) or "(built at runtime)",
        )
    console.print(table)


# ===========================================================================
# etl
# ===========================================================================


@etl_app.command("run")
def etl_run(
    stale_days: int = typer.Option(21, "--stale-days", help="Delist listings unseen for N days"),
    skip_views: bool = typer.Option(False, "--skip-views"),
    skip_prune: bool = typer.Option(False, "--skip-prune"),
) -> None:
    """Post-load maintenance: delist stale rows, refresh rollups, prune raw HTML."""
    from homz.etl.pipeline import backfill_locality_aggregates, finalize

    async def _run() -> dict[str, Any]:
        summary = await finalize(
            mark_stale_days=stale_days,
            refresh_views=not skip_views,
            prune_raw=not skip_prune,
        )
        summary["locations_updated"] = await backfill_locality_aggregates()
        return summary

    _print_json(asyncio.run(_run()))


@etl_app.command("insights")
def etl_insights(days: int = typer.Option(90, "--days")) -> None:
    """Compute price/supply/yield trends into `market_insights`."""
    from homz.etl.price_history import generate_market_insights

    written = asyncio.run(generate_market_insights(days=days))
    console.print(f"[green]wrote {written} market insight rows[/green]")


@etl_app.command("dedupe")
def etl_dedupe(limit: int = typer.Option(5000, "--limit")) -> None:
    """Re-run cross-source dedupe over active listings.

    Documents sharing a `dedupe_key` are the same unit by construction, so the
    richest one becomes canonical and the rest point at it.
    """
    from pymongo import UpdateOne

    from homz.db import documents as D
    from homz.db.mongo import get_database

    async def _run() -> dict[str, Any]:
        db = get_database()
        pipeline = [
            {"$match": {"is_active": True, "dedupe_key": {"$ne": None},
                        "canonical_id": None}},
            {"$addFields": {"_completeness": {"$add": [
                {"$cond": [{"$ne": ["$title", None]}, 1, 0]},
                {"$cond": [{"$ne": ["$description", None]}, 1, 0]},
                {"$cond": [{"$ne": ["$rera_number", None]}, 1, 0]},
                {"$size": {"$ifNull": ["$images", []]}},
            ]}}},
            {"$sort": {"_completeness": -1, "last_seen_at": -1}},
            {"$group": {"_id": "$dedupe_key", "ids": {"$push": "$_id"}}},
            {"$match": {"$expr": {"$gt": [{"$size": "$ids"}, 1]}}},
            {"$limit": limit},
        ]
        clusters = await db[D.PROPERTIES].aggregate(
            pipeline, allowDiskUse=True
        ).to_list(length=limit)

        operations, links = [], []
        for cluster in clusters:
            canonical, *duplicates = cluster["ids"]
            for duplicate in duplicates:
                operations.append(UpdateOne(
                    {"_id": duplicate}, {"$set": {"canonical_id": canonical}}
                ))
                links.append(UpdateOne(
                    {"canonical_id": canonical, "duplicate_id": duplicate},
                    {"$set": {"score": 1.0, "reason": "identical dedupe_key"}},
                    upsert=True,
                ))
        if operations:
            await db[D.PROPERTIES].bulk_write(operations, ordered=False)
            await db[D.PROPERTY_DUPLICATES].bulk_write(links, ordered=False)
        return {"clusters": len(clusters), "linked": len(operations)}

    _print_json(asyncio.run(_run()))


# ===========================================================================
# enrich
# ===========================================================================


@enrich_app.command("run")
def enrich_run(
    llm: bool = typer.Option(True, "--llm/--no-llm", help="Use the Claude tier"),
    batch: bool = typer.Option(
        None, "--batch/--no-batch", help="Use the Batches API (50% cheaper)"
    ),
    limit: int = typer.Option(None, "--limit", help="Max rows for the LLM tier"),
) -> None:
    """Run the full enrichment pipeline."""
    from homz.db.mongo import get_database
    from homz.enrichment.pipeline import EnrichmentPipeline

    async def _run() -> dict[str, Any]:
        pipeline = EnrichmentPipeline(get_database(), use_llm=llm, use_batch=batch)
        try:
            report = await pipeline.run_all(llm_limit=limit)
            return report.as_dict()
        finally:
            await pipeline.aclose()

    _print_json(asyncio.run(_run()))


@enrich_app.command("scores")
def enrich_scores(
    force: bool = typer.Option(False, "--force", help="Rescore everything, not just pending")
) -> None:
    """Recompute deterministic scores only (no LLM calls, no cost)."""
    from homz.db.mongo import get_database
    from homz.enrichment.pipeline import EnrichmentPipeline

    async def _run() -> dict[str, int]:
        pipeline = EnrichmentPipeline(get_database(), use_llm=False)
        builders = await pipeline.score_builders()
        properties = await pipeline.score_properties(force=force)
        return {"builders": builders, "properties": properties}

    _print_json(asyncio.run(_run()))


@enrich_app.command("estimate")
def enrich_estimate(limit: int = typer.Option(100, "--limit")) -> None:
    """Estimate LLM cost for the pending backlog before spending anything."""
    from homz.db import documents as D
    from homz.db.mongo import get_database
    from homz.enrichment import prompts
    from homz.enrichment.llm import LLMClient, LLMRequest, LLMUsage, estimate_cost

    async def _run() -> dict[str, Any]:
        db = get_database()
        pending = await db[D.REDDIT_POSTS].count_documents({"enriched_at": None})
        sample = await db[D.REDDIT_POSTS].find(
            {"enriched_at": None}, projection={"title": 1, "body": 1}
        ).sort("score", -1).limit(min(limit, 20)).to_list(length=20)

        if not sample:
            return {"pending": 0, "note": "nothing to enrich"}

        client = LLMClient()
        try:
            counts = []
            for row in sample:
                request = LLMRequest(
                    custom_id="estimate",
                    system=prompts.REDDIT_SYSTEM_PROMPT,
                    user=prompts.reddit_user_prompt(
                        title=row["title"], body=row["body"], comments=[]
                    ),
                    schema=prompts.REDDIT_SCHEMA,
                )
                counts.append(await client.count_tokens(request))
        finally:
            await client.aclose()

        avg_input = sum(counts) / len(counts)
        projected = LLMUsage(
            input_tokens=int(avg_input * pending),
            output_tokens=int(400 * pending),
            requests=int(pending),
        )
        cost = estimate_cost(projected)
        return {
            "pending_rows": int(pending),
            "avg_input_tokens": round(avg_input),
            "projected_cost_usd": cost,
            "projected_cost_batch_usd": {k: round(v / 2, 4) for k, v in cost.items()},
            "model": settings.llm_model,
        }

    _print_json(asyncio.run(_run()))


# ===========================================================================
# db
# ===========================================================================


@db_app.command("init")
def db_init(
    force_backend: str = typer.Option(
        None, "--backend", help="atlas | text (default: probe the server)"
    ),
) -> None:
    """Create collections, indexes and Atlas Search indexes. Idempotent."""
    from homz.db.documents import ensure_schema
    from homz.db.mongo import detect_backend, get_database

    async def _run() -> dict[str, Any]:
        backend = force_backend or await detect_backend()
        report = await ensure_schema(get_database(), backend=backend)
        report["backend"] = backend
        return report

    report = asyncio.run(_run())
    _print_json(report)
    if report.get("warnings"):
        console.print("[yellow]warnings above — schema is usable but check them[/yellow]")
    if report.get("backend") == "atlas":
        console.print(
            "[dim]Atlas Search indexes build asynchronously — "
            "run `homz db search-status` until queryable=true.[/dim]"
        )


@db_app.command("search-status")
def db_search_status() -> None:
    """Atlas Search index build state.

    A freshly created index returns zero results until `queryable` is true,
    which otherwise looks exactly like a broken search.
    """
    from homz.db.documents import search_index_status
    from homz.db.mongo import get_database

    rows = asyncio.run(search_index_status(get_database()))
    if not rows:
        console.print("[yellow]no search indexes found[/yellow]")
        return
    table = Table("collection", "index", "status", "queryable")
    for row in rows:
        queryable = row.get("queryable")
        table.add_row(
            row.get("collection", ""),
            row.get("name", row.get("error", "")),
            str(row.get("status", "")),
            "[green]yes[/green]" if queryable else "[yellow]building[/yellow]",
        )
    console.print(table)


@db_app.command("set-uri")
def db_set_uri(
    uri: str = typer.Argument(..., help="Atlas connection string (quote it!)"),
    env_file: Path = typer.Option(Path(".env"), "--env-file"),
) -> None:
    """Write HOMZ_MONGODB_URI into .env, then verify it.

    Exists because the Atlas string contains `&`, which most shells read as
    "background this command" — pasting it unquoted silently truncates the URI
    at the first `&`. This validates the shape before writing.
    """
    import re

    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        console.print("[red]not a MongoDB URI — expected mongodb:// or mongodb+srv://[/red]")
        raise typer.Exit(1)
    if "USER:PASSWORD" in uri or "xxxxx" in uri:
        console.print("[red]that is still the placeholder, not your real string[/red]")
        raise typer.Exit(1)
    if "&" not in uri and "retryWrites" in uri:
        console.print(
            "[yellow]warning: the URI has 'retryWrites' but no '&' — if you pasted "
            "unquoted, your shell may have truncated it. Wrap it in single quotes.[/yellow]"
        )
    if "@" not in uri:
        console.print("[yellow]warning: no credentials in the URI (no '@')[/yellow]")

    if not env_file.exists():
        example = Path(".env.example")
        if not example.exists():
            console.print(f"[red]{env_file} not found and no .env.example to copy[/red]")
            raise typer.Exit(1)
        env_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        console.print(f"created {env_file} from .env.example")

    lines = env_file.read_text(encoding="utf-8").splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("HOMZ_MONGODB_URI="):
            lines[i] = f"HOMZ_MONGODB_URI={uri}"
            replaced = True
            break
    if not replaced:
        lines.append(f"HOMZ_MONGODB_URI={uri}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    redacted = re.sub(r"://[^@/]+@", "://***:***@", uri)
    console.print(f"[green]wrote[/green] {env_file}: {redacted}")
    console.print("now run: [cyan]homz db ping[/cyan] then [cyan]homz db init[/cyan]")


@db_app.command("ping")
def db_ping() -> None:
    """Test the Atlas connection and explain any failure.

    Atlas problems all look like the same timeout; this maps each cause to the
    thing you actually have to change.
    """
    from homz.db.mongo import diagnose

    report = asyncio.run(diagnose())
    _print_json(report)
    if report.get("ok"):
        backend = report.get("backend")
        console.print(f"[green]connected[/green] — search backend: [cyan]{backend}[/cyan]")
        if backend != "atlas":
            console.print(
                "[yellow]not an Atlas cluster: search falls back to $text "
                "(no fuzzy/typo tolerance)[/yellow]"
            )
    else:
        console.print(f"[red]{report.get('error', 'connection failed')}[/red]")
        if report.get("fix"):
            console.print(f"[yellow]{report['fix']}[/yellow]")
        raise typer.Exit(1)


@db_app.command("check")
def db_check() -> None:
    """Verify connectivity and print document counts."""
    from homz.db.mongo import get_database, healthcheck, server_info
    from homz.db.repository import Repository

    async def _run() -> dict[str, Any]:
        ok = await healthcheck()
        if not ok:
            return {"database": False, "uri": settings.redacted_mongodb_uri}
        return {
            "database": True,
            "uri": settings.redacted_mongodb_uri,
            "server": await server_info(),
            "counts": await Repository(get_database()).counts(),
        }

    _print_json(asyncio.run(_run()))


@db_app.command("refresh-views")
def db_refresh_views() -> None:
    """Rebuild the four rollup collections."""
    from homz.db.mongo import get_database
    from homz.db.repository import Repository

    asyncio.run(Repository(get_database()).refresh_market_views())
    console.print("[green]rollups refreshed[/green]")


@db_app.command("reset")
def db_reset(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt")
) -> None:
    """Drop every collection. Destructive."""
    from homz.db.documents import drop_all
    from homz.db.mongo import get_database

    if not yes:
        typer.confirm(
            f"Drop ALL collections in {settings.mongodb_database} "
            f"({settings.redacted_mongodb_uri})?",
            abort=True,
        )
    dropped = asyncio.run(drop_all(get_database()))
    console.print(f"[red]dropped {len(dropped)} collections[/red]")


# ===========================================================================
# search / ops
# ===========================================================================


@app.command("search")
def search_cli(
    query: str = typer.Argument(..., help="Free-text search"),
    city: str = typer.Option(None, "--city"),
    listing_type: str = typer.Option(None, "--listing-type"),
    limit: int = typer.Option(10, "--limit"),
) -> None:
    """Query the warehouse from the terminal."""
    from homz.db.mongo import get_database
    from homz.search.query import PropertySearchQuery, search_properties

    async def _run() -> tuple[list[dict[str, Any]], int]:
        return await search_properties(
            get_database(),
            PropertySearchQuery(
                q=query, city=city, listing_type=listing_type, page_size=limit
            ),
        )

    results, total = asyncio.run(_run())
    from homz.common.parsing import format_price_inr

    table = Table("project", "config", "price", "₹/sqft", "locality", "score")
    for row in results:
        table.add_row(
            (row.get("project_name") or row.get("title") or "")[:40],
            row.get("configuration") or "",
            format_price_inr(row.get("price") or row.get("rent_monthly")) or "-",
            str(row.get("price_per_sqft") or "-"),
            f"{row.get('sector') or row.get('locality') or ''}, {row.get('city') or ''}",
            str(row.get("investment_score") or "-"),
        )
    console.print(table)
    console.print(f"[dim]{total} total matches[/dim]")


@ops_app.command("status")
def ops_status() -> None:
    """Recent run history and current warehouse size."""
    from homz.db import documents as D
    from homz.db.mongo import get_database
    from homz.db.repository import Repository

    async def _run() -> tuple[dict[str, int], list[dict[str, Any]]]:
        db = get_database()
        counts = await Repository(db).counts()
        runs = await db[D.SCRAPE_RUNS].find(
            projection={"_id": 0, "source": 1, "job": 1, "status": 1, "started_at": 1,
                        "duration_s": 1, "parsed": 1, "errors": 1, "blocked": 1},
        ).sort("started_at", -1).limit(20).to_list(length=20)
        return counts, runs

    counts, runs = asyncio.run(_run())

    counts_table = Table("collection", "documents")
    for name, value in counts.items():
        counts_table.add_row(name, f"{value:,}")
    console.print(counts_table)

    runs_table = Table("source", "job", "status", "started", "sec", "parsed", "err", "blk")
    for run in runs:
        status = run["status"]
        colour = {"success": "green", "partial": "yellow"}.get(status, "red")
        runs_table.add_row(
            run["source"],
            run["job"][:28],
            f"[{colour}]{status}[/{colour}]",
            str(run["started_at"])[:19],
            str(run["duration_s"] or ""),
            str(run["parsed"]),
            str(run["errors"]),
            str(run["blocked"]),
        )
    console.print(runs_table)


@ops_app.command("raw")
def ops_raw(
    key: str = typer.Argument(None, help="Archive key to dump"),
    prune: bool = typer.Option(False, "--prune", help="Delete partitions past retention"),
) -> None:
    """Inspect or prune the raw-HTML archive."""
    from homz.common.rawstore import RawStore

    store = RawStore()
    if prune:
        removed = store.prune()
        console.print(f"[green]pruned {removed} partitions[/green]")
        return
    if key:
        content = store.get_text(key)
        if content is None:
            console.print(f"[red]not found: {key}[/red]")
            raise typer.Exit(1)
        console.print(content[:20_000])
        return
    console.print(
        f"archive root: {store.root}\nsize: {store.usage_bytes() / 1e6:.1f} MB\n"
        f"retention: {settings.raw_html_retention_days} days"
    )


@ops_app.command("tasks")
def ops_tasks(
    claim: int = typer.Option(0, "--claim", help="Claim N tasks and print them"),
    worker: str = typer.Option("cli", "--worker"),
) -> None:
    """Inspect (or claim from) the on-demand fill queue."""
    from homz.db.mongo import get_database
    from homz.services.ondemand import DemandFiller

    async def _run() -> dict[str, Any]:
        filler = DemandFiller(get_database())
        payload: dict[str, Any] = {"stats": await filler.stats()}
        if claim:
            payload["claimed"] = await filler.claim(worker=worker, limit=claim)
        return payload

    _print_json(asyncio.run(_run()))


@ops_app.command("config")
def ops_config() -> None:
    """Print effective configuration (secrets redacted)."""
    data = settings.model_dump()
    for key in ("reddit_client_secret", "reddit_client_id", "database_url", "proxies"):
        if data.get(key):
            data[key] = "***redacted***"
    _print_json(data)


def _render_pipeline_results(results: list[Any]) -> None:
    any_dry_run = any(r.as_dict().get("dry_run") for r in results)
    if any_dry_run:
        console.print(
            "[yellow]DRY RUN — parsed records were discarded, nothing was "
            "written to the database.[/yellow]\n"
            "[dim]'parsed' is the number that tells you scraping works. "
            "Re-run without --dry-run to persist.[/dim]"
        )

    table = Table("source", "parsed", "inserted", "updated", "failed", "dupes", "sec")
    for result in results:
        payload = result.as_dict()
        parsed = sum(r["parsed"] for r in payload["reports"]) if payload["reports"] else 0
        load = payload["load"]
        skipped = "[dim]—[/dim]" if payload.get("dry_run") else None
        table.add_row(
            payload["source"],
            f"[green]{parsed}[/green]" if parsed else "[red]0[/red]",
            skipped or str(load["inserted"]),
            skipped or str(load["updated"]),
            skipped or str(load["failed"]),
            skipped or str(load["duplicates_linked"]),
            str(payload["duration_s"]),
        )
    console.print(table)

    for result in results:
        payload = result.as_dict()
        for report in payload["reports"]:
            if report["status"] != "success":
                console.print(
                    f"[yellow]{payload['source']}/{report['job']}: {report['status']}[/yellow] "
                    f"errors={report['errors']} blocked={report['blocked']}"
                )
                for sample in report.get("error_samples", [])[:3]:
                    console.print(f"  [dim]{sample}[/dim]")


@export_app.command("feed")
def export_feed(
    out: str = typer.Option("./data/feed", "--out", help="Directory to write segment JSON into"),
    limit: int = typer.Option(500, "--limit", help="Records per page, matching the API default"),
    indent: bool = typer.Option(False, "--indent", help="Pretty-print (larger files)"),
) -> None:
    """Write the website's `/api/data` payloads from the warehouse.

    Produces one file per city segment for the Projects catalogue
    (`ggnResidentialProjects.json`, …) plus one per city+category for
    individual listings (`ggnSaleProperties.json`, `ggnRentProperties.json`,
    `ggnPgProperties.json`, `ggnCommercialProperties.json`, …), all in the
    envelope the front end already consumes, so publishing them refreshes the
    site without a front-end change.
    """
    from pathlib import Path

    from homz.db.mongo import get_database
    from homz.services import feed as feed_service
    from homz.services import listings_feed

    async def _run() -> tuple[
        tuple[dict[str, list], int], tuple[dict[str, list], int]
    ]:
        db = get_database()
        projects = await feed_service.load_projects(db)
        properties = await listings_feed.load_properties(db)
        return feed_service.partition(projects), listings_feed.partition(properties)

    (project_buckets, project_withheld), (listing_buckets, listing_withheld) = asyncio.run(_run())
    target = Path(out)
    target.mkdir(parents=True, exist_ok=True)

    def _write_segments(
        buckets: dict[str, list], build_response, kind: str, *, full: bool = False
    ) -> tuple[int, Table]:
        """`full=True` writes every record in the segment, ignoring `--limit`.

        The Properties feed is meant to be fetched once per category and
        filtered client-side — silently truncating it at the CLI's page-size
        default would make that filtering operate on a partial dataset.
        """
        table = Table("segment", kind, "file")
        total = 0
        for segment, records in buckets.items():
            segment_limit = max(limit, len(records)) if full else limit
            payload = build_response(segment, records, limit=segment_limit)
            path = target / f"{segment}.json"
            path.write_text(
                json.dumps(payload, default=feed_service._default, indent=2 if indent else None),
                encoding="utf-8",
            )
            total += len(records)
            table.add_row(segment, str(len(records)), str(path))
        return total, table

    project_total, project_table = _write_segments(
        project_buckets, feed_service.build_response, "projects", full=True
    )
    console.print(project_table)
    if project_withheld:
        console.print(
            f"[dim]Withheld {project_withheld} stub project(s) with no price, configurations "
            f"or amenities — they stay in the warehouse and publish once details appear.[/dim]"
        )

    listing_total, listing_table = _write_segments(
        listing_buckets, listings_feed.build_response, "properties", full=True
    )
    console.print(listing_table)
    if listing_withheld:
        console.print(
            f"[dim]Withheld {listing_withheld} listing(s) with no price, configuration "
            f"or amenities, or an unrecognized listing type.[/dim]"
        )

    if project_total == 0 and listing_total == 0:
        console.print(
            "[yellow]No projects or properties in the warehouse — run `homz scrape source "
            "squareyards` / `magicbricks` first, then re-export.[/yellow]"
        )
    else:
        console.print(
            f"[green]Exported {project_total} projects across {len(project_buckets)} segments "
            f"and {listing_total} properties across {len(listing_buckets)} segments.[/green]"
        )


if __name__ == "__main__":
    app()
