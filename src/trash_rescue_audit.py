#!/usr/bin/env python3
"""Deep audit messages that the sorter planned for Gmail Trash.

Default mode is a dry-run report. Use --apply --i-understand-restore to untrash
rescue candidates and apply review labels.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import gmail_sorter


PROJECT_DIR = Path(__file__).resolve().parents[1]
ROOT_LABEL = "Trash Rescue"
REVIEW_LABELS = {
    "100": f"{ROOT_LABEL}/Review - 100 Confidence",
    "75": f"{ROOT_LABEL}/Review - 75-99 Confidence",
    "other": f"{ROOT_LABEL}/Review - Under 75 Confidence",
}

PRIORITY_TERMS = sorted(
    set(
        gmail_sorter.IMMIGRATION_KEYWORDS
        + gmail_sorter.STUDIES_KEYWORDS
        + [
            "law office",
            "retainer",
            "client file",
            "case number",
            "application number",
            "uci",
            "offer letter",
            "letter of acceptance",
            "tuition receipt",
            "enrollment letter",
            "enrolment letter",
            "transcript",
        ]
    )
)

HUMAN_CONVERSATION_TERMS = [
    "re:",
    "fw:",
    "fwd:",
    "following up",
    "attached",
    "please find",
    "let me know",
    "regards",
    "sincerely",
    "thank you",
    "thanks",
]

HIGH_VALUE_TERMS = sorted(
    set(
        gmail_sorter.TRANSACTIONAL_KEYWORDS
        + [
            "appointment",
            "application",
            "approval",
            "authorized",
            "case",
            "contract",
            "deadline",
            "document",
            "employment",
            "enrolment",
            "enrollment",
            "interview",
            "legal",
            "notice",
            "permit",
            "policy",
            "school",
            "statement",
            "university",
        ]
    )
)

MARKETING_TERMS = sorted(set(gmail_sorter.AD_SUBJECT_KEYWORDS + gmail_sorter.AD_BODY_KEYWORDS + gmail_sorter.AD_SENDER_KEYWORDS))


@dataclass
class RescueAudit:
    message_id: str
    thread_id: str
    date: str
    sender: str
    sender_email: str
    sender_domain: str
    registered_domain: str
    subject: str
    original_confidence: int
    original_reasons: list[str] = field(default_factory=list)
    original_negative_reasons: list[str] = field(default_factory=list)
    gmail_labels: list[str] = field(default_factory=list)
    still_in_trash: bool = False
    deep_risk_score: int = 0
    script_delete_confidence: int = 0
    rescue_reasons: list[str] = field(default_factory=list)
    keep_trash_reasons: list[str] = field(default_factory=list)
    recommended_action: str = "keep_trash"
    review_label: str = ""
    has_real_attachment: bool = False
    attachment_names: list[str] = field(default_factory=list)
    attachment_mime_types: list[str] = field(default_factory=list)
    body_unsubscribe_links: list[str] = field(default_factory=list)
    size_estimate: int = 0
    snippet: str = ""
    body_excerpt: str = ""
    model_decision: str = ""
    model_confidence: float = 0.0
    model_reason: str = ""
    model_error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deep-audit Gmail messages previously planned for Trash.")
    parser.add_argument("--credentials", default=str(PROJECT_DIR / "secrets" / "credentials.json"))
    parser.add_argument("--token-modify", default=str(PROJECT_DIR / "secrets" / "token_sorter_modify.json"))
    parser.add_argument("--progress-file", default=str(PROJECT_DIR / "data" / "gmail_sorter_all_years_progress.json"))
    parser.add_argument("--out-prefix", default=str(PROJECT_DIR / "reports" / "trash_rescue_audit"))
    parser.add_argument("--from-audit-json", default="", help="Load an existing audit JSON instead of fetching Gmail again.")
    parser.add_argument("--max-messages", type=int, default=0, help="Limit number of progress candidates to inspect.")
    parser.add_argument("--min-confidence", type=int, default=0, help="Only inspect candidates with original ad confidence >= N.")
    parser.add_argument("--include-not-in-trash", action="store_true", help="Also report candidates no longer carrying Gmail TRASH.")
    parser.add_argument("--checkpoint-every", type=int, default=100, help="Write partial reports every N audited Gmail messages.")
    parser.add_argument("--workers", type=int, default=1, help="Reserved for future parallel audit; current audit is sequential to reduce Gmail pressure.")
    parser.add_argument("--sleep", type=float, default=0.1)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-sleep", type=float, default=5.0)
    parser.add_argument("--http-timeout", type=float, default=120.0)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--openai", action="store_true", help="Use OpenAI Responses API for borderline candidates when OPENAI_API_KEY is set.")
    parser.add_argument("--openai-model", default=os.environ.get("TRASH_RESCUE_MODEL", "gpt-5"))
    parser.add_argument("--openai-max", type=int, default=200, help="Maximum candidates to send to OpenAI.")
    parser.add_argument("--web-search", action="store_true", help="Ask the OpenAI model to use web search for sender/domain context when available.")
    parser.add_argument("--llm-export", action="store_true", help="Write local-LLM review packets for Qwen or another local model.")
    parser.add_argument("--llm-body-chars", type=int, default=1200, help="Maximum normalized body excerpt characters per LLM packet.")
    parser.add_argument("--model-results", default="", help="Import local-model JSONL decisions and merge them into the report.")
    parser.add_argument("--local-llm", action="store_true", help="Send review packets to the local llama.cpp OpenAI-compatible server and merge decisions.")
    parser.add_argument("--local-llm-url", default="http://127.0.0.1:8080/v1/chat/completions")
    parser.add_argument("--local-llm-model", default="local")
    parser.add_argument("--local-llm-profile", default="qwen36", help="Profile passed to llm-switch when --start-local-llm is used.")
    parser.add_argument("--start-local-llm", action="store_true", help="Run llm-switch before calling the local model.")
    parser.add_argument("--local-llm-all", action="store_true", help="Send every audited Trash row to the local model, not only rescue/borderline rows.")
    parser.add_argument("--local-llm-max", type=int, default=0, help="Maximum audit rows to send to the local model; 0 means all rescue/borderline rows.")
    parser.add_argument("--local-llm-temperature", type=float, default=0.1)
    parser.add_argument("--local-llm-timeout", type=float, default=180.0)
    parser.add_argument("--local-llm-results", default="", help="Where to write local model JSONL results. Defaults beside out-prefix.")
    parser.add_argument("--apply", action="store_true", help="Untrash rescue candidates and apply review labels.")
    parser.add_argument("--label-only", action="store_true", help="Apply review labels without untrashing.")
    parser.add_argument("--i-understand-restore", action="store_true", help="Required with --apply.")
    parser.add_argument("--delete-passed-trash", action="store_true", help="Permanently delete messages that both script and local model classify as 100%% safe trash.")
    parser.add_argument("--i-understand-permanent-delete", action="store_true", help="Required with --delete-passed-trash. Gmail permanent delete cannot be undone.")
    return parser.parse_args()


def load_trash_candidates(path: Path, min_confidence: int, max_messages: int) -> list[gmail_sorter.Decision]:
    progress = gmail_sorter.load_progress(path)
    candidates = [
        item
        for item in progress.values()
        if "trash" in item.planned_actions and item.ad_confidence >= min_confidence
    ]
    candidates.sort(key=lambda item: (item.ad_confidence, item.date, item.sender_domain), reverse=True)
    if max_messages:
        candidates = candidates[:max_messages]
    return candidates


def audit_from_dict(data: dict[str, Any]) -> RescueAudit:
    valid = RescueAudit.__dataclass_fields__.keys()
    return RescueAudit(**{key: data[key] for key in valid if key in data})


def load_existing_audit(path: Path) -> list[RescueAudit]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [audit_from_dict(item) for item in data]


def is_missing_gmail_message_error(error: Exception) -> bool:
    status = getattr(getattr(error, "resp", None), "status", None)
    if status == 404:
        return True
    lowered = str(error).lower()
    return "notfound" in lowered or "requested entity was not found" in lowered


def searchable_text(message: dict[str, Any], headers: dict[str, str]) -> str:
    payload = message.get("payload", {})
    body_text = gmail_sorter.collect_body_text(payload, max_chars=80_000)
    parts = [
        headers.get("from", ""),
        headers.get("to", ""),
        headers.get("cc", ""),
        headers.get("subject", ""),
        message.get("snippet", ""),
        body_text,
    ]
    return "\n".join(part for part in parts if part)


def compact_text(text: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized[:limit]


def hits(text: str, terms: list[str]) -> list[str]:
    lowered = text.lower()
    found = []
    for term in terms:
        pattern = r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])"
        if re.search(pattern, lowered):
            found.append(term)
    return found


def confidence_bucket(confidence: int) -> str:
    if confidence >= 100:
        return "100"
    if confidence >= 75:
        return "75"
    return "other"


def label_for_confidence(confidence: int) -> str:
    return REVIEW_LABELS[confidence_bucket(confidence)]


def compute_script_delete_confidence(decision: gmail_sorter.Decision, score: int, rescue_reasons: list[str], keep_trash_reasons: list[str], real_attachment_count: int) -> int:
    if score > 0 or rescue_reasons or real_attachment_count:
        return 0
    if decision.ad_confidence < 100:
        return 0
    if not decision.perfect_ad_match:
        return 0
    required_evidence = {
        "original_perfect_ad_match",
        "original_100_confidence",
    }
    if not required_evidence.issubset(set(keep_trash_reasons)):
        return 0
    if not any(reason.startswith("marketing_terms:") for reason in keep_trash_reasons):
        return 0
    if "unsubscribe_signal" not in keep_trash_reasons:
        return 0
    return 100


def audit_message(decision: gmail_sorter.Decision, message: dict[str, Any], body_chars: int = 1200) -> RescueAudit:
    payload = message.get("payload", {})
    headers = gmail_sorter.header_map(payload)
    sender = headers.get("from", decision.sender)
    sender_email, sender_domain = gmail_sorter.parse_sender(sender)
    registered_domain = gmail_sorter.registered_domain_for(sender_domain or decision.sender_domain)
    subject = headers.get("subject", decision.subject)
    date = gmail_sorter.parse_date(headers.get("date", decision.date), message.get("internalDate")) or decision.date
    labels = message.get("labelIds", [])
    body_text = gmail_sorter.collect_body_text(payload, max_chars=80_000)
    full_text = "\n".join(
        part
        for part in [
            headers.get("from", ""),
            headers.get("to", ""),
            headers.get("cc", ""),
            headers.get("subject", ""),
            message.get("snippet", ""),
            body_text,
        ]
        if part
    )

    priority_hits = hits(full_text, PRIORITY_TERMS)
    high_value_hits = hits(full_text, HIGH_VALUE_TERMS)
    marketing_hits = hits(full_text, MARKETING_TERMS)
    conversation_hits = hits(f"{subject}\n{message.get('snippet', '')}", HUMAN_CONVERSATION_TERMS)
    real_attachment_count, inline_attachment_count = gmail_sorter.attachment_counts(payload)
    attachment_names, attachment_mime_types = gmail_sorter.collect_attachment_details(payload)
    body_unsubscribe_links = gmail_sorter.extract_body_unsubscribe_links(payload)

    rescue_reasons = []
    keep_trash_reasons = []
    score = 0

    if priority_hits:
        score += min(60, 18 * len(priority_hits))
        rescue_reasons.append("priority_terms:" + ", ".join(priority_hits[:8]))
    if high_value_hits:
        score += min(45, 10 * len(high_value_hits))
        rescue_reasons.append("high_value_terms:" + ", ".join(high_value_hits[:8]))
    if real_attachment_count:
        score += 35
        rescue_reasons.append(f"real_attachments:{real_attachment_count}")
    elif inline_attachment_count:
        keep_trash_reasons.append(f"inline_attachments_only:{inline_attachment_count}")
    if conversation_hits:
        score += min(25, 8 * len(conversation_hits))
        rescue_reasons.append("conversation_signals:" + ", ".join(conversation_hits[:6]))
    if any(label in labels for label in gmail_sorter.IMPORTANT_LABELS):
        score += 30
        rescue_reasons.append("gmail_important_primary_or_starred")
    if decision.protected or decision.negative_reasons:
        score += 15
        rescue_reasons.append("original_protection_or_negative_reason")

    if marketing_hits:
        score -= min(45, 8 * len(marketing_hits))
        keep_trash_reasons.append("marketing_terms:" + ", ".join(marketing_hits[:8]))
    if headers.get("list-unsubscribe") or body_unsubscribe_links:
        score -= 15
        keep_trash_reasons.append("unsubscribe_signal")
    if decision.perfect_ad_match:
        score -= 25
        keep_trash_reasons.append("original_perfect_ad_match")
    if decision.ad_confidence >= 100:
        score -= 10
        keep_trash_reasons.append("original_100_confidence")

    score = max(0, min(100, score))
    recommended_action = "rescue_review" if score >= 45 else "keep_trash"
    script_delete_confidence = compute_script_delete_confidence(
        decision,
        score,
        rescue_reasons,
        keep_trash_reasons,
        real_attachment_count,
    )

    return RescueAudit(
        message_id=decision.message_id,
        thread_id=message.get("threadId", decision.thread_id),
        date=date,
        sender=sender,
        sender_email=sender_email or decision.sender_email,
        sender_domain=sender_domain or decision.sender_domain,
        registered_domain=registered_domain,
        subject=subject,
        original_confidence=decision.ad_confidence,
        original_reasons=decision.reasons,
        original_negative_reasons=decision.negative_reasons,
        gmail_labels=labels,
        still_in_trash="TRASH" in labels,
        deep_risk_score=score,
        script_delete_confidence=script_delete_confidence,
        rescue_reasons=rescue_reasons,
        keep_trash_reasons=keep_trash_reasons,
        recommended_action=recommended_action,
        review_label=label_for_confidence(decision.ad_confidence),
        has_real_attachment=real_attachment_count > 0,
        attachment_names=attachment_names,
        attachment_mime_types=attachment_mime_types,
        body_unsubscribe_links=body_unsubscribe_links,
        size_estimate=int(message.get("sizeEstimate") or 0),
        snippet=message.get("snippet", decision.snippet),
        body_excerpt=compact_text(body_text, body_chars),
    )


def model_should_review(item: RescueAudit) -> bool:
    if item.recommended_action == "rescue_review":
        return True
    if item.deep_risk_score >= 30 and item.original_confidence < 100:
        return True
    if item.has_real_attachment or item.original_negative_reasons:
        return True
    return False


def call_openai_reasoner(item: RescueAudit, model: str, web_search: bool) -> tuple[str, str, str]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return "", "", "OPENAI_API_KEY is not set"

    prompt = {
        "task": "Decide whether a Gmail message that was moved to Trash may deserve human restore review.",
        "instruction": "Return compact JSON with keys decision and reason. decision must be rescue_review or keep_trash.",
        "message": {
            "sender": item.sender,
            "sender_domain": item.sender_domain,
            "registered_domain": item.registered_domain,
            "subject": item.subject,
            "date": item.date,
            "snippet": item.snippet,
            "original_confidence": item.original_confidence,
            "original_reasons": item.original_reasons,
            "original_negative_reasons": item.original_negative_reasons,
            "deep_risk_score": item.deep_risk_score,
            "rescue_reasons": item.rescue_reasons,
            "keep_trash_reasons": item.keep_trash_reasons,
            "attachment_names": item.attachment_names,
            "attachment_mime_types": item.attachment_mime_types,
        },
    }
    body: dict[str, Any] = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": json.dumps(prompt, ensure_ascii=False),
                    }
                ],
            }
        ],
    }
    if web_search:
        body["tools"] = [{"type": "web_search_preview"}]

    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        return "", "", f"OpenAI HTTP {error.code}: {detail[:500]}"
    except Exception as error:
        return "", "", str(error)

    text = extract_response_text(data)
    decision = ""
    reason = text
    try:
        parsed = json.loads(text)
        decision = str(parsed.get("decision", ""))
        reason = str(parsed.get("reason", ""))
    except Exception:
        if "rescue_review" in text:
            decision = "rescue_review"
        elif "keep_trash" in text:
            decision = "keep_trash"
    return decision, reason, ""


def extract_response_text(data: dict[str, Any]) -> str:
    chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(content["text"])
    if chunks:
        return "\n".join(chunks).strip()
    if data.get("output_text"):
        return str(data["output_text"]).strip()
    return json.dumps(data)[:2000]


def write_reports(out_prefix: Path, audits: list[RescueAudit]) -> None:
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    rows = [asdict(item) for item in audits]
    (out_prefix.with_suffix(".json")).write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    fieldnames = list(RescueAudit.__dataclass_fields__.keys())
    with out_prefix.with_suffix(".csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            flat = {}
            for key, value in row.items():
                flat[key] = "; ".join(str(part) for part in value) if isinstance(value, list) else value
            writer.writerow(flat)

    rescue = [item for item in audits if item.recommended_action == "rescue_review"]
    by_domain = Counter(item.registered_domain or item.sender_domain or "(unknown)" for item in rescue)
    by_confidence = Counter(confidence_bucket(item.original_confidence) for item in rescue)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "audited": len(audits),
        "still_in_trash": sum(1 for item in audits if item.still_in_trash),
        "rescue_review": len(rescue),
        "keep_trash": sum(1 for item in audits if item.recommended_action == "keep_trash"),
        "rescue_by_confidence_bucket": dict(by_confidence),
        "top_rescue_domains": by_domain.most_common(50),
        "permanent_delete_ready": len(permanent_delete_candidates(audits)),
    }
    (out_prefix.with_name(out_prefix.name + "_summary.json")).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_html_report(out_prefix.with_suffix(".html"), audits, summary)


def llm_packet(item: RescueAudit) -> dict[str, Any]:
    return {
        "message_id": item.message_id,
        "task": "Decide if this Gmail Trash message should be restored for human review before permanent deletion.",
        "allowed_decisions": ["rescue_review", "keep_trash"],
        "sender": item.sender,
        "sender_email": item.sender_email,
        "sender_domain": item.sender_domain,
        "registered_domain": item.registered_domain,
        "date": item.date,
        "subject": item.subject,
        "snippet": item.snippet,
        "body_excerpt": item.body_excerpt,
        "original_ad_confidence": item.original_confidence,
        "original_sorter_reasons": item.original_reasons,
        "original_sorter_negative_reasons": item.original_negative_reasons,
        "gmail_labels": item.gmail_labels,
        "still_in_trash": item.still_in_trash,
        "local_deep_risk_score": item.deep_risk_score,
        "local_rescue_reasons": item.rescue_reasons,
        "local_keep_trash_reasons": item.keep_trash_reasons,
        "has_real_attachment": item.has_real_attachment,
        "attachment_names": item.attachment_names,
        "attachment_mime_types": item.attachment_mime_types,
        "body_unsubscribe_links": item.body_unsubscribe_links[:5],
        "size_estimate": item.size_estimate,
        "review_policy": {
            "restore_if": [
                "immigration, visa, IRCC, legal counsel, study permit, school, transcript, tuition, or academic evidence",
                "real attachments or documents",
                "human conversation or follow-up",
                "security, finance, receipt, government/legal, appointment, employment, or account evidence",
                "sender/domain context suggests real institution or person, not marketing",
            ],
            "keep_trash_if": [
                "clear newsletter, sale, discount, coupon, social notification, or marketing blast",
                "unsubscribe/list/bulk evidence dominates and no durable record value is present",
                "only inline marketing images and no real attachment/document value",
            ],
        },
    }


def write_llm_export(out_prefix: Path, audits: list[RescueAudit]) -> None:
    review_items = sorted(
        audits,
        key=lambda item: (item.recommended_action == "rescue_review", item.deep_risk_score, item.original_confidence),
        reverse=True,
    )
    jsonl_path = out_prefix.with_name(out_prefix.name + "_llm_input.jsonl")
    prompt_path = out_prefix.with_name(out_prefix.name + "_llm_prompt.md")
    with jsonl_path.open("w", encoding="utf-8") as file:
        for item in review_items:
            file.write(json.dumps(llm_packet(item), ensure_ascii=False) + "\n")
    prompt_path.write_text(
        """# Trash Rescue Local-Model Review

You are reviewing Gmail messages that were moved to Trash by an automated cleanup.
Your job is to prevent permanent deletion mistakes.

Read each JSONL record from the input file. For each record, output exactly one JSON object per line:

```json
{"message_id":"...", "decision":"rescue_review", "confidence":0.0, "reason":"short reason", "signals":["signal1","signal2"]}
```

Allowed decisions:

- `rescue_review`: restore or label this message for human review before permanent deletion.
- `keep_trash`: it is likely safe to leave in Trash.

Prefer `rescue_review` for immigration, visa, IRCC, legal/lawyer, studies, school, transcript, tuition, government/legal, finance/security, receipts/orders, real attachments, or human conversation. Prefer `keep_trash` for obvious marketing/newsletter/social-sale messages with unsubscribe/list/bulk evidence and no durable value.

Keep the reason short and concrete. Do not output prose outside JSONL.
""",
        encoding="utf-8",
    )
    print(f"Wrote local-LLM input {jsonl_path}")
    print(f"Wrote local-LLM prompt {prompt_path}")


def local_llm_prompt(packet: dict[str, Any]) -> str:
    return (
        "You are double-checking Gmail Trash before permanent deletion. "
        "Return exactly one JSON object and no prose. "
        "Schema: {\"message_id\": string, \"decision\": \"rescue_review\"|\"keep_trash\", "
        "\"confidence\": number from 0 to 1, \"reason\": short string, \"signals\": array of short strings}. "
        "Use confidence 1.0 only when you are completely certain the message is safe to permanently delete. "
        "If there is any durable account, immigration, legal, school, attachment, financial, security, receipt, "
        "or human-conversation value, choose rescue_review. "
        "Prefer rescue_review for immigration, visa, IRCC, legal/lawyer, studies, school, transcript, tuition, "
        "government/legal, finance/security, receipts/orders, real attachments, or human conversation. "
        "Prefer keep_trash for obvious marketing/newsletter/social-sale messages with unsubscribe/list/bulk evidence "
        "and no durable value.\n\n"
        f"Record:\n{json.dumps(packet, ensure_ascii=False)}"
    )


def ensure_local_llm(args: argparse.Namespace) -> None:
    if args.start_local_llm:
        subprocess.run(["llm-switch", args.local_llm_profile], check=True)
        return
    models_url = args.local_llm_url.rsplit("/", 2)[0] + "/models"
    try:
        with urllib.request.urlopen(models_url, timeout=5) as response:
            response.read()
    except Exception as error:
        raise SystemExit(
            f"Local LLM is not responding at {models_url}. "
            f"Start it with: llm-switch {args.local_llm_profile} "
            "or rerun with --start-local-llm."
        ) from error


def call_local_llm(packet: dict[str, Any], args: argparse.Namespace) -> tuple[dict[str, Any] | None, str]:
    body = {
        "model": args.local_llm_model,
        "messages": [
            {
                "role": "user",
                "content": local_llm_prompt(packet),
            }
        ],
        "temperature": args.local_llm_temperature,
        "top_p": 0.8,
        "max_tokens": 256,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    request = urllib.request.Request(
        args.local_llm_url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=args.local_llm_timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        return None, str(error)
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None, f"no JSON object in response: {text[:300]}"
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as error:
        return None, f"invalid JSON response: {error}: {text[:300]}"
    parsed.setdefault("message_id", packet["message_id"])
    timings = data.get("timings", {})
    if isinstance(timings, dict):
        parsed["_local_llm_timing"] = {
            "prompt_tokens_per_second": timings.get("prompt_per_second"),
            "generation_tokens_per_second": timings.get("predicted_per_second"),
            "draft_tokens": timings.get("draft_n"),
            "draft_tokens_accepted": timings.get("draft_n_accepted"),
        }
    return parsed, ""


def run_local_llm_review(audits: list[RescueAudit], out_prefix: Path, args: argparse.Namespace) -> Path:
    ensure_local_llm(args)
    results_path = Path(args.local_llm_results) if args.local_llm_results else out_prefix.with_name(out_prefix.name + "_local_llm_results.jsonl")
    completed_ids = set()
    if results_path.exists():
        for raw_line in results_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if row.get("message_id"):
                completed_ids.add(str(row["message_id"]))
        if completed_ids:
            imported = import_model_results(results_path, audits)
            print(f"Resuming local LLM review with {len(completed_ids)} existing decisions imported ({imported} matched current audit).")
    sorted_audits = sorted(
        audits,
        key=lambda row: (row.recommended_action == "rescue_review", row.deep_risk_score, row.original_confidence),
        reverse=True,
    )
    if args.local_llm_all:
        review_items = [item for item in sorted_audits if item.message_id not in completed_ids]
    else:
        review_items = [
            item
            for item in sorted_audits
            if model_should_review(item) and item.message_id not in completed_ids
        ]
    if args.local_llm_max:
        review_items = review_items[: args.local_llm_max]
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("a", encoding="utf-8") as file:
        for index, item in enumerate(review_items, 1):
            packet = llm_packet(item)
            result, error = call_local_llm(packet, args)
            if result is None:
                result = {
                    "message_id": item.message_id,
                    "decision": "",
                    "confidence": 0,
                    "reason": error,
                    "signals": ["local_llm_error"],
                }
            file.write(json.dumps(result, ensure_ascii=False) + "\n")
            if index == 1 or index == len(review_items) or index % 25 == 0:
                timing = result.get("_local_llm_timing", {})
                if timing:
                    draft_tokens = timing.get("draft_tokens") or 0
                    draft_accepted = timing.get("draft_tokens_accepted") or 0
                    acceptance = (draft_accepted / draft_tokens) if draft_tokens else 0
                    print(
                        "Local LLM reviewed "
                        f"{index}/{len(review_items)} candidates "
                        f"(gen={float(timing.get('generation_tokens_per_second') or 0):.1f} tok/s, "
                        f"prompt={float(timing.get('prompt_tokens_per_second') or 0):.1f} tok/s, "
                        f"draft_accept={acceptance:.1%})...",
                        flush=True,
                    )
                else:
                    print(f"Local LLM reviewed {index}/{len(review_items)} candidates...", flush=True)
    imported = import_model_results(results_path, audits)
    print(f"Imported {imported} local LLM decisions from {results_path}")
    return results_path


def import_model_results(path: Path, audits: list[RescueAudit]) -> int:
    by_id = {item.message_id: item for item in audits}
    imported = 0
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            print(f"Skipping invalid model result line {line_number}", file=sys.stderr)
            continue
        message_id = str(row.get("message_id", ""))
        item = by_id.get(message_id)
        if not item:
            continue
        decision = str(row.get("decision", ""))
        reason = str(row.get("reason", ""))
        confidence = row.get("confidence", "")
        try:
            model_confidence = float(confidence)
        except (TypeError, ValueError):
            model_confidence = 0.0
        item.model_decision = decision
        item.model_confidence = model_confidence
        item.model_reason = f"{reason} (model confidence={confidence})" if confidence != "" else reason
        if decision == "rescue_review":
            item.recommended_action = "rescue_review"
            if "local_model_rescue_review" not in item.rescue_reasons:
                item.rescue_reasons.append("local_model_rescue_review")
        elif decision == "keep_trash" and item.deep_risk_score < 60:
            item.recommended_action = "keep_trash"
            item.keep_trash_reasons.append("local_model_keep_trash")
        imported += 1
    return imported


def esc(value: Any) -> str:
    return gmail_sorter.esc(value)


def render_list(values: list[str]) -> str:
    return "<br>".join(esc(value) for value in values)


def write_html_report(path: Path, audits: list[RescueAudit], summary: dict[str, Any]) -> None:
    rescue = [item for item in audits if item.recommended_action == "rescue_review"]
    top_domains = summary.get("top_rescue_domains", [])
    domain_rows = "".join(
        f"<tr><td>{esc(domain)}</td><td>{esc(count)}</td></tr>"
        for domain, count in top_domains[:30]
    )
    rows = []
    for item in rescue[:500]:
        rows.append(
            "<tr>"
            f"<td>{esc(item.deep_risk_score)}</td>"
            f"<td>{esc(item.original_confidence)}</td>"
            f"<td>{esc(item.review_label)}</td>"
            f"<td>{esc(item.date)}</td>"
            f"<td>{esc(item.sender)}</td>"
            f"<td>{esc(item.subject)}</td>"
            f"<td>{render_list(item.rescue_reasons)}</td>"
            f"<td>{render_list(item.keep_trash_reasons)}</td>"
            f"<td>{render_list(item.attachment_names)}</td>"
            f"<td>{esc(item.model_decision)}</td>"
            f"<td>{esc(item.model_reason)}</td>"
            "</tr>"
        )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Trash Rescue Audit</title>
<style>
body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 24px; color: #17202a; }}
h1, h2 {{ margin: 0 0 12px; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 16px 0 24px; }}
.card {{ border: 1px solid #d8dee4; border-radius: 8px; padding: 12px; background: #f8fafc; }}
.card strong {{ display: block; color: #57606a; font-size: 13px; }}
.card span {{ font-size: 26px; font-weight: 700; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0 28px; }}
th, td {{ border: 1px solid #d8dee4; padding: 8px; vertical-align: top; text-align: left; }}
th {{ background: #eef2f6; position: sticky; top: 0; }}
.note {{ color: #57606a; max-width: 900px; line-height: 1.45; }}
</style>
</head>
<body>
<h1>Trash Rescue Audit</h1>
<p class="note">Dry-run safety review of messages previously planned for Trash. Rescue candidates are not automatically correct; review them before emptying Gmail Trash.</p>
<div class="cards">
<section class="card"><strong>Audited</strong><span>{esc(summary["audited"])}</span></section>
<section class="card"><strong>Still In Trash</strong><span>{esc(summary["still_in_trash"])}</span></section>
<section class="card"><strong>Rescue Review</strong><span>{esc(summary["rescue_review"])}</span></section>
<section class="card"><strong>Keep Trash</strong><span>{esc(summary["keep_trash"])}</span></section>
<section class="card"><strong>Delete Ready</strong><span>{esc(summary["permanent_delete_ready"])}</span></section>
</div>
<h2>Top Rescue Domains</h2>
<table><thead><tr><th>Domain</th><th>Messages</th></tr></thead><tbody>{domain_rows}</tbody></table>
<h2>Rescue Candidates</h2>
<table><thead><tr>
<th>Risk</th><th>Original Confidence</th><th>Review Label</th><th>Date</th><th>Sender</th><th>Subject</th><th>Rescue Reasons</th><th>Keep-Trash Reasons</th><th>Attachments</th><th>Model</th><th>Model Reason</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>
</body></html>"""
    path.write_text(html, encoding="utf-8")


def get_or_create_labels(service: Any, names: list[str], retries: int, retry_sleep: float) -> dict[str, str]:
    return gmail_sorter.get_or_create_labels(service, names, retries, retry_sleep)


def build_gmail_modify_service(args: argparse.Namespace) -> Any:
    google_libs = gmail_sorter.load_google_libraries()
    _, _, _, build, _ = google_libs
    creds = gmail_sorter.get_credentials(
        Path(args.credentials).expanduser(),
        Path(args.token_modify).expanduser(),
        [gmail_sorter.MODIFY_SCOPE],
        args.open_browser,
        google_libs,
    )
    return gmail_sorter.build_gmail_service(build, creds, args)


def permanent_delete_candidates(audits: list[RescueAudit]) -> list[RescueAudit]:
    return [
        item
        for item in audits
        if (
            item.still_in_trash
            and item.recommended_action == "keep_trash"
            and item.script_delete_confidence == 100
            and item.model_decision == "keep_trash"
            and item.model_confidence >= 1.0
            and not item.has_real_attachment
            and not item.rescue_reasons
        )
    ]


def write_delete_manifest(out_prefix: Path, candidates: list[RescueAudit]) -> None:
    json_path = out_prefix.with_name(out_prefix.name + "_permanent_delete_manifest.json")
    csv_path = out_prefix.with_name(out_prefix.name + "_permanent_delete_manifest.csv")
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "message_count": len(candidates),
        "requirements": [
            "still_in_trash",
            "recommended_action == keep_trash",
            "script_delete_confidence == 100",
            "model_decision == keep_trash",
            "model_confidence >= 1.0",
            "no real attachments",
            "no rescue reasons",
        ],
        "items": [asdict(item) for item in candidates],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["message_id", "date", "sender", "subject", "original_confidence", "script_delete_confidence", "model_confidence", "model_reason"])
        writer.writeheader()
        for item in candidates:
            writer.writerow(
                {
                    "message_id": item.message_id,
                    "date": item.date,
                    "sender": item.sender,
                    "subject": item.subject,
                    "original_confidence": item.original_confidence,
                    "script_delete_confidence": item.script_delete_confidence,
                    "model_confidence": item.model_confidence,
                    "model_reason": item.model_reason,
                }
            )


def apply_permanent_deletes(service: Any, audits: list[RescueAudit], args: argparse.Namespace, out_prefix: Path) -> None:
    candidates = permanent_delete_candidates(audits)
    write_delete_manifest(out_prefix, candidates)
    if not candidates:
        print("No permanent-delete candidates met both 100% gates.")
        return
    for index, item in enumerate(candidates, 1):
        gmail_sorter.execute_with_retries(
            service.users().messages().delete(userId="me", id=item.message_id),
            args.retries,
            args.retry_sleep,
        )
        if index == 1 or index == len(candidates) or index % 100 == 0:
            print(f"Permanently deleted {index}/{len(candidates)} verified trash messages...", flush=True)


def apply_rescue_actions(service: Any, audits: list[RescueAudit], args: argparse.Namespace) -> None:
    rescue = [item for item in audits if item.recommended_action == "rescue_review" and item.still_in_trash]
    if not rescue:
        print("No rescue candidates to apply.")
        return
    label_ids = get_or_create_labels(service, sorted(set(item.review_label for item in rescue)), args.retries, args.retry_sleep)
    for index, item in enumerate(rescue, 1):
        if not args.label_only:
            gmail_sorter.execute_with_retries(
                service.users().messages().untrash(userId="me", id=item.message_id),
                args.retries,
                args.retry_sleep,
            )
        gmail_sorter.execute_with_retries(
            service.users().messages().modify(
                userId="me",
                id=item.message_id,
                body={"addLabelIds": [label_ids[item.review_label]]},
            ),
            args.retries,
            args.retry_sleep,
        )
        if index == 1 or index == len(rescue) or index % 50 == 0:
            action = "labeled" if args.label_only else "untrashed+labeled"
            print(f"Applied rescue action ({action}) {index}/{len(rescue)}...", flush=True)


def main() -> int:
    args = parse_args()
    if args.http_timeout > 0:
        socket.setdefaulttimeout(args.http_timeout)
    if args.apply and not args.i_understand_restore:
        print("Refusing apply without --i-understand-restore.", file=sys.stderr)
        return 2
    if args.delete_passed_trash and not args.i_understand_permanent_delete:
        print("Refusing permanent delete without --i-understand-permanent-delete.", file=sys.stderr)
        return 2

    out_prefix = Path(args.out_prefix)

    if args.from_audit_json:
        audits = load_existing_audit(Path(args.from_audit_json))
        if args.model_results:
            imported = import_model_results(Path(args.model_results), audits)
            print(f"Imported {imported} local-model decisions from {args.model_results}")
        if args.local_llm:
            run_local_llm_review(audits, out_prefix, args)
        if args.llm_export:
            write_llm_export(out_prefix, audits)
        write_reports(out_prefix, audits)
        if args.delete_passed_trash:
            service = build_gmail_modify_service(args)
            apply_permanent_deletes(service, audits, args, out_prefix)
        rescue_count = sum(1 for item in audits if item.recommended_action == "rescue_review")
        print(f"OFFLINE REPORT: audited={len(audits)} rescue_review={rescue_count} keep_trash={len(audits) - rescue_count}")
        print(f"Wrote {out_prefix.with_suffix('.html')}")
        print(f"Wrote {out_prefix.with_suffix('.csv')}")
        print(f"Wrote {out_prefix.with_suffix('.json')}")
        return 0

    candidates = load_trash_candidates(Path(args.progress_file), args.min_confidence, args.max_messages)
    print(f"Loaded {len(candidates)} planned-trash candidates from {args.progress_file}")

    google_libs = gmail_sorter.load_google_libraries()
    *_, HttpError = google_libs
    service = build_gmail_modify_service(args)

    audits: list[RescueAudit] = []
    skipped = 0
    missing_ids: list[str] = []
    throttle = gmail_sorter.AdaptiveThrottle(args.sleep)
    for index, decision in enumerate(candidates, 1):
        try:
            message = gmail_sorter.get_message_metadata(
                service,
                decision.message_id,
                args.retries,
                args.retry_sleep,
                throttle,
                include_attachment_details=True,
            )
        except HttpError as error:
            if is_missing_gmail_message_error(error):
                missing_ids.append(decision.message_id)
                if len(missing_ids) <= 5:
                    print(f"Skipping missing Gmail message {decision.message_id} (already deleted or stale progress entry).", file=sys.stderr)
                elif len(missing_ids) == 6:
                    print("Additional missing Gmail messages will be summarized instead of printed individually.", file=sys.stderr)
            else:
                print(f"Skipping {decision.message_id}: {error}", file=sys.stderr)
            skipped += 1
            continue
        audit = audit_message(decision, message, args.llm_body_chars)
        if audit.still_in_trash or args.include_not_in_trash:
            audits.append(audit)
        else:
            skipped += 1
        if args.checkpoint_every and len(audits) and len(audits) % args.checkpoint_every == 0:
            partial = Path(args.out_prefix).with_name(Path(args.out_prefix).name + "_partial")
            write_reports(partial, audits)
            if args.llm_export:
                write_llm_export(partial, audits)
        if index == 1 or index == len(candidates) or index % 100 == 0:
            print(f"Audited {index}/{len(candidates)} candidates; report rows={len(audits)}; skipped={skipped}; missing={len(missing_ids)}", flush=True)

    if missing_ids:
        missing_path = out_prefix.with_name(out_prefix.name + "_missing_gmail_ids.txt")
        missing_path.write_text("\n".join(missing_ids) + "\n", encoding="utf-8")
        print(f"Skipped {len(missing_ids)} missing Gmail message IDs. Wrote {missing_path}")

    if args.openai:
        openai_count = 0
        for item in sorted(audits, key=lambda row: row.deep_risk_score, reverse=True):
            if openai_count >= args.openai_max:
                break
            if not model_should_review(item):
                continue
            decision, reason, error = call_openai_reasoner(item, args.openai_model, args.web_search)
            item.model_decision = decision
            item.model_reason = reason
            item.model_error = error
            if decision == "rescue_review":
                item.recommended_action = "rescue_review"
                if "model_rescue_review" not in item.rescue_reasons:
                    item.rescue_reasons.append("model_rescue_review")
            elif decision == "keep_trash" and item.deep_risk_score < 60:
                item.recommended_action = "keep_trash"
                item.keep_trash_reasons.append("model_keep_trash")
            openai_count += 1
            time.sleep(0.05)
        print(f"OpenAI-reviewed {openai_count} candidates.")

    if args.model_results:
        imported = import_model_results(Path(args.model_results), audits)
        print(f"Imported {imported} local-model decisions from {args.model_results}")
    if args.local_llm:
        run_local_llm_review(audits, out_prefix, args)

    audits.sort(key=lambda item: (item.recommended_action == "rescue_review", item.deep_risk_score, item.original_confidence), reverse=True)
    write_reports(out_prefix, audits)
    if args.llm_export:
        write_llm_export(out_prefix, audits)

    rescue_count = sum(1 for item in audits if item.recommended_action == "rescue_review")
    print(f"DRY RUN: audited={len(audits)} rescue_review={rescue_count} keep_trash={len(audits) - rescue_count}")
    print(f"Wrote {out_prefix.with_suffix('.html')}")
    print(f"Wrote {out_prefix.with_suffix('.csv')}")
    print(f"Wrote {out_prefix.with_suffix('.json')}")
    print(f"Wrote {out_prefix.with_name(out_prefix.name + '_summary.json')}")

    if args.apply:
        apply_rescue_actions(service, audits, args)
    if args.delete_passed_trash:
        apply_permanent_deletes(service, audits, args, out_prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
