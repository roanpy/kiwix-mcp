# kiwix-mcp

Local, read-only MCP server for searching and reading ZIM archives.

Place archives in:

```text
/Users/user/.chroma_db/kiwix/archives
```

Run over stdio:

```bash
/Users/user/Developer/kiwix-mcp/.venv/bin/python /Users/user/Developer/kiwix-mcp/server.py
```

Tools:

- `list_archives`: list selectable ZIM files from `KIWIX_ARCHIVE_DIR`.
- `search`: full-text search when the ZIM contains an index; title fallback otherwise.
- `read_article`: bounded text extraction plus internal image references.
- `extract_image`: return native MCP image content plus a temporary `file_path`
  fallback for clients such as pi. Temporary images are deduplicated and capped
  at the 16 most recently used files.

Set `KIWIX_ARCHIVE_DIR` to use another archive directory. Single-file `.zim`
and split `.zimaa` archives are detected. The calling agent is instructed to
translate answers into the user's language while preserving source URIs.
