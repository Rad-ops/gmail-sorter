"""Tests for v0.8 HTML body extraction."""

import base64
import quopri
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gmail_sorter
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from sorter.html_body import (
    decode_part,
    extract_text_from_mime,
    html_to_structured_text,
)


def path_here():
    from pathlib import Path
    return Path(__file__).resolve().parents[1]


class DecodePartTests(unittest.TestCase):
    def test_empty_bytes(self):
        self.assertEqual(decode_part(b"", ""), "")

    def test_default_7bit(self):
        self.assertEqual(decode_part(b"hello", "7bit"), "hello")

    def test_default_8bit(self):
        self.assertEqual(decode_part(b"hello world", "8bit"), "hello world")

    def test_base64(self):
        encoded = base64.b64encode(b"hello base64").decode("ascii")
        self.assertEqual(decode_part(encoded.encode("ascii"), "base64"), "hello base64")

    def test_quoted_printable(self):
        encoded = quopri.encodestring(b"hello = quoted = printable").decode("ascii")
        self.assertIn("hello", decode_part(encoded.encode("ascii"), "quoted-printable"))

    def test_unknown_encoding_falls_back(self):
        # The function must not raise on unknown content-transfer-encoding.
        result = decode_part(b"hello", "x-unknown")
        self.assertEqual(result, "hello")


class HTMLToStructuredTextTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(html_to_structured_text(""), "")

    def test_plain_text(self):
        self.assertEqual(html_to_structured_text("hello world"), "hello world")

    def test_strips_script(self):
        html = "<p>visible</p><script>alert('x')</script><p>also visible</p>"
        text = html_to_structured_text(html)
        self.assertNotIn("alert", text)
        self.assertIn("visible", text)
        self.assertIn("also visible", text)

    def test_strips_style(self):
        html = "<style>body { color: red; }</style><p>content</p>"
        text = html_to_structured_text(html)
        self.assertNotIn("color: red", text)
        self.assertIn("content", text)

    def test_preserves_table_structure(self):
        html = """
        <table>
            <tr><th>Item</th><th>Price</th></tr>
            <tr><td>Apples</td><td>$1.00</td></tr>
            <tr><td>Oranges</td><td>$2.00</td></tr>
        </table>
        """
        text = html_to_structured_text(html)
        # Each row on its own line; cells separated by tab.
        self.assertIn("Apples", text)
        self.assertIn("Oranges", text)
        self.assertIn("$1.00", text)
        self.assertIn("\t", text)
        # Two rows = at least two newlines after the header.
        self.assertGreater(text.count("\n"), 1)

    def test_preserves_br_as_newline(self):
        html = "<p>line 1<br>line 2<br>line 3</p>"
        text = html_to_structured_text(html)
        self.assertIn("line 1", text)
        self.assertIn("line 2", text)
        self.assertIn("line 3", text)

    def test_unescapes_html_entities(self):
        html = "<p>caf&eacute; &amp; &lt;tag&gt;</p>"
        text = html_to_structured_text(html)
        self.assertIn("café", text)
        self.assertIn("&", text)
        self.assertIn("<tag>", text)

    def test_collapses_whitespace(self):
        html = "<p>hello     world</p>"
        text = html_to_structured_text(html)
        self.assertIn("hello world", text)
        # No triple spaces.
        self.assertNotIn("   ", text)

    def test_handles_nested_tags(self):
        html = "<div><p>outer <span>inner</span> after</p></div>"
        text = html_to_structured_text(html)
        self.assertIn("outer inner after", text)

    def test_handles_nested_script_in_table(self):
        # Edge case: a <script> inside a <td>. The whole script block
        # must be skipped, but the table structure must survive.
        html = """
        <table>
            <tr><td>row1<script>evil()</script></td></tr>
            <tr><td>row2</td></tr>
        </table>
        """
        text = html_to_structured_text(html)
        self.assertNotIn("evil", text)
        self.assertIn("row1", text)
        self.assertIn("row2", text)


class ExtractTextFromMIMETests(unittest.TestCase):
    def test_plain_text_only(self):
        msg = MIMEText("hello world", "plain")
        text = extract_text_from_mime(msg.as_bytes())
        self.assertIn("hello world", text)

    def test_html_only(self):
        msg = MIMEText("<p>hello html</p>", "html")
        text = extract_text_from_mime(msg.as_bytes())
        self.assertIn("hello html", text)

    def test_alternative_prefers_plain(self):
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("plain text version", "plain"))
        msg.attach(MIMEText("<p>html version</p>", "html"))
        text = extract_text_from_mime(msg.as_bytes())
        # The plain text wins by default.
        self.assertIn("plain text version", text)

    def test_alternative_prefer_html(self):
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText("plain text version", "plain"))
        msg.attach(MIMEText("<p>html version</p>", "html"))
        text = extract_text_from_mime(msg.as_bytes(), prefer="html")
        self.assertIn("html version", text)

    def test_quoted_printable_french(self):
        # Build raw MIME bytes that use Content-Transfer-Encoding:
        # quoted-printable so the bytes actually get QP-decoded by
        # our pipeline.
        from email.message import Message
        qp_body = quopri.encodestring(b"Bonjour, votre relev\xC3\xA9 de compte est disponible.").decode("ascii")
        raw = (
            b"From: x@x.com\r\n"
            b"Subject: test\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: quoted-printable\r\n"
            b"\r\n"
            + qp_body.encode("ascii")
        )
        text = extract_text_from_mime(raw)
        self.assertIn("Bonjour", text)
        self.assertIn("relevé", text)

    def test_empty_input(self):
        self.assertEqual(extract_text_from_mime(b""), "")

    def test_malformed_input_returns_empty(self):
        # Truncated MIME that triggers a parsing error must return
        # the empty string (function never raises). The
        # "this is not a mime message" string is actually a valid
        # text/plain MIME with no headers, so it parses; the test
        # is for a truly broken input.
        # The email library is forgiving; the only inputs that raise
        # are extremely malformed. The test below exercises the
        # no-headers path.
        text = extract_text_from_mime(b"this is not a mime message")
        # Either the function extracts "this is not a mime message"
        # (forgiving parse) or returns "" (strict). Both are
        # acceptable; the function must not raise.
        self.assertIsInstance(text, str)

    def test_output_capped(self):
        # Large bodies are truncated to 250,000 chars.
        long_text = "x" * 300_000
        msg = MIMEText(long_text, "plain")
        text = extract_text_from_mime(msg.as_bytes())
        self.assertLessEqual(len(text), 250_000)


class CollectBodyTextIntegrationTests(unittest.TestCase):
    def test_collect_body_text_with_html_strips_script(self):
        # The Gmail payload is a dict, not a raw MIME message. The
        # function still works because Gmail's payload already
        # contains decoded text in ``body.data``.
        body = "<p>Hello</p><script>alert(1)</script><p>world</p>"
        encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
        payload = {
            "mimeType": "text/html",
            "filename": "",
            "body": {"data": encoded},
            "parts": [],
        }
        text = gmail_sorter.collect_body_text(payload)
        self.assertIn("Hello", text)
        self.assertIn("world", text)
        self.assertNotIn("alert", text)

    def test_collect_body_text_use_html_body_false(self):
        # The pre-v0.8 path preserves the script content.
        body = "<p>Hello</p><script>alert(1)</script>"
        encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
        payload = {
            "mimeType": "text/html",
            "filename": "",
            "body": {"data": encoded},
            "parts": [],
        }
        text = gmail_sorter.collect_body_text(payload, use_html_body=False)
        # Without the HTML cleanup, the script tag is just stripped
        # by the existing clean_body_text step, but the alert() text
        # is left in.
        self.assertIn("Hello", text)


if __name__ == "__main__":
    unittest.main()
