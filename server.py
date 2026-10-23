from __future__ import annotations

# kiwix-mcp - local MCP server for Kiwix ZIM archives
# Copyright (C) 2026 Yann Pan
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.

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

DEFAULT_ARCHIVE_DIR = Path.home() / ".local" / "share" / "kiwix-mcp" / "archives"
# Earlier releases defaulted to this path; still honoured when it exists so
# pre-0.1.x installations keep working without setting KIWIX_ARCHIVE_DIR.
LEGACY_ARCHIVE_DIR = Path.home() / ".chroma_db" / "kiwix" / "archives"
_IMAGE_DIR_OVERRIDE = os.environ.get("KIWIX_MCP_IMAGE_DIR", "").strip()
# A stable location, because extract_image hands clients a file_path they read
# after the call returns: TMPDIR varies per client environment and /tmp is
# cleared on reboot, so a temp-based base split the cache and lost files.
IMAGE_TEMP_DIR = (
    Path(_IMAGE_DIR_OVERRIDE).expanduser()
    if _IMAGE_DIR_OVERRIDE
    else Path.home() / ".cache" / "kiwix-mcp" / "images"
)
MAX_ARTICLE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_CHARS = 12_000
# Some ZIM entries are script-driven app shells (PhET simulations, for example)
# whose content never reaches the HTML, so both readers explain the blank result.
NO_TEXT_NOTE = (
    "No extractable text in this entry. It is probably an interactive or "
    "script-driven page rather than a readable document; try another entry "
    "or archive."
)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TEMP_IMAGES = 16
MAX_IMAGE_REFERENCES = 30
MAX_TITLE_SUGGESTIONS = 32
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
    "across archives. "
    "Copy article_path verbatim from search results: it is a ZIM path, not the "
    "article title. The server tolerates one URL-decoding pass only when a "
    "verbatim lookup misses, but copying the returned value is always preferred. "
    "Argument names are literal: search uses query, and article tools use "
    "archive_id plus article_path; article_id, path, and search_criteria are not "
    "aliases. "
    "After an argument error, check the tool's input schema and correct the call "
    "before retrying; never repeat an unchanged failing call. "
    "Choose the archive by language and collection: prefer full archives for "
    "coverage and maxi archives when images matter; maxi adds media, not article "
    "coverage, and selected collections such as wp1-0.8 are subsets. Use title, "
    "date, and description to break ties. "
    "A no-hit search is not proof the topic is absent: read its guidance and errors "
    "before deciding, and do not silently switch archives. "
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
    override = os.environ.get("KIWIX_ARCHIVE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    # ponytail: legacy fallback is scaffolding; drop it once old installs are gone
    return LEGACY_ARCHIVE_DIR if LEGACY_ARCHIVE_DIR.is_dir() else DEFAULT_ARCHIVE_DIR


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
    # An agent that guesses an archive_id needs the real ones to recover, so the
    # error carries them instead of dead-ending.
    if not archives:
        raise ValueError(
            f"Unknown archive: {archive_id!r}. No archives found in "
            f"{_archive_dir()} - add .zim files there or set KIWIX_ARCHIVE_DIR."
        )
    names = sorted(archives)
    shown = ", ".join(names[:20])
    if len(names) > 20:
        shown += f", ... ({len(names) - 20} more)"
    raise ValueError(
        f"Unknown archive: {archive_id!r}. Pass one archive_id exactly as "
        f"returned by list_archives. Available ({len(names)}): {shown}"
    )


# Bounded, but comfortably above a realistic archive count: with maxsize below
# the number of archives, list_archives evicts and reopens every one each round.
# ponytail: fixed bound; key on archive count if collections get huge
@lru_cache(maxsize=32)
def _open_archive(path: str, mtime_ns: int) -> Archive:
    del mtime_ns
    return Archive(Path(path))


def _archive(path: Path) -> Archive:
    return _open_archive(str(path), path.stat().st_mtime_ns)


def _entry(archive: Archive, article_path: str):
    entry = _raw_entry(archive, article_path)
    seen: set[str] = set()
    while entry.is_redirect:
        if entry.path in seen or len(seen) >= 8:
            raise ValueError("Redirect loop in ZIM entry")
        seen.add(entry.path)
        entry = entry.get_redirect_entry()
    return entry


def _raw_entry(archive: Archive, article_path: str):
    """Resolve a path without following redirects, with actionable errors.

    Every path lookup goes through here so a bad or missing article_path
    produces a message the caller can act on instead of a bare libzim KeyError.
    """
    if not str(article_path).strip():
        raise ValueError(
            "article_path is required. Pass an article_path from search results, "
            "or an archive's main_entry_path from list_archives."
        )
    raw_path = str(article_path)
    candidates = [raw_path]
    if "%" in raw_path:
        try:
            decoded_path = unquote(raw_path, errors="strict")
        except UnicodeDecodeError:
            decoded_path = raw_path
        if decoded_path != raw_path:
            candidates.append(decoded_path)
    for candidate in candidates:
        try:
            return archive.get_entry_by_path(candidate)
        except KeyError:
            pass
    # Agents commonly pass the human-readable title (spaces) where the ZIM
    # stores a path (underscores), so resolve by title before giving up.
    titles: list[str] = []
    for candidate in candidates:
        title = _normalize_title(candidate)
        if title and title not in titles:
            titles.append(title)
    for title in titles:
        try:
            if archive.has_entry_by_title(title):
                return archive.get_entry_by_title(title)
        except (OSError, RuntimeError):
            pass
    raise ValueError(_article_not_found_message(archive, article_path))


def _normalize_title(value: str) -> str:
    """Normalize title separators without decoding or changing case."""
    return " ".join(str(value).replace("_", " ").split())


def _title_key(value: str, *, path_side: bool = False) -> str:
    """Comparable title key; legacy namespaces are stripped only from paths."""
    title = _normalize_title(value)
    namespace, separator, remainder = title.partition("/")
    if path_side and separator and len(namespace) == 1 and namespace.isalpha():
        title = remainder
    return title.casefold()


def _exact_suggested_path(
    archive: Archive, search_result: Any, query: str
) -> str | None:
    """Find an exact title among a bounded suggestion page without promoting near misses."""
    try:
        suggested = list(search_result.getResults(0, MAX_TITLE_SUGGESTIONS))
    except (OSError, RuntimeError, ValueError):
        return None
    query_key = _title_key(query)
    for article_path in suggested:
        path = str(article_path)
        try:
            entry = archive.get_entry_by_path(path)
            title = str(entry.title or "")
        except (AttributeError, KeyError, OSError, RuntimeError, ValueError):
            entry = None
            title = ""
        if title and _title_key(title) == query_key:
            return path
        if title and getattr(entry, "is_redirect", False):
            # Redirect suggestion entries can retain an alias path while their
            # displayed title differs; permit only exact path equality there.
            if _title_key(path) == query_key:
                return path
        elif not title and _title_key(path, path_side=True) == query_key:
            return path
    return None


def _article_not_found_message(archive: Archive, article_path: str) -> str:
    """Explain the miss and, when possible, name a close match."""
    hint = ""
    try:
        candidates = [
            str(path)
            for path in SuggestionSearcher(archive)
            .suggest(str(article_path))
            .getResults(0, 1)
        ]
    except (OSError, RuntimeError, ValueError):
        candidates = []
    if candidates:
        hint = f" Closest match: {candidates[0]!r}."
    return (
        f"Article not found: {article_path!r}. Pass an article_path copied exactly "
        f"from a search result in this archive; it is not the article title. "
        f"A single URL-encoded path is accepted, but do not build paths by hand.{hint}"
    )


def _too_large(size: int) -> None:
    raise ValueError(
        f"Article is too large to read safely: {size} bytes, above the "
        f"{MAX_ARTICLE_BYTES}-byte source limit. No article-reading tool can open "
        "it because inspect_article, read_article, list_references, and resources "
        "apply the same limit before parsing. Choose a smaller article or another archive; "
        "max_chars reduces returned text only, not the source load."
    )


def _not_text(mimetype: str) -> None:
    raise ValueError(
        f"Article is not text: {mimetype}. Use extract_image for image entries."
    )


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
        # Strip only the single separator slash: real ZIM paths can themselves
        # start with "/" (for example the article about /etc/passwd), and
        # lstrip() would eat that and break the round trip from _article_uri.
        article_path = parsed.path[1:] if parsed.path.startswith("/") else parsed.path
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
    searched_archives = 0
    filtered_archives = 0
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
                filtered_archives += 1
                continue
            searched_archives += 1
            archive_estimates: list[int] = []
            for variant in query_variants:
                if mode == "fulltext" and not archive.has_fulltext_index:
                    raise ValueError(
                        f"Archive does not support full-text search: {name}. "
                        f"Use mode='auto' or mode='title' for this archive."
                    )
                title_result = None
                if (
                    mode == "title"
                    or not archive.has_fulltext_index
                    or getattr(archive, "has_title_index", False)
                ):
                    title_result = SuggestionSearcher(archive).suggest(
                        _normalize_title(variant)
                    )
                if mode == "fulltext":
                    search_result = Searcher(archive).search(Query().set_query(variant))
                    search_mode = "fulltext"
                elif mode == "title" or not archive.has_fulltext_index:
                    search_result = title_result
                    search_mode = "title"
                else:
                    search_result = Searcher(archive).search(Query().set_query(variant))
                    search_mode = "fulltext"
                # SuggestionSearcher is case-insensitive; get_entry_by_title
                # is not. Reuse the bounded suggestion page for exact detection
                # so lowercase and underscore input hit capitalized titles.
                exact_path = (
                    _exact_suggested_path(archive, title_result, variant)
                    if title_result is not None
                    else None
                )
                if exact_path is None:
                    title = _normalize_title(variant)
                    exact_path = (
                        str(archive.get_entry_by_title(title).path)
                        if title and archive.has_entry_by_title(title)
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
            # A single explicitly chosen archive stays strict; in cross-archive
            # mode one archive without a full-text index must not discard the
            # results the other archives could still provide.
            if mode == "fulltext" and not cross_archive:
                raise
            errors.append({"archive_id": name, "error": str(exc)})
    # All exact hits precede lazy archive/variant streams, on every page.
    match_stream = chain(exact_matches, chain.from_iterable(result_streams))
    canonical_matches: list[tuple[Archive, str, str, str, str]] = []
    canonical_seen: set[tuple[str, str]] = set()
    for archive, name, article_path, match_type, matched_query in match_stream:
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
    filters_applied = cross_archive and bool(language or flavour)
    # A zero full-text estimate can coexist with a title hit (for example an
    # underscore query): zero would contradict a non-empty page, so report the
    # estimate as unknown instead of misleading the caller.
    estimated_value = (
        None
        if estimated_matches == 0 and results
        else (estimated_matches if estimated_known else None)
    )
    result: dict[str, Any] = {
        "status": "ok" if results else "no_hits",
        "query": query,
        "offset": offset,
        "next_offset": offset + len(page_matches)
        if has_more and page_matches and offset + len(page_matches) <= 1000
        else None,
        "estimated_matches": estimated_value,
        "results": results,
        "errors": errors,
        "mode": mode,
        "filters": {
            "language": language,
            "flavour": flavour,
            "applied": filters_applied,
        },
        "searched_archives": searched_archives,
        "filtered_archives": filtered_archives,
    }
    if errors:
        result["partial"] = True
        if results:
            result["guidance"] = (
                f"Results are partial because {len(errors)} archive or result "
                "operation(s) failed. Inspect errors and retry the affected "
                "archive_id explicitly if needed; do not treat this page as complete."
            )
        else:
            result["guidance"] = (
                f"No usable results were returned, but {len(errors)} operation(s) "
                "failed. This does not establish that the topic is absent. Inspect "
                "errors and retry the affected archive_id explicitly."
            )
    elif not results:
        if offset:
            result["guidance"] = (
                f"No results were found on page offset {offset}. This does not "
                "establish that the topic is absent; reuse the previous page's "
                "next_offset or try different search terms."
            )
        elif language or flavour:
            if not cross_archive:
                result["guidance"] = (
                    "The language/flavour filters were ignored because a single "
                    "archive was selected. Rerun with archive_id='*' to apply them, "
                    "or use different search terms in this archive."
                )
            else:
                result["guidance"] = (
                    f"No matches passed the cross-archive filters; {filtered_archives} "
                    "archive(s) were filtered out. Rerun the same query without "
                    "filters to widen coverage, or choose another archive explicitly. "
                    "A maxi flavour adds media, not articles: it does not by itself "
                    "mean fuller article coverage."
                )
        else:
            result["guidance"] = (
                "No indexed matches were found in the searched archive(s). Try "
                "different search terms, or explicitly select another archive after "
                "checking coverage in list_archives. A maxi flavour adds media, not "
                "articles; selected collections such as wp1-0.8 are subsets and the "
                "server never switches archives for you."
            )
    return result


def read_article(
    archive_id: str,
    article_path: str,
    query: str = "",
    max_chars: int | None = None,
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
        _too_large(item.size)
    if not (item.mimetype.startswith("text/") or "html" in item.mimetype):
        _not_text(item.mimetype)
    # `max_chars` is canonical, `limit` is a legacy alias. None marks "not
    # supplied" so an explicit max_chars=12000 is no longer indistinguishable
    # from the default and silently overridden by limit.
    if max_chars is None:
        max_chars = DEFAULT_MAX_CHARS if limit is None else limit
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
    if not text.strip():
        result["note"] = NO_TEXT_NOTE
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
    requested_entry = _raw_entry(archive, article_path)
    requested_path = str(requested_entry.path)
    entry = _entry(archive, article_path)
    item = entry.get_item()
    if item.size > MAX_ARTICLE_BYTES:
        _too_large(item.size)
    if not (item.mimetype.startswith("text/") or "html" in item.mimetype):
        _not_text(item.mimetype)

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

    result: dict[str, Any] = {
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
    if total_chars == 0:
        result["note"] = NO_TEXT_NOTE
    return result


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
        _too_large(item.size)
    if "html" not in item.mimetype.lower():
        raise ValueError("References are only available for HTML articles")

    soup = BeautifulSoup(
        bytes(item.content).decode("utf-8", errors="replace"), "html.parser"
    )
    labels = _reference_labels(soup)
    references = [_reference_item(node, labels) for node in _reference_nodes(soup)]
    total_references = len(references)
    # Surface real labels when a filter misses, so the caller can retry instead
    # of guessing what a citation_label looks like in this article.
    available_labels: list[str] = []
    if citation_label is not None:
        requested_label = " ".join(str(citation_label).split()).casefold()
        unfiltered = references
        references = [
            reference
            for reference in references
            if reference["citation_label"] is not None
            and " ".join(reference["citation_label"].split()).casefold()
            == requested_label
        ]
        if not references:
            available_labels = [
                str(reference["citation_label"])
                for reference in unfiltered
                if reference["citation_label"] is not None
            ][:10]
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
        "available_citation_labels": available_labels,
        "citation_label": citation_label,
        "references": page,
        "uri": _article_uri(name, entry.path),
    }


MAX_SEE_ALSO_LINKS = 10
MAX_BODY_LINKS = 15


def _zh_variants_enabled() -> bool:
    mode = os.environ.get("KIWIX_MCP_ZH_VARIANTS", "auto").strip().lower()
    return mode != "off"


def _contains_han(query: str) -> bool:
    return any(
        "\u3400" <= char <= "\u4dbf"
        or "\u4e00" <= char <= "\u9fff"
        or "\uf900" <= char <= "\ufaff"
        or "\U00020000" <= char <= "\U0002fa1f"
        for char in query
    )


def _query_variants(query: str) -> list[str]:
    """Return the query plus a bounded character-level Chinese variant.

    English and other non-Han queries return without importing the optional
    module. KIWIX_MCP_ZH_VARIANTS=off disables expansion for every query.
    """
    if query.isascii() or not _zh_variants_enabled() or not _contains_han(query):
        return [query]
    from zh_variants import query_variants

    return query_variants(query)


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
    "further reading",
    "参见",
    "參見",
    "参看",
    "參看",
    "参阅",
    "參閱",
    "另见",
    "另見",
    "延伸阅读",
    "延伸閱讀",
    "扩展阅读",
    "擴展閱讀",
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
        # MediaWiki language-conversion markup leaks into ids ("扩-{展}-阅读"),
        # so normalise the id and also consider the visible heading text.
        heading_id = (
            (h2.get("id") or "")
            .replace("_", " ")
            .replace("-{", "")
            .replace("}-", "")
            .strip()
            .lower()
        )
        if {heading_id, h2.get_text(" ", strip=True).lower()} & _SEE_ALSO_LABELS:
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
    IMAGE_TEMP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
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
            available = [image["image_path"] for image in images][:5]
            raise ValueError(
                f"Image path not found in article: {image_path}. "
                f"Use an image_path from read_article's images list"
                + (f", for example: {', '.join(available)}" if available else "")
                + "."
            )
    return _extract_image(archive, article_path, image_index)


_TOOL_DEFINITIONS = [
    Tool(
        name="list_archives",
        title="List ZIM archives",
        description="List the ZIM archives available here. This is the discovery entry point: call it first to get the exact archive_id values every other tool needs, plus language, flavour and main_entry_path.",
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
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
        description='Search a ZIM archive. Arguments: {"archive_id":"*","query":"topic","limit":5}. Required: query and archive_id from list_archives; use an exact ID or "*" to aggregate archives. Use limit/offset for result pages. Read guidance and errors before treating a miss as absence. maxi means more media, not more articles; selected wp1-0.8 collections are subsets.',
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
            "additionalProperties": False,
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
                        "applied": {"type": "boolean"},
                    },
                },
                "searched_archives": {"type": "integer"},
                "filtered_archives": {"type": "integer"},
                "partial": {"type": "boolean"},
                "guidance": {"type": "string"},
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
        description="Inspect one specific article by path: clean lead, heading outline, MediaWiki infobox facts, redirects, and reference count. It does not list or browse archive contents; use search to find articles and list_archives to see archives.",
        inputSchema={
            "type": "object",
            "properties": {
                "archive_id": {
                    "type": "string",
                    "description": "Exact archive_id returned by list_archives.",
                },
                "article_path": {
                    "type": "string",
                    "description": "Copy article_path verbatim from search results; it is not a title.",
                },
            },
            "additionalProperties": False,
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
                "note": {
                    "type": "string",
                    "description": "Present only when the entry yielded no extractable text.",
                },
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
                    "description": "Required. Copy article_path exactly from a search result; it is not the article title. One URL-encoded pass is tolerated as a fallback.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional term to center the first text window.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 50000,
                    "description": "Maximum characters returned, from 1000 to 50000 (e.g. 2000); omit for the default. Use this instead of limit.",
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
            "additionalProperties": False,
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
                "note": {
                    "type": "string",
                    "description": "Present only when the entry yielded no extractable text.",
                },
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
                "archive_id": {
                    "type": "string",
                    "description": "Exact archive_id returned by list_archives.",
                },
                "article_path": {
                    "type": "string",
                    "description": "Copy article_path verbatim from search results; it is not a title.",
                },
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "citation_label": {"type": "string"},
            },
            "additionalProperties": False,
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
                "available_citation_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Real labels to retry with when citation_label matched nothing.",
                },
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
                "available_citation_labels",
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
                "archive_id": {
                    "type": "string",
                    "description": "Exact archive_id returned by list_archives.",
                },
                "article_path": {
                    "type": "string",
                    "description": "Copy article_path verbatim from search results; it is not a title.",
                },
                "image_index": {"type": "integer", "minimum": 0},
                "image_path": {
                    "type": "string",
                    "description": "Choose an exact image_path returned by read_article; overrides image_index.",
                },
            },
            "additionalProperties": False,
            "required": ["archive_id", "article_path"],
        },
        annotations=WRITES_CACHE,
    ),
]


# Derived from the published schemas so the interface has a single source of truth.
_TOOL_ARGUMENTS: dict[str, tuple[set[str], set[str]]] = {
    tool.name: (
        set(tool.input_schema.get("properties", {})),
        set(tool.input_schema.get("required", [])),
    )
    for tool in _TOOL_DEFINITIONS
}


def _declared_type(spec: dict[str, Any]) -> str:
    """First non-null JSON Schema type, so ["string", "null"] reads as string."""
    declared = spec.get("type", "string")
    if isinstance(declared, list):
        return next((item for item in declared if item != "null"), "string")
    return str(declared)


_TOOL_ARGUMENT_TYPES: dict[str, dict[str, str]] = {
    tool.name: {
        key: _declared_type(spec)
        for key, spec in tool.input_schema.get("properties", {}).items()
    }
    for tool in _TOOL_DEFINITIONS
}


def _coerce_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Coerce scalar arguments to their declared types.

    Clients routinely send numbers as strings, which otherwise surfaces as a
    bare TypeError deep inside a tool instead of a usable message.
    """
    types = _TOOL_ARGUMENT_TYPES.get(name, {})
    coerced = dict(arguments)
    for key, value in arguments.items():
        expected = types.get(key)
        if expected == "integer":
            if isinstance(value, bool):
                # bool is an int subclass; True would silently become 1.
                raise ValueError(
                    f"Invalid argument for {name}: {key} must be an integer, "
                    f"got {value!r}."
                )
            if isinstance(value, int):
                continue
            try:
                coerced[key] = int(str(value).strip())
            except (TypeError, ValueError):
                raise ValueError(
                    f"Invalid argument for {name}: {key} must be an integer, "
                    f"got {value!r}."
                ) from None
        elif expected == "boolean" and not isinstance(value, bool):
            text = str(value).strip().lower()
            if text in {"true", "1", "yes"}:
                coerced[key] = True
            elif text in {"false", "0", "no"}:
                coerced[key] = False
            else:
                raise ValueError(
                    f"Invalid argument for {name}: {key} must be a boolean, "
                    f"got {value!r}."
                )
        elif expected == "string" and not isinstance(value, str):
            # A number where a string is declared (e.g. query=2026) is usable;
            # a list, object or null would only surface as a bare AttributeError
            # deep inside the tool, so reject it here with a real message.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                coerced[key] = str(value)
            else:
                raise ValueError(
                    f"Invalid argument for {name}: {key} must be a string, "
                    f"got {type(value).__name__}."
                )
    return coerced


def _dispatch_tool(name: str, arguments: Any) -> Any:
    _LOGGER.info("tool=%s", name)
    # A client that sends a JSON string, list or scalar instead of an object
    # never reaches a tool; say so once instead of leaking an iteration error.
    if arguments is None:
        arguments = {}
    elif not isinstance(arguments, dict):
        allowed_names = sorted(_TOOL_ARGUMENTS.get(name, (set(), set()))[0])
        expected = (
            f"Valid arguments: {', '.join(allowed_names)}."
            if allowed_names
            else f"{name} takes no arguments."
        )
        raise ValueError(
            f"Invalid arguments for {name}: expected a JSON object of named "
            f"parameters, got {type(arguments).__name__}. {expected}"
        )
    # Agents routinely guess parameter names (path, limit, id). Reject unknown or
    # missing keys here so the error names the valid parameters instead of
    # surfacing a bare Python TypeError the caller cannot act on.
    if name in _TOOL_ARGUMENTS:
        allowed, required = _TOOL_ARGUMENTS[name]
        unknown = sorted(set(arguments) - allowed)
        missing = sorted(required - set(arguments))
        if unknown or missing:
            problems = []
            if unknown:
                problems.append(f"unknown argument(s): {', '.join(unknown)}")
            if missing:
                problems.append(f"missing required argument(s): {', '.join(missing)}")
            raise ValueError(
                f"Invalid arguments for {name} ({'; '.join(problems)}). "
                + (
                    f"Valid arguments: {', '.join(sorted(allowed))}. "
                    f"Required: {', '.join(sorted(required)) or 'none'}."
                    if allowed
                    else f"{name} takes no arguments."
                )
            )
        arguments = _coerce_arguments(name, arguments)
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
    raise ValueError(
        f"Unknown tool: {name!r}. Available tools: "
        f"{', '.join(tool.name for tool in _TOOL_DEFINITIONS)}."
    )


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
        _too_large(item.size)
    if not (item.mimetype.startswith("text/") or "html" in item.mimetype):
        _not_text(item.mimetype)

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
        return _mcp_result(_dispatch_tool(params.name, params.arguments))
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
