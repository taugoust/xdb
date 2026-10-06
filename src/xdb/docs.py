"""Anonymous AMD Fluid Topics documentation access; no FPGA tools required."""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import shlex
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from xdb.errors import XdbError
from xdb.docs_html import topic_html_to_markdown

BASE_URL = "https://docs.amd.com/api/khub"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class DocsError(XdbError):
    """A documentation request could not be completed safely."""


class _DocsHTTPError(DocsError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_~.-]+", value) or value in {".", ".."}:
        raise DocsError("expected an opaque map/topic ID from docs search, not a URL")
    return value


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _timeout(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and positive")
    return number


def add_docs_parser(subparsers) -> None:
    parser = subparsers.add_parser("docs", help="read public AMD documentation (no Vivado needed)")
    commands = parser.add_subparsers(dest="docs_cmd", required=True)
    search = commands.add_parser("search", help="search topics; compact results or full JSON")
    search.add_argument("query", help="query verbatim; retain double quotes for exact phrases")
    search.add_argument("--page", type=_positive_int, default=1)
    search.add_argument("--per-page", type=_positive_int, default=10)
    search.add_argument("--locale", default="en-US")
    search.add_argument("--document-id", help="server-side Document_ID facet")
    search.add_argument("--product", help="server-side Product facet")
    search.add_argument("--version", help="server-side Doc_Version facet")
    search.add_argument("--json", action="store_true", help="emit the complete metadata envelope")
    maps = commands.add_parser("maps", help="discover documents and maps")
    maps.add_argument("query", help="document/map search query")
    maps.add_argument("--page", type=_positive_int, default=1)
    maps.add_argument("--per-page", type=_positive_int, default=10)
    maps.add_argument("--json", action="store_true", help="emit the complete metadata envelope")
    read = commands.add_parser("read", help="read a topic as Markdown with provenance")
    read.add_argument("map_id")
    read.add_argument("topic_id")
    read.add_argument("--json", action="store_true", help="emit metadata and Markdown as JSON")
    toc = commands.add_parser("toc", help="browse a map table of contents")
    toc.add_argument("map_id")
    toc.add_argument("--filter", dest="toc_filter", help="case-insensitive title substring")
    toc.add_argument(
        "--limit", type=_positive_int, default=60, help="maximum matching topics to print"
    )
    toc.add_argument("--offset", type=int, default=0, help="skip this many matching topics")
    toc.add_argument("--json", action="store_true", help="emit the complete metadata envelope/tree")
    for command in (search, maps, read, toc):
        command.add_argument(
            "--timeout", type=_timeout, default=30.0, help="per-request timeout in seconds"
        )


class AmdDocs:
    def __init__(self, timeout: float = 30.0):
        if not math.isfinite(timeout) or timeout <= 0:
            raise DocsError("timeout must be finite and positive")
        self.timeout = timeout
        self.opener = build_opener(_NoRedirect())

    def _request(
        self,
        path: str,
        *,
        body: dict | None = None,
        markdown: bool = False,
        html_content: bool = False,
    ):
        headers = {
            "Ft-Calling-App": "xdb",
            "Accept": "text/html"
            if html_content
            else ("text/markdown" if markdown else "application/json"),
        }
        data = None if body is None else json.dumps(body).encode("utf-8")
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(BASE_URL + path, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                expected = (
                    "text/html"
                    if html_content
                    else ("text/markdown" if markdown else "application/json")
                )
                if response.headers.get_content_type() != expected:
                    raise DocsError(
                        f"AMD returned unexpected content (expected {expected}); portal/API may have changed"
                    )
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise DocsError("AMD response exceeds the 16 MiB limit; narrow the request")
                text = raw.decode("utf-8")
                return text if markdown or html_content else json.loads(text)
        except HTTPError as exc:
            if exc.code in (401, 403):
                detail = "access denied; only anonymous public documentation is supported"
            elif exc.code == 404:
                detail = "not found; search again for the current map/topic IDs"
            elif exc.code == 429:
                detail = "rate limited; wait before retrying"
            else:
                detail = "request failed (redirects are not followed)"
            raise _DocsHTTPError(exc.code, f"AMD documentation HTTP {exc.code}: {detail}") from exc
        except (URLError, OSError) as exc:
            raise DocsError(f"AMD documentation connection failed: {exc}") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DocsError("AMD returned invalid UTF-8/JSON; portal/API may have changed") from exc

    @staticmethod
    def _envelope(path: str, **payload) -> dict:
        return {
            "source_url": BASE_URL + path,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            **payload,
        }

    def search(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 10,
        locale: str = "en-US",
        document_id: str | None = None,
        product: str | None = None,
        version: str | None = None,
    ) -> dict:
        if not query.strip():
            raise DocsError("search query must not be empty")
        if page < 1 or not 1 <= per_page <= 100:
            raise DocsError("page must be positive and per-page must be between 1 and 100")
        body = {
            "query": query,
            "contentLocale": locale,
            "paging": {"page": page, "perPage": per_page},
        }
        filters = [
            {"key": key, "values": [value]}
            for key, value in (
                ("Document_ID", document_id),
                ("Product", product),
                ("Doc_Version", version),
            )
            if value is not None
        ]
        if filters:
            body["filters"] = filters
        result = self._request("/topics/search", body=body)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("results"), list)
            or not isinstance(result.get("paging"), dict)
        ):
            raise DocsError("unexpected AMD search response schema")
        return self._envelope("/topics/search", request=body, response=result)

    def maps(self, query: str, *, page: int = 1, per_page: int = 10) -> dict:
        if not query.strip() or page < 1 or not 1 <= per_page <= 100:
            raise DocsError("query must be nonempty; page positive and per-page between 1 and 100")
        body = {"query": query, "paging": {"page": page, "perPage": per_page}}
        result = self._request("/maps/search", body=body)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("results"), list)
            or not isinstance(result.get("paging"), dict)
        ):
            raise DocsError("unexpected AMD map search response schema")
        return self._envelope("/maps/search", request=body, response=result)

    def read(self, map_id: str, topic_id: str) -> dict:
        path = f"/maps/{_identifier(map_id)}/topics/{_identifier(topic_id)}"
        topic = self._request(path)
        if not isinstance(topic, dict) or not isinstance(topic.get("metadata"), list):
            raise DocsError("unexpected AMD topic response schema")
        content_path = path + "/content?format=markdown"
        source_format = "markdown"
        fallback_status = None
        try:
            markdown = self._request(content_path, markdown=True)
        except _DocsHTTPError as exc:
            if exc.status not in {404, 406, 415}:
                raise
            fallback_status = exc.status
            content_path = path + "/content"
            source_format = "html"
            content = self._request(content_path, html_content=True)
            markdown = topic_html_to_markdown(content, BASE_URL + content_path)
        return self._envelope(
            path,
            map_id=map_id,
            topic_id=topic_id,
            topic=topic,
            markdown=markdown,
            content_url=BASE_URL + content_path,
            source_format=source_format,
            markdown_fallback_status=fallback_status,
        )

    def toc(self, map_id: str) -> dict:
        path = f"/maps/{_identifier(map_id)}/toc"
        result = self._request(path)
        if not isinstance(result, list):
            raise DocsError("unexpected AMD TOC response schema")
        return self._envelope(path, map_id=map_id, topics=result)


def _metadata(item: dict, key: str) -> str:
    for value in item.get("metadata", []):
        if value.get("key") == key:
            return ", ".join(str(v) for v in value.get("values", []))
    return ""


def _render_search(result: dict, *, locale: str, timeout: float) -> None:
    response, request = result["response"], result["request"]
    paging = response["paging"]
    current = paging.get("currentPage", 1)
    print(
        f"Results: {paging.get('totalResultsCount', 'unknown')}; page {current}; last page: {paging.get('isLastPage', 'unknown')}"
    )
    if not paging.get("isLastPage", True):
        args = [
            "xdb",
            "docs",
            "search",
            request["query"],
            "--page",
            str(int(current) + 1),
            "--per-page",
            str(request["paging"]["perPage"]),
            "--locale",
            locale,
            "--timeout",
            str(timeout),
        ]
        for facet in request.get("filters", []):
            opt = {
                "Document_ID": "--document-id",
                "Product": "--product",
                "Doc_Version": "--version",
            }.get(facet["key"])
            if opt:
                args += [opt, facet["values"][0]]
        print("Next: " + shlex.join(args))
    for item in response["results"]:
        title = (
            html.unescape(re.sub(r"<[^>]*>", "", item.get("htmlTitle") or ""))
            or _metadata(item, "ft:title")
            or "(untitled)"
        )
        excerpt = html.unescape(re.sub(r"<[^>]*>", "", item.get("htmlExcerpt") or ""))
        occurrences = item.get("occurrences") or []
        breadcrumb = " > ".join(occurrences[0].get("breadcrumb", [])) if occurrences else ""
        map_id, topic_id = item.get("mapId", ""), item.get("contentId", "")
        print(
            f"\n{title} — {_metadata(item, 'Document_ID')} rev {_metadata(item, 'Doc_Version') or _metadata(item, 'Revision')}"
        )
        if breadcrumb:
            print(f"  {breadcrumb}")
        if excerpt:
            print(f"  {excerpt}")
        print(
            f"  {item.get('topicUrl', '')}\n  xdb docs read {shlex.quote(map_id)} {shlex.quote(topic_id)}"
        )


def _flatten_toc(
    nodes: list, ancestors: tuple[str, ...] = ()
) -> list[tuple[str, str, tuple[str, ...]]]:
    found = []
    for node in nodes:
        title = html.unescape(
            str(node.get("title") or node.get("name") or node.get("label") or "(untitled)")
        )
        topic_id = str(node.get("contentId") or node.get("topicId") or node.get("id") or "")
        chain = ancestors + (title,)
        found.append((title, topic_id, ancestors))
        children = node.get("children", [])
        if isinstance(children, list):
            found.extend(_flatten_toc(children, chain))
    return found


def _render_toc(topics: list, map_id: str, *, query: str | None, limit: int, offset: int) -> None:
    if offset < 0:
        raise DocsError("TOC offset must be zero or greater")
    flat = _flatten_toc(topics)
    matches = [entry for entry in flat if query is None or query.casefold() in entry[0].casefold()]
    print(f"TOC matches: {len(matches)}; offset {offset}; limit {limit}")
    end = min(offset + limit, len(matches))
    for title, topic_id, ancestors in matches[offset:end]:
        context = " > ".join((*ancestors, title))
        if topic_id:
            print(
                f"{context} [{topic_id}] — xdb docs read {shlex.quote(map_id)} {shlex.quote(topic_id)}"
            )
        else:
            print(context)
    if end < len(matches):
        args = ["xdb", "docs", "toc", map_id, "--limit", str(limit), "--offset", str(end)]
        if query is not None:
            args += ["--filter", query]
        print("Next: " + shlex.join(args))


def _render_maps(result: dict, query: str, timeout: float = 30.0) -> None:
    response = result["response"]
    p = response["paging"]
    print(f"Maps: {p.get('totalResultsCount', 'unknown')}; page {p.get('currentPage', 1)}")
    if not p.get("isLastPage", True):
        print(
            "Next: "
            + shlex.join(
                [
                    "xdb",
                    "docs",
                    "maps",
                    query,
                    "--page",
                    str(int(p["currentPage"]) + 1),
                    "--per-page",
                    str(result["request"]["paging"]["perPage"]),
                    "--timeout",
                    str(timeout),
                ]
            )
        )
    for item in response["results"]:
        meta = item.get("metadata", [])

        def val(key):
            for m in meta:
                if m.get("key") == key:
                    return ", ".join(m.get("values", []))
            return ""

        version = val("Doc_Version") or val("Revision")
        print(
            f"\n{item.get('title') or html.unescape(re.sub(r'<[^>]*>', '', item.get('htmlTitle') or ''))} [{item.get('mapId', '')}]"
        )
        print(f"  Document_ID: {val('Document_ID')}; Version: {version}; Product: {val('Product')}")
        print(f"  {item.get('mapUrl') or item.get('readerUrl') or ''}")
        print(f"  xdb docs toc {shlex.quote(item.get('mapId', ''))}")


def run_docs(args) -> None:
    client = AmdDocs(args.timeout)
    if args.docs_cmd == "search":
        result = client.search(
            args.query,
            page=args.page,
            per_page=args.per_page,
            locale=args.locale,
            document_id=args.document_id,
            product=args.product,
            version=args.version,
        )
        if not args.json:
            _render_search(result, locale=args.locale, timeout=args.timeout)
            return
    elif args.docs_cmd == "maps":
        result = client.maps(args.query, page=args.page, per_page=args.per_page)
        if not args.json:
            _render_maps(result, args.query, args.timeout)
            return
    elif args.docs_cmd == "toc":
        if args.offset < 0:
            raise DocsError("TOC offset must be zero or greater")
        result = client.toc(args.map_id)
        if not args.json:
            _render_toc(
                result["topics"],
                args.map_id,
                query=args.toc_filter,
                limit=args.limit,
                offset=args.offset,
            )
            return
    else:
        result = client.read(args.map_id, args.topic_id)
        if not args.json:
            print(f"Source: {result['source_url']}\nRetrieved: {result['retrieved_at']}")
            if result.get("source_format") == "html":
                print(
                    f"Format: converted from HTML (Markdown HTTP {result['markdown_fallback_status']})"
                )
            for item in result["topic"]["metadata"]:
                if item.get("key") in {
                    "Document_ID",
                    "Revision",
                    "Release_Date",
                    "ft:lastEdition",
                    "ft:prettyUrl",
                }:
                    print(f"{item['key']}: {', '.join(item.get('values', []))}")
            print("\n" + result["markdown"])
            return
    print(json.dumps(result, indent=2, ensure_ascii=False))
