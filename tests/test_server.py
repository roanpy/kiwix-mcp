import asyncio
import json
import os
from pathlib import Path
import select
import subprocess
import sys
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


def test_mediawiki_cleanup_keeps_article_text_and_removes_noise() -> None:
    html = b"""
    <main><div class="mw-parser-output">
      <table class="sidebar"><tr><td>Navigation noise</td></tr></table>
      <table class="infobox"><tr><th>Born</th><td>Visible fact</td></tr></table>
      <p>Useful lead <span style="display: none">hidden value</span>[1]</p>
      <div class="mw-heading"><h2 id="History">History</h2></div>
      <p>Useful body</p>
      <div class="mw-references-wrap"><ol class="references"><li>Long source</li></ol></div>
    </div></main>
    """

    text = server._plain_text(html, "text/html")

    assert text == "Useful lead [1]\nHistory\nUseful body"


def test_generic_stackexchange_cleanup_prefers_post_content() -> None:
    html = b"""
    <div id="mainbar">
      <div class="question">
        <div class="votecell">99</div>
        <div class="js-post-body"><p>Question body that should be read first.</p></div>
        <div class="post-taglist">navigation tag</div>
        <div class="post-signature">author metadata</div>
      </div>
      <h2>1 Answer</h2><div class="answer"><p>Useful answer.</p></div>
    </div>
    """

    text = server._plain_text(html, "text/html")

    assert text == "Question body that should be read first.\n1 Answer\nUseful answer."


def test_wikipedia_structure_sections_references_and_resource_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = """
    <html><head><link rel="canonical" href="https://en.wikipedia.org/wiki/Test"></head>
    <body><main><div class="mw-parser-output">
      <table class="infobox"><tr><th>Born</th><td><span hidden>machine</span>1900</td></tr></table>
      <p>Lead paragraph with enough useful text for article inspection and study.
        <sup class="reference"><a href="#cite_note-book-40"><span class="mw-reflink-text">[1]</span></a></sup>
      </p>
      <div class="mw-heading"><h2 id="First_section">First section</h2></div>
      <p>First section text.
        <sup class="reference"><a href="#cite_note-web-7"><span class="mw-reflink-text">[2]</span></a></sup>
      </p>
      <div class="mw-heading"><h3 id="Child">Child</h3></div><p>Child text.</p>
      <div class="mw-heading"><h2 id="Second">Second</h2></div><p>Must not leak.</p>
      <div class="mw-references-wrap"><ol class="references">
        <li id="cite_note-book-40"><span class="mw-cite-backlink">^</span>Book source</li>
        <li id="cite_note-web-7"><span class="mw-cite-backlink">^</span>
          <a class="external" href="https://example.com/source">Web source</a>
        </li>
      </ol></div>
    </div></main></body></html>
    """
    item = SimpleNamespace(
        size=len(html), mimetype="text/html", content=html.encode(), title="Test"
    )
    entry = SimpleNamespace(
        is_redirect=False, path="Test", title="Test", get_item=lambda: item
    )
    archive = SimpleNamespace(
        get_entry_by_path=lambda path: entry,
        get_metadata=lambda key: {"Language": b"eng", "Date": b"2026-01-01"}[key],
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: archive)

    inspected = server.inspect_article("test.zim", "Test")
    assert inspected["profile"] == "mediawiki"
    assert inspected["lead"].startswith("Lead paragraph")
    assert inspected["facts"] == [{"name": "Born", "value": "1900"}]
    assert inspected["total_sections"] == 3
    assert inspected["outline"][0]["uri"].endswith("#First_section")
    assert inspected["total_references"] == 2
    assert inspected["canonical_url"] == "https://en.wikipedia.org/wiki/Test"
    assert inspected["language"] == "eng"

    article = server.read_article(
        "test.zim",
        "Test",
        section="First section",
        include_images=False,
        include_links=False,
    )
    assert article["section"]["anchor"] == "First_section"
    assert "First section text" in article["text"]
    assert "Child text" in article["text"]
    assert "Must not leak" not in article["text"]

    references = server.list_references("test.zim", "Test", citation_label="2", limit=1)
    assert references["total_references"] == 2
    assert references["matched_references"] == 1
    assert references["references"][0]["citation_label"] == "2"
    assert references["references"][0]["links"] == [
        {"title": "Web source", "url": "https://example.com/source"}
    ]

    resource = server._read_article_resource("kiwix://test.zim/Test#First_section")
    assert "First section text" in resource.contents[0].text
    assert "Must not leak" not in resource.contents[0].text
    assert resource.meta["section"]["anchor"] == "First_section"


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
        "inspect_article",
        "read_article",
        "list_references",
        "extract_image",
    ]
    tools = {tool.name: tool for tool in result.tools}
    assert tools["list_archives"].output_schema is not None
    assert (
        "flavour"
        in tools["list_archives"].output_schema["properties"]["archives"]["items"][
            "properties"
        ]
    )
    archive_output = tools["list_archives"].output_schema["properties"]["archives"][
        "items"
    ]["properties"]
    assert {"size_bytes", "article_count", "language", "error"} <= archive_output.keys()
    assert (
        "match_type"
        in tools["search"].output_schema["properties"]["results"]["items"]["properties"]
    )
    assert "mode" in tools["search"].input_schema["properties"]
    assert "outline" in tools["inspect_article"].output_schema["properties"]
    assert "section" in tools["read_article"].input_schema["properties"]
    assert "offset" in tools["read_article"].input_schema["properties"]
    assert "image_offset" in tools["read_article"].input_schema["properties"]
    article_output = tools["read_article"].output_schema["properties"]
    assert "mimetype" in article_output
    assert "images" in article_output
    assert "is_main" not in article_output["images"]["items"]["properties"]
    assert "see_also" in article_output
    assert "links" in article_output
    assert "citation_label" in tools["list_references"].input_schema["properties"]
    assert "image_path" in tools["extract_image"].input_schema["properties"]


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


def test_read_resource_does_not_substitute_missing_article_with_main_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("first.zim", Path("first.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: SimpleNamespace())
    monkeypatch.setattr(
        server,
        "_entry",
        lambda archive, article_path: (_ for _ in ()).throw(
            ValueError(f"Cannot find entry: {article_path}")
        ),
    )

    with pytest.raises(ValueError, match="mainPage"):
        asyncio.run(
            server._read_resource_v2(
                None,
                SimpleNamespace(uri="kiwix://first.zim/mainPage"),
            )
        )


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
        "primary": True,
    }


def test_links_prefer_see_also_and_fallback_to_body() -> None:
    archive = SimpleNamespace(has_entry_by_path=lambda path: True)
    soup = server.BeautifulSoup(
        '<p><a href="body">Body</a></p><h2 id="See_also">See also</h2>'
        '<ul><li><a href="related">Related</a></li></ul><h2>References</h2>',
        "html.parser",
    )
    see_also, links, notes = server._find_links(archive, soup, "article", "en_all.zim")
    assert see_also == [
        {
            "article_path": "related",
            "title": "Related",
            "uri": "kiwix://en_all.zim/related",
        }
    ]
    assert links == []
    assert notes == []

    see_also, links, notes = server._find_links(
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


def test_article_uri_round_trip_special_chars() -> None:
    for archive_id, article_path in [
        ("zh_all.zim", "Wikipedia:首页"),
        ("en_all.zim", "Topic/子 标题"),
        ("a:b.zim", "path/with spaces"),
    ]:
        uri = server._article_uri(archive_id, article_path)
        parsed_id, parsed_path = server._parse_article_uri(uri)
        assert parsed_id == archive_id
        assert parsed_path == article_path


def test_search_fulltext_mode_returns_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = SimpleNamespace(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
    )
    search_result = SimpleNamespace(
        getResults=lambda start, limit: ["a", "b"],
        getEstimatedMatches=lambda: 2,
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

    result = server.search("query", "test.zim", mode="fulltext")
    assert result["mode"] == "fulltext"
    assert [item["match_type"] for item in result["results"]] == [
        "fulltext",
        "fulltext",
    ]


def test_search_title_mode_forces_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = SimpleNamespace(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
    )
    suggestion_result = SimpleNamespace(
        getResults=lambda start, limit: ["suggested"],
        getEstimatedMatches=lambda: 1,
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: archive)
    monkeypatch.setattr(
        server,
        "SuggestionSearcher",
        lambda archive: SimpleNamespace(suggest=lambda query: suggestion_result),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, archive_id, article_path, query: {"article_path": article_path},
    )

    result = server.search("query", "test.zim", mode="title")
    assert result["mode"] == "title"
    assert result["results"][0]["match_type"] == "title"


def test_search_cross_archive_with_fulltext_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = SimpleNamespace(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
        name="first",
        get_metadata=lambda key: b"en" if key == "Language" else b"",
    )
    second = SimpleNamespace(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
        name="second",
        get_metadata=lambda key: b"zh" if key == "Language" else b"",
    )
    results = {"first": ["a"], "second": ["c"]}
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

    result = server.search("query", "*", mode="fulltext", limit=2)
    assert result["mode"] == "fulltext"
    assert [item["article_path"] for item in result["results"]] == ["a", "c"]


def test_search_cross_archive_filters_by_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = SimpleNamespace(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
        name="first",
        get_metadata=lambda key: b"eng" if key == "Language" else b"",
    )
    second = SimpleNamespace(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
        name="second",
        get_metadata=lambda key: b"zho" if key == "Language" else b"",
    )
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
                getResults=lambda start, limit: [archive.name],
                getEstimatedMatches=lambda: 1,
            )
        ),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, archive_id, article_path, query: {"archive_id": archive_id},
    )

    # two-letter alias "zh" maps to ZIM's "zho"
    result = server.search("query", "*", language="zh")
    assert [item["archive_id"] for item in result["results"]] == ["second.zim"]
    assert result["filters"]["language"] == "zh"

    # three-letter code matches directly
    result = server.search("query", "*", language="eng")
    assert [item["archive_id"] for item in result["results"]] == ["first.zim"]

    # unknown language matches nothing
    result = server.search("query", "*", language="xx")
    assert result["results"] == []


def test_search_language_filter_strips_metadata_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = SimpleNamespace(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
        get_metadata=lambda key: b"eng, zho" if key == "Language" else b"",
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: archive)
    monkeypatch.setattr(
        server,
        "Searcher",
        lambda archive: SimpleNamespace(
            search=lambda query: SimpleNamespace(
                getResults=lambda start, limit: ["article"],
                getEstimatedMatches=lambda: 1,
            )
        ),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, archive_id, article_path, query: {"article_path": article_path},
    )

    assert server.search("query", "*", language="zh")["results"]


def test_search_single_archive_ignores_language_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = SimpleNamespace(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
        get_metadata=lambda key: b"en" if key == "Language" else b"",
    )
    search_result = SimpleNamespace(
        getResults=lambda start, limit: ["a"],
        getEstimatedMatches=lambda: 1,
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

    result = server.search("query", "test.zim", language="zh")
    assert len(result["results"]) == 1


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
    assert (
        next(path for path in tmp_path.iterdir() if path.name != ".lock").read_bytes()
        == b"img"
    )
    assert not old.exists()
    assert (
        server._TOOL_DEFINITIONS[-1].annotations.model_dump(by_alias=True)[
            "readOnlyHint"
        ]
        is False
    )


def test_links_recognize_extended_see_also_labels() -> None:
    archive = SimpleNamespace(has_entry_by_path=lambda path: True)
    soup = server.BeautifulSoup(
        '<h2 id="延伸阅读">延伸阅读</h2>'
        '<ul><li><a href="extra">Extra</a></li></ul>'
        "<h2>References</h2>",
        "html.parser",
    )
    see_also, links, _ = server._find_links(archive, soup, "article", "zh_all.zim")
    assert see_also == [
        {
            "article_path": "extra",
            "title": "Extra",
            "uri": "kiwix://zh_all.zim/extra",
        }
    ]
    assert links == []


def test_links_do_not_treat_references_as_see_also() -> None:
    archive = SimpleNamespace(has_entry_by_path=lambda path: True)
    soup = server.BeautifulSoup(
        '<h2 id="参考文献">参考文献</h2><a href="source">Source</a>',
        "html.parser",
    )

    see_also, links, _ = server._find_links(archive, soup, "article", "zh_all.zim")

    assert see_also == []
    assert links[0]["article_path"] == "source"


def test_read_resource_truncates_long_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_text = "x" * (server.MAX_RESOURCE_CHARS + 100)
    item = SimpleNamespace(
        size=len(long_text),
        mimetype="text/plain",
        content=long_text.encode(),
    )
    entry = SimpleNamespace(
        is_redirect=False, path="article", title="Article", get_item=lambda: item
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: SimpleNamespace())
    monkeypatch.setattr(server, "_entry", lambda archive, article_path: entry)

    result = asyncio.run(
        server._read_resource_v2(
            None,
            SimpleNamespace(uri="kiwix://test.zim/article"),
        )
    )
    assert result.meta["truncated"] is True
    assert result.meta["total_chars"] == len(long_text)
    assert result.meta["next_offset_hint"] == server.MAX_RESOURCE_CHARS
    assert len(result.contents[0].text) == server.MAX_RESOURCE_CHARS
    assert result.contents[0].meta["truncated"] is True


def test_list_resources_skips_archive_with_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_archive_paths",
        lambda: {"bad.zim": Path("bad.zim"), "good.zim": Path("good.zim")},
    )
    good_archive = SimpleNamespace(
        has_main_entry=True,
        main_entry=SimpleNamespace(path="main", title="Main"),
    )

    def _archive(path: Path) -> object:
        if path.name == "bad.zim":
            raise OSError("cannot open")
        return good_archive

    monkeypatch.setattr(server, "_archive", _archive)
    result = asyncio.run(server._list_resources_v2(None, None))
    names = [r.name for r in result.resources]
    assert "good.zim-main" in names
    assert "bad.zim-main" not in names


def test_list_resources_skips_when_main_entry_path_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_archive_paths",
        lambda: {"first.zim": Path("first.zim")},
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
                get_redirect_entry=lambda: (_ for _ in ()).throw(
                    RuntimeError("broken")
                ),
            ),
        ),
    )
    result = asyncio.run(server._list_resources_v2(None, None))
    assert result.resources == []


def test_stdio_protocol_round_trip(tmp_path: Path) -> None:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "list_archives", "arguments": {}},
        },
    ]
    env = {**os.environ, "KIWIX_ARCHIVE_DIR": str(tmp_path)}
    process = subprocess.Popen(
        [sys.executable, str(Path(server.__file__))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    responses = {}
    for message in messages:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        if "id" in message:
            assert select.select([process.stdout], [], [], 15)[0]
            response = json.loads(process.stdout.readline())
            responses[response["id"]] = response
    process.stdin.close()
    try:
        returncode = process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    stderr = process.stderr.read() if process.stderr is not None else ""
    assert process.stdout.read() == ""
    assert returncode == 0, stderr

    assert responses[1]["result"]["protocolVersion"] == "2025-11-25"
    assert [tool["name"] for tool in responses[2]["result"]["tools"]] == [
        "list_archives",
        "search",
        "inspect_article",
        "read_article",
        "list_references",
        "extract_image",
    ]
    assert responses[3]["result"]["resources"] == []
    assert responses[4]["result"]["isError"] is False


def test_image_cache_is_safe_across_processes(tmp_path: Path) -> None:
    code = """
import sys
from pathlib import Path
import server
server.IMAGE_TEMP_DIR = Path(sys.argv[1])
for index in range(20):
    server._cache_image(f"{sys.argv[2]}-{index}".encode(), "image/png")
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path), str(index)],
            cwd=Path(server.__file__).parent,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for index in range(12)
    ]
    results = [process.communicate(timeout=30) for process in processes]

    assert all(process.returncode == 0 for process in processes), "\n".join(
        stderr for _, stderr in results if stderr
    )
    assert (
        len([path for path in tmp_path.iterdir() if path.name != ".lock"])
        <= server.MAX_TEMP_IMAGES
    )


def test_hatnotes_are_removed_from_text_and_lead() -> None:
    html = b"""
    <main><div class="mw-parser-output">
      <div class="hatnote">"Turing" redirects here. For other uses, see
        <a href="A/Turing_(disambiguation)">Turing (disambiguation)</a>.</div>
      <p>Alan Mathison Turing was an English mathematician, computer
        scientist, logician, cryptanalyst, philosopher and theoretical
        biologist who formalised computation.</p>
    </div></main>
    """
    soup = server.BeautifulSoup(html, "html.parser")

    text = server._plain_text(html, "text/html")
    assert "redirects here" not in text
    assert text.startswith("Alan Mathison Turing")

    lead = server._article_lead(soup)
    assert "redirects here" not in lead
    assert lead.startswith("Alan Mathison Turing")


def test_hatnote_links_are_exposed_as_notes() -> None:
    archive = SimpleNamespace(has_entry_by_path=lambda path: True)
    soup = server.BeautifulSoup(
        '<div class="mw-parser-output">'
        '<div class="hatnote">主条目：<a href="./图灵测试">图灵测试</a></div>'
        "<p>Body</p></div>",
        "html.parser",
    )
    _, _, notes = server._find_links(archive, soup, "A/图灵", "zh_all.zim")
    assert len(notes) == 1
    assert notes[0]["label"].startswith("主条目")
    assert notes[0]["links"][0]["article_path"] == "A/图灵测试"


def test_find_images_dedupes_and_drops_tiny_icons() -> None:
    soup = server.BeautifulSoup(
        '<img src="header.jpg" width="250" height="200">'
        '<img src="flag.png" width="22" height="22">'
        '<img src="flag.png" width="22" height="22">'
        '<img src="lock.png" width="9" height="9" alt="freely accessible">'
        '<img src="header.jpg" width="250" height="200">'
        '<img src="statue.jpg" width="200" height="150">',
        "html.parser",
    )
    images = server._find_images(soup, "A/Topic")
    assert [img["filename"] for img in images] == ["header.jpg", "statue.jpg"]
    assert images[0]["primary"] is True
    assert images[1]["primary"] is False
    assert [img["index"] for img in images] == [0, 1]


def test_search_traditional_query_matches_simplified_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = SimpleNamespace(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: query == "图灵机",
        get_entry_by_title=lambda query: SimpleNamespace(path="A/图灵机"),
    )
    search_result = SimpleNamespace(
        getResults=lambda start, limit: ["A/图灵机"],
        getEstimatedMatches=lambda: 1,
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("zh_all.zim", Path("zh_all.zim"))]
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

    result = server.search("圖靈機", "zh_all.zim")

    assert result["results"][0]["match_type"] == "exact_title"
    assert result["results"][0]["matched_query"] == "图灵机"


def test_query_variants_only_differ_for_traditional() -> None:
    assert server._query_variants("圖靈") == ["圖靈", "图灵"]
    assert server._query_variants("艾伦·图灵") == ["艾伦·图灵", "艾倫·圖靈"]
    assert server._query_variants("1518年法国舞蹈瘟疫") == [
        "1518年法国舞蹈瘟疫",
        "1518年法國舞蹈瘟疫",
    ]
    assert server._query_variants("Alan Turing") == ["Alan Turing"]


def test_reverse_map_drops_ambiguous_simplified_chars() -> None:
    reverse = server._simplified_to_traditional()
    # 發 and 髮 both map to 发, so 发 must not reverse-map at all
    assert "发" not in reverse
    assert reverse.get("国") == "國"
