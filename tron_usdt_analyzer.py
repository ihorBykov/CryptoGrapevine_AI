#!/usr/bin/env python3
"""Analyze confirmed USDT (TRC-20) transfers for TRON mainnet addresses."""

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests

TRONSCAN_API = "https://apilist.tronscanapi.com/api/token_trc20/transfers"
TRX_API = "https://apilist.tronscanapi.com/api/asset/transfer"
ACCOUNT_LIST_API = "https://apilist.tronscanapi.com/api/account/list"
TOKEN_ASSET_OVERVIEW_API = "https://apilist.tronscanapi.com/api/account/token_asset_overview"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
PAGE_SIZE = 50
MAX_TRANSFERS = 10_000
SAMPLE_ADDRESS = "TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb"


class TransactionHistoryTooLarge(ValueError):
    """The complete transfer history cannot be retrieved through TronScan."""

    def __init__(self, address, asset, minimum_transfer_count=MAX_TRANSFERS):
        self.address = address
        self.asset = asset
        self.minimum_transfer_count = minimum_transfer_count
        super().__init__(
            f"{asset} transfer history contains at least {minimum_transfer_count:,} operations "
            "and exceeds TronScan's full-export limit; manual verification is required."
        )


def normalize_address(address):
    """Check TRON Base58 address syntax; TronScan validates its checksum."""
    if not re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", address):
        raise ValueError("Expected a TRON mainnet address starting with T")
    return address


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


def get_json(session, url, params):
    """Retry transient failures. All requests are read-only and have timeouts."""
    for attempt in range(3):
        time.sleep(0.4 if attempt == 0 else 2 ** attempt)
        try:
            response = session.get(url, params=params, timeout=30)
            if (response.status_code == 429 or response.status_code >= 500) and attempt < 2:
                continue
            response.raise_for_status()
            return response.json()
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
    raise ValueError("API request failed")


def get_usdt_price(session):
    try:
        data = get_json(
            session,
            "https://api.coingecko.com/api/v3/simple/price",
            {"ids": "tether", "vs_currencies": "usd"},
        )
        return Decimal(str(data["tether"]["usd"]))
    except (requests.RequestException, KeyError, ValueError, TypeError) as error:
        print(f"Could not fetch USDT price: {error}")
        return None


def get_trx_price(session):
    try:
        data = get_json(
            session,
            "https://api.coingecko.com/api/v3/simple/price",
            {"ids": "tron", "vs_currencies": "usd"},
        )
        return Decimal(str(data["tron"]["usd"]))
    except (requests.RequestException, KeyError, ValueError, TypeError) as error:
        print(f"Could not fetch TRX price: {error}")
        return None


def get_account_balances(session, address):
    """Return current native TRX and USDT (TRC-20) balances from TronScan."""
    address = normalize_address(address)
    account_data = get_json(session, ACCOUNT_LIST_API, {
        "address": address,
        "start": 0,
        "limit": 1,
    })
    accounts = account_data.get("data")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("TronScan did not return account balance data")

    account = accounts[0]
    try:
        trx_balance = Decimal(str(account["balance"])).scaleb(-6)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Invalid TronScan TRX balance") from error

    token_data = get_json(session, TOKEN_ASSET_OVERVIEW_API, {
        "address": address,
        "sort": "false",
    })
    tokens = token_data.get("data")
    if not isinstance(tokens, list):
        raise ValueError("Invalid TronScan token balance response")

    usdt = next((token for token in tokens if token.get("tokenId") == USDT_CONTRACT), None)
    if usdt is None:
        usdt_balance = Decimal(0)
    else:
        try:
            usdt_balance = Decimal(str(usdt["balance"])).scaleb(-int(usdt["tokenDecimal"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Invalid TronScan USDT balance") from error

    transaction_count = account.get("totalTransactionCount")
    try:
        transaction_count = int(transaction_count) if transaction_count is not None else None
    except (TypeError, ValueError):
        transaction_count = None

    return {
        "trx_balance": trx_balance,
        "usdt_balance": usdt_balance,
        "account_transaction_count": transaction_count,
    }


def get_transfers(session, address):
    """Fetch all confirmed USDT transfer events when the full history is available."""
    transfers = []
    for start in range(0, MAX_TRANSFERS, PAGE_SIZE):
        data = get_json(session, TRONSCAN_API, {
            "contract_address": USDT_CONTRACT,
            "relatedAddress": address,
            "confirm": "0",
            "start": start,
            "limit": PAGE_SIZE,
        })
        page = data.get("token_transfers")
        if not isinstance(page, list):
            raise ValueError("Invalid TronScan response")
        transfers.extend(page)
        print(f"Loaded {len(transfers)} confirmed USDT transfers")
        if len(page) < PAGE_SIZE:
            return transfers

    raise TransactionHistoryTooLarge(address, "USDT (TRC-20)")


def get_trx_transfers(session, address):
    """Fetch all confirmed TRX transfer records when the full history is available."""
    transfers = []
    for start in range(0, MAX_TRANSFERS, PAGE_SIZE):
        data = get_json(session, TRX_API, {
            "name": "trx", "relatedAddress": address, "confirm": "0",
            "start": start, "limit": PAGE_SIZE,
        })
        page = data.get("Data")
        if not isinstance(page, list):
            raise ValueError("Invalid TronScan TRX response")
        transfers.extend(page)
        print(f"Loaded {len(transfers)} confirmed TRX transfers")
        if len(page) < PAGE_SIZE:
            return transfers

    raise TransactionHistoryTooLarge(address, "TRX")


def analyze_transfers(transfers, address):
    """Calculate USDT event counts and amounts using raw integer token quantities."""
    address = normalize_address(address)
    inbound_raw = outbound_raw = 0
    inbound_count = outbound_count = 0
    timestamps = []
    transfer_ids = set()

    for transfer in transfers:
        if transfer.get("contract_address") != USDT_CONTRACT:
            raise ValueError("Response contains a transfer from another token contract")
        if transfer.get("event_type") not in (None, "Transfer"):
            raise ValueError("Response contains a non-transfer event")
        if transfer.get("confirmed") is False or transfer.get("contractRet") not in (None, "SUCCESS"):
            raise ValueError("Response contains an unsuccessful or unconfirmed transfer")
        decimals = int(transfer.get("tokenInfo", {}).get("tokenDecimal", 6))
        if decimals != 6:
            raise ValueError("Unexpected USDT token decimal precision")
        amount = int(transfer["quant"])
        if amount < 0:
            raise ValueError("Invalid negative USDT transfer amount")
        transfer_id = (transfer["transaction_id"], transfer.get("from_address"),
                       transfer.get("to_address"), str(amount), transfer.get("block_ts"))
        if transfer_id in transfer_ids:
            continue
        transfer_ids.add(transfer_id)
        timestamps.append(int(transfer["block_ts"]) // 1000)
        if transfer.get("to_address") == address:
            inbound_raw += amount
            inbound_count += 1
        if transfer.get("from_address") == address:
            outbound_raw += amount
            outbound_count += 1

    def usdt(raw):
        return Decimal(raw).scaleb(-6)

    return {
        "total_operations": len(transfer_ids),
        "inbound_operations": inbound_count,
        "outbound_operations": outbound_count,
        "first_transaction": datetime.fromtimestamp(min(timestamps), timezone.utc) if timestamps else None,
        "last_transaction": datetime.fromtimestamp(max(timestamps), timezone.utc) if timestamps else None,
        "inbound_usdt": usdt(inbound_raw),
        "outbound_usdt": usdt(outbound_raw),
        "total_volume_usdt": usdt(inbound_raw + outbound_raw),
        "net_flow_usdt": usdt(inbound_raw - outbound_raw),
    }


def analyze_trx_transfers(transfers, address):
    """Calculate native TRX transfer counts and amounts; 1 TRX is 1,000,000 sun."""
    address = normalize_address(address)
    inbound_raw = outbound_raw = 0
    inbound_count = outbound_count = 0
    timestamps = []
    transfer_ids = set()

    for transfer in transfers:
        if transfer.get("confirmed") is False or transfer.get("contractRet") not in (None, "SUCCESS"):
            raise ValueError("Response contains an unsuccessful or unconfirmed TRX transfer")
        amount = int(transfer["amount"])
        if amount < 0:
            raise ValueError("Invalid negative TRX transfer amount")
        transfer_id = (transfer["transactionHash"], transfer.get("transferFromAddress"),
                       transfer.get("transferToAddress"), str(amount), transfer.get("timestamp"))
        if transfer_id in transfer_ids:
            continue
        transfer_ids.add(transfer_id)
        timestamps.append(int(transfer["timestamp"]) // 1000)
        if transfer.get("transferToAddress") == address:
            inbound_raw += amount
            inbound_count += 1
        if transfer.get("transferFromAddress") == address:
            outbound_raw += amount
            outbound_count += 1

    def trx(raw):
        return Decimal(raw).scaleb(-6)

    return {
        "trx_operation_ids": transfer_ids,
        "total_trx_operations": len(transfer_ids),
        "inbound_trx_operations": inbound_count,
        "outbound_trx_operations": outbound_count,
        "first_trx_transfer": datetime.fromtimestamp(min(timestamps), timezone.utc) if timestamps else None,
        "last_trx_transfer": datetime.fromtimestamp(max(timestamps), timezone.utc) if timestamps else None,
        "inbound_trx": trx(inbound_raw),
        "outbound_trx": trx(outbound_raw),
        "total_trx_volume": trx(inbound_raw + outbound_raw),
        "net_trx_flow": trx(inbound_raw - outbound_raw),
    }


def analyze_activity(usdt_transfers, trx_transfers, address):
    """Combine USDT and native-TRX results while counting transaction hashes once."""
    usdt = analyze_transfers(usdt_transfers, address)
    trx = analyze_trx_transfers(trx_transfers, address)
    transaction_hashes = {transfer["transaction_id"] for transfer in usdt_transfers}
    transaction_hashes.update(transfer["transactionHash"] for transfer in trx_transfers)
    timestamps = [date for date in (
        usdt["first_transaction"], usdt["last_transaction"],
        trx["first_trx_transfer"], trx["last_trx_transfer"],
    ) if date is not None]
    return {
        **usdt,
        **trx,
        "total_transactions": len(transaction_hashes),
        "first_transaction": min(timestamps) if timestamps else None,
        "last_transaction": max(timestamps) if timestamps else None,
    }


def format_usd(amount, price):
    return f"${amount * price:,.2f}" if price is not None else "N/A (price unavailable)"


def format_manual_verification_summary(error, balances, usdt_price, trx_price):
    """Build the useful partial report available when transfer history is capped."""
    lines = []
    if balances is None:
        lines.append("Current TRX and USDT balances are unavailable.")
    else:
        account_count = balances["account_transaction_count"]
        if account_count is not None:
            lines.append(f"TronScan account transactions: {account_count:,}")
        lines.extend((
            f"Current USDT balance (TRC-20): {balances['usdt_balance']:,.6f} USDT "
            f"({format_usd(balances['usdt_balance'], usdt_price)})",
            f"Current TRX balance: {balances['trx_balance']:,.6f} TRX "
            f"({format_usd(balances['trx_balance'], trx_price)})",
        ))
    lines.append(f"⚠️ {error}")
    return "\n".join(lines)


def print_analysis(address, analysis, usdt_price, trx_price):
    print("\n" + "=" * 65)
    print("TRON ADDRESS ANALYSIS RESULTS")
    print(f"Address: {address}")
    print("=" * 65)
    first, last = analysis["first_transaction"], analysis["last_transaction"]
    if first is not None and last is not None:
        print(f"  First Transfer (UTC): {first:%Y-%m-%d %H:%M:%S}")
        print(f"  Last Transfer (UTC):  {last:%Y-%m-%d %H:%M:%S}")
        span = last - first
        hours, remainder = divmod(span.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        print(f"  Activity Span (first to last): {span.days} days, {hours:02}:{minutes:02}:{seconds:02}")
    else:
        print("  First Transfer (UTC): N/A")
        print("  Last Transfer (UTC):  N/A")
        print("  Activity Span (first to last): N/A")
    print("-" * 65)
    if usdt_price is not None:
        print(f"Current USDT Price: ${usdt_price:,.6f}")
    print(f"USDT Transfer Operations: {analysis['total_operations']}")
    print(f"Total USDT Volume (in + out): {analysis['total_volume_usdt']:,.6f} USDT ({format_usd(analysis['total_volume_usdt'], usdt_price)})")
    print(f"Inbound USDT: {analysis['inbound_usdt']:,.6f} USDT ({format_usd(analysis['inbound_usdt'], usdt_price)})")
    print(f"Inbound Operations: {analysis['inbound_operations']}")
    print(f"Outbound USDT: {analysis['outbound_usdt']:,.6f} USDT ({format_usd(analysis['outbound_usdt'], usdt_price)})")
    print(f"Outbound Operations: {analysis['outbound_operations']}")
    print(f"Net USDT Flow (in - out): {analysis['net_flow_usdt']:,.6f} USDT ({format_usd(analysis['net_flow_usdt'], usdt_price)})")
    print("-" * 65)
    if trx_price is not None:
        print(f"Current TRX Price: ${trx_price:,.6f}")
    print(f"TRX Transfer Operations: {analysis['total_trx_operations']}")
    print(f"Total TRX Volume (in + out): {analysis['total_trx_volume']:,.6f} TRX ({format_usd(analysis['total_trx_volume'], trx_price)})")
    print(f"Inbound TRX: {analysis['inbound_trx']:,.6f} TRX ({format_usd(analysis['inbound_trx'], trx_price)})")
    print(f"Inbound TRX Operations: {analysis['inbound_trx_operations']}")
    print(f"Outbound TRX: {analysis['outbound_trx']:,.6f} TRX ({format_usd(analysis['outbound_trx'], trx_price)})")
    print(f"Outbound TRX Operations: {analysis['outbound_trx_operations']}")
    print(f"Net TRX Flow (in - out): {analysis['net_trx_flow']:,.6f} TRX ({format_usd(analysis['net_trx_flow'], trx_price)})")
    print("USDT TRC-20 and native TRX transfers only; TRC-10 tokens and TRX fees are excluded.")
    print("=" * 65)


def print_manual_verification_summary(address, error, balances, usdt_price, trx_price):
    print("\n" + "=" * 65)
    print("TRON ADDRESS ANALYSIS RESULTS")
    print(f"Address: {address}")
    print("=" * 65)
    print(format_manual_verification_summary(error, balances, usdt_price, trx_price))
    print("=" * 65)


def get_sample_transfers():
    return [
        {"transaction_id": "sample-in", "from_address": "TOther", "to_address": SAMPLE_ADDRESS,
         "contract_address": USDT_CONTRACT, "quant": "125050000", "block_ts": 1704067200000,
         "confirmed": True, "contractRet": "SUCCESS", "event_type": "Transfer",
         "tokenInfo": {"tokenDecimal": 6}},
        {"transaction_id": "sample-out", "from_address": SAMPLE_ADDRESS, "to_address": "TOther",
         "contract_address": USDT_CONTRACT, "quant": "25000000", "block_ts": 1704240000000,
         "confirmed": True, "contractRet": "SUCCESS", "event_type": "Transfer",
         "tokenInfo": {"tokenDecimal": 6}},
    ]


def get_sample_trx_transfers():
    return [
        {"transactionHash": "sample-trx-in", "transferFromAddress": "TOther", "transferToAddress": SAMPLE_ADDRESS,
         "amount": 155_000_000, "timestamp": 1704153600000, "confirmed": True, "contractRet": "SUCCESS"},
    ]


def main():
    parser = argparse.ArgumentParser(description="Analyze TRON USDT (TRC-20) addresses from a text file, one per line.")
    parser.add_argument("file", nargs="?", type=Path,
                        default=Path(__file__).resolve().with_name("tron_wallets.txt"),
                        help="Address file (default: tron_wallets.txt next to this script)")
    parser.add_argument("--test", action="store_true", help="Use sample data without API requests")
    args = parser.parse_args()

    if args.test:
        print("Running in TEST mode with sample data and a mock USDT price...")
        analysis = analyze_activity(get_sample_transfers(), get_sample_trx_transfers(), SAMPLE_ADDRESS)
        print_analysis(SAMPLE_ADDRESS, analysis, Decimal("0.9998"), Decimal("0.12"))
        return

    try:
        addresses = load_addresses(args.file)
    except (OSError, UnicodeError) as error:
        print(f"Could not read address file {args.file}: {error}", file=sys.stderr)
        sys.exit(1)
    if not addresses:
        print(f"No TRON addresses found. Add one address per line to {args.file}.")
        return

    with requests.Session() as session:
        print(f"Loaded {len(addresses)} addresses from {args.file}")
        print("Fetching current USDT and TRX prices...")
        usdt_price = get_usdt_price(session)
        trx_price = get_trx_price(session)
        for index, address in enumerate(addresses, start=1):
            print(f"\n[{index}/{len(addresses)}] Analyzing address: {address}")
            try:
                usdt_transfers = get_transfers(session, address)
                trx_transfers = get_trx_transfers(session, address)
                analysis = analyze_activity(usdt_transfers, trx_transfers, address)
                print_analysis(address, analysis, usdt_price, trx_price)
            except TransactionHistoryTooLarge as error:
                try:
                    balances = get_account_balances(session, address)
                except (requests.RequestException, KeyError, ValueError, TypeError) as balance_error:
                    print(f"Could not fetch balances for {address}: {balance_error}")
                    balances = None
                print_manual_verification_summary(address, error, balances, usdt_price, trx_price)
            except (requests.RequestException, KeyError, ValueError, TypeError) as error:
                print(f"Could not analyze address {address}: {error}")


if __name__ == "__main__":
    main()
