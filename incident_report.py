"""Create concise, shareable incident reports from pipeline enrichment results."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable


REPORTS_DIR = Path("data/reports")
MAX_SOURCE_SNIPPET_CHARS = 500

VERIFICATION_LABELS = {
    "unverified_claim": "Source claim; independently unverified",
    "partially_supported": "Partially supported by concrete artifacts; independent verification incomplete",
    "text_supported": "Supported by explicit source text; independently unverified",
}


def _section(title: str) -> str:
    return f"\n## {title}\n"


def _details_as_markdown_list(details: str) -> Iterable[str]:
    """Preserve analyzer line breaks in Markdown renderers such as Telegram."""
    for line in details.splitlines():
        line = line.strip()
        if line:
            yield f"- {line}"


def _source_lines(search: dict) -> Iterable[str]:
    if search.get("error"):
        yield f"Search failed: {search['error']}"
        return
    if search.get("skipped"):
        yield f"Search skipped: {search['skipped']}"
        return

    payload = search.get("result", {})
    sources = payload.get("results", [])
    if not sources:
        yield "Tavily found no relevant sources."
        return

    cache_status = "used" if search.get("from_cache") else "new request"
    if search.get("fallback_to_address_only"):
        cache_status += "; contextual search was empty, so address-only search was used"
    yield "Cache: " + cache_status
    for index, source in enumerate(sources, start=1):
        title = source.get("title") or "Untitled"
        url = source.get("url") or ""
        snippet = " ".join(source.get("content", "").split())[:MAX_SOURCE_SNIPPET_CHARS]
        score = source.get("score")
        yield f"\n#### {index}. {title}"
        yield f"- **URL:** {url}"
        if isinstance(score, (float, int)):
            yield f"- **Tavily relevance:** {score:.2f}"
        if snippet:
            yield f"- **Excerpt:** {snippet}"


def save_incident_report(
    post: dict,
    report,
    wallet_reviews: list,
    tavily_by_address: Dict[str, dict],
    reports_dir: Path = REPORTS_DIR,
) -> Path:
    """Save one Markdown attachment with model, on-chain, and public-web context."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    post_id = str(post.get("id") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    report_file = reports_dir / f"incident_{post_id}.md"

    lines = [
        "# CryptoGrapevine incident report",
        f"Created: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S UTC}",
        _section("Source post"),
        f"- **Source:** @{post.get('author', 'unknown')}",
        f"- **Date:** {post.get('created_at', 'unknown')}",
        f"- **URL:** {post.get('url', '')}",
        "",
        "> " + post.get("text", "").replace("\n", "\n> "),
        _section("Gemini assessment"),
        f"- **Incident:** {report.is_incident}",
        f"- **Type:** {report.incident_type or 'Unknown'}",
        f"- **Classification confidence:** {report.classification_confidence}",
        f"- **Event verification:** {VERIFICATION_LABELS.get(report.event_verification, report.event_verification)}",
        f"- **Decision reason:** {report.decision_reason}",
        f"- **Summary:** {report.summary_en or '—'}",
    ]
    if report.estimated_loss_usd is not None:
        lines.append(f"- **Reported loss:** ${report.estimated_loss_usd:,.2f}")

    lines.extend((_section("Wallet review"),))
    for wallet in wallet_reviews:
        lines.extend((
            f"### {wallet.address} ({wallet.blockchain})",
        ))
        if wallet.details:
            lines.extend(_details_as_markdown_list(wallet.details))
        else:
            lines.append(f"- {wallet.error or 'No data.'}")

    lines.extend((_section("Tavily public sources"),))
    for extracted in report.extracted_addresses:
        lines.append(f"### {extracted.address} ({extracted.role})")
        if extracted.role_basis:
            lines.append(f"- **Role basis:** {extracted.role_basis}")
        source_lines = list(_source_lines(tavily_by_address.get(extracted.address, {
            "skipped": "no result",
        })))
        lines.extend(f"- {line}" if index == 0 else line for index, line in enumerate(source_lines))

    report_file.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return report_file
