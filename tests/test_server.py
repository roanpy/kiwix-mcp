from pathlib import Path

import pytest

import server


def test_empty_archive_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    assert "Useful answer" in server._excerpt("x" * 1200 + content, "Useful answer", 1000)


def test_archive_selection_rejects_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KIWIX_ARCHIVE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="Unknown archive"):
        server._select_paths("missing.zim")

