#!/usr/bin/env python3
"""Stage-based Gmail sorter for mail before December 30, 2025.

Default mode is a dry-run classification pass. The HTML dashboard is the main
review surface; use it before running any --apply stage.

The safety model is intentionally boring: scan first, write reports/manifests,
then require explicit flags before Gmail is changed. The code is split around
that same idea so a new reader can trace each phase without guessing where the
destructive operations happen.
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import logging
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

# Policy data and pure keyword matching live in the sorter package so the
# cleanup rules can be edited and config-driven without touching Gmail I/O or
# apply paths. These names are re-exported here for backwards compatibility with
# the companion scripts (trash_rescue_audit, apply_domain_trash_policy) and the
# tests, which all do `import gmail_sorter`.
from sorter import policy
from sorter.keywords import compile_keywords, contains_any, keyword_hits, regex_hits
from sorter.config_loader import apply_overrides, load_policy_overrides
from sorter.embeddings import compute_embedding_scores, cosine_similarity
from sorter.schema import CURRENT_SCHEMA_VERSION, migrate


# Re-export policy names for backwards compatibility.
READONLY_SCOPE = policy.READONLY_SCOPE
MODIFY_SCOPE = policy.MODIFY_SCOPE
MAIL_SCOPE = policy.MAIL_SCOPE
DEFAULT_QUERY = policy.DEFAULT_QUERY
ROOT_LABEL = policy.ROOT_LABEL
NON_LABEL_CATEGORIES = policy.NON_LABEL_CATEGORIES
BULK_MAIL_REASONS = policy.BULK_MAIL_REASONS
AD_SUBJECT_KEYWORDS = policy.AD_SUBJECT_KEYWORDS
AD_BODY_KEYWORDS = policy.AD_BODY_KEYWORDS
AD_SENDER_KEYWORDS = policy.AD_SENDER_KEYWORDS
STRONG_PROMO_SUBJECT_PATTERNS = policy.STRONG_PROMO_SUBJECT_PATTERNS
PROMO_SENDER_LOCALPARTS = policy.PROMO_SENDER_LOCALPARTS
TRANSACTIONAL_KEYWORDS = policy.TRANSACTIONAL_KEYWORDS
IMPORTANT_LABELS = policy.IMPORTANT_LABELS
PROTECTED_CATEGORIES = policy.PROTECTED_CATEGORIES
IMMIGRATION_KEYWORDS = policy.IMMIGRATION_KEYWORDS
STUDIES_KEYWORDS = policy.STUDIES_KEYWORDS
CATEGORY_RULES = policy.CATEGORY_RULES
PRIMARY_CATEGORY_PRECEDENCE = policy.PRIMARY_CATEGORY_PRECEDENCE

PROJECT_DIR = Path(__file__).resolve().parents[1]
APP_VERSION = "0.8.1"
VERSION_CODE = "20260706"
SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
log = logging.getLogger("sorter")


# v0.6: AI review packet body excerpt cap (smaller than the centroid text
# cap so a single AI packet is small enough to ship in a bounded prompt).
AI_REVIEW_BODY_EXCERPT_CHARS = 1200
# v0.7: privacy-bounded cleaned body excerpt persisted to
# message_features.body_text_excerpt. Used by update_category_centroids as
# the embed text for centroid learning. The cap keeps the local cache from
# growing unbounded on a multi-year mailbox.
BODY_EXCERPT_FOR_FEATURES = policy.BODY_EXCERPT_FOR_FEATURES





@dataclass
class Config:
    """User-maintained allow/block lists, split into domains and exact senders."""

    allow_domains: set[str] = field(default_factory=set)
    block_domains: set[str] = field(default_factory=set)
    allow_senders: set[str] = field(default_factory=set)
    block_senders: set[str] = field(default_factory=set)


@dataclass
class Decision:
    """One normalized decision row used by reports, manifests, and state.

    The dataclass is deliberately wider than a pure action object because the
    dashboard needs to explain why a message was protected, archived, or queued
    for Trash review.
    """

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
    primary_category: str = ""
    category_confidence: dict[str, int] = field(default_factory=dict)
    ad_confidence: int = 0
    reasons: list[str] = field(default_factory=list)
    negative_reasons: list[str] = field(default_factory=list)
    planned_actions: list[str] = field(default_factory=list)
    archive_reason: str = ""
    has_attachment: bool = False
    has_real_attachment: bool = False
    attachment_count: int = 0
    inline_attachment_count: int = 0
    message_size_estimate: int = 0
    body_len: int = 0
    body_category_hits: list[str] = field(default_factory=list)
    body_text_excerpt: str = ""
    detected_language: str = ""
    list_unsubscribe: str = ""
    body_unsubscribe_links: list[str] = field(default_factory=list)
    attachment_names: list[str] = field(default_factory=list)
    attachment_mime_types: list[str] = field(default_factory=list)
    protected: bool = False
    perfect_ad_match: bool = False
    review_priority: str = "normal"
    action_done: str = "no"
    scanned_at: str = ""
    schema_version: int = SCHEMA_VERSION


class AdaptiveThrottle:
    """Small shared throttle for Gmail API pressure.

    Gmail rate limits tend to arrive in bursts. Workers share this object so a
    retryable error slows the whole scan down instead of letting every thread
    keep hammering the API independently.
    """

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
    """Import Google dependencies lazily so `--help` and tests stay lightweight."""

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
    """Load or create the OAuth token for exactly the scope requested by caller."""

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
    """Build a Gmail client and attach the configured HTTP timeout when possible."""

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
    """Read the human-editable allow/block files into fast lookup sets."""

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
    """Run one Gmail request with retry/backoff only for errors worth retrying."""

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


def collect_body_text(payload: dict[str, Any], max_chars: int = 250_000, use_html_body: bool = True) -> str:
    """Walk a Gmail message payload and return the decoded body text.

    v0.8: when ``use_html_body`` is True (default), HTML parts are
    converted to structured text (style/script stripped, tables as
    tab-separated rows) so receipt line items and the like survive
    into the cleaned body. When False, the function falls back to the
    pre-v0.8 simple collector (each part decoded verbatim, joined
    with newlines).
    """

    if use_html_body:
        from sorter.html_body import html_to_structured_text

    chunks: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        if sum(len(chunk) for chunk in chunks) >= max_chars:
            return
        mime_type = (part.get("mimeType") or "").lower()
        filename = part.get("filename") or ""
        body = part.get("body", {})
        data = body.get("data", "")
        if data and not filename and mime_type in {"text/plain", "text/html"}:
            decoded = decode_payload_text(data)
            if mime_type == "text/html" and use_html_body:
                decoded = html_to_structured_text(decoded)
            chunks.append(decoded)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    return "\n".join(chunks)[:max_chars]


# Footer/signature markers that carry no classification signal. A line matching
# these starts a footer block that is dropped before category matching so a
# promotional footer under a real reply does not flip the message into Ads.
FOOTER_MARKERS = [
    "unsubscribe",
    "manage preferences",
    "email preferences",
    "you are receiving this",
    "sent from my iphone",
    "sent from my ipad",
    "sent from my android",
    "sent from my blackberry",
    "get outlook for android",
    "get outlook for ios",
    "this email and any attachments",
    "confidentiality notice",
    "disclaimer:",
    "regards,",
    "-- ",
    "_",
]

# A quoted-reply line in plain text starts with one or more '>'. A top forwarded
# block usually starts with "----- Original Message -----" or "On ... wrote:".
_QUOTED_LINE_RE = re.compile(r"^\s*>+")
_FORWARD_HEADER_RE = re.compile(r"^\s*-{2,}\s*(Original Message|Forwarded message)\s*-{2,}", re.IGNORECASE)
_ON_WROTE_RE = re.compile(r"^\s*on .+wrote:\s*$", re.IGNORECASE)


def clean_body_text(body_text: str, keep_chars: int = 8000) -> str:
    """Strip quoted reply chains, forwarded blocks, and footer signatures.

    The cleaned text is what the category rules see, so a reply that quotes a
    promotional email is not misclassified as promo, and a long unsubscribe
    footer under a real message does not dominate the body. Unsubscribe link
    extraction still uses the raw body, so footer URLs are not lost.
    """

    if not body_text:
        return ""
    lines = body_text.splitlines()
    kept: list[str] = []
    in_forward_block = False
    for line in lines:
        if _FORWARD_HEADER_RE.match(line):
            in_forward_block = True
            continue
        if in_forward_block:
            # A forwarded block ends at the first blank line after headers, but
            # to be safe we drop until we leave the quoted region entirely.
            if _QUOTED_LINE_RE.match(line) or _ON_WROTE_RE.match(line):
                continue
            in_forward_block = False
        if _QUOTED_LINE_RE.match(line) or _ON_WROTE_RE.match(line):
            continue
        # Once we hit a footer marker, drop the rest of this block.
        # The check is rstrip-aware: a line of just ``--`` (the
        # post-strip form of the standard email signature separator
        # ``-- \n``) must still match the marker ``-- ``. The rstrip
        # # normalization lets both the bare ``--`` and the more
        # # verbose ``-- `` (with trailing whitespace) trigger the
        # # break.
        lowered_raw = line.lower()
        lowered = line.strip().lower()
        for marker in FOOTER_MARKERS:
            marker_stripped = marker.rstrip()
            if not marker_stripped:
                continue
            if lowered.startswith(marker) or lowered == marker:
                break
            if marker_stripped and (lowered == marker_stripped or lowered_raw.lstrip().startswith(marker_stripped + " ") or lowered_raw.lstrip() == marker_stripped):
                break
        else:
            kept.append(line)
            continue
        break
    cleaned = "\n".join(kept).strip()
    return cleaned[:keep_chars]


def find_unsubscribe_links_in_text(body_text: str, limit: int = 20) -> list[str]:
    """Scrub unsubscribe/preference URLs out of already-decoded body text.

    Kept separate from payload walking so a caller that already collected body
    text (for categorization) can reuse it instead of decoding the payload a
    second time.
    """

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


def extract_body_unsubscribe_links(payload: dict[str, Any], limit: int = 20) -> list[str]:
    # Keep reports privacy-light: inspect transient body text, persist only scrubbed unsubscribe targets.
    body_text = html.unescape(collect_body_text(payload))
    return find_unsubscribe_links_in_text(body_text, limit)


def payload_has_attachment(payload: dict[str, Any]) -> bool:
    filename = payload.get("filename") or ""
    body = payload.get("body", {})
    if filename or body.get("attachmentId"):
        return True
    return any(payload_has_attachment(part) for part in payload.get("parts", []) or [])


def payload_headers(payload: dict[str, Any]) -> dict[str, str]:
    return header_map(payload)


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


def _date_le(message_date: str, cutoff_iso: str) -> bool:
    """Return True when message_date (YYYY-MM-DD) is on or before cutoff."""

    try:
        return message_date[:10] <= cutoff_iso[:10]
    except Exception:
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
    """Score promotional likelihood and keep the positive/negative evidence.

    The score is capped at 100, but the evidence lists matter more than the
    number. Reports and rescue audits use those lists to explain the decision.
    """

    searchable = " ".join([sender, sender_domain, subject, snippet])
    score = 0
    reasons: list[str] = []
    negative_reasons: list[str] = []

    # User blocklist entries are strong signals, but still do not bypass the
    # later protection checks for attachments, important labels, or priority
    # categories.
    if sender_domain in config.block_domains:
        score += 60
        reasons.append("blocklisted_domain")
    sender_email, _ = parse_sender(sender)
    if sender_email in config.block_senders:
        score += 60
        reasons.append("blocklisted_sender")
    # Allowlist entries are intentionally heavy because a false positive here is
    # worse than leaving some junk visible.
    if sender_domain in config.allow_domains or sender_email in config.allow_senders:
        score -= 100
        negative_reasons.append("allowlisted_sender_or_domain")

    if "CATEGORY_PROMOTIONS" in labels:
        score += 50
        reasons.append("gmail_category_promotions")
    # Bulk-mail headers are valuable because they come from the mail transport,
    # not just marketing copy in a subject line.
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
        ("sender", keyword_hits(sender, AD_SENDER_KEYWORDS), 8, 25),
        ("subject", keyword_hits(subject, AD_SUBJECT_KEYWORDS), 10, 35),
        ("snippet", keyword_hits(snippet, AD_BODY_KEYWORDS), 12, 30),
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

    # Transactional words pull the score down. A receipt or account alert from a
    # promotional sender is still a durable record.
    negative_hits = keyword_hits(searchable, TRANSACTIONAL_KEYWORDS)
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
        len(keyword_hits(subject, AD_SUBJECT_KEYWORDS))
        + len(regex_hits(subject, STRONG_PROMO_SUBJECT_PATTERNS))
        + len(keyword_hits(snippet, AD_BODY_KEYWORDS))
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


def categorize_with_confidence(searchable: str, labels: list[str], ad_confidence: int, sender_profile_cats: dict[str, int] | None = None, subject: str = "", body_text: str = "", sender_text: str = "") -> dict[str, int]:
    """Return {category: confidence 0-100} from evidence.

    Confidence is the policy input that lets the relabel/label stages apply only
    meaningful categories instead of every keyword that happened to hit. A
    category that matches only one weak keyword scores low and can be dropped by
    a --label-confidence floor, while a protected bucket that matches several
    keywords plus a sender profile scores high and is kept.

    Scoring model (intentionally simple and explainable):
      - subject keyword hits: 30 each (strong signal — the sender chose these words)
      - body keyword hits: 20 each (weaker — body text is longer and noisier)
      - sender/domain keyword hits: 15 each (sender metadata)
      - keyword family capped at 75
      - a Gmail CATEGORY_* label for the bucket adds 30
      - a sender-profile hit adds the learned weight (capped at 25)
      - Ads Promotions / Newsletters Bulk derive their confidence from the ad
        confidence / promotions label directly

    subject, body_text, and sender_text are separate so the scorer can weight a
    keyword hit in the subject line higher than one buried in the body. When
    they are empty, the combined ``searchable`` string is used as a fallback so
    older callers still work.
    """

    categories: dict[str, int] = {}
    if ad_confidence >= 65:
        categories["Ads Promotions"] = ad_confidence
    elif "CATEGORY_PROMOTIONS" in labels:
        categories["Newsletters Bulk"] = 60

    # Map Gmail CATEGORY_* labels to sorter categories for a confidence boost.
    gmail_category_boost = {
        "CATEGORY_UPDATES": {"Account Security", "Finance", "Receipts Orders", "Utilities", "Subscriptions"},
        "CATEGORY_SOCIAL": {"Social"},
        "CATEGORY_FORUMS": {"Forums"},
    }

    profile_cats = sender_profile_cats or {}
    # Use the separated fields when available; fall back to the combined
    # searchable string for older callers that don't pass them.
    subject_field = subject if subject else searchable
    body_field = body_text if body_text else ""
    sender_field = sender_text if sender_text else searchable
    for name, keywords, exclusions in CATEGORY_RULES:
        if exclusions and keyword_hits(searchable, exclusions):
            continue
        # Score keyword hits by where they appear: subject > body > sender.
        subject_hits = keyword_hits(subject_field, keywords)
        body_hits = keyword_hits(body_field, keywords) if body_field else []
        sender_hits = keyword_hits(sender_field, keywords) if sender_field else []
        # A keyword that appears in both subject and body counts once, at the
        # higher (subject) weight.
        all_hits = list(dict.fromkeys(subject_hits + body_hits + sender_hits))
        if not all_hits:
            continue
        body_only = [h for h in body_hits if h not in subject_hits]
        sender_only = [h for h in sender_hits if h not in subject_hits and h not in body_hits]
        keyword_score = min(75, 30 * len(subject_hits) + 20 * len(body_only) + 15 * len(sender_only))
        category_label_score = 0
        for gmail_label, buckets in gmail_category_boost.items():
            if gmail_label in labels and name in buckets:
                category_label_score = 30
                break
        profile_score = min(25, profile_cats.get(name, 0))
        categories[name] = min(100, keyword_score + category_label_score + profile_score)

    if "CATEGORY_SOCIAL" in labels and "Social" not in categories:
        categories["Social"] = 60
    if "CATEGORY_FORUMS" in labels:
        categories["Forums"] = max(categories.get("Forums", 0), 60)
    if "CATEGORY_UPDATES" in labels and not categories:
        categories["Updates"] = 50
    if not categories:
        categories["Review"] = 30
    return categories


def categorize(searchable: str, labels: list[str], ad_confidence: int) -> list[str]:
    """Assign dashboard labels from evidence; this does not apply Gmail labels.

    Kept as a thin wrapper over :func:`categorize_with_confidence` for callers
    that only want the category names.
    """

    return sorted(categorize_with_confidence(searchable, labels, ad_confidence).keys())


# Precedence used to choose a single primary category is imported from
# sorter.policy (PRIMARY_CATEGORY_PRECEDENCE) so it can be config-driven.


def pick_primary_category(categories: list[str]) -> str:
    """Choose one primary category by precedence for cleaner single-label filing."""

    for name in PRIMARY_CATEGORY_PRECEDENCE:
        if name in categories:
            return name
    return categories[0] if categories else "Review"


def labelable_categories(categories: list[str]) -> list[str]:
    """Drop catch-all buckets so generic mail is not tagged Sorter/Review."""

    filtered = [category for category in categories if category not in NON_LABEL_CATEGORIES]
    return filtered or []


def decide(message: dict[str, Any], args: argparse.Namespace, config: Config) -> Decision:
    """Turn one Gmail message payload into a conservative action plan."""

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
    # Body-aware scanning: when the payload is fetched with format=full
    # (--scan full), include a bounded slice of the decoded body in the text the
    # category rules see. This lets categorization use the email body, header,
    # footer, and unsubscribe evidence instead of only the subject and snippet.
    # Ad confidence is intentionally still scored on headers+subject+snippet so
    # a long promotional body does not inflate the trash score.
    body_text = html.unescape(collect_body_text(payload))
    body_len = len(body_text)
    category_searchable = searchable
    body_included = bool(body_len) and getattr(args, "scan", "metadata") == "full"
    cached_body: dict[str, Any] = {}
    if not body_included and getattr(args, "scan", "metadata") == "full":
        # No fresh body in this payload (metadata-only fetch used the cache).
        # Reuse the cached body category hits so categorization is still
        # body-aware without a re-fetch.
        cached_index = getattr(args, "cached_body_features", {}) or {}
        cached_body = cached_index.get(message.get("id", ""), {})
        if cached_body:
            body_len = cached_body.get("body_len", 0)
            body_included = True
    # Compute the cleaned body excerpt once and reuse it. The excerpt is what
    # v0.7 stores in message_features.body_text_excerpt so the next scan can
    # embed the real body text without re-fetching from Gmail. The excerpt is
    # bounded to BODY_EXCERPT_FOR_FEATURES (4000 chars) and uses the same
    # quote/footer stripping as categorization, so replies quoting a promo
    # email do not leak the promo's body into the cached excerpt.
    body_excerpt_for_features = ""
    if body_included and body_text:
        body_excerpt_for_features = clean_body_text(body_text, keep_chars=BODY_EXCERPT_FOR_FEATURES)
    ad_confidence, reasons, negative_reasons = score_ad(headers, labels, sender, sender_domain, subject, snippet, config)
    if body_included:
        # Use the cleaned body (quotes/footers stripped) for categorization so a
        # reply that quotes a promo email is not misclassified. Unsubscribe link
        # extraction below still uses the raw body_text so footer URLs survive.
        if body_text:
            category_searchable = f"{searchable}\n{clean_body_text(body_text)}"
            reasons.append(f"body_included:{body_len}")
        elif cached_body:
            for hit_cat in cached_body.get("body_category_hits", []):
                reasons.append(f"cached_body:{hit_cat}")
    body_hit_categories = sorted(body_category_hits(body_text).keys()) if body_included and body_text else (cached_body.get("body_category_hits", []) if cached_body else [])
    age_boost = age_score_boost(message_date)
    if age_boost and ad_confidence >= args.ad_threshold:
        ad_confidence = min(100, ad_confidence + age_boost)
        reasons.append(f"older_mail_boost:{age_boost}")
    # Sender history feeds both categorization confidence (a learned weight
    # boosts a matched category) and a fallback that surfaces a category the
    # subject keywords missed entirely.
    profile_cats: dict[str, int] = {}
    if getattr(args, "use_sender_profiles", True):
        profile_index = getattr(args, "sender_profiles", {}) or {}
        profile_cats = sender_profile_categories_from_index(profile_index, sender_email, registered_domain)
    # v0.7: detect the message language once. The detector picks which
    # language-specific keyword overlay (config/policy.<lang>.yaml) should be
    # applied at the categorization step. It is never used to gate mail or to
    # change the protection decision. On a cache-only scan the fresh body is
    # empty so we fall back to the cached excerpt.
    from sorter.lang import detect as detect_language
    lang_source = f"{subject} {body_excerpt_for_features}"
    if not lang_source.strip() and cached_body:
        lang_source = f"{subject} {cached_body.get('body_text_excerpt', '')}"
    detected_language = detect_language(lang_source)
    # v0.7: apply the per-language keyword overlay (FR/FA) when one is
    # configured and the detector picked a non-English language. The overlay
    # extends or replaces the matching category's keyword list, and is
    # restored at the end of decide() so the next message — possibly in a
    # different language — starts from a clean policy state.
    from sorter.config_loader import activate_language_overlay, restore_policy
    overlay_token: dict | None = None
    if detected_language and detected_language != "en":
        overlay_dir = getattr(args, "_policy_config_dir", None)
        if overlay_dir is not None:
            from pathlib import Path
            from sorter.config_loader import load_language_overlay
            overlay = load_language_overlay(Path(overlay_dir), detected_language)
            if overlay:
                overlay_token = activate_language_overlay(overlay)
    try:
        category_confidence = categorize_with_confidence(
            category_searchable, labels, ad_confidence, profile_cats,
            subject=subject,
            body_text=clean_body_text(body_text) if body_included else "",
            sender_text=f"{sender} {sender_domain}",
        )
        # v0.8: per-keyword learned weights. When the user has enabled
        # --use-learned-weights and the trained model is loaded, blend
        # the learned score in via max(keyword, learned) for every
        # category. The model is the data-driven ceiling, the
        # keyword rules are the explainable floor.
        learned_weights = getattr(args, "_learned_weights", None) or {}
        if learned_weights:
            from sorter.learned_weights import apply_learned_score
            gmail_promotions = "CATEGORY_PROMOTIONS" in labels
            gmail_primary = "CATEGORY_PRIMARY" in labels
            for cat in list(category_confidence.keys()):
                weights = learned_weights.get(cat)
                if weights is None:
                    continue
                # Approximate the per-message hit counts for the
                # feature vector. We don't have the per-category
                # hit counts split by position at the categorize
                # step (they were collapsed into keyword_score), so
                # use a simple proxy: the stored confidence itself
                # is a good proxy for the total hit count, and
                # gmail_category_boost is the same +30 the
                # hand-tuned code applies.
                approx_hits = max(1, min(3, int(category_confidence[cat] / 25)))
                learned = apply_learned_score(
                    weights,
                    subject_hits=approx_hits,
                    body_hits=approx_hits,
                    sender_hits=1 if cat in (profile_cats or {}) else 0,
                    has_gmail_promotions=gmail_promotions,
                    has_gmail_primary=gmail_primary,
                    sender_profile_boost=float(profile_cats.get(cat, 0)),
                )
                if learned > category_confidence[cat]:
                    category_confidence[cat] = learned
                    if learned >= getattr(args, "label_confidence", 50):
                        reasons.append(f"learned_boost:{cat}:{learned}")
    finally:
        if overlay_token is not None:
            restore_policy(overlay_token)
    # Embedding-based semantic scoring (hybrid). When an embedding backend is
    # available and centroids have been learned, compute the cosine similarity
    # between this message's text and each category centroid. The embedding
    # score can *boost* a category the keyword rules scored low on, but never
    # *lowers* a keyword score — it's max(keyword, embedding). This catches
    # semantic matches the lexical rules miss (e.g. a bank statement with no
    # "bank" keyword still embeds close to the Finance centroid). Falls back
    # to keyword-only when no backend is available.
    embedding_backend = getattr(args, "_embedding_backend", None)
    category_centroids = getattr(args, "category_centroids", {}) or {}
    if embedding_backend and category_centroids:
        embed_text = f"{subject} {snippet}"
        if body_included and body_text:
            embed_text += f" {clean_body_text(body_text)[:2000]}"
        embed_scores = compute_embedding_scores(embed_text, category_centroids, embedding_backend)
        for cat, sim in embed_scores.items():
            if cat in NON_LABEL_CATEGORIES:
                continue
            emb_conf = int(sim * 100)
            if emb_conf > category_confidence.get(cat, 0):
                category_confidence[cat] = emb_conf
                if emb_conf >= getattr(args, "label_confidence", 50):
                    reasons.append(f"embedding_boost:{cat}:{sim:.2f}")
    # Merge body-derived category hits from a previous full scan when this run
    # used the cache (metadata-only fetch). Cached hits let categorization stay
    # body-aware without re-fetching the body.
    if body_hit_categories and cached_body:
        for hit_cat in body_hit_categories:
            if hit_cat in NON_LABEL_CATEGORIES:
                continue
            category_confidence[hit_cat] = max(category_confidence.get(hit_cat, 0), 60)
    # Merge sender-profile-only categories that the keyword rules missed.
    min_profile_weight = getattr(args, "sender_profile_min_weight", 6)
    for category, weight in profile_cats.items():
        if category in NON_LABEL_CATEGORIES or category in category_confidence:
            continue
        if weight >= min_profile_weight:
            category_confidence[category] = min(100, weight)
            reasons.append(f"sender_profile:{category}:{weight}")
    has_attachment = payload_has_attachment(payload)
    real_attachment_count, inline_attachment_count = attachment_counts(payload)
    has_real_attachment = real_attachment_count > 0
    if has_real_attachment:
        category_confidence["Priority Attachments"] = 100
    # Q1: Suppress Shopping when Ads Promotions is high-confidence — it's
    # redundant to tag a promo email with both Ads Promotions and Shopping.
    if "Ads Promotions" in category_confidence and category_confidence["Ads Promotions"] >= 65:
        if "Shopping" in category_confidence:
            del category_confidence["Shopping"]
            negative_reasons.append("shopping_suppressed_under_ads")
    # Apply the confidence floor and per-message cap. Protected/priority buckets
    # are always kept (safety first), and the primary category is always kept so
    # the dashboard's single-label view never loses its anchor.
    label_confidence = getattr(args, "label_confidence", 0)
    max_labels = getattr(args, "max_labels_per_message", 0)
    always_keep = PROTECTED_CATEGORIES | {"Ads Promotions", "Newsletters Bulk"}
    kept: list[tuple[str, int]] = []
    dropped: list[str] = []
    for name, conf in sorted(category_confidence.items(), key=lambda kv: kv[1], reverse=True):
        if name in NON_LABEL_CATEGORIES or name in always_keep or conf >= label_confidence:
            kept.append((name, conf))
        else:
            dropped.append(f"{name}:{conf}")
    if max_labels and len(kept) > max_labels:
        protected_kept = [(n, c) for n, c in kept if n in always_keep]
        optional = [(n, c) for n, c in kept if n not in always_keep]
        optional.sort(key=lambda kv: kv[1], reverse=True)
        allowed_optional = optional[: max(0, max_labels - len(protected_kept))]
        for name, conf in optional[len(allowed_optional):]:
            dropped.append(f"{name}:cap")
        kept = protected_kept + allowed_optional
    if dropped:
        negative_reasons.append(f"low_confidence_labels:{','.join(dropped)}")
    categories = sorted(name for name, _ in kept)
    category_confidence_kept = {name: conf for name, conf in kept}
    # Q3: Thread-aware labeling. If the message landed in a catch-all (Review)
    # and --use-thread-aware is on, inherit the thread's dominant category from
    # past decisions. This fixes replies in a Finance/Immigration thread that
    # have no category keywords of their own. Never overrides a real keyword
    # match or a protected category; only fills the catch-all gap.
    if getattr(args, "use_thread_aware", False) and set(categories).issubset(NON_LABEL_CATEGORIES):
        thread_map = getattr(args, "thread_dominant_categories", {}) or {}
        thread_id = message.get("threadId", "")
        dominant = thread_map.get(thread_id, "")
        if dominant and dominant not in NON_LABEL_CATEGORIES:
            categories = sorted(set(categories) | {dominant})
            category_confidence_kept[dominant] = 55
            reasons.append(f"thread_inherited:{dominant}")
    # v0.8: thread-level conversation modeling. The thread features
    # are read from args.thread_features in main(). The boost is
    # small (cap 15) and only applies to the thread's top
    # category, so a noisy thread can't blow up an unrelated
    # category.
    thread_features = getattr(args, "thread_features", {}) or {}
    thread_id = message.get("threadId", "")
    if thread_id and getattr(args, "use_thread_modeling", True):
        feature = thread_features.get(thread_id)
        if feature is not None:
            from sorter.thread_features import compute_thread_boost
            boost = compute_thread_boost(feature, feature.top_category)
            if boost > 0 and feature.top_category not in NON_LABEL_CATEGORIES:
                old_conf = category_confidence_kept.get(feature.top_category, 0)
                category_confidence_kept[feature.top_category] = max(old_conf, min(100, old_conf + boost))
                reasons.append(f"thread_model_boost:{feature.top_category}:+{boost}")
    body_unsubscribe_links = find_unsubscribe_links_in_text(body_text) if body_included else extract_body_unsubscribe_links(payload)
    attachment_names, attachment_mime_types = collect_attachment_details(payload) if has_attachment else ([], [])
    # This is the main safety gate. A protected message can still be reported
    # and labeled, but archive/trash actions are removed before apply.
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
    # Perfect ad matches are the only class designed for high-confidence Trash,
    # and even they must survive the same protection checks.
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

    primary_category = pick_primary_category(categories)
    # Only turn meaningful categories into Gmail labels. Catch-all buckets like
    # Review/Updates are kept for the dashboard but never applied, so generic
    # mail is not tagged Sorter/Review across the whole mailbox.
    planned_actions = [f"label:{category}" for category in labelable_categories(categories)]

    is_unread = "UNREAD" in labels
    recent_cutoff_days = getattr(args, "archive_min_age_days", 0)
    too_recent_to_archive = False
    if recent_cutoff_days and message_date:
        try:
            age_days = (datetime.now(timezone.utc).date() - datetime.fromisoformat(message_date).date()).days
            too_recent_to_archive = age_days < recent_cutoff_days
        except ValueError:
            too_recent_to_archive = False

    # Archive now requires independent bulk-mail evidence, not just a high ad
    # score. A one-off message that happens to score high on subject keywords is
    # no longer pulled out of the inbox. Gmail's own Newsletters/Bulk bucket is
    # accepted as sufficient bulk evidence on its own.
    bulk_signal_reasons = sorted(BULK_MAIL_REASONS.intersection(reasons))
    has_bulk_signal = bool(bulk_signal_reasons) or "Newsletters Bulk" in categories
    archive_reason = ""
    can_archive = (
        not protected
        and has_bulk_signal
        and ad_confidence >= args.archive_threshold
        and not (getattr(args, "archive_skip_unread", False) and is_unread)
        and not too_recent_to_archive
    )
    if can_archive:
        evidence = bulk_signal_reasons or (["gmail_newsletters_bulk"] if "Newsletters Bulk" in categories else [])
        archive_reason = f"ad_confidence={ad_confidence};bulk={','.join(evidence)}"
    elif not protected and ad_confidence >= args.archive_threshold and not has_bulk_signal:
        negative_reasons.append("archive_no_bulk_signal")
    elif not protected and has_bulk_signal and getattr(args, "archive_skip_unread", False) and is_unread:
        negative_reasons.append("archive_skipped_unread")
    elif not protected and has_bulk_signal and too_recent_to_archive:
        negative_reasons.append(f"archive_too_recent:{recent_cutoff_days}d")

    trash_threshold = args.pre_2020_trash_threshold if is_before_year(message_date, 2020) else args.trash_threshold
    can_trash = not protected and (
        perfect_ad_match
        or (ad_confidence >= trash_threshold and "Ads Promotions" in categories)
    )
    if is_before_year(message_date, 2020) and "Ads Promotions" in categories:
        reasons.append(f"pre_2020_trash_threshold:{trash_threshold}")
    if args.stage in {"archive", "trash"} and can_archive:
        planned_actions.append("archive")
    # Trash requires both the stage and the explicit trash flag. A normal scan
    # or archive run should never sneak a Trash action into the manifest.
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
        primary_category=primary_category,
        category_confidence=category_confidence_kept,
        ad_confidence=ad_confidence,
        reasons=reasons,
        negative_reasons=negative_reasons,
        planned_actions=planned_actions,
        archive_reason=archive_reason,
        has_attachment=has_attachment,
        has_real_attachment=has_real_attachment,
        attachment_count=real_attachment_count,
        inline_attachment_count=inline_attachment_count,
        message_size_estimate=int(message.get("sizeEstimate") or 0),
        body_len=body_len,
        body_category_hits=body_hit_categories,
        body_text_excerpt=body_excerpt_for_features,
        detected_language=detected_language,
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
    """Remove Trash actions from threads that include messages outside the plan."""

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
    """Load older progress rows while tolerating newly added dataclass fields."""

    valid = Decision.__dataclass_fields__.keys()
    if "registered_domain" not in data:
        data["registered_domain"] = registered_domain_for(data.get("sender_domain", ""))
    if "has_real_attachment" not in data:
        data["has_real_attachment"] = bool(data.get("has_attachment", False))
    data.setdefault("attachment_count", 1 if data.get("has_real_attachment") else 0)
    data.setdefault("inline_attachment_count", 0)
    data.setdefault("message_size_estimate", 0)
    data.setdefault("body_len", 0)
    data.setdefault("body_category_hits", [])
    data.setdefault("category_confidence", {})
    data.setdefault("schema_version", 0)
    if "primary_category" not in data:
        data["primary_category"] = pick_primary_category(data.get("categories", []) or [])
    data.setdefault("archive_reason", "")
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
    """Create the local SQLite state database used for resumability/auditing."""

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    migrate(conn)
    return conn


def sender_profile_key(kind: str, value: str, category: str = "") -> str:
    """Return the row key for a (kind, value, category) tuple.

    v0.7 — category is now part of the key. Pre-v0.7 the primary key was
    ``kind:value``, which meant two decisions for the same sender in
    different categories (e.g. a friend who sends both personal mail and
    event invites) collided on the row and lost precision. v0.7 includes
    the category in the key so the table supports the full one-row-per-
    (sender, category) shape that the rest of the code already assumes.
    """

    base = f"{kind}:{value.lower()}"
    if category:
        return f"{base}:{category.lower()}"
    return base


def load_sender_profile_index(
    conn: sqlite3.Connection | None,
    min_hits: int = 3,
    half_life_days: int = 180,
    now: datetime | None = None,
) -> dict[str, dict[str, int]]:
    """Precompute a sender/domain -> {category: weight} index for the scan.

    decide() runs in worker threads without DB access, so the index is built
    once before the scan and consulted via args.sender_profiles. Sender rows
    outweigh domain rows so a specific address beats a noisy domain.

    v0.7 — time decay: a profile row older than ``half_life_days`` carries
    less weight than a recent one. The decay is
    ``weight = base_hits * exp(-Δdays / half_life_days)``, which is smooth
    (no cliff) and well-behaved at the boundary. A half-life of 0 disables
    decay, preserving the pre-v0.7 flat-weight behavior. The function
    reads the v3 ``first_seen`` column; pre-v0.7 rows fall back to
    ``last_seen`` so the migration is invisible to the caller.
    """

    index: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    if conn is None:
        return {}
    if half_life_days <= 0:
        # Pre-v0.7 behavior: no decay.
        for kind, weight in (("sender", 3), ("domain", 1)):
            prefix = f"{kind}:%"
            cur = conn.execute(
                "SELECT key, category, hits FROM sender_profile WHERE key LIKE ? AND hits>=?",
                (prefix, min_hits),
            )
            for key, category, hits in cur.fetchall():
                index[key][category] += int(hits) * weight
        return {key: {cat: int(weight) for cat, weight in cats.items()} for key, cats in index.items()}

    now = now or datetime.now(timezone.utc)
    for kind, weight in (("sender", 3), ("domain", 1)):
        # v0.7: keys are (kind, value, category). LIKE prefix selects every
        # category the sender/domain has been labeled as.
        prefix = f"{kind}:%"
        cur = conn.execute(
            "SELECT key, category, hits, COALESCE(first_seen, last_seen) FROM sender_profile WHERE key LIKE ? AND hits>=?",
            (prefix, min_hits),
        )
        for key, category, hits, first_seen_raw in cur.fetchall():
            try:
                first_seen_dt = datetime.fromisoformat(first_seen_raw)
            except (TypeError, ValueError):
                index[key][category] += int(hits) * weight
                continue
            if first_seen_dt.tzinfo is None:
                first_seen_dt = first_seen_dt.replace(tzinfo=timezone.utc)
            delta_days = max(0.0, (now - first_seen_dt).total_seconds() / 86400.0)
            decay = 2 ** (-delta_days / half_life_days)
            index[key][category] += int(hits) * weight * decay
    return {key: {cat: max(1, int(round(weight))) for cat, weight in cats.items()} for key, cats in index.items()}


def load_sender_diversity(conn: sqlite3.Connection | None) -> dict[str, int]:
    """Return a {sender_key: distinct_category_count} map for the dashboard.

    A sender with diversity > 4 is considered "noisy" and shows up in the
    dashboard's Noisy Senders section. v0.7: keys are
    (kind, value, category); the dashboard wants the diversity grouped by
    the (kind, value) parent. We strip the trailing ``:category`` segment
    in Python so the SQL stays simple.
    """

    diversity: dict[str, int] = {}
    if conn is None:
        return diversity
    try:
        cur = conn.execute(
            "SELECT key, category FROM sender_profile WHERE hits > 0"
        )
    except sqlite3.OperationalError:
        return diversity
    parent_cats: dict[str, set[str]] = {}
    for key, category in cur.fetchall():
        # Strip the trailing ``:category`` segment if present. Keys always
        # have the form ``kind:value:category`` (lowercased) in v0.7; the
        # pre-v0.7 shape ``kind:value`` is accepted by the conditional
        # below so older data continues to work.
        if ":" in key:
            segments = key.rsplit(":", 1)
            parent = segments[0]
        else:
            parent = key
        parent_cats.setdefault(parent, set()).add(category)
    return {key: len(cats) for key, cats in parent_cats.items()}


def load_thread_dominant_categories(conn: sqlite3.Connection | None, progress: dict[str, Decision] | None = None) -> dict[str, str]:
    """Build a thread_id -> dominant_category map from existing decisions.

    Used by --use-thread-aware to propagate a thread's dominant category to
    replies that would otherwise land in a catch-all (Review). Only non-catch-all
    categories with confidence >= 50 contribute. The dominant category is the
    one with the highest total confidence across the thread's known messages.
    """

    dominant: dict[str, str] = {}
    if conn is None:
        return dominant
    try:
        cur = conn.execute(
            "SELECT thread_id, categories_json, ad_confidence, protected FROM messages"
        )
        thread_cats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for thread_id, cats_json, ad_conf, protected in cur.fetchall():
            try:
                cats = json.loads(cats_json or "[]")
            except json.JSONDecodeError:
                continue
            for cat in cats:
                if cat in NON_LABEL_CATEGORIES:
                    continue
                weight = 2 if protected else 1
                thread_cats[thread_id][cat] += weight
        for thread_id, cats in thread_cats.items():
            if cats:
                dominant[thread_id] = max(cats.items(), key=lambda kv: kv[1])[0]
    except sqlite3.OperationalError:
        pass
    return dominant


def sender_profile_categories_from_index(index: dict[str, dict[str, int]], sender_email: str, registered_domain: str) -> dict[str, int]:
    """Look up learned categories for a sender/domain from the precomputed index."""

    weighted: dict[str, int] = defaultdict(int)
    for kind, value in (("sender", sender_email), ("domain", registered_domain)):
        if not value:
            continue
        # v0.7: the precomputed index now groups per-category under the
        # (kind, value) key, not under a single (kind, value) row. The
        # look-up walks every (kind, value, *) entry in the index that
        # matches the sender.
        prefix = f"{kind}:{value.lower()}:"
        for key, cats in index.items():
            if not key.startswith(prefix):
                continue
            for category, weight in cats.items():
                weighted[category] += weight
    return dict(weighted)


def body_category_hits(body_text: str) -> dict[str, list[str]]:
    """Return {category: [matched keywords]} found in the body text only.

    Used to cache which category rules the body satisfied so a re-run can reuse
    the body-derived features without re-fetching the message from Gmail.
    """

    hits: dict[str, list[str]] = {}
    if not body_text:
        return hits
    for name, keywords, _exclusions in CATEGORY_RULES:
        matched = keyword_hits(body_text, keywords)
        if matched:
            hits[name] = matched
    return hits


def upsert_message_features(conn: sqlite3.Connection | None, decisions: list[Decision], scan_mode: str) -> None:
    """Cache compact body features per message.

    Persists the body length, the category keyword names that hit in the body,
    the unsubscribe count, and a privacy-bounded cleaned body excerpt. The
    excerpt is what the embedding centroid learner reads on the next scan so it
    can learn from real message semantics instead of the category-hit names
    alone. Raw body text is never persisted: the excerpt is bounded to
    :data:`BODY_EXCERPT_FOR_FEATURES` and stripped of quoted reply chains and
    footer markers.
    """

    if conn is None or not decisions:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for item in decisions:
        if not item.body_len:
            continue
        rows.append(
            (
                item.message_id,
                item.body_len,
                json.dumps(item.body_category_hits, ensure_ascii=False),
                len(item.body_unsubscribe_links),
                scan_mode,
                item.body_text_excerpt or "",
                now,
            )
        )
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO message_features (
            message_id, body_len, body_category_hits_json, body_unsubscribe_count,
            scan_mode, body_text_excerpt, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            body_len=excluded.body_len,
            body_category_hits_json=excluded.body_category_hits_json,
            body_unsubscribe_count=excluded.body_unsubscribe_count,
            scan_mode=excluded.scan_mode,
            body_text_excerpt=excluded.body_text_excerpt,
            updated_at=excluded.updated_at
        """,
        rows,
    )
    conn.commit()


def load_body_features_index(conn: sqlite3.Connection | None) -> dict[str, dict[str, Any]]:
    """Precompute a message_id -> body-features index for cache-skip scans.

    When --scan full is used without --refresh-existing, the worker can fetch
    metadata-only for messages whose body features are already cached, saving
    Gmail quota. decide() then consults this index to apply the cached body
    category hits so categorization is still body-aware without re-fetching.
    """

    index: dict[str, dict[str, Any]] = {}
    if conn is None:
        return index
    cur = conn.execute(
        "SELECT message_id, body_len, body_category_hits_json, body_unsubscribe_count, body_text_excerpt FROM message_features WHERE scan_mode='full'"
    )
    for message_id, body_len, hits_json, unsub_count, body_excerpt in cur.fetchall():
        try:
            hits = json.loads(hits_json or "[]")
        except json.JSONDecodeError:
            hits = []
        index[message_id] = {
            "body_len": int(body_len or 0),
            "body_category_hits": list(hits),
            "body_unsubscribe_count": int(unsub_count or 0),
            "body_text_excerpt": body_excerpt or "",
        }
    return index


def load_category_centroids(conn: sqlite3.Connection | None) -> dict[str, list[float]]:
    """Load per-category centroid embeddings from SQLite.

    Centroids are average embedding vectors learned from past high-confidence
    decisions. Used by the embedding pre-classifier to compute semantic
    similarity scores for each category.
    """

    centroids: dict[str, list[float]] = {}
    if conn is None:
        return centroids
    try:
        cur = conn.execute("SELECT category, embedding_json, dimension FROM category_centroid")
        for category, emb_json, dim in cur.fetchall():
            try:
                vec = json.loads(emb_json or "[]")
                if vec and len(vec) == dim:
                    centroids[category] = [float(x) for x in vec]
            except (json.JSONDecodeError, TypeError):
                continue
    except sqlite3.OperationalError:
        pass
    return centroids


def update_category_centroids(
    conn: sqlite3.Connection | None,
    decisions: list[Decision],
    backend: Any,
    confidence_floor: int = 70,
    max_messages_per_category: int = 500,
    body_cap: int = BODY_EXCERPT_FOR_FEATURES,
) -> int:
    """Recompute per-category centroid embeddings from high-confidence decisions.

    For each category, collects messages with that category at confidence >=
    floor, embeds their subject + snippet + the cleaned body excerpt persisted
    in v0.7 (``body_text_excerpt``), averages the vectors, and stores the
    centroid in SQLite. Returns the number of centroids updated.

    Only categories with at least 3 high-confidence messages get a centroid, so
    a new category doesn't get a noisy centroid from one message.

    The text built for each message is ``subject + snippet + body_excerpt``,
    truncated to ``body_cap`` characters. Pre-v0.7, the text was built from
    ``subject + snippet + body_category_hits`` (the *names* of categories that
    hit, not the body text itself). That was a real weakness: the centroids
    never learned what a Finance message *sounds like* from real finance mail,
    only from the subject + the literal string "Finance". With v0.7 the body
    excerpt is persisted (see :data:`BODY_EXCERPT_FOR_FEATURES`) and the
    centroids now embed the real body semantics.
    """

    if conn is None or backend is None:
        return 0
    from sorter.embeddings import average_vectors

    # Group messages by category.
    cat_messages: dict[str, list[str]] = defaultdict(list)
    for item in decisions:
        for cat, conf in item.category_confidence.items():
            if cat in NON_LABEL_CATEGORIES:
                continue
            if conf < confidence_floor:
                continue
            # v0.7: include the cleaned body excerpt (if any) before falling
            # back to the legacy body_category_hits names. A message with no
            # excerpt is rare outside of metadata-only scans; we still keep
            # the hits fallback so the function stays correct for older
            # decision rows.
            text = f"{item.subject} {item.snippet}"
            excerpt = (item.body_text_excerpt or "").strip()
            if excerpt:
                text = f"{text} {excerpt}"
            elif item.body_category_hits:
                # Legacy fallback: a pre-v0.7 decision whose body excerpt is
                # empty but which carries category-hit names. Older centroids
                # were learned this way; we keep the same text shape so
                # re-scoring against them remains consistent until enough v0.7
                # excerpts land to fully refresh the centroids.
                text = f"{text} {' '.join(item.body_category_hits)}"
            cat_messages[cat].append(text[:body_cap])

    now = datetime.now(timezone.utc).isoformat()
    updated = 0
    for category, texts in cat_messages.items():
        if len(texts) < 3:
            continue
        # Cap the number of messages embedded per category to avoid unbounded
        # growth on very large mailboxes.
        sample = texts[:max_messages_per_category]
        vectors = []
        for text in sample:
            vec = backend.embed(text)
            if vec:
                vectors.append(vec)
        if len(vectors) < 3:
            continue
        centroid = average_vectors(vectors)
        if not centroid:
            continue
        conn.execute(
            """
            INSERT INTO category_centroid (category, embedding_json, dimension, message_count, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(category) DO UPDATE SET
                embedding_json=excluded.embedding_json,
                dimension=excluded.dimension,
                message_count=excluded.message_count,
                updated_at=excluded.updated_at
            """,
            (category, json.dumps(centroid, ensure_ascii=False), len(centroid), len(vectors), now),
        )
        updated += 1
    conn.commit()
    return updated


def update_sender_profiles(conn: sqlite3.Connection | None, decisions: list[Decision], confidence_floor: int = 65) -> None:
    """Accumulate high-confidence category decisions per sender/domain.

    Profiles are learned only from decisions at or above confidence_floor so
    borderline guesses do not poison the sender history. Protected messages
    still contribute their category (immigration/studies/etc.) because those
    are exactly the labels we want to remember for a sender.

    v0.7 — diversity tracking: every write to a (key, category) pair
    increments the sender's distinct-category count so the dashboard can
    surface "noisy" senders (diversity > 4). first_seen is set on the
    *first* write for a key so the time-decay in
    :func:`load_sender_profile_index` has a stable anchor.
    """

    if conn is None or not decisions:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for item in decisions:
        if not item.categories or item.ad_confidence < confidence_floor and not item.protected:
            continue
        for category in item.categories:
            if category in NON_LABEL_CATEGORIES:
                continue
            for kind, value in (("sender", item.sender_email), ("domain", item.registered_domain or item.sender_domain)):
                if not value:
                    continue
                # v0.7: include the category in the key so a single sender
                # can have one row per category, not one row total.
                key = sender_profile_key(kind, value, category)
                rows.append(
                    (
                        key,
                        kind,
                        category,
                        1,
                        1 if item.protected else 0,
                        item.date or now[:10],
                        now,
                    )
                )
    if not rows:
        return
    # The UPSERT has to set first_seen to the *existing* value when there is
    # one. SQLite does not let us reference the existing row from inside the
    # INSERT statement (no SELECT in the VALUES clause), so we update
    # first_seen in a follow-up pass that runs only for newly inserted rows.
    # The primary key is ``key`` which is now (kind, value, category).
    conn.executemany(
        """
        INSERT INTO sender_profile (key, kind, category, hits, protected_hits, last_seen, updated_at, first_seen, last_hits, category_diversity)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(key) DO UPDATE SET
            hits=sender_profile.hits+excluded.hits,
            protected_hits=sender_profile.protected_hits+excluded.protected_hits,
            last_seen=MAX(sender_profile.last_seen, excluded.last_seen),
            updated_at=excluded.updated_at,
            last_hits=excluded.last_hits
        """,
        [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[5], str(r[3])) for r in rows],
    )
    # First-seen backfill: any row that was just created gets first_seen set
    # to last_seen (which is the date the message was received). Existing
    # rows are untouched.
    conn.execute(
        """
        UPDATE sender_profile
        SET first_seen = last_seen
        WHERE first_seen IS NULL OR first_seen = ''
        """
    )
    # v0.7: refresh the per-sender distinct-category count. Done in a
    # second pass so the diversity is computed against the freshly updated
    # rows, not the pre-upsert state.
    conn.execute(
        """
        UPDATE sender_profile
        SET category_diversity = (
            SELECT COUNT(DISTINCT category) FROM sender_profile sp2
            WHERE sp2.key = sender_profile.key AND sp2.hits > 0
        )
        WHERE key IN (SELECT DISTINCT key FROM sender_profile WHERE hits > 0)
        """
    )
    conn.commit()


def sender_profile_categories(conn: sqlite3.Connection | None, sender_email: str, registered_domain: str, min_hits: int = 3) -> dict[str, int]:
    """Return {category: weight} a sender/domain has been labeled as before.

    Sender-level hits outweigh domain-level hits so a single noisy domain does
    not override a specific address. Returns an empty dict when there is no
    usable history, so callers can fall back to keyword classification.
    """

    if conn is None:
        return {}
    weighted: dict[str, int] = defaultdict(int)
    for kind, value, weight in (("sender", sender_email, 3), ("domain", registered_domain, 1)):
        if not value:
            continue
        # v0.7: keys are now (kind, value, category). Use a LIKE prefix to
        # match every category a sender/domain has been seen as.
        prefix = f"{kind}:{value.lower()}:%"
        cur = conn.execute(
            "SELECT category, hits FROM sender_profile WHERE key LIKE ? AND hits>=?",
            (prefix, min_hits),
        )
        for category, hits in cur.fetchall():
            weighted[category] += hits * weight
    return dict(weighted)


def upsert_state_decisions(conn: sqlite3.Connection | None, decisions: list[Decision]) -> None:
    """Persist the latest decision for each message after scans or applies."""

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
    """Append a durable record of a Gmail write that actually succeeded."""

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


def apply_archive_policy_caps(decisions: list[Decision], args: argparse.Namespace) -> None:
    """Cap planned archive actions the same way trash is capped.

    Archive is reversible, but an archive apply can still pull thousands of
    messages out of the inbox in one run. These caps give the same total,
    per-domain, and canary controls the trash stage already has so an archive
    apply can be proven on a small batch first.
    """

    def strip_archive(item: Decision, reason: str) -> None:
        item.planned_actions = [action for action in item.planned_actions if action != "archive"]
        item.negative_reasons.append(reason)
        item.review_priority = "archive_capped_review"

    archive_items = [item for item in decisions if "archive" in item.planned_actions]
    if args.max_archive_total and len(archive_items) > args.max_archive_total:
        allowed = {item.message_id for item in archive_items[: args.max_archive_total]}
        for item in archive_items[args.max_archive_total :]:
            strip_archive(item, f"archive_total_cap:{args.max_archive_total}")
        archive_items = [item for item in archive_items if item.message_id in allowed]

    if args.max_archive_per_domain:
        seen: dict[str, int] = defaultdict(int)
        for item in archive_items:
            domain = item.registered_domain or item.sender_domain or "(unknown)"
            seen[domain] += 1
            if seen[domain] > args.max_archive_per_domain:
                strip_archive(item, f"archive_domain_cap:{args.max_archive_per_domain}")

    if args.archive_canary_limit and args.apply and args.stage == "archive":
        remaining = [item for item in decisions if "archive" in item.planned_actions]
        for item in remaining[args.archive_canary_limit :]:
            strip_archive(item, f"archive_canary_limit:{args.archive_canary_limit}")


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
    """Fetch message metadata, classify it, and save progress as the scan runs."""

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
        # googleapiclient services are not documented as thread-safe, so each
        # worker lazily gets its own Gmail client while sharing the same creds.
        if not hasattr(thread_local, "service"):
            thread_local.service = build_gmail_service(build_func, creds, args)
        return thread_local.service

    def worker(message_id: str) -> tuple[str, Decision | None, str | None]:
        try:
            cached_index = getattr(args, "cached_body_features", {}) or {}
            # When a previous full scan cached this message's body features and
            # the user did not ask for a full refresh, fetch metadata-only and
            # let decide() reapply the cached body category hits. This avoids a
            # costly format=full fetch per message on re-runs.
            full_fetch = args.attachment_details or getattr(args, "scan", "metadata") == "full"
            use_cache = full_fetch and message_id in cached_index and not getattr(args, "refresh_existing", False)
            message = get_message_metadata(
                service_for_thread(),
                message_id,
                args.retries,
                args.retry_sleep,
                throttle,
                full_fetch and not use_cache,
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
    """Apply a reviewed decision set to Gmail.

    Trash is done as single-message calls so progress is visible and resumable.
    Label/archive operations are grouped into Gmail batchModify calls because
    they are reversible and much cheaper to apply in bulk.
    """

    started = time.monotonic()
    categories = sorted({category for item in decisions for category in item.categories})
    label_ids = get_or_create_labels(service, [f"{ROOT_LABEL}/{category}" for category in categories], args.retries, args.retry_sleep)
    grouped: dict[tuple[tuple[str, ...], tuple[str, ...]], list[Decision]] = {}
    trash_items: list[Decision] = []
    for item in decisions:
        # Re-check protection at the last possible moment. If a future scan bug
        # accidentally planned archive/trash on a protected row, apply still
        # strips the destructive action before talking to Gmail.
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


def write_relabel_manifest(path: Path, decisions: list[Decision], service: Any, args: argparse.Namespace) -> None:
    """Write a before->after relabel preview without applying anything.

    Uses each message's current Sorter labels (captured at scan time) and the
    freshly computed desired categories. Works in dry-run because it only reads
    the existing label list; labels that do not exist yet are reported as adds.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    id_to_name: dict[str, str] = {}
    if service is not None:
        name_to_id = list_labels(service, args.retries, args.retry_sleep)
        id_to_name = {lid: name for name, lid in name_to_id.items()}
    sorter_label_ids = {lid for lid, name in id_to_name.items() if name.startswith(f"{ROOT_LABEL}/")}
    rows = []
    for item in decisions:
        current = sorted(id_to_name[lid] for lid in item.existing_labels if lid in sorter_label_ids)
        desired = sorted(f"{ROOT_LABEL}/{category}" for category in labelable_categories(item.categories))
        add = sorted(set(desired) - set(current))
        remove = sorted(set(current) - set(desired))
        if add or remove or current:
            rows.append(
                {
                    "message_id": item.message_id,
                    "date": item.date,
                    "sender_domain": item.sender_domain,
                    "subject": item.subject,
                    "primary_category": item.primary_category,
                    "current_sorter_labels": current,
                    "desired_sorter_labels": desired,
                    "add_labels": add,
                    "remove_labels": remove,
                }
            )
    payload = {
        "stage": "relabel",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "message_count": len(rows),
        "items": rows,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def list_labels(service: Any, retries: int, retry_sleep: float) -> dict[str, str]:
    """Return {label_name: label_id} for all user labels."""

    existing = execute_with_retries(service.users().labels().list(userId="me"), retries, retry_sleep).get("labels", [])
    return {label.get("name", ""): label.get("id", "") for label in existing}


def compute_relabel_plan(item: Decision, sorter_label_ids: set[str], desired_name_to_id: dict[str, str]) -> tuple[set[str], set[str]]:
    """Diff a message's current Sorter labels against the desired set.

    Returns (add_ids, remove_ids). Only labels in the Sorter/ namespace are
    ever removed; user-created and Gmail system labels are never touched. The
    desired set is empty for messages that only land in catch-all buckets, so a
    relabel pass clears stale Sorter labels off generic mail.
    """

    current_sorter = {lid for lid in item.existing_labels if lid in sorter_label_ids}
    desired_ids = set(desired_name_to_id.values())
    return desired_ids - current_sorter, current_sorter - desired_ids


def apply_relabel(service: Any, decisions: list[Decision], args: argparse.Namespace, state_conn: sqlite3.Connection | None = None) -> None:
    """Remove stale Sorter/* labels and apply the corrected set in one pass.

    This is the re-run path for a mailbox the sorter already labeled once. It
    uses each message's current Sorter labels (captured at scan time) and the
    freshly computed desired categories, then issues one batchModify per group
    carrying both addLabelIds and removeLabelIds. Non-Sorter labels are never
    touched, and protected messages are still labeled (just correctly).
    """

    started = time.monotonic()
    name_to_id = list_labels(service, args.retries, args.retry_sleep)
    sorter_label_ids = {lid for name, lid in name_to_id.items() if name.startswith(f"{ROOT_LABEL}/")}

    desired_categories = sorted({category for item in decisions for category in labelable_categories(item.categories)})
    desired_names = [f"{ROOT_LABEL}/{category}" for category in desired_categories]
    created = get_or_create_labels(service, desired_names, args.retries, args.retry_sleep)
    name_to_id.update(created)

    planned: list[tuple[Decision, set[str], set[str]]] = []
    run_id = getattr(args, "relabel_run_id", "") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    already_applied = load_relabel_run_applied(state_conn, run_id)
    skipped = 0
    for item in decisions:
        if item.message_id in already_applied:
            skipped += 1
            item.action_done = "yes"
            continue
        desired_name_to_id = {f"{ROOT_LABEL}/{category}": created[f"{ROOT_LABEL}/{category}"] for category in labelable_categories(item.categories)}
        add_ids, remove_ids = compute_relabel_plan(item, sorter_label_ids, desired_name_to_id)
        if add_ids or remove_ids:
            planned.append((item, add_ids, remove_ids))

    grouped: dict[tuple[tuple[str, ...], tuple[str, ...]], list[Decision]] = {}
    for item, add_ids, remove_ids in planned:
        grouped.setdefault((tuple(sorted(add_ids)), tuple(sorted(remove_ids))), []).append(item)

    batch_count = sum((len(items) + args.batch_size - 1) // args.batch_size for items in grouped.values())
    print(
        f"Relabel plan: {len(planned)} messages need label changes in {batch_count} batchModify calls; "
        f"{len(desired_categories)} target Sorter labels.",
        flush=True,
    )

    batch_index = 0
    id_to_name = {lid: name for name, lid in name_to_id.items()}
    if skipped:
        print(f"Relabel resume: skipping {skipped} messages already applied in run_id={run_id}.", flush=True)
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
                previous = sorted(id_to_name.get(lid, lid) for lid in item.existing_labels if lid in sorter_label_ids)
                detail = json.dumps({"run_id": run_id, "removed": sorted(remove_ids), "added": sorted(add_ids), "previous_labels": previous}, ensure_ascii=False)
                record_action_ledger(state_conn, args.stage, "relabel", item.message_id, status="success", detail=detail)
            upsert_state_decisions(state_conn, chunk)
            if batch_index == 1 or batch_index == batch_count or batch_index % args.apply_progress_every == 0:
                print(f"Relabeled batch {batch_index}/{batch_count}...", flush=True)
    print(f"Relabel run_id={run_id}. Undo with --undo-relabel {run_id}.", flush=True)


def load_relabel_run_applied(conn: sqlite3.Connection | None, run_id: str) -> set[str]:
    """Return message IDs already applied in a relabel run (for resume)."""

    if conn is None or not run_id:
        return set()
    rows = conn.execute(
        "SELECT message_id FROM action_ledger WHERE stage='relabel' AND action='relabel' AND detail LIKE ?",
        (f'%"run_id": "{run_id}"%',),
    ).fetchall()
    return {row[0] for row in rows}


def undo_relabel(service: Any, run_id: str, args: argparse.Namespace, state_conn: sqlite3.Connection | None = None) -> int:
    """Reverse a relabel run by swapping the recorded adds/removes back.

    Reads the action_ledger rows for ``run_id``, restores each message's
    previous Sorter labels (re-adding what was removed and removing what was
    added), and records an ``undo_relabel`` ledger entry. Non-Sorter labels are
    never touched. Requires --apply to change Gmail; otherwise prints a dry-run
    preview.
    """

    if state_conn is None:
        print("Undo requires the SQLite state database (not --disable-state-db).", file=sys.stderr)
        return 2
    rows = state_conn.execute(
        "SELECT message_id, detail FROM action_ledger WHERE stage='relabel' AND action='relabel' AND detail LIKE ? ORDER BY id",
        (f'%"run_id": "{run_id}"%',),
    ).fetchall()
    if not rows:
        print(f"No relabel ledger rows found for run_id={run_id}.", file=sys.stderr)
        return 1
    name_to_id = list_labels(service, args.retries, args.retry_sleep)
    id_to_name = {lid: name for name, lid in name_to_id.items()}
    planned = 0
    grouped: dict[tuple[tuple[str, ...], tuple[str, ...]], list[str]] = {}
    for message_id, detail_json in rows:
        try:
            detail = json.loads(detail_json or "{}")
        except json.JSONDecodeError:
            continue
        # Reverse: undo adds by removing them, undo removes by re-adding them.
        remove_ids = {lid for lid in detail.get("added", []) if lid in name_to_id.values()}
        add_ids = {lid for lid in detail.get("removed", []) if lid in name_to_id.values()}
        if not add_ids and not remove_ids:
            continue
        planned += 1
        grouped.setdefault((tuple(sorted(add_ids)), tuple(sorted(remove_ids))), []).append(message_id)

    batch_count = sum((len(ids) + args.batch_size - 1) // args.batch_size for ids in grouped.values())
    print(f"Undo relabel run_id={run_id}: {planned} messages, {batch_count} batchModify calls.", flush=True)
    if not args.apply:
        print("DRY RUN: no Gmail changes. Re-run with --apply to undo.", flush=True)
        return 0
    batch_index = 0
    for (add_ids, remove_ids), message_ids in grouped.items():
        for start in range(0, len(message_ids), args.batch_size):
            batch_index += 1
            chunk = message_ids[start : start + args.batch_size]
            body: dict[str, list[str]] = {"ids": chunk}
            if add_ids:
                body["addLabelIds"] = list(add_ids)
            if remove_ids:
                body["removeLabelIds"] = list(remove_ids)
            execute_with_retries(service.users().messages().batchModify(userId="me", body=body), args.retries, args.retry_sleep)
            for message_id in chunk:
                record_action_ledger(state_conn, "relabel", "undo_relabel", message_id, status="success", detail=json.dumps({"undo_run_id": run_id}))
            if batch_index == 1 or batch_index == batch_count or batch_index % args.apply_progress_every == 0:
                print(f"Undo batch {batch_index}/{batch_count}...", flush=True)
    print(f"Undone relabel run_id={run_id}.", flush=True)
    return 0


def prune_empty_sorter_labels(service: Any, retries: int, retry_sleep: float) -> list[str]:
    """Delete Sorter/* labels that no longer have any messages."""

    labels = execute_with_retries(service.users().labels().list(userId="me"), retries, retry_sleep).get("labels", [])
    pruned: list[str] = []
    for label in labels:
        name = label.get("name", "")
        if not name.startswith(f"{ROOT_LABEL}/"):
            continue
        if int(label.get("messagesTotal", 0) or 0) == 0:
            try:
                execute_with_retries(service.users().labels().delete(userId="me", id=label["id"]), retries, retry_sleep)
                pruned.append(name)
            except Exception as error:
                print(f"Could not prune empty label {name}: {error}", file=sys.stderr)
    return pruned


def write_csv(path: Path, decisions: list[Decision]) -> None:
    """Write the flat report used for spreadsheet review."""

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
    """Write the full machine-readable decision report."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(item) for item in decisions], indent=2, ensure_ascii=False), encoding="utf-8")


def write_unsubscribe_report(path: Path, decisions: list[Decision]) -> None:
    """Write unsubscribe targets without storing full email bodies."""

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
    """Summarize noisy senders by registered domain for quick cleanup review."""

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
    """Rank domains by estimated storage impact so big wins are easy to spot."""

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
    """Write the per-domain review queue used before approving large actions."""

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
    """Give a human reviewer a starting point, not an automatic approval."""

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
    """Write exactly what each apply stage would touch."""

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
    """Read a reviewed manifest and return the message IDs allowed for apply."""

    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data.get("message_ids", []))


# --- AI label review packet export/merge -------------------------------------
#
# The code classifies with keyword rules + sender profiles + confidence scoring.
# That is fast and explainable, but it cannot understand context, intent, or
# nuance the way a language model can. So low-confidence decisions are exported
# as bounded review packets for an AI (local Qwen, Opencode, or any model) to
# inspect, suggest corrections, and write back. The script then merges both
# opinions before applying.
#
# Privacy: packets contain sender, subject, snippet, a bounded body excerpt
# (max 1200 chars, quotes/footers stripped), and the code's decision + reasons.
# They do NOT contain OAuth tokens, full body text, or attachment bytes.
# Packets are written to data/ which is gitignored.




def export_ai_review_packets(path: Path, decisions: list[Decision], threshold: int, body_excerpt_chars: int = AI_REVIEW_BODY_EXCERPT_CHARS, sender_profiles: dict[str, dict[str, int]] | None = None, thread_dominant: dict[str, str] | None = None) -> int:
    """Export low-confidence decisions as JSONL for AI label review.

    A message is exported when its highest category confidence is below the
    threshold, OR it landed in a catch-all bucket (Review/Updates), OR it has
    conflicting non-protected categories. Messages at 100% confidence are
    skipped — the code is sure, no AI review needed.

    Packets are enriched with the full list of available categories (so the AI
    knows the vocabulary), the sender's past categories (from profiles), and the
    thread's dominant category — all context that helps the AI make a better
    suggestion than the keyword rules alone.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    # The full category vocabulary the AI can choose from.
    available_categories = sorted(
        set(name for name, _, _ in CATEGORY_RULES)
        | {"Ads Promotions", "Newsletters Bulk", "Review", "Updates", "Priority Attachments", "Social", "Forums"}
    )
    profiles = sender_profiles or {}
    threads = thread_dominant or {}
    packets: list[dict[str, Any]] = []
    for item in decisions:
        max_conf = max(item.category_confidence.values()) if item.category_confidence else 0
        is_catchall = bool(NON_LABEL_CATEGORIES.intersection(item.categories))
        has_conflict = len([c for c in item.categories if c not in NON_LABEL_CATEGORIES]) > 2
        if max_conf >= 100 and not is_catchall:
            continue
        if max_conf >= threshold and not is_catchall and not has_conflict:
            continue
        # Build a bounded body excerpt from snippet + any cached body hits.
        body_excerpt = item.snippet or ""
        if item.body_category_hits:
            body_excerpt += f"\n[body keywords: {', '.join(item.body_category_hits)}]"
        body_excerpt = body_excerpt[:body_excerpt_chars]
        # Enrich: sender's past categories from the profile index.
        sender_past = sorted(
            profiles.get(f"sender:{item.sender_email.lower()}", {}).keys()
            | profiles.get(f"domain:{(item.registered_domain or item.sender_domain or '').lower()}", {}).keys()
        ) if profiles else []
        # Enrich: thread's dominant category.
        thread_dominant_cat = threads.get(item.thread_id, "")
        packets.append({
            "message_id": item.message_id,
            "thread_id": item.thread_id,
            "date": item.date,
            "sender": item.sender,
            "sender_email": item.sender_email,
            "sender_domain": item.sender_domain,
            "registered_domain": item.registered_domain,
            "subject": item.subject,
            "body_excerpt": body_excerpt,
            "code_categories": item.categories,
            "code_primary_category": item.primary_category,
            "code_confidence": item.category_confidence,
            "code_reasons": item.reasons,
            "code_negative_reasons": item.negative_reasons,
            "protected": item.protected,
            "has_attachment": item.has_attachment,
            "has_real_attachment": item.has_real_attachment,
            "list_unsubscribe": item.list_unsubscribe[:200] if item.list_unsubscribe else "",
            "available_categories": available_categories,
            "sender_past_categories": sender_past,
            "thread_dominant_category": thread_dominant_cat,
            "ai_label": "",
            "ai_confidence": 0,
            "ai_reason": "",
            "ai_reviewed": False,
        })
    with path.open("w", encoding="utf-8") as file:
        for packet in packets:
            file.write(json.dumps(packet, ensure_ascii=False) + "\n")
    return len(packets)


def merge_ai_labels(
    decisions: list[Decision],
    path: Path,
    min_ai_confidence: float = 0.7,
    min_ai_removal_confidence: float = 0.85,
) -> tuple[int, int, int]:
    """Merge AI-reviewed labels back into decisions.

    v0.6 semantics (preserved):
      * Additive override: the AI's label is added to ``item.categories`` if
        it is not already there, the per-category confidence is set, and a
        reason ``ai_override:<label>:<conf>`` is appended.
      * Protected status is never removed — the AI can add a protected
        category but cannot take one away.

    v0.7 addition — removal pass:
      * When ``ai_label in item.categories`` and the AI's confidence is
        ``>= min_ai_removal_confidence`` (default 0.85, stricter than
        ``min_ai_confidence``), the AI can drop a *non-protected* category
        from the message. The drop is recorded as ``ai_remove:<old>:<conf>``
        in ``reasons``. Removing a protected category is rejected even at
        very high confidence: protection is the safety floor.

    Returns ``(agreed, overridden, removed)`` where ``removed`` is the count
    of messages the AI actively corrected by removing a wrong label.
    """

    if not path.exists():
        return 0, 0, 0
    ai_map: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            packet = json.loads(line)
        except json.JSONDecodeError:
            continue
        if packet.get("ai_reviewed") and packet.get("ai_label"):
            ai_map[packet["message_id"]] = packet
    agreed = 0
    overridden = 0
    removed = 0
    for item in decisions:
        packet = ai_map.get(item.message_id)
        if not packet:
            continue
        ai_label = packet["ai_label"]
        ai_conf = float(packet.get("ai_confidence", 0))
        # Removal pass: AI says this label is wrong, and confidence is high.
        if ai_label in item.categories and ai_conf >= min_ai_removal_confidence:
            # The AI is voting against ai_label. If ai_label is protected, do
            # not remove; protection is the safety floor.
            if ai_label in PROTECTED_CATEGORIES:
                agreed += 1
                continue
            # The AI is confirming a label the code already assigned; that
            # is the "agreed" case, not the removal case.
            if ai_label == item.primary_category:
                agreed += 1
                continue
            # Remove the wrong label and refresh the primary if needed.
            item.categories = [c for c in item.categories if c != ai_label]
            item.category_confidence.pop(ai_label, None)
            item.reasons.append(f"ai_remove:{ai_label}:{ai_conf:.2f}")
            new_primary = pick_primary_category(item.categories) if item.categories else "Review"
            if new_primary != item.primary_category:
                item.primary_category = new_primary
            item.planned_actions = [f"label:{c}" for c in labelable_categories(item.categories)]
            removed += 1
            continue
        if ai_label in item.categories:
            agreed += 1
            continue
        if ai_conf < min_ai_confidence:
            continue
        # AI suggests a different label. Apply it with a reason, but never
        # remove a protected category the code already assigned.
        if ai_label not in NON_LABEL_CATEGORIES and ai_label not in item.categories:
            item.categories.append(ai_label)
            item.categories = sorted(set(item.categories))
            item.category_confidence[ai_label] = int(ai_conf * 100)
            item.reasons.append(f"ai_override:{ai_label}:{ai_conf:.2f}")
            # Re-pick primary if the AI's label is higher precedence.
            new_primary = pick_primary_category(item.categories)
            if new_primary != item.primary_category:
                item.primary_category = new_primary
            item.planned_actions = [f"label:{c}" for c in labelable_categories(item.categories)]
            overridden += 1
    return agreed, overridden, removed


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def pct(part: int | float, total: int | float) -> float:
    return 0.0 if not total else (float(part) / float(total)) * 100


def extract_unsubscribe_targets(header_value: str) -> list[str]:
    """Pull List-Unsubscribe targets out of Gmail header syntax."""

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
    """Group unsubscribe opportunities by sender domain for dashboard ranking."""

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
    """Render a bounded dashboard table so huge mailboxes stay browser-friendly."""

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
    """Create the main human review surface for a scan/apply run."""

    path.parent.mkdir(parents=True, exist_ok=True)
    category_counts = Counter(category for item in decisions for category in item.categories)
    sender_counts = Counter(item.registered_domain or item.sender_domain or "(unknown)" for item in decisions)
    review_counts = Counter(item.review_priority for item in decisions)
    trash_items = [item for item in decisions if item.review_priority in {"trash_review", "thread_review"}]
    archive_items = [item for item in decisions if "archive" in item.planned_actions]
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
    # Resumed scans can contain old cached rows. Show the count instead of
    # hiding it, because stale progress can otherwise make reports feel haunted.
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
<h2 class="section-title">Archive Review</h2>
<p class="note">Messages planned for archive now require an independent bulk-mail signal (List-Unsubscribe, List-Id, bulk precedence, campaign header, Gmail Promotions, or a body unsubscribe link), not just a high ad score. The archive reason column shows the evidence used.</p>
{render_table(archive_items, ["date", "ad_confidence", "primary_category", "sender_domain", "subject", "archive_reason", "planned_actions"], 100)}
<h2 class="section-title">Relabel Review</h2>
<p class="note">When run with --stage relabel, the sorter removes stale Sorter/* labels and re-applies the corrected set computed from the latest scan (use --scan full for body-aware relabeling). This preview shows the desired primary/category and the planned label actions per message; the exact before/after diff is written to manifests/relabel_manifest.json.</p>
{render_table(decisions, ["date", "primary_category", "sender_domain", "subject", "categories", "planned_actions", "body_len"], 100)}
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
{render_table(decisions, ["date", "ad_confidence", "review_priority", "sender_domain", "subject", "primary_category", "categories", "planned_actions"], 100)}
</main></body></html>""",
        encoding="utf-8",
    )


def write_yearly_dashboards(out_prefix: Path, decisions: list[Decision], args: argparse.Namespace) -> list[Path]:
    """Split the combined dashboard into year-sized review pages."""

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
    """Define the command-line contract for scan, report, and apply workflows."""

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
    parser.add_argument("--archive-threshold", type=int, default=65, help="Minimum ad confidence required to archive; archive also requires an independent bulk-mail signal.")
    parser.add_argument("--label-confidence", type=int, default=50, help="Minimum per-category confidence to apply a label; categories below this are dropped unless protected/priority. 0 disables the floor.")
    parser.add_argument("--max-labels-per-message", type=int, default=3, help="Cap applied Sorter labels per message; protected/priority buckets are always kept. 0 disables the cap.")
    parser.add_argument("--archive-min-age-days", type=int, default=0, help="Do not archive messages newer than this many days; 0 disables the recency guard.")
    parser.add_argument("--archive-skip-unread", action="store_true", help="Never archive messages that are still unread.")
    parser.add_argument("--trash-threshold", type=int, default=90)
    parser.add_argument("--pre-2020-trash-threshold", type=int, default=75)
    parser.add_argument("--stage", choices=["classify", "label", "archive", "trash", "relabel"], default="classify")
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
    parser.add_argument("--max-archive-per-domain", type=int, default=0, help="Cap planned archive actions per registered domain; 0 disables the cap.")
    parser.add_argument("--max-archive-total", type=int, default=0, help="Cap total planned archive actions; 0 disables the cap.")
    parser.add_argument("--archive-canary-limit", type=int, default=0, help="When applying archive, only keep the first N archive actions in the apply set.")
    parser.add_argument("--prune-empty-labels", action="store_true", help="After a relabel apply, delete Sorter/* labels that no longer have any messages.")
    parser.add_argument("--relabel-since-date", default="", help="Restrict a relabel stage to messages on or before this YYYY-MM-DD date.")
    parser.add_argument("--relabel-label", default="", help="Restrict a relabel stage to messages that currently carry this Sorter label (with or without the 'Sorter/' prefix).")
    parser.add_argument("--undo-relabel", default="", help="Reverse a previous relabel run by its run_id (printed at the end of an apply). Use with --apply to actually undo; otherwise dry-run.")
    parser.add_argument("--relabel-run-id", default="", help="Reuse a relabel run_id to resume an interrupted apply (skips messages already recorded in the ledger for that run).")
    parser.add_argument("--export-ai-review", action="store_true", help="After scan, export low-confidence decisions as JSONL review packets for an AI model to inspect and suggest labels.")
    parser.add_argument("--ai-review-threshold", type=int, default=75, help="Export decisions whose top category confidence is below this for AI review; 100-confidence messages are always skipped.")
    parser.add_argument("--ai-review-file", default=str(PROJECT_DIR / "data" / "label_review_packets.jsonl"), help="Path to the AI review JSONL file (export and merge both use this path).")
    parser.add_argument("--merge-ai-labels", action="store_true", help="Before apply, merge AI-reviewed labels from the review file back into decisions. The AI can add a label the code missed; protected status is never removed.")
    parser.add_argument("--ai-merge-min-confidence", type=float, default=0.7, help="Minimum AI confidence (0-1) required to override the code's label with the AI's suggestion.")
    parser.add_argument("--ai-merge-min-removal-confidence", type=float, default=0.85, help="v0.7: minimum AI confidence (0-1) required to REMOVE a non-protected category the code already assigned. Stricter than the addition threshold because removal is harder to undo.")
    parser.add_argument("--no-ai-learning", action="store_true", help="v0.7: disable the active-learning pass that pushes AI-verified decisions into sender_profile and category centroids.")
    parser.add_argument("--attachment-details", action="store_true", help="Fetch metadata-rich payloads for attachment names/types, not attachment bytes.")
    parser.add_argument("--scan", choices=["metadata", "full"], default="metadata", help="metadata = headers+snippet (fast); full = also read decoded body text for body-aware categorization. full costs more Gmail quota and is meant for a relabel/re-scan pass.")
    parser.add_argument("--use-sender-profiles", dest="use_sender_profiles", action="store_true", default=True, help="Use learned sender/domain category history to fix keyword misses (default on).")
    parser.add_argument("--no-sender-profiles", dest="use_sender_profiles", action="store_false", help="Disable sender-profile-assisted categorization.")
    parser.add_argument("--sender-profile-min-weight", type=int, default=6, help="Minimum learned weight required to add a profile-backed category the keywords missed.")
    parser.add_argument("--sender-profile-floor", type=int, default=65, help="Only learn profiles from decisions at or above this ad confidence (protected mail always contributes).")
    parser.add_argument("--sender-profile-half-life-days", type=int, default=180, help="v0.7: half-life in days for the sender-profile time decay. A row older than this contributes half as much weight as a fresh one. 0 disables decay (pre-v0.7 behavior).")
    parser.add_argument("--use-learned-weights", action="store_true", default=False, help="v0.8: replace the hand-tuned keyword weights with weights learned from the labeled data in the SQLite messages table. Trained on every scan; persisted to data/learned_weights.json. Falls back to hand-tuned weights when not enough labeled data exists.")
    parser.add_argument("--learned-weights-file", type=str, default="data/learned_weights.json", help="v0.8: path to the learned-weights JSON file.")
    parser.add_argument("--use-thread-aware", action="store_true", default=False, help="Propagate a thread's dominant category to replies that would otherwise land in a catch-all (Review). Never overrides a real keyword match or a protected category.")
    parser.add_argument("--use-thread-modeling", dest="use_thread_modeling", action="store_true", default=True, help="v0.8: thread-level conversation modeling. Builds a thread feature vector (message_count, distinct_senders, top_category_share, etc.) and uses it to boost a category's confidence by up to 15 points. More principled than the plurality vote.")
    parser.add_argument("--no-thread-modeling", dest="use_thread_modeling", action="store_false", help="v0.8: disable thread-level conversation modeling.")
    parser.add_argument("--use-sender-reputation", dest="use_sender_reputation", action="store_true", default=True, help="v0.8: first-class sender reputation signal. Computes total_messages, ad_fraction, and a derived 0-100 score per sender; high-reputation senders get -15 ad confidence, low-reputation senders get +10. The dashboard surfaces suggested blocklist candidates.")
    parser.add_argument("--no-sender-reputation", dest="use_sender_reputation", action="store_false", help="v0.8: disable sender reputation signal.")
    parser.add_argument("--since-history-id", type=str, default="", help="v0.8: incremental scan via the Gmail History API. Pass a numeric historyId, 'auto' to use the stored last_history_id, 'reset' to force a full re-scan, or empty to disable incremental mode.")
    parser.add_argument("--use-html-body", dest="use_html_body", action="store_true", default=True, help="v0.8: better HTML body extraction. Strips <style>/<script> blocks, preserves table structure as tab-separated rows, decodes quoted-printable bodies, and handles multipart/alternative correctly.")
    parser.add_argument("--no-html-body", dest="use_html_body", action="store_false", help="v0.8: disable the new HTML body extraction (fall back to the pre-v0.8 simple collector).")
    parser.add_argument("--use-embeddings", action="store_true", default=False, help="Enable embedding-based semantic classification. Uses the local LLM embedding endpoint or sentence-transformers to compute similarity to per-category centroids learned from past decisions. Falls back to keyword-only when unavailable.")
    parser.add_argument("--embedding-endpoint", default="http://127.0.0.1:8080/v1/embeddings", help="OpenAI-compatible /v1/embeddings endpoint for the local LLM server.")
    parser.add_argument("--embedding-model", default="local", help="Model name for the HTTP embedding endpoint.")
    parser.add_argument("--embedding-st-model", default="", help="sentence-transformers model name (e.g. all-MiniLM-L6-v2). Used when --embedding-endpoint is empty. Requires the sentence-transformers package.")
    parser.add_argument("--embedding-confidence-floor", type=int, default=70, help="Only learn centroids from decisions at or above this confidence.")
    parser.add_argument("--apply", action="store_true", help="Actually modify Gmail for the selected stage.")
    parser.add_argument("--trash-obvious-ads", action="store_true", help="Allow trash actions during --stage trash.")
    parser.add_argument("--i-understand-trash", action="store_true", help="Required with --apply --stage trash --trash-obvious-ads.")
    parser.add_argument("--open-browser", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    run_log = PROJECT_DIR / "data" / "runs" / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.log"
    try:
        run_log.parent.mkdir(parents=True, exist_ok=True)
        logging.getLogger().addHandler(logging.FileHandler(run_log, encoding="utf-8"))
        log.info("run log: %s", run_log)
    except OSError:
        log.warning("could not open run log at %s", run_log)
    apply_overrides(load_policy_overrides(PROJECT_DIR / "config" / "policy.yaml"))
    # v0.7: pass the config directory to decide() so per-language overlays
    # (config/policy.fr.yaml, config/policy.fa.yaml) can be loaded on demand.
    args._policy_config_dir = str(PROJECT_DIR / "config")
    # v0.8: train (or load) the per-keyword learned weights. Training
    # reads the labeled data from the SQLite messages table; the result
    # is cached in JSON so subsequent scans are fast.
    args._learned_weights = {}
    if getattr(args, "use_learned_weights", False):
        from sorter.learned_weights import load_weights, save_weights, train_from_decisions
        weights_path = Path(getattr(args, "learned_weights_file", "data/learned_weights.json"))
        weights = load_weights(weights_path)
        if not weights:
            weights = train_from_decisions(state_conn)
            if weights:
                save_weights(weights_path, weights)
                trained_count = sum(1 for w in weights.values() if w.confidence > 0)
                log.info("learned-weights: trained %d categories from %d decisions", trained_count, len(weights))
        args._learned_weights = weights
    log.info("gmail-sorter %s (%s) schema_version=%s", APP_VERSION, VERSION_CODE, SCHEMA_VERSION)
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

    # v0.8.1: resolve the --since-history-id flag into a concrete
    # history id. The flag accepts three values:
    #   - empty ("")        : incremental mode disabled; full re-scan.
    #   - "auto"            : use the stored last_history_id from
    #                         state_meta. The default for weekly
    #                         maintenance runs.
    #   - "reset"           : force a full re-scan and reset the
    #                         stored last_history_id to the current
    #                         mailbox state.
    #   - <numeric string>  : use the given history id explicitly.
    # The resolved value is stored on args so the rest of the
    # pipeline can read it.
    history_id_resolution = ""
    if state_conn is not None:
        from sorter.incremental import get_last_history_id, set_last_history_id, set_meta
        since = getattr(args, "since_history_id", "") or ""
        if since == "auto":
            stored = get_last_history_id(state_conn)
            if stored:
                history_id_resolution = f"auto:{stored}"
                log.info("since-history-id=auto: resuming from historyId=%s", stored)
            else:
                history_id_resolution = "auto:none"
                log.info("since-history-id=auto: no stored historyId; full re-scan")
        elif since == "reset":
            history_id_resolution = "reset"
            log.info("since-history-id=reset: forcing a full re-scan")
        elif since.isdigit():
            history_id_resolution = f"explicit:{since}"
            log.info("since-history-id=%s: explicit historyId", since)
        else:
            history_id_resolution = "disabled" if not since else f"unknown:{since}"
            if since:
                log.warning("since-history-id=%r is not 'auto', 'reset', or numeric; treating as disabled", since)
    args.history_id_resolution = history_id_resolution

    # v0.8.1+: wire the actual incremental fetch via the History API.
    # When a valid history ID is available (auto: or explicit:), we fetch
    # only the changed events since that ID instead of a full re-list.
    incremental_history_id = 0
    if history_id_resolution and ":" in history_id_resolution:
        prefix, _, suffix = history_id_resolution.partition(":")
        if prefix in ("auto", "explicit") and suffix.isdigit():
            incremental_history_id = int(suffix)

    try:
        history_records: list = []
        if incremental_history_id:
            from sorter.incremental import (
                fetch_all_history, parse_history_response,
                collect_message_ids, apply_label_events,
                remove_deleted_messages, set_last_history_id,
            )
            history_records, latest_history_id = fetch_all_history(
                service, incremental_history_id,
            )
            if history_records:
                events = parse_history_response({"history": history_records})
                all_ids = collect_message_ids(events)
                deleted_ids: set[str] = set()
                for e in events:
                    deleted_ids.update(e.messages_deleted)
                added_or_labeled = all_ids - deleted_ids
                message_ids = sorted(added_or_labeled)
                applied_event_count = apply_label_events(state_conn, events)
                if deleted_ids:
                    remove_deleted_messages(state_conn, list(deleted_ids))
                # Persist the latest history ID for the next incremental run.
                if latest_history_id:
                    set_last_history_id(state_conn, latest_history_id)
                    set_meta(state_conn, "last_scan_at",
                             datetime.now(timezone.utc).isoformat())
                log.info(
                    "incremental scan: %d events, %d added/labeled ids, "
                    "%d label events applied, %d deleted",
                    len(events), len(message_ids), applied_event_count, len(deleted_ids),
                )
            else:
                log.info("history.list returned no records; falling back to full re-scan")
                message_ids = list_message_ids(
                    service, args.query, args.max_messages,
                    args.retries, args.retry_sleep, list_throttle,
                )
        else:
            message_ids = list_message_ids(
                service, args.query, args.max_messages,
                args.retries, args.retry_sleep, list_throttle,
            )
    except HttpError as error:
        print(f"Failed to list messages: {error}", file=sys.stderr)
        if state_conn is not None:
            state_conn.close()
        return 1

    # Persist the current history ID for the next incremental run.
    # If we did an incremental scan, the latest ID was already stored by
    # fetch_all_history. For "reset" or a stale history ID, get the
    # current ID from the user profile and persist it.
    if state_conn is not None and (
        history_id_resolution == "reset"
        or (bool(incremental_history_id) and not history_records)
    ):
        from sorter.incremental import get_current_history_id, set_meta, set_last_history_id
        current_hid = get_current_history_id(service)
        if current_hid:
            set_last_history_id(state_conn, current_hid)
            set_meta(state_conn, "last_scan_at", datetime.now(timezone.utc).isoformat())
            set_meta(state_conn, "last_full_scan_at", datetime.now(timezone.utc).isoformat())

    progress_path = Path(args.progress_file)
    progress = load_progress(progress_path) if args.resume else {}
    args.sender_profiles = load_sender_profile_index(
        state_conn,
        half_life_days=getattr(args, "sender_profile_half_life_days", 180),
    ) if getattr(args, "use_sender_profiles", True) else {}
    args.cached_body_features = load_body_features_index(state_conn) if getattr(args, "scan", "metadata") == "full" and not args.refresh_existing else {}
    args.thread_dominant_categories = load_thread_dominant_categories(state_conn) if getattr(args, "use_thread_aware", False) else {}
    # v0.8: build thread-level features (message_count, distinct_senders,
    # top_category_share, etc.) for the thread-aware boost. The new
    # table is populated lazily on every scan; the load function
    # returns an empty dict on a fresh install.
    args.thread_features = {}
    if getattr(args, "use_thread_modeling", True):
        from sorter.thread_features import build_thread_features, upsert_thread_features, load_thread_features_index
        features = build_thread_features(state_conn)
        if features:
            upsert_thread_features(state_conn, features)
        args.thread_features = load_thread_features_index(state_conn)
    # v0.8: build sender_reputation (lifetime message count, ad
    # fraction, derived score) so score_ad can apply the -15/+10
    # adjustment.
    args.sender_reputation = {}
    if getattr(args, "use_sender_reputation", True):
        from sorter.sender_reputation import (
            build_sender_reputation, upsert_sender_reputation, load_sender_reputation_index, suggest_blocklist,
        )
        reputations = build_sender_reputation(state_conn)
        if reputations:
            upsert_sender_reputation(state_conn, reputations)
        args.sender_reputation = load_sender_reputation_index(state_conn)
        candidates = suggest_blocklist(args.sender_reputation)
        if candidates:
            log.info("blocklist candidates (>=%d msgs, >=%.0f%% ads, no protected): %s",
                     200, 0.80 * 100, ", ".join(candidates[:10]))
    # Embedding backend and centroids: optional semantic classification layer.
    args._embedding_backend = None
    args.category_centroids = {}
    if getattr(args, "use_embeddings", False):
        from sorter.embeddings import create_embedding_backend
        args._embedding_backend = create_embedding_backend(
            endpoint=getattr(args, "embedding_endpoint", ""),
            model=getattr(args, "embedding_model", "local"),
            st_model=getattr(args, "embedding_st_model", ""),
        )
        if args._embedding_backend is not None:
            args.category_centroids = load_category_centroids(state_conn)
            if args.category_centroids:
                log.info("embedding backend active; %d category centroids loaded", len(args.category_centroids))
            else:
                log.info("embedding backend active but no centroids yet; will learn after this scan")
        else:
            log.warning("--use-embeddings requested but no backend available; falling back to keyword-only")
    profile_count = len(args.sender_profiles)
    cache_count = len(args.cached_body_features)
    thread_count = len(args.thread_dominant_categories)
    centroid_count = len(args.category_centroids)
    print(
        f"Scanning {len(message_ids)} messages matching query: {args.query} with workers={max(1, args.workers)}"
        + (f"; sender profiles loaded={profile_count}" if profile_count else "")
        + (f"; cached body features={cache_count} (will fetch metadata-only for these)" if cache_count else "")
        + (f"; thread-aware dominant categories={thread_count}" if thread_count else "")
        + (f"; embedding centroids={centroid_count}" if centroid_count else "")
    )
    progress = scan_messages(message_ids, progress, creds, build, args, config)
    save_progress(progress_path, progress)

    decisions = decisions_for_current_query(progress, message_ids)
    upsert_state_decisions(state_conn, decisions)
    if getattr(args, "use_sender_profiles", True):
        update_sender_profiles(state_conn, decisions, confidence_floor=args.sender_profile_floor)
    if getattr(args, "scan", "metadata") == "full":
        upsert_message_features(state_conn, decisions, scan_mode="full")
    if getattr(args, "_embedding_backend", None) is not None:
        updated = update_category_centroids(state_conn, decisions, args._embedding_backend, confidence_floor=args.embedding_confidence_floor)
        if updated:
            print(f"Updated {updated} category centroid embeddings.", flush=True)
    stale_progress_count = len(progress) - len(decisions)
    if stale_progress_count > 0:
        print(f"Ignoring {stale_progress_count} cached decisions outside the current query/report set.")

    if getattr(args, "undo_relabel", ""):
        code = undo_relabel(service, args.undo_relabel, args, state_conn)
        if state_conn is not None:
            state_conn.close()
        return code

    if args.stage == "trash":
        protect_mixed_threads(service, decisions, args.retries, args.retry_sleep, args.thread_check_limit)

    if args.manifest:
        manifest_ids = load_manifest_ids(Path(args.manifest))
        decisions = [item for item in decisions if item.message_id in manifest_ids]
        print(f"Restricted apply/report set to {len(decisions)} messages from manifest: {args.manifest}")

    if args.stage == "relabel":
        before_filter = len(decisions)
        if getattr(args, "relabel_since_date", ""):
            try:
                cutoff = datetime.fromisoformat(args.relabel_since_date).date()
                decisions = [item for item in decisions if item.date and _date_le(item.date, cutoff.isoformat())]
                print(f"Relabel filter --relabel-since-date {args.relabel_since_date}: {len(decisions)}/{before_filter} messages kept.")
            except ValueError:
                print(f"--relabel-since-date must be YYYY-MM-DD; got {args.relabel_since_date}", file=sys.stderr)
                return 2
        if getattr(args, "relabel_label", ""):
            label_map = list_labels(service, args.retries, args.retry_sleep)
            target_ids = {lid for name, lid in label_map.items() if name == args.relabel_label or name == f"{ROOT_LABEL}/{args.relabel_label}"}
            if not target_ids:
                print(f"--relabel-label '{args.relabel_label}' did not match any Gmail label.", file=sys.stderr)
                return 2
            decisions = [item for item in decisions if target_ids.intersection(item.existing_labels)]
            print(f"Relabel filter --relabel-label '{args.relabel_label}': {len(decisions)}/{before_filter} messages kept.")

    if args.stage == "trash":
        apply_trash_policy_caps(decisions, args)
        upsert_state_decisions(state_conn, decisions)

    if args.stage in {"archive", "trash"}:
        apply_archive_policy_caps(decisions, args)
        upsert_state_decisions(state_conn, decisions)

    # Merge AI-reviewed labels before apply. The AI file is filled by a model
    # between the export step and this run; see HANDOVER.md for the workflow.
    if getattr(args, "merge_ai_labels", False):
        ai_path = Path(getattr(args, "ai_review_file", ""))
        min_removal = getattr(args, "ai_merge_min_removal_confidence", 0.85)
        agreed, overridden, removed = merge_ai_labels(
            decisions,
            ai_path,
            min_ai_confidence=args.ai_merge_min_confidence,
            min_ai_removal_confidence=min_removal,
        )
        print(
            f"AI label merge: {agreed} agreed, {overridden} added, {removed} removed.",
            flush=True,
        )
        upsert_state_decisions(state_conn, decisions)
        # v0.7: active learning. Push the AI's verified decisions back into
        # the sender profile and (when an embedding backend is on) the
        # category centroids so the next scan benefits immediately.
        if not getattr(args, "no_ai_learning", False):
            from sorter.ai_learning import apply_ai_learning
            try:
                with ai_path.open("r", encoding="utf-8") as f:
                    ai_packets = [json.loads(line) for line in f if line.strip()]
            except (FileNotFoundError, json.JSONDecodeError):
                ai_packets = []
            report = apply_ai_learning(
                state_conn,
                decisions,
                ai_packets,
                embedding_backend=getattr(args, "_embedding_backend", None),
            )
            print(
                f"AI active learning: considered {report['considered']} packets, "
                f"{report['profile_bumps']} sender-profile bumps, "
                f"{report['centroid_contributions']} centroid contributions.",
                flush=True,
            )

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

    if args.apply and args.stage == "relabel" and decisions:
        print(f"Applying relabel to {len(decisions)} messages (reading current Sorter/* labels and re-applying the corrected set)...", flush=True)
        apply_relabel(service, decisions, args, state_conn)
        if args.prune_empty_labels:
            pruned = prune_empty_sorter_labels(service, args.retries, args.retry_sleep)
            print(f"Pruned {len(pruned)} empty Sorter labels." + (f" {', '.join(pruned)}" if pruned else ""))
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
    if args.stage == "relabel":
        write_relabel_manifest(Path(args.manifest_dir) / "relabel_manifest.json", decisions, service, args)
    if getattr(args, "export_ai_review", False):
        ai_path = Path(getattr(args, "ai_review_file", ""))
        packet_count = export_ai_review_packets(ai_path, decisions, args.ai_review_threshold, sender_profiles=getattr(args, "sender_profiles", {}), thread_dominant=getattr(args, "thread_dominant_categories", {}))
        print(f"Exported {packet_count} low-confidence decisions for AI review to {ai_path}", flush=True)
        print("Fill ai_label/ai_confidence/ai_reason/ai_reviewed in that file, then re-run with --merge-ai-labels.", flush=True)
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
