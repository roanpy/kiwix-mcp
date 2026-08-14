from __future__ import annotations

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

MCP_INSTRUCTIONS = (
    "Search and read local ZIM archives. Use list_archives and choose archive_id "
    "by language and collection: prefer full archives for coverage and maxi archives "
    "when images matter; use title, date, and description to break ties. "
    "For long Wikipedia articles, call inspect_article first, then read_article with "
    "a returned section anchor. Use list_references when a claim needs provenance. "
    "For images, inspect read_article metadata and prefer image_path over index 0. "
    "For cross-archive comparison, call search with archive_id='*'. "
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
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
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
    "#toc",
    ".toc",
    "#catlinks",
    ".catlinks",
    ".printfooter",
    ".noprint",
)
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
                try:
                    value = archive.get_metadata(key)
                    item[key.lower()] = bytes(value).decode("utf-8", errors="replace")
                except (KeyError, RuntimeError):
                    pass
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
    selected = _select_paths("") if cross_archive else _select_paths(archive_id)

    def _archive_matches(archive: Archive) -> bool:
        """Cross-archive filter by language/flavour metadata; True when no filter."""
        if not cross_archive:
            return True
        if language:
            try:
                value = bytes(archive.get_metadata("Language")).decode(
                    "utf-8", errors="replace"
                )
            except (KeyError, RuntimeError, ValueError):
                return False
            languages = {token.strip() for token in value.lower().split(",")}
            alias = _LANGUAGE_ALIASES.get(language, "")
            if not any(token in languages for token in (language, alias) if token):
                return False
        if flavour:
            try:
                value = bytes(archive.get_metadata("Flavour")).decode(
                    "utf-8", errors="replace"
                )
            except (KeyError, RuntimeError, ValueError):
                return False
            if flavour != value.lower().strip():
                return False
        return True

    exact_matches: list[tuple[Archive, str, str, str]] = []
    other_matches: list[tuple[Archive, str, str, str]] = []
    errors: list[dict[str, str]] = []
    estimated_matches = 0
    estimated_known = False
    for name, path in selected:
        try:
            archive = _archive(path)
            if not _archive_matches(archive):
                continue
            if mode == "fulltext":
                if not archive.has_fulltext_index:
                    raise ValueError(
                        f"Archive does not support full-text search: {name}"
                    )
                search_result = Searcher(archive).search(Query().set_query(query))
                search_mode = "fulltext"
            elif mode == "title" or not archive.has_fulltext_index:
                search_result = SuggestionSearcher(archive).suggest(query)
                search_mode = "title"
            else:
                search_result = Searcher(archive).search(Query().set_query(query))
                search_mode = "fulltext"
            exact_path = (
                archive.get_entry_by_title(query).path
                if archive.has_entry_by_title(query)
                else None
            )
            estimate = search_result.getEstimatedMatches()
            if estimate is not None:
                estimated_matches += int(estimate)
                estimated_known = True
            found = search_result.getResults(0, offset + limit + 1)
            seen: set[str] = set()
            if exact_path:
                exact_matches.append((archive, name, str(exact_path), "exact_title"))
                seen.add(str(exact_path))
            for article_path in found:
                article_path = str(article_path)
                if article_path in seen:
                    continue
                seen.add(article_path)
                other_matches.append((archive, name, article_path, search_mode))
        except (OSError, RuntimeError, ValueError) as exc:
            if mode == "fulltext":
                raise
            errors.append({"archive_id": name, "error": str(exc)})
    matches = exact_matches + other_matches
    page_matches = matches[offset : offset + limit]
    results: list[dict[str, Any]] = []
    for archive, name, article_path, match_type in page_matches:
        try:
            item = _article_summary(archive, name, article_path, query)
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append({"archive_id": name, "error": str(exc)})
            continue
        item["search_mode"] = (
            "fulltext" if match_type in {"exact_title", "fulltext"} else "title"
        )
        item["match_type"] = match_type
        results.append(item)
    has_more = len(matches) > offset + len(page_matches)
    return {
        "status": "ok" if results else "no_hits",
        "query": query,
        "offset": offset,
        "next_offset": offset + len(page_matches)
        if has_more and page_matches
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
    max_chars = min(max(int(max_chars), 1000), 50000)
    offset = max(int(offset), 0)
    content = bytes(item.content)
    images: list[dict[str, Any]] | None = None
    see_also: list[dict[str, Any]] | None = None
    links: list[dict[str, Any]] | None = None
    selected_section: dict[str, Any] | None = None
    canonical_url: str | None = None
    if "html" in item.mimetype.lower():
        soup = BeautifulSoup(content.decode("utf-8", errors="replace"), "html.parser")
        canonical = soup.find("link", rel="canonical", href=True)
        canonical_url = str(canonical["href"]) if canonical else None
        if include_images:
            images = _find_images(soup, entry.path)
        if include_links:
            see_also, links = _find_links(archive, soup, entry.path, name)
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract internal article links: curated See-also section first, then body links.

    Returns (see_also, links); each item is {article_path, title} with
    article_path verified to exist and directly usable with read_article.
    """
    see_also: list[dict[str, Any]] = []
    body: list[dict[str, Any]] = []
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

    if not see_also:
        for anchor in root.find_all("a", href=True):
            if len(body) >= MAX_BODY_LINKS:
                break
            add(anchor, body, MAX_BODY_LINKS)

    return see_also, body


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
    images: list[dict[str, Any]] = []
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue
        try:
            image_path = _resolve_image_path(article_path, str(src))
        except ValueError:
            continue
        figure = img.find_parent("figure")
        caption = figure.find("figcaption") if figure else None
        width, height = img.get("width"), img.get("height")
        width_value = int(width) if str(width).isdigit() else None
        images.append(
            {
                "index": len(images),
                "src": str(src),
                "image_path": image_path,
                "filename": posixpath.basename(image_path),
                "alt": str(img.get("alt", "")),
                "caption": caption.get_text(" ", strip=True) if caption else "",
                "width": width_value,
                "height": int(height) if str(height).isdigit() else None,
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
        description="Search one selected ZIM archive with exact-title priority, pagination, and match types; use archive_id='*' to aggregate all archives.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "archive_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "offset": {"type": "integer", "minimum": 0, "maximum": 1000},
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
        description="Read bounded article text with optional image metadata and related links.",
        inputSchema={
            "type": "object",
            "properties": {
                "archive_id": {"type": "string"},
                "article_path": {"type": "string"},
                "query": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 50000},
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
    import asyncio

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await mcp.run(
                read_stream, write_stream, mcp.create_initialization_options()
            )

    asyncio.run(_run())
