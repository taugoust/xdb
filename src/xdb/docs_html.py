"""Readable fallback for AMD topic HTML fragments, not full portal applications."""

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from xdb.errors import XdbError


class _TopicText(HTMLParser):
    def __init__(self, source_url: str):
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.parts: list[str] = []
        self.hidden: list[str] = []
        self.links: list[str] = []
        self.table = False
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.first_row = True
        self.full_page = False

    def emit(self, value: str):
        (self.cell if self.cell is not None else self.parts).append(value)

    def handle_starttag(self, tag, attrs):
        if tag == "html":
            self.full_page = True
        if tag in {"script", "style", "nav", "noscript", "head"}:
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            target = urljoin(self.source_url, href)
            safe = urlsplit(target).scheme in {"https", "http"} and bool(href)
            self.links.append(target if safe else "")
            if safe:
                self.emit("[")
        elif tag == "table":
            self.table, self.first_row = True, True
            self.emit("\n\n")
        elif tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            self.cell = []
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.emit("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in {"p", "div", "section", "ul", "ol", "pre", "blockquote"}:
            self.emit("\n\n")
        elif tag == "li":
            self.emit("\n- ")
        elif tag == "br":
            self.emit("\n")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag == "a" and self.links:
            target = self.links.pop()
            if target:
                # Angle brackets keep parentheses/spaces in URLs from breaking links.
                target = target.replace("<", "%3C").replace(">", "%3E").replace("\n", "")
                self.emit(f"](<{target}>)")
        elif tag in {"td", "th"} and self.cell is not None:
            text = " ".join("".join(self.cell).split()).replace("|", "\\|")
            self.cell = None
            if self.row is not None:
                self.row.append(text)
        elif tag == "tr" and self.row is not None:
            self.parts.append("| " + " | ".join(self.row) + " |\n")
            if self.first_row:
                self.parts.append("| " + " | ".join("---" for _ in self.row) + " |\n")
                self.first_row = False
            self.row = None
        elif tag == "table":
            self.table = False
            self.emit("\n\n")
        elif tag in {
            "p",
            "div",
            "section",
            "pre",
            "blockquote",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }:
            self.emit("\n\n")

    def handle_data(self, data):
        if not self.hidden:
            # Ignore source indentation between table rows, preserve spaces inline.
            if self.table and self.cell is None and not data.strip():
                return
            self.emit(data)


def topic_html_to_markdown(source: str, source_url: str) -> str:
    parser = _TopicText(source_url)
    parser.feed(source)
    parser.close()
    if parser.full_page:
        raise XdbError(
            "AMD returned a full HTML page rather than a topic fragment; article content was not retrieved"
        )
    text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines()).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if not text:
        raise XdbError(
            "AMD returned no readable topic content; this is not evidence of access denial"
        )
    return text + "\n"
