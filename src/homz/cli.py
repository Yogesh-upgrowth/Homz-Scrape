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

app.add_typer(scrape_app, name="scrape")
app.add_typer(etl_app, name="etl")
app.add_typer(enrich_app, name="enrich")
app.add_typer(db_app, name="db")
app.add_typer(ops_app, name="ops")

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
    """Re-run cross-source dedupe over recent active listings."""
    from sqlalchemy import text

    from homz.db.engine import session_scope

    async def _run() -> dict[str, Any]:
        async with session_scope() as session:
            # Records sharing a dedupe_key are the same unit by construction.
            result = await session.execute(
                text(
                    """
                    WITH ranked AS (
                        SELECT id, dedupe_key,
                               ROW_NUMBER() OVER (
                                   PARTITION BY dedupe_key
                                   ORDER BY (title IS NOT NULL)::int
                                          + (description IS NOT NULL)::int
                                          + (rera_number IS NOT NULL)::int DESC,
                                            last_seen_at DESC
                               ) AS rn,
                               FIRST_VALUE(id) OVER (
                                   PARTITION BY dedupe_key
                                   ORDER BY (title IS NOT NULL)::int
                                          + (description IS NOT NULL)::int
                                          + (rera_number IS NOT NULL)::int DESC,
                                            last_seen_at DESC
                               ) AS canonical_id
                        FROM properties
                        WHERE is_active AND dedupe_key IS NOT NULL
                          AND canonical_property_id IS NULL
                        LIMIT :limit
                    )
                    UPDATE properties p
                    SET canonical_property_id = r.canonical_id
                    FROM ranked r
                    WHERE p.id = r.id AND r.rn > 1
                    """
                ),
                {"limit": limit},
            )
            await session.commit()
            return {"linked": result.rowcount or 0}

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
    from homz.db.engine import session_scope
    from homz.enrichment.pipeline import EnrichmentPipeline

    async def _run() -> dict[str, Any]:
        async with session_scope() as session:
            pipeline = EnrichmentPipeline(session, use_llm=llm, use_batch=batch)
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
    from homz.db.engine import session_scope
    from homz.enrichment.pipeline import EnrichmentPipeline

    async def _run() -> dict[str, int]:
        async with session_scope() as session:
            pipeline = EnrichmentPipeline(session, use_llm=False)
            builders = await pipeline.score_builders()
            properties = await pipeline.score_properties(force=force)
            return {"builders": builders, "properties": properties}

    _print_json(asyncio.run(_run()))


@enrich_app.command("estimate")
def enrich_estimate(limit: int = typer.Option(100, "--limit")) -> None:
    """Estimate LLM cost for the pending backlog before spending anything."""
    from sqlalchemy import text

    from homz.db.engine import session_scope
    from homz.enrichment import prompts
    from homz.enrichment.llm import LLMClient, LLMRequest, LLMUsage, estimate_cost

    async def _run() -> dict[str, Any]:
        async with session_scope() as session:
            pending = (
                await session.execute(
                    text("SELECT COUNT(*) FROM reddit_posts WHERE enriched_at IS NULL")
                )
            ).scalar_one()
            sample = (
                (
                    await session.execute(
                        text(
                            "SELECT title, body FROM reddit_posts WHERE enriched_at IS NULL "
                            "ORDER BY score DESC LIMIT :n"
                        ),
                        {"n": min(limit, 20)},
                    )
                )
                .mappings()
                .all()
            )

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
    sql_dir: Path = typer.Option(Path("sql"), "--sql-dir", help="Directory of .sql migrations")
) -> None:
    """Apply the SQL schema files in order (idempotent)."""
    from homz.db.engine import session_scope

    files = sorted(sql_dir.glob("*.sql"))
    if not files:
        console.print(f"[red]no .sql files found in {sql_dir}[/red]")
        raise typer.Exit(1)

    async def _run() -> None:
        for path in files:
            console.print(f"applying [cyan]{path.name}[/cyan] …")
            statements = path.read_text(encoding="utf-8")
            async with session_scope() as session:
                # Execute as one script: the file contains functions and DO
                # blocks whose bodies contain semicolons.
                raw = await session.connection()
                await raw.exec_driver_sql(statements)
        console.print("[green]schema applied[/green]")

    asyncio.run(_run())


@db_app.command("check")
def db_check() -> None:
    """Verify connectivity and print row counts."""
    from homz.db.engine import healthcheck, session_scope
    from homz.db.repository import Repository

    async def _run() -> dict[str, Any]:
        ok = await healthcheck()
        if not ok:
            return {"database": False}
        async with session_scope() as session:
            return {"database": True, "counts": await Repository(session).counts()}

    _print_json(asyncio.run(_run()))


@db_app.command("refresh-views")
def db_refresh_views(concurrent: bool = typer.Option(True, "--concurrent/--blocking")) -> None:
    """Refresh the materialized rollups."""
    from homz.db.engine import session_scope
    from homz.db.repository import Repository

    async def _run() -> None:
        async with session_scope() as session:
            await Repository(session).refresh_market_views(concurrent=concurrent)

    asyncio.run(_run())
    console.print("[green]views refreshed[/green]")


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
    from homz.db.engine import session_scope
    from homz.search.query import PropertySearchQuery, search_properties

    async def _run() -> tuple[list[dict[str, Any]], int]:
        async with session_scope() as session:
            return await search_properties(
                session,
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
    from sqlalchemy import text

    from homz.db.engine import session_scope
    from homz.db.repository import Repository

    async def _run() -> tuple[dict[str, int], list[dict[str, Any]]]:
        async with session_scope() as session:
            counts = await Repository(session).counts()
            runs = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT source, job, status, started_at, duration_s,
                                   parsed, errors, blocked
                            FROM scrape_runs ORDER BY started_at DESC LIMIT 20
                            """
                        )
                    )
                )
                .mappings()
                .all()
            )
            return counts, [dict(r) for r in runs]

    counts, runs = asyncio.run(_run())

    counts_table = Table("table", "rows")
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


@ops_app.command("config")
def ops_config() -> None:
    """Print effective configuration (secrets redacted)."""
    data = settings.model_dump()
    for key in ("reddit_client_secret", "reddit_client_id", "database_url", "proxies"):
        if data.get(key):
            data[key] = "***redacted***"
    _print_json(data)


def _render_pipeline_results(results: list[Any]) -> None:
    table = Table("source", "parsed", "inserted", "updated", "failed", "dupes", "sec")
    for result in results:
        payload = result.as_dict()
        parsed = sum(r["parsed"] for r in payload["reports"]) if payload["reports"] else 0
        load = payload["load"]
        table.add_row(
            payload["source"],
            str(parsed),
            str(load["inserted"]),
            str(load["updated"]),
            str(load["failed"]),
            str(load["duplicates_linked"]),
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


if __name__ == "__main__":
    app()
