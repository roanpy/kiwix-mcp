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

- `list_archives`: list ZIM metadata, including available search indexes and
  `flavour` (for example `maxi` or `nopic`) for archive selection.
- `search`: search one selected archive with exact-title priority, pagination,
  an estimated match count, and per-result `match_type`; use `archive_id="*"`
  for a small cross-archive comparison.
- `read_article`: bounded text with `next_offset` continuation, capped image
  metadata, image metadata pagination via `image_offset`, and concise related links.
- `extract_image`: return native MCP image content plus a temporary `file_path`
  fallback for clients such as pi. Pass `image_path` from `read_article` when
  index 0 is not the desired image. Temporary images are deduplicated and capped
  at the 16 most recently used files.

Set `KIWIX_ARCHIVE_DIR` to use another archive directory. Single-file `.zim`
and split `.zimaa` archives are detected. The calling agent is instructed to
translate answers into the user's language while preserving source URIs.

When `read_article` returns `next_offset`, call it again with the same
`archive_id` and `article_path` plus that `offset` to continue a long article.
When it returns `next_image_offset`, call it again with the same article and
that `image_offset` to continue a long image list.

Diagnostics are off by default. Set `KIWIX_MCP_LOG_LEVEL=INFO` for stderr logs,
or set `KIWIX_MCP_LOG_FILE=/path/to/kiwix.log` for a small rotating log (up to
three 1 MiB files).
