# kiwix-mcp

Local, read-only MCP server for searching and reading ZIM archives.

Place archives in:

```text
/Users/peter/.chroma_db/kiwix/archives
```

Run over stdio:

```bash
/Users/peter/Developer/kiwix-mcp/.venv/bin/python /Users/peter/Developer/kiwix-mcp/server.py
```

Tools:

- `search`: full-text search when the ZIM contains an index; title fallback otherwise.
- `read_article`: bounded text extraction for a search result.

