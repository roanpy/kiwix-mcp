from __future__ import annotations

import asyncio
import base64
from copy import copy
import fcntl
import hashlib
import json
import logging
import mimetypes
import os
import posixpath
import sys
import tempfile
from functools import lru_cache
from itertools import chain
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup
from libzim import Archive, Query, Searcher, SuggestionSearcher
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    ImageContent,
    ListToolsResult,
    ListResourceTemplatesResult,
    ListResourcesResult,
    ReadResourceResult,
    TextContent,
    Tool,
    ToolAnnotations,
    Resource,
    ResourceTemplate,
    TextResourceContents,
)

DEFAULT_ARCHIVE_DIR = Path.home() / ".chroma_db" / "kiwix" / "archives"
IMAGE_TEMP_DIR = Path(tempfile.gettempdir()) / "kiwix-mcp"
MAX_ARTICLE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TEMP_IMAGES = 16
MAX_IMAGE_REFERENCES = 30
MAX_RESOURCE_CHARS = 50_000
MAX_LEAD_CHARS = 2_000
MAX_OUTLINE_ITEMS = 200
MAX_INFOBOX_FACTS = 40
MAX_REFERENCE_TEXT_CHARS = 2_000
MAX_REFERENCE_LINKS = 5
MAX_LOG_BYTES = 1 * 1024 * 1024
LOG_BACKUP_COUNT = 2

_LOGGER = logging.getLogger("kiwix-mcp")

# Idle shutdown: stdio clients keep one process per open session. After this
# many seconds without a request, terminate this stateless child process; a
# compatible client respawns it on the next call. Set 0 to disable.
_IDLE_TIMEOUT_S = int(os.environ.get("KIWIX_MCP_IDLE_TIMEOUT", "600"))
_idle_timer: asyncio.TimerHandle | None = None


def _exit_idle() -> None:
    _LOGGER.info("idle for %ss, shutting down", _IDLE_TIMEOUT_S)
    # ponytail: hard exit is deliberate; this process owns no persistent state,
    # and cancelling MCP's nested stdio task can hang during SDK cleanup.
    os._exit(0)


def _cancel_idle_timer() -> None:
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
        _idle_timer = None


def _reset_idle_timer() -> None:
    global _idle_timer
    _cancel_idle_timer()
    if _IDLE_TIMEOUT_S <= 0:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _idle_timer = loop.call_later(_IDLE_TIMEOUT_S, _exit_idle)


class _ActivityReadStream:
    """Reset the idle timer for every inbound MCP frame."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def receive(self) -> Any:
        item = await self._stream.receive()
        _reset_idle_timer()
        return item

    def __aiter__(self) -> _ActivityReadStream:
        return self

    async def __anext__(self) -> Any:
        item = await self._stream.__anext__()
        _reset_idle_timer()
        return item

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def __aenter__(self) -> _ActivityReadStream:
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> Any:
        return await self._stream.__aexit__(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


MCP_INSTRUCTIONS = (
    "Search and read local ZIM archives. Always call list_archives first, then pass "
    "one returned archive_id exactly in every search, inspect_article, read_article, "
    "list_references, and extract_image call; use archive_id='*' only for search "
    "across archives. Choose the archive by language and collection: prefer full "
    "archives for coverage and maxi archives "
    "when images matter; use title, date, and description to break ties. "
    "For long Wikipedia articles, call inspect_article first, then read_article with "
    "a returned section anchor; read_article uses max_chars and offset (limit is "
    "accepted only as a compatibility alias for max_chars). "
    "Use list_references when a claim needs provenance. "
    "For images, inspect read_article metadata and prefer the image with "
    "primary=True; use its image_path with extract_image instead of index 0. "
    "For cross-archive comparison, call search with archive_id='*'. "
    "To follow a topic across articles, reuse read_article links/see_also "
    "article_path values (and uri fragments) for the next hop instead of "
    "searching again. "
    "Ask only when the user's goal leaves the choice ambiguous. "
    "For bilingual Wikipedia research, translate the search query into each archive's "
    "language and search the archives separately; do not assume article paths match. "
    "Answer in the user's language, preserve proper nouns, archive dates, and source URIs."
)
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
WRITES_CACHE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


def _archive_dir() -> Path:
    return Path(os.environ.get("KIWIX_ARCHIVE_DIR", DEFAULT_ARCHIVE_DIR)).expanduser()


def _configure_logging() -> None:
    """Enable diagnostics only when explicitly requested via environment."""
    log_file = os.environ.get("KIWIX_MCP_LOG_FILE", "").strip()
    log_level = os.environ.get("KIWIX_MCP_LOG_LEVEL", "").strip().upper()
    if not log_file and not log_level:
        return
    level = getattr(logging, log_level or "INFO", logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        )
    else:
        handler = logging.StreamHandler(sys.stderr)
    logging.basicConfig(
        level=level,
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _archive_paths() -> dict[str, Path]:
    root = _archive_dir()
    paths = (*root.glob("*.zim"), *root.glob("*.zimaa"))
    return {path.name: path for path in sorted(paths) if path.is_file()}


def _archive_size(path: Path) -> int:
    if path.suffix == ".zimaa":
        return sum(
            part.stat().st_size for part in path.parent.glob(f"{path.name[:-1]}?")
        )
    return path.stat().st_size


def _select_paths(archive_id: str) -> list[tuple[str, Path]]:
    archives = _archive_paths()
    requested = archive_id.strip()
    if not requested:
        return list(archives.items())
    for name, path in archives.items():
        logical_name = f"{path.stem}.zim" if path.suffix == ".zimaa" else name
        if requested in {name, path.stem, logical_name}:
            return [(name, path)]
    raise ValueError(f"Unknown archive: {archive_id}")


@lru_cache(maxsize=8)
def _open_archive(path: str, mtime_ns: int) -> Archive:
    del mtime_ns
    return Archive(Path(path))


def _archive(path: Path) -> Archive:
    return _open_archive(str(path), path.stat().st_mtime_ns)


def _entry(archive: Archive, article_path: str):
    entry = archive.get_entry_by_path(article_path)
    seen: set[str] = set()
    while entry.is_redirect:
        if entry.path in seen or len(seen) >= 8:
            raise ValueError("Redirect loop in ZIM entry")
        seen.add(entry.path)
        entry = entry.get_redirect_entry()
    return entry


def _article_uri(archive_id: str, article_path: str) -> str:
    return f"kiwix://{quote(str(archive_id), safe='')}/{quote(article_path, safe='/')}"


def _section_uri(archive_id: str, article_path: str, section: str) -> str:
    return f"{_article_uri(archive_id, article_path)}#{quote(section, safe='')}"


def _parse_article_uri(uri: str) -> tuple[str, str]:
    parsed = urlsplit(uri)
    if parsed.scheme != "kiwix":
        raise ValueError(f"Unsupported resource URI scheme: {parsed.scheme!r}")
    if parsed.netloc:
        archive_id = parsed.netloc
        article_path = parsed.path.lstrip("/")
        if not article_path:
            raise ValueError(f"Invalid resource URI: {uri}")
    else:
        remainder = parsed.path.lstrip("/")
        if "/" not in remainder:
            raise ValueError(f"Invalid resource URI: {uri}")
        archive_id, article_path = remainder.split("/", 1)
        if not article_path:
            raise ValueError(f"Invalid resource URI: {uri}")
    if not archive_id or not article_path:
        raise ValueError(f"Invalid resource URI: {uri}")
    return unquote(archive_id), unquote(article_path)


def _parse_article_section(uri: str) -> str:
    return unquote(urlsplit(uri).fragment)


def _main_entry_path(archive: Archive) -> str | None:
    if not getattr(archive, "has_main_entry", False):
        return None
    try:
        main_entry = archive.main_entry
        if getattr(main_entry, "is_redirect", False):
            return str(main_entry.get_redirect_entry().path)
        return str(main_entry.path)
    except (OSError, RuntimeError, ValueError):
        return None


def _archive_metadata(archive: Archive, key: str) -> str | None:
    try:
        return bytes(archive.get_metadata(key)).decode("utf-8", errors="replace")
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _plain_text(content: bytes, mimetype: str) -> str:
    text = content.decode("utf-8", errors="replace")
    if "html" not in mimetype.lower():
        return " ".join(text.split())
    return _soup_text(BeautifulSoup(text, "html.parser"))


_HEADING_TAGS = ("h2", "h3", "h4", "h5", "h6")
_MEDIAWIKI_NOISE = (
    ".navbox",
    ".sidebar",
    ".vertical-navbox",
    ".infobox",
    ".mw-references-wrap",
    ".reflist",
    "ol.references",
    ".mw-editsection",
    ".mw-empty-elt",
    "#toc",
    ".toc",
    "#catlinks",
    ".catlinks",
    ".printfooter",
    ".noprint",
)
_HATNOTE_SELECTOR = ".hatnote, .dablink, .rellink"
_STACKEXCHANGE_NOISE = (
    ".votecell",
    ".post-signature",
    ".post-taglist",
    ".comments",
    ".js-post-menu",
    ".question-status",
    ".bottom-notice",
)


def _content_root(soup: BeautifulSoup) -> tuple[Any, bool]:
    mediawiki = soup.select_one(".mw-parser-output")
    if mediawiki is not None:
        return mediawiki, True
    return (
        soup.select_one("#mainbar")
        or soup.select_one("main")
        or soup.select_one("article")
        or soup.select_one("#content")
        or soup.body
        or soup,
        False,
    )


def _render_text(root: Any, mediawiki: bool) -> str:
    for node in list(root.select("script, style, noscript, svg, nav, header, footer")):
        if node.parent is not None:
            node.decompose()
    _drop_hidden(root)
    for node in list(root.select(_HATNOTE_SELECTOR)):
        if node.parent is not None:
            node.decompose()
    if mediawiki:
        for node in list(root.select(", ".join(_MEDIAWIKI_NOISE))):
            if node.parent is not None:
                node.decompose()
    elif root.get("id") == "mainbar":
        for node in list(root.select(", ".join(_STACKEXCHANGE_NOISE))):
            if node.parent is not None:
                node.decompose()
    for node in root.find_all("br"):
        node.replace_with("\n")
    for node in root.find_all(["th", "td"]):
        node.insert_after("\t")
    for node in root.find_all(
        ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "figcaption"]
    ):
        node.insert_after("\n")
    return "\n".join(
        line
        for line in (" ".join(part.split()) for part in root.get_text().splitlines())
        if line
    )


def _drop_hidden(root: Any) -> None:
    for node in list(root.find_all(True)):
        if node.parent is None:
            continue
        style = str(node.get("style") or "").replace(" ", "").casefold()
        if node.has_attr("hidden") or "display:none" in style:
            node.decompose()


def _soup_text(soup: BeautifulSoup) -> str:
    root, mediawiki = _content_root(soup)
    return _render_text(root, mediawiki)


def _compact_text(node: Any, max_chars: int | None = None) -> str:
    text = " ".join(node.get_text(" ", strip=True).split())
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars].rstrip()
    return text


def _is_mediawiki_noise(node: Any) -> bool:
    for parent in (node, *node.parents):
        classes = set(parent.get("class") or []) if hasattr(parent, "get") else set()
        if classes.intersection(
            {
                "navbox",
                "sidebar",
                "vertical-navbox",
                "infobox",
                "mw-references-wrap",
                "reflist",
            }
        ):
            return True
        if getattr(parent, "name", None) == "ol" and "references" in classes:
            return True
    return False


def _section_key(value: str) -> str:
    return " ".join(unquote(value).replace("_", " ").split()).casefold()


def _heading_anchor(heading: Any) -> str:
    return str(heading.get("id") or _compact_text(heading)).strip()


def _article_outline(
    soup: BeautifulSoup, archive_id: str, article_path: str
) -> tuple[list[dict[str, Any]], int]:
    root, mediawiki = _content_root(soup)
    headings = [
        heading
        for heading in root.find_all(_HEADING_TAGS)
        if _compact_text(heading)
        and (not mediawiki or not _is_mediawiki_noise(heading))
    ]
    outline = []
    for heading in headings[:MAX_OUTLINE_ITEMS]:
        anchor = _heading_anchor(heading)
        outline.append(
            {
                "level": int(heading.name[1]),
                "title": _compact_text(heading),
                "anchor": anchor,
                "uri": _section_uri(archive_id, article_path, anchor),
            }
        )
    return outline, len(headings)


def _find_section(root: Any, section: str) -> Any:
    requested = _section_key(section)
    headings = [
        heading for heading in root.find_all(_HEADING_TAGS) if _compact_text(heading)
    ]
    for heading in headings:
        if requested in {
            _section_key(_heading_anchor(heading)),
            _section_key(_compact_text(heading)),
        }:
            return heading
    choices = ", ".join(_compact_text(heading) for heading in headings[:10])
    raise ValueError(f"Unknown section: {section}. Available sections: {choices}")


def _section_fragment(
    soup: BeautifulSoup, section: str
) -> tuple[BeautifulSoup, dict[str, Any]]:
    root, _ = _content_root(soup)
    heading = _find_section(root, section)
    level = int(heading.name[1])
    anchor = _heading_anchor(heading)

    for parent in heading.parents:
        if parent is root:
            break
        if parent.name == "section" and parent.find(_HEADING_TAGS) is heading:
            fragment = BeautifulSoup(str(parent), "html.parser")
            return fragment, {
                "level": level,
                "title": _compact_text(heading),
                "anchor": anchor,
            }

    start = (
        heading.parent
        if "mw-heading" in (heading.parent.get("class") or [])
        else heading
    )
    fragment = BeautifulSoup("<div></div>", "html.parser")
    container = fragment.div
    for sibling in (start, *start.next_siblings):
        if sibling is not start and getattr(sibling, "name", None):
            next_heading = (
                sibling
                if sibling.name in _HEADING_TAGS
                else sibling.find(_HEADING_TAGS)
            )
            if next_heading is not None and int(next_heading.name[1]) <= level:
                break
        container.append(copy(sibling))
    return fragment, {
        "level": level,
        "title": _compact_text(heading),
        "anchor": anchor,
    }


def _article_text(
    soup: BeautifulSoup, section: str = ""
) -> tuple[str, dict[str, Any] | None]:
    _, mediawiki = _content_root(soup)
    if not section.strip():
        return _soup_text(soup), None
    fragment, selected = _section_fragment(soup, section)
    fragment_root = fragment.body or fragment
    return _render_text(fragment_root, mediawiki), selected


def _article_lead(soup: BeautifulSoup) -> str:
    root, mediawiki = _content_root(soup)
    paragraphs: list[str] = []
    for node in root.find_all(("p", "h2")):
        if node.name == "h2":
            break
        if mediawiki and _is_mediawiki_noise(node):
            continue
        visible = copy(node)
        _drop_hidden(visible)
        # A hatnote ("X redirects here", "主条目：…") wrapped in a <p> is
        # navigation chrome, not lead prose.
        for hatnote in visible.select(_HATNOTE_SELECTOR):
            hatnote.decompose()
        text = _compact_text(visible)
        if len(text) < 40:
            continue
        paragraphs.append(text)
        if len(paragraphs) >= 3 or sum(map(len, paragraphs)) >= MAX_LEAD_CHARS:
            break
    if paragraphs:
        return "\n".join(paragraphs)[:MAX_LEAD_CHARS].rstrip()
    fallback = _render_text(copy(root), mediawiki)
    return fallback[:MAX_LEAD_CHARS].rstrip()


def _infobox_facts(soup: BeautifulSoup) -> list[dict[str, str]]:
    root, mediawiki = _content_root(soup)
    if not mediawiki:
        return []
    facts: list[dict[str, str]] = []
    for row in root.select("table.infobox tr"):
        name = row.find("th", recursive=False)
        value = row.find("td", recursive=False)
        if name is None or value is None:
            continue
        visible_name = copy(name)
        visible_value = copy(value)
        _drop_hidden(visible_name)
        _drop_hidden(visible_value)
        key = _compact_text(visible_name, 200)
        text = _compact_text(visible_value, 500)
        if key and text:
            facts.append({"name": key, "value": text})
        if len(facts) >= MAX_INFOBOX_FACTS:
            break
    return facts


def _reference_nodes(soup: BeautifulSoup) -> list[Any]:
    root, mediawiki = _content_root(soup)
    if not mediawiki:
        return []
    return list(root.select("ol.references > li"))


def _reference_labels(soup: BeautifulSoup) -> dict[str, str]:
    root, mediawiki = _content_root(soup)
    if not mediawiki:
        return {}
    labels: dict[str, str] = {}
    for marker in root.select("sup.reference, sup.mw-ref"):
        anchor = marker.find("a", href=True)
        if anchor is None:
            continue
        citation_id = unquote(urlsplit(str(anchor["href"])).fragment)
        if not citation_id.startswith("cite_note-"):
            continue
        label_node = marker.select_one(".mw-reflink-text") or marker
        label = _compact_text(label_node).strip()
        if label.startswith("[") and label.endswith("]"):
            label = label[1:-1].strip()
        if label:
            labels.setdefault(citation_id, label)
    return labels


def _reference_item(node: Any, labels: dict[str, str]) -> dict[str, Any]:
    citation_id = str(node.get("id") or "")
    links: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for anchor in node.find_all("a", href=True):
        url = str(anchor["href"])
        if not url.startswith(("http://", "https://")) or url in seen_urls:
            continue
        seen_urls.add(url)
        links.append({"title": _compact_text(anchor, 200), "url": url})
        if len(links) >= MAX_REFERENCE_LINKS:
            break
    cleaned = copy(node)
    _drop_hidden(cleaned)
    for backlink in cleaned.select(".mw-cite-backlink"):
        backlink.decompose()
    return {
        "citation_label": labels.get(citation_id),
        "citation_id": citation_id,
        "text": _compact_text(cleaned, MAX_REFERENCE_TEXT_CHARS),
        "links": links,
    }


def _excerpt(text: str, query: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    needle = query.strip().casefold()
    position = text.casefold().find(needle) if needle else -1
    if position < 0:
        return text[:max_chars].rstrip()
    start = max(0, position - max_chars // 3)
    end = min(len(text), start + max_chars)
    return text[start:end].strip()


def _article_window(
    text: str, query: str, max_chars: int, offset: int
) -> tuple[str, int, int | None]:
    """Return a query-centered first window or an exact continuation window."""
    start = max(int(offset), 0)
    if start == 0:
        needle = query.strip().casefold()
        position = text.casefold().find(needle) if needle else -1
        if position >= 0:
            start = max(0, position - max_chars // 3)
    start = min(start, len(text))
    end = min(len(text), start + max_chars)
    return text[start:end], start, end if end < len(text) else None


def _article_summary(
    archive: Archive, archive_id: str, article_path: str, query: str
) -> dict[str, Any]:
    entry = _entry(archive, article_path)
    item = entry.get_item()
    summary = ""
    if item.size <= MAX_ARTICLE_BYTES and (
        item.mimetype.startswith("text/") or "html" in item.mimetype
    ):
        summary = _excerpt(_plain_text(bytes(item.content), item.mimetype), query, 600)
    return {
        "archive_id": archive_id,
        "article_path": entry.path,
        "title": entry.title or item.title,
        "mimetype": item.mimetype,
        "snippet": summary,
        "uri": _article_uri(archive_id, entry.path),
    }


def list_archives() -> dict[str, Any]:
    """List ZIM archives in KIWIX_ARCHIVE_DIR for archive_id selection."""
    archives: list[dict[str, Any]] = []
    for name, path in _archive_paths().items():
        item: dict[str, Any] = {
            "archive_id": name,
            "size_bytes": _archive_size(path),
        }
        try:
            archive = _archive(path)
            item["article_count"] = archive.article_count
            item["media_count"] = archive.media_count
            item["has_fulltext_index"] = archive.has_fulltext_index
            item["has_title_index"] = archive.has_title_index
            item["has_main_entry"] = getattr(archive, "has_main_entry", False)
            main_entry_path = _main_entry_path(archive)
            if main_entry_path is not None:
                item["main_entry_path"] = main_entry_path
            for key in (
                "Title",
                "Language",
                "Date",
                "Description",
                "Name",
                "Tags",
                "Flavour",
            ):
                value = _archive_metadata(archive, key)
                if value is not None:
                    item[key.lower()] = value
        except (OSError, RuntimeError, ValueError) as exc:
            item["error"] = str(exc)
        archives.append(item)
    return {
        "status": "ok" if archives else "empty",
        "directory": str(_archive_dir()),
        "archives": archives,
    }


def search(
    query: str,
    archive_id: str,
    limit: int = 5,
    offset: int = 0,
    mode: str = "auto",
    language: str = "",
    flavour: str = "",
) -> dict[str, Any]:
    """Search one archive, or all archives with archive_id='*'.

    language/flavour only filter archives in cross-archive mode ("*"); a
    single explicitly selected archive is always searched as requested.
    """
    query = query.strip()
    if not query:
        raise ValueError("query is required")
    if not archive_id.strip():
        raise ValueError("archive_id is required")
    mode = mode.strip().lower()
    if mode not in {"auto", "fulltext", "title"}:
        raise ValueError("mode must be one of: auto, fulltext, title")
    language = language.strip().lower()
    flavour = flavour.strip().lower()
    cross_archive = archive_id.strip() == "*"
    limit = min(max(int(limit), 1), 20)
    offset = min(max(int(offset), 0), 1000)
    limit = min(limit, 1001 - offset)
    selected = _select_paths("") if cross_archive else _select_paths(archive_id)

    def _archive_matches(archive: Archive) -> bool:
        """Cross-archive filter by language/flavour metadata; True when no filter."""
        if not cross_archive:
            return True
        if language:
            value = _archive_metadata(archive, "Language")
            if value is None:
                return False
            languages = {token.strip() for token in value.lower().split(",")}
            alias = _LANGUAGE_ALIASES.get(language, "")
            if not any(token in languages for token in (language, alias) if token):
                return False
        if flavour:
            value = _archive_metadata(archive, "Flavour")
            if value is None:
                return False
            if flavour != value.lower().strip():
                return False
        return True

    exact_matches: list[tuple[Archive, str, str, str, str]] = []
    result_streams = []
    errors: list[dict[str, str]] = []
    estimated_matches = 0
    estimated_known = False
    query_variants = _query_variants(query)
    exact_seen: set[tuple[str, str]] = set()

    def _matches(archive, name, search_result, search_mode, variant):
        # Read past redirect aliases; a raw candidate count is not a page size.
        start = 0
        batch_size = max(32, offset + limit + 1)
        while True:
            try:
                found = list(search_result.getResults(start, batch_size))
            except (OSError, RuntimeError, ValueError) as exc:
                if mode == "fulltext":
                    raise
                errors.append({"archive_id": name, "error": str(exc)})
                return
            for article_path in found:
                yield archive, name, str(article_path), search_mode, variant
            if len(found) < batch_size:
                return
            start += len(found)

    for name, path in selected:
        try:
            archive = _archive(path)
            if not _archive_matches(archive):
                continue
            archive_estimates: list[int] = []
            for variant in query_variants:
                if mode == "fulltext":
                    if not archive.has_fulltext_index:
                        raise ValueError(
                            f"Archive does not support full-text search: {name}"
                        )
                    search_result = Searcher(archive).search(Query().set_query(variant))
                    search_mode = "fulltext"
                elif mode == "title" or not archive.has_fulltext_index:
                    search_result = SuggestionSearcher(archive).suggest(variant)
                    search_mode = "title"
                else:
                    search_result = Searcher(archive).search(Query().set_query(variant))
                    search_mode = "fulltext"
                # SuggestionSearcher is case-insensitive; get_entry_by_title
                # is not. Use suggest for exact detection so lowercase input
                # hits capitalized titles.
                exact_path = None
                if getattr(archive, "has_title_index", False):
                    suggest_result = SuggestionSearcher(archive).suggest(variant)
                    suggested = list(suggest_result.getResults(0, 1))
                    if suggested:
                        suggested_path = str(suggested[0])
                        if (
                            suggested_path.replace("_", " ").casefold()
                            == variant.casefold()
                        ):
                            exact_path = suggested_path
                if exact_path is None:
                    exact_path = (
                        str(archive.get_entry_by_title(variant).path)
                        if archive.has_entry_by_title(variant)
                        else None
                    )
                estimate = search_result.getEstimatedMatches()
                if estimate is not None:
                    archive_estimates.append(int(estimate))
                if exact_path:
                    exact_path = str(exact_path)
                    if (name, exact_path) not in exact_seen:
                        exact_seen.add((name, exact_path))
                        exact_matches.append(
                            (archive, name, exact_path, "exact_title", variant)
                        )
                result_streams.append(
                    _matches(archive, name, search_result, search_mode, variant)
                )
            if archive_estimates:
                estimated_matches += max(archive_estimates)
                estimated_known = True
        except (OSError, RuntimeError, ValueError) as exc:
            if mode == "fulltext":
                raise
            errors.append({"archive_id": name, "error": str(exc)})
    # All exact hits precede lazy archive/variant streams, on every page.
    matches = chain(exact_matches, chain.from_iterable(result_streams))
    canonical_matches: list[tuple[Archive, str, str, str, str]] = []
    canonical_seen: set[tuple[str, str]] = set()
    for archive, name, article_path, match_type, matched_query in matches:
        try:
            canonical_path = str(_entry(archive, article_path).path)
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append({"archive_id": name, "error": str(exc)})
            continue
        key = (name, canonical_path)
        if key in canonical_seen:
            continue
        canonical_seen.add(key)
        canonical_matches.append(
            (archive, name, canonical_path, match_type, matched_query)
        )
        if len(canonical_matches) >= offset + limit + 1:
            break
    matches = canonical_matches
    page_matches = matches[offset : offset + limit]
    results: list[dict[str, Any]] = []
    for archive, name, article_path, match_type, matched_query in page_matches:
        try:
            item = _article_summary(
                archive,
                name,
                article_path,
                "" if match_type == "exact_title" else matched_query,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append({"archive_id": name, "error": str(exc)})
            continue
        item["search_mode"] = "fulltext" if match_type == "fulltext" else "title"
        item["match_type"] = match_type
        if matched_query != query:
            item["matched_query"] = matched_query
        results.append(item)
    has_more = len(matches) > offset + len(page_matches)
    return {
        "status": "ok" if results else "no_hits",
        "query": query,
        "offset": offset,
        "next_offset": offset + len(page_matches)
        if has_more and page_matches and offset + len(page_matches) <= 1000
        else None,
        "estimated_matches": estimated_matches if estimated_known else None,
        "results": results,
        "errors": errors,
        "mode": mode,
        "filters": {"language": language, "flavour": flavour},
    }


def read_article(
    archive_id: str,
    article_path: str,
    query: str = "",
    max_chars: int = 12000,
    offset: int = 0,
    include_images: bool = True,
    include_links: bool = True,
    image_offset: int = 0,
    section: str = "",
    limit: int | None = None,
) -> dict[str, Any]:
    """Read article text. include_images/include_links 时附带图片列表和条目链接。"""
    selected = _select_paths(archive_id)
    if len(selected) != 1:
        raise ValueError("archive_id is required")
    name, path = selected[0]
    archive = _archive(path)
    entry = _entry(archive, article_path)
    item = entry.get_item()
    if item.size > MAX_ARTICLE_BYTES:
        raise ValueError(f"Article is too large to read safely: {item.size} bytes")
    if not (item.mimetype.startswith("text/") or "html" in item.mimetype):
        raise ValueError(f"Article is not text: {item.mimetype}")
    if limit is not None and max_chars == 12000:
        max_chars = limit
    max_chars = min(max(int(max_chars), 1000), 50000)
    offset = max(int(offset), 0)
    content = bytes(item.content)
    images: list[dict[str, Any]] | None = None
    see_also: list[dict[str, Any]] | None = None
    links: list[dict[str, Any]] | None = None
    notes: list[dict[str, Any]] | None = None
    selected_section: dict[str, Any] | None = None
    canonical_url: str | None = None
    if "html" in item.mimetype.lower():
        soup = BeautifulSoup(content.decode("utf-8", errors="replace"), "html.parser")
        canonical = soup.find("link", rel="canonical", href=True)
        canonical_url = str(canonical["href"]) if canonical else None
        if include_images:
            images = _find_images(soup, entry.path)
        if include_links:
            see_also, links, notes = _find_links(archive, soup, entry.path, name)
        text, selected_section = _article_text(soup, section)
    else:
        if section.strip():
            raise ValueError("Sections are only available for HTML articles")
        text = _plain_text(content, item.mimetype)
    window, text_offset, next_offset = _article_window(text, query, max_chars, offset)
    result: dict[str, Any] = {
        "status": "ok",
        "archive_id": name,
        "article_path": entry.path,
        "title": entry.title or item.title,
        "mimetype": item.mimetype,
        "text": window,
        "offset": text_offset,
        "next_offset": next_offset,
        "truncated": next_offset is not None,
        "total_chars": len(text),
        "uri": (
            _section_uri(name, entry.path, selected_section["anchor"])
            if selected_section
            else _article_uri(name, entry.path)
        ),
        "requested_article_path": article_path,
        "redirected": article_path != entry.path,
        "section": selected_section,
        "canonical_url": canonical_url,
        "language": _archive_metadata(archive, "Language"),
        "archive_date": _archive_metadata(archive, "Date"),
    }
    if include_images:
        images = images or []
        image_offset = min(max(int(image_offset), 0), len(images))
        image_page = images[image_offset : image_offset + MAX_IMAGE_REFERENCES]
        next_image_offset = (
            image_offset + len(image_page)
            if image_offset + len(image_page) < len(images)
            else None
        )
        result["total_images"] = len(images)
        result["image_offset"] = image_offset
        result["images"] = image_page
        result["next_image_offset"] = next_image_offset
        result["images_truncated"] = next_image_offset is not None
    if include_links:
        see_also = see_also or []
        links = links or []
        result["see_also"] = see_also
        result["links"] = links
        result["notes"] = notes or []
        result["total_links"] = len(see_also) + len(links)
    return result


def inspect_article(archive_id: str, article_path: str) -> dict[str, Any]:
    """Return a compact structural view before reading a potentially long article."""
    selected = _select_paths(archive_id)
    if len(selected) != 1:
        raise ValueError("archive_id is required")
    name, path = selected[0]
    archive = _archive(path)
    requested_entry = archive.get_entry_by_path(article_path)
    requested_path = str(requested_entry.path)
    entry = _entry(archive, article_path)
    item = entry.get_item()
    if item.size > MAX_ARTICLE_BYTES:
        raise ValueError(f"Article is too large to read safely: {item.size} bytes")
    if not (item.mimetype.startswith("text/") or "html" in item.mimetype):
        raise ValueError(f"Article is not text: {item.mimetype}")

    content = bytes(item.content)
    outline: list[dict[str, Any]] = []
    facts: list[dict[str, str]] = []
    total_sections = 0
    total_references = 0
    canonical_url: str | None = None
    profile = "text"
    if "html" in item.mimetype.lower():
        soup = BeautifulSoup(content.decode("utf-8", errors="replace"), "html.parser")
        _, mediawiki = _content_root(soup)
        profile = "mediawiki" if mediawiki else "html"
        outline, total_sections = _article_outline(soup, name, entry.path)
        facts = _infobox_facts(soup)
        total_references = len(_reference_nodes(soup))
        lead = _article_lead(soup)
        canonical = soup.find("link", rel="canonical", href=True)
        canonical_url = str(canonical["href"]) if canonical else None
        total_chars = len(_soup_text(soup))
    else:
        text = _plain_text(content, item.mimetype)
        lead = text[:MAX_LEAD_CHARS].rstrip()
        total_chars = len(text)

    return {
        "status": "ok",
        "archive_id": name,
        "requested_article_path": requested_path,
        "article_path": entry.path,
        "redirected": requested_path != entry.path,
        "title": entry.title or item.title,
        "mimetype": item.mimetype,
        "profile": profile,
        "lead": lead,
        "outline": outline,
        "total_sections": total_sections,
        "outline_truncated": total_sections > len(outline),
        "facts": facts,
        "total_references": total_references,
        "total_chars": total_chars,
        "uri": _article_uri(name, entry.path),
        "canonical_url": canonical_url,
        "language": _archive_metadata(archive, "Language"),
        "archive_date": _archive_metadata(archive, "Date"),
    }


def list_references(
    archive_id: str,
    article_path: str,
    offset: int = 0,
    limit: int = 20,
    citation_label: str | None = None,
) -> dict[str, Any]:
    """List MediaWiki references with stable citation ids and external URLs."""
    selected = _select_paths(archive_id)
    if len(selected) != 1:
        raise ValueError("archive_id is required")
    name, path = selected[0]
    archive = _archive(path)
    entry = _entry(archive, article_path)
    item = entry.get_item()
    if item.size > MAX_ARTICLE_BYTES:
        raise ValueError(f"Article is too large to read safely: {item.size} bytes")
    if "html" not in item.mimetype.lower():
        raise ValueError("References are only available for HTML articles")

    soup = BeautifulSoup(
        bytes(item.content).decode("utf-8", errors="replace"), "html.parser"
    )
    labels = _reference_labels(soup)
    references = [_reference_item(node, labels) for node in _reference_nodes(soup)]
    total_references = len(references)
    if citation_label is not None:
        requested_label = " ".join(str(citation_label).split()).casefold()
        references = [
            reference
            for reference in references
            if reference["citation_label"] is not None
            and " ".join(reference["citation_label"].split()).casefold()
            == requested_label
        ]
    matched_references = len(references)
    offset = min(max(int(offset), 0), len(references))
    limit = min(max(int(limit), 1), 50)
    page = references[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(references) else None
    return {
        "status": "ok" if page else "no_hits",
        "archive_id": name,
        "article_path": entry.path,
        "offset": offset,
        "next_offset": next_offset,
        "total_references": total_references,
        "matched_references": matched_references,
        "citation_label": citation_label,
        "references": page,
        "uri": _article_uri(name, entry.path),
    }


MAX_SEE_ALSO_LINKS = 10
MAX_BODY_LINKS = 15

# OpenCC TSCharacters one-to-one mappings; multi-output entries are omitted.
# Embedded to keep offline query expansion deterministic and dependency-free.
_TRADITIONAL_TO_SIMPLIFIED = {
    "㑯": "㑔",
    "㑳": "㑇",
    "㑶": "㐹",
    "㓨": "刾",
    "㘚": "㘎",
    "㜄": "㚯",
    "㜏": "㛣",
    "㠏": "㟆",
    "㥮": "㤘",
    "㩜": "㨫",
    "㩳": "㧐",
    "㩵": "擜",
    "䁻": "䀥",
    "䃮": "鿎",
    "䊷": "䌶",
    "䋙": "䌺",
    "䋚": "䌻",
    "䋹": "䌿",
    "䋻": "䌾",
    "䍦": "䍠",
    "䎱": "䎬",
    "䙡": "䙌",
    "䜀": "䜧",
    "䝼": "䞍",
    "䥇": "䦂",
    "䥑": "鿏",
    "䥱": "䥾",
    "䦛": "䦶",
    "䦟": "䦷",
    "䯀": "䯅",
    "䰾": "鲃",
    "䱷": "䲣",
    "䱽": "䲝",
    "䲁": "鳚",
    "䲘": "鳤",
    "䴉": "鹮",
    "丟": "丢",
    "並": "并",
    "亂": "乱",
    "亙": "亘",
    "亞": "亚",
    "佇": "伫",
    "佈": "布",
    "佔": "占",
    "併": "并",
    "來": "来",
    "侖": "仑",
    "侶": "侣",
    "侷": "局",
    "俁": "俣",
    "係": "系",
    "俔": "伣",
    "俠": "侠",
    "俥": "伡",
    "俬": "私",
    "倀": "伥",
    "倆": "俩",
    "倈": "俫",
    "倉": "仓",
    "個": "个",
    "們": "们",
    "倖": "幸",
    "倫": "伦",
    "倲": "㑈",
    "偉": "伟",
    "偑": "㐽",
    "側": "侧",
    "偵": "侦",
    "偽": "伪",
    "傌": "㐷",
    "傑": "杰",
    "傖": "伧",
    "傘": "伞",
    "備": "备",
    "傢": "家",
    "傭": "佣",
    "傯": "偬",
    "傳": "传",
    "傴": "伛",
    "債": "债",
    "傷": "伤",
    "傾": "倾",
    "僂": "偻",
    "僅": "仅",
    "僉": "佥",
    "僑": "侨",
    "僕": "仆",
    "僞": "伪",
    "僥": "侥",
    "僨": "偾",
    "僱": "雇",
    "價": "价",
    "儀": "仪",
    "儁": "俊",
    "儂": "侬",
    "億": "亿",
    "儈": "侩",
    "儉": "俭",
    "儎": "傤",
    "儐": "傧",
    "儔": "俦",
    "儕": "侪",
    "償": "偿",
    "優": "优",
    "儲": "储",
    "儷": "俪",
    "儸": "㑩",
    "儺": "傩",
    "儻": "傥",
    "儼": "俨",
    "兇": "凶",
    "兌": "兑",
    "兒": "儿",
    "兗": "兖",
    "內": "内",
    "兩": "两",
    "冊": "册",
    "冑": "胄",
    "冪": "幂",
    "凈": "净",
    "凍": "冻",
    "凜": "凛",
    "凱": "凯",
    "別": "别",
    "刪": "删",
    "剄": "刭",
    "則": "则",
    "剎": "刹",
    "剗": "刬",
    "剛": "刚",
    "剝": "剥",
    "剮": "剐",
    "剴": "剀",
    "創": "创",
    "剷": "铲",
    "劇": "剧",
    "劉": "刘",
    "劊": "刽",
    "劌": "刿",
    "劍": "剑",
    "劏": "㓥",
    "劑": "剂",
    "劚": "㔉",
    "勁": "劲",
    "動": "动",
    "務": "务",
    "勛": "勋",
    "勝": "胜",
    "勞": "劳",
    "勢": "势",
    "勩": "勚",
    "勱": "劢",
    "勳": "勋",
    "勵": "励",
    "勸": "劝",
    "勻": "匀",
    "匭": "匦",
    "匯": "汇",
    "匱": "匮",
    "區": "区",
    "協": "协",
    "卹": "恤",
    "卻": "却",
    "卽": "即",
    "厙": "厍",
    "厠": "厕",
    "厤": "历",
    "厭": "厌",
    "厲": "厉",
    "厴": "厣",
    "參": "参",
    "叄": "叁",
    "叢": "丛",
    "吳": "吴",
    "吶": "呐",
    "呂": "吕",
    "咼": "呙",
    "員": "员",
    "唄": "呗",
    "唸": "念",
    "問": "问",
    "啓": "启",
    "啞": "哑",
    "啟": "启",
    "啢": "唡",
    "喎": "㖞",
    "喚": "唤",
    "喪": "丧",
    "喫": "吃",
    "喬": "乔",
    "單": "单",
    "喲": "哟",
    "嗆": "呛",
    "嗇": "啬",
    "嗊": "唝",
    "嗎": "吗",
    "嗚": "呜",
    "嗩": "唢",
    "嗶": "哔",
    "嘆": "叹",
    "嘍": "喽",
    "嘓": "啯",
    "嘔": "呕",
    "嘖": "啧",
    "嘗": "尝",
    "嘜": "唛",
    "嘩": "哗",
    "嘮": "唠",
    "嘯": "啸",
    "嘰": "叽",
    "嘵": "哓",
    "嘸": "呒",
    "嘽": "啴",
    "噓": "嘘",
    "噚": "㖊",
    "噝": "咝",
    "噠": "哒",
    "噥": "哝",
    "噦": "哕",
    "噯": "嗳",
    "噲": "哙",
    "噴": "喷",
    "噸": "吨",
    "嚀": "咛",
    "嚇": "吓",
    "嚌": "哜",
    "嚐": "尝",
    "嚕": "噜",
    "嚙": "啮",
    "嚥": "咽",
    "嚦": "呖",
    "嚨": "咙",
    "嚮": "向",
    "嚲": "亸",
    "嚳": "喾",
    "嚴": "严",
    "嚶": "嘤",
    "囀": "啭",
    "囁": "嗫",
    "囂": "嚣",
    "囅": "冁",
    "囈": "呓",
    "囉": "啰",
    "囌": "苏",
    "囑": "嘱",
    "囪": "囱",
    "圇": "囵",
    "國": "国",
    "圍": "围",
    "園": "园",
    "圓": "圆",
    "圖": "图",
    "團": "团",
    "垻": "坝",
    "埡": "垭",
    "埰": "采",
    "執": "执",
    "堅": "坚",
    "堊": "垩",
    "堖": "垴",
    "堝": "埚",
    "堯": "尧",
    "報": "报",
    "場": "场",
    "塊": "块",
    "塋": "茔",
    "塏": "垲",
    "塒": "埘",
    "塗": "涂",
    "塚": "冢",
    "塢": "坞",
    "塤": "埙",
    "塵": "尘",
    "塹": "堑",
    "墊": "垫",
    "墜": "坠",
    "墮": "堕",
    "墰": "坛",
    "墳": "坟",
    "墶": "垯",
    "墻": "墙",
    "墾": "垦",
    "壇": "坛",
    "壋": "垱",
    "壎": "埙",
    "壓": "压",
    "壘": "垒",
    "壙": "圹",
    "壚": "垆",
    "壜": "坛",
    "壞": "坏",
    "壟": "垄",
    "壠": "垅",
    "壢": "坜",
    "壩": "坝",
    "壪": "塆",
    "壯": "壮",
    "壺": "壶",
    "壼": "壸",
    "壽": "寿",
    "夠": "够",
    "夢": "梦",
    "夾": "夹",
    "奐": "奂",
    "奧": "奥",
    "奩": "奁",
    "奪": "夺",
    "奬": "奖",
    "奮": "奋",
    "奼": "姹",
    "妝": "妆",
    "姍": "姗",
    "姦": "奸",
    "娛": "娱",
    "婁": "娄",
    "婦": "妇",
    "婭": "娅",
    "媧": "娲",
    "媯": "妫",
    "媰": "㛀",
    "媼": "媪",
    "媽": "妈",
    "嫋": "袅",
    "嫗": "妪",
    "嫵": "妩",
    "嫺": "娴",
    "嫻": "娴",
    "嫿": "婳",
    "嬀": "妫",
    "嬃": "媭",
    "嬈": "娆",
    "嬋": "婵",
    "嬌": "娇",
    "嬙": "嫱",
    "嬡": "嫒",
    "嬤": "嬷",
    "嬪": "嫔",
    "嬰": "婴",
    "嬸": "婶",
    "孃": "娘",
    "孋": "㛤",
    "孌": "娈",
    "孫": "孙",
    "學": "学",
    "孿": "孪",
    "宮": "宫",
    "寀": "采",
    "寢": "寝",
    "實": "实",
    "寧": "宁",
    "審": "审",
    "寫": "写",
    "寬": "宽",
    "寵": "宠",
    "寶": "宝",
    "將": "将",
    "專": "专",
    "尋": "寻",
    "對": "对",
    "導": "导",
    "尷": "尴",
    "屆": "届",
    "屍": "尸",
    "屓": "屃",
    "屜": "屉",
    "屢": "屡",
    "層": "层",
    "屨": "屦",
    "屬": "属",
    "岡": "冈",
    "峯": "峰",
    "峴": "岘",
    "島": "岛",
    "峽": "峡",
    "崍": "崃",
    "崑": "昆",
    "崗": "岗",
    "崢": "峥",
    "崬": "岽",
    "嵐": "岚",
    "嵗": "岁",
    "嵾": "㟥",
    "嶁": "嵝",
    "嶄": "崭",
    "嶇": "岖",
    "嶔": "嵚",
    "嶗": "崂",
    "嶠": "峤",
    "嶢": "峣",
    "嶧": "峄",
    "嶨": "峃",
    "嶮": "崄",
    "嶸": "嵘",
    "嶺": "岭",
    "嶼": "屿",
    "嶽": "岳",
    "巋": "岿",
    "巒": "峦",
    "巔": "巅",
    "巖": "岩",
    "巰": "巯",
    "巹": "卺",
    "帥": "帅",
    "師": "师",
    "帳": "帐",
    "帶": "带",
    "幀": "帧",
    "幃": "帏",
    "幓": "㡎",
    "幗": "帼",
    "幘": "帻",
    "幟": "帜",
    "幣": "币",
    "幫": "帮",
    "幬": "帱",
    "幷": "并",
    "幹": "干",
    "幾": "几",
    "庫": "库",
    "廁": "厕",
    "廂": "厢",
    "廄": "厩",
    "廈": "厦",
    "廎": "庼",
    "廕": "荫",
    "廚": "厨",
    "廝": "厮",
    "廟": "庙",
    "廠": "厂",
    "廡": "庑",
    "廢": "废",
    "廣": "广",
    "廩": "廪",
    "廳": "厅",
    "弒": "弑",
    "弔": "吊",
    "弳": "弪",
    "張": "张",
    "強": "强",
    "彆": "别",
    "彈": "弹",
    "彌": "弥",
    "彎": "弯",
    "彔": "录",
    "彙": "汇",
    "彠": "彟",
    "彥": "彦",
    "彫": "雕",
    "彲": "彨",
    "彿": "佛",
    "後": "后",
    "徑": "径",
    "從": "从",
    "徠": "徕",
    "復": "复",
    "徹": "彻",
    "恆": "恒",
    "恥": "耻",
    "悅": "悦",
    "悞": "悮",
    "悵": "怅",
    "悶": "闷",
    "悽": "凄",
    "惡": "恶",
    "惱": "恼",
    "惲": "恽",
    "惻": "恻",
    "愛": "爱",
    "愜": "惬",
    "愨": "悫",
    "愴": "怆",
    "愷": "恺",
    "愾": "忾",
    "慄": "栗",
    "態": "态",
    "慍": "愠",
    "慘": "惨",
    "慚": "惭",
    "慟": "恸",
    "慣": "惯",
    "慤": "悫",
    "慪": "怄",
    "慫": "怂",
    "慮": "虑",
    "慳": "悭",
    "慶": "庆",
    "慺": "㥪",
    "慼": "戚",
    "慾": "欲",
    "憂": "忧",
    "憊": "惫",
    "憐": "怜",
    "憑": "凭",
    "憒": "愦",
    "憖": "慭",
    "憚": "惮",
    "憤": "愤",
    "憫": "悯",
    "憮": "怃",
    "憲": "宪",
    "憶": "忆",
    "懇": "恳",
    "應": "应",
    "懌": "怿",
    "懍": "懔",
    "懞": "蒙",
    "懟": "怼",
    "懣": "懑",
    "懤": "㤽",
    "懨": "恹",
    "懲": "惩",
    "懶": "懒",
    "懷": "怀",
    "懸": "悬",
    "懺": "忏",
    "懼": "惧",
    "懾": "慑",
    "戀": "恋",
    "戇": "戆",
    "戔": "戋",
    "戧": "戗",
    "戩": "戬",
    "戰": "战",
    "戱": "戯",
    "戲": "戏",
    "戶": "户",
    "拋": "抛",
    "拚": "拼",
    "挩": "捝",
    "挱": "挲",
    "挾": "挟",
    "捨": "舍",
    "捫": "扪",
    "捱": "挨",
    "捲": "卷",
    "掃": "扫",
    "掄": "抡",
    "掆": "㧏",
    "掗": "挜",
    "掙": "挣",
    "掛": "挂",
    "採": "采",
    "揀": "拣",
    "揚": "扬",
    "換": "换",
    "揮": "挥",
    "揯": "搄",
    "損": "损",
    "搖": "摇",
    "搗": "捣",
    "搧": "扇",
    "搵": "揾",
    "搶": "抢",
    "摑": "掴",
    "摜": "掼",
    "摟": "搂",
    "摯": "挚",
    "摳": "抠",
    "摶": "抟",
    "摺": "折",
    "摻": "掺",
    "撈": "捞",
    "撏": "挦",
    "撐": "撑",
    "撓": "挠",
    "撝": "㧑",
    "撟": "挢",
    "撣": "掸",
    "撥": "拨",
    "撫": "抚",
    "撲": "扑",
    "撳": "揿",
    "撻": "挞",
    "撾": "挝",
    "撿": "捡",
    "擁": "拥",
    "擄": "掳",
    "擇": "择",
    "擊": "击",
    "擋": "挡",
    "擓": "㧟",
    "擔": "担",
    "據": "据",
    "擠": "挤",
    "擡": "抬",
    "擬": "拟",
    "擯": "摈",
    "擰": "拧",
    "擱": "搁",
    "擲": "掷",
    "擴": "扩",
    "擷": "撷",
    "擺": "摆",
    "擻": "擞",
    "擼": "撸",
    "擽": "㧰",
    "擾": "扰",
    "攄": "摅",
    "攆": "撵",
    "攏": "拢",
    "攔": "拦",
    "攖": "撄",
    "攙": "搀",
    "攛": "撺",
    "攜": "携",
    "攝": "摄",
    "攢": "攒",
    "攣": "挛",
    "攤": "摊",
    "攪": "搅",
    "攬": "揽",
    "敎": "教",
    "敓": "敚",
    "敗": "败",
    "敘": "叙",
    "敵": "敌",
    "數": "数",
    "斂": "敛",
    "斃": "毙",
    "斆": "敩",
    "斕": "斓",
    "斬": "斩",
    "斷": "断",
    "旂": "旗",
    "旣": "既",
    "時": "时",
    "晉": "晋",
    "晝": "昼",
    "暈": "晕",
    "暉": "晖",
    "暘": "旸",
    "暢": "畅",
    "暫": "暂",
    "曄": "晔",
    "曆": "历",
    "曇": "昙",
    "曉": "晓",
    "曏": "向",
    "曖": "暧",
    "曠": "旷",
    "曨": "昽",
    "曬": "晒",
    "書": "书",
    "會": "会",
    "朧": "胧",
    "朮": "术",
    "東": "东",
    "枴": "拐",
    "柵": "栅",
    "柺": "拐",
    "査": "查",
    "桿": "杆",
    "梔": "栀",
    "梘": "枧",
    "條": "条",
    "梟": "枭",
    "梲": "棁",
    "棄": "弃",
    "棊": "棋",
    "棖": "枨",
    "棗": "枣",
    "棟": "栋",
    "棡": "㭎",
    "棧": "栈",
    "棲": "栖",
    "棶": "梾",
    "椲": "㭏",
    "楊": "杨",
    "楓": "枫",
    "楨": "桢",
    "業": "业",
    "極": "极",
    "榘": "矩",
    "榦": "干",
    "榪": "杩",
    "榮": "荣",
    "榲": "榅",
    "榿": "桤",
    "構": "构",
    "槍": "枪",
    "槓": "杠",
    "槤": "梿",
    "槧": "椠",
    "槨": "椁",
    "槮": "椮",
    "槳": "桨",
    "槶": "椢",
    "槼": "椝",
    "樁": "桩",
    "樂": "乐",
    "樅": "枞",
    "樑": "梁",
    "樓": "楼",
    "標": "标",
    "樞": "枢",
    "樢": "㭤",
    "樣": "样",
    "樧": "榝",
    "樫": "㭴",
    "樳": "桪",
    "樸": "朴",
    "樹": "树",
    "樺": "桦",
    "樿": "椫",
    "橈": "桡",
    "橋": "桥",
    "機": "机",
    "橢": "椭",
    "橫": "横",
    "檁": "檩",
    "檉": "柽",
    "檔": "档",
    "檜": "桧",
    "檟": "槚",
    "檢": "检",
    "檣": "樯",
    "檮": "梼",
    "檯": "台",
    "檳": "槟",
    "檸": "柠",
    "檻": "槛",
    "櫃": "柜",
    "櫓": "橹",
    "櫚": "榈",
    "櫛": "栉",
    "櫝": "椟",
    "櫞": "橼",
    "櫟": "栎",
    "櫥": "橱",
    "櫧": "槠",
    "櫨": "栌",
    "櫪": "枥",
    "櫫": "橥",
    "櫬": "榇",
    "櫱": "蘖",
    "櫳": "栊",
    "櫸": "榉",
    "櫻": "樱",
    "欄": "栏",
    "欅": "榉",
    "權": "权",
    "欏": "椤",
    "欒": "栾",
    "欖": "榄",
    "欞": "棂",
    "欽": "钦",
    "歎": "叹",
    "歐": "欧",
    "歟": "欤",
    "歡": "欢",
    "歲": "岁",
    "歷": "历",
    "歸": "归",
    "歿": "殁",
    "殘": "残",
    "殞": "殒",
    "殤": "殇",
    "殨": "㱮",
    "殫": "殚",
    "殭": "僵",
    "殮": "殓",
    "殯": "殡",
    "殰": "㱩",
    "殲": "歼",
    "殺": "杀",
    "殻": "壳",
    "殼": "壳",
    "毀": "毁",
    "毆": "殴",
    "毿": "毵",
    "氂": "牦",
    "氈": "毡",
    "氌": "氇",
    "氣": "气",
    "氫": "氢",
    "氬": "氩",
    "氳": "氲",
    "汎": "泛",
    "汙": "污",
    "決": "决",
    "沒": "没",
    "沖": "冲",
    "況": "况",
    "泝": "溯",
    "洩": "泄",
    "洶": "汹",
    "浹": "浃",
    "涇": "泾",
    "涗": "涚",
    "涼": "凉",
    "淒": "凄",
    "淚": "泪",
    "淥": "渌",
    "淨": "净",
    "淩": "凌",
    "淪": "沦",
    "淵": "渊",
    "淶": "涞",
    "淺": "浅",
    "渙": "涣",
    "減": "减",
    "渢": "沨",
    "渦": "涡",
    "測": "测",
    "渾": "浑",
    "湊": "凑",
    "湞": "浈",
    "湧": "涌",
    "湯": "汤",
    "溈": "沩",
    "準": "准",
    "溝": "沟",
    "溫": "温",
    "溮": "浉",
    "溳": "涢",
    "溼": "湿",
    "滄": "沧",
    "滅": "灭",
    "滌": "涤",
    "滎": "荥",
    "滙": "汇",
    "滬": "沪",
    "滯": "滞",
    "滲": "渗",
    "滷": "卤",
    "滸": "浒",
    "滻": "浐",
    "滾": "滚",
    "滿": "满",
    "漁": "渔",
    "漊": "溇",
    "漚": "沤",
    "漢": "汉",
    "漣": "涟",
    "漬": "渍",
    "漲": "涨",
    "漵": "溆",
    "漸": "渐",
    "漿": "浆",
    "潁": "颍",
    "潑": "泼",
    "潔": "洁",
    "潙": "沩",
    "潚": "㴋",
    "潛": "潜",
    "潤": "润",
    "潯": "浔",
    "潰": "溃",
    "潷": "滗",
    "潿": "涠",
    "澀": "涩",
    "澆": "浇",
    "澇": "涝",
    "澐": "沄",
    "澗": "涧",
    "澠": "渑",
    "澤": "泽",
    "澦": "滪",
    "澩": "泶",
    "澮": "浍",
    "澱": "淀",
    "澾": "㳠",
    "濁": "浊",
    "濃": "浓",
    "濄": "㳡",
    "濕": "湿",
    "濘": "泞",
    "濚": "溁",
    "濛": "蒙",
    "濜": "浕",
    "濟": "济",
    "濤": "涛",
    "濧": "㳔",
    "濫": "滥",
    "濰": "潍",
    "濱": "滨",
    "濺": "溅",
    "濼": "泺",
    "濾": "滤",
    "瀂": "澛",
    "瀅": "滢",
    "瀆": "渎",
    "瀇": "㲿",
    "瀉": "泻",
    "瀏": "浏",
    "瀕": "濒",
    "瀘": "泸",
    "瀝": "沥",
    "瀟": "潇",
    "瀠": "潆",
    "瀦": "潴",
    "瀧": "泷",
    "瀨": "濑",
    "瀲": "潋",
    "瀾": "澜",
    "灃": "沣",
    "灄": "滠",
    "灑": "洒",
    "灕": "漓",
    "灘": "滩",
    "灝": "灏",
    "灡": "㳕",
    "灣": "湾",
    "灤": "滦",
    "灧": "滟",
    "灩": "滟",
    "災": "灾",
    "為": "为",
    "烏": "乌",
    "烴": "烃",
    "無": "无",
    "煉": "炼",
    "煒": "炜",
    "煙": "烟",
    "煢": "茕",
    "煥": "焕",
    "煩": "烦",
    "煬": "炀",
    "煱": "㶽",
    "熅": "煴",
    "熒": "荧",
    "熗": "炝",
    "熱": "热",
    "熲": "颎",
    "熾": "炽",
    "燁": "烨",
    "燈": "灯",
    "燉": "炖",
    "燒": "烧",
    "燙": "烫",
    "燜": "焖",
    "營": "营",
    "燦": "灿",
    "燬": "毁",
    "燭": "烛",
    "燴": "烩",
    "燶": "㶶",
    "燻": "熏",
    "燼": "烬",
    "燾": "焘",
    "爍": "烁",
    "爐": "炉",
    "爛": "烂",
    "爭": "争",
    "爲": "为",
    "爺": "爷",
    "爾": "尔",
    "牀": "床",
    "牆": "墙",
    "牘": "牍",
    "牴": "抵",
    "牽": "牵",
    "犖": "荦",
    "犛": "牦",
    "犢": "犊",
    "犧": "牺",
    "狀": "状",
    "狹": "狭",
    "狽": "狈",
    "猙": "狰",
    "猶": "犹",
    "猻": "狲",
    "獁": "犸",
    "獃": "呆",
    "獄": "狱",
    "獅": "狮",
    "獎": "奖",
    "獨": "独",
    "獪": "狯",
    "獫": "猃",
    "獮": "狝",
    "獰": "狞",
    "獱": "㺍",
    "獲": "获",
    "獵": "猎",
    "獷": "犷",
    "獸": "兽",
    "獺": "獭",
    "獻": "献",
    "獼": "猕",
    "玀": "猡",
    "現": "现",
    "琱": "雕",
    "琺": "珐",
    "琿": "珲",
    "瑋": "玮",
    "瑒": "玚",
    "瑣": "琐",
    "瑤": "瑶",
    "瑩": "莹",
    "瑪": "玛",
    "瑲": "玱",
    "璉": "琏",
    "璡": "琎",
    "璣": "玑",
    "璦": "瑷",
    "璫": "珰",
    "璯": "㻅",
    "環": "环",
    "璵": "玙",
    "璸": "瑸",
    "璽": "玺",
    "璿": "璇",
    "瓊": "琼",
    "瓏": "珑",
    "瓔": "璎",
    "瓚": "瓒",
    "甌": "瓯",
    "甕": "瓮",
    "產": "产",
    "産": "产",
    "畝": "亩",
    "畢": "毕",
    "畫": "画",
    "異": "异",
    "畵": "画",
    "當": "当",
    "疇": "畴",
    "疊": "叠",
    "痙": "痉",
    "痠": "酸",
    "痾": "疴",
    "瘂": "痖",
    "瘋": "疯",
    "瘍": "疡",
    "瘓": "痪",
    "瘞": "瘗",
    "瘡": "疮",
    "瘧": "疟",
    "瘮": "瘆",
    "瘲": "疭",
    "瘺": "瘘",
    "瘻": "瘘",
    "療": "疗",
    "癆": "痨",
    "癇": "痫",
    "癉": "瘅",
    "癒": "愈",
    "癘": "疠",
    "癟": "瘪",
    "癡": "痴",
    "癢": "痒",
    "癤": "疖",
    "癥": "症",
    "癧": "疬",
    "癩": "癞",
    "癬": "癣",
    "癭": "瘿",
    "癮": "瘾",
    "癰": "痈",
    "癱": "瘫",
    "癲": "癫",
    "發": "发",
    "皁": "皂",
    "皚": "皑",
    "皰": "疱",
    "皸": "皲",
    "皺": "皱",
    "盃": "杯",
    "盜": "盗",
    "盞": "盏",
    "盡": "尽",
    "監": "监",
    "盤": "盘",
    "盧": "卢",
    "盪": "荡",
    "眞": "真",
    "眥": "眦",
    "眾": "众",
    "睏": "困",
    "睜": "睁",
    "睞": "睐",
    "瞘": "眍",
    "瞜": "䁖",
    "瞞": "瞒",
    "瞶": "瞆",
    "瞼": "睑",
    "矇": "蒙",
    "矓": "眬",
    "矚": "瞩",
    "矯": "矫",
    "硃": "朱",
    "硜": "硁",
    "硤": "硖",
    "硨": "砗",
    "硯": "砚",
    "碕": "埼",
    "碩": "硕",
    "碭": "砀",
    "碸": "砜",
    "確": "确",
    "碼": "码",
    "碽": "䂵",
    "磑": "硙",
    "磚": "砖",
    "磠": "硵",
    "磣": "碜",
    "磧": "碛",
    "磯": "矶",
    "磽": "硗",
    "磾": "䃅",
    "礄": "硚",
    "礎": "础",
    "礙": "碍",
    "礦": "矿",
    "礪": "砺",
    "礫": "砾",
    "礬": "矾",
    "礱": "砻",
    "祿": "禄",
    "禍": "祸",
    "禎": "祯",
    "禕": "祎",
    "禡": "祃",
    "禦": "御",
    "禪": "禅",
    "禮": "礼",
    "禰": "祢",
    "禱": "祷",
    "禿": "秃",
    "秈": "籼",
    "稅": "税",
    "稈": "秆",
    "稏": "䅉",
    "稜": "棱",
    "稟": "禀",
    "種": "种",
    "稱": "称",
    "穀": "谷",
    "穇": "䅟",
    "穌": "稣",
    "積": "积",
    "穎": "颖",
    "穠": "秾",
    "穡": "穑",
    "穢": "秽",
    "穩": "稳",
    "穫": "获",
    "穭": "穞",
    "窩": "窝",
    "窪": "洼",
    "窮": "穷",
    "窯": "窑",
    "窵": "窎",
    "窶": "窭",
    "窺": "窥",
    "竄": "窜",
    "竅": "窍",
    "竇": "窦",
    "竈": "灶",
    "竊": "窃",
    "竪": "竖",
    "競": "竞",
    "筆": "笔",
    "筍": "笋",
    "筧": "笕",
    "筴": "䇲",
    "箇": "个",
    "箋": "笺",
    "箏": "筝",
    "節": "节",
    "範": "范",
    "築": "筑",
    "篋": "箧",
    "篔": "筼",
    "篠": "筿",
    "篤": "笃",
    "篩": "筛",
    "篳": "筚",
    "簀": "箦",
    "簍": "篓",
    "簑": "蓑",
    "簞": "箪",
    "簡": "简",
    "簣": "篑",
    "簫": "箫",
    "簹": "筜",
    "簽": "签",
    "簾": "帘",
    "籃": "篮",
    "籌": "筹",
    "籔": "䉤",
    "籙": "箓",
    "籛": "篯",
    "籜": "箨",
    "籟": "籁",
    "籠": "笼",
    "籤": "签",
    "籩": "笾",
    "籪": "簖",
    "籬": "篱",
    "籮": "箩",
    "籲": "吁",
    "粵": "粤",
    "糉": "粽",
    "糝": "糁",
    "糞": "粪",
    "糧": "粮",
    "糰": "团",
    "糲": "粝",
    "糴": "籴",
    "糶": "粜",
    "糹": "纟",
    "糾": "纠",
    "紀": "纪",
    "紂": "纣",
    "約": "约",
    "紅": "红",
    "紆": "纡",
    "紇": "纥",
    "紈": "纨",
    "紉": "纫",
    "紋": "纹",
    "納": "纳",
    "紐": "纽",
    "紓": "纾",
    "純": "纯",
    "紕": "纰",
    "紖": "纼",
    "紗": "纱",
    "紘": "纮",
    "紙": "纸",
    "級": "级",
    "紛": "纷",
    "紜": "纭",
    "紝": "纴",
    "紡": "纺",
    "紬": "䌷",
    "紮": "扎",
    "細": "细",
    "紱": "绂",
    "紲": "绁",
    "紳": "绅",
    "紵": "纻",
    "紹": "绍",
    "紺": "绀",
    "紼": "绋",
    "紿": "绐",
    "絀": "绌",
    "終": "终",
    "絃": "弦",
    "組": "组",
    "絅": "䌹",
    "絆": "绊",
    "絎": "绗",
    "結": "结",
    "絕": "绝",
    "絛": "绦",
    "絝": "绔",
    "絞": "绞",
    "絡": "络",
    "絢": "绚",
    "給": "给",
    "絨": "绒",
    "絰": "绖",
    "統": "统",
    "絲": "丝",
    "絳": "绛",
    "絶": "绝",
    "絹": "绢",
    "綁": "绑",
    "綃": "绡",
    "綆": "绠",
    "綈": "绨",
    "綉": "绣",
    "綌": "绤",
    "綏": "绥",
    "綐": "䌼",
    "綑": "捆",
    "經": "经",
    "綜": "综",
    "綞": "缍",
    "綠": "绿",
    "綢": "绸",
    "綣": "绻",
    "綫": "线",
    "綬": "绶",
    "維": "维",
    "綯": "绹",
    "綰": "绾",
    "綱": "纲",
    "網": "网",
    "綳": "绷",
    "綴": "缀",
    "綸": "纶",
    "綹": "绺",
    "綺": "绮",
    "綻": "绽",
    "綽": "绰",
    "綾": "绫",
    "綿": "绵",
    "緄": "绲",
    "緇": "缁",
    "緊": "紧",
    "緋": "绯",
    "緑": "绿",
    "緒": "绪",
    "緓": "绬",
    "緔": "绱",
    "緗": "缃",
    "緘": "缄",
    "緙": "缂",
    "緝": "缉",
    "緞": "缎",
    "締": "缔",
    "緡": "缗",
    "緣": "缘",
    "緦": "缌",
    "編": "编",
    "緩": "缓",
    "緬": "缅",
    "緯": "纬",
    "緱": "缑",
    "緲": "缈",
    "練": "练",
    "緶": "缏",
    "緹": "缇",
    "緻": "致",
    "緼": "缊",
    "縈": "萦",
    "縉": "缙",
    "縊": "缢",
    "縋": "缒",
    "縐": "绉",
    "縑": "缣",
    "縕": "缊",
    "縗": "缞",
    "縛": "缚",
    "縝": "缜",
    "縞": "缟",
    "縟": "缛",
    "縣": "县",
    "縧": "绦",
    "縫": "缝",
    "縭": "缡",
    "縮": "缩",
    "縱": "纵",
    "縲": "缧",
    "縳": "䌸",
    "縴": "纤",
    "縵": "缦",
    "縶": "絷",
    "縷": "缕",
    "縹": "缥",
    "總": "总",
    "績": "绩",
    "繃": "绷",
    "繅": "缫",
    "繆": "缪",
    "繒": "缯",
    "織": "织",
    "繕": "缮",
    "繚": "缭",
    "繞": "绕",
    "繡": "绣",
    "繢": "缋",
    "繩": "绳",
    "繪": "绘",
    "繫": "系",
    "繭": "茧",
    "繮": "缰",
    "繯": "缳",
    "繰": "缲",
    "繳": "缴",
    "繸": "䍁",
    "繹": "绎",
    "繼": "继",
    "繽": "缤",
    "繾": "缱",
    "繿": "䍀",
    "纇": "颣",
    "纈": "缬",
    "纊": "纩",
    "續": "续",
    "纍": "累",
    "纏": "缠",
    "纓": "缨",
    "纔": "才",
    "纖": "纤",
    "纘": "缵",
    "纜": "缆",
    "缽": "钵",
    "罃": "䓨",
    "罈": "坛",
    "罌": "罂",
    "罎": "坛",
    "罰": "罚",
    "罵": "骂",
    "罷": "罢",
    "羅": "罗",
    "羆": "罴",
    "羈": "羁",
    "羋": "芈",
    "羣": "群",
    "羥": "羟",
    "羨": "羡",
    "義": "义",
    "羶": "膻",
    "習": "习",
    "翫": "玩",
    "翬": "翚",
    "翹": "翘",
    "翽": "翙",
    "耬": "耧",
    "耮": "耢",
    "聖": "圣",
    "聞": "闻",
    "聯": "联",
    "聰": "聪",
    "聲": "声",
    "聳": "耸",
    "聵": "聩",
    "聶": "聂",
    "職": "职",
    "聹": "聍",
    "聽": "听",
    "聾": "聋",
    "肅": "肃",
    "脅": "胁",
    "脈": "脉",
    "脛": "胫",
    "脣": "唇",
    "脫": "脱",
    "脹": "胀",
    "腎": "肾",
    "腖": "胨",
    "腡": "脶",
    "腦": "脑",
    "腫": "肿",
    "腳": "脚",
    "腸": "肠",
    "膃": "腽",
    "膕": "腘",
    "膚": "肤",
    "膞": "䏝",
    "膠": "胶",
    "膩": "腻",
    "膽": "胆",
    "膾": "脍",
    "膿": "脓",
    "臉": "脸",
    "臍": "脐",
    "臏": "膑",
    "臘": "腊",
    "臚": "胪",
    "臟": "脏",
    "臠": "脔",
    "臢": "臜",
    "臥": "卧",
    "臨": "临",
    "臺": "台",
    "與": "与",
    "興": "兴",
    "舉": "举",
    "舊": "旧",
    "舖": "铺",
    "舘": "馆",
    "艙": "舱",
    "艤": "舣",
    "艦": "舰",
    "艫": "舻",
    "艱": "艰",
    "艷": "艳",
    "芻": "刍",
    "苧": "苎",
    "茲": "兹",
    "荊": "荆",
    "莊": "庄",
    "莖": "茎",
    "莢": "荚",
    "莧": "苋",
    "華": "华",
    "菴": "庵",
    "菸": "烟",
    "萇": "苌",
    "萊": "莱",
    "萬": "万",
    "萴": "荝",
    "萵": "莴",
    "葉": "叶",
    "葒": "荭",
    "葤": "荮",
    "葦": "苇",
    "葯": "药",
    "葷": "荤",
    "蒓": "莼",
    "蒔": "莳",
    "蒕": "蒀",
    "蒞": "莅",
    "蒼": "苍",
    "蓀": "荪",
    "蓆": "席",
    "蓋": "盖",
    "蓮": "莲",
    "蓯": "苁",
    "蓴": "莼",
    "蓽": "荜",
    "蔔": "卜",
    "蔘": "参",
    "蔞": "蒌",
    "蔣": "蒋",
    "蔥": "葱",
    "蔦": "茑",
    "蔭": "荫",
    "蕁": "荨",
    "蕆": "蒇",
    "蕎": "荞",
    "蕒": "荬",
    "蕓": "芸",
    "蕕": "莸",
    "蕘": "荛",
    "蕢": "蒉",
    "蕩": "荡",
    "蕪": "芜",
    "蕭": "萧",
    "蕷": "蓣",
    "薀": "蕰",
    "薈": "荟",
    "薊": "蓟",
    "薌": "芗",
    "薑": "姜",
    "薔": "蔷",
    "薘": "荙",
    "薟": "莶",
    "薦": "荐",
    "薩": "萨",
    "薳": "䓕",
    "薴": "苧",
    "薵": "䓓",
    "薺": "荠",
    "藍": "蓝",
    "藎": "荩",
    "藝": "艺",
    "藥": "药",
    "藪": "薮",
    "藭": "䓖",
    "藴": "蕴",
    "藶": "苈",
    "藹": "蔼",
    "藺": "蔺",
    "蘀": "萚",
    "蘄": "蕲",
    "蘆": "芦",
    "蘇": "苏",
    "蘊": "蕴",
    "蘚": "藓",
    "蘞": "蔹",
    "蘢": "茏",
    "蘭": "兰",
    "蘺": "蓠",
    "蘿": "萝",
    "虆": "蔂",
    "處": "处",
    "虛": "虚",
    "虜": "虏",
    "號": "号",
    "虧": "亏",
    "虯": "虬",
    "蛺": "蛱",
    "蛻": "蜕",
    "蜆": "蚬",
    "蝕": "蚀",
    "蝟": "猬",
    "蝦": "虾",
    "蝨": "虱",
    "蝸": "蜗",
    "螄": "蛳",
    "螞": "蚂",
    "螢": "萤",
    "螮": "䗖",
    "螻": "蝼",
    "螿": "螀",
    "蟄": "蛰",
    "蟈": "蝈",
    "蟎": "螨",
    "蟣": "虮",
    "蟬": "蝉",
    "蟯": "蛲",
    "蟲": "虫",
    "蟶": "蛏",
    "蟻": "蚁",
    "蠁": "蚃",
    "蠅": "蝇",
    "蠆": "虿",
    "蠍": "蝎",
    "蠐": "蛴",
    "蠑": "蝾",
    "蠔": "蚝",
    "蠟": "蜡",
    "蠣": "蛎",
    "蠨": "蟏",
    "蠱": "蛊",
    "蠶": "蚕",
    "蠻": "蛮",
    "衆": "众",
    "衊": "蔑",
    "術": "术",
    "衕": "同",
    "衚": "胡",
    "衛": "卫",
    "衝": "冲",
    "袞": "衮",
    "裊": "袅",
    "裏": "里",
    "補": "补",
    "裝": "装",
    "裡": "里",
    "製": "制",
    "複": "复",
    "褌": "裈",
    "褘": "袆",
    "褲": "裤",
    "褳": "裢",
    "褸": "褛",
    "褻": "亵",
    "襇": "裥",
    "襉": "裥",
    "襏": "袯",
    "襖": "袄",
    "襝": "裣",
    "襠": "裆",
    "襤": "褴",
    "襪": "袜",
    "襯": "衬",
    "襲": "袭",
    "襴": "襕",
    "覈": "核",
    "見": "见",
    "覎": "觃",
    "規": "规",
    "覓": "觅",
    "視": "视",
    "覘": "觇",
    "覡": "觋",
    "覥": "觍",
    "覦": "觎",
    "親": "亲",
    "覬": "觊",
    "覯": "觏",
    "覲": "觐",
    "覷": "觑",
    "覺": "觉",
    "覽": "览",
    "覿": "觌",
    "觀": "观",
    "觴": "觞",
    "觶": "觯",
    "觸": "触",
    "訁": "讠",
    "訂": "订",
    "訃": "讣",
    "計": "计",
    "訊": "讯",
    "訌": "讧",
    "討": "讨",
    "訐": "讦",
    "訒": "讱",
    "訓": "训",
    "訕": "讪",
    "訖": "讫",
    "記": "记",
    "訛": "讹",
    "訝": "讶",
    "訟": "讼",
    "訣": "诀",
    "訥": "讷",
    "訩": "讻",
    "訪": "访",
    "設": "设",
    "許": "许",
    "訴": "诉",
    "訶": "诃",
    "診": "诊",
    "註": "注",
    "証": "证",
    "詁": "诂",
    "詆": "诋",
    "詎": "讵",
    "詐": "诈",
    "詒": "诒",
    "詔": "诏",
    "評": "评",
    "詖": "诐",
    "詗": "诇",
    "詘": "诎",
    "詛": "诅",
    "詞": "词",
    "詠": "咏",
    "詡": "诩",
    "詢": "询",
    "詣": "诣",
    "試": "试",
    "詩": "诗",
    "詫": "诧",
    "詬": "诟",
    "詭": "诡",
    "詮": "诠",
    "詰": "诘",
    "話": "话",
    "該": "该",
    "詳": "详",
    "詵": "诜",
    "詼": "诙",
    "詿": "诖",
    "誄": "诔",
    "誅": "诛",
    "誆": "诓",
    "誇": "夸",
    "誌": "志",
    "認": "认",
    "誑": "诳",
    "誒": "诶",
    "誕": "诞",
    "誘": "诱",
    "誚": "诮",
    "語": "语",
    "誠": "诚",
    "誡": "诫",
    "誣": "诬",
    "誤": "误",
    "誥": "诰",
    "誦": "诵",
    "誨": "诲",
    "說": "说",
    "説": "说",
    "誰": "谁",
    "課": "课",
    "誶": "谇",
    "誹": "诽",
    "誼": "谊",
    "誾": "訚",
    "調": "调",
    "諂": "谄",
    "諄": "谆",
    "談": "谈",
    "諉": "诿",
    "請": "请",
    "諍": "诤",
    "諏": "诹",
    "諑": "诼",
    "諒": "谅",
    "論": "论",
    "諗": "谂",
    "諛": "谀",
    "諜": "谍",
    "諝": "谞",
    "諞": "谝",
    "諡": "谥",
    "諢": "诨",
    "諤": "谔",
    "諦": "谛",
    "諧": "谐",
    "諭": "谕",
    "諱": "讳",
    "諳": "谙",
    "諶": "谌",
    "諷": "讽",
    "諸": "诸",
    "諺": "谚",
    "諼": "谖",
    "諾": "诺",
    "謀": "谋",
    "謁": "谒",
    "謂": "谓",
    "謄": "誊",
    "謅": "诌",
    "謊": "谎",
    "謎": "谜",
    "謐": "谧",
    "謔": "谑",
    "謖": "谡",
    "謗": "谤",
    "謙": "谦",
    "謚": "谥",
    "講": "讲",
    "謝": "谢",
    "謠": "谣",
    "謡": "谣",
    "謨": "谟",
    "謫": "谪",
    "謬": "谬",
    "謭": "谫",
    "謳": "讴",
    "謹": "谨",
    "謾": "谩",
    "譁": "哗",
    "證": "证",
    "譎": "谲",
    "譏": "讥",
    "譖": "谮",
    "識": "识",
    "譙": "谯",
    "譚": "谭",
    "譜": "谱",
    "譟": "噪",
    "譫": "谵",
    "譭": "毁",
    "譯": "译",
    "議": "议",
    "譴": "谴",
    "護": "护",
    "譸": "诪",
    "譽": "誉",
    "讀": "读",
    "讅": "谉",
    "變": "变",
    "讋": "詟",
    "讌": "䜩",
    "讒": "谗",
    "讓": "让",
    "讕": "谰",
    "讖": "谶",
    "讚": "赞",
    "讜": "谠",
    "讞": "谳",
    "豈": "岂",
    "豎": "竖",
    "豐": "丰",
    "豔": "艳",
    "豬": "猪",
    "豶": "豮",
    "貍": "狸",
    "貓": "猫",
    "貙": "䝙",
    "貝": "贝",
    "貞": "贞",
    "貟": "贠",
    "負": "负",
    "財": "财",
    "貢": "贡",
    "貧": "贫",
    "貨": "货",
    "販": "贩",
    "貪": "贪",
    "貫": "贯",
    "責": "责",
    "貯": "贮",
    "貰": "贳",
    "貳": "贰",
    "貴": "贵",
    "貶": "贬",
    "貸": "贷",
    "貺": "贶",
    "費": "费",
    "貼": "贴",
    "貽": "贻",
    "貿": "贸",
    "賀": "贺",
    "賁": "贲",
    "賂": "赂",
    "賃": "赁",
    "賄": "贿",
    "賅": "赅",
    "資": "资",
    "賈": "贾",
    "賊": "贼",
    "賑": "赈",
    "賒": "赊",
    "賓": "宾",
    "賕": "赇",
    "賙": "赒",
    "賚": "赉",
    "賜": "赐",
    "賞": "赏",
    "賠": "赔",
    "賡": "赓",
    "賢": "贤",
    "賣": "卖",
    "賤": "贱",
    "賦": "赋",
    "賧": "赕",
    "質": "质",
    "賫": "赍",
    "賬": "账",
    "賭": "赌",
    "賰": "䞐",
    "賴": "赖",
    "賵": "赗",
    "賺": "赚",
    "賻": "赙",
    "購": "购",
    "賽": "赛",
    "賾": "赜",
    "贄": "贽",
    "贅": "赘",
    "贇": "赟",
    "贈": "赠",
    "贊": "赞",
    "贋": "赝",
    "贍": "赡",
    "贏": "赢",
    "贐": "赆",
    "贓": "赃",
    "贔": "赑",
    "贖": "赎",
    "贗": "赝",
    "贛": "赣",
    "贜": "赃",
    "赬": "赪",
    "趕": "赶",
    "趙": "赵",
    "趨": "趋",
    "趲": "趱",
    "跡": "迹",
    "踐": "践",
    "踰": "逾",
    "踴": "踊",
    "蹌": "跄",
    "蹕": "跸",
    "蹟": "迹",
    "蹠": "跖",
    "蹣": "蹒",
    "蹤": "踪",
    "蹺": "跷",
    "躂": "跶",
    "躉": "趸",
    "躊": "踌",
    "躋": "跻",
    "躍": "跃",
    "躎": "䟢",
    "躑": "踯",
    "躒": "跞",
    "躓": "踬",
    "躕": "蹰",
    "躚": "跹",
    "躡": "蹑",
    "躥": "蹿",
    "躦": "躜",
    "躪": "躏",
    "軀": "躯",
    "車": "车",
    "軋": "轧",
    "軌": "轨",
    "軍": "军",
    "軑": "轪",
    "軒": "轩",
    "軔": "轫",
    "軛": "轭",
    "軟": "软",
    "軤": "轷",
    "軫": "轸",
    "軲": "轱",
    "軸": "轴",
    "軹": "轵",
    "軺": "轺",
    "軻": "轲",
    "軼": "轶",
    "軾": "轼",
    "較": "较",
    "輅": "辂",
    "輇": "辁",
    "輈": "辀",
    "載": "载",
    "輊": "轾",
    "輒": "辄",
    "輓": "挽",
    "輔": "辅",
    "輕": "轻",
    "輛": "辆",
    "輜": "辎",
    "輝": "辉",
    "輞": "辋",
    "輟": "辍",
    "輥": "辊",
    "輦": "辇",
    "輩": "辈",
    "輪": "轮",
    "輬": "辌",
    "輯": "辑",
    "輳": "辏",
    "輸": "输",
    "輻": "辐",
    "輼": "辒",
    "輾": "辗",
    "輿": "舆",
    "轀": "辒",
    "轂": "毂",
    "轄": "辖",
    "轅": "辕",
    "轆": "辘",
    "轉": "转",
    "轍": "辙",
    "轎": "轿",
    "轔": "辚",
    "轟": "轰",
    "轡": "辔",
    "轢": "轹",
    "轤": "轳",
    "辦": "办",
    "辭": "辞",
    "辮": "辫",
    "辯": "辩",
    "農": "农",
    "迴": "回",
    "這": "这",
    "連": "连",
    "週": "周",
    "進": "进",
    "遊": "游",
    "運": "运",
    "過": "过",
    "達": "达",
    "違": "违",
    "遙": "遥",
    "遜": "逊",
    "遞": "递",
    "遠": "远",
    "遡": "溯",
    "適": "适",
    "遲": "迟",
    "遶": "绕",
    "遷": "迁",
    "選": "选",
    "遺": "遗",
    "遼": "辽",
    "邁": "迈",
    "還": "还",
    "邇": "迩",
    "邊": "边",
    "邏": "逻",
    "邐": "逦",
    "郟": "郏",
    "郵": "邮",
    "鄆": "郓",
    "鄉": "乡",
    "鄒": "邹",
    "鄔": "邬",
    "鄖": "郧",
    "鄧": "邓",
    "鄭": "郑",
    "鄰": "邻",
    "鄲": "郸",
    "鄴": "邺",
    "鄶": "郐",
    "鄺": "邝",
    "酇": "酂",
    "酈": "郦",
    "醃": "腌",
    "醖": "酝",
    "醜": "丑",
    "醞": "酝",
    "醟": "蒏",
    "醣": "糖",
    "醫": "医",
    "醬": "酱",
    "醱": "酦",
    "釀": "酿",
    "釁": "衅",
    "釃": "酾",
    "釅": "酽",
    "釋": "释",
    "釒": "钅",
    "釓": "钆",
    "釔": "钇",
    "釕": "钌",
    "釗": "钊",
    "釘": "钉",
    "釙": "钋",
    "針": "针",
    "釣": "钓",
    "釤": "钐",
    "釦": "扣",
    "釧": "钏",
    "釩": "钒",
    "釵": "钗",
    "釷": "钍",
    "釹": "钕",
    "釺": "钎",
    "釾": "䥺",
    "鈀": "钯",
    "鈁": "钫",
    "鈃": "钘",
    "鈄": "钭",
    "鈅": "钥",
    "鈈": "钚",
    "鈉": "钠",
    "鈍": "钝",
    "鈎": "钩",
    "鈐": "钤",
    "鈑": "钣",
    "鈒": "钑",
    "鈔": "钞",
    "鈕": "钮",
    "鈞": "钧",
    "鈡": "钟",
    "鈣": "钙",
    "鈥": "钬",
    "鈦": "钛",
    "鈧": "钪",
    "鈮": "铌",
    "鈰": "铈",
    "鈳": "钶",
    "鈴": "铃",
    "鈷": "钴",
    "鈸": "钹",
    "鈹": "铍",
    "鈺": "钰",
    "鈽": "钸",
    "鈾": "铀",
    "鈿": "钿",
    "鉀": "钾",
    "鉆": "钻",
    "鉈": "铊",
    "鉉": "铉",
    "鉋": "铇",
    "鉍": "铋",
    "鉑": "铂",
    "鉕": "钷",
    "鉗": "钳",
    "鉚": "铆",
    "鉛": "铅",
    "鉞": "钺",
    "鉢": "钵",
    "鉤": "钩",
    "鉦": "钲",
    "鉬": "钼",
    "鉭": "钽",
    "鉳": "锫",
    "鉶": "铏",
    "鉸": "铰",
    "鉺": "铒",
    "鉻": "铬",
    "鉿": "铪",
    "銀": "银",
    "銃": "铳",
    "銅": "铜",
    "銍": "铚",
    "銑": "铣",
    "銓": "铨",
    "銖": "铢",
    "銘": "铭",
    "銚": "铫",
    "銛": "铦",
    "銜": "衔",
    "銠": "铑",
    "銣": "铷",
    "銥": "铱",
    "銦": "铟",
    "銨": "铵",
    "銩": "铥",
    "銪": "铕",
    "銫": "铯",
    "銬": "铐",
    "銱": "铞",
    "銳": "锐",
    "銷": "销",
    "銹": "锈",
    "銻": "锑",
    "銼": "锉",
    "鋁": "铝",
    "鋃": "锒",
    "鋅": "锌",
    "鋇": "钡",
    "鋌": "铤",
    "鋏": "铗",
    "鋒": "锋",
    "鋙": "铻",
    "鋝": "锊",
    "鋟": "锓",
    "鋣": "铘",
    "鋤": "锄",
    "鋥": "锃",
    "鋦": "锔",
    "鋨": "锇",
    "鋩": "铓",
    "鋪": "铺",
    "鋭": "锐",
    "鋮": "铖",
    "鋯": "锆",
    "鋰": "锂",
    "鋱": "铽",
    "鋶": "锍",
    "鋸": "锯",
    "鋼": "钢",
    "錁": "锞",
    "錄": "录",
    "錆": "锖",
    "錇": "锫",
    "錈": "锩",
    "錏": "铔",
    "錐": "锥",
    "錒": "锕",
    "錕": "锟",
    "錘": "锤",
    "錙": "锱",
    "錚": "铮",
    "錛": "锛",
    "錟": "锬",
    "錠": "锭",
    "錡": "锜",
    "錢": "钱",
    "錦": "锦",
    "錨": "锚",
    "錩": "锠",
    "錫": "锡",
    "錮": "锢",
    "錯": "错",
    "録": "录",
    "錳": "锰",
    "錶": "表",
    "錸": "铼",
    "錼": "镎",
    "鍀": "锝",
    "鍁": "锨",
    "鍃": "锪",
    "鍅": "钫",
    "鍆": "钔",
    "鍇": "锴",
    "鍈": "锳",
    "鍋": "锅",
    "鍍": "镀",
    "鍔": "锷",
    "鍘": "铡",
    "鍚": "钖",
    "鍛": "锻",
    "鍠": "锽",
    "鍤": "锸",
    "鍥": "锲",
    "鍩": "锘",
    "鍬": "锹",
    "鍰": "锾",
    "鍵": "键",
    "鍶": "锶",
    "鍺": "锗",
    "鍼": "针",
    "鎂": "镁",
    "鎄": "锿",
    "鎇": "镅",
    "鎊": "镑",
    "鎌": "镰",
    "鎔": "镕",
    "鎖": "锁",
    "鎘": "镉",
    "鎚": "锤",
    "鎛": "镈",
    "鎡": "镃",
    "鎢": "钨",
    "鎣": "蓥",
    "鎦": "镏",
    "鎧": "铠",
    "鎩": "铩",
    "鎪": "锼",
    "鎬": "镐",
    "鎭": "镇",
    "鎮": "镇",
    "鎰": "镒",
    "鎲": "镋",
    "鎳": "镍",
    "鎵": "镓",
    "鎶": "鿔",
    "鎸": "镌",
    "鎿": "镎",
    "鏃": "镞",
    "鏈": "链",
    "鏌": "镆",
    "鏍": "镙",
    "鏐": "镠",
    "鏑": "镝",
    "鏗": "铿",
    "鏘": "锵",
    "鏜": "镗",
    "鏝": "镘",
    "鏞": "镛",
    "鏟": "铲",
    "鏡": "镜",
    "鏢": "镖",
    "鏤": "镂",
    "鏨": "錾",
    "鏰": "镚",
    "鏵": "铧",
    "鏷": "镤",
    "鏹": "镪",
    "鏺": "䥽",
    "鏽": "锈",
    "鐃": "铙",
    "鐋": "铴",
    "鐐": "镣",
    "鐒": "铹",
    "鐓": "镦",
    "鐔": "镡",
    "鐘": "钟",
    "鐙": "镫",
    "鐝": "镢",
    "鐠": "镨",
    "鐥": "䦅",
    "鐦": "锎",
    "鐧": "锏",
    "鐨": "镄",
    "鐫": "镌",
    "鐮": "镰",
    "鐯": "䦃",
    "鐲": "镯",
    "鐳": "镭",
    "鐵": "铁",
    "鐶": "镮",
    "鐸": "铎",
    "鐺": "铛",
    "鐿": "镱",
    "鑄": "铸",
    "鑊": "镬",
    "鑌": "镔",
    "鑑": "鉴",
    "鑒": "鉴",
    "鑔": "镲",
    "鑕": "锧",
    "鑞": "镴",
    "鑠": "铄",
    "鑣": "镳",
    "鑥": "镥",
    "鑭": "镧",
    "鑰": "钥",
    "鑱": "镵",
    "鑲": "镶",
    "鑷": "镊",
    "鑹": "镩",
    "鑼": "锣",
    "鑽": "钻",
    "鑾": "銮",
    "鑿": "凿",
    "钂": "镋",
    "長": "长",
    "門": "门",
    "閂": "闩",
    "閃": "闪",
    "閆": "闫",
    "閈": "闬",
    "閉": "闭",
    "開": "开",
    "閌": "闶",
    "閎": "闳",
    "閏": "闰",
    "閑": "闲",
    "間": "间",
    "閔": "闵",
    "閘": "闸",
    "閡": "阂",
    "閣": "阁",
    "閤": "合",
    "閥": "阀",
    "閨": "闺",
    "閩": "闽",
    "閫": "阃",
    "閬": "阆",
    "閭": "闾",
    "閱": "阅",
    "閲": "阅",
    "閶": "阊",
    "閹": "阉",
    "閻": "阎",
    "閼": "阏",
    "閽": "阍",
    "閾": "阈",
    "閿": "阌",
    "闃": "阒",
    "闆": "板",
    "闇": "暗",
    "闈": "闱",
    "闊": "阔",
    "闋": "阕",
    "闌": "阑",
    "闍": "阇",
    "闐": "阗",
    "闒": "阘",
    "闓": "闿",
    "闔": "阖",
    "闕": "阙",
    "闖": "闯",
    "關": "关",
    "闞": "阚",
    "闠": "阓",
    "闡": "阐",
    "闢": "辟",
    "闤": "阛",
    "闥": "闼",
    "陘": "陉",
    "陝": "陕",
    "陣": "阵",
    "陰": "阴",
    "陳": "陈",
    "陸": "陆",
    "陽": "阳",
    "隉": "陧",
    "隊": "队",
    "階": "阶",
    "隕": "陨",
    "際": "际",
    "隨": "随",
    "險": "险",
    "隯": "陦",
    "隱": "隐",
    "隴": "陇",
    "隸": "隶",
    "隻": "只",
    "雋": "隽",
    "雖": "虽",
    "雙": "双",
    "雛": "雏",
    "雜": "杂",
    "雞": "鸡",
    "離": "离",
    "難": "难",
    "雲": "云",
    "電": "电",
    "霑": "沾",
    "霢": "霡",
    "霧": "雾",
    "霽": "霁",
    "靂": "雳",
    "靄": "霭",
    "靆": "叇",
    "靈": "灵",
    "靉": "叆",
    "靚": "靓",
    "靜": "静",
    "靝": "靔",
    "靨": "靥",
    "鞏": "巩",
    "鞝": "绱",
    "鞦": "秋",
    "鞽": "鞒",
    "韁": "缰",
    "韃": "鞑",
    "韆": "千",
    "韉": "鞯",
    "韋": "韦",
    "韌": "韧",
    "韍": "韨",
    "韓": "韩",
    "韙": "韪",
    "韜": "韬",
    "韞": "韫",
    "韻": "韵",
    "響": "响",
    "頁": "页",
    "頂": "顶",
    "頃": "顷",
    "項": "项",
    "順": "顺",
    "頇": "顸",
    "須": "须",
    "頊": "顼",
    "頌": "颂",
    "頎": "颀",
    "頏": "颃",
    "預": "预",
    "頑": "顽",
    "頒": "颁",
    "頓": "顿",
    "頗": "颇",
    "領": "领",
    "頜": "颌",
    "頡": "颉",
    "頤": "颐",
    "頦": "颏",
    "頭": "头",
    "頮": "颒",
    "頰": "颊",
    "頲": "颋",
    "頴": "颕",
    "頷": "颔",
    "頸": "颈",
    "頹": "颓",
    "頻": "频",
    "頽": "颓",
    "顆": "颗",
    "題": "题",
    "額": "额",
    "顎": "颚",
    "顏": "颜",
    "顒": "颙",
    "顓": "颛",
    "顔": "颜",
    "顙": "颡",
    "顛": "颠",
    "類": "类",
    "顢": "颟",
    "顥": "颢",
    "顧": "顾",
    "顫": "颤",
    "顬": "颥",
    "顯": "显",
    "顰": "颦",
    "顱": "颅",
    "顳": "颞",
    "顴": "颧",
    "風": "风",
    "颭": "飐",
    "颮": "飑",
    "颯": "飒",
    "颱": "台",
    "颳": "刮",
    "颶": "飓",
    "颸": "飔",
    "颻": "飖",
    "颼": "飕",
    "飀": "飗",
    "飄": "飘",
    "飆": "飙",
    "飈": "飚",
    "飛": "飞",
    "飠": "饣",
    "飢": "饥",
    "飣": "饤",
    "飥": "饦",
    "飩": "饨",
    "飪": "饪",
    "飫": "饫",
    "飭": "饬",
    "飯": "饭",
    "飱": "飧",
    "飲": "饮",
    "飴": "饴",
    "飼": "饲",
    "飽": "饱",
    "飾": "饰",
    "飿": "饳",
    "餃": "饺",
    "餄": "饸",
    "餅": "饼",
    "餈": "糍",
    "餉": "饷",
    "養": "养",
    "餌": "饵",
    "餎": "饹",
    "餏": "饻",
    "餑": "饽",
    "餒": "馁",
    "餓": "饿",
    "餕": "馂",
    "餖": "饾",
    "餚": "肴",
    "餛": "馄",
    "餜": "馃",
    "餞": "饯",
    "餡": "馅",
    "館": "馆",
    "餳": "饧",
    "餶": "馉",
    "餷": "馇",
    "餺": "馎",
    "餼": "饩",
    "餾": "馏",
    "餿": "馊",
    "饁": "馌",
    "饃": "馍",
    "饅": "馒",
    "饈": "馐",
    "饉": "馑",
    "饊": "馓",
    "饋": "馈",
    "饌": "馔",
    "饑": "饥",
    "饒": "饶",
    "饗": "飨",
    "饜": "餍",
    "饞": "馋",
    "饢": "馕",
    "馬": "马",
    "馭": "驭",
    "馮": "冯",
    "馱": "驮",
    "馳": "驰",
    "馴": "驯",
    "馹": "驲",
    "駁": "驳",
    "駐": "驻",
    "駑": "驽",
    "駒": "驹",
    "駔": "驵",
    "駕": "驾",
    "駘": "骀",
    "駙": "驸",
    "駛": "驶",
    "駝": "驼",
    "駟": "驷",
    "駡": "骂",
    "駢": "骈",
    "駭": "骇",
    "駰": "骃",
    "駱": "骆",
    "駸": "骎",
    "駿": "骏",
    "騁": "骋",
    "騂": "骍",
    "騅": "骓",
    "騌": "骔",
    "騍": "骒",
    "騎": "骑",
    "騏": "骐",
    "騖": "骛",
    "騙": "骗",
    "騤": "骙",
    "騧": "䯄",
    "騫": "骞",
    "騭": "骘",
    "騮": "骝",
    "騰": "腾",
    "騶": "驺",
    "騷": "骚",
    "騸": "骟",
    "騾": "骡",
    "驀": "蓦",
    "驁": "骜",
    "驂": "骖",
    "驃": "骠",
    "驅": "驱",
    "驊": "骅",
    "驌": "骕",
    "驍": "骁",
    "驏": "骣",
    "驕": "骄",
    "驗": "验",
    "驚": "惊",
    "驛": "驿",
    "驟": "骤",
    "驢": "驴",
    "驤": "骧",
    "驥": "骥",
    "驦": "骦",
    "驪": "骊",
    "驫": "骉",
    "骯": "肮",
    "髏": "髅",
    "髒": "脏",
    "體": "体",
    "髕": "髌",
    "髖": "髋",
    "髮": "发",
    "鬆": "松",
    "鬍": "胡",
    "鬚": "须",
    "鬢": "鬓",
    "鬥": "斗",
    "鬧": "闹",
    "鬨": "哄",
    "鬩": "阋",
    "鬮": "阄",
    "鬱": "郁",
    "鬹": "鬶",
    "魎": "魉",
    "魘": "魇",
    "魚": "鱼",
    "魛": "鱽",
    "魢": "鱾",
    "魨": "鲀",
    "魯": "鲁",
    "魴": "鲂",
    "魷": "鱿",
    "魺": "鲄",
    "鮁": "鲅",
    "鮃": "鲆",
    "鮊": "鲌",
    "鮋": "鲉",
    "鮍": "鲏",
    "鮎": "鲇",
    "鮐": "鲐",
    "鮑": "鲍",
    "鮒": "鲋",
    "鮓": "鲊",
    "鮚": "鲒",
    "鮜": "鲘",
    "鮝": "鲞",
    "鮞": "鲕",
    "鮣": "䲟",
    "鮦": "鲖",
    "鮪": "鲔",
    "鮫": "鲛",
    "鮭": "鲑",
    "鮮": "鲜",
    "鮳": "鲓",
    "鮶": "鲪",
    "鮺": "鲝",
    "鯀": "鲧",
    "鯁": "鲠",
    "鯇": "鲩",
    "鯉": "鲤",
    "鯊": "鲨",
    "鯒": "鲬",
    "鯔": "鲻",
    "鯕": "鲯",
    "鯖": "鲭",
    "鯗": "鲞",
    "鯛": "鲷",
    "鯝": "鲴",
    "鯡": "鲱",
    "鯢": "鲵",
    "鯤": "鲲",
    "鯧": "鲳",
    "鯨": "鲸",
    "鯪": "鲮",
    "鯫": "鲰",
    "鯰": "鲶",
    "鯴": "鲺",
    "鯷": "鳀",
    "鯽": "鲫",
    "鯿": "鳊",
    "鰁": "鳈",
    "鰂": "鲗",
    "鰃": "鳂",
    "鰆": "䲠",
    "鰈": "鲽",
    "鰉": "鳇",
    "鰌": "䲡",
    "鰍": "鳅",
    "鰏": "鲾",
    "鰐": "鳄",
    "鰒": "鳆",
    "鰓": "鳃",
    "鰛": "鳁",
    "鰜": "鳒",
    "鰟": "鳑",
    "鰠": "鳋",
    "鰣": "鲥",
    "鰥": "鳏",
    "鰧": "䲢",
    "鰨": "鳎",
    "鰩": "鳐",
    "鰭": "鳍",
    "鰮": "鳁",
    "鰱": "鲢",
    "鰲": "鳌",
    "鰳": "鳓",
    "鰵": "鳘",
    "鰷": "鲦",
    "鰹": "鲣",
    "鰺": "鲹",
    "鰻": "鳗",
    "鰼": "鳛",
    "鰾": "鳔",
    "鱂": "鳉",
    "鱅": "鳙",
    "鱈": "鳕",
    "鱉": "鳖",
    "鱒": "鳟",
    "鱔": "鳝",
    "鱖": "鳜",
    "鱗": "鳞",
    "鱘": "鲟",
    "鱝": "鲼",
    "鱟": "鲎",
    "鱠": "鲙",
    "鱣": "鳣",
    "鱤": "鳡",
    "鱧": "鳢",
    "鱨": "鲿",
    "鱭": "鲚",
    "鱯": "鳠",
    "鱷": "鳄",
    "鱸": "鲈",
    "鱺": "鲡",
    "鳥": "鸟",
    "鳧": "凫",
    "鳩": "鸠",
    "鳬": "凫",
    "鳲": "鸤",
    "鳳": "凤",
    "鳴": "鸣",
    "鳶": "鸢",
    "鳾": "䴓",
    "鴆": "鸩",
    "鴇": "鸨",
    "鴉": "鸦",
    "鴒": "鸰",
    "鴕": "鸵",
    "鴛": "鸳",
    "鴝": "鸲",
    "鴞": "鸮",
    "鴟": "鸱",
    "鴣": "鸪",
    "鴦": "鸯",
    "鴨": "鸭",
    "鴯": "鸸",
    "鴰": "鸹",
    "鴴": "鸻",
    "鴷": "䴕",
    "鴻": "鸿",
    "鴿": "鸽",
    "鵁": "䴔",
    "鵂": "鸺",
    "鵃": "鸼",
    "鵐": "鹀",
    "鵑": "鹃",
    "鵒": "鹆",
    "鵓": "鹁",
    "鵜": "鹈",
    "鵝": "鹅",
    "鵠": "鹄",
    "鵡": "鹉",
    "鵪": "鹌",
    "鵬": "鹏",
    "鵮": "鹐",
    "鵯": "鹎",
    "鵲": "鹊",
    "鵷": "鹓",
    "鵾": "鹍",
    "鶄": "䴖",
    "鶇": "鸫",
    "鶉": "鹑",
    "鶊": "鹒",
    "鶓": "鹋",
    "鶖": "鹙",
    "鶘": "鹕",
    "鶚": "鹗",
    "鶡": "鹖",
    "鶥": "鹛",
    "鶩": "鹜",
    "鶪": "䴗",
    "鶬": "鸧",
    "鶯": "莺",
    "鶲": "鹟",
    "鶴": "鹤",
    "鶹": "鹠",
    "鶺": "鹡",
    "鶻": "鹘",
    "鶼": "鹣",
    "鶿": "鹚",
    "鷀": "鹚",
    "鷁": "鹢",
    "鷂": "鹞",
    "鷄": "鸡",
    "鷉": "䴘",
    "鷊": "鹝",
    "鷓": "鹧",
    "鷖": "鹥",
    "鷗": "鸥",
    "鷙": "鸷",
    "鷚": "鹨",
    "鷥": "鸶",
    "鷦": "鹪",
    "鷫": "鹔",
    "鷯": "鹩",
    "鷲": "鹫",
    "鷳": "鹇",
    "鷴": "鹇",
    "鷸": "鹬",
    "鷹": "鹰",
    "鷺": "鹭",
    "鷽": "鸴",
    "鸂": "㶉",
    "鸇": "鹯",
    "鸊": "䴙",
    "鸌": "鹱",
    "鸏": "鹲",
    "鸕": "鸬",
    "鸘": "鹴",
    "鸚": "鹦",
    "鸛": "鹳",
    "鸝": "鹂",
    "鸞": "鸾",
    "鹵": "卤",
    "鹹": "咸",
    "鹺": "鹾",
    "鹼": "碱",
    "鹽": "盐",
    "麗": "丽",
    "麥": "麦",
    "麩": "麸",
    "麫": "面",
    "麯": "曲",
    "黃": "黄",
    "黌": "黉",
    "點": "点",
    "黨": "党",
    "黲": "黪",
    "黴": "霉",
    "黶": "黡",
    "黷": "黩",
    "黽": "黾",
    "黿": "鼋",
    "鼂": "鼌",
    "鼉": "鼍",
    "鼕": "冬",
    "鼴": "鼹",
    "齊": "齐",
    "齋": "斋",
    "齎": "赍",
    "齏": "齑",
    "齒": "齿",
    "齔": "龀",
    "齕": "龁",
    "齗": "龂",
    "齙": "龅",
    "齜": "龇",
    "齟": "龃",
    "齠": "龆",
    "齡": "龄",
    "齣": "出",
    "齦": "龈",
    "齪": "龊",
    "齬": "龉",
    "齲": "龋",
    "齶": "腭",
    "齷": "龌",
    "龍": "龙",
    "龎": "厐",
    "龐": "庞",
    "龑": "䶮",
    "龔": "龚",
    "龕": "龛",
    "龜": "龟",
    "鿁": "䜤",
    "鿓": "鿒",
    "𠗣": "㓆",
    "𡞵": "㛟",
    "𡠹": "㛿",
    "𡢃": "㛠",
    "𡻕": "岁",
    "𡾱": "㟜",
    "𣈶": "暅",
    "𣙎": "㭣",
    "𣯶": "毶",
    "𣾷": "㳢",
    "𤪺": "㻘",
    "𤫩": "㻏",
    "𥢢": "䅪",
    "𦪙": "䑽",
    "𧜗": "䘞",
    "𧜵": "䙊",
    "𧝞": "䘛",
    "𧩙": "䜥",
    "𧵳": "䞌",
    "𧶧": "䞎",
    "𨊰": "䢀",
    "𨊸": "䢁",
    "𨋢": "䢂",
    "𨦫": "䦀",
    "𨧜": "䦁",
    "𨯅": "䥿",
    "𩞯": "䭪",
    "𩣑": "䯃",
    "𩶘": "䲞",
}


def _simplified(query: str) -> str:
    return "".join(_TRADITIONAL_TO_SIMPLIFIED.get(char, char) for char in query)


@lru_cache(maxsize=1)
def _simplified_to_traditional() -> dict[str, str]:
    """Reverse map for simplified input; only defined where unambiguous."""
    reverse: dict[str, str] = {}
    ambiguous: set[str] = set()
    for trad, simp in _TRADITIONAL_TO_SIMPLIFIED.items():
        if simp in ambiguous:
            continue
        if simp not in reverse:
            reverse[simp] = trad
        else:
            # ambiguous reverse (e.g. 发→發/髮): don't guess
            reverse.pop(simp, None)
            ambiguous.add(simp)
    return reverse


def _traditional(query: str) -> str:
    reverse = _simplified_to_traditional()
    return "".join(reverse.get(char, char) for char in query)


def _query_variants(query: str) -> list[str]:
    """Query plus a simplified or traditional Chinese variant."""
    variants = [query]
    simplified = _simplified(query)
    if simplified != query:
        variants.append(simplified)
        return variants
    traditional = _traditional(query)
    if traditional != query:
        variants.append(traditional)
    return variants


# ZIM Language metadata uses ISO 639-2/T codes (e.g. "eng", "zho"); accept the
# common two-letter ISO 639-1 alias when filtering cross-archive searches.
_LANGUAGE_ALIASES = {
    "en": "eng",
    "zh": "zho",
    "de": "deu",
    "fr": "fra",
    "es": "spa",
    "it": "ita",
    "pt": "por",
    "ru": "rus",
    "ja": "jpn",
    "ko": "kor",
    "ar": "ara",
    "hi": "hin",
    "nl": "nld",
    "sv": "swe",
    "pl": "pol",
    "tr": "tur",
}
_SEE_ALSO_LABELS = {
    "see also",
    "参见",
    "參見",
    "参阅",
    "參閱",
    "另见",
    "另見",
    "延伸阅读",
    "延伸閱讀",
}


def _find_links(
    archive: Archive, soup: BeautifulSoup, article_path: str, archive_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract internal article links.

    Returns (see_also, links, notes). notes come from hatnotes ("X
    redirects here", "主条目：…") and surface disambiguation targets.
    see_also is the curated set; links is the broader lead/body set. Both are
    returned so link-following traversal keeps its full fan-out, and no path
    appears twice. Each item is {article_path, uri, title} with article_path
    verified to exist and directly usable with read_article.
    """
    see_also: list[dict[str, Any]] = []
    body: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    seen: set[str] = set()
    root, mediawiki = _content_root(soup)

    def add(anchor, target: list[dict[str, Any]], cap: int) -> None:
        if len(target) >= cap:
            return
        if mediawiki and _is_mediawiki_noise(anchor):
            return
        if anchor.find_parent(["nav", "header", "footer", "aside"]):
            return
        if anchor.find_parent(class_=[name[1:] for name in _STACKEXCHANGE_NOISE]):
            return
        try:
            path = _resolve_image_path(article_path, str(anchor["href"]))
        except (KeyError, ValueError):
            return
        if path in seen:
            return
        try:
            if not archive.has_entry_by_path(path):
                return
        except RuntimeError:
            return
        seen.add(path)
        target.append(
            {
                "article_path": path,
                "uri": _article_uri(archive_id, path),
                "title": anchor.get_text(" ", strip=True),
            }
        )

    for hatnote in root.select(_HATNOTE_SELECTOR):
        if len(notes) >= 5:
            break
        note_links: list[dict[str, Any]] = []
        for anchor in hatnote.find_all("a", href=True):
            try:
                path = _resolve_image_path(article_path, str(anchor["href"]))
            except (KeyError, ValueError):
                continue
            try:
                if not archive.has_entry_by_path(path):
                    continue
            except RuntimeError:
                continue
            note_links.append(
                {
                    "article_path": path,
                    "uri": _article_uri(archive_id, path),
                    "title": anchor.get_text(" ", strip=True),
                }
            )
        label = _compact_text(hatnote, 300)
        if note_links:
            notes.append({"label": label, "links": note_links})

    for h2 in root.find_all("h2"):
        label = (h2.get("id") or "").replace("_", " ").strip().lower()
        if not label:
            label = h2.get_text(" ", strip=True).lower()
        if label in _SEE_ALSO_LABELS:
            for tag in h2.find_all_next():
                if tag.name == "h2":
                    break
                if tag.name == "a" and tag.get("href"):
                    add(tag, see_also, MAX_SEE_ALSO_LINKS)
            break

    for anchor in root.find_all("a", href=True):
        if len(body) >= MAX_BODY_LINKS:
            break
        add(anchor, body, MAX_BODY_LINKS)

    return see_also, body, notes


def _find_images_in_article(
    archive: Archive, article_path: str
) -> list[dict[str, Any]]:
    """Find internal image references in article HTML."""
    entry = _entry(archive, article_path)
    item = entry.get_item()
    if not (item.mimetype.startswith("text/") or "html" in item.mimetype):
        return []
    html = bytes(item.content).decode("utf-8", errors="replace")
    return _find_images(BeautifulSoup(html, "html.parser"), entry.path)


def _find_images(soup: BeautifulSoup, article_path: str) -> list[dict[str, Any]]:
    """Article images with duplicate paths and tiny icons removed.

    ZIM convention puts the main article image first; repeated references
    to the same image (flags, padlock icons in citations) add no recall
    value, so only the first occurrence is kept.
    """
    images: list[dict[str, Any]] = []
    seen: set[str] = set()
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue
        try:
            image_path = _resolve_image_path(article_path, str(src))
        except ValueError:
            continue
        if image_path in seen:
            continue
        width, height = img.get("width"), img.get("height")
        width_value = int(width) if str(width).isdigit() else None
        height_value = int(height) if str(height).isdigit() else None
        if (
            width_value is not None
            and height_value is not None
            and max(width_value, height_value) < 50
        ):
            # Padlock/flag/wiki-logo chrome, not article imagery.
            continue
        seen.add(image_path)
        figure = img.find_parent("figure")
        caption = figure.find("figcaption") if figure else None
        images.append(
            {
                "index": len(images),
                "src": str(src),
                "image_path": image_path,
                "filename": posixpath.basename(image_path),
                "alt": str(img.get("alt", "")),
                "caption": caption.get_text(" ", strip=True) if caption else "",
                "width": width_value,
                "height": height_value,
                "primary": not images,
            }
        )
    return images


def _resolve_image_path(article_path: str, src: str) -> str:
    """Resolve an internal HTML image URL to a ZIM entry path."""
    parsed = urlsplit(src)
    if parsed.scheme or parsed.netloc or not parsed.path:
        raise ValueError(f"Not an internal image URL: {src}")
    path = unquote(parsed.path)
    if path.startswith("/"):
        return posixpath.normpath(path).lstrip("/")
    return posixpath.normpath(
        posixpath.join("/", posixpath.dirname(article_path), path)
    ).lstrip("/")


def _cache_image(raw: bytes, mimetype: str) -> Path:
    IMAGE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = IMAGE_TEMP_DIR / ".lock"
    with lock_path.open("a+b") as lock_file:
        # Every stdio client has its own process, but all clients share this cache.
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        suffix = mimetypes.guess_extension(mimetype) or ".img"
        file_path = IMAGE_TEMP_DIR / f"{hashlib.sha256(raw).hexdigest()[:16]}{suffix}"
        if file_path.exists():
            file_path.touch()
        else:
            file_path.write_bytes(raw)
        files = sorted(
            (
                path
                for path in IMAGE_TEMP_DIR.iterdir()
                if path.is_file() and path != lock_path
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for stale in files[MAX_TEMP_IMAGES:]:
            stale.unlink(missing_ok=True)
    return file_path


def _extract_image(archive: Archive, article_path: str, image_index: int) -> list[Any]:
    """Return image metadata followed by native MCP image content."""
    images = _find_images_in_article(archive, article_path)
    if not images:
        raise ValueError("Article contains no images")
    if image_index < 0 or image_index >= len(images):
        raise ValueError(
            f"Image index {image_index} out of range (0-{len(images) - 1})"
        )

    img_info = images[image_index]
    img_entry = _entry(archive, img_info["image_path"])
    img_item = img_entry.get_item()
    if not img_item.mimetype.startswith("image/"):
        raise ValueError(
            f"Entry is not an image: {img_item.mimetype} (path: {img_info['image_path']})"
        )
    if img_item.size > MAX_IMAGE_BYTES:
        raise ValueError(f"Image too large: {img_item.size} bytes")

    raw = bytes(img_item.content)
    file_path = _cache_image(raw, img_item.mimetype)
    metadata = {
        "status": "ok",
        "file_path": str(file_path),
        "mimetype": img_item.mimetype,
        "size_bytes": img_item.size,
        "image_index": image_index,
        "image_path": img_info["image_path"],
        "total_images": len(images),
        "alt": img_info["alt"],
        "filename": img_info["filename"],
        "article_path": article_path,
    }
    return [
        metadata,
        ImageContent(
            type="image",
            data=base64.b64encode(raw).decode("ascii"),
            mimeType=img_item.mimetype,
        ),
    ]


def extract_image(
    archive_id: str,
    article_path: str,
    image_index: int = 0,
    image_path: str | None = None,
) -> list[Any]:
    """Return native MCP image content plus file_path.

    If native images are unsupported, call the client's read tool on file_path.
    """
    selected = _select_paths(archive_id)
    if len(selected) != 1:
        raise ValueError("archive_id is required")
    _, path = selected[0]
    archive = _archive(path)
    if image_path is not None:
        images = _find_images_in_article(archive, article_path)
        requested_path = posixpath.normpath(unquote(image_path).lstrip("/"))
        image_index = next(
            (
                index
                for index, image in enumerate(images)
                if image["image_path"] == requested_path
            ),
            -1,
        )
        if image_index < 0:
            raise ValueError(f"Image path not found in article: {image_path}")
    return _extract_image(archive, article_path, image_index)


_TOOL_DEFINITIONS = [
    Tool(
        name="list_archives",
        title="List ZIM archives",
        description="List ZIM archives and metadata, including flavour, for archive selection.",
        inputSchema={"type": "object", "properties": {}},
        outputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "directory": {"type": "string"},
                "archives": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "archive_id": {"type": "string"},
                            "size_bytes": {"type": "integer"},
                            "article_count": {"type": "integer"},
                            "media_count": {"type": "integer"},
                            "has_fulltext_index": {"type": "boolean"},
                            "has_title_index": {"type": "boolean"},
                            "flavour": {"type": "string"},
                            "has_main_entry": {"type": "boolean"},
                            "main_entry_path": {"type": "string"},
                            "title": {"type": "string"},
                            "language": {"type": "string"},
                            "date": {"type": "string"},
                            "description": {"type": "string"},
                            "name": {"type": "string"},
                            "tags": {"type": "string"},
                            "error": {"type": "string"},
                        },
                    },
                },
            },
            "required": ["status", "directory", "archives"],
        },
        annotations=READ_ONLY,
    ),
    Tool(
        name="search",
        title="Search a ZIM archive",
        description="Search a ZIM archive. Required: query and archive_id from list_archives; pass archive_id exactly, or '*' to aggregate archives. Use limit/offset for result pages.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The article/topic to find.",
                },
                "archive_id": {
                    "type": "string",
                    "description": "Required exact archive_id from list_archives, or '*' for cross-archive search.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Number of search results, not article characters.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 1000,
                    "description": "Use the previous search result's next_offset.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "fulltext", "title"],
                    "default": "auto",
                    "description": "auto picks fulltext when available, otherwise title-suggestion.",
                },
                "language": {
                    "type": "string",
                    "description": "Filter cross-archive search by language code (e.g. 'zh', 'en'); only effective with archive_id='*'.",
                },
                "flavour": {
                    "type": "string",
                    "description": "Filter cross-archive search by flavour (e.g. 'maxi', 'nopic'); only effective with archive_id='*'.",
                },
            },
            "required": ["query", "archive_id"],
        },
        outputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "query": {"type": "string"},
                "offset": {"type": "integer"},
                "next_offset": {"type": ["integer", "null"]},
                "estimated_matches": {"type": ["integer", "null"]},
                "mode": {"type": "string", "enum": ["auto", "fulltext", "title"]},
                "filters": {
                    "type": "object",
                    "properties": {
                        "language": {"type": "string"},
                        "flavour": {"type": "string"},
                    },
                },
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "archive_id": {"type": "string"},
                            "article_path": {"type": "string"},
                            "title": {"type": "string"},
                            "mimetype": {"type": "string"},
                            "snippet": {"type": "string"},
                            "uri": {"type": "string"},
                            "search_mode": {
                                "type": "string",
                                "enum": ["fulltext", "title"],
                            },
                            "match_type": {
                                "type": "string",
                                "enum": ["exact_title", "fulltext", "title"],
                            },
                            "matched_query": {"type": "string"},
                        },
                    },
                },
                "errors": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["status", "query", "offset", "results", "errors"],
        },
        annotations=READ_ONLY,
    ),
    Tool(
        name="inspect_article",
        title="Inspect a ZIM article",
        description="Inspect an article before reading it: clean lead, heading outline, MediaWiki infobox facts, redirects, and reference count.",
        inputSchema={
            "type": "object",
            "properties": {
                "archive_id": {"type": "string"},
                "article_path": {"type": "string"},
            },
            "required": ["archive_id", "article_path"],
        },
        outputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "archive_id": {"type": "string"},
                "requested_article_path": {"type": "string"},
                "article_path": {"type": "string"},
                "redirected": {"type": "boolean"},
                "title": {"type": "string"},
                "mimetype": {"type": "string"},
                "profile": {"type": "string", "enum": ["mediawiki", "html", "text"]},
                "lead": {"type": "string"},
                "outline": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "level": {"type": "integer"},
                            "title": {"type": "string"},
                            "anchor": {"type": "string"},
                            "uri": {"type": "string"},
                        },
                        "required": ["level", "title", "anchor", "uri"],
                    },
                },
                "total_sections": {"type": "integer"},
                "outline_truncated": {"type": "boolean"},
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["name", "value"],
                    },
                },
                "total_references": {"type": "integer"},
                "total_chars": {"type": "integer"},
                "uri": {"type": "string"},
                "canonical_url": {"type": ["string", "null"]},
                "language": {"type": ["string", "null"]},
                "archive_date": {"type": ["string", "null"]},
            },
            "required": [
                "status",
                "archive_id",
                "article_path",
                "title",
                "profile",
                "lead",
                "outline",
                "facts",
                "total_references",
                "uri",
            ],
        },
        annotations=READ_ONLY,
    ),
    Tool(
        name="read_article",
        title="Read a ZIM article",
        description="Read one article. Required: archive_id and article_path from a search result. Use max_chars for text size and offset for continuation; limit is accepted only as a compatibility alias for max_chars.",
        inputSchema={
            "type": "object",
            "properties": {
                "archive_id": {
                    "type": "string",
                    "description": "Required exact archive_id from the search result.",
                },
                "article_path": {
                    "type": "string",
                    "description": "Required article_path from the search result.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional term to center the first text window.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 50000,
                    "description": "Maximum characters returned in this window; use this instead of limit.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 50000,
                    "description": "Compatibility alias for max_chars; prefer max_chars.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Continue from a previous next_offset; query only centers the first window.",
                },
                "include_images": {"type": "boolean"},
                "include_links": {"type": "boolean"},
                "image_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Continue image metadata from a previous next_image_offset; page size is capped at 30.",
                },
                "section": {
                    "type": "string",
                    "description": "Read one heading subtree by the title or anchor returned by inspect_article.",
                },
            },
            "required": ["archive_id", "article_path"],
        },
        outputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "archive_id": {"type": "string"},
                "article_path": {"type": "string"},
                "title": {"type": "string"},
                "mimetype": {"type": "string"},
                "text": {"type": "string"},
                "offset": {"type": "integer"},
                "next_offset": {"type": ["integer", "null"]},
                "truncated": {"type": "boolean"},
                "total_chars": {"type": "integer"},
                "uri": {"type": "string"},
                "requested_article_path": {"type": "string"},
                "redirected": {"type": "boolean"},
                "section": {"type": ["object", "null"]},
                "canonical_url": {"type": ["string", "null"]},
                "language": {"type": ["string", "null"]},
                "archive_date": {"type": ["string", "null"]},
                "total_images": {"type": "integer"},
                "image_offset": {"type": "integer"},
                "next_image_offset": {"type": ["integer", "null"]},
                "images_truncated": {"type": "boolean"},
                "images": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "src": {"type": "string"},
                            "image_path": {"type": "string"},
                            "filename": {"type": "string"},
                            "alt": {"type": "string"},
                            "caption": {"type": "string"},
                            "width": {"type": ["integer", "null"]},
                            "height": {"type": ["integer", "null"]},
                            "primary": {"type": "boolean"},
                        },
                    },
                },
                "see_also": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "article_path": {"type": "string"},
                            "uri": {"type": "string"},
                            "title": {"type": "string"},
                        },
                    },
                },
                "links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "article_path": {"type": "string"},
                            "uri": {"type": "string"},
                            "title": {"type": "string"},
                        },
                    },
                },
                "notes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "links": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "article_path": {"type": "string"},
                                        "uri": {"type": "string"},
                                        "title": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                },
                "total_links": {"type": "integer"},
            },
            "required": [
                "status",
                "archive_id",
                "article_path",
                "title",
                "text",
                "offset",
                "truncated",
                "total_chars",
                "uri",
                "requested_article_path",
                "redirected",
                "section",
                "canonical_url",
                "language",
                "archive_date",
            ],
        },
        annotations=READ_ONLY,
    ),
    Tool(
        name="list_references",
        title="List article references",
        description="List paginated MediaWiki citations and notes with archived external URLs; optionally select a visible marker such as '1', 'a', or 'note 1'.",
        inputSchema={
            "type": "object",
            "properties": {
                "archive_id": {"type": "string"},
                "article_path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "citation_label": {"type": "string"},
            },
            "required": ["archive_id", "article_path"],
        },
        outputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "archive_id": {"type": "string"},
                "article_path": {"type": "string"},
                "offset": {"type": "integer"},
                "next_offset": {"type": ["integer", "null"]},
                "total_references": {"type": "integer"},
                "matched_references": {"type": "integer"},
                "citation_label": {"type": ["string", "null"]},
                "references": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "citation_label": {"type": ["string", "null"]},
                            "citation_id": {"type": "string"},
                            "text": {"type": "string"},
                            "links": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "url": {"type": "string"},
                                    },
                                    "required": ["title", "url"],
                                },
                            },
                        },
                        "required": [
                            "citation_label",
                            "citation_id",
                            "text",
                            "links",
                        ],
                    },
                },
                "uri": {"type": "string"},
            },
            "required": [
                "status",
                "archive_id",
                "article_path",
                "offset",
                "total_references",
                "matched_references",
                "citation_label",
                "references",
                "uri",
            ],
        },
        annotations=READ_ONLY,
    ),
    Tool(
        name="extract_image",
        title="Extract a ZIM image",
        description="Return a ZIM image as native MCP image content and a temporary file fallback; prefer image_path from read_article over the index-0 fallback.",
        inputSchema={
            "type": "object",
            "properties": {
                "archive_id": {"type": "string"},
                "article_path": {"type": "string"},
                "image_index": {"type": "integer", "minimum": 0},
                "image_path": {
                    "type": "string",
                    "description": "Choose an exact image_path returned by read_article; overrides image_index.",
                },
            },
            "required": ["archive_id", "article_path"],
        },
        annotations=WRITES_CACHE,
    ),
]


def _dispatch_tool(name: str, arguments: dict[str, Any]) -> Any:
    _LOGGER.info("tool=%s", name)
    if name == "list_archives":
        return list_archives()
    if name == "search":
        return search(**arguments)
    if name == "inspect_article":
        return inspect_article(**arguments)
    if name == "read_article":
        return read_article(**arguments)
    if name == "list_references":
        return list_references(**arguments)
    if name == "extract_image":
        return extract_image(**arguments)
    raise ValueError(f"Unknown tool: {name}")


def _json_content(value: Any) -> TextContent:
    return TextContent(type="text", text=json.dumps(value, ensure_ascii=False))


def _mcp_result(value: Any) -> CallToolResult:
    if isinstance(value, dict):
        return CallToolResult(content=[_json_content(value)], structuredContent=value)
    content: list[Any] = []
    for item in value:
        content.append(_json_content(item) if isinstance(item, dict) else item)
    return CallToolResult(content=content)


async def _list_tools_v2(_ctx: Any, _params: Any) -> ListToolsResult:
    return ListToolsResult(tools=_TOOL_DEFINITIONS)


def _archive_resources() -> list[Resource]:
    resources: list[Resource] = []
    for archive_id, path in _archive_paths().items():
        try:
            archive = _archive(path)
            if not archive.has_main_entry:
                continue
            article_path = _main_entry_path(archive)
            if article_path is None:
                continue
            main_entry = archive.main_entry
            resources.append(
                Resource(
                    name=f"{archive_id}-main",
                    title=str(main_entry.title or archive_id),
                    uri=_article_uri(archive_id, article_path),
                    description=f"Main entry for {archive_id}",
                    mimeType="text/plain",
                    meta={
                        "archive_id": archive_id,
                        "article_path": article_path,
                        "resource_type": "main_entry",
                    },
                )
            )
        except (OSError, RuntimeError, ValueError):
            continue
    return resources


def _read_article_resource(uri: str) -> ReadResourceResult:
    archive_id, article_path = _parse_article_uri(uri)
    section = _parse_article_section(uri)
    selected = _select_paths(archive_id)
    if len(selected) != 1:
        raise ValueError("archive_id is required")
    _, path = selected[0]
    archive = _archive(path)
    entry = _entry(archive, article_path)
    item = entry.get_item()
    if item.size > MAX_ARTICLE_BYTES:
        raise ValueError(f"Article is too large to read safely: {item.size} bytes")
    if not (item.mimetype.startswith("text/") or "html" in item.mimetype):
        raise ValueError(f"Article is not text: {item.mimetype}")

    content = bytes(item.content)
    selected_section: dict[str, Any] | None = None
    if section:
        if "html" not in item.mimetype.lower():
            raise ValueError("Sections are only available for HTML articles")
        soup = BeautifulSoup(content.decode("utf-8", errors="replace"), "html.parser")
        full_text, selected_section = _article_text(soup, section)
    else:
        full_text = _plain_text(content, item.mimetype)
    truncated = len(full_text) > MAX_RESOURCE_CHARS
    text = full_text if not truncated else full_text[:MAX_RESOURCE_CHARS].rstrip()
    meta = {
        "archive_id": archive_id,
        "article_path": entry.path,
        "mimetype": item.mimetype,
        "status": "ok",
        "total_chars": len(full_text),
        "truncated": truncated,
        "section": selected_section,
    }
    if truncated:
        meta["next_offset_hint"] = MAX_RESOURCE_CHARS
    return ReadResourceResult(
        contents=[
            TextResourceContents(
                uri=uri,
                mimeType="text/plain",
                text=text,
                meta={
                    "truncated": truncated,
                    "next_offset_hint": meta.get("next_offset_hint"),
                    "section": selected_section,
                },
            )
        ],
        meta=meta,
    )


async def _list_resources_v2(_ctx: Any, _params: Any) -> ListResourcesResult:
    return ListResourcesResult(resources=_archive_resources())


async def _list_resource_templates_v2(
    _ctx: Any, _params: Any
) -> ListResourceTemplatesResult:
    return ListResourceTemplatesResult(
        resourceTemplates=[
            ResourceTemplate(
                name="kiwix-article",
                title="Kiwix article",
                uriTemplate="kiwix://{archive_id}/{+article_path}",
                description="Read article content from a ZIM archive.",
                mimeType="text/plain",
            )
        ]
    )


async def _read_resource_v2(_ctx: Any, params: Any) -> ReadResourceResult:
    return _read_article_resource(params.uri)


async def _call_tool_v2(_ctx: Any, params: Any) -> CallToolResult:
    try:
        return _mcp_result(_dispatch_tool(params.name, params.arguments or {}))
    except Exception as exc:
        _LOGGER.warning("tool=%s failed: %s", params.name, exc)
        return CallToolResult(
            content=[TextContent(type="text", text=str(exc))], isError=True
        )


mcp = Server(
    "kiwix",
    version="0.1.0",
    instructions=MCP_INSTRUCTIONS,
    on_list_tools=_list_tools_v2,
    on_call_tool=_call_tool_v2,
    on_list_resources=_list_resources_v2,
    on_list_resource_templates=_list_resource_templates_v2,
    on_read_resource=_read_resource_v2,
)


if __name__ == "__main__":
    # libzim/Xapian writes diagnostics directly to fd 1. Keep JSON-RPC on a
    # duplicate of the original stdout and send native diagnostics to stderr.
    protocol_stdout = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = protocol_stdout
    _configure_logging()

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            _reset_idle_timer()
            try:
                await mcp.run(
                    _ActivityReadStream(read_stream),
                    write_stream,
                    mcp.create_initialization_options(),
                )
            finally:
                _cancel_idle_timer()

    asyncio.run(_run())
