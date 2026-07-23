from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from libzim import Archive, Query, Searcher, SuggestionSearcher
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

DEFAULT_ARCHIVE_DIR = Path("/Users/user/.chroma_db/kiwix/archives")
MAX_ARTICLE_BYTES = 16 * 1024 * 1024

mcp = FastMCP("kiwix", instructions="Search and read local ZIM archives.")
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _archive_dir() -> Path:
    return Path(os.environ.get("KIWIX_ARCHIVE_DIR", DEFAULT_ARCHIVE_DIR)).expanduser()


def _archive_paths() -> dict[str, Path]:
    root = _archive_dir()
    return {path.name: path for path in sorted(root.glob("*.zim")) if path.is_file()}


def _select_paths(archive_id: str) -> list[tuple[str, Path]]:
    archives = _archive_paths()
    requested = archive_id.strip()
    if not requested:
        return list(archives.items())
    for name, path in archives.items():
        if requested in {name, path.stem}:
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
    soup = BeautifulSoup(text, "html.parser")
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    return "\n".join(
        line for line in (part.strip() for part in soup.get_text("\n").splitlines()) if line
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


def _article_summary(archive: Archive, archive_id: str, article_path: str, query: str) -> dict[str, Any]:
    entry = _entry(archive, article_path)
    item = entry.get_item()
    summary = ""
    if item.size <= MAX_ARTICLE_BYTES and (item.mimetype.startswith("text/") or "html" in item.mimetype):
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
def search(query: str, archive_id: str = "", limit: int = 5) -> dict[str, Any]:
    """Search local ZIM archives. Uses full-text indexes when available."""
    query = query.strip()
    if not query:
        raise ValueError("query is required")
    limit = min(max(int(limit), 1), 20)
    selected = _select_paths(archive_id)
    if not selected:
        return {"status": "empty", "message": f"Place .zim files in {_archive_dir()}", "results": []}

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for name, path in selected:
        if len(results) >= limit:
            break
        try:
            archive = _archive(path)
            remaining = limit - len(results)
            if archive.has_fulltext_index:
                found = Searcher(archive).search(Query().set_query(query)).getResults(0, remaining)
                mode = "fulltext"
            else:
                found = SuggestionSearcher(archive).suggest(query).getResults(0, remaining)
                mode = "title"
            for article_path in found:
                item = _article_summary(archive, name, str(article_path), query)
                item["search_mode"] = mode
                results.append(item)
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append({"archive_id": name, "error": str(exc)})
    return {"status": "ok" if results else "no_hits", "query": query, "results": results, "errors": errors}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def read_article(
    archive_id: str,
    article_path: str,
    query: str = "",
    max_chars: int = 12000,
) -> dict[str, Any]:
    """Read text from one article returned by search."""
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
    text = _plain_text(bytes(item.content), item.mimetype)
    return {
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


if __name__ == "__main__":
    mcp.run(transport="stdio")

