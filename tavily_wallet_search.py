#!/usr/bin/env python3
"""Search public web sources for a wallet address with Tavily Search API."""

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

TAVILY_SEARCH_API = "https://api.tavily.com/search"
DEFAULT_ADDRESS = "0x3c4f6ca9bea79432eaf8d98c3be4570064f1fde8"
DEFAULT_OUTPUT = Path("data/tavily_wallet_search.json")
MAX_SOURCES_PER_SEARCH = 5
CACHE_DIR = Path("data/tavily_cache")
CACHE_TTL = timedelta(days=7)


def search_wallet(
    address: str,
    context: str = "",
    max_results: int = MAX_SOURCES_PER_SEARCH,
    search_depth: str = "basic",
) -> dict:
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise ValueError("TAVILY_API_KEY is not configured. Add it to the project's .env file.")

    response = requests.post(
        TAVILY_SEARCH_API,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "query": " ".join(part for part in (f'"{address}"', context.strip()) if part),
            "topic": "general",
            "search_depth": search_depth,
            "max_results": max_results,
            "exact_match": True,
            "include_answer": False,
            "include_raw_content": False,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _cache_file(address: str, context: str, cache_dir: Path) -> Path:
    """Keep results isolated when the same address is searched in another case."""
    query_key = f"{address.lower()}\n{context.strip().lower()}"
    cache_key = hashlib.sha256(query_key.encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{cache_key}.json"


def _read_fresh_cache(cache_file: Path) -> Optional[dict]:
    """Return a cache entry only while it is within its retention period."""
    if not cache_file.exists():
        return None

    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(cached["cached_at"])
        if datetime.now(timezone.utc) - cached_at < CACHE_TTL:
            return cached
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def _has_sources(payload: dict) -> bool:
    return bool(payload.get("result", {}).get("results", []))


def _search_with_empty_result_retry(
    address: str,
    context: str,
    max_results: int,
    search_depth: str,
) -> tuple[dict, int]:
    """Retry once only when Tavily returns an empty result set."""
    result = search_wallet(address, context, max_results=max_results, search_depth=search_depth)
    attempts = 1
    if not result.get("results"):
        result = search_wallet(address, context, max_results=max_results, search_depth=search_depth)
        attempts += 1
    return result, attempts


def search_wallet_cached(
    address: str,
    context: str = "",
    max_results: int = MAX_SOURCES_PER_SEARCH,
    search_depth: str = "basic",
    cache_dir: Path = CACHE_DIR,
    use_cache: bool = True,
) -> dict:
    """Search an address once per seven days and retain enough data for a report.

    Context improves relevance for many incidents, but it can also make a valid
    address appear absent from search results.  In that case reuse or request a
    second, address-only result so the report does not silently lose sources.
    """
    cache_file = _cache_file(address, context, cache_dir)
    cached = _read_fresh_cache(cache_file) if use_cache else None
    if cached and _has_sources(cached):
        return {**cached, "from_cache": True}

    if not context:
        result, attempts = _search_with_empty_result_retry(
            address,
            "",
            min(max_results, MAX_SOURCES_PER_SEARCH),
            search_depth,
        )
        payload = {
            "address": address,
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "context": "",
            "result": result,
            "from_cache": False,
            "attempts": attempts,
        }
        if _has_sources(payload):
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    if cached:
        payload = {**cached, "from_cache": True}
    else:
        result = search_wallet(
            address,
            context,
            max_results=min(max_results, MAX_SOURCES_PER_SEARCH),
            search_depth=search_depth,
        )
        payload = {
            "address": address,
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "context": context,
            "result": result,
            "from_cache": False,
        }

    if context and not _has_sources(payload):
        address_only_cache = _cache_file(address, "", cache_dir)
        address_only = _read_fresh_cache(address_only_cache) if use_cache else None
        if not address_only or not _has_sources(address_only):
            address_only_result, attempts = _search_with_empty_result_retry(
                address,
                "",
                min(max_results, MAX_SOURCES_PER_SEARCH),
                search_depth,
            )
            address_only = {
                "address": address,
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "context": "",
                "result": address_only_result,
                "from_cache": False,
                "attempts": attempts,
            }
            if _has_sources(address_only):
                cache_dir.mkdir(parents=True, exist_ok=True)
                address_only_cache.write_text(
                    json.dumps(address_only, ensure_ascii=False, indent=2), encoding="utf-8"
                )

        payload = {
            **payload,
            "result": address_only["result"],
            "fallback_to_address_only": True,
            "fallback_from_cache": bool(address_only.get("from_cache")),
            "fallback_attempts": address_only.get("attempts", 0),
        }

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main():
    parser = argparse.ArgumentParser(
        description="Search public web sources for a cryptocurrency wallet address."
    )
    parser.add_argument("address", nargs="?", default=DEFAULT_ADDRESS)
    parser.add_argument("--context", default="",
                        help="Extra terms from the post, for example: 'M1llionz USDT freeze'")
    parser.add_argument("--max-results", type=int, default=MAX_SOURCES_PER_SEARCH,
                        choices=range(1, MAX_SOURCES_PER_SEARCH + 1),
                        help="Number of returned sources (maximum: 5)")
    parser.add_argument("--search-depth", choices=("basic", "advanced"), default="basic")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-cache", action="store_true", help="Force a new Tavily request")
    args = parser.parse_args()

    load_dotenv()
    search = search_wallet_cached(
        args.address,
        args.context,
        args.max_results,
        args.search_depth,
        use_cache=not args.no_cache,
    )
    result = search["result"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(search, ensure_ascii=False, indent=2), encoding="utf-8")

    results = result.get("results", [])
    print(f"Query: {result.get('query', args.address)}")
    print("Cache: " + ("hit" if search["from_cache"] else "miss"))
    if search.get("fallback_to_address_only"):
        print("Fallback: address-only search")
    print(f"Sources found: {len(results)}")
    print(f"Saved full response: {args.output}")
    for index, source in enumerate(results, start=1):
        snippet = " ".join(source.get("content", "").split())
        print(f"\n[{index}] {source.get('title', 'Untitled')}")
        print(source.get("url", ""))
        print(snippet[:500])


if __name__ == "__main__":
    main()
