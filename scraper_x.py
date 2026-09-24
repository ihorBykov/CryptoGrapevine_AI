import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse
from apify_client import ApifyClient
from dotenv import load_dotenv

load_dotenv()

ACTOR_ID = "xquik/x-tweet-scraper"
STATE_FILE = Path(".seen_tweets.json")
DEFAULT_ARCHIVE_FILE = Path("data/latest_x_posts.json")


SEARCH_TERMS = [
    "(BTC OR USDT OR ETH) (from:CertiKAlert)",
]


def _first_text(*values: object) -> Optional[str]:
    """Return the first non-empty string among alternative actor fields."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _author_from_post_url(url: object) -> Optional[str]:
    """Extract the handle from a canonical X/Twitter post URL, if present."""
    if not isinstance(url, str):
        return None

    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path_parts = [part for part in parsed.path.split("/") if part]
    if host not in {"x.com", "twitter.com"} or len(path_parts) < 3:
        return None
    if path_parts[1] != "status":
        return None

    handle = path_parts[0].removeprefix("@")
    if handle.lower() == "i":
        return None
    return handle if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle) else None


def extract_author(item: Dict[str, object]) -> str:
    """Get an X handle from actor fields, falling back to the canonical post URL."""
    author = item.get("author")
    author_data = author if isinstance(author, dict) else {}
    return _first_text(
        author_data.get("username"),
        author_data.get("userName"),
        item.get("authorUsername"),
        item.get("author_username"),
        _author_from_post_url(item.get("url")),
        _author_from_post_url(item.get("tweetUrl")),
        _author_from_post_url(item.get("twitterUrl")),
    ) or "unknown"


def _load_seen() -> set:
    return set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()


def _save_seen(seen: set) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen)[-500:]))


def save_posts(posts: List[Dict[str, str]], file_path: Path = DEFAULT_ARCHIVE_FILE) -> None:
    """Save normalized X posts so they can be replayed without Apify."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "source": "xquik/x-tweet-scraper",
        "posts": posts,
    }
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_posts(file_path: Path) -> List[Dict[str, str]]:
    """Load an archive created by save_posts (or a legacy JSON list of posts)."""
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    posts = payload.get("posts") if isinstance(payload, dict) else payload
    if not isinstance(posts, list):
        raise ValueError("Archive must contain a posts list.")

    required_fields = {"id", "author", "text", "url", "created_at"}
    invalid = [index for index, post in enumerate(posts) if not isinstance(post, dict) or not required_fields <= post.keys()]
    if invalid:
        raise ValueError(f"Archive contains invalid posts: {invalid}")
    return posts


def fetch_new_posts(
    max_items: int = 10,
    *,
    mark_seen: bool = True,
    save_to: Optional[Path] = DEFAULT_ARCHIVE_FILE,
) -> List[Dict[str, str]]:
    """Fetch new posts and optionally archive the normalized result as JSON."""
    client = ApifyClient(os.environ["APIFY_TOKEN"])

    run = client.actor(ACTOR_ID).call(
        run_input={
            "searchTerms": SEARCH_TERMS,
            "maxItems": max_items,
            "sort": "Latest",
            "tweetLanguage": "en",
        },
        timeout_secs=180,
    )
    if not run or run.get("status") != "SUCCEEDED":
        raise RuntimeError(f"Apify run failed: {run.get('status') if run else 'None'}")

    seen = _load_seen()
    posts = []

    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        tweet_id, text = item.get("id"), item.get("text")
        if not tweet_id or not text or tweet_id in seen:
            continue
        seen.add(tweet_id)
        posts.append({
            "id": tweet_id,
            "author": extract_author(item),
            "text": text,
            "url": item.get("url"),
            "created_at": item.get("createdAt"),
        })

    if mark_seen:
        _save_seen(seen)
    if save_to is not None:
        save_posts(posts, save_to)
    return posts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fetch X posts once or replay a saved archive without calling Apify."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input", type=Path, help="JSON archive to replay; Apify is not called")
    source.add_argument("--fetch", action="store_true", help="Fetch fresh posts from Apify (the default)")
    parser.add_argument("--max-items", type=int, default=10, help="Maximum posts requested from Apify")
    parser.add_argument("--output", type=Path, default=DEFAULT_ARCHIVE_FILE,
                        help=f"Archive path after --fetch (default: {DEFAULT_ARCHIVE_FILE})")
    parser.add_argument("--mark-seen", action="store_true",
                        help="Record fetched posts in .seen_tweets.json; main.py already does this")
    args = parser.parse_args()

    if args.input:
        posts = load_posts(args.input)
        print(f"📂 Loaded {len(posts)} posts from {args.input}; Apify was not called.")
    else:
        posts = fetch_new_posts(
            max_items=args.max_items,
            mark_seen=args.mark_seen,
            save_to=args.output,
        )
        print(f"💾 Saved {len(posts)} posts to {args.output}")

    for p in posts:
        print(f"@{p['author']}: {p['text']}")
