from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from xdb.cli import main
from xdb.docs import AmdDocs, DocsError
from xdb.docs_html import topic_html_links
from xdb.errors import XdbError


class DocsRefsCliTests(unittest.TestCase):
    def test_refs_fetches_html_directly(self):
        client = AmdDocs()
        with patch.object(
            client,
            "_request",
            side_effect=[{"metadata": []}, '<p><a href="https://example.org/a">Example</a></p>'],
        ) as request:
            result = client.refs("map", "topic")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["links"][0]["kind"], "external")
        self.assertEqual(request.call_args.kwargs, {"html_content": True})

    def test_refs_rejects_portal_and_does_not_misclassify_hosts(self):
        with self.assertRaises(XdbError):
            topic_html_links('<html><a href="/login">Login</a></html>', "https://docs.amd.com/")
        links = topic_html_links(
            '<a href="https://docs.amd.com.evil.test/a">Fake</a>', "https://docs.amd.com/"
        )
        self.assertEqual(links[0]["kind"], "external")

    def test_refs_json_routes_and_emits_full_envelope(self):
        expected = {
            "source_url": "https://docs.amd.com/source",
            "retrieved_at": "now",
            "content_url": "https://docs.amd.com/content",
            "map_id": "map",
            "topic_id": "topic",
            "topic": {"metadata": [{"key": "Document_ID", "values": ["PG347"]}]},
            "links": [
                {"text": "manual", "url": "https://docs.amd.com/manual", "kind": "public-doc"}
            ],
        }
        with (
            patch("xdb.docs.AmdDocs") as client,
            patch("sys.argv", ["xdb", "docs", "refs", "map", "topic", "--json"]),
            redirect_stdout(io.StringIO()) as output,
        ):
            client.return_value.refs.return_value = expected
            main()
        self.assertEqual(json.loads(output.getvalue()), expected)
        client.return_value.refs.assert_called_once_with("map", "topic")

    def test_refs_default_includes_provenance_metadata_and_copyable_links(self):
        result = {
            "source_url": "https://docs.amd.com/source",
            "content_url": "https://docs.amd.com/content",
            "topic": {
                "metadata": [
                    {"key": "Document_ID", "values": ["UG973"]},
                    {"key": "Doc_Version", "values": ["2021.1 English"]},
                    {"key": "ft:prettyUrl", "values": ["2021.1-English/ug973"]},
                ]
            },
            "links": [
                {
                    "text": "000037546",
                    "url": "https://adaptivesupport.amd.com/s/article/000037546",
                    "kind": "external-support",
                }
            ],
        }
        with (
            patch("xdb.docs.AmdDocs") as client,
            patch("sys.argv", ["xdb", "docs", "refs", "map", "topic"]),
            redirect_stdout(io.StringIO()) as output,
        ):
            client.return_value.refs.return_value = result
            main()
        text = output.getvalue()
        for expected in [
            "Source: https://docs.amd.com/source",
            "Content: https://docs.amd.com/content",
            "UG973",
            "2021.1 English",
            "000037546",
            "https://adaptivesupport.amd.com/s/article/000037546",
            "external-support",
        ]:
            self.assertIn(expected, text)

    def test_refs_api_error_is_reported(self):
        with (
            patch("xdb.docs.AmdDocs") as client,
            patch("sys.argv", ["xdb", "docs", "refs", "map", "topic"]),
            patch("sys.stderr", new_callable=io.StringIO) as error,
            self.assertRaises(SystemExit) as raised,
        ):
            client.return_value.refs.side_effect = DocsError(
                "AMD documentation HTTP 403: access denied"
            )
            main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("403", error.getvalue())
        self.assertNotIn("unexpected internal error", error.getvalue())


if __name__ == "__main__":
    unittest.main()
