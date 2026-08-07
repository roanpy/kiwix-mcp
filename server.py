from __future__ import annotations

import base64
import hashlib
import inspect
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

DEFAULT_ARCHIVE_DIR = Path("/Users/peter/.chroma_db/kiwix/archives")
IMAGE_TEMP_DIR = Path(tempfile.gettempdir()) / "kiwix-mcp"
MAX_ARTICLE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TEMP_IMAGES = 16
MAX_IMAGE_REFERENCES = 30
MAX_RESOURCE_CHARS = 50_000
MAX_LOG_BYTES = 1 * 1024 * 1024
LOG_BACKUP_COUNT = 2

_LOGGER = logging.getLogger("kiwix-mcp")

MCP_INSTRUCTIONS = (
    "Search and read local ZIM archives. Use list_archives and choose archive_id "
    "by language and collection: prefer full archives for coverage and maxi archives "
    "when images matter; use title, date, and description to break ties. "
    "For images, inspect read_article metadata and prefer image_path over index 0. "
    "For cross-archive comparison, call search with archive_id='*'. "
    "Ask only when the user's goal leaves the choice ambiguous. "
    "Answer in the user's language; "
    "translate source text when needed, preserve proper nouns, and keep source URIs."
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
    article_path = unquote(article_path)
    return archive_id, article_path


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


def _plain_text(content: bytes, mimetype: str) -> str:
    text = content.decode("utf-8", errors="replace")
    if "html" not in mimetype.lower():
        return " ".join(text.split())
    return _soup_text(BeautifulSoup(text, "html.parser"))


def _soup_text(soup: BeautifulSoup) -> str:
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    for node in soup.find_all("br"):
        node.replace_with("\n")
    for node in soup.find_all(["th", "td"]):
        node.insert_after("\t")
    for node in soup.find_all(
        ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "figcaption"]
    ):
        node.insert_after("\n")
    return "\n".join(
        line
        for line in (" ".join(part.split()) for part in soup.get_text().splitlines())
        if line
    )


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
) -> dict[str, Any]:
    """Search one archive, or all archives with archive_id='*'."""
    query = query.strip()
    if not query:
        raise ValueError("query is required")
    if not archive_id.strip():
        raise ValueError("archive_id is required")
    mode = mode.strip().lower()
    if mode not in {"auto", "fulltext", "title"}:
        raise ValueError("mode must be one of: auto, fulltext, title")
    limit = min(max(int(limit), 1), 20)
    offset = min(max(int(offset), 0), 1000)
    selected = (
        _select_paths("") if archive_id.strip() == "*" else _select_paths(archive_id)
    )

    exact_matches: list[tuple[Archive, str, str, str]] = []
    other_matches: list[tuple[Archive, str, str, str]] = []
    errors: list[dict[str, str]] = []
    estimated_matches = 0
    estimated_known = False
    for name, path in selected:
        try:
            archive = _archive(path)
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
    if "html" in item.mimetype.lower():
        soup = BeautifulSoup(content.decode("utf-8", errors="replace"), "html.parser")
        if include_images:
            images = _find_images(soup, entry.path)
        if include_links:
            see_also, links = _find_links(archive, soup, entry.path, name)
        text = _soup_text(soup)
    else:
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
        "uri": _article_uri(name, entry.path),
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


MAX_SEE_ALSO_LINKS = 10
MAX_BODY_LINKS = 15
_SEE_ALSO_LABELS = {"see also", "参见", "參見"}


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

    def add(anchor, target: list[dict[str, Any]], cap: int) -> None:
        if len(target) >= cap:
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

    for h2 in soup.find_all("h2"):
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
        for anchor in soup.find_all("a", href=True):
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
        images.append(
            {
                "index": len(images),
                "src": str(src),
                "image_path": image_path,
                "filename": posixpath.basename(image_path),
                "alt": str(img.get("alt", "")),
                "caption": caption.get_text(" ", strip=True) if caption else "",
                "width": int(width) if str(width).isdigit() else None,
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
    suffix = mimetypes.guess_extension(mimetype) or ".img"
    file_path = IMAGE_TEMP_DIR / f"{hashlib.sha256(raw).hexdigest()[:16]}{suffix}"
    if file_path.exists():
        file_path.touch()
    else:
        file_path.write_bytes(raw)
    files = sorted(
        (path for path in IMAGE_TEMP_DIR.iterdir() if path.is_file()),
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
                            "flavour": {"type": "string"},
                            "has_main_entry": {"type": "boolean"},
                            "main_entry_path": {"type": "string"},
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
                "text": {"type": "string"},
                "offset": {"type": "integer"},
                "next_offset": {"type": ["integer", "null"]},
                "truncated": {"type": "boolean"},
                "total_chars": {"type": "integer"},
                "uri": {"type": "string"},
                "total_images": {"type": "integer"},
                "image_offset": {"type": "integer"},
                "next_image_offset": {"type": ["integer", "null"]},
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
    if name == "read_article":
        return read_article(**arguments)
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


def _legacy_result(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    return [_json_content(item) if isinstance(item, dict) else item for item in value]


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
    selected = _select_paths(archive_id)
    if len(selected) != 1:
        raise ValueError("archive_id is required")
    _, path = selected[0]
    archive = _archive(path)
    try:
        entry = _entry(archive, article_path)
    except (OSError, RuntimeError, ValueError):
        resolved_main_entry = _main_entry_path(archive)
        if resolved_main_entry is None or article_path == resolved_main_entry:
            raise
        entry = _entry(archive, resolved_main_entry)
    item = entry.get_item()
    if item.size > MAX_ARTICLE_BYTES:
        raise ValueError(f"Article is too large to read safely: {item.size} bytes")
    if not (item.mimetype.startswith("text/") or "html" in item.mimetype):
        raise ValueError(f"Article is not text: {item.mimetype}")

    full_text = _plain_text(bytes(item.content), item.mimetype)
    truncated = len(full_text) > MAX_RESOURCE_CHARS
    text = full_text if not truncated else full_text[:MAX_RESOURCE_CHARS].rstrip()
    meta = {
        "archive_id": archive_id,
        "article_path": entry.path,
        "mimetype": item.mimetype,
        "status": "ok",
        "total_chars": len(full_text),
        "truncated": truncated,
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


if "on_list_tools" in inspect.signature(Server).parameters:
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
else:
    mcp = Server("kiwix", instructions=MCP_INSTRUCTIONS)

    @mcp.list_tools()
    async def _list_tools_v1() -> list[Tool]:
        return _TOOL_DEFINITIONS

    @mcp.call_tool()
    async def _call_tool_v1(name: str, arguments: dict[str, Any]) -> Any:
        return _legacy_result(_dispatch_tool(name, arguments or {}))


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
