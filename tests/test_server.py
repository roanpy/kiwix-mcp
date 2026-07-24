import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import ImageContent, TextContent

import server


def test_empty_archive_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KIWIX_ARCHIVE_DIR", str(tmp_path))
    result = server.search("test")
    assert result["status"] == "empty"
    assert result["results"] == []


def test_html_cleanup_and_query_excerpt() -> None:
    content = server._plain_text(
        b"<html><style>bad</style><body><h1>Title</h1><p>Useful answer</p><script>bad</script></body></html>",
        "text/html",
    )
    assert content == "Title\nUseful answer"
    assert "Useful answer" in server._excerpt(
        "x" * 1200 + content, "Useful answer", 1000
    )


def test_archive_selection_rejects_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KIWIX_ARCHIVE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="Unknown archive"):
        server._select_paths("missing.zim")


def test_zim_image_url_resolution() -> None:
    assert (
        server._resolve_image_path("topic/article", "../images/a%20b.png?width=640#x")
        == "images/a b.png"
    )
    assert server._resolve_image_path("topic", "../../assets/a.png") == "assets/a.png"
    assert server._resolve_image_path("topic", "/assets/a.png") == "assets/a.png"
    with pytest.raises(ValueError, match="internal image"):
        server._resolve_image_path("topic", "https://example.com/a.png")


def test_extract_image_returns_native_mcp_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    item = SimpleNamespace(mimetype="image/webp", size=3, content=b"img")
    entry = SimpleNamespace(is_redirect=False, get_item=lambda: item)
    archive = SimpleNamespace(get_entry_by_path=lambda path: entry)
    monkeypatch.setattr(
        server,
        "_find_images_in_article",
        lambda archive, path: [
            {"image_path": "image.webp", "filename": "image.webp", "alt": ""}
        ],
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: archive)
    monkeypatch.setattr(server, "IMAGE_TEMP_DIR", tmp_path)
    monkeypatch.setattr(server, "MAX_TEMP_IMAGES", 1)
    old = tmp_path / "old.webp"
    old.write_bytes(b"old")
    os.utime(old, ns=(1, 1))

    content = asyncio.run(
        server.mcp._tool_manager.call_tool(
            "extract_image",
            {"archive_id": "test.zim", "article_path": "article"},
            convert_result=True,
        )
    )
    assert isinstance(content[0], TextContent)
    assert isinstance(content[1], ImageContent)
    assert content[1].mimeType == "image/webp"
    assert next(tmp_path.iterdir()).read_bytes() == b"img"
    assert not old.exists()
