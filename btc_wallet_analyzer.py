#!/usr/bin/env python3
"""Analyze Bitcoin mainnet addresses using the public Blockstream Esplora API."""

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests

MAX_DETAILED_TRANSACTION_COUNT = 10_000


class TransactionHistoryTooLarge(ValueError):
    """Full history would require too many Blockstream pagination requests."""

    def __init__(self, address, summary):
        self.address = address
        self.summary = summary
        self.tx_count = summary["chain_stats"]["tx_count"]
        super().__init__(
            f"Address history contains {self.tx_count:,} confirmed transactions and exceeds "
            f"the automatic full-analysis limit of {MAX_DETAILED_TRANSACTION_COUNT:,}; "
            "manual verification is required."
        )

API_URL = "https://blockstream.info/api"
PAGE_SIZE = 25
SAMPLE_ADDRESS = "1BoatSLRHtKNngkdXEeobR76b53LETtpyT"


def normalize_address(address):
    """Check address syntax; the API also validates the address checksum."""
    if re.fullmatch(r"[13][1-9A-HJ-NP-Za-km-z]{25,34}", address):
        return address  # Base58 addresses are case-sensitive.
    if address == address.lower() or address == address.upper():
        if re.fullmatch(r"bc1[02-9ac-hj-np-z]{11,87}", address.lower()):
            return address.lower()
    raise ValueError("Expected a Bitcoin mainnet address (1..., 3... or bc1...)")


def load_addresses(file_path):
    addresses = []
    with file_path.open(encoding="utf-8-sig") as address_file:
        for line_number, line in enumerate(address_file, start=1):
            address = line.strip()
            if not address or address.startswith("#"):
                continue
            try:
                addresses.append(normalize_address(address))
            except ValueError:
                print(f"Skipping invalid address at {file_path}:{line_number}: {address}")
    return addresses


def get_json(session, url):
    """Retry temporary service/network failures, but never return partial data."""
    for attempt in range(3):
        time.sleep(0.4 if attempt == 0 else 2 ** attempt)
        try:
            response = session.get(url, timeout=30)
            if (response.status_code == 429 or response.status_code >= 500) and attempt < 2:
                continue
            response.raise_for_status()
            return response.json()
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
    raise ValueError("API request failed")


def get_btc_price(session):
    try:
        data = get_json(session, "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")
        return Decimal(str(data["bitcoin"]["usd"]))
    except (requests.RequestException, KeyError, ValueError, TypeError) as error:
        print(f"Could not fetch BTC price: {error}")
        return None


def get_transactions(session, address):
    """Fetch confirmed history in full, plus aggregate unconfirmed statistics."""
    summary = get_json(session, f"{API_URL}/address/{address}")

    tx_count = summary["chain_stats"]["tx_count"]
    if tx_count > MAX_DETAILED_TRANSACTION_COUNT:
        raise TransactionHistoryTooLarge(address, summary)

    transactions = {}
    cursor = None
    while True:
        url = f"{API_URL}/address/{address}/txs/chain"
        if cursor:
            url += f"/{cursor}"
        page = get_json(session, url)
        if not isinstance(page, list):
            raise ValueError("Invalid transaction history response")
        previous_count = len(transactions)
        for tx in page:
            if not tx["status"]["confirmed"]:
                raise ValueError("Unconfirmed transaction in confirmed history")
            transactions[tx["txid"]] = tx
        print(f"Loaded {len(transactions)} confirmed transactions")
        if page and len(transactions) == previous_count:
            raise ValueError("History pagination stopped advancing; report skipped")
        if len(page) < PAGE_SIZE:
            break
        cursor = page[-1]["txid"]

    if len(transactions) != summary["chain_stats"]["tx_count"]:
        raise ValueError("History count differs from address statistics (history changed or is incomplete); rerun analysis")
    return list(transactions.values()), summary


def btc(satoshis):
    return Decimal(f"{satoshis}e-8")


def get_balance_summary(summary):
    """Calculate confirmed balance and pending mempool change from address summary."""
    confirmed = summary["chain_stats"]
    mempool = summary.get("mempool_stats", {})
    confirmed_satoshis = int(confirmed["funded_txo_sum"]) - int(confirmed["spent_txo_sum"])
    pending_satoshis = int(mempool.get("funded_txo_sum", 0)) - int(mempool.get("spent_txo_sum", 0))
    return {
        "confirmed_btc": btc(confirmed_satoshis),
        "pending_mempool_btc": btc(pending_satoshis),
        "confirmed_tx_count": int(confirmed["tx_count"]),
        "mempool_tx_count": int(mempool.get("tx_count", 0)),
    }


def analyze_transactions(transactions, address):
    """Sum outputs received and previous outputs spent by this specific address."""
    address = normalize_address(address)
    transactions = {tx["txid"]: tx for tx in transactions}.values()
    received = spent = 0
    timestamps = []
    for tx in transactions:
        if not tx["status"]["confirmed"]:
            raise ValueError("Analysis requires confirmed transactions")
        timestamps.append(int(tx["status"]["block_time"]))
        for output in tx["vout"]:
            if output.get("scriptpubkey_address") == address:
                received += int(output["value"])
        for tx_input in tx["vin"]:
            if tx_input.get("is_coinbase"):
                continue
            previous_output = tx_input["prevout"]
            if previous_output is None:
                raise ValueError("Missing previous output; cannot calculate BTC volume")
            if previous_output.get("scriptpubkey_address") == address:
                spent += int(previous_output["value"])
    return {
        "total_transactions": len(transactions),
        "first_transaction": datetime.fromtimestamp(min(timestamps), timezone.utc) if timestamps else None,
        "last_transaction": datetime.fromtimestamp(max(timestamps), timezone.utc) if timestamps else None,
        "received_satoshis": received,
        "spent_satoshis": spent,
        "inbound_btc": btc(received),
        "outbound_btc": btc(spent),
        "net_flow": btc(received - spent),
    }


def verify_totals(analysis, summary):
    stats = summary["chain_stats"]
    if (analysis["received_satoshis"] != stats["funded_txo_sum"]
            or analysis["spent_satoshis"] != stats["spent_txo_sum"]):
        raise ValueError("History amounts differ from address statistics; rerun analysis")


def format_usd(amount, price):
    return f"${amount * price:,.2f}" if price is not None else "N/A (price unavailable)"


def print_analysis(address, analysis, price, unconfirmed_count=0):
    print("\n" + "=" * 65)
    print("BITCOIN ADDRESS ANALYSIS RESULTS")
    print(f"Address: {address}")
    print("=" * 65)


def print_manual_verification_summary(address, summary, price):
    """Report summary data when the complete history is too large to analyze."""
    balances = get_balance_summary(summary)
    print("\n" + "=" * 65)
    print("BITCOIN ADDRESS MANUAL VERIFICATION REQUIRED")
    print(f"Address: {address}")
    print("=" * 65)
    print(f"Confirmed Transactions: {balances['confirmed_tx_count']:,}")
    print(f"Confirmed BTC Balance: {balances['confirmed_btc']:.8f} BTC "
          f"({format_usd(balances['confirmed_btc'], price)})")
    print(f"Unconfirmed Transactions: {balances['mempool_tx_count']}")
    print(f"Pending Mempool Balance Change: {balances['pending_mempool_btc']:+.8f} BTC")
    print(f"⚠️ History exceeds {MAX_DETAILED_TRANSACTION_COUNT:,} confirmed transactions; "
          "full volume analysis requires manual verification.")
    print("=" * 65)
    print(f"Unique Transactions (confirmed): {analysis['total_transactions']}")
    first, last = analysis["first_transaction"], analysis["last_transaction"]
    if first is not None and last is not None:
        print(f"  First Transaction (UTC, block time): {first:%Y-%m-%d %H:%M:%S}")
        print(f"  Last Transaction (UTC, block time):  {last:%Y-%m-%d %H:%M:%S}")
        span = last - first
        hours, remainder = divmod(span.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        print(f"  Activity Span (first to last): {span.days} days, {hours:02}:{minutes:02}:{seconds:02}")
    else:
        print("  First Transaction (UTC): N/A")
        print("  Last Transaction (UTC): N/A")
        print("  Activity Span (first to last): N/A")
    print(f"Unconfirmed Transactions (excluded from dates and amounts): {unconfirmed_count}")
    print("-" * 65)
    if price is not None:
        print(f"Current BTC Price: ${price:,.2f}")
    for label, key in (("Received BTC (outputs, including change)", "inbound_btc"),
                       ("Spent BTC (inputs)", "outbound_btc"),
                       ("Net Flow (received - spent)", "net_flow")):
        print(f"{label}: {analysis[key]:.8f} BTC ({format_usd(analysis[key], price)})")
    print("Spent inputs include funds used for change and transaction fees.")
    print("This report covers one address; other addresses in the same wallet are not grouped.")
    print("=" * 65)


def get_sample_transactions():
    return [
        {"txid": "sample-in", "status": {"confirmed": True, "block_time": 1704067200},
         "vin": [{"prevout": {"scriptpubkey_address": "other", "value": 100010000}}],
         "vout": [{"scriptpubkey_address": SAMPLE_ADDRESS, "value": 100000000}]},
        {"txid": "sample-out", "status": {"confirmed": True, "block_time": 1704240000},
         "vin": [{"prevout": {"scriptpubkey_address": SAMPLE_ADDRESS, "value": 100000000}}],
         "vout": [{"scriptpubkey_address": "other", "value": 40000000},
                  {"scriptpubkey_address": SAMPLE_ADDRESS, "value": 59990000}]},
    ]


def main():
    parser = argparse.ArgumentParser(description="Analyze Bitcoin mainnet addresses from a text file, one per line.")
    parser.add_argument("file", nargs="?", type=Path,
                        default=Path(__file__).resolve().with_name("btc_wallets.txt"),
                        help="Address file (default: btc_wallets.txt next to this script)")
    parser.add_argument("--test", action="store_true", help="Use sample data without API requests")
    args = parser.parse_args()
    if args.test:
        print("Running in TEST mode with sample data and a mock BTC price...")
        print_analysis(SAMPLE_ADDRESS, analyze_transactions(get_sample_transactions(), SAMPLE_ADDRESS), Decimal(60000))
        return
    try:
        addresses = load_addresses(args.file)
    except (OSError, UnicodeError) as error:
        print(f"Could not read address file {args.file}: {error}", file=sys.stderr)
        sys.exit(1)
    if not addresses:
        print(f"No addresses found. Add one Bitcoin mainnet address per line to {args.file}.")
        return
    with requests.Session() as session:
        print(f"Loaded {len(addresses)} addresses from {args.file}")
        print("Fetching current BTC price...")
        price = get_btc_price(session)
        for index, address in enumerate(addresses, start=1):
            print(f"\n[{index}/{len(addresses)}] Analyzing address: {address}")
            try:
                transactions, summary = get_transactions(session, address)
                analysis = analyze_transactions(transactions, address)
                verify_totals(analysis, summary)
                print_analysis(address, analysis, price, summary["mempool_stats"]["tx_count"])
            except TransactionHistoryTooLarge as error:
                print_manual_verification_summary(address, error.summary, price)
            except (requests.RequestException, KeyError, ValueError, TypeError) as error:
                print(f"Could not analyze address {address}: {error}")


if __name__ == "__main__":
    main()
