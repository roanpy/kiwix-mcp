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
    result = server.list_archives()
    assert result["status"] == "empty"
    assert result["archives"] == []


def test_html_cleanup_and_query_excerpt() -> None:
    content = server._plain_text(
        b"<html><style>bad</style><body><h1>Title</h1><p>Useful <a>linked</a> answer</p><script>bad</script></body></html>",
        "text/html",
    )
    assert content == "Title\nUseful linked answer"
    assert "Useful answer" in server._excerpt(
        "x" * 1200 + "Useful answer", "Useful answer", 1000
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


def test_image_metadata_uses_caption_and_dimensions() -> None:
    soup = server.BeautifulSoup(
        '<figure><img src="../a.jpg" width="250" height="166">'
        "<figcaption>Useful caption</figcaption></figure>",
        "html.parser",
    )
    assert server._find_images(soup, "topic/article")[0] == {
        "index": 0,
        "src": "../a.jpg",
        "image_path": "a.jpg",
        "filename": "a.jpg",
        "alt": "",
        "caption": "Useful caption",
        "width": 250,
        "height": 166,
    }


def test_links_prefer_see_also_and_fallback_to_body() -> None:
    archive = SimpleNamespace(has_entry_by_path=lambda path: True)
    soup = server.BeautifulSoup(
        '<p><a href="body">Body</a></p><h2 id="See_also">See also</h2>'
        '<ul><li><a href="related">Related</a></li></ul><h2>References</h2>',
        "html.parser",
    )
    see_also, links = server._find_links(archive, soup, "article")
    assert see_also == [{"article_path": "related", "title": "Related"}]
    assert links == []

    see_also, links = server._find_links(
        archive,
        server.BeautifulSoup('<a href="body">Body</a>', "html.parser"),
        "article",
    )
    assert see_also == []
    assert links == [{"article_path": "body", "title": "Body"}]


def test_search_pagination_keeps_exact_title_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = SimpleNamespace(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: True,
        get_entry_by_title=lambda query: SimpleNamespace(path="exact"),
    )
    search_result = SimpleNamespace(
        getResults=lambda start, limit: ["other-1", "exact", "other-2", "other-3"]
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: archive)
    monkeypatch.setattr(
        server,
        "Searcher",
        lambda archive: SimpleNamespace(search=lambda query: search_result),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, archive_id, article_path, query: {"article_path": article_path},
    )

    first = server.search("Exact", "test.zim", limit=2)
    second = server.search("Exact", "test.zim", limit=2, offset=2)
    assert [item["article_path"] for item in first["results"]] == ["exact", "other-1"]
    assert [item["article_path"] for item in second["results"]] == [
        "other-2",
        "other-3",
    ]


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
            {
                "image_path": "image.webp",
                "filename": "image.webp",
                "alt": "",
                "caption": "",
                "width": 1,
                "height": 1,
            }
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
    assert (
        server.mcp._tool_manager.get_tool("extract_image").annotations.readOnlyHint
        is False
    )
