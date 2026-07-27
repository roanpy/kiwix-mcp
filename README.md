# kiwix-mcp

Local MCP server for searching and reading ZIM archives.

Place archives in:

```text
/Users/peter/.chroma_db/kiwix/archives
```

Run over stdio:

```bash
/Users/peter/Developer/kiwix-mcp/.venv/bin/python /Users/peter/Developer/kiwix-mcp/server.py
```

Tools:

- `list_archives`: list ZIM metadata used by the agent to choose the best archive.
- `search`: search one selected archive with exact-title priority and pagination.
- `read_article`: bounded text plus image metadata and concise related links.
- `extract_image`: return native MCP image content plus a temporary `file_path`
  fallback for clients such as pi. Temporary images are deduplicated and capped
  at the 16 most recently used files.

Set `KIWIX_ARCHIVE_DIR` to use another archive directory. Single-file `.zim`
and split `.zimaa` archives are detected. The calling agent is instructed to
translate answers into the user's language while preserving source URIs.
