#!/usr/bin/env python3
"""
Ethereum Wallet Analyzer
Analyzes transaction history for Ethereum wallet addresses from a text file.
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
from dotenv import load_dotenv

ETHERSCAN_V2_API_URL = "https://api.etherscan.io/v2/api"
ETHERSCAN_RESULT_WINDOW_LIMIT = 10_000


class TransactionHistoryTooLarge(ValueError):
    """Etherscan cannot paginate a single block range past its 10,000-record window."""

    def __init__(self, action):
        self.action = action
        super().__init__(
            f"{action} history exceeds Etherscan's "
            f"{ETHERSCAN_RESULT_WINDOW_LIMIT:,}-record window; manual verification is required."
        )

def get_eth_price():
    """
    Fetch current ETH price in USD using CoinGecko API (free, no API key required)
    """
    try:
        response = requests.get('https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd', timeout=30)
        response.raise_for_status()
        data = response.json()
        return data['ethereum']['usd']
    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch ETH price: {e}")
        return None


def get_eth_balance(api_key, address):
    """Fetch the latest native ETH balance and convert wei to ETH."""
    params = {
        "module": "account",
        "action": "balance",
        "address": address,
        "tag": "latest",
        "chainid": 1,
        "apikey": api_key,
    }
    try:
        response = requests.get(ETHERSCAN_V2_API_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as error:
        raise ValueError(f"balance: request failed ({type(error).__name__})") from None

    raw_balance = data.get("result")
    if data.get("status") != "1" or not isinstance(raw_balance, str) or not raw_balance.isdigit():
        detail = str(raw_balance or data.get("message", "Invalid API response"))
        raise ValueError(f"balance: {detail.replace(api_key, '[REDACTED]')}")
    return Decimal(raw_balance).scaleb(-18)


def _get_sent_transaction_edge(api_key, address, sort):
    """Fetch one earliest or latest normal transaction sent by an address."""
    params = {
        "module": "account",
        "action": "txlist",
        "from": address,
        "fromto_opr": "or",
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": 1,
        "sort": sort,
        "chainid": 1,
        "apikey": api_key,
    }
    try:
        response = requests.get(ETHERSCAN_V2_API_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as error:
        raise ValueError(f"sent transaction edge: request failed ({type(error).__name__})") from None

    records = data.get("result")
    if data.get("status") == "0" and data.get("message") == "No transactions found" and records == []:
        return None
    if data.get("status") != "1" or not isinstance(records, list) or len(records) > 1:
        detail = str(records or data.get("message", "Invalid API response"))
        raise ValueError(f"sent transaction edge: {detail.replace(api_key, '[REDACTED]')}")
    return records[0] if records else None


def get_sent_transaction_boundaries(api_key, address):
    """Return the first and last normal transactions sent without fetching full history."""
    return (
        _get_sent_transaction_edge(api_key, address, "asc"),
        _get_sent_transaction_edge(api_key, address, "desc"),
    )


def format_sent_transaction_boundaries(first_sent, last_sent):
    """Format sent-transaction timestamps and activity span for a report."""
    if not first_sent or not last_sent:
        return "No sent normal transactions found."

    first_dt = datetime.fromtimestamp(int(first_sent["timeStamp"]), timezone.utc)
    last_dt = datetime.fromtimestamp(int(last_sent["timeStamp"]), timezone.utc)
    span = last_dt - first_dt
    hours, remainder = divmod(span.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (
        f"First Sent Normal Transaction (UTC): {first_dt:%Y-%m-%d %H:%M:%S}\n"
        f"Last Sent Normal Transaction (UTC): {last_dt:%Y-%m-%d %H:%M:%S}\n"
        f"Sent Normal Transaction Activity Span: "
        f"{span.days} days, {hours:02}:{minutes:02}:{seconds:02}"
    )


def fetch_transaction_list(api_key, address, action, offset=1000, max_pages=None):
    """Fetch pages up to max_pages (None = all pages)."""
    transactions = []
    page = 1
    while True:
        params = {
            "module": "account", "action": action, "address": address,
            "startblock": 0, "endblock": 99999999, "page": page,
            "offset": offset, "sort": "asc", "chainid": 1, "apikey": api_key,
        }
        time.sleep(0.4)
        try:
            response = requests.get(ETHERSCAN_V2_API_URL, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as error:
            raise ValueError(f"{action}: request failed ({type(error).__name__})") from None

        records = data.get("result")
        if data.get("status") == "0" and data.get("message") == "No transactions found" and records == []:
            break
        if data.get("status") != "1" or not isinstance(records, list):
            detail = str(records or data.get("message", "Invalid API response"))
            if "Result window is too large" in detail:
                raise TransactionHistoryTooLarge(action)
            raise ValueError(f"{action}: {detail.replace(api_key, '[REDACTED]')}")
        transactions.extend(records)
        print(f"{action} page {page}: Found {len(records)} records")
        if len(records) < offset:
            break
        if max_pages and page >= max_pages:
            break
        page += 1
    return transactions


def get_transactions(api_key, address):
    """Return complete normal, internal ETH and ERC-20 transaction histories.

    Etherscan returns up to 1,000 records per page. A full page only means that
    another page may exist; it is not evidence that the address is an exchange.
    """
    print("Loading complete normal transaction history...")
    normal = fetch_transaction_list(api_key, address, "txlist")

    print("Loading complete internal ETH operation history...")
    internal = fetch_transaction_list(api_key, address, "txlistinternal")

    print("Loading complete ERC-20 transfer history...")
    tokens = fetch_transaction_list(api_key, address, "tokentx")

    return normal, internal, tokens


def analyze_transactions(transactions, address, internal_transactions=None, token_transactions=None):
    """Count unique hashes; sum successful normal/internal ETH values in Wei."""
    internal_transactions = internal_transactions or []
    token_transactions = token_transactions or []
    wallet = address.lower()
    inbound_wei = 0
    outbound_wei = 0
    hashes = set()
    timestamps = []
    failed_eth_operations = 0

    for group in (transactions, internal_transactions, token_transactions):
        hashes.update(tx["hash"].lower() for tx in group)
        timestamps.extend(int(tx["timeStamp"]) for tx in group if tx.get("timeStamp") not in (None, ""))

    first_transaction = datetime.fromtimestamp(min(timestamps), timezone.utc) if timestamps else None
    last_transaction = datetime.fromtimestamp(max(timestamps), timezone.utc) if timestamps else None

    for group in (transactions, internal_transactions):
        for tx in group:
            if tx.get("isError") == "1" or tx.get("txreceipt_status") == "0":
                failed_eth_operations += 1
                continue
            value_wei = int(tx["value"])
            recipient = tx.get("to") or tx.get("contractAddress", "")
            if recipient.lower() == wallet:
                inbound_wei += value_wei
            if tx["from"].lower() == wallet:
                outbound_wei += value_wei

    return {
        "total_transactions": len(hashes),
        "first_transaction": first_transaction,
        "last_transaction": last_transaction,
        "normal_eth_transactions": len(transactions),
        "internal_eth_operations": len(internal_transactions),
        "erc20_transfers": len(token_transactions),
        "failed_eth_operations": failed_eth_operations,
        "inbound_eth": Decimal(f"{inbound_wei}e-18"),
        "outbound_eth": Decimal(f"{outbound_wei}e-18"),
        "net_flow": Decimal(f"{inbound_wei - outbound_wei}e-18"),
    }


def format_usd(amount_eth, eth_price_usd):
    """
    Convert ETH amount to USD and format it
    """
    if eth_price_usd is None:
        return "N/A (price unavailable)"
    usd_value = Decimal(str(amount_eth)) * Decimal(str(eth_price_usd))
    return f"${usd_value:,.2f}"

def get_sample_transactions():
    """
    Return sample transaction data for testing without API key
    """
    return [
        {
            'from': '0xABC1234567890ABCDEF1234567890ABCDEF123456',
            'to': '0xTestWalletAddress1234567890123456789012345678',
            'value': '1000000000000000000',  # 1 ETH in Wei
            'hash': '0x1234567890abcdef',
            'timeStamp': '1704067200',  # 2024-01-01 00:00:00 UTC
        },
        {
            'from': '0xTestWalletAddress1234567890123456789012345678',
            'to': '0xDEF9876543210FEDCBA9876543210FEDCBA987654',
            'value': '500000000000000000',  # 0.5 ETH in Wei
            'hash': '0xabcdef1234567890',
            'timeStamp': '1704153600',  # 2024-01-02 00:00:00 UTC
        },
        {
            'from': '0xXYZ999999999999999999999999999999999999999',
            'to': '0xTestWalletAddress1234567890123456789012345678',
            'value': '2000000000000000000',  # 2 ETH in Wei
            'hash': '0x567890abcdef1234',
            'timeStamp': '1704240000',  # 2024-01-03 00:00:00 UTC
        }
    ]

def load_addresses(file_path):
    """Read one address per line, skipping blanks, comments and invalid entries."""
    addresses = []
    with file_path.open(encoding="utf-8-sig") as address_file:
        for line_number, line in enumerate(address_file, start=1):
            address = line.strip()
            if not address or address.startswith("#"):
                continue
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
                print(f"Skipping invalid address at {file_path}:{line_number}: {address}")
                continue
            addresses.append(address)
    return addresses


def print_analysis(address, analysis, eth_price_usd, eth_balance=None):
    """Print a separate report identifying the wallet being analyzed."""
    print("\n" + "="*50)
    print("WALLET ANALYSIS RESULTS")
    print(f"Wallet: {address}")
    print("="*50)
    print(f"Unique Transactions (normal/internal/ERC-20): {analysis['total_transactions']}")
    first_transaction = analysis['first_transaction']
    last_transaction = analysis['last_transaction']
    if first_transaction is not None and last_transaction is not None:
        print(f"  First Transaction (UTC): {first_transaction:%Y-%m-%d %H:%M:%S}")
        print(f"  Last Transaction (UTC):  {last_transaction:%Y-%m-%d %H:%M:%S}")
        span = last_transaction - first_transaction
        hours, remainder = divmod(span.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        print(f"  Activity Span (first to last): {span.days} days, {hours:02}:{minutes:02}:{seconds:02}")
    else:
        print("  First Transaction (UTC): N/A")
        print("  Last Transaction (UTC):  N/A")
        print("  Activity Span (first to last): N/A")
    print(f"Normal ETH Transactions: {analysis['normal_eth_transactions']}")
    print(f"Internal ETH Operations: {analysis['internal_eth_operations']}")
    print(f"ERC-20 Transfers: {analysis['erc20_transfers']}")
    print(f"Failed ETH Operations (excluded from volume): {analysis['failed_eth_operations']}")
    if eth_price_usd:
        print(f"Current ETH Price: ${eth_price_usd:.2f}")
    if eth_balance is not None:
        print(f"Current ETH Balance: {eth_balance:.6f} ETH ({format_usd(eth_balance, eth_price_usd)})")
    print("-" * 50)
    print(f"Inbound ETH: {analysis['inbound_eth']:.6f} ETH ({format_usd(analysis['inbound_eth'], eth_price_usd)})")
    print(f"Outbound ETH: {analysis['outbound_eth']:.6f} ETH ({format_usd(analysis['outbound_eth'], eth_price_usd)})")
    print("ETH volume includes normal and internal operations; excludes gas fees.")
    print(f"Net Flow: {analysis['net_flow']:.6f} ETH ({format_usd(analysis['net_flow'], eth_price_usd)})")
    print("="*50)


def main():
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Analyze Ethereum wallets from a text file, one address per line.")
    parser.add_argument("file", nargs="?", type=Path, default=script_dir / "eth_wallets.txt",
                        help="Address file (default: eth_wallets.txt next to this script)")
    parser.add_argument("--test", action="store_true", help="Run with sample data without API requests")
    args = parser.parse_args()
    load_dotenv(script_dir / ".env")

    if args.test:
        print("Running in TEST mode with sample data...")
        address = "0xTestWalletAddress1234567890123456789012345678"
        transactions = get_sample_transactions()
        analysis = analyze_transactions(transactions, address)
        print_analysis(address, analysis, 3000.00, Decimal("1.250000"))
        return

    try:
        addresses = load_addresses(args.file)
    except (OSError, UnicodeError) as error:
        print(f"Could not read address file {args.file}: {error}", file=sys.stderr)
        sys.exit(1)

    if not addresses:
        print(f"No valid addresses found. Add one Ethereum address per line to {args.file}.")
        return

    api_key = os.getenv("ETHERSCAN_API_KEY", "").strip()
    if not api_key:
        print("Missing ETHERSCAN_API_KEY. Set it in .env or your environment.", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(addresses)} wallets from {args.file}")
    print("Fetching current ETH price...")
    eth_price_usd = get_eth_price()

    for index, address in enumerate(addresses, start=1):
        print(f"\n[{index}/{len(addresses)}] Analyzing wallet: {address}")
        try:
            eth_balance = get_eth_balance(api_key, address)
            transactions, internal, tokens = get_transactions(api_key, address)
            analysis = analyze_transactions(transactions, address, internal, tokens)
            print_analysis(address, analysis, eth_price_usd, eth_balance)
        except TransactionHistoryTooLarge as error:
            try:
                first_sent, last_sent = get_sent_transaction_boundaries(api_key, address)
            except (KeyError, TypeError, ValueError):
                first_sent = last_sent = None
            print(
                f"Current ETH Balance: {eth_balance:.6f} ETH "
                f"({format_usd(eth_balance, eth_price_usd)})\n"
                f"⚠️ {error}\n"
                f"{format_sent_transaction_boundaries(first_sent, last_sent)}"
            )
        except (requests.exceptions.RequestException, KeyError, ValueError, TypeError) as error:
            print(f"Could not analyze wallet {address}: {error}")
            continue


if __name__ == "__main__":
    main()
