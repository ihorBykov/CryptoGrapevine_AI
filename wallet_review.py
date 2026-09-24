import argparse
import json
import os
from datetime import datetime, timezone
import requests
from typing import Optional
from pydantic import BaseModel
from dotenv import load_dotenv

from eth_wallet_analyzer import (
    get_transactions as eth_get_transactions,
    get_eth_balance,
    analyze_transactions as eth_analyze,
    get_eth_price,
    format_usd as eth_format_usd,
    TransactionHistoryTooLarge,
    get_sent_transaction_boundaries,
)
from btc_wallet_analyzer import (
    get_transactions as btc_get_transactions,
    analyze_transactions as btc_analyze,
    verify_totals,
    get_btc_price,
    format_usd as btc_format_usd,
    get_balance_summary as btc_get_balance_summary,
    TransactionHistoryTooLarge as BtcTransactionHistoryTooLarge,
)
from tron_usdt_analyzer import (
    get_transfers,
    get_trx_transfers,
    analyze_activity,
    get_usdt_price,
    get_trx_price,
    format_usd as tron_format_usd,
    get_account_balances,
    format_manual_verification_summary as tron_format_manual_summary,
    TransactionHistoryTooLarge as TronTransactionHistoryTooLarge,
)

load_dotenv()


class WalletReview(BaseModel):
    address: str
    blockchain: str
    tx_count: Optional[int] = None
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    details: Optional[str] = None
    is_exchange_like: Optional[bool] = None
    requires_manual_verification: Optional[bool] = None
    error: Optional[str] = None


def _fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else "N/A"


def _fmt_span(first, last) -> str:
    if not first or not last:
        return "N/A"
    span = last - first
    hours, remainder = divmod(span.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{span.days} days, {hours:02}:{minutes:02}:{seconds:02}"


def review_eth_wallet(address: str) -> WalletReview:
    api_key = os.environ.get("ETHERSCAN_API_KEY", "").strip()
    if not api_key:
        return WalletReview(address=address, blockchain="Ethereum", error="ETHERSCAN_API_KEY is not configured")

    eth_balance = get_eth_balance(api_key, address)
    eth_price = get_eth_price()
    try:
        normal, internal, tokens = eth_get_transactions(api_key, address)
    except TransactionHistoryTooLarge as error:
        try:
            first_sent, last_sent = get_sent_transaction_boundaries(api_key, address)
        except (KeyError, TypeError, ValueError):
            first_sent = last_sent = None

        first_dt = datetime.fromtimestamp(int(first_sent["timeStamp"]), timezone.utc) if first_sent else None
        last_dt = datetime.fromtimestamp(int(last_sent["timeStamp"]), timezone.utc) if last_sent else None
        sent_activity = "No sent normal transactions found."
        if first_sent and last_sent:
            sent_activity = (
                f"First sent normal transaction (UTC): {_fmt_dt(first_dt)}\n"
                f"Last sent normal transaction (UTC): {_fmt_dt(last_dt)}\n"
                f"Sent normal transaction activity span: {_fmt_span(first_dt, last_dt)}"
            )
        details = (
            f"Current ETH balance: {eth_balance:.6f} ETH ({eth_format_usd(eth_balance, eth_price)})\n"
            f"⚠️ {error}\n"
            f"{sent_activity}"
        )
        return WalletReview(
            address=address,
            blockchain="Ethereum",
            first_seen=_fmt_dt(first_dt) if first_dt else None,
            last_seen=_fmt_dt(last_dt) if last_dt else None,
            details=details,
            requires_manual_verification=True,
        )

    analysis = eth_analyze(normal, address, internal, tokens)

    details = (
        f"Current ETH balance: {eth_balance:.6f} ETH ({eth_format_usd(eth_balance, eth_price)})\n"
        f"Unique transactions (normal/internal/ERC-20): {analysis['total_transactions']}\n"
        f"First transaction (UTC): {_fmt_dt(analysis['first_transaction'])}\n"
        f"Last transaction (UTC): {_fmt_dt(analysis['last_transaction'])}\n"
        f"Activity span: {_fmt_span(analysis['first_transaction'], analysis['last_transaction'])}\n"
        f"Normal ETH transactions: {analysis['normal_eth_transactions']}\n"
        f"Internal ETH operations: {analysis['internal_eth_operations']}\n"
        f"ERC-20 transfers: {analysis['erc20_transfers']}\n"
        f"Failed ETH operations: {analysis['failed_eth_operations']}\n"
        + (f"Current ETH price: ${eth_price:.2f}\n" if eth_price else "")
        + f"Inbound ETH: {analysis['inbound_eth']:.6f} ETH ({eth_format_usd(analysis['inbound_eth'], eth_price)})\n"
        f"Outbound ETH: {analysis['outbound_eth']:.6f} ETH ({eth_format_usd(analysis['outbound_eth'], eth_price)})\n"
        f"Net Flow: {analysis['net_flow']:.6f} ETH ({eth_format_usd(analysis['net_flow'], eth_price)})"
    )

    return WalletReview(
        address=address,
        blockchain="Ethereum",
        tx_count=analysis["total_transactions"],
        first_seen=_fmt_dt(analysis["first_transaction"]),
        last_seen=_fmt_dt(analysis["last_transaction"]),
        details=details,
    )


def review_btc_wallet(address: str) -> WalletReview:
    with requests.Session() as session:
        try:
            transactions, summary = btc_get_transactions(session, address)
        except BtcTransactionHistoryTooLarge as error:
            price = get_btc_price(session)
            balances = btc_get_balance_summary(error.summary)
            details = (
                f"Confirmed transactions: {balances['confirmed_tx_count']:,}\n"
                f"Current confirmed BTC balance: {balances['confirmed_btc']:.8f} BTC "
                f"({btc_format_usd(balances['confirmed_btc'], price)})\n"
                f"Unconfirmed transactions: {balances['mempool_tx_count']}\n"
                f"Mempool balance change: {balances['pending_mempool_btc']:+.8f} BTC\n"
                f"⚠️ {error}"
            )
            return WalletReview(
                address=address,
                blockchain="Bitcoin",
                tx_count=balances["confirmed_tx_count"],
                details=details,
                requires_manual_verification=True,
            )

        analysis = btc_analyze(transactions, address)
        verify_totals(analysis, summary)
        price = get_btc_price(session)

    unconfirmed = summary.get("mempool_stats", {}).get("tx_count", 0)

    details = (
        f"Unique confirmed transactions: {analysis['total_transactions']}\n"
        f"First transaction (UTC, block time): {_fmt_dt(analysis['first_transaction'])}\n"
        f"Last transaction (UTC, block time): {_fmt_dt(analysis['last_transaction'])}\n"
        f"Activity span: {_fmt_span(analysis['first_transaction'], analysis['last_transaction'])}\n"
        f"Unconfirmed transactions: {unconfirmed}\n"
        + (f"Current BTC price: ${price:,.2f}\n" if price else "")
        + f"Inbound BTC: {analysis['inbound_btc']:.8f} BTC ({btc_format_usd(analysis['inbound_btc'], price)})\n"
        f"Outbound BTC: {analysis['outbound_btc']:.8f} BTC ({btc_format_usd(analysis['outbound_btc'], price)})\n"
        f"Net Flow: {analysis['net_flow']:.8f} BTC ({btc_format_usd(analysis['net_flow'], price)})"
    )

    return WalletReview(
        address=address,
        blockchain="Bitcoin",
        tx_count=analysis["total_transactions"],
        first_seen=_fmt_dt(analysis["first_transaction"]),
        last_seen=_fmt_dt(analysis["last_transaction"]),
        details=details,
    )


def review_tron_wallet(address: str) -> WalletReview:
    with requests.Session() as session:
        try:
            usdt_transfers = get_transfers(session, address)
            trx_transfers = get_trx_transfers(session, address)
        except TronTransactionHistoryTooLarge as error:
            usdt_price = get_usdt_price(session)
            trx_price = get_trx_price(session)
            try:
                balances = get_account_balances(session, address)
            except (requests.RequestException, KeyError, ValueError, TypeError):
                balances = None
            return WalletReview(
                address=address,
                blockchain="TRON",
                tx_count=balances["account_transaction_count"] if balances else None,
                details=tron_format_manual_summary(error, balances, usdt_price, trx_price),
                requires_manual_verification=True,
            )

        analysis = analyze_activity(usdt_transfers, trx_transfers, address)
        usdt_price = get_usdt_price(session)
        trx_price = get_trx_price(session)
        try:
            account_balances = get_account_balances(session, address)
        except (requests.RequestException, KeyError, ValueError, TypeError):
            account_balances = None

    account_tx_count = (
        account_balances["account_transaction_count"] if account_balances else None
    )
    details = (
        (f"TronScan account transactions: {account_tx_count:,}\n"
         if account_tx_count is not None else "")
        + f"First operation (UTC): {_fmt_dt(analysis['first_transaction'])}\n"
        f"Last operation (UTC): {_fmt_dt(analysis['last_transaction'])}\n"
        f"Activity span: {_fmt_span(analysis['first_transaction'], analysis['last_transaction'])}\n"
        f"--- USDT (TRC-20) ---\n"
        + (f"Current USDT price: ${usdt_price:,.4f}\n" if usdt_price else "")
        + f"USDT operations: {analysis['total_operations']}\n"
        f"Inbound USDT: {analysis['inbound_usdt']:,.6f} ({tron_format_usd(analysis['inbound_usdt'], usdt_price)}), operations: {analysis['inbound_operations']}\n"
        f"Outbound USDT: {analysis['outbound_usdt']:,.6f} ({tron_format_usd(analysis['outbound_usdt'], usdt_price)}), operations: {analysis['outbound_operations']}\n"
        f"Net Flow USDT: {analysis['net_flow_usdt']:,.6f} ({tron_format_usd(analysis['net_flow_usdt'], usdt_price)})\n"
        f"--- TRX ---\n"
        + (f"Current TRX price: ${trx_price:,.4f}\n" if trx_price else "")
        + f"TRX operations: {analysis['total_trx_operations']}\n"
        f"Inbound TRX: {analysis['inbound_trx']:,.6f} ({tron_format_usd(analysis['inbound_trx'], trx_price)}), operations: {analysis['inbound_trx_operations']}\n"
        f"Outbound TRX: {analysis['outbound_trx']:,.6f} ({tron_format_usd(analysis['outbound_trx'], trx_price)}), operations: {analysis['outbound_trx_operations']}\n"
        f"Net Flow TRX: {analysis['net_trx_flow']:,.6f} ({tron_format_usd(analysis['net_trx_flow'], trx_price)})"
    )

    return WalletReview(
        address=address,
        blockchain="TRON",
        tx_count=account_tx_count,
        first_seen=_fmt_dt(analysis["first_transaction"]),
        last_seen=_fmt_dt(analysis["last_transaction"]),
        details=details,
    )


_DISPATCH = {
    "ethereum": review_eth_wallet,
    "bitcoin": review_btc_wallet,
    "tron": review_tron_wallet,
}


def review_wallet(address: str, blockchain: str) -> WalletReview:
    handler = _DISPATCH.get((blockchain or "").strip().lower())
    if handler is None:
        return WalletReview(address=address, blockchain=blockchain,
                             error=f"No analyzer is available for {blockchain}.")
    try:
        return handler(address)
    except Exception as e:
        return WalletReview(address=address, blockchain=blockchain, error=str(e))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Review one wallet independently of the X/Gemini/Telegram pipeline."
    )
    parser.add_argument("--address", required=True, help="Full wallet address")
    parser.add_argument("--blockchain", required=True,
                        choices=sorted(_DISPATCH), help="Blockchain network")
    args = parser.parse_args()

    review = review_wallet(args.address, args.blockchain)
    print(json.dumps(review.model_dump(exclude_none=True), ensure_ascii=False, indent=2))
