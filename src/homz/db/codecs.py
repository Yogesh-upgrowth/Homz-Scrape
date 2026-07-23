"""BSON type handling.

Two conversions have to be automatic, because getting either wrong silently
corrupts data rather than raising:

1. **Money must never become a float.** Python `Decimal` maps to BSON
   `Decimal128` (128-bit, base-10, exact). Without a codec, pymongo raises
   `InvalidDocument` on a Decimal — but the tempting "fix" is `float(price)`,
   which turns ₹1,25,00,000 into 12499999.999999998 and quietly poisons every
   median, yield and score downstream.

2. **`datetime.date` is not a BSON type.** BSON only has UTC datetime. Pydantic
   models use `date` for possession/launch dates, so those need widening to
   midnight-UTC datetimes on write and narrowing back on read.

A `TypeRegistry` applies both to every read and write on the database handle,
so no call site can forget.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from bson.codec_options import CodecOptions, TypeCodec, TypeRegistry
from bson.decimal128 import Decimal128


class DecimalCodec(TypeCodec):
    """`Decimal` ⟷ `Decimal128`."""

    python_type = Decimal
    bson_type = Decimal128

    def transform_python(self, value: Decimal) -> Decimal128:
        # Decimal128 has 34 significant digits; INR amounts never approach that,
        # but NaN/Infinity would raise, so normalize them away first.
        if value.is_nan() or value.is_infinite():
            raise ValueError(f"cannot store non-finite Decimal: {value!r}")
        return Decimal128(value)

    def transform_bson(self, value: Decimal128) -> Decimal:
        return value.to_decimal()


class DateCodec(TypeCodec):
    """`date` → midnight-UTC `datetime` on write.

    Note this is write-only: BSON has no date type, so reads come back as
    `datetime`. Pydantic narrows them again when the document is validated
    into a model, and `as_date()` below handles the raw-dict paths.
    """

    python_type = date
    bson_type = datetime

    def transform_python(self, value: date) -> datetime:
        return datetime(value.year, value.month, value.day, tzinfo=UTC)

    def transform_bson(self, value: datetime) -> datetime:
        # Datetimes stay datetimes — only Pydantic knows which fields are dates.
        return value


TYPE_REGISTRY = TypeRegistry(
    [DecimalCodec(), DateCodec()],
    # Anything else unencodable should raise loudly rather than be dropped.
)

CODEC_OPTIONS: CodecOptions = CodecOptions(
    type_registry=TYPE_REGISTRY,
    tz_aware=True,          # datetimes come back with UTC attached, not naive
    tzinfo=UTC,
)


# ---------------------------------------------------------------------------
# helpers for the raw-dict paths (aggregation output, API responses)
# ---------------------------------------------------------------------------


def as_decimal(value: Any) -> Decimal | None:
    """Coerce a BSON numeric back to `Decimal`, whatever shape it arrived in."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Aggregation operators ($avg, $percentile) return doubles even over
        # Decimal128 input. Round-trip through str so we don't inherit the
        # binary-float representation error.
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except ArithmeticError:
            return None
    return None


def as_float(value: Any) -> float | None:
    decimal_value = as_decimal(value)
    return float(decimal_value) if decimal_value is not None else None


def as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def jsonable(value: Any) -> Any:
    """Recursively convert BSON types to JSON-serialisable Python.

    FastAPI can serialise `Decimal`, but not `Decimal128` or `ObjectId`, so
    every document leaving the API goes through this.
    """
    from bson import ObjectId

    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, ObjectId):
        return str(value)
    return value


def to_bson_safe(value: Any) -> Any:
    """Prepare a Pydantic-dumped dict for insertion.

    Pydantic's `model_dump()` leaves enums as `StrEnum` (a `str` subclass, so
    BSON-safe), `Decimal` and `date` (handled by the registry), and sets
    (which BSON cannot encode) — this converts the last of those.
    """
    if isinstance(value, dict):
        return {k: to_bson_safe(v) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return sorted(to_bson_safe(v) for v in value)
    if isinstance(value, list | tuple):
        return [to_bson_safe(v) for v in value]
    return value
