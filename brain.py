import argparse
import os
import re
import json
import io
import sys
import time
import warnings
from pathlib import Path
from textwrap import dedent
from typing import List, Optional, Literal
from pydantic import BaseModel, Field

warnings.filterwarnings("ignore", category=FutureWarning, module=r"google\.")
from google import genai
from google.genai import types
from dotenv import load_dotenv

import logging

logging.captureWarnings(True)
logging.getLogger("py.warnings").setLevel(logging.ERROR)
logging.getLogger("google").setLevel(logging.ERROR)
logging.getLogger("google.genai").setLevel(logging.ERROR)

load_dotenv()

_client = None


def get_client():
    """Create the Gemini client only when an AI request is actually made."""
    global _client
    if _client is None:
        _client = genai.Client()
    return _client

REGEX_PATTERNS = {
    "EVM": r"\b0x[a-fA-F0-9]{40}\b",
    "BTC_Legacy_Script": r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b",
    "BTC_SegWit": r"\bbc1[a-z0-9]{35,59}\b",
    "Solana": r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b",
    "TRON": r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b"
}


def is_complete_address(address: str) -> bool:
    """Accept only a complete address of one of the supported networks."""
    return any(re.fullmatch(pattern, address) for pattern in REGEX_PATTERNS.values())


def address_appears_in_text(address: str, text: str) -> bool:
    """EVM addresses are case-insensitive; Base58 and Bech32 addresses are not."""
    if re.fullmatch(REGEX_PATTERNS["EVM"], address):
        return address.lower() in text.lower()
    return address in text


def _role_is_explicit(address: str, role: str, source_text: str) -> bool:
    """Require a direct role marker in the same sentence as the address."""
    role_markers = {
        "attacker": r"\b(attacker|hacker|thief|drainer)\b",
        "victim": r"\bvictim\b",
        "theft address": r"\b(theft address|stolen funds address)\b",
        "recipient": r"\b(recipient|sent to|received by)\b",
        "deposit address": r"\b(deposit address|deposit wallet)\b",
        "drainer": r"\b(drainer|draining wallet)\b",
        "mixer": r"\b(mixer|mixing service)\b",
    }
    marker = role_markers.get(role.strip().lower())
    if not marker:
        return role.strip().lower() in {"unknown", "unconfirmed", "not established"}

    address_pattern = re.escape(address)
    flags = re.IGNORECASE if re.fullmatch(REGEX_PATTERNS["EVM"], address) else 0
    sentences = re.split(r"(?<=[.!?])\s+|\n+", source_text)
    return any(
        re.search(address_pattern, sentence, flags) and re.search(marker, sentence, re.IGNORECASE)
        for sentence in sentences
    )


def _role_basis(source_text: str) -> str:
    lowered = source_text.lower()
    if any(marker in lowered for marker in ("unauthorized withdrawal", "unauthorized transfer", "stolen", "theft")):
        return "Mentioned in connection with a suspected unauthorized withdrawal; control is not established."
    return "The supplied text does not establish who controls this address."


def _neutralize_unsupported_attribution(report: "IncidentReport") -> None:
    """Remove model wording that attributes an address without direct evidence."""
    replacements = (
        (r"\b(?:an?|the)\s+attacker(?:'s)?\s+address\b", "the address mentioned in connection with the reported event"),
        (r"\baddress\s+controlled\s+by\s+(?:an?|the)\s+attacker\b", "address whose controller is not established"),
    )

    def neutralize(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        for pattern, replacement in replacements:
            value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
        return value

    report.decision_reason = neutralize(report.decision_reason) or report.decision_reason
    report.summary_en = neutralize(report.summary_en)
    report.evidence = [neutralize(item) or item for item in report.evidence]


def remove_invalid_extracted_addresses(report: "IncidentReport", source_text: str) -> "IncidentReport":
    """Defend against incomplete or invented addresses returned by a model."""
    valid_addresses = []
    rejected_addresses = []
    seen = set()
    role_was_downgraded = False

    for extracted in report.extracted_addresses:
        address = extracted.address.strip()
        key = address.lower() if re.fullmatch(REGEX_PATTERNS["EVM"], address) else address
        if not is_complete_address(address) or not address_appears_in_text(address, source_text):
            rejected_addresses.append(extracted.address)
            continue
        if key not in seen:
            seen.add(key)
            role = extracted.role.strip() or "Unknown"
            if not _role_is_explicit(address, role, source_text):
                role = "Unknown"
                role_was_downgraded = True
            valid_addresses.append(extracted.model_copy(update={
                "address": address,
                "role": role,
                "role_basis": _role_basis(source_text) if role == "Unknown" else None,
            }))

    if rejected_addresses:
        print("[WARN] Removed incomplete addresses or addresses absent from the post: "
              + ", ".join(rejected_addresses))
    report.extracted_addresses = valid_addresses
    if role_was_downgraded:
        _neutralize_unsupported_attribution(report)
    return report

SECURITY_KEYWORDS = [
    "exploit", "hack", "drain", "stolen", "steal", "theft",
    "victim", "attacker", "phishing", "vulnerability",
    "rugpull", "rug pull", "compromised", "ransomware",
    "launder", "scam", "fraud",
]


def low_cost_filter(text: str) -> bool:
    text_lower = text.lower()

    matched_keywords = [
        keyword
        for keyword in SECURITY_KEYWORDS
        if keyword in text_lower
    ]

    matched_artifacts = [
        name
        for name, pattern in REGEX_PATTERNS.items()
        if re.search(pattern, text)
    ]

    print(f"   Keywords: {matched_keywords or 'none'}")
    print(f"   Artifacts: {matched_artifacts or 'none'}")

    return bool(matched_keywords and matched_artifacts)


class ExtractedAddress(BaseModel):
    address: str = Field(
        description="Complete cryptocurrency wallet or contract address extracted from the text."
    )

    blockchain: str = Field(
        description="Blockchain network, for example Bitcoin, Ethereum, Solana or TRON."
    )

    role: str = Field(
        description=(
            "Role only when it is explicitly tied to this exact address in the supplied text. "
            "Use Unknown when the text merely places the address in a transaction or incident. "
            "Examples of explicit roles are: "
            "Attacker, Victim, Theft Address, Drainer, Mixer, Recipient, "
            "Deposit Address, or Unknown."
        )
    )

    role_basis: Optional[str] = Field(
        default=None,
        description=(
            "A short explanation for the address role. For Unknown, state that the address "
            "is mentioned in connection with the reported event but its controller is not established."
        )
    )


class IncidentReport(BaseModel):
    is_incident: bool = Field(
        description=(
            "True if the text contains an actionable lead about a specific "
            "cryptocurrency security incident. False for general news, market "
            "commentary, advertising, education, or ordinary transactions."
        )
    )

    decision_reason: str = Field(
        description=(
            "A concise explanation of why the post was classified "
            "as an incident or rejected. Use only the supplied text."
        )
    )

    classification_confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "Confidence that the post should be classified as a potential incident. "
            "This measures classification confidence, not whether the reported event "
            "has been independently verified."
        )
    )

    incident_type: Optional[
        Literal[
            "Exploit",
            "Phishing",
            "Wallet Drainer",
            "Social Engineering",
            "Ransomware",
            "Rug Pull",
            "Exit Scam",
            "Investment Scam",
            "Impersonation Scam",
            "Approval Scam",
            "Account Takeover",
            "Private Key Compromise",
            "SIM Swap",
            "Insider Theft",
            "Money Laundering",
            "Unknown",
        ]
    ] = Field(
        default=None,
        description=(
            "The specific incident category supported by the supplied text. "
            "Use Unknown when the original attack or fraud mechanism is not explicit."
        )
    )

    victim: Optional[str] = Field(
        default=None,
        description=(
            "Name of the affected person, protocol, project, wallet provider "
            "or exchange. Null when the victim is not identified."
        )
    )

    estimated_loss_usd: Optional[float] = Field(
        default=None,
        description=(
            "Reported loss converted to a numeric USD value. "
            "Null when no USD estimate is present."
        )
    )

    extracted_addresses: List[ExtractedAddress] = Field(
        default_factory=list,
        description=(
            "All complete cryptocurrency addresses explicitly present in the text. "
            "Do not include shortened strings such as 0x4487."
        )
    )

    evidence: List[str] = Field(
        default_factory=list,
        description=(
            "Short factual indicators explicitly present in the supplied text. "
            "Do not add external information or claim on-chain verification."
        )
    )

    event_verification: Literal[
        "unverified_claim",
        "partially_supported",
        "text_supported",
    ] = Field(
        description=(
            "The evidence status of the reported event, separate from classification "
            "confidence. Use unverified_claim when the post only makes an allegation "
            "that has not been independently checked; partially_supported when it "
            "includes some concrete artifacts but independent verification is incomplete; "
            "text_supported when the extracted conclusions are directly supported by "
            "the supplied text. This field never means on-chain verification."
        )
    )

    summary_en: Optional[str] = Field(
        default=None,
        description=(
            "A concise one or two sentence English summary. "
            "Clearly describe unverified allegations as claims."
        )
    )

GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite",
    "Antigravity",
]

SYSTEM_PROMPT = dedent("""
    You are a crypto threat intelligence triage analyst.

    Your task is to determine whether a social media post contains an
    actionable lead about a potential cryptocurrency security incident.

    CLASSIFY AS AN INCIDENT when the post reports or investigates at least one of:
    - theft or unauthorized movement of cryptocurrency;
    - protocol exploit or smart-contract vulnerability;
    - phishing, wallet drainer, seed phrase compromise, or social engineering theft;
    - ransomware or extortion involving cryptocurrency;
    - rug pull, exit scam, or fraudulent fund diversion;
    - laundering or movement of funds connected to a known incident;
    - compromise of an exchange, protocol, bridge, wallet, or private key.

    INCIDENT TYPE RULES:
    - Choose a specific incident_type only when the supplied text explicitly names
      the mechanism or makes it unambiguous. Otherwise return Unknown.
    - Exploit: a protocol or smart-contract vulnerability was exploited.
    - Phishing: credentials, seed phrase, wallet connection, or data were obtained
      through a deceptive message or website.
    - Wallet Drainer: a malicious wallet-draining application, script, or approval
      transferred assets.
    - Social Engineering: a victim was manipulated into sending assets or revealing
      access without a more specific phishing or impersonation mechanism.
    - Ransomware: assets were demanded after system or data extortion.
    - Rug Pull: project insiders removed liquidity or abandoned a token/project.
    - Exit Scam: operators disappeared after taking user or investor funds.
    - Investment Scam: a fraudulent investment, yield, trading, or Ponzi scheme.
    - Impersonation Scam: an attacker pretended to be a person, project, exchange,
      or support representative.
    - Approval Scam: malicious token approval or permit enabled a transfer.
    - Account Takeover: an exchange, wallet, or social account was taken over.
    - Private Key Compromise: a private key was exposed, stolen, or used without
      authorization.
    - SIM Swap: a phone number was hijacked to obtain account access.
    - Insider Theft: an employee, founder, administrator, or other insider stole funds.
    - Money Laundering: the main subject is explicitly the concealment or movement
      of funds known to be illicit.
    - Do not infer a type from words such as "scam", "theft", "stolen", or from
      an address's later transfers alone. Those words may establish a potential
      incident, but its incident_type must be Unknown unless the original mechanism
      is stated. A post that only traces funds after an earlier incident must use Unknown.

    DO NOT CLASSIFY AS AN INCIDENT when the post is only:
    - general market commentary;
    - token price discussion;
    - promotion or advertising;
    - an ordinary transaction without allegations of abuse;
    - educational content without a specific event;
    - a repeated headline without any investigative detail;
    - speculation that contains no identifiable event or evidence.

    EVIDENCE RULES:
    - Use only information explicitly present in the supplied text.
    - Do not claim that an address was verified on-chain.
    - Do not invent victims, actors, amounts, dates, or attribution.
    - Treat allegations as unconfirmed unless the text provides verification.
    - Extract every complete cryptocurrency address found in the text.
    - Assign an address role only when the text explicitly ties that role to the exact address.
      An address following words such as "at", "to", or "from" is not automatically an attacker,
      victim, recipient, or theft address. If the relationship is ambiguous, use role Unknown and
      explain that the address is mentioned in connection with the suspected event.
    - Do not treat shortened strings such as "0x4487" as complete addresses.
    - If the available information is insufficient, prefer is_incident=false.
    - Be precise and conservative, especially with address roles and attribution.

    IMPORTANT DISTINCTION:
    - classification_confidence describes how certain you are about the classification of the post.
    - event_verification describes how well the reported event is supported.
      A high confidence classification can correctly have unverified_claim when
      the post clearly reports an incident but no independent verification is available.
    """).strip()


def build_user_content(text: str) -> str:
    return f"Analyze the following data:\n\n{text}"

TRANSIENT_MARKERS = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED")


def ai_incident_parser_gemini(
    text: str,
    max_retries: int = 1,
    retry_delay: float = 2.0,
    models: Optional[List[str]] = None,
) -> Optional[IncidentReport]:
    """Parse an eligible post with fallback models and transient-error retries."""
    last_error: Optional[Exception] = None

    for model_name in models or GEMINI_MODELS:
        for attempt in range(max_retries + 1):
            suppress_stderr = io.StringIO()
            old_stderr = sys.stderr
            try:
                sys.stderr = suppress_stderr

                response = get_client().models.generate_content(
                    model=model_name,
                    contents=build_user_content(text),
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        response_schema=IncidentReport,
                        temperature=0.1
                    ),
                )
                response_text = response.text
                sys.stderr = old_stderr
                parsed_json = json.loads(response_text)
                print(f"[INFO] Response received from {model_name}")
                return remove_invalid_extracted_addresses(IncidentReport(**parsed_json), text)

            except Exception as e:
                sys.stderr = old_stderr
                last_error = e
                error_text = str(e)

                is_daily_quota = (
                        "PerDay" in error_text
                        or "requests per day" in error_text.lower()
                )

                is_transient = any(
                    marker in error_text
                    for marker in TRANSIENT_MARKERS
                )

                if is_daily_quota:
                    break

                if is_transient and attempt < max_retries:
                    time.sleep(retry_delay * (attempt + 1))
                    continue

                break

        print(f"[WARN] Model {model_name} failed: {last_error}")

    print(f"[ERROR] All Gemini models are unavailable. Last error: {last_error}")
    return None


FILTERED_OUT = object()

def process_message(raw_text: str, models: Optional[List[str]] = None) -> Optional[IncidentReport]:
    """Filter a post locally, then return its Gemini incident assessment."""
    print("\n" + "=" * 50)
    print(f"📥 NEW POST:\n{raw_text.strip()}")

    passed_filter = low_cost_filter(raw_text)
    print(f"\n🔍 Stage 1 (regex + keywords): {'✅ PASSED' if passed_filter else '❌ REJECTED'}")

    if not passed_filter:
        print("💰 Gemini was not called.")
        return FILTERED_OUT

    print("🧠 Stage 2: Sending data to Gemini Flash...")
    report = ai_incident_parser_gemini(raw_text, models=models)

    if report is None:
        print("⚠️ Gemini is temporarily unavailable. Analysis was not completed.")
        return None
    elif report.is_incident:
        print("🚨 INCIDENT DETECTED!")
        print(json.dumps(report.model_dump(), indent=2, ensure_ascii=False))
    else:
        print("ℹ️ Gemini classified this as a non-incident (is_incident: False).")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run and debug Gemini incident analysis without main.py.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="Post text to analyze")
    source.add_argument("--file", type=Path, help="UTF-8 text file to analyze")
    source.add_argument("--posts-file", type=Path,
                        help="JSON archive from scraper_x.py; select a post with --post-index")
    parser.add_argument("--post-index", type=int, default=0,
                        help="Post number in --posts-file (default: 0)")
    parser.add_argument("--model", action="append",
                        help="Gemini model to use; repeat for an explicit fallback sequence")
    parser.add_argument("--skip-filter", action="store_true",
                        help="Call Gemini even if the local keyword/address filter rejects the text")
    parser.add_argument("--show-prompt", action="store_true",
                        help="Print the exact system and user prompts without calling Gemini")
    args = parser.parse_args()

    if args.text is not None:
        raw_text = args.text
    elif args.file is not None:
        raw_text = args.file.read_text(encoding="utf-8")
    else:
        from scraper_x import load_posts

        posts = load_posts(args.posts_file)
        try:
            post = posts[args.post_index]
        except IndexError:
            parser.error(f"Archive contains only {len(posts)} posts; index {args.post_index} is unavailable.")
        raw_text = f"[{post['author']}]: {post['text']}"
        print(f"📂 Loaded post {args.post_index} from {args.posts_file}")
    if args.show_prompt:
        print("=== SYSTEM PROMPT ===")
        print(SYSTEM_PROMPT)
        print("\n=== USER CONTENT ===")
        print(build_user_content(raw_text))
        print("\n=== MODELS ===")
        print(", ".join(args.model or GEMINI_MODELS))
    elif args.skip_filter:
        report = ai_incident_parser_gemini(raw_text, models=args.model)
        print(json.dumps(report.model_dump() if report else None, ensure_ascii=False, indent=2))
    else:
        report = process_message(raw_text, models=args.model)
        if report is not None and report is not FILTERED_OUT:
            print("\n=== STRUCTURED RESULT ===")
            print(json.dumps(report.model_dump(), ensure_ascii=False, indent=2))
