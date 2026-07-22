"""DOM / embedded-state extraction helpers shared by all portal parsers.

Portal markup churns; the structured data they emit for Google churns far less.
So every parser follows the same ladder:

    1. JSON-LD  (`<script type="application/ld+json">`)      — most stable
    2. Framework state (`__NEXT_DATA__`, `window.__INITIAL_STATE__`)
    3. OpenGraph / meta tags
    4. CSS selectors                                          — most fragile

`find_first_key` exists because portals rename JSON wrappers constantly but
keep leaf key names ("propertyDetails" moves, "priceD" stays). Searching by key
survives a restructure that a fixed path would not.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any

import orjson
from bs4 import BeautifulSoup, Tag

from homz.common.parsing import clean_text

# ---------------------------------------------------------------------------
# selectors
# ---------------------------------------------------------------------------


def select_one(node: Tag | BeautifulSoup | None, *selectors: str) -> Tag | None:
    """First selector that matches wins. Lets a parser list fallbacks inline."""
    if node is None:
        return None
    for selector in selectors:
        try:
            found = node.select_one(selector)
        except Exception:  # invalid selector — skip rather than crash the parse
            continue
        if found is not None:
            return found
    return None


def select_all(node: Tag | BeautifulSoup | None, *selectors: str) -> list[Tag]:
    if node is None:
        return []
    for selector in selectors:
        try:
            found = node.select(selector)
        except Exception:
            continue
        if found:
            return found
    return []


def text_of(node: Tag | BeautifulSoup | None, *selectors: str, separator: str = " ") -> str | None:
    if not selectors:
        return clean_text(node.get_text(separator=separator)) if node else None
    found = select_one(node, *selectors)
    return clean_text(found.get_text(separator=separator)) if found else None


def texts_of(node: Tag | BeautifulSoup | None, *selectors: str) -> list[str]:
    from homz.common.parsing import dedupe_preserve_order

    return dedupe_preserve_order([el.get_text(" ") for el in select_all(node, *selectors)])


def attr_of(node: Tag | BeautifulSoup | None, attribute: str, *selectors: str) -> str | None:
    target = select_one(node, *selectors) if selectors else node
    if target is None:
        return None
    value = target.get(attribute)
    if isinstance(value, list):
        value = " ".join(value)
    return clean_text(value)


def meta_content(soup: BeautifulSoup, *names: str) -> str | None:
    """Read an OpenGraph/meta tag by `property` or `name`."""
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        )
        if tag and tag.get("content"):
            return clean_text(tag["content"])
    return None


def label_value_pairs(
    node: Tag | BeautifulSoup | None,
    *,
    row_selector: str = "tr, li, .item, .detail-row",
    label_selector: str = "th, .label, .name, dt, span:first-child",
    value_selector: str = "td, .value, .val, dd, span:last-child",
) -> dict[str, str]:
    """Scrape a spec table / definition list into {label: value}.

    Falls back to splitting on ':' when the row has no distinct label node,
    which covers the "Carpet Area: 1250 sqft" pattern portals love.
    """
    out: dict[str, str] = {}
    for row in select_all(node, row_selector):
        label = text_of(row, label_selector)
        value = text_of(row, value_selector)
        if not label or not value or label == value:
            raw = clean_text(row.get_text(" "))
            if raw and ":" in raw:
                label, _, value = (part.strip() for part in raw.partition(":"))
            else:
                continue
        label = clean_text(label)
        value = clean_text(value)
        if label and value and label.lower() != value.lower():
            out[label] = value
    return out


# ---------------------------------------------------------------------------
# embedded JSON
# ---------------------------------------------------------------------------


def json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """All JSON-LD objects on the page, flattened out of @graph wrappers."""
    blocks: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            parsed = orjson.loads(raw.strip())
        except orjson.JSONDecodeError:
            # Some portals emit trailing commas / concatenated objects.
            cleaned = re.sub(r",\s*([}\]])", r"\1", raw.strip())
            try:
                parsed = orjson.loads(cleaned)
            except orjson.JSONDecodeError:
                continue
        blocks.extend(_flatten_ld(parsed))
    return blocks


def _flatten_ld(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, list):
        for item in node:
            yield from _flatten_ld(item)
    elif isinstance(node, dict):
        if "@graph" in node:
            yield from _flatten_ld(node["@graph"])
        else:
            yield node


def json_ld_of_type(soup: BeautifulSoup, *types: str) -> dict[str, Any] | None:
    wanted = {t.lower() for t in types}
    for block in json_ld(soup):
        block_type = block.get("@type")
        candidates = block_type if isinstance(block_type, list) else [block_type]
        if any(str(c).lower() in wanted for c in candidates if c):
            return block
    return None


def next_data(soup: BeautifulSoup) -> dict[str, Any] | None:
    """Next.js hydration payload (`<script id="__NEXT_DATA__">`)."""
    script = soup.find("script", attrs={"id": "__NEXT_DATA__"})
    if not script:
        return None
    raw = script.string or script.get_text()
    if not raw:
        return None
    try:
        return orjson.loads(raw)
    except orjson.JSONDecodeError:
        return None


_STATE_VAR_RE_TEMPLATE = r"{name}\s*=\s*(\{{.*?\}})\s*;?\s*(?:</script>|\n)"


def window_state(soup: BeautifulSoup, *var_names: str) -> dict[str, Any] | None:
    """Pull `window.__INITIAL_STATE__ = {...}` style blobs out of inline JS."""
    html = str(soup)
    for name in var_names:
        escaped = re.escape(name)
        match = re.search(
            _STATE_VAR_RE_TEMPLATE.format(name=escaped), html, re.DOTALL
        )
        if not match:
            continue
        candidate = _balanced_json(html, match.start(1))
        if candidate is None:
            continue
        try:
            return orjson.loads(candidate)
        except orjson.JSONDecodeError:
            continue
    return None


def _balanced_json(text: str, start: int) -> str | None:
    """Walk braces to find the end of a JSON object, respecting strings."""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, min(len(text), start + 5_000_000)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def find_first_key(node: Any, *keys: str, max_depth: int = 12) -> Any:
    """Depth-first search for the first occurrence of any of `keys`."""
    wanted = set(keys)
    stack: list[tuple[Any, int]] = [(node, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        if isinstance(current, dict):
            for key in wanted:
                if key in current:
                    return current[key]
            stack.extend((v, depth + 1) for v in current.values())
        elif isinstance(current, list):
            stack.extend((v, depth + 1) for v in current)
    return None


def find_all_keys(node: Any, *keys: str, max_depth: int = 12) -> list[Any]:
    wanted = set(keys)
    found: list[Any] = []
    stack: list[tuple[Any, int]] = [(node, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        if isinstance(current, dict):
            for key, value in current.items():
                if key in wanted:
                    found.append(value)
                stack.append((value, depth + 1))
        elif isinstance(current, list):
            stack.extend((v, depth + 1) for v in current)
    return found


def deep_get(node: Any, path: str, default: Any = None) -> Any:
    """`deep_get(data, "props.pageProps.property.price")`."""
    current = node
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return default
        if current is None:
            return default
    return current


def first(values: Iterable[Any], default: Any = None) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return default


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------

_IMAGE_EXT_RE = re.compile(r"\.(jpe?g|png|webp|avif)(\?|$)", re.I)
_PLACEHOLDER_RE = re.compile(r"placeholder|blank|lazy|spacer|no[-_]?image|default", re.I)


def extract_images(
    node: Tag | BeautifulSoup | None,
    *,
    base_url: str,
    selectors: tuple[str, ...] = ("img",),
    allow_hosts: tuple[str, ...] = (),
    limit: int = 40,
) -> list:
    """Collect real photo URLs, skipping lazy-load placeholders and sprites.

    Reads `data-src` / `data-original` / `srcset` before `src` because portals
    put a 1px placeholder in `src` until the image scrolls into view.
    """
    from homz.common.parsing import absolute_url
    from homz.common.schema import Image

    images: list[Image] = []
    seen: set[str] = set()

    for element in select_all(node, *selectors):
        candidate = None
        for attribute in ("data-src", "data-original", "data-lazy", "data-img", "src"):
            value = element.get(attribute)
            if value and not value.startswith("data:"):
                candidate = value
                break
        if not candidate:
            srcset = element.get("srcset") or element.get("data-srcset")
            if srcset:
                # Highest-resolution entry is last in a well-formed srcset.
                candidate = srcset.split(",")[-1].strip().split(" ")[0]
        if not candidate:
            continue

        url = absolute_url(base_url, candidate)
        if not url or _PLACEHOLDER_RE.search(url):
            continue
        if allow_hosts and not any(host in url for host in allow_hosts):
            continue
        if not _IMAGE_EXT_RE.search(url) and "image" not in url.lower():
            continue

        key = url.split("?")[0]
        if key in seen:
            continue
        seen.add(key)

        images.append(
            Image(
                url=url,
                caption=clean_text(element.get("alt")),
                is_primary=not images,
            )
        )
        if len(images) >= limit:
            break
    return images
