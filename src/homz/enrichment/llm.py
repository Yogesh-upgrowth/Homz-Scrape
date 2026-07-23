"""Claude client for the enrichment tier.

Two execution modes:

* **live** — `messages.create` per item. Use for small backfills, debugging,
  and anything latency-sensitive.
* **batch** — the Message Batches API. Use for bulk enrichment: 50% of standard
  price, up to 100k requests per batch, results typically inside an hour. This
  is the default for scheduled runs (`HOMZ_LLM_USE_BATCH=true`).

Both paths use:
  * `output_config.format` with a JSON schema, so the response shape is
    enforced server-side rather than parsed hopefully;
  * a frozen system prompt with `cache_control`, so the instruction block is a
    cache read (~0.1x) instead of a full-price input on every call.

Note on caching: Opus 4.8 has a 4096-token minimum cacheable prefix. Our system
prompts are shorter than that, so caching is a no-op unless you also pin a
larger shared prefix. `verify_cache_hits()` reports what actually happened so
this is measurable rather than assumed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import anthropic
import orjson

from homz.logging_setup import get_logger
from homz.settings import settings

log = get_logger(__name__)

#: Bumped when a prompt or schema changes so previously enriched rows can be
#: re-processed selectively (`properties.enrichment_version`).
ENRICHMENT_VERSION = 1


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0
    failures: int = 0

    def add(self, usage: Any) -> None:
        self.requests += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def as_dict(self) -> dict[str, int]:
        return {
            "requests": self.requests,
            "failures": self.failures,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }


@dataclass
class LLMRequest:
    """One unit of work, keyed so batch results can be reattached."""

    custom_id: str
    system: str
    user: str
    schema: dict[str, Any]
    max_tokens: int = 2048


@dataclass
class LLMResult:
    custom_id: str
    data: dict[str, Any] | None
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.data is not None


class LLMClient:
    def __init__(
        self,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
        api_key: str | None = None,
        cache_system_prompt: bool | None = None,
    ) -> None:
        self.model = model or settings.llm_model
        self.max_tokens = max_tokens or settings.llm_max_tokens
        self.effort = effort or settings.llm_effort
        self.cache_system_prompt = (
            settings.llm_cache_system_prompt if cache_system_prompt is None else cache_system_prompt
        )
        # A bare constructor also picks up an `ant auth login` profile, so we
        # only pass api_key when one was explicitly supplied.
        self._client = (
            anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        )
        self.usage = LLMUsage()

    async def aclose(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- request construction ----------------------------------------------

    def _system_blocks(self, system: str) -> list[dict[str, Any]]:
        block: dict[str, Any] = {"type": "text", "text": system}
        if self.cache_system_prompt:
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

    def _params(self, request: LLMRequest) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": min(request.max_tokens, self.max_tokens),
            "system": self._system_blocks(request.system),
            "messages": [{"role": "user", "content": request.user}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": request.schema},
            },
        }

    # -- live path ----------------------------------------------------------

    async def complete(self, request: LLMRequest) -> LLMResult:
        try:
            response = await self._client.messages.create(**self._params(request))
        except anthropic.RateLimitError as exc:
            self.usage.failures += 1
            log.warning("llm.rate_limited", custom_id=request.custom_id, error=str(exc)[:200])
            return LLMResult(request.custom_id, None, error="rate_limited")
        except anthropic.APIStatusError as exc:
            self.usage.failures += 1
            log.error(
                "llm.api_error",
                custom_id=request.custom_id,
                status=exc.status_code,
                type=getattr(exc, "type", None),
                error=str(exc)[:300],
            )
            return LLMResult(request.custom_id, None, error=f"api_error:{exc.status_code}")
        except anthropic.APIConnectionError as exc:
            self.usage.failures += 1
            return LLMResult(request.custom_id, None, error=f"connection:{str(exc)[:120]}")

        self.usage.add(response.usage)

        if response.stop_reason == "refusal":
            log.warning("llm.refusal", custom_id=request.custom_id)
            return LLMResult(request.custom_id, None, error="refusal")
        if response.stop_reason == "max_tokens":
            log.warning("llm.truncated", custom_id=request.custom_id)

        data = _parse_json_content(response.content)
        if data is None:
            self.usage.failures += 1
            return LLMResult(request.custom_id, None, error="unparseable_output")
        return LLMResult(request.custom_id, data)

    async def complete_many(
        self, requests: list[LLMRequest], *, concurrency: int = 4
    ) -> list[LLMResult]:
        semaphore = asyncio.Semaphore(concurrency)

        async def _one(request: LLMRequest) -> LLMResult:
            async with semaphore:
                return await self.complete(request)

        return list(await asyncio.gather(*(_one(r) for r in requests)))

    # -- batch path ---------------------------------------------------------

    async def submit_batch(self, requests: list[LLMRequest]) -> str:
        """Submit a Message Batch; returns the batch id to poll."""
        from anthropic.types.messages.batch_create_params import Request

        payload = [
            Request(custom_id=r.custom_id, params=self._params(r))  # type: ignore[arg-type]
            for r in requests
        ]
        batch = await self._client.messages.batches.create(requests=payload)
        log.info("llm.batch_submitted", batch_id=batch.id, count=len(requests))
        return batch.id

    async def poll_batch(
        self,
        batch_id: str,
        *,
        interval: float = 60.0,
        # Not an asyncio cancel scope: this is a wall-clock deadline for a
        # server-side job that legitimately runs for hours.
        timeout: float = 24 * 3600,  # noqa: ASYNC109
    ) -> str:
        elapsed = 0.0
        while elapsed < timeout:
            batch = await self._client.messages.batches.retrieve(batch_id)
            if batch.processing_status == "ended":
                log.info(
                    "llm.batch_ended",
                    batch_id=batch_id,
                    succeeded=batch.request_counts.succeeded,
                    errored=batch.request_counts.errored,
                )
                return "ended"
            log.info(
                "llm.batch_processing",
                batch_id=batch_id,
                processing=batch.request_counts.processing,
                succeeded=batch.request_counts.succeeded,
            )
            await asyncio.sleep(interval)
            elapsed += interval
        raise TimeoutError(f"batch {batch_id} did not finish within {timeout}s")

    async def fetch_batch_results(self, batch_id: str) -> list[LLMResult]:
        """Results arrive in arbitrary order — always key by `custom_id`."""
        results: list[LLMResult] = []
        async for entry in await self._client.messages.batches.results(batch_id):
            custom_id = entry.custom_id
            result_type = entry.result.type
            if result_type != "succeeded":
                self.usage.failures += 1
                results.append(LLMResult(custom_id, None, error=result_type))
                continue

            message = entry.result.message
            self.usage.add(message.usage)
            if message.stop_reason == "refusal":
                results.append(LLMResult(custom_id, None, error="refusal"))
                continue
            data = _parse_json_content(message.content)
            results.append(LLMResult(custom_id, data, error=None if data else "unparseable_output"))
        return results

    async def run_batch(
        self, requests: list[LLMRequest], *, poll_interval: float = 60.0
    ) -> list[LLMResult]:
        if not requests:
            return []
        batch_id = await self.submit_batch(requests)
        await self.poll_batch(batch_id, interval=poll_interval)
        return await self.fetch_batch_results(batch_id)

    # -- diagnostics --------------------------------------------------------

    async def count_tokens(self, request: LLMRequest) -> int:
        """Real token count for cost projection. Never estimate with tiktoken —
        it is a different tokenizer and undercounts Claude by 15-20%."""
        response = await self._client.messages.count_tokens(
            model=self.model,
            system=self._system_blocks(request.system),
            messages=[{"role": "user", "content": request.user}],
        )
        return response.input_tokens

    def verify_cache_hits(self) -> dict[str, Any]:
        """Report whether prompt caching is actually paying off.

        Zero cache reads across many requests means the prefix is below the
        model's minimum cacheable size (4096 tokens on Opus 4.8) or something
        volatile leaked into the system prompt.
        """
        total = self.usage.cache_read_tokens + self.usage.input_tokens
        return {
            "cache_read_tokens": self.usage.cache_read_tokens,
            "cache_write_tokens": self.usage.cache_write_tokens,
            "uncached_input_tokens": self.usage.input_tokens,
            "cache_hit_ratio": (
                round(self.usage.cache_read_tokens / total, 3) if total else 0.0
            ),
            "caching_effective": self.usage.cache_read_tokens > 0,
        }


def _parse_json_content(content: list[Any]) -> dict[str, Any] | None:
    """Pull the JSON object out of the response content blocks."""
    for block in content:
        if getattr(block, "type", None) != "text":
            continue
        text = getattr(block, "text", "") or ""
        if not text.strip():
            continue
        try:
            parsed = orjson.loads(text)
        except orjson.JSONDecodeError:
            # Defensive: strip markdown fences if a future model wraps output.
            stripped = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
            try:
                parsed = orjson.loads(stripped)
            except orjson.JSONDecodeError:
                continue
        if isinstance(parsed, dict):
            return parsed
    return None


def estimate_cost(usage: LLMUsage, *, model: str | None = None) -> dict[str, float]:
    """Rough USD estimate. Rates are per million tokens.

    Batch requests bill at 50% of these rates — pass the batch usage separately
    if you need an exact split.
    """
    rates = {
        "claude-opus-4-8": (5.0, 25.0),
        "claude-opus-4-7": (5.0, 25.0),
        "claude-sonnet-5": (3.0, 15.0),
        "claude-haiku-4-5": (1.0, 5.0),
    }
    input_rate, output_rate = rates.get(model or settings.llm_model, (5.0, 25.0))
    uncached = usage.input_tokens / 1_000_000 * input_rate
    cached = usage.cache_read_tokens / 1_000_000 * input_rate * 0.1
    written = usage.cache_write_tokens / 1_000_000 * input_rate * 1.25
    output = usage.output_tokens / 1_000_000 * output_rate
    return {
        "input_usd": round(uncached, 4),
        "cache_read_usd": round(cached, 4),
        "cache_write_usd": round(written, 4),
        "output_usd": round(output, 4),
        "total_usd": round(uncached + cached + written + output, 4),
    }
