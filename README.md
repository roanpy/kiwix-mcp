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

Tools:

- `list_archives`: list selectable ZIM files and their title, language, and date.
- `search`: search one selected archive using its full-text index or title fallback.
- `read_article`: bounded, paragraph-preserving text plus internal image metadata.
- `extract_image`: return native MCP image content plus a temporary `file_path`
  fallback for clients such as pi. Temporary images are deduplicated and capped
  at the 16 most recently used files.

Set `KIWIX_ARCHIVE_DIR` to use another archive directory. Single-file `.zim`
and split `.zimaa` archives are detected. The calling agent is instructed to
translate answers into the user's language while preserving source URIs.
