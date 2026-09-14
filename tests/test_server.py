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


def _search_archive(**attributes):
    return SimpleNamespace(
        get_entry_by_path=lambda path: SimpleNamespace(path=path, is_redirect=False),
        **attributes,
    )


def test_empty_archive_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KIWIX_ARCHIVE_DIR", str(tmp_path))
    result = server.list_archives()
    assert result["status"] == "empty"
    assert result["archives"] == []


def test_archive_dir_resolution_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Explicit override always wins.
    monkeypatch.setenv("KIWIX_ARCHIVE_DIR", str(tmp_path))
    assert server._archive_dir() == tmp_path

    # Blank override counts as unset; legacy path wins only when it exists.
    monkeypatch.setenv("KIWIX_ARCHIVE_DIR", "   ")
    legacy = tmp_path / "legacy"
    monkeypatch.setattr(server, "LEGACY_ARCHIVE_DIR", legacy)
    monkeypatch.setattr(server, "DEFAULT_ARCHIVE_DIR", tmp_path / "default")
    assert server._archive_dir() == tmp_path / "default"
    legacy.mkdir()
    assert server._archive_dir() == legacy


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


def test_read_article_accepts_limit_compatibility_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = SimpleNamespace(
        size=2000, mimetype="text/plain", content=("x" * 2000).encode(), title="Article"
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
        "test.zim", "article", limit=1000, include_images=False, include_links=False
    )

    assert len(result["text"]) == 1000
    assert result["next_offset"] == 1000


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
    assert "archive_id from list_archives" in tools["search"].description
    assert "compatibility alias" in tools["read_article"].description
    assert "limit" in tools["read_article"].input_schema["properties"]
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


def test_links_recognize_see_also_despite_converter_markup_id() -> None:
    """zh.wikipedia emits ids like "扩-{展}-阅读"; match on id or heading text."""
    archive = SimpleNamespace(has_entry_by_path=lambda path: True)
    soup = server.BeautifulSoup(
        '<h2 id="扩-{展}-阅读">扩展阅读</h2>'
        '<ul><li><a href="extra">Extra</a></li></ul>'
        "<h2>参考文献</h2>",
        "html.parser",
    )
    see_also, _, _ = server._find_links(archive, soup, "article", "zh_all.zim")
    assert [item["article_path"] for item in see_also] == ["extra"]


def test_links_recognize_cankan_and_further_reading_labels() -> None:
    archive = SimpleNamespace(has_entry_by_path=lambda path: True)
    for heading, path, archive_id in [
        ('<h2 id="參看">參看</h2>', "quantum", "zh_all.zim"),
        ('<h2 id="参看">参看</h2>', "quantum", "zh_all.zim"),
        ('<h2 id="Further_reading">Further reading</h2>', "book", "en_all.zim"),
    ]:
        soup = server.BeautifulSoup(
            f'{heading}<ul><li><a href="{path}">X</a></li></ul><h2>References</h2>',
            "html.parser",
        )
        see_also, _, _ = server._find_links(archive, soup, "article", archive_id)
        assert [item["article_path"] for item in see_also] == [path], heading


def test_cross_archive_fulltext_survives_archive_without_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One index-less archive must not discard the other archives' results."""
    good = _search_archive(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
        name="good.zim",
    )
    bad = SimpleNamespace(has_fulltext_index=False, name="bad.zim")
    archives = {"good.zim": good, "bad.zim": bad}
    search_result = SimpleNamespace(
        getResults=lambda start, limit: ["a", "b"],
        getEstimatedMatches=lambda: 2,
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda _: [(n, Path(n)) for n in archives]
    )
    monkeypatch.setattr(server, "_archive", lambda path: archives[path.name])
    monkeypatch.setattr(
        server,
        "Searcher",
        lambda archive: SimpleNamespace(search=lambda query: search_result),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, name, path, query: {"article_path": path},
    )

    result = server.search("topic", "*", mode="fulltext", limit=5)

    assert result["status"] == "ok"
    assert [item["article_path"] for item in result["results"]] == ["a", "b"]
    assert [error["archive_id"] for error in result["errors"]] == ["bad.zim"]
    assert "does not support full-text search" in result["errors"][0]["error"]


def test_single_archive_fulltext_stays_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = SimpleNamespace(
        has_fulltext_index=False, name="only.zim", has_entry_by_title=lambda q: False
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda _: [("only.zim", Path("only.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda _: archive)

    with pytest.raises(ValueError, match="does not support full-text search"):
        server.search("topic", "only.zim", mode="fulltext")


def test_dispatch_coerces_string_scalars() -> None:
    assert server._coerce_arguments("extract_image", {"image_index": "3"}) == {
        "image_index": 3
    }
    assert server._coerce_arguments("read_article", {"include_links": "false"}) == {
        "include_links": False
    }
    assert server._coerce_arguments("read_article", {"image_offset": 2}) == {
        "image_offset": 2
    }
    with pytest.raises(ValueError, match="must be an integer"):
        server._coerce_arguments("extract_image", {"image_index": "abc"})
    with pytest.raises(ValueError, match="must be a boolean"):
        server._coerce_arguments("read_article", {"include_links": "maybe"})


def test_dispatch_rejects_booleans_for_integer_arguments() -> None:
    """bool is an int subclass; True must not silently become 1."""
    for tool, arguments in [
        ("search", {"limit": True}),
        ("extract_image", {"image_index": True}),
        ("read_article", {"offset": False}),
    ]:
        with pytest.raises(ValueError, match="must be an integer"):
            server._coerce_arguments(tool, arguments)


def test_dispatch_coerces_and_rejects_string_arguments() -> None:
    """Numbers are usable as strings; lists/objects/null must be named."""
    assert server._coerce_arguments("search", {"query": 2026}) == {"query": "2026"}
    for tool, arguments, bad_type in [
        ("search", {"archive_id": [1, 2]}, "list"),
        ("search", {"query": {"a": 1}}, "dict"),
        ("search", {"archive_id": None}, "NoneType"),
        ("read_article", {"section": [1]}, "list"),
    ]:
        with pytest.raises(ValueError) as excinfo:
            server._coerce_arguments(tool, arguments)
        message = str(excinfo.value)
        assert "must be a string" in message
        assert bad_type in message


def test_declared_type_reads_nullable_schema_types() -> None:
    assert server._declared_type({"type": ["string", "null"]}) == "string"
    assert server._declared_type({"type": ["null", "string"]}) == "string"
    assert server._declared_type({"type": "integer"}) == "integer"
    assert server._declared_type({}) == "string"


def test_reference_label_miss_lists_available_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (
        '<html><body><main><div class="mw-parser-output">'
        '<p>Claim<sup class="reference"><a href="#cite_note-a">'
        '<span class="mw-reflink-text">[1]</span></a></sup></p>'
        '<div class="mw-references-wrap"><ol class="references">'
        '<li id="cite_note-a"><span class="mw-cite-backlink">^</span>A</li>'
        "</ol></div></div></main></body></html>"
    )
    item = SimpleNamespace(
        size=len(html), mimetype="text/html", content=html.encode(), title="T"
    )
    entry = SimpleNamespace(
        is_redirect=False, path="T", title="T", get_item=lambda: item
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda _: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda _: SimpleNamespace())
    monkeypatch.setattr(server, "_entry", lambda archive, article_path: entry)

    miss = server.list_references("test.zim", "T", citation_label="999")
    assert miss["status"] == "no_hits"
    assert miss["matched_references"] == 0
    assert miss["available_citation_labels"] == ["1"]

    hit = server.list_references("test.zim", "T", citation_label="1")
    assert hit["status"] == "ok"
    assert hit["matched_references"] == 1
    assert hit["available_citation_labels"] == []

    unfiltered = server.list_references("test.zim", "T")
    assert unfiltered["available_citation_labels"] == []


def test_unknown_tool_lists_available_tools() -> None:
    with pytest.raises(ValueError) as excinfo:
        server._dispatch_tool("list_zims", {})
    message = str(excinfo.value)
    assert "Unknown tool" in message
    for name in (
        "list_archives",
        "search",
        "inspect_article",
        "read_article",
        "list_references",
        "extract_image",
    ):
        assert name in message


def test_dispatch_rejects_guessed_argument_names() -> None:
    """Agents guess parameter names; the error must name the valid ones."""
    for tool, bad in [
        ("search", {"path": "x"}),
        ("inspect_article", {"path": "x", "archive_id": "a.zim"}),
        ("read_article", {"path": "x"}),
    ]:
        with pytest.raises(ValueError) as excinfo:
            server._dispatch_tool(tool, bad)
        message = str(excinfo.value)
        assert "unknown argument(s): path" in message
        assert "Valid arguments:" in message
        assert "missing required argument(s)" in message


def test_dispatch_rejects_non_object_arguments() -> None:
    """A client sending a string/list/scalar must get one clear message."""
    for bad in ["query=x", '{"a": 1}', ["query"], 42, True]:
        with pytest.raises(ValueError) as excinfo:
            server._dispatch_tool("search", bad)
        message = str(excinfo.value)
        assert "expected a JSON object of named parameters" in message
        assert type(bad).__name__ in message
        assert "Valid arguments: archive_id" in message

    with pytest.raises(ValueError, match="takes no arguments"):
        server._dispatch_tool("list_archives", "unexpected")


def test_dispatch_treats_null_arguments_as_empty() -> None:
    with pytest.raises(ValueError) as excinfo:
        server._dispatch_tool("search", None)
    assert "missing required argument(s): archive_id, query" in str(excinfo.value)

    result = server._dispatch_tool("list_archives", None)
    assert result["status"] in {"ok", "empty"}


def test_dispatch_reports_missing_required_arguments() -> None:
    with pytest.raises(ValueError) as excinfo:
        server._dispatch_tool("search", {})
    assert "missing required argument(s): archive_id, query" in str(excinfo.value)


def test_dispatch_argument_specs_match_published_schemas() -> None:
    """The validation table is derived from the schemas, not hand-maintained."""
    for tool in server._TOOL_DEFINITIONS:
        allowed, required = server._TOOL_ARGUMENTS[tool.name]
        assert allowed == set(tool.input_schema.get("properties", {}))
        assert required == set(tool.input_schema.get("required", []))
        assert required <= allowed


def test_unknown_archive_error_lists_available_archives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("alpha.zim", "beta.zim"):
        (tmp_path / name).write_bytes(b"zim")
    monkeypatch.setenv("KIWIX_ARCHIVE_DIR", str(tmp_path))

    with pytest.raises(ValueError) as excinfo:
        server._select_paths("wikipedia_zh_all_maxi_2026_08_zim_main")

    message = str(excinfo.value)
    assert "Unknown archive" in message
    assert "list_archives" in message
    assert "alpha.zim" in message and "beta.zim" in message


def test_article_path_falls_back_to_title() -> None:
    """Agents pass "Machine learning"; the ZIM path is "Machine_learning"."""
    canonical = SimpleNamespace(path="Machine_learning", is_redirect=False)
    archive = SimpleNamespace(
        get_entry_by_path=lambda path: (_ for _ in ()).throw(
            KeyError("Cannot find entry")
        ),
        has_entry_by_title=lambda title: title == "Machine learning",
        get_entry_by_title=lambda title: canonical,
    )
    assert server._entry(archive, "Machine learning") is canonical


def _never_found_archive():
    return SimpleNamespace(
        get_entry_by_path=lambda path: (_ for _ in ()).throw(
            KeyError("Cannot find entry")
        ),
        has_entry_by_title=lambda title: False,
    )


def test_article_not_found_message_guides_and_suggests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server,
        "SuggestionSearcher",
        lambda _: SimpleNamespace(
            suggest=lambda query: SimpleNamespace(
                getResults=lambda start, limit: ["World_War_2"]
            )
        ),
    )
    with pytest.raises(ValueError) as excinfo:
        server._entry(_never_found_archive(), "world war 2")

    message = str(excinfo.value)
    assert "not the article title" in message
    assert "must not be URL-encoded" in message
    assert "Closest match: 'World_War_2'" in message


def test_article_not_found_message_without_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server,
        "SuggestionSearcher",
        lambda _: SimpleNamespace(
            suggest=lambda query: SimpleNamespace(getResults=lambda start, limit: [])
        ),
    )
    with pytest.raises(ValueError) as excinfo:
        server._entry(_never_found_archive(), "nonsense")
    assert "Closest match" not in str(excinfo.value)


def test_missing_article_path_errors_are_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = SimpleNamespace(
        has_entry_by_title=lambda title: False,
        has_entry_by_path=lambda path: False,
    )

    with pytest.raises(ValueError, match="article_path is required"):
        server._entry(archive, "")
    with pytest.raises(ValueError, match="article_path is required"):
        server._raw_entry(archive, "   ")

    def missing(path: str) -> object:
        raise KeyError("Cannot find entry")

    archive.get_entry_by_path = missing
    monkeypatch.setattr(
        server,
        "SuggestionSearcher",
        lambda _: SimpleNamespace(
            suggest=lambda q: SimpleNamespace(getResults=lambda a, b: [])
        ),
    )
    with pytest.raises(ValueError, match="Article not found"):
        server._entry(archive, "Nope")


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


def test_links_return_see_also_and_body_without_duplicates() -> None:
    archive = SimpleNamespace(has_entry_by_path=lambda path: True)
    soup = server.BeautifulSoup(
        '<p><a href="body">Body</a></p><h2 id="See_also">See also</h2>'
        '<ul><li><a href="related">Related</a></li></ul>'
        '<h2>References</h2><p><a href="body">Body again</a></p>',
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
    # Body links stay available for link-following traversal even when a
    # curated See also section exists.
    assert links == [
        {"article_path": "body", "title": "Body", "uri": "kiwix://en_all.zim/body"}
    ]
    assert not {item["article_path"] for item in see_also} & {
        item["article_path"] for item in links
    }
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


def test_article_uri_round_trip_keeps_leading_slash() -> None:
    """Real articles exist at paths like /etc/passwd; their uri must read back."""
    for article_path in [
        "/etc/passwd",
        "/dev/null",
        "/",
        "trailing/",
        "per%20cent",
        "quote'",
    ]:
        uri = server._article_uri("en_all.zim", article_path)
        archive_id, parsed_path = server._parse_article_uri(uri)
        assert archive_id == "en_all.zim"
        assert parsed_path == article_path, uri

        section_uri = server._section_uri("en_all.zim", article_path, "History")
        _, section_path = server._parse_article_uri(section_uri)
        assert section_path == article_path, section_uri
        assert server._parse_article_section(section_uri) == "History"


def test_resource_reads_article_at_leading_slash_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = b"<html><body><p>passwd file</p></body></html>"
    item = SimpleNamespace(size=len(html), mimetype="text/html", content=html)
    entry = SimpleNamespace(
        is_redirect=False,
        path="/etc/passwd",
        title="/etc/passwd",
        get_item=lambda: item,
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda _: [("en_all.zim", Path("en_all.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda _: SimpleNamespace())
    monkeypatch.setattr(server, "_entry", lambda archive, article_path: entry)

    uri = server._article_uri("en_all.zim", "/etc/passwd")
    result = server._read_article_resource(uri)
    assert result.meta["article_path"] == "/etc/passwd"
    assert "passwd file" in result.contents[0].text


def test_search_fulltext_mode_returns_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _search_archive(
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
    archive = _search_archive(
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
    first = _search_archive(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
        name="first",
        get_metadata=lambda key: b"en" if key == "Language" else b"",
    )
    second = _search_archive(
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
    first = _search_archive(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
        name="first",
        get_metadata=lambda key: b"eng" if key == "Language" else b"",
    )
    second = _search_archive(
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
    archive = _search_archive(
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
    archive = _search_archive(
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
    archive = _search_archive(
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


def test_search_pages_past_redirects_across_archives(monkeypatch) -> None:
    archives = {
        name: _search_archive(
            name=name,
            has_fulltext_index=True,
            has_entry_by_title=lambda query: False,
        )
        for name in ("first", "second")
    }
    archives["first"].get_entry_by_path = lambda path: SimpleNamespace(
        path="canonical" if path.startswith("alias") else path, is_redirect=False
    )
    paths = {
        "first": [f"alias-{i}" for i in range(70)] + ["tail-1", "tail-2"],
        "second": ["second-1", "second-2"],
    }
    reads = []

    def searcher(archive):
        def get_results(start, limit):
            reads.append((archive.name, start))
            return paths[archive.name][start : start + limit]

        return SimpleNamespace(
            search=lambda query: SimpleNamespace(
                getResults=get_results,
                getEstimatedMatches=lambda: len(paths[archive.name]),
            )
        )

    monkeypatch.setattr(
        server, "_select_paths", lambda _: [(name, Path(name)) for name in archives]
    )
    monkeypatch.setattr(server, "_archive", lambda path: archives[path.name])
    monkeypatch.setattr(server, "Searcher", searcher)
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, name, path, query: {
            "archive_id": name,
            "article_path": path,
        },
    )
    results = []
    offset = 0
    for _ in range(4):
        page = server.search("topic", "*", limit=2, offset=offset)
        results.extend(item["article_path"] for item in page["results"])
        offset = page["next_offset"]
        if offset is None:
            break
    assert offset is None
    assert results == ["canonical", "tail-1", "tail-2", "second-1", "second-2"]
    assert ("first", 64) in reads


def test_search_last_offset_does_not_repeat_pages(monkeypatch) -> None:
    archive = _search_archive(
        has_fulltext_index=True, has_entry_by_title=lambda _: False
    )
    paths = [str(i) for i in range(1100)]
    monkeypatch.setattr(server, "_select_paths", lambda _: [("test", Path("test"))])
    monkeypatch.setattr(server, "_archive", lambda _: archive)
    monkeypatch.setattr(
        server,
        "Searcher",
        lambda _: SimpleNamespace(
            search=lambda query: SimpleNamespace(
                getResults=lambda start, limit: paths[start : start + limit],
                getEstimatedMatches=lambda: len(paths),
            )
        ),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, name, path, query: {
            "article_path": path,
        },
    )
    page = server.search("topic", "test", offset=1000)
    assert [item["article_path"] for item in page["results"]] == ["1000"]
    assert page["next_offset"] is None


def test_search_can_aggregate_all_archives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _search_archive(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
        name="first",
    )
    second = _search_archive(
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
    second_page = server.search("query", "*", limit=2, offset=2)

    assert result["mode"] == "auto"
    assert [item["article_path"] for item in result["results"]] == ["a", "b", "c"]
    assert [item["archive_id"] for item in result["results"]] == [
        "first.zim",
        "first.zim",
        "second.zim",
    ]
    assert result["estimated_matches"] == 4
    assert result["next_offset"] == 3
    assert [item["article_path"] for item in second_page["results"]] == ["c", "d"]
    assert second_page["next_offset"] is None


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
        {"jsonrpc": "2.0", "id": 5, "method": "ping", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "list_archives", "arguments": {}},
        },
    ]
    env = {
        **os.environ,
        "KIWIX_ARCHIVE_DIR": str(tmp_path),
        "KIWIX_MCP_IDLE_TIMEOUT": "3",
    }
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
    assert responses[5]["result"] == {}
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
    archive = _search_archive(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: query == "图灵机",
        get_entry_by_title=lambda query: SimpleNamespace(path="A/图灵机"),
    )
    search_results = iter(
        [
            SimpleNamespace(
                getResults=lambda start, limit: [],
                getEstimatedMatches=lambda: 0,
            ),
            SimpleNamespace(
                getResults=lambda start, limit: ["A/图灵机", "A/related"],
                getEstimatedMatches=lambda: 2,
            ),
        ]
    )
    summary_queries = []
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("zh_all.zim", Path("zh_all.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: archive)
    monkeypatch.setattr(
        server,
        "Searcher",
        lambda archive: SimpleNamespace(search=lambda query: next(search_results)),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, archive_id, article_path, query: (
            summary_queries.append(query) or {"article_path": article_path}
        ),
    )

    result = server.search("圖靈機", "zh_all.zim")

    assert result["results"][0]["match_type"] == "exact_title"
    assert result["results"][0]["matched_query"] == "图灵机"
    assert result["estimated_matches"] == 2
    assert summary_queries == ["", "图灵机"]


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
    # Three-or-more-way collisions must stay excluded after later entries.
    assert "台" not in reverse
    assert server._query_variants("台湾") == ["台湾", "台灣"]
    assert server._query_variants("后台") == ["后台", "後台"]
    assert reverse.get("国") == "國"


def test_cross_archive_exact_title_outranks_fulltext_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-archive search: exact_title from any archive must rank above
    fulltext hits from other archives, regardless of archive order."""
    canonical = SimpleNamespace(path="Fainting_goat", is_redirect=False)
    alias = SimpleNamespace(
        path="Fainting_Goat",
        is_redirect=True,
        get_redirect_entry=lambda: canonical,
    )
    wiki_archive = _search_archive(
        has_fulltext_index=True,
        has_title_index=True,
        has_entry_by_title=lambda query: query == "Fainting goat",
        get_entry_by_title=lambda query: SimpleNamespace(path="A/Fainting_goat"),
    )
    wiki_archive.get_entry_by_path = lambda path: (
        alias if path == alias.path else canonical
    )
    wikibooks_archive = _search_archive(
        has_fulltext_index=True,
        has_title_index=False,
        has_entry_by_title=lambda query: False,
    )

    def select_paths(archive_id: str):
        return [
            ("wikibooks_en_all_maxi_2026-04.zim", Path("wikibooks.zim")),
            ("wikipedia_en_all_nopic_2026-06.zim", Path("wikipedia.zim")),
        ]

    noise_result = SimpleNamespace(
        getResults=lambda start, limit: [
            "Goats/Breeds",
            "Goats/Printable_version",
            "Goats/Introduction",
        ],
        getEstimatedMatches=lambda: 3,
    )
    wiki_result = SimpleNamespace(
        getResults=lambda start, limit: [canonical.path],
        getEstimatedMatches=lambda: 1,
    )
    suggest_result = SimpleNamespace(
        getResults=lambda start, limit: ["Fainting_Goat", "Fainting_goat_syndrome"],
        getEstimatedMatches=lambda: 2,
    )

    monkeypatch.setattr(server, "_select_paths", select_paths)
    monkeypatch.setattr(
        server,
        "_archive",
        lambda path: wiki_archive if "wikipedia" in str(path) else wikibooks_archive,
    )
    monkeypatch.setattr(
        server,
        "Searcher",
        lambda archive: SimpleNamespace(
            search=lambda query: (
                wiki_result if archive is wiki_archive else noise_result
            )
        ),
    )
    monkeypatch.setattr(
        server,
        "SuggestionSearcher",
        lambda archive: SimpleNamespace(suggest=lambda query: suggest_result),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, archive_id, article_path, query: {"article_path": article_path},
    )

    result = server.search("fainting goat", "*", limit=5)

    assert result["results"][0]["match_type"] == "exact_title"
    assert result["results"][0]["search_mode"] == "title"
    assert result["results"][0]["article_path"] == canonical.path
    assert [item["article_path"] for item in result["results"]].count(
        canonical.path
    ) == 1


def test_stdio_idle_timeout_exits_real_process(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "KIWIX_ARCHIVE_DIR": str(tmp_path),
        "KIWIX_MCP_IDLE_TIMEOUT": "1",
        "KIWIX_MCP_LOG_LEVEL": "INFO",
    }
    process = subprocess.Popen(
        [sys.executable, str(Path(server.__file__))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.wait(timeout=5) == 0
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        pytest.fail("stdio server did not exit after its idle timeout")
    stderr = process.stderr.read() if process.stderr is not None else ""
    assert "idle for 1s, shutting down" in stderr


def test_activity_resets_idle_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = SimpleNamespace(cancelled=False)
    previous.cancel = lambda: setattr(previous, "cancelled", True)
    replacement = object()
    loop = SimpleNamespace(
        call_later=lambda delay, callback: (
            replacement
            if (delay, callback) == (server._IDLE_TIMEOUT_S, server._exit_idle)
            else None
        )
    )
    monkeypatch.setattr(server, "_idle_timer", previous)
    monkeypatch.setattr(server.asyncio, "get_running_loop", lambda: loop)

    server._reset_idle_timer()

    assert previous.cancelled is True
    assert server._idle_timer is replacement


def test_inbound_frame_resets_idle_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    class ReadStream:
        async def receive(self):
            return "initialize"

    def reset() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(server, "_reset_idle_timer", reset)
    wrapped = server._ActivityReadStream(ReadStream())

    assert asyncio.run(wrapped.receive()) == "initialize"
    assert calls == 1
