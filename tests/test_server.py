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


def test_list_archives_exposes_zim_flavour(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = SimpleNamespace(
        article_count=10,
        media_count=20,
        has_fulltext_index=True,
        has_title_index=True,
        get_metadata=lambda key: {
            "Title": b"Test",
            "Flavour": b"maxi",
        }[key],
    )
    path = tmp_path / "test.zim"
    path.write_bytes(b"zim")
    monkeypatch.setenv("KIWIX_ARCHIVE_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_archive", lambda path: archive)

    result = server.list_archives()

    assert result["archives"][0]["title"] == "Test"
    assert result["archives"][0]["flavour"] == "maxi"


def test_html_cleanup_and_query_excerpt() -> None:
    content = server._plain_text(
        b"<html><style>bad</style><body><h1>Title</h1><p>Useful <a>linked</a> answer</p><script>bad</script></body></html>",
        "text/html",
    )
    assert content == "Title\nUseful linked answer"
    assert "Useful answer" in server._excerpt(
        "x" * 1200 + "Useful answer", "Useful answer", 1000
    )


def test_article_window_supports_query_and_continuation() -> None:
    text = "a" * 1200 + "needle" + "b" * 1200
    first, offset, next_offset = server._article_window(text, "needle", 1000, 0)
    assert "needle" in first
    assert offset > 0
    assert next_offset is not None

    second, second_offset, _ = server._article_window(text, "", 1000, next_offset)
    assert second_offset == next_offset
    assert second == text[next_offset : next_offset + 1000]


def test_read_article_is_not_truncated_when_query_window_contains_all_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "a" * 900 + "needle" + "b" * 94
    item = SimpleNamespace(
        size=len(text), mimetype="text/plain", content=text.encode(), title="Article"
    )
    entry = SimpleNamespace(
        is_redirect=False, path="article", title="Article", get_item=lambda: item
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: SimpleNamespace())
    monkeypatch.setattr(server, "_entry", lambda archive, article_path: entry)

    result = server.read_article(
        "test.zim",
        "article",
        query="needle",
        max_chars=1000,
        include_images=False,
        include_links=False,
    )

    assert result["next_offset"] is None
    assert result["truncated"] is False
    assert result["text"] == text[567:]


def test_archive_selection_rejects_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KIWIX_ARCHIVE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="Unknown archive"):
        server._select_paths("missing.zim")


def test_mcp_tool_registry_is_stable() -> None:
    result = asyncio.run(server._list_tools_v2(None, None))
    assert [tool.name for tool in result.tools] == [
        "list_archives",
        "search",
        "read_article",
        "extract_image",
    ]
    assert result.tools[0].output_schema is not None
    assert (
        "flavour"
        in result.tools[0].output_schema["properties"]["archives"]["items"][
            "properties"
        ]
    )
    assert (
        "match_type"
        in result.tools[1].output_schema["properties"]["results"]["items"]["properties"]
    )
    assert "mode" in result.tools[1].input_schema["properties"]
    assert "offset" in result.tools[2].input_schema["properties"]
    assert "image_offset" in result.tools[2].input_schema["properties"]
    assert "image_path" in result.tools[3].input_schema["properties"]


def test_mcp_resource_template_is_available() -> None:
    result = asyncio.run(server._list_resource_templates_v2(None, None))
    assert [item.uri_template for item in result.resource_templates] == [
        "kiwix://{archive_id}/{+article_path}"
    ]


def test_mcp_resource_list_uses_main_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archives = {"first.zim": Path("first.zim"), "second.zim": Path("second.zim")}
    monkeypatch.setattr(
        server,
        "_archive_paths",
        lambda: archives,
    )
    monkeypatch.setattr(
        server,
        "_archive",
        lambda path: SimpleNamespace(
            has_main_entry=path.name == "first.zim",
            main_entry=SimpleNamespace(path="main", title="Main"),
        ),
    )
    result = asyncio.run(server._list_resources_v2(None, None))
    resources = {r.name: r for r in result.resources}
    assert "first.zim-main" in resources
    assert resources["first.zim-main"].uri == "kiwix://first.zim/main"
    assert "second.zim-main" not in resources


def test_mcp_resource_list_resolves_redirect_main_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archives = {"first.zim": Path("first.zim")}
    monkeypatch.setattr(
        server,
        "_archive_paths",
        lambda: archives,
    )
    monkeypatch.setattr(
        server,
        "_archive",
        lambda path: SimpleNamespace(
            has_main_entry=True,
            main_entry=SimpleNamespace(
                path="mainPage",
                title="Main",
                is_redirect=True,
                get_redirect_entry=lambda: SimpleNamespace(path="questions"),
            ),
        ),
    )

    result = asyncio.run(server._list_resources_v2(None, None))
    resources = {r.name: r for r in result.resources}
    assert resources["first.zim-main"].uri == "kiwix://first.zim/questions"
    assert resources["first.zim-main"].meta["article_path"] == "questions"


def test_resource_read_returns_clean_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = SimpleNamespace(
        size=20,
        mimetype="text/html",
        content=b"<html><p>Hello <b>World</b></p></html>",
    )
    archive = SimpleNamespace(main_entry=SimpleNamespace(path="main"))
    entry = SimpleNamespace(
        path="topic/Article.html", title="Article", get_item=lambda: item
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: archive)
    monkeypatch.setattr(server, "_entry", lambda archive, path: entry)

    result = asyncio.run(
        server._read_resource_v2(
            None,
            SimpleNamespace(uri="kiwix://test.zim/topic/Article.html"),
        )
    )
    assert result.contents[0].text == "Hello World"
    assert result.meta["archive_id"] == "test.zim"
    assert result.contents[0].meta["truncated"] is False


def test_read_resource_follows_main_entry_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = SimpleNamespace(
        size=40,
        mimetype="text/html",
        content=b"<html><p>Hello from resolved main entry</p></html>",
    )

    def _entry(_archive: object, article_path: str):
        if article_path == "questions":
            return SimpleNamespace(path="questions", get_item=lambda: item)
        raise ValueError(f"Cannot find entry: {article_path}")

    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("first.zim", Path("first.zim"))]
    )
    monkeypatch.setattr(
        server,
        "_archive",
        lambda path: SimpleNamespace(
            has_main_entry=True,
            main_entry=SimpleNamespace(
                path="mainPage",
                is_redirect=True,
                get_redirect_entry=lambda: SimpleNamespace(path="questions"),
            ),
        ),
    )
    monkeypatch.setattr(server, "_entry", _entry)

    result = asyncio.run(
        server._read_resource_v2(
            None,
            SimpleNamespace(uri="kiwix://first.zim/mainPage"),
        )
    )
    assert result.contents[0].text == "Hello from resolved main entry"


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
    see_also, links = server._find_links(archive, soup, "article", "en_all.zim")
    assert see_also == [
        {
            "article_path": "related",
            "title": "Related",
            "uri": "kiwix://en_all.zim/related",
        }
    ]
    assert links == []

    see_also, links = server._find_links(
        archive,
        server.BeautifulSoup('<a href="body">Body</a>', "html.parser"),
        "article",
        "en_all.zim",
    )
    assert see_also == []
    assert links == [
        {"article_path": "body", "title": "Body", "uri": "kiwix://en_all.zim/body"}
    ]


def test_search_mode_can_forbid_fulltext_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = SimpleNamespace(
        has_fulltext_index=False, has_entry_by_title=lambda query: False
    )
    monkeypatch.setattr(
        server,
        "_select_paths",
        lambda archive_id: [("test.zim", Path("test.zim"))],
    )
    monkeypatch.setattr(server, "_archive", lambda path: archive)
    with pytest.raises(ValueError, match="does not support full-text search"):
        server.search("query", "test.zim", mode="fulltext")


def test_article_uri_round_trip() -> None:
    uri = server._article_uri("zh_all.zim", "Science/Topic A")
    archive_id, article_path = server._parse_article_uri(uri)
    assert archive_id == "zh_all.zim"
    assert article_path == "Science/Topic A"


def test_search_pagination_keeps_exact_title_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = SimpleNamespace(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: True,
        get_entry_by_title=lambda query: SimpleNamespace(path="exact"),
    )
    paths = ["other-1", "exact", "other-2", "other-3"]
    search_result = SimpleNamespace(
        getResults=lambda start, limit: paths[start : start + limit],
        getEstimatedMatches=lambda: len(paths),
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
    assert first["mode"] == "auto"
    assert [item["article_path"] for item in first["results"]] == ["exact", "other-1"]
    assert [item["match_type"] for item in first["results"]] == [
        "exact_title",
        "fulltext",
    ]
    assert [item["article_path"] for item in second["results"]] == [
        "other-2",
        "other-3",
    ]
    assert [item["match_type"] for item in second["results"]] == [
        "fulltext",
        "fulltext",
    ]
    assert first["next_offset"] == 2
    assert second["next_offset"] is None
    assert second["estimated_matches"] == 4


def test_search_can_aggregate_all_archives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = SimpleNamespace(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
        name="first",
    )
    second = SimpleNamespace(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
        name="second",
    )
    results = {
        "first": ["a", "b"],
        "second": ["c", "d"],
    }
    monkeypatch.setattr(
        server,
        "_select_paths",
        lambda archive_id: [
            ("first.zim", Path("first.zim")),
            ("second.zim", Path("second.zim")),
        ],
    )
    monkeypatch.setattr(
        server, "_archive", lambda path: first if path.name == "first.zim" else second
    )
    monkeypatch.setattr(
        server,
        "Searcher",
        lambda archive: SimpleNamespace(
            search=lambda query: SimpleNamespace(
                getResults=lambda start, limit: results[archive.name][
                    start : start + limit
                ],
                getEstimatedMatches=lambda: len(results[archive.name]),
            )
        ),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, archive_id, article_path, query: {
            "archive_id": archive_id,
            "article_path": article_path,
        },
    )

    result = server.search("query", "*", limit=3)

    assert result["mode"] == "auto"
    assert [item["article_path"] for item in result["results"]] == ["a", "b", "c"]
    assert [item["archive_id"] for item in result["results"]] == [
        "first.zim",
        "first.zim",
        "second.zim",
    ]
    assert result["estimated_matches"] == 4
    assert result["next_offset"] == 3


def test_read_article_caps_image_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = "".join(f'<img src="/{index}.png">' for index in range(31))
    item = SimpleNamespace(
        size=len(html), mimetype="text/html", content=html.encode(), title="Article"
    )
    entry = SimpleNamespace(
        is_redirect=False, path="article", title="Article", get_item=lambda: item
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: SimpleNamespace())
    monkeypatch.setattr(server, "_entry", lambda archive, article_path: entry)

    result = server.read_article("test.zim", "article", include_links=False)
    assert result["total_images"] == 31
    assert len(result["images"]) == server.MAX_IMAGE_REFERENCES
    assert result["images_truncated"] is True

    next_page = server.read_article(
        "test.zim", "article", include_links=False, image_offset=30
    )
    assert next_page["image_offset"] == 30
    assert len(next_page["images"]) == 1
    assert next_page["next_image_offset"] is None


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

    result = asyncio.run(
        server._call_tool_v2(
            None,
            SimpleNamespace(
                name="extract_image",
                arguments={
                    "archive_id": "test.zim",
                    "article_path": "article",
                    "image_path": "image.webp",
                },
            ),
        )
    )
    assert isinstance(result.content[0], TextContent)
    assert isinstance(result.content[1], ImageContent)
    assert result.content[1].model_dump(by_alias=True)["mimeType"] == "image/webp"
    assert next(tmp_path.iterdir()).read_bytes() == b"img"
    assert not old.exists()
    assert (
        server._TOOL_DEFINITIONS[-1].annotations.model_dump(by_alias=True)[
            "readOnlyHint"
        ]
        is False
    )
