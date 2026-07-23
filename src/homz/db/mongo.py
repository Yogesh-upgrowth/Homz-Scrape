"""Motor client lifecycle and backend detection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import OperationFailure, PyMongoError

from homz.db.codecs import CODEC_OPTIONS
from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)

_client: AsyncIOMotorClient | None = None
_backend: str | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(
            settings.mongodb_uri,
            maxPoolSize=settings.mongodb_max_pool_size,
            minPoolSize=settings.mongodb_min_pool_size,
            serverSelectionTimeoutMS=settings.mongodb_timeout_ms,
            connectTimeoutMS=settings.mongodb_timeout_ms,
            retryWrites=True,
            appname="homz-intel",
            tz_aware=True,
        )
        log.debug("mongo.client_created", uri=settings.redacted_mongodb_uri)
    return _client


def get_database() -> AsyncIOMotorDatabase:
    """Database handle with the Decimal128/date codecs bound.

    Always go through this rather than `client[name]` — a handle without
    `CODEC_OPTIONS` will reject `Decimal` values at write time.
    """
    return get_client().get_database(
        settings.mongodb_database, codec_options=CODEC_OPTIONS
    )


async def close_client() -> None:
    global _client, _backend
    if _client is not None:
        _client.close()
        _client = None
        _backend = None


@asynccontextmanager
async def database() -> AsyncIterator[AsyncIOMotorDatabase]:
    """Context-manager form of `get_database()`.

    Mongo opens no transaction for single-document writes, so this is just a
    handle — it exists so call sites read the same as any other resource
    acquisition."""
    yield get_database()


# ---------------------------------------------------------------------------
# health & capability detection
# ---------------------------------------------------------------------------


async def healthcheck() -> bool:
    try:
        await get_client().admin.command("ping")
        return True
    except PyMongoError as exc:
        log.error("mongo.healthcheck_failed", error=str(exc)[:300])
        return False


async def diagnose() -> dict[str, Any]:
    """Connect and translate the failure into something actionable.

    Atlas connection problems all surface as the same opaque
    `ServerSelectionTimeoutError`, and the three real causes (IP not on the
    allowlist, wrong password, SRV lookup failing) need completely different
    fixes. This maps them.
    """
    from pymongo.errors import (
        ConfigurationError,
        OperationFailure,
        ServerSelectionTimeoutError,
    )

    uri = settings.mongodb_uri
    result: dict[str, Any] = {
        "uri": settings.redacted_mongodb_uri,
        "database": settings.mongodb_database,
        "ok": False,
    }

    if not uri or "USER:PASSWORD" in uri or "xxxxx" in uri:
        result["error"] = "placeholder URI"
        result["fix"] = (
            "Set HOMZ_MONGODB_URI in .env to your Atlas connection string "
            "(Atlas → Cluster → Connect → Drivers → Python)."
        )
        return result

    try:
        client = get_client()
        await client.admin.command("ping")
        build = await client.admin.command("buildInfo")
        result.update({
            "ok": True,
            "server_version": build.get("version"),
            "backend": await detect_backend(),
            "transactions": await supports_transactions(),
        })
        return result

    except ConfigurationError as exc:
        message = str(exc)
        result["error"] = message[:300]
        if "does not exist" in message or "DNS" in message:
            result["fix"] = (
                "The SRV hostname did not resolve. Check the cluster address, "
                "and that `dnspython` is installed (required for mongodb+srv://)."
            )
        else:
            result["fix"] = "Check the URI format."
        return result

    except OperationFailure as exc:
        result["error"] = str(exc)[:300]
        if exc.code in {18, 8000}:  # AuthenticationFailed / Atlas auth error
            result["fix"] = (
                "Authentication failed. Verify the database user and password "
                "(Atlas → Database Access). If the password contains @ : / or ?, "
                "it must be percent-encoded in the URI."
            )
        else:
            result["fix"] = "The user may lack readWrite on this database."
        return result

    except ServerSelectionTimeoutError as exc:
        result["error"] = str(exc)[:300]
        result["fix"] = (
            "Could not reach the cluster. The usual cause is the IP allowlist: "
            "Atlas → Network Access → Add IP Address (use your current IP, or "
            "0.0.0.0/0 only for throwaway development). Also check the cluster "
            "is not paused."
        )
        return result

    except PyMongoError as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        return result


async def server_info() -> dict[str, Any]:
    try:
        info = await get_client().admin.command("buildInfo")
        return {
            "version": info.get("version"),
            "modules": info.get("modules", []),
            "backend": await detect_backend(),
        }
    except PyMongoError as exc:
        return {"error": str(exc)[:200]}


async def detect_backend() -> str:
    """Return "atlas" or "text" — which search implementation to use.

    Atlas exposes `$listSearchIndexes`; a self-hosted server fails the command.
    Probing the live server beats trusting the URI, because a self-hosted
    cluster can sit behind any hostname and `mongodb+srv` only implies DNS
    seedlist discovery, not Atlas.
    """
    global _backend
    if settings.search_backend in {"atlas", "text"}:
        return settings.search_backend
    if _backend is not None:
        return _backend

    db = get_database()
    try:
        # Cheap and side-effect free: an empty aggregation against the stage.
        await db.command({"aggregate": "properties", "pipeline": [
            {"$listSearchIndexes": {}}, {"$limit": 1},
        ], "cursor": {}})
        _backend = "atlas"
    except OperationFailure as exc:
        # 40324 "Unrecognized pipeline stage" / 59 "no such command" on
        # self-hosted; anything else is a real problem worth surfacing.
        if exc.code in {40324, 59, 31082, 6047401}:
            _backend = "text"
        else:
            log.warning("mongo.backend_probe_failed", code=exc.code, error=str(exc)[:200])
            _backend = "text"
    except PyMongoError as exc:
        log.warning("mongo.backend_probe_error", error=str(exc)[:200])
        _backend = "text"

    log.info("mongo.backend_detected", backend=_backend, atlas_hint=settings.is_atlas)
    return _backend


async def supports_transactions() -> bool:
    """Multi-document transactions need a replica set or sharded cluster.

    Atlas always qualifies; a standalone `mongod` does not. Nothing in the
    ingest path requires one (every write is a single document), so this only
    gates optional consistency upgrades.
    """
    try:
        hello = await get_client().admin.command("hello")
        return bool(hello.get("setName") or hello.get("msg") == "isdbgrid")
    except PyMongoError:
        return False
