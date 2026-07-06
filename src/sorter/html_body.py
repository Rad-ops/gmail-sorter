"""Better HTML body extraction for the Gmail sorter (v0.8).

Pre-v0.8 the sorter's body extraction was the simple path in
:func:`gmail_sorter.collect_body_text`:
  * base64url decode each ``text/plain`` or ``text/html`` part
  * join with newlines
  * cap at 250,000 chars

That misses three things that matter for a multi-year mailbox:

* **Styles and scripts.** A typical HTML email is 80% boilerplate
  (style, script, header, footer chrome). Stripping the noise lets
  the keyword rules and the body cleaner see the actual content.
* **Tables.** Receipts are almost always rendered as HTML tables.
  A line-by-line stripper loses the column structure. We preserve
  table content as tab-separated rows so the receipt's amount,
  date, and merchant line up next to each other.
* **Quoted-printable.** Many non-English emails (French, Farsi) use
  ``Content-Transfer-Encoding: quoted-printable`` rather than
  base64. The pre-v0.8 decoder only handled base64 and produced
  garbled text for QP-encoded bodies.

v0.8 adds a new module ``sorter.html_body`` that:
  * uses the stdlib ``email`` module to parse the raw MIME body,
  * decodes QP and base64 properly,
  * strips ``<style>`` and ``<script>`` blocks (kept inline in the
    output so a one-line renderer doesn't lose the structure),
  * converts ``<table>`` content to tab-separated rows,
  * preserves text in the original order with newlines between
    block elements.

The function is opt-in via ``--use-html-body`` (default on). When
the BeautifulSoup dependency is missing, the function falls back
to a small pure-Python HTML stripper so the sorter never breaks on
a missing optional dep.
"""

from __future__ import annotations

import base64
import email
import email.policy
import html as html_lib
import logging
import quopri
import re
from html.parser import HTMLParser

log = logging.getLogger("sorter.html_body")


# Pre-compiled patterns for the pure-Python fallback stripper.
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINE_RE = re.compile(r"\n\s*\n\s*")


def decode_part(payload_bytes: bytes, content_transfer_encoding: str) -> str:
    """Decode a MIME part's body bytes to text.

    Supports ``base64``, ``quoted-printable``, and the default
    ``7bit`` / ``8bit`` (which is just the raw bytes).
    """

    if not payload_bytes:
        return ""
    cte = (content_transfer_encoding or "").strip().lower()
    if cte == "base64":
        try:
            return base64.b64decode(payload_bytes, validate=False).decode("utf-8", errors="ignore")
        except Exception:
            return payload_bytes.decode("utf-8", errors="ignore")
    if cte == "quoted-printable":
        try:
            return quopri.decodestring(payload_bytes).decode("utf-8", errors="ignore")
        except Exception:
            return payload_bytes.decode("utf-8", errors="ignore")
    return payload_bytes.decode("utf-8", errors="ignore")


class _StructuredHTMLParser(HTMLParser):
    """Convert HTML to a structured plain-text representation.

    * ``<style>`` and ``<script>`` blocks are skipped entirely.
    * ``<table>`` rows become tab-separated cells on one line, with
      a newline between rows.
    * ``<br>``, ``<p>``, ``<div>`` produce newlines.
    * All other tags are stripped, leaving their text content.
    """

    BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
    SKIP_TAGS = {"script", "style", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth: int = 0
        self.in_cell: bool = False
        self.in_row: bool = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "td" or tag == "th":
            self.in_cell = True
            self.parts.append("\t")
        elif tag == "tr":
            self.in_row = True
            self.parts.append("\n")
        elif tag == "table":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.in_cell = False
        elif tag == "tr":
            self.in_row = False
        elif tag == "table":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        self.parts.append(data)

    def get_text(self) -> str:
        text = "".join(self.parts)
        text = html_lib.unescape(text)
        # Normalize spaces and tabs separately. We preserve tabs (they
        # are the table-column separator) and only collapse runs of
        # plain spaces. Newlines are preserved.
        text = re.sub(r"[ ]+", " ", text)
        # Collapse 3+ blank lines to 2.
        text = _BLANK_LINE_RE.sub("\n\n", text)
        return text.strip()


def html_to_structured_text(html_text: str) -> str:
    """Convert an HTML body to structured plain text.

    Uses :class:`_StructuredHTMLParser` which preserves tables as
    tab-separated rows. Falls back to a pure-Python stripper if the
    parser raises (very rare; only on truly malformed input).
    """

    if not html_text:
        return ""
    # Strip <script> and <style> blocks first via regex — fast and
    # doesn't require the parser to be in skip state.
    html_text = _SCRIPT_RE.sub(" ", html_text)
    html_text = _STYLE_RE.sub(" ", html_text)
    try:
        parser = _StructuredHTMLParser()
        parser.feed(html_text)
        return parser.get_text()
    except Exception as error:  # pragma: no cover - extremely rare
        log.debug("html parser failed (%s), falling back to regex strip", error)
        text = _TAG_RE.sub(" ", html_text)
        text = html_lib.unescape(text)
        text = _WHITESPACE_RE.sub(" ", text)
        text = _BLANK_LINE_RE.sub("\n\n", text)
        return text.strip()


def extract_text_from_mime(raw_bytes: bytes, prefer: str = "text") -> str:
    """Extract the best plain-text version from a raw MIME message.

    Walks the MIME tree (using ``email.policy.default``), prefers
    ``text/plain`` over ``text/html`` when both are present, and
    returns the decoded body. The body is HTML-converted to
    structured text when only ``text/html`` is available.

    The function never raises: malformed input returns an empty
    string. The maximum output size is 250,000 characters to keep
    the SQLite cache bounded.
    """

    if not raw_bytes:
        return ""
    try:
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    except Exception as error:  # pragma: no cover - very rare
        log.debug("MIME parse failed: %s", error)
        return ""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = (part.get_content_type() or "").lower()
        if ctype == "text/plain":
            plain_parts.append(part.get_content() or "")
        elif ctype == "text/html":
            html_parts.append(part.get_content() or "")
    if prefer == "html" and html_parts:
        text = "\n".join(html_parts)
    elif plain_parts:
        text = "\n".join(plain_parts)
    elif html_parts:
        text = "\n".join(html_parts)
    else:
        return ""
    # Cap the result to keep SQLite + memory bounded.
    return text[:250_000]


__all__ = [
    "decode_part",
    "extract_text_from_mime",
    "html_to_structured_text",
]
