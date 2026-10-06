import unittest
from email.message import Message
from unittest.mock import MagicMock
from urllib.error import HTTPError

from xdb.docs import AmdDocs, DocsError
from xdb.docs_html import topic_html_links, topic_html_to_markdown
from xdb.errors import XdbError


def reply(text, mime):
    result = MagicMock()
    result.headers = Message()
    result.headers["Content-Type"] = mime
    result.read.return_value = text.encode()
    result.__enter__.return_value = result
    return result


class DocsHtmlTests(unittest.TestCase):
    def test_link_extraction_relative_dedup_hidden_and_unsafe(self):
        source = '<div><p><a href="../guide#part">A &amp; B</a><a href="../guide#part">A &amp; B</a><script><a href="https://evil">hidden</a></script><nav><a href="https://evil">nav</a></nav><a href="javascript:bad">bad</a><table><tr><td><a href="https://adaptivesupport.amd.com/s/article/id">AR</a></td></tr></table></div>'
        links = topic_html_links(source, "https://docs.amd.com/content/topic")
        self.assertEqual(links, [
            {"text": "A & B", "url": "https://docs.amd.com/guide#part", "kind": "public-doc"},
            {"text": "AR", "url": "https://adaptivesupport.amd.com/s/article/id", "kind": "external-support"},
        ])

    def test_realistic_known_issues_fragment(self):
        fragment = '<div class="body conbody"><p><span>Vivado Design Suite</span> Tools Known Issues can be found at Answer Record <a href="https://adaptivesupport.amd.com/s/article/000037546?language=en_US">000037546</a>.</p></div>'
        text = topic_html_to_markdown(fragment, "https://docs.amd.com/content")
        self.assertIn("Vivado Design Suite Tools Known Issues", text)
        self.assertIn(
            "[000037546](<https://adaptivesupport.amd.com/s/article/000037546?language=en_US>)",
            text,
        )

    def test_tables_links_and_hidden_content(self):
        text = topic_html_to_markdown(
            '<h2>Bits &amp; fields</h2><script>secret</script><style>hidden</style><nav>menu</nav><table><tr><th>Bit</th><th>Description</th></tr><tr><td>0</td><td>A &amp; B <a href="/r/topic">reference</a></td></tr></table>',
            "https://docs.amd.com/content",
        )
        self.assertIn("## Bits & fields", text)
        self.assertIn("| Bit | Description |", text)
        self.assertIn("| 0 | A & B [reference](<https://docs.amd.com/r/topic>) |", text)
        for unwanted in ["secret", "hidden", "menu"]:
            self.assertNotIn(unwanted, text)

    def test_reject_empty_and_full_portal(self):
        for source in [
            "",
            "<script>app()</script>",
            "<html><body>Salesforce loading...</body></html>",
        ]:
            with self.assertRaises(XdbError):
                topic_html_to_markdown(source, "https://docs.amd.com/content")

    def test_unsafe_link_not_emitted(self):
        self.assertEqual(
            topic_html_to_markdown(
                '<p><a href="javascript:bad()">label</a></p>', "https://docs.amd.com/content"
            ),
            "label\n",
        )

    def test_fallback_preserves_provenance(self):
        for status in [404, 406, 415]:
            client = AmdDocs()
            client.opener = MagicMock()
            client.opener.open.side_effect = [
                reply('{"metadata":[]}', "application/json"),
                HTTPError("url", status, "error", Message(), None),
                reply("<p>Content</p>", "text/html"),
            ]
            result = client.read("map", "topic")
            self.assertEqual(result["markdown"], "Content\n")
            self.assertEqual(result["source_format"], "html")
            self.assertEqual(result["markdown_fallback_status"], status)
            self.assertTrue(result["content_url"].endswith("/content"))
            self.assertEqual(client.opener.open.call_count, 3)

    def test_no_fallback_for_denied_rate_limit_or_server_error(self):
        for status in [401, 403, 429, 500, 503]:
            client = AmdDocs()
            client.opener = MagicMock()
            client.opener.open.side_effect = [
                reply('{"metadata":[]}', "application/json"),
                HTTPError("url", status, "error", Message(), None),
            ]
            with self.assertRaises(DocsError):
                client.read("map", "topic")
            self.assertEqual(client.opener.open.call_count, 2)
