from __future__ import annotations

import hashlib
import mimetypes
import os
import posixpath
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from libzim import Archive, Query, Searcher, SuggestionSearcher
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations

DEFAULT_ARCHIVE_DIR = Path("/Users/user/.chroma_db/kiwix/archives")
IMAGE_TEMP_DIR = Path(tempfile.gettempdir()) / "kiwix-mcp"
MAX_ARTICLE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TEMP_IMAGES = 16

mcp = FastMCP(
    "kiwix",
    instructions=(
        "Search and read local ZIM archives. Use list_archives to select archive_id. "
        "Answer in the user's language; "
        "translate source text when needed, preserve proper nouns, and keep source URIs."
    ),
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


@lru_cache(maxsize=4)
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
        "uri": f"kiwix://{archive_id}/{entry.path}",
    }


@mcp.tool(annotations=READ_ONLY, structured_output=True)
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
            for key in ("Title", "Language", "Date"):
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


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def search(query: str, archive_id: str, limit: int = 5) -> dict[str, Any]:
    """Search one ZIM archive selected by archive_id from list_archives."""
    query = query.strip()
    if not query:
        raise ValueError("query is required")
    if not archive_id.strip():
        raise ValueError("archive_id is required")
    limit = min(max(int(limit), 1), 20)
    selected = _select_paths(archive_id)

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for name, path in selected:
        if len(results) >= limit:
            break
        try:
            archive = _archive(path)
            remaining = limit - len(results)
            if archive.has_fulltext_index:
                found = (
                    Searcher(archive)
                    .search(Query().set_query(query))
                    .getResults(0, remaining)
                )
                mode = "fulltext"
            else:
                found = (
                    SuggestionSearcher(archive).suggest(query).getResults(0, remaining)
                )
                mode = "title"
            for article_path in found:
                item = _article_summary(archive, name, str(article_path), query)
                item["search_mode"] = mode
                results.append(item)
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append({"archive_id": name, "error": str(exc)})
    return {
        "status": "ok" if results else "no_hits",
        "query": query,
        "results": results,
        "errors": errors,
    }


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def read_article(
    archive_id: str,
    article_path: str,
    query: str = "",
    max_chars: int = 12000,
    include_images: bool = True,
) -> dict[str, Any]:
    """Read article text. include_images=True 时附带文章中的图片列表。"""
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
    content = bytes(item.content)
    images: list[dict[str, Any]] | None = None
    if "html" in item.mimetype.lower():
        soup = BeautifulSoup(content.decode("utf-8", errors="replace"), "html.parser")
        if include_images:
            images = _find_images(soup, entry.path)
        text = _soup_text(soup)
    else:
        text = _plain_text(content, item.mimetype)
    result: dict[str, Any] = {
        "status": "ok",
        "archive_id": name,
        "article_path": entry.path,
        "title": entry.title or item.title,
        "mimetype": item.mimetype,
        "text": _excerpt(text, query, max_chars),
        "truncated": len(text) > max_chars,
        "total_chars": len(text),
        "uri": f"kiwix://{name}/{entry.path}",
    }
    if include_images:
        images = images or []
        result["total_images"] = len(images)
        result["images"] = images
    return result


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
        "total_images": len(images),
        "alt": img_info["alt"],
        "filename": img_info["filename"],
        "article_path": article_path,
    }
    return [metadata, Image(data=raw, format=img_item.mimetype.removeprefix("image/"))]


@mcp.tool(annotations=WRITES_CACHE, structured_output=False)
def extract_image(
    archive_id: str, article_path: str, image_index: int = 0
) -> list[Any]:
    """Return native MCP image content plus file_path.

    If native images are unsupported, call the client's read tool on file_path.
    """
    selected = _select_paths(archive_id)
    if len(selected) != 1:
        raise ValueError("archive_id is required")
    _, path = selected[0]
    archive = _archive(path)
    return _extract_image(archive, article_path, image_index)


if __name__ == "__main__":
    # libzim/Xapian writes diagnostics directly to fd 1. Keep JSON-RPC on a
    # duplicate of the original stdout and send native diagnostics to stderr.
    protocol_stdout = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = protocol_stdout
    mcp.run(transport="stdio")
