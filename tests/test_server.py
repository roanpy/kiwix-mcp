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
import zh_variants


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


def _script_page_archive(text: str) -> SimpleNamespace:
    """An HTML entry whose body carries no text, like a PhET simulation shell."""
    html = text.encode()
    item = SimpleNamespace(
        size=len(html), mimetype="text/html", content=html, title="Simulation"
    )
    entry = SimpleNamespace(
        is_redirect=False, path="sim.html", title="光的折射", get_item=lambda: item
    )
    return SimpleNamespace(), entry


def test_script_driven_page_gets_an_explanatory_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank extraction must explain itself rather than look like a bug."""
    # Mirrors a real PhET shell: a <title>, an empty body, and only scripts.
    archive, entry = _script_page_archive(
        "<html><head><title>光的折射</title></head>"
        '<body><script src="app.js"></script></body></html>'
    )
    monkeypatch.setattr(server, "_select_paths", lambda _: [("t.zim", Path("t.zim"))])
    monkeypatch.setattr(server, "_archive", lambda _: archive)
    monkeypatch.setattr(server, "_entry", lambda a, p: entry)
    monkeypatch.setattr(server, "_raw_entry", lambda a, p: entry)

    inspected = server.inspect_article("t.zim", "sim.html")
    assert inspected["total_chars"] == 0
    assert inspected["note"] == server.NO_TEXT_NOTE

    read = server.read_article(
        "t.zim", "sim.html", include_images=False, include_links=False
    )
    assert read["text"] == ""
    assert read["note"] == server.NO_TEXT_NOTE

    shell = server.read_article("t.zim", "sim.html")
    assert shell["text"] == ""
    assert shell["note"] == server.NO_TEXT_NOTE
    assert shell["total_images"] == 0
    assert shell["see_also"] == []
    assert shell["links"] == []
    assert shell["returned_links"] == 0

    # A page with real text must not carry the note.
    archive2, entry2 = _script_page_archive(
        "<html><body><p>Real text here</p></body></html>"
    )
    monkeypatch.setattr(server, "_archive", lambda _: archive2)
    monkeypatch.setattr(server, "_entry", lambda a, p: entry2)
    monkeypatch.setattr(server, "_raw_entry", lambda a, p: entry2)
    assert "note" not in server.inspect_article("t.zim", "sim.html")


def test_max_chars_is_not_overridden_by_limit_when_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit max_chars equal to the default must still win over limit."""
    text = "x" * 20000
    item = SimpleNamespace(
        size=len(text), mimetype="text/plain", content=text.encode(), title="T"
    )
    entry = SimpleNamespace(
        is_redirect=False, path="T", title="T", get_item=lambda: item
    )
    monkeypatch.setattr(server, "_select_paths", lambda _: [("t.zim", Path("t.zim"))])
    monkeypatch.setattr(server, "_archive", lambda _: SimpleNamespace())
    monkeypatch.setattr(server, "_entry", lambda archive, article_path: entry)

    def chars(**kwargs) -> int:
        return len(
            server.read_article(
                "t.zim", "T", include_images=False, include_links=False, **kwargs
            )["text"]
        )

    assert chars() == server.DEFAULT_MAX_CHARS
    assert chars(limit=1500) == 1500
    assert chars(max_chars=1500) == 1500
    # The regression: an explicit max_chars equal to the default used to lose.
    assert chars(max_chars=server.DEFAULT_MAX_CHARS, limit=1500) == 12000
    assert chars(max_chars=1500, limit=9000) == 1500


def test_oversized_article_recovery_matches_the_real_limit() -> None:
    with pytest.raises(ValueError) as excinfo:
        server._too_large(server.MAX_ARTICLE_BYTES + 1)
    message = str(excinfo.value)
    assert f"{server.MAX_ARTICLE_BYTES}-byte source limit" in message
    assert "No article-reading tool can open it" in message
    assert "max_chars reduces returned text only" in message
    assert "Call inspect_article for its outline" not in message


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


def test_read_article_offset_at_end_explains_empty_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "x" * 2000
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
        offset=len(text),
        include_images=False,
        include_links=False,
    )

    assert result["text"] == ""
    assert result["offset"] == len(text)
    assert "at or beyond the end" in result["note"]


def test_reference_offset_at_end_explains_total(
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

    result = server.list_references("test.zim", "T", offset=5)

    assert result["status"] == "no_hits"
    assert result["total_references"] == 1
    assert result["requested_offset"] == 5
    assert result["offset"] == 1
    assert "at or beyond the last page" in result["guidance"]


def test_archive_paths_lists_split_first_part_and_ignores_continuations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KIWIX_ARCHIVE_DIR", str(tmp_path))
    (tmp_path / "wiki.zim").write_bytes(b"x")
    for name, data in {
        "split.zimaa": b"a",
        "split.zimab": b"bb",
        "split.zimaz": b"zzz",
        "split.zimba": b"bbbb",
        "split.zimbb": b"bbbbb",
        "weird[1].zimaa": b"w" * 6,
        "weird[1].zimab": b"w" * 7,
    }.items():
        (tmp_path / name).write_bytes(data)
    (tmp_path / "split.zimaa.bak").write_bytes(b"x" * 10)
    (tmp_path / "split.zimZZ").write_bytes(b"x" * 10)
    (tmp_path / "split.zimac").mkdir()
    (tmp_path / "orphan.zimab").write_bytes(b"z")

    paths = server._archive_paths()

    assert set(paths) == {"wiki.zim", "split.zimaa", "weird[1].zimaa"}
    # aa..az plus ba/bb, but not .bak, uppercase, directories, or unrelated files.
    assert server._archive_size(paths["split.zimaa"]) == 15
    assert server._archive_size(paths["weird[1].zimaa"]) == 13
    assert server._select_paths("split.zim") == [
        ("split.zimaa", tmp_path / "split.zimaa")
    ]
    assert server._select_paths("weird[1].zim") == [
        ("weird[1].zimaa", tmp_path / "weird[1].zimaa")
    ]


@pytest.mark.parametrize("failure_at", ["open", "size"])
def test_list_archives_surfaces_mixed_archive_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_at: str
) -> None:
    monkeypatch.setenv("KIWIX_ARCHIVE_DIR", str(tmp_path))
    (tmp_path / "good.zim").write_bytes(b"good")
    (tmp_path / "bad.zim").write_bytes(b"bad")
    good = SimpleNamespace(
        article_count=1,
        media_count=2,
        has_fulltext_index=True,
        has_title_index=True,
    )

    def open_archive(path: Path):
        if path.name == "bad.zim" and failure_at == "open":
            raise RuntimeError("corrupt archive")
        return good

    original_size = server._archive_size

    def archive_size(path: Path):
        if path.name == "bad.zim" and failure_at == "size":
            raise OSError("corrupt archive size")
        return original_size(path)

    monkeypatch.setattr(server, "_archive", open_archive)
    monkeypatch.setattr(server, "_archive_size", archive_size)

    result = server.list_archives()

    assert result["status"] == "ok"
    by_name = {item["archive_id"]: item for item in result["archives"]}
    assert by_name["good.zim"]["article_count"] == 1
    assert "error" not in by_name["good.zim"]
    assert "corrupt archive" in by_name["bad.zim"]["error"]


def test_search_auto_falls_back_to_title_without_fulltext_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _search_archive(
        has_fulltext_index=False,
        has_title_index=True,
        has_entry_by_title=lambda query: False,
    )
    suggestion = SimpleNamespace(
        getResults=lambda start, limit: ["Topic"],
        getEstimatedMatches=lambda: 1,
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda _: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda _: archive)
    monkeypatch.setattr(
        server,
        "SuggestionSearcher",
        lambda _: SimpleNamespace(suggest=lambda query: suggestion),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, name, path, query: {"article_path": path},
    )

    result = server.search("topic", "test.zim", mode="auto")

    assert result["status"] == "ok"
    assert result["mode"] == "auto"
    assert result["results"][0]["search_mode"] == "title"
    assert result["results"][0]["article_path"] == "Topic"


def test_search_without_xapian_indexes_uses_native_title_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _search_archive(
        has_fulltext_index=False,
        has_title_index=False,
        has_main_entry=True,
        main_entry=SimpleNamespace(path="main", title="Main", is_redirect=False),
        has_entry_by_title=lambda query: False,
    )
    native = SimpleNamespace(
        getResults=lambda start, limit: ["Target", "Target-2", "Target-3"],
        getEstimatedMatches=lambda: 3,
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda _: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda _: archive)
    monkeypatch.setattr(
        server,
        "SuggestionSearcher",
        lambda _: SimpleNamespace(suggest=lambda query: native),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, name, path, query: {"article_path": path},
    )

    result = server.search("Target", "test.zim", mode="auto")

    assert result["status"] == "ok"
    assert result["errors"] == []
    assert [item["article_path"] for item in result["results"]] == [
        "Target",
        "Target-2",
        "Target-3",
    ]
    assert all(item["search_mode"] == "title" for item in result["results"])


def test_search_without_xapian_indexes_still_uses_exact_title_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _search_archive(
        has_fulltext_index=False,
        has_title_index=False,
        has_entry_by_title=lambda query: query == "Target",
        get_entry_by_title=lambda query: SimpleNamespace(path="Target"),
    )
    empty = SimpleNamespace(
        getResults=lambda start, limit: [],
        getEstimatedMatches=lambda: 0,
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda _: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda _: archive)
    monkeypatch.setattr(
        server,
        "SuggestionSearcher",
        lambda _: SimpleNamespace(suggest=lambda query: empty),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, name, path, query: {"article_path": path},
    )

    result = server.search("Target", "test.zim", mode="auto")

    assert result["status"] == "ok"
    assert result["results"][0]["match_type"] == "exact_title"
    assert result["results"][0]["article_path"] == "Target"


@pytest.mark.parametrize("failure_at", ["suggest", "estimate", "results"])
def test_search_native_title_failure_gets_main_entry_hint(
    monkeypatch: pytest.MonkeyPatch, failure_at: str
) -> None:
    archive = _search_archive(
        has_fulltext_index=False,
        has_title_index=False,
        has_main_entry=True,
        main_entry=SimpleNamespace(path="main", title="Main", is_redirect=False),
        has_entry_by_title=lambda query: False,
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda _: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda _: archive)

    def fail(*_args):
        raise RuntimeError("native lookup failed")

    native = SimpleNamespace(
        getEstimatedMatches=fail if failure_at == "estimate" else lambda: 1,
        getResults=fail if failure_at == "results" else lambda *_: [],
    )
    monkeypatch.setattr(
        server,
        "SuggestionSearcher",
        lambda _: SimpleNamespace(
            suggest=fail if failure_at == "suggest" else lambda _: native
        ),
    )

    result = server.search("topic", "test.zim", mode="auto")

    assert result["status"] == "no_hits"
    assert result["errors"]
    assert "Native title search failed" in result["errors"][0]["error"]
    assert "native lookup failed" in result["errors"][0]["error"]
    assert "main_entry_path 'main'" in result["errors"][0]["error"]
    assert "Inspect errors" in result["guidance"]


def test_search_fulltext_mode_is_not_silently_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _search_archive(
        has_fulltext_index=False,
        has_title_index=True,
        has_entry_by_title=lambda query: False,
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda _: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda _: archive)

    with pytest.raises(ValueError, match="does not support full-text search"):
        server.search("topic", "test.zim", mode="fulltext")


def test_read_article_plain_text_has_no_link_or_image_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _read_text_fixture(monkeypatch, "Plain text body without markup.")

    result = server.read_article("test.zim", "article")

    assert result["text"] == "Plain text body without markup."
    assert result["total_images"] == 0
    assert result["images"] == []
    assert result["see_also"] == []
    assert result["links"] == []
    assert result["returned_links"] == 0
    assert result["total_links"] == 0
    assert result["links_truncated"] is False


def test_read_article_rejects_binary_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = SimpleNamespace(
        size=8, mimetype="application/octet-stream", content=b"\x00\x01binary"
    )
    entry = SimpleNamespace(
        is_redirect=False, path="article", title="Article", get_item=lambda: item
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda _: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda _: SimpleNamespace())
    monkeypatch.setattr(server, "_entry", lambda _archive, _path: entry)

    with pytest.raises(ValueError, match="not text"):
        server.read_article("test.zim", "article")


def test_read_article_generic_html_link_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [f"page-{index}" for index in range(30)]
    html = (
        "<main>" + "".join(f'<a href="{path}">{path}</a>' for path in paths) + "</main>"
    )
    _read_html_fixture(monkeypatch, html, set(paths))

    first = server.read_article("test.zim", "article", include_images=False)
    second = server.read_article(
        "test.zim", "article", include_images=False, link_offset=25
    )

    assert [item["article_path"] for item in first["links"]] == paths[:25]
    assert first["next_link_offset"] == 25
    assert [item["article_path"] for item in second["links"]] == paths[25:]
    assert second["next_link_offset"] is None
    assert second["links_truncated"] is False


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
    assert "link_offset" in tools["read_article"].input_schema["properties"]
    article_output = tools["read_article"].output_schema["properties"]
    assert "mimetype" in article_output
    assert "images" in article_output
    assert "is_main" not in article_output["images"]["items"]["properties"]
    assert "see_also" in article_output
    assert "links" in article_output
    assert {
        "query_found",
        "query_match_offset",
        "requested_offset",
        "link_offset",
        "next_link_offset",
        "links_truncated",
        "returned_links",
        "links_window_exhausted",
    } <= article_output.keys()
    assert "full-article link count" in article_output["total_links"]["description"]
    assert "never above 1000" in article_output["next_link_offset"]["description"]
    assert "whole article" in article_output["links"]["description"]
    assert "at or beyond the end" in article_output["note"]["description"]
    search_output = tools["search"].output_schema["properties"]
    assert {"requested_offset", "window_truncated"} <= search_output.keys()
    reference_output = tools["list_references"].output_schema["properties"]
    assert "requested_offset" in reference_output
    assert "citation_label" in tools["list_references"].input_schema["properties"]
    assert "image_path" in tools["extract_image"].input_schema["properties"]


def test_packaging_metadata_includes_runtime_modules_and_notices() -> None:
    import tomllib

    root = Path(__file__).resolve().parent.parent
    metadata = tomllib.loads((root / "pyproject.toml").read_text())

    assert metadata["build-system"]["build-backend"] == "setuptools.build_meta"
    assert set(metadata["tool"]["setuptools"]["py-modules"]) == {
        "server",
        "zh_variants",
    }
    assert set(metadata["project"]["license-files"]) >= {
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "LICENSES/*",
    }
    for relative in (
        "LICENSE",
        "MANIFEST.in",
        "THIRD_PARTY_NOTICES.md",
        "LICENSES/Apache-2.0.txt",
    ):
        assert (root / relative).is_file()


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
    assert result["partial"] is True
    assert "Results are partial" in result["guidance"]


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


def test_mcp_handler_does_not_swallow_false_like_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIWIX_ARCHIVE_DIR", "/nonexistent-test-directory")
    for bad in (False, 0, "", []):
        result = asyncio.run(
            server._call_tool_v2(
                None, SimpleNamespace(name="list_archives", arguments=bad)
            )
        )
        assert result.is_error is True
        assert "expected a JSON object of named parameters" in result.content[0].text

    empty = asyncio.run(
        server._call_tool_v2(None, SimpleNamespace(name="list_archives", arguments={}))
    )
    assert empty.is_error is not True

    omitted = asyncio.run(
        server._call_tool_v2(
            None, SimpleNamespace(name="list_archives", arguments=None)
        )
    )
    assert omitted.is_error is not True


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


def test_tool_input_schemas_reject_unknown_properties() -> None:
    for tool in server._TOOL_DEFINITIONS:
        assert tool.input_schema["additionalProperties"] is False


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


def test_article_path_title_fallback_normalizes_whitespace() -> None:
    canonical = SimpleNamespace(path="Machine_learning", is_redirect=False)
    looked_up: list[str] = []

    def has_title(title: str) -> bool:
        looked_up.append(title)
        return title == "Machine learning"

    archive = SimpleNamespace(
        get_entry_by_path=lambda path: (_ for _ in ()).throw(KeyError(path)),
        has_entry_by_title=has_title,
        get_entry_by_title=lambda title: canonical,
    )

    assert server._entry(archive, "  Machine___learning  ") is canonical
    assert looked_up == ["Machine learning"]


def test_article_path_accepts_one_url_encoded_fallback() -> None:
    decoded = "中国大陆区 App_Store"
    encoded = server.quote(decoded, safe="")
    canonical = SimpleNamespace(path=decoded, is_redirect=False)
    calls: list[str] = []

    def get_entry_by_path(path: str):
        calls.append(path)
        if path == decoded:
            return canonical
        raise KeyError(path)

    archive = SimpleNamespace(
        get_entry_by_path=get_entry_by_path,
        has_entry_by_title=lambda title: False,
    )

    assert server._entry(archive, encoded) is canonical
    assert calls == [encoded, decoded]


def test_article_path_prefers_literal_percent_encoded_key() -> None:
    literal = "%E4%B8%AD"
    canonical = SimpleNamespace(path=literal, is_redirect=False)
    calls: list[str] = []

    def get_entry_by_path(path: str):
        calls.append(path)
        if path == literal:
            return canonical
        raise KeyError(path)

    archive = SimpleNamespace(
        get_entry_by_path=get_entry_by_path,
        has_entry_by_title=lambda title: False,
    )

    assert server._entry(archive, literal) is canonical
    assert calls == [literal]


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
    assert "single URL-encoded path is accepted" in message
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
    with pytest.raises(ValueError, match="internal image"):
        server._resolve_image_path("topic", "#section")


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


def _read_html_fixture(
    monkeypatch: pytest.MonkeyPatch,
    html: str,
    valid_paths: set[str] | None = None,
    article_path: str = "article",
) -> SimpleNamespace:
    item = SimpleNamespace(
        size=len(html), mimetype="text/html", content=html.encode(), title="Article"
    )
    entry = SimpleNamespace(
        is_redirect=False,
        path=article_path,
        title="Article",
        get_item=lambda: item,
    )
    valid = set(valid_paths or ())
    archive = SimpleNamespace(has_entry_by_path=lambda path: path in valid)
    monkeypatch.setattr(
        server, "_select_paths", lambda _: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda _: archive)
    monkeypatch.setattr(server, "_entry", lambda _archive, _path: entry)
    return archive


def _read_text_fixture(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    item = SimpleNamespace(
        size=len(text), mimetype="text/plain", content=text.encode(), title="Article"
    )
    entry = SimpleNamespace(
        is_redirect=False, path="article", title="Article", get_item=lambda: item
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda _: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda _: SimpleNamespace())
    monkeypatch.setattr(server, "_entry", lambda _archive, _path: entry)


def test_read_article_paginates_combined_links_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    curated = [f"curated-{index}" for index in range(3)]
    body = [f"body-{index}" for index in range(30)]
    html = (
        '<div class="mw-parser-output">'
        + "".join(f'<p><a href="{path}">{path}</a></p>' for path in body)
        + '<h2 id="See_also">See also</h2><ul>'
        + "".join(f'<li><a href="{path}">{path}</a></li>' for path in curated)
        + "</ul>"
        + '<p><a href="curated-0">Duplicate curated</a></p>'
        + "</div>"
    )
    _read_html_fixture(monkeypatch, html, set(curated + body))

    first = server.read_article("test.zim", "article", include_images=False)
    assert first["link_offset"] == 0
    assert first["returned_links"] == server.MAX_LINK_PAGE == 25
    assert first["total_links"] == first["returned_links"]
    assert first["links_truncated"] is True
    assert first["next_link_offset"] == 25
    assert first["links_window_exhausted"] is False
    assert [item["article_path"] for item in first["see_also"]] == curated
    assert [item["article_path"] for item in first["links"]] == body[:22]

    second = server.read_article(
        "test.zim", "article", include_images=False, link_offset=25
    )
    assert second["link_offset"] == 25
    assert second["returned_links"] == 8
    assert second["links_truncated"] is False
    assert second["next_link_offset"] is None
    assert second["see_also"] == []
    assert [item["article_path"] for item in second["links"]] == body[22:]

    page_paths = [
        item["article_path"]
        for page in (first, second)
        for key in ("see_also", "links")
        for item in page[key]
    ]
    assert len(page_paths) == len(set(page_paths)) == 33
    assert set(page_paths) == set(curated + body)


def test_read_article_links_skip_noise_missing_and_external(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (
        '<div class="mw-parser-output">'
        '<a href="https://example.com/external">External</a>'
        '<a href="missing">Missing</a>'
        '<a href="#section">Same-article fragment</a>'
        '<nav><a href="nav-link">Navigation</a></nav>'
        '<table class="infobox"><tr><td><a href="infobox-link">Infobox</a></td></tr></table>'
        '<p><a href="body-1">Usable</a></p>'
        "</div>"
    )
    _read_html_fixture(monkeypatch, html, {"nav-link", "infobox-link", "body-1"})

    result = server.read_article("test.zim", "article", include_images=False)

    assert [item["article_path"] for item in result["links"]] == ["body-1"]
    assert result["see_also"] == []
    assert result["returned_links"] == 1
    assert result["links_truncated"] is False


def test_read_article_link_offsets_handle_exact_end_and_overrun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = ["a", "b", "c"]
    html = '<div class="mw-parser-output">' + "".join(
        f'<a href="{path}">{path}</a>' for path in paths
    )
    _read_html_fixture(monkeypatch, html, set(paths))

    exact = server.read_article(
        "test.zim", "article", include_images=False, link_offset=2
    )
    assert exact["returned_links"] == 1
    assert exact["links_truncated"] is False
    assert exact["next_link_offset"] is None

    overrun = server.read_article(
        "test.zim", "article", include_images=False, link_offset=3
    )
    assert overrun["returned_links"] == 0
    assert overrun["link_offset"] == 3
    assert overrun["links_truncated"] is False
    assert overrun["next_link_offset"] is None

    clamped = server.read_article(
        "test.zim", "article", include_images=False, link_offset=5000
    )
    assert clamped["link_offset"] == server.LINK_OFFSET_LIMIT == 1000
    assert clamped["returned_links"] == 0
    assert clamped["links_window_exhausted"] is False


def test_read_article_link_pagination_is_lazy_and_reports_window_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [f"body-{index}" for index in range(1030)]
    calls = 0

    def has_entry_by_path(path: str) -> bool:
        nonlocal calls
        calls += 1
        return path in set(paths)

    html = '<div class="mw-parser-output">' + "".join(
        f'<a href="{path}">{path}</a>' for path in paths
    )
    item = SimpleNamespace(
        size=len(html), mimetype="text/html", content=html.encode(), title="Article"
    )
    entry = SimpleNamespace(
        is_redirect=False, path="article", title="Article", get_item=lambda: item
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda _: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(
        server,
        "_archive",
        lambda _: SimpleNamespace(has_entry_by_path=has_entry_by_path),
    )
    monkeypatch.setattr(server, "_entry", lambda _archive, _path: entry)

    first = server.read_article("test.zim", "article", include_images=False)
    assert calls == server.MAX_LINK_PAGE + 1
    assert first["links_truncated"] is True
    assert first["next_link_offset"] == 25

    calls = 0
    last = server.read_article(
        "test.zim", "article", include_images=False, link_offset=1000
    )
    assert calls == 1000 + 1 + 1
    assert last["link_offset"] == 1000
    assert last["returned_links"] == 1
    assert last["links_truncated"] is True
    assert last["next_link_offset"] is None
    assert last["links_window_exhausted"] is True
    assert "link window ends at offset 1000" in last["links_guidance"]


def test_read_article_link_window_is_exactly_0_to_1000(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [f"body-{index}" for index in range(1030)]
    html = '<div class="mw-parser-output">' + "".join(
        f'<a href="{path}">{path}</a>' for path in paths
    )
    _read_html_fixture(monkeypatch, html, set(paths))

    near_end = server.read_article(
        "test.zim", "article", include_images=False, link_offset=990
    )
    assert near_end["returned_links"] == 11
    assert [item["article_path"] for item in near_end["links"]] == paths[990:1001]
    assert near_end["next_link_offset"] is None
    assert near_end["links_truncated"] is True
    assert near_end["links_window_exhausted"] is True

    at_end = server.read_article(
        "test.zim", "article", include_images=False, link_offset=1000
    )
    assert at_end["returned_links"] == 1
    assert [item["article_path"] for item in at_end["links"]] == [paths[1000]]
    assert at_end["next_link_offset"] is None
    assert at_end["links_truncated"] is True
    assert at_end["links_window_exhausted"] is True

    oversized = server.read_article(
        "test.zim", "article", include_images=False, link_offset=5000
    )
    assert oversized["link_offset"] == 1000
    assert oversized["returned_links"] == 1

    cursors: list[int] = []
    seen: list[str] = []
    cursor = 0
    while cursor is not None:
        page = server.read_article(
            "test.zim", "article", include_images=False, link_offset=cursor
        )
        assert page["link_offset"] == cursor
        assert len(page["see_also"]) + len(page["links"]) == page["returned_links"]
        assert page["returned_links"] <= 25
        seen.extend(item["article_path"] for item in page["links"])
        cursors.append(cursor)
        cursor = page["next_link_offset"]
        if cursor is not None:
            assert cursor > cursors[-1]
    assert cursors == list(range(0, 1000, 25)) + [1000]
    assert seen == paths[:1001]


def test_read_article_include_links_false_skips_link_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _read_html_fixture(monkeypatch, '<div class="mw-parser-output"></div>')

    def unexpected(*_args, **_kwargs):
        raise AssertionError("link extraction must not run")

    monkeypatch.setattr(server, "_link_candidates", unexpected)
    monkeypatch.setattr(server, "_find_link_notes", unexpected)

    result = server.read_article(
        "test.zim", "article", include_images=False, include_links=False
    )

    assert "see_also" not in result
    assert "links" not in result
    assert "link_offset" not in result
    assert "total_links" not in result


def test_read_article_keeps_hatnote_notes_before_text_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (
        '<div class="mw-parser-output">'
        '<div class="hatnote">主条目：<a href="target">目标</a></div>'
        '<h2 id="Body">Body</h2><p>Body text.</p></div>'
    )
    _read_html_fixture(monkeypatch, html, {"target"})

    result = server.read_article("test.zim", "article", include_images=False)

    assert len(result["notes"]) == 1
    assert result["notes"][0]["label"].startswith("主条目")
    assert result["notes"][0]["links"][0]["article_path"] == "target"

    section = server.read_article(
        "test.zim", "article", section="Body", include_images=False
    )
    assert section["section"]["anchor"] == "Body"
    assert len(section["notes"]) == 1
    assert section["notes"][0]["links"][0]["article_path"] == "target"


def test_see_also_traversal_stops_at_article_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (
        "<html><body><main>"
        '<h2 id="See_also">See also</h2><a href="inside">Inside</a>'
        "</main>"
        '<footer><a href="outside">Outside</a></footer>'
        "</body></html>"
    )
    _read_html_fixture(monkeypatch, html, {"inside", "outside"})

    result = server.read_article("test.zim", "article", include_images=False)

    assert [item["article_path"] for item in result["see_also"]] == ["inside"]
    assert [item["article_path"] for item in result["links"]] == []


def test_query_feedback_tracks_literal_first_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "Opening text. " + "x" * 400 + " Needle here " + "y" * 400
    _read_text_fixture(monkeypatch, text)

    found = server.read_article(
        "test.zim", "article", query="needle", include_images=False, include_links=False
    )
    assert found["query_found"] is True
    assert found["query_match_offset"] == text.lower().index("needle")
    assert found["requested_offset"] == 0
    assert "Needle here" in found["text"]
    assert "query_note" not in found

    missed = server.read_article(
        "test.zim", "article", query="absent", include_images=False, include_links=False
    )
    assert missed["query_found"] is False
    assert missed["query_match_offset"] is None
    assert missed["requested_offset"] == 0
    assert missed["text"] == text[: server.DEFAULT_MAX_CHARS]
    assert "not at a matching passage" in missed["query_note"]

    blank = server.read_article(
        "test.zim", "article", query="   ", include_images=False, include_links=False
    )
    assert blank["query_found"] is None
    assert blank["query_match_offset"] is None
    assert "query_note" not in blank


def test_query_feedback_continuation_keeps_requested_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "a" * 3000 + " needle " + "b" * 3000
    _read_text_fixture(monkeypatch, text)

    result = server.read_article(
        "test.zim",
        "article",
        query="needle",
        offset=2500,
        max_chars=1000,
        include_images=False,
        include_links=False,
    )

    assert result["offset"] == 2500
    assert result["requested_offset"] == 2500
    assert result["text"] == text[2500:3500]
    assert result["query_found"] is True
    assert result["query_match_offset"] == text.index("needle")

    beyond = server.read_article(
        "test.zim",
        "article",
        offset=len(text) + 10,
        include_images=False,
        include_links=False,
    )
    assert beyond["requested_offset"] == len(text) + 10
    assert beyond["offset"] == len(text)
    assert "at or beyond the end" in beyond["note"]


def test_query_feedback_is_section_local_and_unicode_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (
        '<div class="mw-parser-output">'
        "<p>Lead paragraph.</p>"
        '<h2 id="First">First</h2><p>Alpha text.</p>'
        '<h2 id="Second">Second</h2><p>Beta needle here. Straße</p>'
        "</div>"
    )
    _read_html_fixture(monkeypatch, html)

    first = server.read_article(
        "test.zim",
        "article",
        section="First",
        query="needle",
        include_images=False,
        include_links=False,
    )
    assert first["query_found"] is False
    assert first["query_match_offset"] is None
    assert "selected section" in first["query_note"]

    second = server.read_article(
        "test.zim",
        "article",
        section="Second",
        query="needle",
        include_images=False,
        include_links=False,
    )
    assert second["query_found"] is True
    assert second["query_match_offset"] is not None
    assert second["text"][second["query_match_offset"] :].startswith("needle")

    unicode_match = server.read_article(
        "test.zim",
        "article",
        section="Second",
        query="STRASSE",
        include_images=False,
        include_links=False,
    )
    assert unicode_match["query_found"] is True
    assert unicode_match["query_match_offset"] is not None
    assert unicode_match["text"][unicode_match["query_match_offset"] :].startswith(
        "Straße"
    )


def test_literal_query_position_maps_casefold_expansions() -> None:
    assert server._literal_query_position("Die Straße", "STRASSE") == 4
    assert server._literal_query_position("İstanbul", "İSTANBUL") == 0
    assert server._literal_query_position("İstanbul", "istanbul") == -1
    assert server._literal_query_position("text", "   ") == -1
    # A folded match can start inside an expansion; it belongs to that original
    # character, not to the following one.
    assert server._literal_query_position("İx", "\u0307") == 0
    assert server._literal_query_position("xİ", "\u0307") == 1
    # Whitespace is trimmed from the query, and equal-length casefold takes the
    # fast path for ordinary ASCII/CJK text.
    assert server._literal_query_position("xx TEXT yy", "  text  ") == 3
    assert server._literal_query_position("abc", "B") == 1
    assert server._literal_query_position("维基百科", "百") == 2


def test_read_article_query_inside_casefold_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _read_text_fixture(monkeypatch, "İx body")

    result = server.read_article(
        "test.zim",
        "article",
        query="\u0307",
        include_images=False,
        include_links=False,
    )

    assert result["query_found"] is True
    assert result["query_match_offset"] == 0
    assert result["text"].startswith("İx")


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


def test_search_reuses_bounded_title_suggestions_for_exact_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _search_archive(
        has_fulltext_index=True,
        has_title_index=True,
        has_entry_by_title=lambda query: False,
    )
    suggestion_queries: list[str] = []
    fulltext_queries: list[str] = []
    suggestions = SimpleNamespace(
        getResults=lambda start, limit: [
            "A/Fainting_goats",
            "A/Fainting_goat",
        ][start : start + limit],
        getEstimatedMatches=lambda: 2,
    )
    fulltext = SimpleNamespace(
        getResults=lambda start, limit: ["Noise"],
        getEstimatedMatches=lambda: 1,
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: archive)
    monkeypatch.setattr(
        server,
        "SuggestionSearcher",
        lambda archive: SimpleNamespace(
            suggest=lambda query: suggestion_queries.append(query) or suggestions
        ),
    )
    monkeypatch.setattr(
        server,
        "Query",
        lambda: SimpleNamespace(set_query=lambda value: SimpleNamespace(query=value)),
    )
    monkeypatch.setattr(
        server,
        "Searcher",
        lambda archive: SimpleNamespace(
            search=lambda query: fulltext_queries.append(query.query) or fulltext
        ),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, name, path, query: {"article_path": path},
    )

    result = server.search("  fainting_goat  ", "test.zim", limit=3)

    assert suggestion_queries == ["fainting goat"]
    assert fulltext_queries == ["fainting_goat"]
    assert [item["article_path"] for item in result["results"]] == [
        "A/Fainting_goat",
        "Noise",
    ]
    assert result["results"][0]["match_type"] == "exact_title"


def test_near_title_suggestion_is_not_promoted_to_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _search_archive(
        has_fulltext_index=True,
        has_title_index=True,
        has_entry_by_title=lambda query: False,
    )
    suggestions = SimpleNamespace(
        getResults=lambda start, limit: ["A/Fainting_goats"],
        getEstimatedMatches=lambda: 1,
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: archive)
    monkeypatch.setattr(
        server,
        "SuggestionSearcher",
        lambda archive: SimpleNamespace(suggest=lambda query: suggestions),
    )
    monkeypatch.setattr(
        server,
        "Searcher",
        lambda archive: SimpleNamespace(
            search=lambda query: SimpleNamespace(
                getResults=lambda start, limit: ["A/Fainting_goats"],
                getEstimatedMatches=lambda: 1,
            )
        ),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, name, path, query: {"article_path": path},
    )

    result = server.search("fainting_goat", "test.zim")

    assert result["results"][0]["match_type"] == "fulltext"


def test_exact_suggestion_uses_display_title_and_legacy_namespace() -> None:
    titles = {
        "Help:Google": "Google",
        "A/C++": "",
        "A/Googles": "Googles",
    }
    archive = SimpleNamespace(
        get_entry_by_path=lambda path: SimpleNamespace(title=titles[path])
    )
    suggestions = SimpleNamespace(
        getResults=lambda start, limit: list(titles)[start : start + limit]
    )

    assert server._exact_suggested_path(archive, suggestions, "google") == "Help:Google"
    assert server._exact_suggested_path(archive, suggestions, "C++") == "A/C++"
    assert server._exact_suggested_path(archive, suggestions, "A/C++") is None
    assert server._exact_suggested_path(archive, suggestions, "Googler") is None


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


def test_search_no_hits_explains_coverage_without_changing_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _search_archive(
        has_fulltext_index=True, has_entry_by_title=lambda query: False
    )
    empty = SimpleNamespace(
        getResults=lambda start, limit: [],
        getEstimatedMatches=lambda: 0,
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: archive)
    monkeypatch.setattr(
        server,
        "Searcher",
        lambda archive: SimpleNamespace(search=lambda query: empty),
    )

    result = server.search("missing topic", "test.zim")

    assert result["status"] == "no_hits"
    assert result["searched_archives"] == 1
    assert result["filtered_archives"] == 0
    assert result["filters"]["applied"] is False
    assert "maxi flavour adds media" in result["guidance"]
    assert "never switches archives" in result["guidance"]


def test_search_filter_miss_and_single_archive_ignore_are_explained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _search_archive(
        has_fulltext_index=True,
        has_entry_by_title=lambda query: False,
        get_metadata=lambda key: b"eng" if key == "Language" else b"nopic",
    )
    empty = SimpleNamespace(
        getResults=lambda start, limit: [],
        getEstimatedMatches=lambda: 0,
    )
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("test.zim", Path("test.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: archive)
    monkeypatch.setattr(
        server,
        "Searcher",
        lambda archive: SimpleNamespace(search=lambda query: empty),
    )

    filtered = server.search("topic", "*", language="zh")
    assert filtered["filters"]["applied"] is True
    assert filtered["searched_archives"] == 0
    assert filtered["filtered_archives"] == 1
    assert "1 archive(s) were filtered out" in filtered["guidance"]

    ignored = server.search("topic", "test.zim", language="zh")
    assert ignored["filters"]["applied"] is False
    assert "filters were ignored" in ignored["guidance"]


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


def test_search_offset_window_end_is_explained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [f"A/{index}" for index in range(1005)]
    archive = _search_archive(
        has_fulltext_index=True,
        has_title_index=False,
        has_entry_by_title=lambda query: False,
    )
    outcome = SimpleNamespace(
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
        lambda archive: SimpleNamespace(search=lambda query: outcome),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, archive_id, article_path, query: {"article_path": article_path},
    )

    result = server.search("q", "test.zim", limit=20, offset=990)

    assert result["status"] == "ok"
    assert result["offset"] == 990
    assert result["requested_offset"] == 990
    assert result["window_truncated"] is True
    assert result["next_offset"] is None
    assert "window ends at offset 1000" in result["guidance"]


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


def test_exact_title_with_zero_fulltext_estimate_reports_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _search_archive(
        has_fulltext_index=True,
        has_title_index=True,
        has_entry_by_title=lambda query: False,
    )
    empty = SimpleNamespace(
        getResults=lambda start, limit: [], getEstimatedMatches=lambda: 0
    )
    suggestion = SimpleNamespace(getResults=lambda start, limit: ["Fainting_goat"])
    monkeypatch.setattr(
        server, "_select_paths", lambda archive_id: [("en_all.zim", Path("en_all.zim"))]
    )
    monkeypatch.setattr(server, "_archive", lambda path: archive)
    monkeypatch.setattr(
        server, "Searcher", lambda archive: SimpleNamespace(search=lambda query: empty)
    )
    monkeypatch.setattr(
        server,
        "SuggestionSearcher",
        lambda archive: SimpleNamespace(suggest=lambda query: suggestion),
    )
    monkeypatch.setattr(
        server,
        "_article_summary",
        lambda archive, archive_id, article_path, query: {"article_path": article_path},
    )

    result = server.search("fainting_goat", "en_all.zim")

    assert [item["match_type"] for item in result["results"]] == ["exact_title"]
    assert result["estimated_matches"] is None

    # A genuine empty result keeps the confirmed zero.
    empty_suggestion = SimpleNamespace(getResults=lambda start, limit: [])
    monkeypatch.setattr(
        server,
        "SuggestionSearcher",
        lambda archive: SimpleNamespace(suggest=lambda query: empty_suggestion),
    )
    no_hits = server.search("nothing_here", "en_all.zim")
    assert no_hits["results"] == []
    assert no_hits["estimated_matches"] == 0


def test_query_variants_only_differ_for_traditional() -> None:
    assert server._query_variants("圖靈") == ["圖靈", "图灵"]
    assert server._query_variants("艾伦·图灵") == ["艾伦·图灵", "艾倫·圖靈"]
    assert server._query_variants("1518年法国舞蹈瘟疫") == [
        "1518年法国舞蹈瘟疫",
        "1518年法國舞蹈瘟疫",
    ]
    assert server._query_variants("Alan Turing") == ["Alan Turing"]


def test_chinese_variants_are_lazy_and_can_be_disabled() -> None:
    root = Path(__file__).resolve().parent.parent
    script = """
import os
import sys
import server

query = os.environ["TEST_QUERY"]
expected = os.environ["TEST_EXPECTED"]
assert server._query_variants(query) == ([query] if expected == "same" else [query, expected])
print("loaded" if "zh_variants" in sys.modules else "not-loaded")
"""

    english = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env={**os.environ, "TEST_QUERY": "Alan Turing", "TEST_EXPECTED": "same"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert english.stdout.strip() == "not-loaded"

    off = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env={
            **os.environ,
            "KIWIX_MCP_ZH_VARIANTS": "off",
            "TEST_QUERY": "圖靈",
            "TEST_EXPECTED": "same",
        },
        check=True,
        capture_output=True,
        text=True,
    )
    assert off.stdout.strip() == "not-loaded"

    automatic = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env={**os.environ, "TEST_QUERY": "圖靈", "TEST_EXPECTED": "图灵"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert automatic.stdout.strip() == "loaded"


def test_reverse_map_drops_ambiguous_simplified_chars() -> None:
    reverse = zh_variants.simplified_to_traditional()
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
