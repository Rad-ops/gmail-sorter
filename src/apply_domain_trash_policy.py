#!/usr/bin/env python3
"""Apply a narrow, user-approved permanent-delete policy to Trash audit rows.

This helper is intentionally late in the workflow. It starts from an already
reviewed rescue-audit JSON file, filters only domains the user approved as
obvious trash, writes a manifest, and requires explicit flags before Gmail
permanent delete is called.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import gmail_sorter
import trash_rescue_audit


PROJECT_DIR = Path(__file__).resolve().parents[1]
INSUFFICIENT_SCOPE_MARKERS = ("insufficientPermissions", "insufficient authentication scopes")

# This list is not model-generated policy. It is the reviewed domain allowlist
# for one cleanup pass, and each row still has to pass the checks below.
DELETE_DOMAINS = {
    "bestbuy.ca",
    "adidas.com",
    "manutd.com",
    "adobe.com",
    "shoppersdrugmart.ca",
    "calvinklein.com",
    "gog.com",
    "yconic.com",
    "studentlifenetwork.com",
    "vans.com",
    "nordstrom.com",
    "aldoshoes.com",
    "sunglasshut.com",
    "starbucks.com",
    "technolife.ir",
    "square-enix.com",
    "ea.com",
    "benefitcosmetics.com",
    "walmart.ca",
    "newegg.ca",
    "tripadvisor.com",
    "aircanadavacations.com",
    "lenovo.com",
    "isaca.org",
    "staples.ca",
    "loanconnect.ca",
}

# Durable records are the kinds of things that make an otherwise promotional
# sender worth keeping: receipts, account alerts, bookings, security notices,
# payment records, and similar evidence.
DURABLE_PATTERN = re.compile(
    r"""
    \b(
      order\s*(?:id|[#]|number|no\.?|confirmation)
      |receipt|invoice
      |tracking\s*(?:number|[#])?
      |shipped|shipment|delivered|delivery\s*(?:confirmation|status)
      |ticket(?:s)?\s*(?:are|is)?\s*(?:waiting|confirmed|attached|ready)
      |booking\s*(?:confirmation|number|reference)
      |reservation\s*(?:confirmation|number)
      |itinerary|boarding\s*pass
      |refund\s*(?:issued|processed|confirmation)
      |return\s*(?:request|label|authorization|confirmation)
      |rma
      |payment\s*(?:received|confirmation|failed|due)
      |statement\s*(?:available|ready)
      |password|login|sign[- ]?in
      |security\s*(?:alert|code|notice|update)
      |verification\s*code|2fa|two[- ]factor
      |account\s*(?:alert|locked|change|updated|statement|notice)
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def parse_args() -> argparse.Namespace:
    """Define the manifest, apply, and verification modes."""

    parser = argparse.ArgumentParser(description="Apply user-approved permanent-delete policy for obvious Trash domains.")
    parser.add_argument("--audit-json", default=str(PROJECT_DIR / "reports" / "trash_rescue_audit_qwen36.json"))
    parser.add_argument("--out-prefix", default=str(PROJECT_DIR / "reports" / "domain_policy_obvious_trash"))
    parser.add_argument("--credentials", default=str(PROJECT_DIR / "secrets" / "credentials.json"))
    parser.add_argument("--token-delete", default=str(PROJECT_DIR / "secrets" / "token_sorter_delete.json"))
    parser.add_argument("--http-timeout", type=float, default=180.0)
    parser.add_argument("--retries", type=int, default=8)
    parser.add_argument("--retry-sleep", type=float, default=8.0)
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--i-understand-permanent-delete", action="store_true")
    parser.add_argument("--verify-delete-status", action="store_true", help="Check manifest message IDs and report whether Gmail still returns them.")
    parser.add_argument("--verify-limit", type=int, default=200, help="Maximum manifest rows to verify; use 0 for all rows.")
    return parser.parse_args()


def row_domain(row: dict[str, Any]) -> str:
    """Prefer registered domains so subdomains group under one policy entry."""

    return str(row.get("registered_domain") or row.get("sender_domain") or "")


def row_text(row: dict[str, Any]) -> str:
    """Collect the fields used for durable-record detection."""

    return " ".join(
        str(row.get(key, ""))
        for key in [
            "subject",
            "snippet",
            "body_excerpt",
            "sender",
        ]
    )


def should_delete(row: dict[str, Any]) -> tuple[bool, str]:
    """Return whether one audited row is allowed into the delete manifest."""

    domain = row_domain(row)
    if domain not in DELETE_DOMAINS:
        return False, "domain_not_in_policy"
    if not row.get("still_in_trash"):
        return False, "not_in_trash"
    if row.get("has_real_attachment"):
        return False, "has_real_attachment"
    if DURABLE_PATTERN.search(row_text(row)):
        return False, "durable_record_signal"
    return True, "listed_domain_no_attachment_no_durable_signal"


def write_jsonl_for_qwen(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write the held-back rows that still need model/human review."""

    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            audit = trash_rescue_audit.RescueAudit(**row)
            file.write(json.dumps(trash_rescue_audit.llm_packet(audit), ensure_ascii=False) + "\n")


def build_service(args: argparse.Namespace) -> Any:
    """Build a Gmail client with full mail scope for permanent delete calls."""

    google_libs = gmail_sorter.load_google_libraries()
    _, _, _, build, _ = google_libs
    creds = gmail_sorter.get_credentials(
        Path(args.credentials).expanduser(),
        Path(args.token_delete).expanduser(),
        [gmail_sorter.MAIL_SCOPE],
        args.open_browser,
        google_libs,
    )
    return gmail_sorter.build_gmail_service(build, creds, args)


def is_insufficient_scope_error(error: Exception) -> bool:
    """Detect when a modify-only OAuth token was reused for delete."""

    text = str(error)
    return any(marker in text for marker in INSUFFICIENT_SCOPE_MARKERS)


def is_missing_message_error(error: Exception) -> bool:
    """Treat missing messages during verification as already gone."""

    response = getattr(error, "resp", None)
    if getattr(response, "status", None) == 404:
        return True
    text = str(error).lower()
    return "requested entity was not found" in text or "not found" in text


def verify_delete_status(args: argparse.Namespace, manifest_path: Path) -> int:
    """Check whether Gmail still returns messages from a delete manifest."""

    if not manifest_path.exists():
        print(f"Missing manifest: {manifest_path}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = list(manifest.get("items", []))
    if args.verify_limit > 0:
        rows = rows[: args.verify_limit]

    service = build_service(args)
    deleted = 0
    still_visible = 0
    errors = 0
    visible_examples: list[str] = []
    for index, row in enumerate(rows, 1):
        message_id = row.get("message_id")
        try:
            gmail_sorter.execute_with_retries(
                service.users().messages().get(userId="me", id=message_id, format="minimal"),
                args.retries,
                args.retry_sleep,
            )
        except Exception as error:
            if is_missing_message_error(error):
                deleted += 1
            else:
                errors += 1
                print(f"Verify failed for {message_id}: {error}", file=sys.stderr)
            continue
        still_visible += 1
        if len(visible_examples) < 10:
            visible_examples.append(str(message_id))
        if index == 1 or index == len(rows) or index % 100 == 0:
            print(f"Verified {index}/{len(rows)} manifest messages...", flush=True)

    print(f"Verified manifest rows: {len(rows)}")
    print(f"Deleted or no longer returned by Gmail: {deleted}")
    print(f"Still returned by Gmail: {still_visible}")
    if visible_examples:
        print("Still-visible examples: " + ", ".join(visible_examples))
    if errors:
        print(f"Verification completed with {errors} unexpected errors.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    """Build, apply, or verify the reviewed obvious-trash domain policy."""

    args = parse_args()
    if args.http_timeout > 0:
        socket.setdefaulttimeout(args.http_timeout)
    if args.apply and not args.i_understand_permanent_delete:
        print("Refusing permanent delete without --i-understand-permanent-delete.", file=sys.stderr)
        return 2

    audit_path = Path(args.audit_json)
    out_prefix = Path(args.out_prefix)
    rows = json.loads(audit_path.read_text(encoding="utf-8"))

    delete_rows: list[dict[str, Any]] = []
    keep_rows: list[dict[str, Any]] = []
    held_reasons: dict[str, int] = {}
    for row in rows:
        # The domain allowlist is only the first gate. Attachments and durable
        # record wording still hold a message back for Qwen/human review.
        should, reason = should_delete(row)
        if should:
            delete_rows.append(row)
        else:
            keep_rows.append(row)
            if row_domain(row) in DELETE_DOMAINS:
                held_reasons[reason] = held_reasons.get(reason, 0) + 1

    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = out_prefix.with_name(out_prefix.name + "_manifest.json")
    manifest_csv = out_prefix.with_name(out_prefix.name + "_manifest.csv")
    remaining_json = out_prefix.with_name(out_prefix.name + "_remaining_for_qwen.json")
    remaining_jsonl = out_prefix.with_name(out_prefix.name + "_remaining_for_qwen_llm_input.jsonl")

    if args.verify_delete_status:
        return verify_delete_status(args, manifest_path)

    # Write the manifest before apply so the local machine has a paper trail of
    # exactly what qualified and why.
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_audit_json": str(audit_path),
        "apply": bool(args.apply),
        "delete_count": len(delete_rows),
        "remaining_for_qwen_count": len(keep_rows),
        "delete_domains": sorted(DELETE_DOMAINS),
        "requirements": [
            "domain is in user-approved obvious-trash domain list",
            "still_in_trash",
            "no real attachment",
            "no durable record signal in sender/subject/snippet/body excerpt",
        ],
        "held_reasons": held_reasons,
        "items": delete_rows,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    with manifest_csv.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["message_id", "date", "domain", "sender", "subject"])
        writer.writeheader()
        for row in delete_rows:
            writer.writerow(
                {
                    "message_id": row.get("message_id"),
                    "date": row.get("date"),
                    "domain": row_domain(row),
                    "sender": row.get("sender"),
                    "subject": row.get("subject"),
                }
            )
    remaining_json.write_text(json.dumps(keep_rows, indent=2, ensure_ascii=False), encoding="utf-8")
    write_jsonl_for_qwen(remaining_jsonl, keep_rows)

    print(f"Policy delete candidates: {len(delete_rows)}")
    print(f"Remaining for Qwen: {len(keep_rows)}")
    print(f"Wrote {manifest_path}")
    print(f"Wrote {manifest_csv}")
    print(f"Wrote {remaining_json}")
    print(f"Wrote {remaining_jsonl}")

    if not args.apply:
        print("DRY RUN: no Gmail messages permanently deleted.")
        return 0

    service = build_service(args)
    errors = 0
    for index, row in enumerate(delete_rows, 1):
        try:
            gmail_sorter.execute_with_retries(
                service.users().messages().delete(userId="me", id=row["message_id"]),
                args.retries,
                args.retry_sleep,
            )
        except Exception as error:
            if is_insufficient_scope_error(error):
                print(
                    "Permanent delete failed because the Gmail token lacks the full mail scope. "
                    f"Delete or reauthorize {args.token_delete} with scope {gmail_sorter.MAIL_SCOPE}.",
                    file=sys.stderr,
                )
                return 3
            errors += 1
            print(f"Delete failed for {row.get('message_id')}: {error}", file=sys.stderr)
            continue
        if index == 1 or index == len(delete_rows) or index % 100 == 0:
            print(f"Permanently deleted {index}/{len(delete_rows)} policy trash messages...", flush=True)
        if args.sleep:
            time.sleep(args.sleep)
    if errors:
        print(f"Completed with {errors} delete errors.", file=sys.stderr)
        return 1
    print(f"Completed permanent delete of {len(delete_rows)} policy trash messages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
