import os
from pathlib import Path
import requests
from dotenv import load_dotenv
from incident_report import VERIFICATION_LABELS

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram_message(text: str) -> None:
    """Send a Telegram message when the bot configuration is available."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram is not configured; alert was not sent.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ Telegram message delivery failed: {e}")


def send_telegram_document(file_path: Path) -> None:
    """Attach a local Markdown report to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram is not configured; report attachment was not sent.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        with file_path.open("rb") as report_file:
            response = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID},
                files={"document": (file_path.name, report_file, "text/markdown")},
                timeout=30,
            )
        response.raise_for_status()
    except Exception as error:
        print(f"❌ Telegram report delivery failed: {error}")


def format_incident_message(
    report,
    post: dict,
    wallet_reviews: list = None,
    include_wallet_details: bool = True,
) -> str:
    addresses = "\n".join(
        f"• <code>{a.address}</code> ({a.blockchain}, Role: {a.role})"
        for a in report.extracted_addresses
    ) or "—"

    loss = f"${report.estimated_loss_usd:,.0f}" if report.estimated_loss_usd else "unknown"

    wallet_info = ""
    if wallet_reviews:
        if include_wallet_details:
            blocks = []
            for w in wallet_reviews:
                if w.error:
                    icon = "🏦" if w.is_exchange_like else "🔸"
                    blocks.append(f"{icon} <code>{w.address}</code> ({w.blockchain}): {w.error}")
                else:
                    blocks.append(f"🔸 <code>{w.address}</code> ({w.blockchain}):\n{w.details}")
            wallet_info = "\n\n<b>Wallet review:</b>\n" + "\n\n".join(blocks)
        else:
            wallet_info = "\n\n<b>Wallet review and Tavily sources:</b> see the attached report."

    return (
        f"🚨 <b>Incident detected</b>\n\n"
        f"<b>Type:</b> {report.incident_type or 'Unknown'}\n"
        f"<b>Classification confidence:</b> {report.classification_confidence}\n"
        f"<b>Event verification:</b> {VERIFICATION_LABELS.get(report.event_verification, report.event_verification)}\n"
        f"<b>Victim:</b> {report.victim or 'Unknown'}\n"
        f"<b>Reported loss:</b> {loss}\n\n"
        f"<b>Addresses:</b>\n{addresses}"
        f"{wallet_info}\n\n"
        f"<b>Summary:</b> {report.summary_en}\n\n"
        f"👤 Source: @{post.get('author')}\n"
        f"📅 Post date: {post.get('created_at')}\n"
        f"🔗 {post.get('url')}"
    )
