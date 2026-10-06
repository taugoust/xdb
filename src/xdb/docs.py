"""Anonymous AMD Fluid Topics documentation access; no FPGA tools required."""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from xdb.errors import XdbError

BASE_URL = "https://docs.amd.com/api/khub"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class DocsError(XdbError):
    """A documentation request could not be completed safely."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never follow a portal login or an unexpected external redirect.
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
    search = commands.add_parser("search", help="search topics; outputs JSON with source metadata")
    search.add_argument("query", help="query verbatim; retain double quotes for exact phrases")
    search.add_argument("--page", type=_positive_int, default=1)
    search.add_argument("--per-page", type=_positive_int, default=10)
    search.add_argument("--locale", default="en-US")
    read = commands.add_parser("read", help="read a topic as Markdown with provenance")
    read.add_argument("map_id")
    read.add_argument("topic_id")
    read.add_argument("--json", action="store_true", help="emit metadata and Markdown as JSON")
    toc = commands.add_parser("toc", help="retrieve the hierarchical table of contents as JSON")
    toc.add_argument("map_id")
    for command in (search, read, toc):
        command.add_argument(
            "--timeout", type=_timeout, default=30.0, help="per-request timeout in seconds"
        )


class AmdDocs:
    def __init__(self, timeout: float = 30.0):
        if not math.isfinite(timeout) or timeout <= 0:
            raise DocsError("timeout must be finite and positive")
        self.timeout = timeout
        self.opener = build_opener(_NoRedirect())

    def _request(self, path: str, *, body: dict | None = None, markdown: bool = False):
        headers = {
            "Ft-Calling-App": "xdb",
            "Accept": "text/markdown" if markdown else "application/json",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        request = Request(BASE_URL + path, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                expected = "text/markdown" if markdown else "application/json"
                if response.headers.get_content_type() != expected:
                    raise DocsError(
                        f"AMD returned unexpected content (expected {expected}); portal/API may have changed"
                    )
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise DocsError("AMD response exceeds the 16 MiB limit; narrow the request")
                text = raw.decode("utf-8")
                return text if markdown else json.loads(text)
        except HTTPError as exc:
            if exc.code in (401, 403):
                detail = "access denied; only anonymous public documentation is supported"
            elif exc.code == 404:
                detail = "not found; search again for the current map/topic IDs"
            elif exc.code == 429:
                detail = "rate limited; wait before retrying"
            else:
                detail = "request failed (redirects are not followed)"
            raise DocsError(f"AMD documentation HTTP {exc.code}: {detail}") from exc
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
        self, query: str, *, page: int = 1, per_page: int = 10, locale: str = "en-US"
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
        result = self._request("/topics/search", body=body)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("results"), list)
            or not isinstance(result.get("paging"), dict)
        ):
            raise DocsError("unexpected AMD search response schema")
        return self._envelope("/topics/search", request=body, response=result)

    def read(self, map_id: str, topic_id: str) -> dict:
        path = f"/maps/{_identifier(map_id)}/topics/{_identifier(topic_id)}"
        topic = self._request(path)
        if not isinstance(topic, dict) or not isinstance(topic.get("metadata"), list):
            raise DocsError("unexpected AMD topic response schema")
        markdown = self._request(path + "/content?format=markdown", markdown=True)
        return self._envelope(
            path, map_id=map_id, topic_id=topic_id, topic=topic, markdown=markdown
        )

    def toc(self, map_id: str) -> dict:
        path = f"/maps/{_identifier(map_id)}/toc"
        result = self._request(path)
        if not isinstance(result, list):
            raise DocsError("unexpected AMD TOC response schema")
        return self._envelope(path, map_id=map_id, topics=result)


def run_docs(args) -> None:
    client = AmdDocs(args.timeout)
    if args.docs_cmd == "search":
        result = client.search(
            args.query, page=args.page, per_page=args.per_page, locale=args.locale
        )
    elif args.docs_cmd == "toc":
        result = client.toc(args.map_id)
    else:
        result = client.read(args.map_id, args.topic_id)
        if not args.json:
            print(f"Source: {result['source_url']}\nRetrieved: {result['retrieved_at']}")
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
