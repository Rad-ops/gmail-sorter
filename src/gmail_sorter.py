#!/usr/bin/env python3
"""Stage-based Gmail sorter for mail before December 30, 2025.

Default mode is a dry-run classification pass. The HTML dashboard is the main
review surface; use it before running any --apply stage.
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import re
import socket
import sqlite3
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any


READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
DEFAULT_QUERY = "before:2025/12/30 -in:trash"
ROOT_LABEL = "Sorter"
PROJECT_DIR = Path(__file__).resolve().parents[1]
APP_VERSION = "0.3.0"
VERSION_CODE = "20260705"

AD_SUBJECT_KEYWORDS = [
    "sale",
    "deal",
    "deals",
    "discount",
    "promo",
    "promotion",
    "coupon",
    "offer",
    "limited time",
    "save ",
    "% off",
    "free shipping",
    "clearance",
    "flash sale",
    "black friday",
    "cyber monday",
    "newsletter",
    "new arrivals",
    "just dropped",
    "shop now",
    "last chance",
    "ends tonight",
    "exclusive",
]

AD_BODY_KEYWORDS = [
    "unsubscribe",
    "manage preferences",
    "email preferences",
    "view in browser",
    "view this email in your browser",
    "you are receiving this email because",
    "marketing email",
    "promotional email",
    "privacy policy",
]

AD_SENDER_KEYWORDS = ["newsletter", "marketing", "promo", "promotions", "offers", "deals"]

STRONG_PROMO_SUBJECT_PATTERNS = [
    r"\b\d{1,2}%\s*off\b",
    r"\b\d{1,2}\s*percent\s*off\b",
    r"\b(?:last chance|final hours|ends tonight|today only)\b",
    r"\b(?:flash sale|clearance|warehouse sale|summer sale|winter sale)\b",
    r"\b(?:black friday|cyber monday|boxing day)\b",
    r"\b(?:free shipping|free delivery)\b",
    r"\b(?:shop now|new arrivals|just dropped)\b",
]

PROMO_SENDER_LOCALPARTS = {
    "deals",
    "email",
    "hello",
    "info",
    "marketing",
    "newsletter",
    "newsletters",
    "offers",
    "promo",
    "promotions",
    "sales",
}

TRANSACTIONAL_KEYWORDS = [
    "2fa",
    "account alert",
    "appointment",
    "bank",
    "bill",
    "booking",
    "code",
    "confirm your email",
    "delivery",
    "document",
    "e-transfer",
    "invoice",
    "login",
    "mfa",
    "order",
    "password",
    "payment",
    "payroll",
    "receipt",
    "refund",
    "reset",
    "security",
    "shipment",
    "shipped",
    "statement",
    "tax",
    "ticket",
    "transaction",
    "verification",
    "verify",
]

IMPORTANT_LABELS = {"CATEGORY_PRIMARY", "STARRED", "IMPORTANT"}
PROTECTED_CATEGORIES = {
    "Account Security",
    "Finance",
    "Government Legal",
    "Health",
    "Insurance",
    "Priority Attachments",
    "Priority Immigration",
    "Priority Studies",
    "Receipts Orders",
    "Utilities",
}

IMMIGRATION_KEYWORDS = [
    "immigration",
    "ircc",
    "cic",
    "visa",
    "work permit",
    "study permit",
    "permanent residence",
    "pr card",
    "express entry",
    "biometrics",
    "lawyer",
    "law firm",
    "legal counsel",
    "barrister",
    "solicitor",
    "marolia",
    "pinaz",
    "tiffani",
    "ronen",
    "raquel",
    "jemma",
    "jonalyn",
    "oskoii",
    "oskooii",
    "oskoui",
    "osgoode",
]

STUDIES_KEYWORDS = [
    "university",
    "college",
    "course",
    "class",
    "assignment",
    "tuition",
    "transcript",
    "diploma",
    "degree",
    "registrar",
    "student",
    "student record",
    "academic",
    "study permit",
    "enrolment",
    "enrollment",
    "exam",
    "grade",
    "syllabus",
]

CATEGORY_RULES = [
    ("Priority Immigration", IMMIGRATION_KEYWORDS),
    ("Priority Studies", STUDIES_KEYWORDS),
    ("Finance", ["bank", "credit card", "debit", "statement", "payment", "payroll", "invoice", "tax", "cra", "irs", "etransfer", "e-transfer"]),
    ("Receipts Orders", ["receipt", "order", "purchase", "shipment", "shipped", "delivered", "delivery", "tracking", "refund", "return"]),
    ("Account Security", ["password", "reset", "verification", "verify", "security alert", "new login", "sign-in", "2fa", "mfa", "authentication", "code"]),
    ("Travel", ["flight", "airline", "hotel", "reservation", "booking", "boarding", "itinerary", "rental car", "airbnb", "uber", "lyft"]),
    ("Health", ["appointment", "clinic", "doctor", "dentist", "pharmacy", "prescription", "medical", "health"]),
    ("Government Legal", ["government", "court", "legal", "visa", "immigration", "passport", "license", "notice"]),
    ("Work School", ["meeting", "calendar", "deadline", "project", "assignment", "university", "college", "school", "course", "class"]),
    ("Social", ["facebook", "instagram", "linkedin", "twitter", "x.com", "reddit", "discord", "snapchat", "tiktok"]),
    ("Subscriptions", ["subscription", "renewal", "membership", "plan", "trial", "billing cycle"]),
    ("Shopping", ["cart", "wishlist", "store", "shop", "retailer", "coupon", "discount"]),
    ("Job Search", ["application", "resume", "interview", "recruiter", "job alert", "candidate", "position"]),
    ("Housing", ["rent", "lease", "landlord", "tenant", "mortgage", "property", "apartment", "condo"]),
    ("Utilities", ["utility", "hydro", "internet", "mobile", "phone bill", "electricity", "gas bill"]),
    ("Insurance", ["insurance", "policy", "claim", "premium", "coverage"]),
    ("Crypto Finance Risk", ["crypto", "bitcoin", "ethereum", "wallet", "exchange", "trading"]),
    ("Old Account Evidence", ["welcome to", "confirm your account", "activate your account", "account created", "username", "registered"]),
]


@dataclass
class Config:
    allow_domains: set[str] = field(default_factory=set)
    block_domains: set[str] = field(default_factory=set)
    allow_senders: set[str] = field(default_factory=set)
    block_senders: set[str] = field(default_factory=set)


@dataclass
class Decision:
    message_id: str
    thread_id: str
    date: str
    sender: str
    sender_email: str
    sender_domain: str
    registered_domain: str
    subject: str
    snippet: str
    existing_labels: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    ad_confidence: int = 0
    reasons: list[str] = field(default_factory=list)
    negative_reasons: list[str] = field(default_factory=list)
    planned_actions: list[str] = field(default_factory=list)
    has_attachment: bool = False
    has_real_attachment: bool = False
    attachment_count: int = 0
    inline_attachment_count: int = 0
    message_size_estimate: int = 0
    list_unsubscribe: str = ""
    body_unsubscribe_links: list[str] = field(default_factory=list)
    attachment_names: list[str] = field(default_factory=list)
    attachment_mime_types: list[str] = field(default_factory=list)
    protected: bool = False
    perfect_ad_match: bool = False
    review_priority: str = "normal"
    action_done: str = "no"
    scanned_at: str = ""


class AdaptiveThrottle:
    def __init__(self, base_sleep: float, max_sleep: float = 10.0) -> None:
        self.base_sleep = max(0.0, base_sleep)
        self.current_sleep = self.base_sleep
        self.max_sleep = max_sleep
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            delay = self.current_sleep
        if delay:
            time.sleep(delay)

    def record_success(self) -> None:
        with self.lock:
            if self.current_sleep > self.base_sleep:
                self.current_sleep = max(self.base_sleep, self.current_sleep * 0.85)

    def record_retryable_error(self) -> None:
        with self.lock:
            self.current_sleep = min(self.max_sleep, max(0.25, self.current_sleep * 2 or 0.25))


def load_google_libraries() -> tuple[Any, Any, Any, Any, Any]:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError as error:
        raise SystemExit("Missing Google API packages. Run: python -m pip install -r requirements.txt") from error
    return Request, Credentials, InstalledAppFlow, build, HttpError


def get_credentials(credentials_path: Path, token_path: Path, scopes: list[str], open_browser: bool, google_libs: tuple[Any, Any, Any, Any, Any]) -> Any:
    Request, Credentials, InstalledAppFlow, _, _ = google_libs
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(f"Missing {credentials_path}")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
            creds = flow.run_local_server(port=0, open_browser=open_browser)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_gmail_service(build_func: Any, creds: Any, args: argparse.Namespace) -> Any:
    if args.http_timeout > 0:
        try:
            import google_auth_httplib2
            import httplib2
        except ImportError as error:
            raise SystemExit("Missing Google API packages. Run: python -m pip install -r requirements.txt") from error
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=args.http_timeout))
        return build_func("gmail", "v1", http=http)
    return build_func("gmail", "v1", credentials=creds)


def load_list(path: Path) -> set[str]:
    if not path.exists():
        return set()
    items = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip().lower()
        if line and not line.startswith("#"):
            items.add(line)
    return items


def load_config(allowlist: Path, blocklist: Path) -> Config:
    allow = load_list(allowlist)
    block = load_list(blocklist)
    return Config(
        allow_domains={item for item in allow if "@" not in item},
        block_domains={item for item in block if "@" not in item},
        allow_senders={item for item in allow if "@" in item},
        block_senders={item for item in block if "@" in item},
    )


def ensure_default_config_files(allowlist: Path, blocklist: Path) -> None:
    if not allowlist.exists():
        allowlist.write_text(
            "# One sender email or domain per line. Anything listed here is protected from archive/trash.\n"
            "# example-bank.com\n"
            "# person@example.com\n",
            encoding="utf-8",
        )
    if not blocklist.exists():
        blocklist.write_text(
            "# One sender email or domain per line. Anything listed here is treated as junk unless protected.\n"
            "# promo.example.com\n",
            encoding="utf-8",
        )


def execute_with_retries(
    request: Any,
    retries: int,
    retry_sleep: float,
    throttle: AdaptiveThrottle | None = None,
) -> dict[str, Any]:
    for attempt in range(retries + 1):
        try:
            if throttle:
                throttle.wait()
            response = request.execute()
            if throttle:
                throttle.record_success()
            return response
        except Exception as error:
            lowered = str(error).lower()
            status = getattr(getattr(error, "resp", None), "status", None)
            retryable = (
                status in {429, 500, 502, 503, 504}
                or (status == 403 and ("quota" in lowered or "rate limit" in lowered))
                or (status is None and ("429" in lowered or "rate limit" in lowered))
                or (status is None and "403" in lowered and "quota" in lowered)
                or "quota exceeded" in lowered
                or "backenderror" in lowered
                or "temporarily unavailable" in lowered
            )
            if attempt >= retries or not retryable:
                raise
            if throttle:
                throttle.record_retryable_error()
            delay = retry_sleep * (2**attempt)
            print(f"Retryable Gmail API error on attempt {attempt + 1}/{retries + 1}; sleeping {delay:.1f}s: {error}", file=sys.stderr, flush=True)
            time.sleep(delay)
    raise RuntimeError("unreachable retry state")


def header_map(payload: dict[str, Any]) -> dict[str, str]:
    return {item.get("name", "").lower(): item.get("value", "") for item in payload.get("headers", [])}


def parse_sender(address: str) -> tuple[str, str]:
    parsed = getaddresses([address])
    email = parsed[0][1].lower() if parsed else ""
    domain = email.rsplit("@", 1)[1].strip(".") if "@" in email else ""
    return email, domain


def parse_date(raw_date: str, internal_date: str | None) -> str:
    try:
        dt = parsedate_to_datetime(raw_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat()
    except Exception:
        pass
    if internal_date:
        try:
            return datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc).date().isoformat()
        except Exception:
            return ""
    return ""


def contains_any(text: str, keywords: list[str]) -> list[str]:
    lowered = text.lower()
    return [keyword for keyword in keywords if keyword in lowered]


def regex_hits(text: str, patterns: list[str]) -> list[str]:
    lowered = text.lower()
    return [pattern for pattern in patterns if re.search(pattern, lowered)]


def sender_localpart(sender_email: str) -> str:
    return sender_email.split("@", 1)[0].lower() if "@" in sender_email else ""


def registered_domain_for(domain: str) -> str:
    if not domain:
        return ""
    try:
        import tldextract
    except ImportError:
        parts = domain.lower().strip(".").split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else domain.lower().strip(".")
    extracted = tldextract.extract(domain)
    if extracted.domain and extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}".lower()
    return domain.lower().strip(".")


def has_one_click_unsubscribe(headers: dict[str, str]) -> bool:
    return headers.get("list-unsubscribe-post", "").strip().lower() == "list-unsubscribe=one-click"


def decode_payload_text(data: str) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def collect_body_text(payload: dict[str, Any], max_chars: int = 250_000) -> str:
    chunks: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        if sum(len(chunk) for chunk in chunks) >= max_chars:
            return
        mime_type = (part.get("mimeType") or "").lower()
        filename = part.get("filename") or ""
        body = part.get("body", {})
        data = body.get("data", "")
        if data and not filename and mime_type in {"text/plain", "text/html"}:
            chunks.append(decode_payload_text(data))
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    return "\n".join(chunks)[:max_chars]


def extract_body_unsubscribe_links(payload: dict[str, Any], limit: int = 20) -> list[str]:
    # Keep reports privacy-light: inspect transient body text, persist only scrubbed unsubscribe targets.
    body_text = html.unescape(collect_body_text(payload))
    if not body_text:
        return []
    candidates = re.findall(r"""https?://[^\s"'<>\\)]+|mailto:[^\s"'<>\\)]+""", body_text, flags=re.IGNORECASE)
    links = []
    seen = set()
    for candidate in candidates:
        cleaned = candidate.rstrip(".,;:!?]").strip()
        lowered = cleaned.lower()
        if "unsubscribe" not in lowered and "email-preferences" not in lowered and "preferences" not in lowered:
            continue
        normalized = normalize_unsubscribe_target(cleaned)
        if normalized not in seen:
            seen.add(normalized)
            links.append(normalized)
        if len(links) >= limit:
            break
    return links


def payload_has_attachment(payload: dict[str, Any]) -> bool:
    filename = payload.get("filename") or ""
    body = payload.get("body", {})
    if filename or body.get("attachmentId"):
        return True
    return any(payload_has_attachment(part) for part in payload.get("parts", []) or [])


def payload_headers(payload: dict[str, Any]) -> dict[str, str]:
    return {item.get("name", "").lower(): item.get("value", "") for item in payload.get("headers", [])}


def is_inline_attachment_part(payload: dict[str, Any]) -> bool:
    headers = payload_headers(payload)
    disposition = headers.get("content-disposition", "").lower()
    mime_type = (payload.get("mimeType") or "").lower()
    filename = payload.get("filename") or ""
    if "attachment" in disposition:
        return False
    if "inline" in disposition:
        return True
    return bool(filename) and mime_type.startswith("image/")


def attachment_counts(payload: dict[str, Any]) -> tuple[int, int]:
    real_count = 0
    inline_count = 0
    filename = payload.get("filename") or ""
    body = payload.get("body", {})
    if filename or body.get("attachmentId"):
        if is_inline_attachment_part(payload):
            inline_count += 1
        else:
            real_count += 1
    for part in payload.get("parts", []) or []:
        child_real, child_inline = attachment_counts(part)
        real_count += child_real
        inline_count += child_inline
    return real_count, inline_count


def payload_has_real_attachment(payload: dict[str, Any]) -> bool:
    real_count, _ = attachment_counts(payload)
    return real_count > 0


def collect_attachment_details(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    names: list[str] = []
    mime_types: list[str] = []
    filename = payload.get("filename") or ""
    body = payload.get("body", {})
    if filename or body.get("attachmentId"):
        if filename:
            names.append(filename)
        mime_type = payload.get("mimeType") or ""
        if mime_type:
            mime_types.append(mime_type)
    for part in payload.get("parts", []) or []:
        child_names, child_mime_types = collect_attachment_details(part)
        names.extend(child_names)
        mime_types.extend(child_mime_types)
    return sorted(set(names)), sorted(set(mime_types))


def age_score_boost(message_date: str) -> int:
    if not message_date:
        return 0
    try:
        year = int(message_date[:4])
    except ValueError:
        return 0
    if year < 2020:
        return 25
    if year < 2023:
        return 5
    return 0


def is_before_year(message_date: str, year: int) -> bool:
    if not message_date:
        return False
    try:
        return int(message_date[:4]) < year
    except ValueError:
        return False


def list_message_ids(
    service: Any,
    query: str,
    max_messages: int | None,
    retries: int,
    retry_sleep: float,
    throttle: AdaptiveThrottle | None = None,
) -> list[str]:
    ids: list[str] = []
    page_token = None
    while True:
        response = execute_with_retries(
            service.users().messages().list(userId="me", q=query or None, pageToken=page_token, maxResults=500),
            retries,
            retry_sleep,
            throttle,
        )
        ids.extend(item["id"] for item in response.get("messages", []))
        if max_messages and len(ids) >= max_messages:
            return ids[:max_messages]
        page_token = response.get("nextPageToken")
        if not page_token:
            return ids


def get_message_metadata(
    service: Any,
    message_id: str,
    retries: int,
    retry_sleep: float,
    throttle: AdaptiveThrottle | None = None,
    include_attachment_details: bool = False,
) -> dict[str, Any]:
    return execute_with_retries(
        service.users().messages().get(
            userId="me",
            id=message_id,
            format="full" if include_attachment_details else "metadata",
            metadataHeaders=[
                "From",
                "Subject",
                "Date",
                "List-Unsubscribe",
                "List-Unsubscribe-Post",
                "List-Id",
                "Precedence",
                "X-Campaign",
                "X-Mailer",
                "Auto-Submitted",
            ],
        ),
        retries,
        retry_sleep,
        throttle,
    )


def score_ad(headers: dict[str, str], labels: list[str], sender: str, sender_domain: str, subject: str, snippet: str, config: Config) -> tuple[int, list[str], list[str]]:
    searchable = " ".join([sender, sender_domain, subject, snippet])
    score = 0
    reasons: list[str] = []
    negative_reasons: list[str] = []

    if sender_domain in config.block_domains:
        score += 60
        reasons.append("blocklisted_domain")
    sender_email, _ = parse_sender(sender)
    if sender_email in config.block_senders:
        score += 60
        reasons.append("blocklisted_sender")
    if sender_domain in config.allow_domains or sender_email in config.allow_senders:
        score -= 100
        negative_reasons.append("allowlisted_sender_or_domain")

    if "CATEGORY_PROMOTIONS" in labels:
        score += 50
        reasons.append("gmail_category_promotions")
    if headers.get("list-unsubscribe"):
        score += 30
        reasons.append("list_unsubscribe_header")
    if has_one_click_unsubscribe(headers):
        score += 25
        reasons.append("one_click_unsubscribe_header")
    if headers.get("list-id"):
        score += 15
        reasons.append("list_id_header")
    if headers.get("precedence", "").lower() in {"bulk", "list"}:
        score += 15
        reasons.append("bulk_or_list_precedence")
    if headers.get("x-campaign"):
        score += 15
        reasons.append("campaign_header")

    for prefix, hits, weight, cap in [
        ("sender", contains_any(sender, AD_SENDER_KEYWORDS), 8, 25),
        ("subject", contains_any(subject, AD_SUBJECT_KEYWORDS), 10, 35),
        ("snippet", contains_any(snippet, AD_BODY_KEYWORDS), 12, 30),
    ]:
        if hits:
            score += min(cap, weight * len(hits))
            reasons.extend(f"{prefix}:{hit}" for hit in hits)

    subject_pattern_hits = regex_hits(subject, STRONG_PROMO_SUBJECT_PATTERNS)
    if subject_pattern_hits:
        score += min(35, 12 * len(subject_pattern_hits))
        reasons.extend(f"subject_pattern:{hit}" for hit in subject_pattern_hits)

    localpart = sender_localpart(sender_email)
    if localpart in PROMO_SENDER_LOCALPARTS or any(token in localpart for token in ("deal", "promo", "offer", "newsletter")):
        score += 18
        reasons.append(f"promotional_sender_localpart:{localpart}")

    negative_hits = contains_any(searchable, TRANSACTIONAL_KEYWORDS)
    if negative_hits:
        score -= min(70, 18 * len(negative_hits))
        negative_reasons.extend(f"transactional:{hit}" for hit in negative_hits)
    if any(label in labels for label in IMPORTANT_LABELS):
        score -= 25
        negative_reasons.append("important_or_primary_label")
    if subject.lower().startswith(("re:", "fwd:", "fw:")):
        score -= 25
        negative_reasons.append("reply_or_forward")
    auto_submitted = headers.get("auto-submitted", "").lower()
    if auto_submitted and auto_submitted != "no":
        score -= 25
        negative_reasons.append("auto_submitted_system_mail")
    return max(0, min(100, score)), reasons, negative_reasons


def is_perfect_ad_match(
    headers: dict[str, str],
    labels: list[str],
    sender_email: str,
    subject: str,
    snippet: str,
    ad_confidence: int,
    categories: list[str],
    negative_reasons: list[str],
) -> bool:
    # Treat "perfect" as auto-trash eligible only when independent bulk headers and promo content agree.
    localpart = sender_localpart(sender_email)
    positive_bulk_signals = sum(
        bool(signal)
        for signal in [
            "CATEGORY_PROMOTIONS" in labels,
            headers.get("list-unsubscribe"),
            has_one_click_unsubscribe(headers),
            headers.get("list-id"),
            headers.get("precedence", "").lower() in {"bulk", "list"},
            headers.get("x-campaign"),
            localpart in PROMO_SENDER_LOCALPARTS,
            any(token in localpart for token in ("deal", "promo", "offer", "newsletter")),
        ]
    )
    promo_content_signals = (
        len(contains_any(subject, AD_SUBJECT_KEYWORDS))
        + len(regex_hits(subject, STRONG_PROMO_SUBJECT_PATTERNS))
        + len(contains_any(snippet, AD_BODY_KEYWORDS))
    )
    disqualifiers = {
        "allowlisted_sender_or_domain",
        "important_or_primary_label",
        "reply_or_forward",
        "auto_submitted_system_mail",
        "has_attachment",
        "protected_category",
    }
    return (
        ad_confidence >= 100
        and "Ads Promotions" in categories
        and positive_bulk_signals >= 3
        and promo_content_signals >= 2
        and not disqualifiers.intersection(negative_reasons)
    )


def categorize(searchable: str, labels: list[str], ad_confidence: int) -> list[str]:
    categories: list[str] = []
    if ad_confidence >= 65:
        categories.append("Ads Promotions")
    elif "CATEGORY_PROMOTIONS" in labels:
        categories.append("Newsletters Bulk")
    for name, keywords in CATEGORY_RULES:
        if contains_any(searchable, keywords):
            categories.append(name)
    if "CATEGORY_SOCIAL" in labels:
        categories.append("Social")
    if "CATEGORY_UPDATES" in labels and not categories:
        categories.append("Updates")
    if "CATEGORY_FORUMS" in labels:
        categories.append("Forums")
    if not categories:
        categories.append("Review")
    return sorted(set(categories))


def decide(message: dict[str, Any], args: argparse.Namespace, config: Config) -> Decision:
    payload = message.get("payload", {})
    headers = header_map(payload)
    labels = message.get("labelIds", [])
    sender = headers.get("from", "")
    sender_email, sender_domain = parse_sender(sender)
    registered_domain = registered_domain_for(sender_domain)
    subject = headers.get("subject", "")
    snippet = message.get("snippet", "")
    message_date = parse_date(headers.get("date", ""), message.get("internalDate"))
    searchable = " ".join([sender, sender_domain, subject, snippet])
    ad_confidence, reasons, negative_reasons = score_ad(headers, labels, sender, sender_domain, subject, snippet, config)
    age_boost = age_score_boost(message_date)
    if age_boost and ad_confidence >= args.ad_threshold:
        ad_confidence = min(100, ad_confidence + age_boost)
        reasons.append(f"older_mail_boost:{age_boost}")
    categories = categorize(searchable, labels, ad_confidence)
    has_attachment = payload_has_attachment(payload)
    real_attachment_count, inline_attachment_count = attachment_counts(payload)
    has_real_attachment = real_attachment_count > 0
    if has_real_attachment:
        categories.append("Priority Attachments")
        categories = sorted(set(categories))
    body_unsubscribe_links = extract_body_unsubscribe_links(payload)
    attachment_names, attachment_mime_types = collect_attachment_details(payload) if has_attachment else ([], [])
    protected = (
        sender_domain in config.allow_domains
        or sender_email in config.allow_senders
        or has_real_attachment
        or bool(PROTECTED_CATEGORIES.intersection(categories))
        or any(label in labels for label in IMPORTANT_LABELS)
    )
    if has_real_attachment:
        negative_reasons.append("has_attachment")
    elif inline_attachment_count:
        reasons.append("inline_attachment_only")
    if PROTECTED_CATEGORIES.intersection(categories):
        negative_reasons.append("protected_category")
    if body_unsubscribe_links:
        reasons.append("body_unsubscribe_link")
    perfect_ad_match = not protected and is_perfect_ad_match(
        headers,
        labels,
        sender_email,
        subject,
        snippet,
        ad_confidence,
        categories,
        negative_reasons,
    )
    if perfect_ad_match:
        reasons.append("perfect_ad_match")

    planned_actions = [f"label:{category}" for category in categories]
    can_archive = not protected and ("Newsletters Bulk" in categories or ad_confidence >= args.ad_threshold)
    trash_threshold = args.pre_2020_trash_threshold if is_before_year(message_date, 2020) else args.trash_threshold
    can_trash = not protected and (
        perfect_ad_match
        or (ad_confidence >= trash_threshold and "Ads Promotions" in categories)
    )
    if is_before_year(message_date, 2020) and "Ads Promotions" in categories:
        reasons.append(f"pre_2020_trash_threshold:{trash_threshold}")
    if args.stage in {"archive", "trash"} and can_archive:
        planned_actions.append("archive")
    if args.stage == "trash" and args.trash_obvious_ads and can_trash:
        planned_actions.append("trash")

    review_priority = "normal"
    if "trash" in planned_actions or ad_confidence >= args.trash_threshold:
        review_priority = "trash_review"
    elif sender_domain in config.block_domains or sender_email in config.block_senders:
        review_priority = "blocked_sender_review"
    elif protected and ad_confidence >= args.ad_threshold:
        review_priority = "protected_ad_review"
    elif has_attachment:
        review_priority = "attachment_review"

    return Decision(
        message_id=message["id"],
        thread_id=message.get("threadId", ""),
        date=message_date,
        sender=sender,
        sender_email=sender_email,
        sender_domain=sender_domain,
        registered_domain=registered_domain,
        subject=subject,
        snippet=snippet,
        existing_labels=labels,
        categories=categories,
        ad_confidence=ad_confidence,
        reasons=reasons,
        negative_reasons=negative_reasons,
        planned_actions=planned_actions,
        has_attachment=has_attachment,
        has_real_attachment=has_real_attachment,
        attachment_count=real_attachment_count,
        inline_attachment_count=inline_attachment_count,
        message_size_estimate=int(message.get("sizeEstimate") or 0),
        list_unsubscribe=headers.get("list-unsubscribe", ""),
        body_unsubscribe_links=body_unsubscribe_links,
        attachment_names=attachment_names,
        attachment_mime_types=attachment_mime_types,
        protected=protected,
        perfect_ad_match=perfect_ad_match,
        review_priority=review_priority,
        scanned_at=datetime.now(timezone.utc).isoformat(),
    )


def get_thread_message_ids(service: Any, thread_id: str, retries: int, retry_sleep: float) -> list[str]:
    thread = execute_with_retries(
        service.users().threads().get(userId="me", id=thread_id, format="metadata", metadataHeaders=["Subject", "From"]),
        retries,
        retry_sleep,
    )
    return [item["id"] for item in thread.get("messages", [])]


def protect_mixed_threads(service: Any, decisions: list[Decision], retries: int, retry_sleep: float, limit: int) -> None:
    checked = 0
    by_thread: dict[str, list[Decision]] = defaultdict(list)
    for item in decisions:
        if "trash" in item.planned_actions:
            by_thread[item.thread_id].append(item)
    for thread_id, items in by_thread.items():
        if checked >= limit:
            return
        checked += 1
        thread_ids = get_thread_message_ids(service, thread_id, retries, retry_sleep)
        if len(thread_ids) > len(items):
            for item in items:
                item.planned_actions = [action for action in item.planned_actions if action != "trash"]
                item.negative_reasons.append("mixed_thread_protected")
                item.review_priority = "thread_review"
                item.protected = True


def decision_from_dict(data: dict[str, Any]) -> Decision:
    valid = Decision.__dataclass_fields__.keys()
    if "registered_domain" not in data:
        data["registered_domain"] = registered_domain_for(data.get("sender_domain", ""))
    if "has_real_attachment" not in data:
        data["has_real_attachment"] = bool(data.get("has_attachment", False))
    data.setdefault("attachment_count", 1 if data.get("has_real_attachment") else 0)
    data.setdefault("inline_attachment_count", 0)
    data.setdefault("message_size_estimate", 0)
    return Decision(**{key: data[key] for key in valid if key in data})


def load_progress(path: Path) -> dict[str, Decision]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {item["message_id"]: decision_from_dict(item) for item in data}


def save_progress(path: Path, decisions: dict[str, Decision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(item) for item in decisions.values()], indent=2, ensure_ascii=False), encoding="utf-8")


def open_state_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            date TEXT,
            sender TEXT,
            sender_email TEXT,
            sender_domain TEXT,
            registered_domain TEXT,
            subject TEXT,
            categories_json TEXT NOT NULL,
            planned_actions_json TEXT NOT NULL,
            ad_confidence INTEGER NOT NULL,
            protected INTEGER NOT NULL,
            perfect_ad_match INTEGER NOT NULL,
            has_attachment INTEGER NOT NULL,
            has_real_attachment INTEGER NOT NULL,
            attachment_count INTEGER NOT NULL,
            inline_attachment_count INTEGER NOT NULL,
            message_size_estimate INTEGER NOT NULL,
            review_priority TEXT,
            action_done TEXT,
            scanned_at TEXT,
            decision_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS action_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            stage TEXT NOT NULL,
            action TEXT NOT NULL,
            message_id TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS domain_review (
            domain TEXT PRIMARY KEY,
            registered_domain TEXT,
            status TEXT NOT NULL DEFAULT 'unreviewed',
            note TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def upsert_state_decisions(conn: sqlite3.Connection | None, decisions: list[Decision]) -> None:
    if conn is None or not decisions:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for item in decisions:
        rows.append(
            (
                item.message_id,
                item.thread_id,
                item.date,
                item.sender,
                item.sender_email,
                item.sender_domain,
                item.registered_domain,
                item.subject,
                json.dumps(item.categories, ensure_ascii=False),
                json.dumps(item.planned_actions, ensure_ascii=False),
                item.ad_confidence,
                int(item.protected),
                int(item.perfect_ad_match),
                int(item.has_attachment),
                int(item.has_real_attachment),
                item.attachment_count,
                item.inline_attachment_count,
                item.message_size_estimate,
                item.review_priority,
                item.action_done,
                item.scanned_at,
                json.dumps(asdict(item), ensure_ascii=False),
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO messages (
            message_id, thread_id, date, sender, sender_email, sender_domain, registered_domain,
            subject, categories_json, planned_actions_json, ad_confidence, protected,
            perfect_ad_match, has_attachment, has_real_attachment, attachment_count,
            inline_attachment_count, message_size_estimate, review_priority, action_done,
            scanned_at, decision_json, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            thread_id=excluded.thread_id,
            date=excluded.date,
            sender=excluded.sender,
            sender_email=excluded.sender_email,
            sender_domain=excluded.sender_domain,
            registered_domain=excluded.registered_domain,
            subject=excluded.subject,
            categories_json=excluded.categories_json,
            planned_actions_json=excluded.planned_actions_json,
            ad_confidence=excluded.ad_confidence,
            protected=excluded.protected,
            perfect_ad_match=excluded.perfect_ad_match,
            has_attachment=excluded.has_attachment,
            has_real_attachment=excluded.has_real_attachment,
            attachment_count=excluded.attachment_count,
            inline_attachment_count=excluded.inline_attachment_count,
            message_size_estimate=excluded.message_size_estimate,
            review_priority=excluded.review_priority,
            action_done=excluded.action_done,
            scanned_at=excluded.scanned_at,
            decision_json=excluded.decision_json,
            updated_at=excluded.updated_at
        """,
        rows,
    )
    conn.commit()


def record_action_ledger(
    conn: sqlite3.Connection | None,
    stage: str,
    action: str,
    message_id: str,
    status: str = "success",
    detail: str = "",
) -> None:
    if conn is None:
        return
    conn.execute(
        "INSERT INTO action_ledger (created_at, stage, action, message_id, status, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), stage, action, message_id, status, detail),
    )
    conn.commit()


def decisions_for_current_query(progress: dict[str, Decision], message_ids: list[str]) -> list[Decision]:
    """Return decisions in Gmail query order, excluding stale rows from reused progress files."""
    return [progress[message_id] for message_id in message_ids if message_id in progress]


def apply_trash_policy_caps(decisions: list[Decision], args: argparse.Namespace) -> None:
    trash_items = [item for item in decisions if "trash" in item.planned_actions]
    if args.max_trash_total and len(trash_items) > args.max_trash_total:
        allowed = {item.message_id for item in trash_items[: args.max_trash_total]}
        for item in trash_items[args.max_trash_total :]:
            item.planned_actions = [action for action in item.planned_actions if action != "trash"]
            item.negative_reasons.append(f"trash_total_cap:{args.max_trash_total}")
            item.review_priority = "trash_capped_review"
        trash_items = [item for item in trash_items if item.message_id in allowed]

    if args.max_trash_per_domain:
        seen: dict[str, int] = defaultdict(int)
        for item in trash_items:
            domain = item.registered_domain or item.sender_domain or "(unknown)"
            seen[domain] += 1
            if seen[domain] > args.max_trash_per_domain:
                item.planned_actions = [action for action in item.planned_actions if action != "trash"]
                item.negative_reasons.append(f"trash_domain_cap:{args.max_trash_per_domain}")
                item.review_priority = "trash_capped_review"

    if args.canary_limit and args.apply and args.stage == "trash":
        remaining = [item for item in decisions if "trash" in item.planned_actions]
        for item in remaining[args.canary_limit :]:
            item.planned_actions = [action for action in item.planned_actions if action != "trash"]
            item.negative_reasons.append(f"trash_canary_limit:{args.canary_limit}")
            item.review_priority = "trash_capped_review"


def should_refresh(decision: Decision, args: argparse.Namespace) -> bool:
    if args.refresh_existing:
        return True
    if not args.refresh_after_days or not decision.scanned_at:
        return False
    try:
        scanned_at = datetime.fromisoformat(decision.scanned_at)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - scanned_at > timedelta(days=args.refresh_after_days)


def scan_messages(
    message_ids: list[str],
    progress: dict[str, Decision],
    creds: Any,
    build_func: Any,
    args: argparse.Namespace,
    config: Config,
) -> dict[str, Decision]:
    throttle = AdaptiveThrottle(args.sleep)
    pending = [
        message_id
        for message_id in message_ids
        if message_id not in progress or should_refresh(progress[message_id], args)
    ]
    if not pending:
        return progress

    progress_path = Path(args.progress_file)
    completed = 0
    thread_local = threading.local()

    def service_for_thread() -> Any:
        if not hasattr(thread_local, "service"):
            thread_local.service = build_gmail_service(build_func, creds, args)
        return thread_local.service

    def worker(message_id: str) -> tuple[str, Decision | None, str | None]:
        try:
            message = get_message_metadata(
                service_for_thread(),
                message_id,
                args.retries,
                args.retry_sleep,
                throttle,
                args.attachment_details,
            )
            return message_id, decide(message, args, config), None
        except Exception as error:
            return message_id, None, str(error)

    if args.workers <= 1:
        for message_id in pending:
            _, decision, error = worker(message_id)
            completed += 1
            if error:
                print(f"Skipping {message_id}: {error}", file=sys.stderr)
            elif decision:
                progress[message_id] = decision
            if completed % args.save_every == 0:
                save_progress(progress_path, progress)
                print(f"Scanned {completed}/{len(pending)} pending; saved progress...")
        return progress

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(worker, message_id) for message_id in pending]
        for future in as_completed(futures):
            message_id, decision, error = future.result()
            completed += 1
            if error:
                print(f"Skipping {message_id}: {error}", file=sys.stderr)
            elif decision:
                progress[message_id] = decision
            if completed % args.save_every == 0:
                save_progress(progress_path, progress)
                print(f"Scanned {completed}/{len(pending)} pending; saved progress...")
    return progress


def create_label(service: Any, label_name: str, retries: int, retry_sleep: float) -> dict[str, Any]:
    return execute_with_retries(
        service.users().labels().create(userId="me", body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}),
        retries,
        retry_sleep,
    )


def get_or_create_labels(service: Any, label_names: list[str], retries: int, retry_sleep: float) -> dict[str, str]:
    existing = execute_with_retries(service.users().labels().list(userId="me"), retries, retry_sleep).get("labels", [])
    label_ids = {label.get("name"): label.get("id") for label in existing}
    for label_name in label_names:
        if label_name not in label_ids:
            created = create_label(service, label_name, retries, retry_sleep)
            label_ids[label_name] = created["id"]
    return {name: label_ids[name] for name in label_names}


def apply_decisions(service: Any, decisions: list[Decision], args: argparse.Namespace, state_conn: sqlite3.Connection | None = None) -> None:
    started = time.monotonic()
    categories = sorted({category for item in decisions for category in item.categories})
    label_ids = get_or_create_labels(service, [f"{ROOT_LABEL}/{category}" for category in categories], args.retries, args.retry_sleep)
    grouped: dict[tuple[tuple[str, ...], tuple[str, ...]], list[Decision]] = {}
    trash_items: list[Decision] = []
    for item in decisions:
        if item.protected and ("trash" in item.planned_actions or "archive" in item.planned_actions):
            item.planned_actions = [action for action in item.planned_actions if action not in {"trash", "archive"}]
        if "trash" in item.planned_actions:
            trash_items.append(item)
            continue
        add_ids = tuple(sorted(label_ids[f"{ROOT_LABEL}/{category}"] for category in item.categories))
        remove_ids = ("INBOX",) if "archive" in item.planned_actions else ()
        grouped.setdefault((add_ids, remove_ids), []).append(item)

    grouped_count = sum(len(items) for items in grouped.values())
    batch_count = sum((len(items) + args.batch_size - 1) // args.batch_size for items in grouped.values())
    print(
        f"Apply plan: trash={len(trash_items)} single-message calls; "
        f"label/archive={grouped_count} messages in {batch_count} batchModify calls.",
        flush=True,
    )

    for index, item in enumerate(trash_items, 1):
        execute_with_retries(service.users().messages().trash(userId="me", id=item.message_id), args.retries, args.retry_sleep)
        item.action_done = "yes"
        record_action_ledger(state_conn, args.stage, "trash", item.message_id)
        upsert_state_decisions(state_conn, [item])
        if index == 1 or index == len(trash_items) or index % args.apply_progress_every == 0:
            elapsed = max(0.001, time.monotonic() - started)
            rate = index / elapsed
            remaining = (len(trash_items) - index) / rate if rate else 0.0
            print(
                f"Trashed {index}/{len(trash_items)} messages "
                f"({rate:.2f}/s, est remaining {remaining / 60:.1f} min)...",
                flush=True,
            )

    batch_index = 0
    for (add_ids, remove_ids), items in grouped.items():
        for start in range(0, len(items), args.batch_size):
            batch_index += 1
            chunk = items[start : start + args.batch_size]
            body: dict[str, list[str]] = {"ids": [item.message_id for item in chunk]}
            if add_ids:
                body["addLabelIds"] = list(add_ids)
            if remove_ids:
                body["removeLabelIds"] = list(remove_ids)
            execute_with_retries(service.users().messages().batchModify(userId="me", body=body), args.retries, args.retry_sleep)
            for item in chunk:
                item.action_done = "yes"
                for action in item.planned_actions:
                    if action == "archive" or action.startswith("label:"):
                        record_action_ledger(state_conn, args.stage, action, item.message_id)
            upsert_state_decisions(state_conn, chunk)
            if batch_index == 1 or batch_index == batch_count or batch_index % args.apply_progress_every == 0:
                print(f"Applied label/archive batch {batch_index}/{batch_count}...", flush=True)


def write_csv(path: Path, decisions: list[Decision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(Decision.__dataclass_fields__.keys())
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for item in decisions:
            row = asdict(item)
            for key, value in row.items():
                if isinstance(value, list):
                    row[key] = "; ".join(str(part) for part in value)
            writer.writerow(row)


def write_json(path: Path, decisions: list[Decision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(item) for item in decisions], indent=2, ensure_ascii=False), encoding="utf-8")


def write_unsubscribe_report(path: Path, decisions: list[Decision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["source", "sender_domain", "sender", "count", "unsubscribe_target"])
        writer.writeheader()
        header_grouped: dict[tuple[str, str, str], int] = Counter(
            (item.sender_domain, item.sender, item.list_unsubscribe)
            for item in decisions
            if item.list_unsubscribe
        )
        body_grouped: dict[tuple[str, str, str], int] = Counter(
            (item.sender_domain, item.sender, link)
            for item in decisions
            for link in item.body_unsubscribe_links
        )
        for (domain, sender, target), count in header_grouped.most_common():
            writer.writerow({"source": "header", "sender_domain": domain, "sender": sender, "count": count, "unsubscribe_target": target})
        for (domain, sender, target), count in body_grouped.most_common():
            writer.writerow({"source": "body", "sender_domain": domain, "sender": sender, "count": count, "unsubscribe_target": target})


def write_sender_report(path: Path, decisions: list[Decision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[Decision]] = defaultdict(list)
    for item in decisions:
        grouped[item.registered_domain or item.sender_domain or "(unknown)"].append(item)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["registered_domain", "count", "avg_ad_confidence", "categories", "planned_trash", "protected", "size_mb", "sample_subject"])
        writer.writeheader()
        for domain, items in sorted(grouped.items(), key=lambda pair: len(pair[1]), reverse=True):
            avg = sum(item.ad_confidence for item in items) / len(items)
            cats = Counter(category for item in items for category in item.categories)
            size_mb = sum(item.message_size_estimate for item in items) / (1024 * 1024)
            writer.writerow({
                "registered_domain": domain,
                "count": len(items),
                "avg_ad_confidence": f"{avg:.1f}",
                "categories": "; ".join(name for name, _ in cats.most_common(5)),
                "planned_trash": sum(1 for item in items if "trash" in item.planned_actions),
                "protected": sum(1 for item in items if item.protected),
                "size_mb": f"{size_mb:.1f}",
                "sample_subject": items[0].subject,
            })


def write_storage_report(path: Path, decisions: list[Decision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[Decision]] = defaultdict(list)
    for item in decisions:
        grouped[item.registered_domain or item.sender_domain or "(unknown)"].append(item)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "registered_domain",
                "messages",
                "size_mb",
                "real_attachments",
                "planned_trash",
                "protected",
                "largest_subject",
                "largest_message_size_mb",
            ],
        )
        writer.writeheader()
        rows = []
        for domain, items in grouped.items():
            largest = max(items, key=lambda item: item.message_size_estimate)
            rows.append(
                {
                    "registered_domain": domain,
                    "messages": len(items),
                    "size_mb": sum(item.message_size_estimate for item in items) / (1024 * 1024),
                    "real_attachments": sum(item.attachment_count for item in items),
                    "planned_trash": sum(1 for item in items if "trash" in item.planned_actions),
                    "protected": sum(1 for item in items if item.protected),
                    "largest_subject": largest.subject,
                    "largest_message_size_mb": largest.message_size_estimate / (1024 * 1024),
                }
            )
        for row in sorted(rows, key=lambda item: item["size_mb"], reverse=True):
            row["size_mb"] = f"{row['size_mb']:.1f}"
            row["largest_message_size_mb"] = f"{row['largest_message_size_mb']:.1f}"
            writer.writerow(row)


def write_review_workflow(path: Path, decisions: list[Decision]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[Decision]] = defaultdict(list)
    for item in decisions:
        grouped[item.registered_domain or item.sender_domain or "(unknown)"].append(item)

    rows = []
    for domain, items in grouped.items():
        categories = Counter(category for item in items for category in item.categories)
        actions = Counter(action for item in items for action in item.planned_actions)
        reasons = Counter(reason for item in items for reason in item.reasons)
        latest = max((item.date for item in items if item.date), default="")
        oldest = min((item.date for item in items if item.date), default="")
        row = {
            "registered_domain": domain,
            "status": "unreviewed",
            "messages": len(items),
            "planned_trash": actions["trash"],
            "planned_archive": actions["archive"],
            "protected": sum(1 for item in items if item.protected),
            "real_attachments": sum(item.attachment_count for item in items),
            "size_mb": round(sum(item.message_size_estimate for item in items) / (1024 * 1024), 1),
            "avg_ad_confidence": round(sum(item.ad_confidence for item in items) / len(items), 1),
            "top_categories": "; ".join(name for name, _ in categories.most_common(5)),
            "top_reasons": "; ".join(name for name, _ in reasons.most_common(5)),
            "oldest": oldest,
            "latest": latest,
            "sample_subjects": " | ".join(item.subject for item in items[:3]),
            "suggested_action": suggested_domain_action(items),
        }
        rows.append(row)

    rows.sort(key=lambda item: (item["planned_trash"], item["messages"], item["size_mb"]), reverse=True)
    csv_path = path / "domain_review.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = list(rows[0].keys()) if rows else [
            "registered_domain",
            "status",
            "messages",
            "planned_trash",
            "planned_archive",
            "protected",
            "real_attachments",
            "size_mb",
            "avg_ad_confidence",
            "top_categories",
            "top_reasons",
            "oldest",
            "latest",
            "sample_subjects",
            "suggested_action",
        ]
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    (path / "domain_review.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")


def suggested_domain_action(items: list[Decision]) -> str:
    protected_count = sum(1 for item in items if item.protected)
    trash_count = sum(1 for item in items if "trash" in item.planned_actions)
    priority_count = sum(1 for item in items if {"Priority Immigration", "Priority Studies", "Priority Attachments"}.intersection(item.categories))
    if priority_count:
        return "protect_priority"
    if trash_count >= 20 and protected_count == 0:
        return "approve_trash"
    if any(item.list_unsubscribe or item.body_unsubscribe_links for item in items):
        return "unsubscribe_review"
    if protected_count:
        return "allow_or_review"
    return "review"


def write_action_manifests(path: Path, decisions: list[Decision]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    manifests = {
        "label": [item for item in decisions if any(action.startswith("label:") for action in item.planned_actions)],
        "archive": [item for item in decisions if "archive" in item.planned_actions],
        "trash": [item for item in decisions if "trash" in item.planned_actions],
    }
    for name, items in manifests.items():
        payload = {
            "stage": name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "message_count": len(items),
            "message_ids": [item.message_id for item in items],
            "items": [asdict(item) for item in items],
        }
        (path / f"{name}_manifest.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_manifest_ids(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data.get("message_ids", []))


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def pct(part: int | float, total: int | float) -> float:
    return 0.0 if not total else (float(part) / float(total)) * 100


def extract_unsubscribe_targets(header_value: str) -> list[str]:
    targets = re.findall(r"<([^>]+)>", header_value)
    if not targets and header_value:
        targets = [part.strip() for part in header_value.split(",")]
    return [target.strip() for target in targets if target.strip()]


def normalize_unsubscribe_target(target: str) -> str:
    cleaned = target.strip().strip("<>").strip()
    if cleaned.lower().startswith("mailto:"):
        return cleaned.lower()
    return cleaned.split("#", 1)[0]


def render_unsubscribe_target(target: str) -> str:
    cleaned = normalize_unsubscribe_target(target)
    lowered = cleaned.lower()
    if lowered.startswith(("http://", "https://", "mailto:")):
        return f'<a href="{esc(cleaned)}" target="_blank" rel="noopener noreferrer">{esc(cleaned)}</a>'
    return esc(cleaned)


def render_unsubscribe_links(targets: list[str]) -> str:
    return "<br>".join(render_unsubscribe_target(target) for target in targets)


def unsubscribe_domain_score(entry: dict[str, Any]) -> float:
    avg_confidence = entry["confidence"] / max(1, entry["count"])
    duplicate_bonus = max(0, entry["count"] - len(entry["targets"])) * 1.5
    volume_bonus = min(30, entry["count"] * 0.8)
    recent_bonus = 8 if entry["latest"] >= "2023-01-01" else 0
    return min(100.0, avg_confidence * 0.55 + volume_bonus + duplicate_bonus + recent_bonus)


def grouped_unsubscribe_domains(items: list[Decision]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        domain = item.sender_domain or "(unknown)"
        entry = grouped.setdefault(
            domain,
            {
                "count": 0,
                "senders": Counter(),
                "targets": Counter(),
                "subjects": Counter(),
                "confidence": 0,
                "latest": "",
            },
        )
        entry["count"] += 1
        entry["confidence"] += item.ad_confidence
        entry["latest"] = max(entry["latest"], item.date)
        entry["senders"][item.sender] += 1
        entry["subjects"][item.subject] += 1
        for target in extract_unsubscribe_targets(item.list_unsubscribe):
            entry["targets"][normalize_unsubscribe_target(target)] += 1
    return grouped


def render_unsubscribable_domains(items: list[Decision], limit: int = 100) -> str:
    grouped = grouped_unsubscribe_domains(items)

    rows = []
    def priority(pair: tuple[str, dict[str, Any]]) -> tuple[float, int, str]:
        _, entry = pair
        return (unsubscribe_domain_score(entry), entry["count"], entry["latest"])

    for domain, entry in sorted(grouped.items(), key=priority, reverse=True)[:limit]:
        targets = "<br>".join(
            f"{render_unsubscribe_target(target)} <span class=\"muted-count\">x{count}</span>"
            for target, count in entry["targets"].most_common(5)
        )
        senders = "<br>".join(esc(sender) for sender, _ in entry["senders"].most_common(3))
        samples = "<br>".join(esc(subject) for subject, _ in entry["subjects"].most_common(3))
        avg_confidence = entry["confidence"] / max(1, entry["count"])
        duplicate_count = max(0, entry["count"] - len(entry["targets"]))
        score = unsubscribe_domain_score(entry)
        rows.append(
            "<tr>"
            f"<td>{esc(domain)}</td>"
            f"<td><span class=\"score {'high' if score >= 75 else 'medium' if score >= 50 else 'low'}\">{score:.0f}</span></td>"
            f"<td>{esc(entry['count'])}</td>"
            f"<td>{esc(len(entry['targets']))}</td>"
            f"<td>{esc(duplicate_count)}</td>"
            f"<td>{avg_confidence:.1f}</td>"
            f"<td>{esc(entry['latest'])}</td>"
            f"<td>{senders}</td>"
            f"<td>{targets}</td>"
            f"<td>{samples}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>Domain</th><th>Score</th><th>Messages</th><th>Unique Links</th><th>Duplicate Headers</th><th>Avg Ad Confidence</th><th>Last Seen</th><th>Senders</th><th>Clickable Unsubscribe Links</th><th>Sample Subjects</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_unsubscribe_priority(items: list[Decision], limit: int = 20) -> str:
    grouped = grouped_unsubscribe_domains(items)
    rows = []
    for domain, entry in sorted(
        grouped.items(),
        key=lambda pair: (unsubscribe_domain_score(pair[1]), pair[1]["count"], pair[1]["latest"]),
        reverse=True,
    )[:limit]:
        score = unsubscribe_domain_score(entry)
        top_target = entry["targets"].most_common(1)[0][0] if entry["targets"] else ""
        rows.append(
            "<tr>"
            f"<td>{esc(domain)}</td>"
            f"<td><span class=\"score {'high' if score >= 75 else 'medium' if score >= 50 else 'low'}\">{score:.0f}</span></td>"
            f"<td>{esc(entry['count'])}</td>"
            f"<td>{esc(len(entry['targets']))}</td>"
            f"<td>{esc(entry['latest'])}</td>"
            f"<td>{render_unsubscribe_target(top_target)}</td>"
            "</tr>"
        )
    return (
        "<div class=\"table-wrap priority\"><table><thead><tr>"
        "<th>Domain</th><th>Unsubscribe Score</th><th>Messages</th><th>Unique Links</th><th>Last Seen</th><th>Best Link</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def render_body_unsubscribe_links(items: list[Decision], limit: int = 100) -> str:
    rows = []
    body_items = [item for item in items if item.body_unsubscribe_links]
    for item in body_items[:limit]:
        rows.append(
            "<tr>"
            f"<td>{esc(item.sender_domain)}</td>"
            f"<td>{esc(item.sender)}</td>"
            f"<td>{esc(item.subject)}</td>"
            f"<td>{render_cell('ad_confidence', item.ad_confidence)}</td>"
            f"<td>{render_unsubscribe_links(item.body_unsubscribe_links)}</td>"
            "</tr>"
        )
    return (
        "<div class=\"table-wrap\"><table><thead><tr>"
        "<th>Domain</th><th>Sender</th><th>Subject</th><th>Ad Confidence</th><th>Clickable Body Unsubscribe Links</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def render_bulk_preview(items: list[Decision], limit: int = 50) -> str:
    grouped: dict[str, list[Decision]] = defaultdict(list)
    for item in items:
        grouped[item.registered_domain or item.sender_domain or "(unknown)"].append(item)
    rows = []
    for domain, domain_items in sorted(grouped.items(), key=lambda pair: len(pair[1]), reverse=True)[:limit]:
        archive_count = sum(1 for item in domain_items if "archive" in item.planned_actions)
        trash_count = sum(1 for item in domain_items if "trash" in item.planned_actions)
        protected_count = sum(1 for item in domain_items if item.protected)
        avg_confidence = sum(item.ad_confidence for item in domain_items) / len(domain_items)
        categories = Counter(category for item in domain_items for category in item.categories)
        rows.append(
            "<tr>"
            f"<td>{esc(domain)}</td>"
            f"<td>{len(domain_items)}</td>"
            f"<td>{archive_count}</td>"
            f"<td>{trash_count}</td>"
            f"<td>{protected_count}</td>"
            f"<td>{avg_confidence:.1f}</td>"
            f"<td>{esc(', '.join(name for name, _ in categories.most_common(4)))}</td>"
            f"<td>{esc(domain_items[0].subject)}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>Domain</th><th>Total</th><th>Would Archive</th><th>Would Trash</th><th>Protected</th><th>Avg Ad Confidence</th><th>Top Categories</th><th>Sample Subject</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_trash_domain_summary(items: list[Decision], limit: int = 50) -> str:
    grouped: dict[str, list[Decision]] = defaultdict(list)
    for item in items:
        if "trash" in item.planned_actions:
            grouped[item.registered_domain or item.sender_domain or "(unknown)"].append(item)
    rows = []
    for domain, domain_items in sorted(grouped.items(), key=lambda pair: len(pair[1]), reverse=True)[:limit]:
        dates = [item.date for item in domain_items if item.date]
        avg_confidence = sum(item.ad_confidence for item in domain_items) / len(domain_items)
        reason_counts = Counter(reason for item in domain_items for reason in item.reasons)
        sample_subjects = "<br>".join(esc(item.subject) for item in domain_items[:3])
        rows.append(
            "<tr>"
            f"<td>{esc(domain)}</td>"
            f"<td>{len(domain_items)}</td>"
            f"<td>{avg_confidence:.1f}</td>"
            f"<td>{esc(min(dates) if dates else '')}</td>"
            f"<td>{esc(max(dates) if dates else '')}</td>"
            f"<td>{esc(', '.join(reason for reason, _ in reason_counts.most_common(5)))}</td>"
            f"<td>{sample_subjects}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>Domain</th><th>Would Trash</th><th>Avg Ad Confidence</th><th>Oldest</th><th>Newest</th><th>Top Reasons</th><th>Sample Subjects</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_reason_summary(items: list[Decision], attr: str, limit: int = 20) -> str:
    counts = Counter(reason for item in items for reason in getattr(item, attr))
    rows = [
        "<tr>"
        f"<td>{esc(reason)}</td>"
        f"<td>{count}</td>"
        "</tr>"
        for reason, count in counts.most_common(limit)
    ]
    return (
        "<table><thead><tr><th>Reason</th><th>Messages</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_table(items: list[Decision], columns: list[str], limit: int = 50) -> str:
    rows = []
    for item in items[:limit]:
        data = asdict(item)
        cells = []
        for column in columns:
            value = data[column]
            if isinstance(value, list):
                value = ", ".join(str(part) for part in value)
            cells.append(f"<td>{render_cell(column, value)}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    headers = "".join(f"<th>{esc(column.replace('_', ' ').title())}</th>" for column in columns)
    return f"<div class=\"table-wrap\"><table><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"


def render_cell(column: str, value: Any) -> str:
    if isinstance(value, list):
        if column == "body_unsubscribe_links":
            return render_unsubscribe_links([str(part) for part in value])
        if column in {"categories", "planned_actions", "reasons", "negative_reasons"}:
            return "".join(f"<span class=\"pill\">{esc(part)}</span>" for part in value)
        value = ", ".join(str(part) for part in value)
    if column in {"ad_confidence", "avg_ad_confidence"}:
        try:
            score = float(value)
            level = "high" if score >= 85 else "medium" if score >= 65 else "low"
            return f"<span class=\"score {level}\">{score:.0f}</span>"
        except (TypeError, ValueError):
            return esc(value)
    if column == "list_unsubscribe":
        targets = extract_unsubscribe_targets(str(value))
        return render_unsubscribe_links(targets) if targets else esc(value)
    if column == "message_size_estimate":
        try:
            return f"{float(value) / (1024 * 1024):.1f} MB"
        except (TypeError, ValueError):
            return esc(value)
    if column in {"protected", "has_attachment", "has_real_attachment", "perfect_ad_match"}:
        return f"<span class=\"status {'yes' if value else 'no'}\">{esc(value)}</span>"
    return esc(value)


def render_metric_card(label: str, value: int | str, total: int | None = None, tone: str = "neutral") -> str:
    bar = ""
    if total:
        width = min(100, max(0, pct(int(value), total))) if isinstance(value, int) else 0
        bar = f"<div class=\"bar\"><span class=\"{tone}\" style=\"width:{width:.1f}%\"></span></div>"
    return f"<section class=\"metric {tone}\"><strong>{esc(label)}</strong><span>{esc(value)}</span>{bar}</section>"


def render_rank_list(items: list[tuple[str, int]], total: int, tone: str = "neutral", limit: int = 12) -> str:
    rows = []
    for name, count in items[:limit]:
        width = min(100, max(2, pct(count, total)))
        rows.append(
            "<li>"
            f"<div><span>{esc(name)}</span><b>{count}</b></div>"
            f"<em><i class=\"{tone}\" style=\"width:{width:.1f}%\"></i></em>"
            "</li>"
        )
    return "<ul class=\"rank-list\">" + "".join(rows) + "</ul>"


def write_dashboard(path: Path, decisions: list[Decision], args: argparse.Namespace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    category_counts = Counter(category for item in decisions for category in item.categories)
    sender_counts = Counter(item.registered_domain or item.sender_domain or "(unknown)" for item in decisions)
    review_counts = Counter(item.review_priority for item in decisions)
    trash_items = [item for item in decisions if item.review_priority in {"trash_review", "thread_review"}]
    protected_ads = [item for item in decisions if item.review_priority == "protected_ad_review"]
    attachment_items = [item for item in decisions if item.has_attachment]
    real_attachment_items = [item for item in decisions if item.has_real_attachment]
    priority_items = [item for item in decisions if {"Priority Immigration", "Priority Studies", "Priority Attachments"}.intersection(item.categories)]
    unsubscribe_items = [item for item in decisions if item.list_unsubscribe]
    body_unsubscribe_items = [item for item in decisions if item.body_unsubscribe_links]
    top_noisy = sender_counts.most_common(20)
    total = len(decisions)
    ad_count = category_counts["Ads Promotions"]
    protected_count = sum(1 for item in decisions if item.protected)
    high_confidence = sum(1 for item in decisions if item.ad_confidence >= 85)
    perfect_ad_count = sum(1 for item in decisions if item.perfect_ad_match)
    archive_count = sum(1 for item in decisions if "archive" in item.planned_actions)
    trash_count = sum(1 for item in decisions if "trash" in item.planned_actions)
    applied_count = sum(1 for item in decisions if item.action_done == "yes")
    total_size_mb = sum(item.message_size_estimate for item in decisions) / (1024 * 1024)
    stale_progress_count = max(0, len(load_progress(Path(args.progress_file))) - total) if args.resume else 0
    pre_2020_trash_items = [
        item for item in decisions if is_before_year(item.date, 2020) and "trash" in item.planned_actions
    ]
    unsubscribe_grouped = grouped_unsubscribe_domains(unsubscribe_items)
    unsubscribe_duplicate_headers = sum(max(0, entry["count"] - len(entry["targets"])) for entry in unsubscribe_grouped.values())

    stat_cards = [
        render_metric_card("Messages", total, None, "neutral"),
        render_metric_card("Ad/Promo", ad_count, total, "warn"),
        render_metric_card("High Confidence Ads", high_confidence, total, "danger"),
        render_metric_card("Perfect Ad Matches", perfect_ad_count, total, "danger"),
        render_metric_card("Protected", protected_count, total, "safe"),
        render_metric_card("Priority Items", len(priority_items), total, "safe"),
        render_metric_card("Real Attachments", len(real_attachment_items), total, "neutral"),
        render_metric_card("Storage MB", f"{total_size_mb:.0f}", None, "neutral"),
        render_metric_card("Header Unsub Domains", len({item.sender_domain for item in unsubscribe_items}), None, "accent"),
        render_metric_card("Body Unsub Links", sum(len(item.body_unsubscribe_links) for item in body_unsubscribe_items), None, "accent"),
        render_metric_card("Deduped Unsub Headers", unsubscribe_duplicate_headers, len(unsubscribe_items) or None, "accent"),
        render_metric_card("Pre-2020 Trash", len(pre_2020_trash_items), total, "danger"),
        render_metric_card("Applied", applied_count, total, "safe"),
        render_metric_card("Cached Outside Query", stale_progress_count, None, "neutral"),
    ]
    cards_html = "".join(stat_cards)
    category_html = render_rank_list(category_counts.most_common(), total, "accent")
    sender_html = render_rank_list(top_noisy, total, "warn")
    review_html = render_rank_list(review_counts.most_common(), total, "safe")
    action_html = render_rank_list(
        [("Would label", total), ("Would archive", archive_count), ("Would trash", trash_count), ("Protected", protected_count)],
        total,
        "danger",
    )

    css = """
    :root{--bg:#f3f5f7;--panel:#fff;--line:#d9e0e7;--text:#1f2933;--muted:#657181;--ink:#18212f;--accent:#2563eb;--warn:#d97706;--danger:#c2410c;--safe:#0f766e}
    *{box-sizing:border-box}body{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;margin:0;background:var(--bg);color:var(--text);letter-spacing:0}
    header{background:#111827;color:white;padding:28px 32px 22px;border-bottom:4px solid #f59e0b}
    header h1{margin:0 0 8px;font-size:30px;font-weight:750}header .meta{color:#cbd5e1;font-size:14px;line-height:1.5}
    main{padding:24px 32px;max-width:1520px;margin:auto}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:18px}
    .metric,.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:0 1px 2px rgba(16,24,40,.05)}
    .metric{padding:16px;min-height:118px}.metric strong{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}.metric span{display:block;font-size:34px;font-weight:760;margin:10px 0 12px;color:var(--ink)}
    .bar, .rank-list em{display:block;height:8px;background:#e8edf3;border-radius:999px;overflow:hidden}.bar span,.rank-list i{display:block;height:100%;border-radius:999px}
    .accent{background:var(--accent)}.warn{background:var(--warn)}.danger{background:var(--danger)}.safe{background:var(--safe)}.neutral{background:#475569}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin:18px 0}.panel{padding:16px}.panel h2{font-size:16px;margin:0 0 12px;color:var(--ink)}
    .rank-list{list-style:none;margin:0;padding:0}.rank-list li{border-top:1px solid #edf1f5;padding:10px 0}.rank-list li:first-child{border-top:0}.rank-list div{display:flex;justify-content:space-between;gap:12px;font-size:14px}.rank-list span{overflow-wrap:anywhere}.rank-list b{font-variant-numeric:tabular-nums;color:var(--ink)}
    h2.section-title{font-size:19px;margin:30px 0 6px;color:var(--ink)}.note{color:var(--muted);margin:0 0 10px;font-size:14px}.danger-text{color:#9a3412}
    .table-wrap{overflow:auto;border:1px solid var(--line);border-radius:8px;background:white;margin:12px 0 28px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
    table{width:100%;border-collapse:collapse;min-width:900px}th,td{border-bottom:1px solid #e7ecf1;padding:10px 11px;text-align:left;vertical-align:top;font-size:13px;line-height:1.35}
    th{background:#edf2f7;color:#334155;position:sticky;top:0;z-index:1;font-size:12px;text-transform:uppercase;letter-spacing:.04em}tr:hover td{background:#fafcff}
    .pill{display:inline-block;background:#eef2ff;color:#3730a3;border:1px solid #c7d2fe;border-radius:999px;padding:2px 7px;margin:1px 3px 1px 0;font-size:12px;white-space:nowrap}
    .score,.status{display:inline-flex;align-items:center;justify-content:center;min-width:40px;border-radius:999px;padding:3px 8px;font-weight:700;font-variant-numeric:tabular-nums}.score.high{background:#fee2e2;color:#991b1b}.score.medium{background:#fef3c7;color:#92400e}.score.low{background:#dcfce7;color:#166534}.status.yes{background:#e0f2fe;color:#075985}.status.no{background:#f1f5f9;color:#475569}
    a{color:#1d4ed8;text-decoration:none;overflow-wrap:anywhere}a:hover{text-decoration:underline}.muted-count{color:#64748b;font-size:12px}.priority table{min-width:760px}
    @media(max-width:720px){header,main{padding-left:16px;padding-right:16px}.metric span{font-size:28px}table{min-width:760px}}
    """
    path.write_text(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Gmail Sorter Dashboard</title><style>{css}</style></head>
<body><header><h1>Gmail Sorter Dashboard</h1><div class="meta">Query: {esc(args.query)}<br>Stage: {esc(args.stage)} | Apply: {esc(args.apply)} | Messages: {total}</div></header>
<main>
<div class="cards">{cards_html}</div>
<div class="grid">
<section class="panel"><h2>Review Queues</h2>{review_html}</section>
<section class="panel"><h2>Categories</h2>{category_html}</section>
<section class="panel"><h2>Noisy Senders</h2>{sender_html}</section>
<section class="panel"><h2>Action Preview</h2>{action_html}</section>
</div>
<h2 class="section-title">Top Sender Bulk Preview</h2>
<p class="note">Shows the impact of archive/trash decisions by sender domain before you apply a stage.</p>
{render_bulk_preview(decisions, 50)}
<h2 class="section-title danger-text">Trash Summary By Domain</h2>
<p class="note">Domain-level view of exactly what the trash stage would touch. Use this to spot a sender that needs allowlisting before applying.</p>
{render_trash_domain_summary(decisions, 50)}
<h2 class="section-title">Decision Reason Summary</h2>
<div class="grid">
<section class="panel"><h2>Positive Reasons</h2>{render_reason_summary(decisions, "reasons", 20)}</section>
<section class="panel"><h2>Protection / Negative Reasons</h2>{render_reason_summary(decisions, "negative_reasons", 20)}</section>
</div>
<h2 class="section-title">Priority Folder Review</h2>
<p class="note">Immigration, studies, and real attachment mail are protected and labeled for higher-priority review.</p>
{render_table(priority_items, ["date", "sender_domain", "registered_domain", "subject", "categories", "attachment_names", "message_size_estimate"], 150)}
<h2 class="section-title danger-text">Trash / Thread Review</h2>
<p class="note">Review these before running the trash stage. Mixed threads and protected items are kept out of trash actions.</p>
{render_table(trash_items, ["ad_confidence", "perfect_ad_match", "review_priority", "protected", "sender_domain", "subject", "reasons", "negative_reasons", "planned_actions"], 100)}
<h2 class="section-title danger-text">Pre-2020 Aggressive Trash Candidates</h2>
<p class="note">Older promotional mail uses a lower trash threshold and stronger age boost, while still respecting protected messages and mixed-thread protection.</p>
{render_table(pre_2020_trash_items, ["date", "ad_confidence", "perfect_ad_match", "protected", "sender_domain", "subject", "reasons", "negative_reasons", "planned_actions"], 100)}
<h2 class="section-title">Protected Ad Review</h2>
{render_table(protected_ads, ["ad_confidence", "sender_domain", "subject", "categories", "negative_reasons"], 100)}
<h2 class="section-title">Attachment Review</h2>
{render_table(attachment_items, ["date", "sender_domain", "subject", "categories", "ad_confidence", "attachment_names", "attachment_mime_types"], 100)}
<h2 class="section-title">Header Unsubscribe Domains</h2>
<p class="note">Separate section for List-Unsubscribe headers, grouped by sender domain and prioritized by ad confidence, volume, and last-seen date.</p>
{render_unsubscribe_priority(unsubscribe_items, 20)}
{render_unsubscribable_domains(unsubscribe_items, 100)}
<h2 class="section-title">Body Unsubscribe Links</h2>
<p class="note">Separate section for unsubscribe links scrubbed from message body text. The report stores only normalized links, not email body content.</p>
{render_body_unsubscribe_links(body_unsubscribe_items, 100)}
<h2 class="section-title">Header Unsubscribe Candidates</h2>
{render_table(unsubscribe_items, ["sender_domain", "sender", "subject", "ad_confidence", "list_unsubscribe"], 100)}
<h2 class="section-title">Recent Sample</h2>
{render_table(decisions, ["date", "ad_confidence", "review_priority", "sender_domain", "subject", "categories", "planned_actions"], 100)}
</main></body></html>""",
        encoding="utf-8",
    )


def write_yearly_dashboards(out_prefix: Path, decisions: list[Decision], args: argparse.Namespace) -> list[Path]:
    grouped: dict[str, list[Decision]] = defaultdict(list)
    for item in decisions:
        year = item.date[:4] if item.date else "unknown"
        grouped[year].append(item)
    paths = []
    for year, year_items in sorted(grouped.items(), reverse=True):
        yearly_args = argparse.Namespace(**vars(args))
        yearly_args.query = f"{args.query} | year:{year}"
        yearly_args.resume = False
        path = out_prefix.with_name(f"{out_prefix.name}_{year}").with_suffix(".html")
        write_dashboard(path, year_items, yearly_args)
        paths.append(path)
    return paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Categorize Gmail messages before December 30, 2025 with dashboard-centered review.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION} ({VERSION_CODE})")
    parser.add_argument("--credentials", default=str(PROJECT_DIR / "secrets" / "credentials.json"))
    parser.add_argument("--token-readonly", default=str(PROJECT_DIR / "secrets" / "token_sorter_readonly.json"))
    parser.add_argument("--token-modify", default=str(PROJECT_DIR / "secrets" / "token_sorter_modify.json"))
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--out-prefix", default=str(PROJECT_DIR / "reports" / "gmail_sorter_report"))
    parser.add_argument("--state-db", default=str(PROJECT_DIR / "data" / "gmail_sorter_state.sqlite"), help="SQLite state database for decisions, review state, and action ledger.")
    parser.add_argument("--disable-state-db", action="store_true", help="Skip SQLite state updates and use JSON/report outputs only.")
    parser.add_argument("--maintenance-days", type=int, default=0, help="Scan only recent non-trash mail from the last N days.")
    parser.add_argument("--since-date", default="", help="Scan non-trash mail after YYYY-MM-DD.")
    parser.add_argument("--allowlist", default=str(PROJECT_DIR / "config" / "allowlist.txt"))
    parser.add_argument("--blocklist", default=str(PROJECT_DIR / "config" / "blocklist.txt"))
    parser.add_argument("--max-messages", type=int, default=None)
    parser.add_argument("--ad-threshold", type=int, default=65)
    parser.add_argument("--trash-threshold", type=int, default=90)
    parser.add_argument("--pre-2020-trash-threshold", type=int, default=75)
    parser.add_argument("--stage", choices=["classify", "label", "archive", "trash"], default="classify")
    parser.add_argument("--workers", type=int, default=8, help="Parallel read/classification workers. Writes remain sequential.")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-sleep", type=float, default=5.0)
    parser.add_argument("--http-timeout", type=float, default=120.0, help="Socket timeout in seconds for Gmail API calls.")
    parser.add_argument("--apply-progress-every", type=int, default=100, help="Print apply-stage progress every N trash calls or batch modifies.")
    parser.add_argument("--resume", action="store_true", help="Reuse and update the progress JSON for interrupted scans.")
    parser.add_argument("--refresh-existing", action="store_true", help="Refresh all cached decisions even when --resume is used.")
    parser.add_argument("--refresh-after-days", type=int, default=7, help="Refresh cached decisions older than this many days.")
    parser.add_argument("--progress-file", default=str(PROJECT_DIR / "data" / "gmail_sorter_progress.json"))
    parser.add_argument("--save-every", type=int, default=250)
    parser.add_argument("--thread-check-limit", type=int, default=500)
    parser.add_argument("--manifest-dir", default=str(PROJECT_DIR / "manifests"))
    parser.add_argument("--manifest", default="", help="Optional reviewed manifest JSON to restrict an apply stage.")
    parser.add_argument("--review-dir", default="", help="Directory for review workflow CSV/JSON outputs. Defaults beside manifest-dir.")
    parser.add_argument("--max-trash-per-domain", type=int, default=0, help="Cap planned trash actions per registered domain; 0 disables the cap.")
    parser.add_argument("--max-trash-total", type=int, default=0, help="Cap total planned trash actions; 0 disables the cap.")
    parser.add_argument("--canary-limit", type=int, default=0, help="When applying trash, only keep the first N trash actions in the apply set.")
    parser.add_argument("--attachment-details", action="store_true", help="Fetch metadata-rich payloads for attachment names/types, not attachment bytes.")
    parser.add_argument("--apply", action="store_true", help="Actually modify Gmail for the selected stage.")
    parser.add_argument("--trash-obvious-ads", action="store_true", help="Allow trash actions during --stage trash.")
    parser.add_argument("--i-understand-trash", action="store_true", help="Required with --apply --stage trash --trash-obvious-ads.")
    parser.add_argument("--open-browser", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.http_timeout > 0:
        socket.setdefaulttimeout(args.http_timeout)
    args.apply_progress_every = max(1, args.apply_progress_every)
    if args.maintenance_days:
        args.query = f"newer_than:{args.maintenance_days}d -in:trash"
    if args.since_date:
        try:
            since = datetime.fromisoformat(args.since_date).strftime("%Y/%m/%d")
        except ValueError:
            print("--since-date must be in YYYY-MM-DD format.", file=sys.stderr)
            return 2
        args.query = f"after:{since} -in:trash"
    allowlist = Path(args.allowlist)
    blocklist = Path(args.blocklist)
    ensure_default_config_files(allowlist, blocklist)
    config = load_config(allowlist, blocklist)

    if args.apply and args.stage == "classify":
        print("--apply has no effect with --stage classify.", file=sys.stderr)
        return 2
    if args.apply and args.stage == "trash" and (not args.trash_obvious_ads or not args.i_understand_trash):
        print("Refusing trash stage without --trash-obvious-ads --i-understand-trash.", file=sys.stderr)
        return 2

    state_conn = None if args.disable_state_db else open_state_db(Path(args.state_db))

    google_libs = load_google_libraries()
    _, _, _, build, HttpError = google_libs
    scopes = [MODIFY_SCOPE] if args.apply else [READONLY_SCOPE]
    token_path = Path(args.token_modify if args.apply else args.token_readonly).expanduser()
    creds = get_credentials(Path(args.credentials).expanduser(), token_path, scopes, args.open_browser, google_libs)
    service = build_gmail_service(build, creds, args)
    list_throttle = AdaptiveThrottle(args.sleep)

    try:
        message_ids = list_message_ids(service, args.query, args.max_messages, args.retries, args.retry_sleep, list_throttle)
    except HttpError as error:
        print(f"Failed to list messages: {error}", file=sys.stderr)
        if state_conn is not None:
            state_conn.close()
        return 1

    progress_path = Path(args.progress_file)
    progress = load_progress(progress_path) if args.resume else {}
    print(f"Scanning {len(message_ids)} messages matching query: {args.query} with workers={max(1, args.workers)}")
    progress = scan_messages(message_ids, progress, creds, build, args, config)
    save_progress(progress_path, progress)

    decisions = decisions_for_current_query(progress, message_ids)
    upsert_state_decisions(state_conn, decisions)
    stale_progress_count = len(progress) - len(decisions)
    if stale_progress_count > 0:
        print(f"Ignoring {stale_progress_count} cached decisions outside the current query/report set.")
    if args.stage == "trash":
        protect_mixed_threads(service, decisions, args.retries, args.retry_sleep, args.thread_check_limit)

    if args.manifest:
        manifest_ids = load_manifest_ids(Path(args.manifest))
        decisions = [item for item in decisions if item.message_id in manifest_ids]
        print(f"Restricted apply/report set to {len(decisions)} messages from manifest: {args.manifest}")

    if args.stage == "trash":
        apply_trash_policy_caps(decisions, args)
        upsert_state_decisions(state_conn, decisions)

    if args.apply and args.stage in {"label", "archive", "trash"} and decisions:
        action_count = sum(
            1
            for item in decisions
            if (
                (args.stage == "trash" and "trash" in item.planned_actions)
                or (args.stage == "archive" and "archive" in item.planned_actions)
                or (args.stage == "label" and any(action.startswith("label:") for action in item.planned_actions))
            )
        )
        print(f"Applying stage={args.stage} to {action_count}/{len(decisions)} messages with planned {args.stage} actions...", flush=True)
        apply_decisions(service, decisions, args, state_conn)
        for item in decisions:
            progress[item.message_id] = item
        save_progress(progress_path, progress)
        upsert_state_decisions(state_conn, decisions)

    decisions.sort(key=lambda item: (item.ad_confidence, item.date, item.sender_domain), reverse=True)
    out_prefix = Path(args.out_prefix)
    write_csv(out_prefix.with_suffix(".csv"), decisions)
    write_json(out_prefix.with_suffix(".json"), decisions)
    write_sender_report(out_prefix.with_name(out_prefix.name + "_senders.csv"), decisions)
    write_storage_report(out_prefix.with_name(out_prefix.name + "_storage.csv"), decisions)
    write_unsubscribe_report(out_prefix.with_name(out_prefix.name + "_unsubscribe.csv"), decisions)
    write_action_manifests(Path(args.manifest_dir), decisions)
    review_dir = Path(args.review_dir) if args.review_dir else Path(args.manifest_dir) / "review"
    write_review_workflow(review_dir, decisions)
    write_dashboard(out_prefix.with_suffix(".html"), decisions, args)
    yearly_dashboards = write_yearly_dashboards(out_prefix, decisions, args)

    ads = sum(1 for item in decisions if "Ads Promotions" in item.categories)
    trash = sum(1 for item in decisions if "trash" in item.planned_actions)
    protected = sum(1 for item in decisions if item.protected)
    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"{mode}: processed {len(decisions)} messages; ads/promotions={ads}; planned trash={trash}; protected={protected}.")
    print(f"Wrote {out_prefix.with_suffix('.html')}")
    print(f"Wrote {out_prefix.with_suffix('.csv')}")
    print(f"Wrote {out_prefix.with_suffix('.json')}")
    print(f"Wrote {out_prefix.with_name(out_prefix.name + '_senders.csv')}")
    print(f"Wrote {out_prefix.with_name(out_prefix.name + '_storage.csv')}")
    print(f"Wrote {out_prefix.with_name(out_prefix.name + '_unsubscribe.csv')}")
    print(f"Wrote {len(yearly_dashboards)} yearly dashboards")
    print(f"Wrote manifests in {Path(args.manifest_dir)}")
    print(f"Wrote review workflow in {review_dir}")
    if state_conn is not None:
        state_conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
