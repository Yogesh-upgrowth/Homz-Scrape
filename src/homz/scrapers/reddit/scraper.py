"""Reddit scraper — official OAuth API only.

This is the sanctioned channel: a registered "script" app, the
`client_credentials` grant, and `oauth.reddit.com`. We never scrape
old.reddit.com HTML and never touch a logged-in session, which means:

  * Reddit's published rate limits apply and are respected (the API returns
    `X-Ratelimit-Remaining` / `-Reset`, which this client honours);
  * the descriptive User-Agent Reddit requires is sent on every call;
  * nothing here breaks if Reddit changes its front-end markup.

Discovery per subreddit: `/new` (recency, cursor-based and therefore
incremental), plus `/search` for the specific real-estate topics that would
otherwise scroll past between runs.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from homz.common.base import BaseScraper, ScrapeJob
from homz.common.enums import Source
from homz.common.http import FetchResult
from homz.common.schema import ScrapedRecord
from homz.common.state import ScrapeState
from homz.scrapers.reddit import parser
from homz.settings import settings

OAUTH_BASE = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

# Topic queries run against each subreddit's search endpoint. These map 1:1 to
# the discussion areas the platform cares about.
SEARCH_QUERIES: tuple[str, ...] = (
    "builder fraud OR builder cheating OR builder scam",
    "possession delay OR delayed possession",
    "RERA complaint OR RERA case",
    "society maintenance OR RWA issue",
    "broker experience OR broker commission",
    "which sector to buy OR sector recommendation",
    "rental yield OR ROI property",
    "home loan OR loan approval property",
    "stamp duty OR registry OR registration charges",
    "hidden charges OR EDC IDC",
    "Dwarka Expressway",
    "SPR OR Southern Peripheral Road",
    "Golf Course Road property",
    "New Gurgaon OR Sohna Road",
    "Noida Expressway property",
    "resale flat OR resale property",
    "under construction OR ready to move",
    "society review OR project review",
)


class RedditAuthError(RuntimeError):
    pass


class RedditClient:
    """Minimal async OAuth client with token refresh and rate-limit awareness."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        user_agent: str,
        timeout: float = 30.0,
    ) -> None:
        if not client_id or not client_secret:
            raise RedditAuthError(
                "Reddit API credentials missing. Register a 'script' app at "
                "https://www.reddit.com/prefs/apps and set HOMZ_REDDIT_CLIENT_ID "
                "and HOMZ_REDDIT_CLIENT_SECRET."
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._user_agent = user_agent
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True
        )
        # Reddit allows 100 QPM for OAuth clients; stay well inside it.
        self._min_interval = 1.1
        self._last_request = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> RedditClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _access_token(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._token_expires_at - 60:
                return self._token

            basic = base64.b64encode(
                f"{self._client_id}:{self._client_secret}".encode()
            ).decode()
            response = await self._client.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials", "scope": "read"},
                headers={
                    "Authorization": f"Basic {basic}",
                    "User-Agent": self._user_agent,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if response.status_code != 200:
                raise RedditAuthError(
                    f"token request failed: {response.status_code} {response.text[:200]}"
                )
            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise RedditAuthError(f"no access_token in response: {payload}")
            self._token = token
            self._token_expires_at = time.monotonic() + float(payload.get("expires_in", 3600))
            return token

    async def get(self, path: str, **params: Any) -> dict[str, Any] | list[Any]:
        token = await self._access_token()

        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)

        url = path if path.startswith("http") else f"{OAUTH_BASE}{path}"
        response = await self._client.get(
            url,
            params={k: v for k, v in params.items() if v is not None},
            headers={"Authorization": f"Bearer {token}", "User-Agent": self._user_agent},
        )
        self._last_request = time.monotonic()

        # Reddit publishes its own budget; obey it rather than guessing.
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining is not None:
            try:
                if float(remaining) < 5 and reset:
                    await asyncio.sleep(min(float(reset) + 1, 90))
            except ValueError:
                pass

        if response.status_code == 401:
            self._token = None  # force refresh, then let the caller retry
            raise RedditAuthError("401 from Reddit — token rejected")
        if response.status_code == 429:
            await asyncio.sleep(float(response.headers.get("retry-after", 60)))
            raise httpx.HTTPStatusError("429", request=response.request, response=response)
        response.raise_for_status()
        return response.json()


class RedditScraper(BaseScraper):
    source = Source.REDDIT
    base_url = "https://oauth.reddit.com"
    needs_browser = False

    default_jobs = ()  # built from settings in `build_jobs()`

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client: RedditClient | None = None
        self._post_cache: dict[str, dict[str, Any]] = {}
        self.default_jobs = self.build_jobs()

    @staticmethod
    def build_jobs() -> tuple[ScrapeJob, ...]:
        return tuple(
            ScrapeJob(
                name="subreddit",
                city=None,
                max_pages=4,
                max_items=300,
                params={"subreddit": sub},
            )
            for sub in settings.reddit_subreddits
        )

    async def __aenter__(self) -> RedditScraper:
        await super().__aenter__()
        self._client = RedditClient(
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            user_agent=settings.reddit_user_agent,
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
        await super().__aexit__(*exc_info)

    @property
    def client(self) -> RedditClient:
        if self._client is None:
            raise RuntimeError("RedditScraper must be used as an async context manager")
        return self._client

    # -- discovery ----------------------------------------------------------

    async def discover(self, job: ScrapeJob, state: ScrapeState) -> AsyncIterator[str]:
        subreddit = job.params.get("subreddit", "gurgaon")
        cursor_key = f"{subreddit}:after"
        seen_ids: set[str] = set()

        # 1. /new — cursor-based, so an incremental run only pulls what is new.
        after = state.cursor.get(cursor_key) if job.incremental else None
        newest_seen: str | None = None

        for _page in range(job.max_pages):
            try:
                payload = await self.client.get(
                    f"/r/{subreddit}/new", limit=100, after=after, raw_json=1
                )
            except Exception as exc:  # noqa: BLE001
                self.log.warning("reddit.new_failed", subreddit=subreddit, error=str(exc)[:200])
                break

            children = _children(payload)
            if not children:
                break

            for child in children:
                data = child.get("data") or {}
                post_id = data.get("id")
                if not post_id or post_id in seen_ids:
                    continue
                if not parser.is_relevant(data.get("title") or "", data.get("selftext")):
                    continue
                seen_ids.add(post_id)
                newest_seen = newest_seen or f"t3_{post_id}"
                self._post_cache[post_id] = data
                yield f"reddit://{subreddit}/{post_id}"
                if len(seen_ids) >= job.max_items:
                    break

            after = (payload.get("data") or {}).get("after") if isinstance(payload, dict) else None
            if not after or len(seen_ids) >= job.max_items:
                break

        if newest_seen:
            # Store the newest fullname so the next run resumes above it.
            state.cursor[cursor_key] = None
            state.cursor[f"{subreddit}:newest"] = newest_seen

        # 2. Targeted topic searches — catches threads that scrolled past.
        for query in SEARCH_QUERIES:
            if len(seen_ids) >= job.max_items:
                break
            try:
                payload = await self.client.get(
                    f"/r/{subreddit}/search",
                    q=query,
                    restrict_sr=1,
                    sort="new",
                    t="year",
                    limit=25,
                    raw_json=1,
                )
            except Exception as exc:  # noqa: BLE001
                self.log.debug("reddit.search_failed", query=query[:40], error=str(exc)[:160])
                continue

            for child in _children(payload):
                data = child.get("data") or {}
                post_id = data.get("id")
                if not post_id or post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                self._post_cache[post_id] = data
                yield f"reddit://{subreddit}/{post_id}"

    # -- fetch --------------------------------------------------------------

    async def fetch_detail(self, url: str, job: ScrapeJob) -> FetchResult:
        """`url` is our internal `reddit://sub/post_id` handle.

        The listing payload is already cached from discovery; this call fetches
        the comment tree and packs both into a FetchResult so the base runner's
        loop stays source-agnostic.
        """
        import orjson

        _, _, remainder = url.partition("reddit://")
        subreddit, _, post_id = remainder.partition("/")

        comments: Any = []
        try:
            comments = await self.client.get(
                f"/r/{subreddit}/comments/{post_id}",
                limit=settings.reddit_comment_limit,
                depth=4,
                sort="top",
                raw_json=1,
            )
        except Exception as exc:  # noqa: BLE001
            self.log.debug("reddit.comments_failed", post_id=post_id, error=str(exc)[:160])

        listing = self._post_cache.get(post_id)
        if listing is None and isinstance(comments, list) and comments:
            first_children = _children(comments[0])
            listing = (first_children[0].get("data") if first_children else None) or {}

        body = orjson.dumps({"post": listing or {}, "comments": comments}).decode()
        raw_key = self.raw_store.put(
            source=self.source.value, url=url, content=body, extension="json"
        )
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            text=body,
            headers={},
            elapsed_s=0.0,
            raw_key=raw_key,
        )

    # -- parse --------------------------------------------------------------

    async def parse_detail(self, result: FetchResult, job: ScrapeJob) -> list[ScrapedRecord]:
        import orjson

        payload = orjson.loads(result.text)
        post_data = payload.get("post") or {}
        if not post_data.get("id"):
            return []

        record = parser.parse_post({"data": post_data}, enrich=False)
        if record is None:
            return []

        record.comments = parser.parse_comments(
            payload.get("comments") or [],
            record.source_id,
            limit=settings.reddit_comment_limit,
        )
        parser.apply_rule_extraction(record)
        record.raw_html_key = result.raw_key
        return [record]


def _children(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        return (payload.get("data") or {}).get("children") or []
    if isinstance(payload, list) and payload:
        return _children(payload[0])
    return []
