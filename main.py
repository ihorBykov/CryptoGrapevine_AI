import argparse
import json
import logging
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import os
from dotenv import load_dotenv
load_dotenv()

from scraper_x import (
    DEFAULT_ARCHIVE_FILE,
    DEFAULT_SEARCH_TERMS_FILE,
    fetch_new_posts,
    load_search_terms,
)
from brain import process_message, FILTERED_OUT
from incident_report import save_incident_report
from tavily_wallet_search import MAX_SOURCES_PER_SEARCH, search_wallet_cached
from telegram_notify import (
    send_telegram_document,
    send_telegram_message,
    format_incident_message,
)
from wallet_review import review_wallet


warnings.filterwarnings("ignore")
logging.captureWarnings(True)
logging.getLogger("py.warnings").setLevel(logging.ERROR)

MAX_TWEETS_PER_RUN = 10
FAILED_QUEUE_FILE = Path("failed_analysis_queue.json")
MAX_TAVILY_SEARCHES_PER_INCIDENT = 2
TAVILY_SEARCH_DEPTH = "basic"


def _tavily_priority(extracted_address) -> int:
    role = (extracted_address.role or "").lower()
    if any(marker in role for marker in ("attacker", "theft", "drainer", "mixer")):
        return 0
    if any(marker in role for marker in ("recipient", "deposit")):
        return 1
    if "victim" in role:
        return 3
    return 2


def _build_tavily_context(report, post: dict) -> str:
    post_text = " ".join(post.get("text", "").split())
    parts = (post.get("author", ""), report.incident_type or "", report.summary_en or "", post_text)
    return " ".join(part for part in parts if part)[:280]


def enrich_incident_addresses(report, post: dict):
    """Run on-chain analysis and up to two Tavily searches concurrently."""
    addresses = report.extracted_addresses
    tavily_enabled = bool(os.environ.get("TAVILY_API_KEY", "").strip())
    tavily_context = _build_tavily_context(report, post)
    tavily_addresses = sorted(addresses, key=_tavily_priority)[:MAX_TAVILY_SEARCHES_PER_INCIDENT]
    tavily_address_values = {item.address for item in tavily_addresses}
    wallet_by_address = {}
    tavily_by_address = {}

    if not tavily_enabled:
        for item in tavily_addresses:
            tavily_by_address[item.address] = {"skipped": "TAVILY_API_KEY is not configured"}

    workers = min(4, len(addresses) + (len(tavily_addresses) if tavily_enabled else 0))
    if workers:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(review_wallet, item.address, item.blockchain): ("wallet", item.address)
                for item in addresses
            }
            if tavily_enabled:
                futures.update({
                    executor.submit(
                        search_wallet_cached,
                        item.address,
                        tavily_context,
                        MAX_SOURCES_PER_SEARCH,
                        TAVILY_SEARCH_DEPTH,
                    ): ("tavily", item.address)
                    for item in tavily_addresses
                })

            for future in as_completed(futures):
                kind, address = futures[future]
                try:
                    result = future.result()
                except Exception as error:
                    if kind == "tavily":
                        tavily_by_address[address] = {"error": str(error)}
                        print(f"⚠️ Tavily could not process {address}: {error}")
                    else:
                        print(f"⚠️ Could not review wallet {address}: {error}")
                    continue
                if kind == "wallet":
                    wallet_by_address[address] = result
                else:
                    tavily_by_address[address] = result
                    source_count = len(result.get("result", {}).get("results", []))
                    fallback = "; address-only fallback" if result.get("fallback_to_address_only") else ""
                    print(f"🔎 Tavily: {address}, sources: {source_count}, "
                          f"cache: {'yes' if result.get('from_cache') else 'no'}{fallback}")

    for item in addresses:
        if item.address not in tavily_address_values:
            tavily_by_address[item.address] = {
                "skipped": f"incident limit: {MAX_TAVILY_SEARCHES_PER_INCIDENT} addresses"
            }

    return [wallet_by_address[item.address] for item in addresses if item.address in wallet_by_address], tavily_by_address


def _load_failed_queue() -> list:
    return json.loads(FAILED_QUEUE_FILE.read_text()) if FAILED_QUEUE_FILE.exists() else []

def _save_failed_queue(items: list) -> None:
    FAILED_QUEUE_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2))


def run_crypto_grapevine_pipeline(search_terms=None) -> None:
    print("\n" + "=" * 60)
    print("🚀 [PIPELINE] Starting Web3 OSINT scan...")
    print("=" * 60)

    pending = _load_failed_queue()
    if pending:
        print(f"🔁 Retrying {len(pending)} posts left from the previous failed run.")
        
    try:
        new_posts = fetch_new_posts(
            max_items=MAX_TWEETS_PER_RUN,
            save_to=DEFAULT_ARCHIVE_FILE,
            search_terms=search_terms,
        )
        print(f"💾 Parsing result saved to {DEFAULT_ARCHIVE_FILE}")
    except Exception as e:
        print(f"❌ [PIPELINE] X scraper failed: {e}")
        new_posts = []

    collected_posts = pending + new_posts

    if not collected_posts:
        print("📭 No posts available for analysis.")
        return

    print(f"\n📥 {len(collected_posts)} posts to analyze "
          f"(new: {len(new_posts)}, retried: {len(pending)}).")

    still_failed = []
    incidents_found = 0

    for post in collected_posts:
        text_to_analyze = f"[{post['author']}]: {post['text']}"
        report = process_message(text_to_analyze)
        print(f"📅 Post date: {post['created_at']}")

        if report is None:
            still_failed.append(post)

        elif report is FILTERED_OUT:
            continue

        elif report.is_incident:
            incidents_found += 1
            wallet_reviews, tavily_by_address = enrich_incident_addresses(report, post)
            report_file = save_incident_report(report=report, post=post, wallet_reviews=wallet_reviews,
                                               tavily_by_address=tavily_by_address)
            print(f"📎 Detailed report saved: {report_file}")

            send_telegram_message(
                format_incident_message(report, post, wallet_reviews=wallet_reviews,
                                        include_wallet_details=False)
            )
            send_telegram_document(report_file)


    _save_failed_queue(still_failed)

    print(f"\n⏱️ [PIPELINE] Run complete. "
          f"Processed: {len(collected_posts) - len(still_failed)}. "
          f"Incidents: {incidents_found}. "
          f"Queued for retry: {len(still_failed)}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the CryptoGrapevine incident-triage pipeline.")
    parser.add_argument("--search-terms-file", type=Path, default=DEFAULT_SEARCH_TERMS_FILE,
                        help=f"Search-term JSON file (default: {DEFAULT_SEARCH_TERMS_FILE})")
    parser.add_argument("--search-term", action="append",
                        help="Temporary search term override; repeat for multiple terms")
    args = parser.parse_args()
    search_terms = args.search_term or load_search_terms(args.search_terms_file)
    run_crypto_grapevine_pipeline(search_terms=search_terms)
