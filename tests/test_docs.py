from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from xdb.cli import main
from xdb.cli_parser import build_parser
from xdb.docs import AmdDocs, DocsError, _NoRedirect, _render_maps, _render_search, _render_toc


def response(payload, content_type="application/json"):
    result = MagicMock()
    result.headers = Message()
    result.headers["Content-Type"] = content_type
    result.read.return_value = (
        json.dumps(payload).encode() if content_type == "application/json" else payload.encode()
    )
    result.__enter__.return_value = result
    return result


class DocsTest(unittest.TestCase):
    def setUp(self):
        self.client = AmdDocs()
        self.client.opener = MagicMock()

    def test_search_preserves_query_metadata_and_paging(self):
        payload = {
            "results": [
                {
                    "mapId": "map",
                    "contentId": "topic",
                    "metadata": [{"key": "Revision", "values": ["1.6"]}],
                }
            ],
            "paging": {"currentPage": 2, "isLastPage": False},
        }
        self.client.opener.open.return_value = response(payload)
        result = self.client.search('"CPM5"', page=2, per_page=3)
        request = self.client.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Ft-calling-app"), "xdb")
        self.assertEqual(
            json.loads(request.data),
            {"query": '"CPM5"', "contentLocale": "en-US", "paging": {"page": 2, "perPage": 3}},
        )
        self.assertEqual(result["response"], payload)
        self.assertIn("retrieved_at", result)

    def test_facet_filters_are_server_side_array(self):
        self.client.opener.open.return_value = response(
            {"results": [], "paging": {"currentPage": 1}}
        )
        self.client.search(
            "reset", document_id="PG347", product="cpm-dma-bridge", version="3.0 English"
        )
        body = json.loads(self.client.opener.open.call_args.args[0].data)
        self.assertEqual(body["query"], "reset")
        self.assertEqual(
            body["filters"],
            [
                {"key": "Document_ID", "values": ["PG347"]},
                {"key": "Product", "values": ["cpm-dma-bridge"]},
                {"key": "Doc_Version", "values": ["3.0 English"]},
            ],
        )

    def test_search_renderer_unescapes_quotes_and_next_page_command(self):
        output = io.StringIO()
        item = {
            "htmlTitle": "<b>A &amp; B</b>",
            "htmlExcerpt": "Use &quot;x y&quot;",
            "mapId": "m",
            "contentId": "t",
            "topicUrl": "https://docs/topic",
            "occurrences": [{"breadcrumb": ["Parent", "Child"]}],
            "metadata": [
                {"key": "Document_ID", "values": ["PG347"]},
                {"key": "Doc_Version", "values": ["3.0"]},
            ],
        }
        with redirect_stdout(output):
            _render_search(
                {
                    "request": {
                        "query": "x y",
                        "paging": {"perPage": 5},
                        "filters": [{"key": "Product", "values": ["x y"]}],
                    },
                    "response": {
                        "paging": {"currentPage": 1, "totalResultsCount": 9, "isLastPage": False},
                        "results": [item | {"occurrences": []}],
                    },
                },
                locale="fr-FR",
                timeout=12.5,
            )
        text = output.getvalue()
        self.assertIn("A & B — PG347 rev 3.0", text)
        self.assertIn('Use "x y"', text)
        self.assertNotIn("Parent > Child", text)
        self.assertNotIn("Traceback", text)
        self.assertIn("--page 2", text)
        self.assertIn("'x y'", text)
        self.assertIn("xdb docs read m t", text)
        self.assertIn("--locale fr-FR", text)
        self.assertIn("--timeout 12.5", text)
        self.assertIn("--product 'x y'", text)

    def test_toc_is_bounded_and_keeps_ancestor(self):
        output = io.StringIO()
        tree = [
            {
                "title": "Parent",
                "contentId": "p",
                "children": [{"title": f"Child {n}", "contentId": f"c{n}"} for n in range(5)],
            }
        ]
        with redirect_stdout(output):
            _render_toc(tree, "map", query="child", limit=2, offset=1)
        self.assertIn("Parent > Child 1", output.getvalue())
        self.assertIn("Next:", output.getvalue())
        self.assertEqual(output.getvalue().count("xdb docs read"), 2)

    def test_maps_render_document_version_product_and_direct_toc(self):
        output = io.StringIO()
        payload = {
            "response": {
                "paging": {"currentPage": 1, "totalResultsCount": 1},
                "results": [
                    {
                        "mapId": "map",
                        "title": "Guide",
                        "metadata": [
                            {"key": "Document_ID", "values": ["PG347"]},
                            {"key": "Doc_Version", "values": ["3.0 English"]},
                            {"key": "Product", "values": ["CPM"]},
                        ],
                    }
                ],
            }
        }
        with redirect_stdout(output):
            _render_maps(payload, "PG347")
        text = output.getvalue()
        self.assertIn("PG347", text)
        self.assertIn("3.0 English", text)
        self.assertIn("CPM", text)
        self.assertIn("xdb docs toc map", text)

    def test_empty_search_accepts_zero_current_page(self):
        self.client.opener.open.return_value = response(
            {
                "results": [],
                "paging": {"currentPage": 0, "isLastPage": True, "totalResultsCount": 0},
            }
        )
        self.assertEqual(self.client.search("absent")["response"]["results"], [])

    def test_read_markdown_and_revision(self):
        topic = {"metadata": [{"key": "Revision", "values": ["1.6"]}]}
        self.client.opener.open.side_effect = [
            response(topic),
            response("# Register\n| Bits | Description |", "text/markdown"),
        ]
        result = self.client.read("map~id", "topic_id")
        self.assertEqual(result["topic"], topic)
        self.assertTrue(result["markdown"].startswith("# Register"))
        self.assertTrue(
            self.client.opener.open.call_args.args[0].full_url.endswith("/content?format=markdown")
        )

    def test_toc_keeps_nested_ids(self):
        tree = [{"contentId": "parent", "children": [{"contentId": "child"}]}]
        self.client.opener.open.return_value = response(tree)
        self.assertEqual(self.client.toc("map")["topics"], tree)

    def test_invalid_input_does_not_request(self):
        for identifier in ["..", "a/b", "https://elsewhere", "a?b", "a%2fb", ""]:
            with self.subTest(identifier=identifier), self.assertRaises(DocsError):
                self.client.toc(identifier)
        for kwargs in [{"query": " "}, {"query": "x", "page": 0}, {"query": "x", "per_page": 101}]:
            with self.assertRaises(DocsError):
                self.client.search(**kwargs)
        self.client.opener.open.assert_not_called()

    def test_http_errors(self):
        for code, message in [
            (401, "anonymous"),
            (403, "anonymous"),
            (404, "not found"),
            (429, "rate limited"),
            (302, "redirects"),
            (503, "failed"),
        ]:
            self.client.opener.open.side_effect = HTTPError(
                "https://docs.amd.com", code, "error", Message(), None
            )
            with self.subTest(code=code), self.assertRaisesRegex(DocsError, message):
                self.client.toc("map")

    def test_network_and_bad_content(self):
        self.client.opener.open.side_effect = URLError("offline")
        with self.assertRaisesRegex(DocsError, "connection"):
            self.client.toc("map")
        self.client.opener.open.side_effect = None
        for reply, message in [
            (response("login", "text/html"), "unexpected content"),
            (response({}, "application/json"), "schema"),
        ]:
            self.client.opener.open.return_value = reply
            with self.assertRaisesRegex(DocsError, message):
                self.client.toc("map")
        reply = response({})
        reply.read.return_value = b"invalid json"
        self.client.opener.open.return_value = reply
        with self.assertRaisesRegex(DocsError, "invalid UTF-8/JSON"):
            self.client.toc("map")
        with patch("xdb.docs.MAX_RESPONSE_BYTES", 2), self.assertRaisesRegex(DocsError, "limit"):
            self.client.toc("map")

    def test_redirects_are_not_followed(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://other"))

    def test_cli_routes_without_hardware(self):
        cases = [
            (["search", "CPM5", "--json"], "search", {"response": {"results": []}}),
            (["toc", "map", "--json"], "toc", {"topics": []}),
            (["read", "map", "topic", "--json"], "read", {"markdown": "# Topic"}),
        ]
        for args, method, payload in cases:
            with (
                self.subTest(args=args),
                patch("xdb.docs.AmdDocs") as client,
                patch("sys.argv", ["xdb", "docs", *args]),
                redirect_stdout(io.StringIO()) as out,
            ):
                getattr(client.return_value, method).return_value = payload
                main()
                self.assertEqual(json.loads(out.getvalue()), payload)

    def test_cli_read_text(self):
        with (
            patch("xdb.docs.AmdDocs") as client,
            patch("sys.argv", ["xdb", "docs", "read", "map", "topic"]),
            redirect_stdout(io.StringIO()) as out,
        ):
            client.return_value.read.return_value = {
                "source_url": "https://docs.amd.com/source",
                "retrieved_at": "now",
                "topic": {"metadata": [{"key": "Revision", "values": ["1.6"]}]},
                "markdown": "# Topic",
            }
            main()
            self.assertIn("Revision: 1.6", out.getvalue())
            self.assertIn("# Topic", out.getvalue())

    def test_cli_error_is_actionable(self):
        with (
            patch("xdb.docs.AmdDocs") as client,
            patch("sys.argv", ["xdb", "docs", "toc", "map"]),
            patch("sys.stderr", new_callable=io.StringIO) as err,
            self.assertRaises(SystemExit) as raised,
        ):
            client.return_value.toc.side_effect = DocsError(
                "AMD documentation HTTP 403: access denied"
            )
            main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("403", err.getvalue())
        self.assertNotIn("unexpected internal error", err.getvalue())

    def test_cli_rejects_invalid_timeout(self):
        for timeout in ["0", "-1", "nan", "inf"]:
            with (
                self.subTest(timeout=timeout),
                patch("sys.stderr", io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                build_parser().parse_args(["docs", "toc", "map", "--timeout", timeout])
