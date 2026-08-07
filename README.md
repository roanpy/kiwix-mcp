# kiwix-mcp

Local MCP server for searching and reading ZIM archives.

Place archives in:

```text
/Users/user/.chroma_db/kiwix/archives
```

Run over stdio:

```bash
/Users/user/Developer/kiwix-mcp/.venv/bin/python /Users/user/Developer/kiwix-mcp/server.py
```

The server uses the MCP 2.x low-level `Server` API and keeps the legacy
initialize/session path available for older MCP clients. The tool names and
stdio command remain unchanged.

Tools:

- `list_archives`: list ZIM metadata, including available search indexes,
  `flavour` (for example `maxi` or `nopic`), `has_main_entry`, and
  `main_entry_path` for archive selection.
- `search`: search one selected archive with exact-title priority, pagination,
  an estimated match count, and per-result `match_type`; use `archive_id="*"`
  for a small cross-archive comparison. Added `mode` parameter:
  `auto` (default), `fulltext`, or `title`. `fulltext` is strict and fails on
  archives without full-text index; `title` forces title-only suggestion lookup.
  Optional `language` and `flavour` filter archives in cross-archive mode only.
- `read_article`: bounded text with `next_offset` continuation, capped image
  metadata (each image entry includes `is_main` for the heuristic lead image),
  image metadata pagination via `image_offset`, and concise related links
  (each link/see-also entry carries a `uri` field).
- `extract_image`: return native MCP image content plus a temporary `file_path`
  fallback for clients such as pi. Pass `image_path` from `read_article` when
  index 0 is not the desired image; prefer entries with `is_main=True`.
  Temporary images are deduplicated and capped at the 16 most recently used files.

MCP resources (2.x):

- Resource template:
  `kiwix://{archive_id}/{+article_path}`
- `list_resources`: returns each archive's main entry as a `text/plain` resource.
- `read_resource`: return plain article text from a stable URI. Text is
  truncated at 50,000 characters (`MAX_RESOURCE_CHARS`); the result `meta` and
  `contents[0].meta` carry `truncated` and `next_offset_hint` when truncation
  occurs (for long articles, use `read_article` with `next_offset`).
- Resource clients can still load article text through `read_resource` when images are
  unavailable.

Set `KIWIX_ARCHIVE_DIR` to use another archive directory. Single-file `.zim`
and split `.zimaa` archives are detected.

When `read_article` returns `next_offset`, call it again with the same
`archive_id` and `article_path` plus that `offset` to continue a long article.
When it returns `next_image_offset`, call it again with the same article and
that `image_offset` to continue a long image list.

Diagnostics are off by default. Set `KIWIX_MCP_LOG_LEVEL=INFO` for stderr logs,
or set `KIWIX_MCP_LOG_FILE=/path/to/kiwix.log` for a small rotating log (up to
three 1 MiB files).
