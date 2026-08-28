# kiwix-mcp

Local MCP server for searching and reading ZIM archives.

Install the locked environment and place archives in:

```bash
uv sync --frozen
mkdir -p ~/.chroma_db/kiwix/archives
```

Run over stdio:

```bash
uv run --frozen python server.py
```

For Hermes, set `lazy: true` and `idle_timeout_seconds: 900` on this server.
Keep `supports_parallel_tool_calls` unset: separate clients already use separate
stdio processes, while one Agent session stays serialized for predictable libzim
access.

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
- `inspect_article`: return a clean lead, heading outline with section URIs,
  MediaWiki infobox facts, redirect/canonical metadata, archive language/date,
  and reference count without returning the full article.
- `read_article`: clean, bounded text with `next_offset` continuation, optional
  `section` selection by title or anchor, capped image
  metadata (`primary` marks the first image kept after duplicate-path removal
  and tiny-icon filtering), image metadata pagination via `image_offset`, and
  concise related links (each link/see-also entry carries a `uri` field).
- `list_references`: page through MediaWiki citations, notes, and preserved external
  URLs, or select a visible marker such as `1`, `a`, or `note 1` via
  `citation_label`.
- `extract_image`: return native MCP image content plus a temporary `file_path`
  fallback for clients such as pi. Prefer the entry with `primary=True` unless
  another image is wanted, then pass the chosen entry's `image_path`.
  Temporary images are deduplicated and capped at the 16 most recently used files.

MCP resources (2.x):

- Resource template:
  `kiwix://{archive_id}/{+article_path}`
- Add a heading fragment to read one section, for example
  `kiwix://archive.zim/Artificial_intelligence#Knowledge_representation`.
- `list_resources`: returns each archive's main entry as a `text/plain` resource.
- `read_resource`: return plain article text from a stable URI. Text is
  truncated at 50,000 characters (`MAX_RESOURCE_CHARS`); the result `meta` and
  `contents[0].meta` carry `truncated` and `next_offset_hint` when truncation
  occurs (for long articles, use `read_article` with `next_offset`).
- Resource clients can still load article text through `read_resource` when images are
  unavailable.

Set `KIWIX_ARCHIVE_DIR` to use another archive directory. Single-file `.zim`
and split `.zimaa` archives are detected. Existing local installations that
store archives elsewhere should keep setting this variable explicitly.

When `read_article` returns `next_offset`, call it again with the same
`archive_id` and `article_path` plus that `offset` to continue a long article.
For long Wikipedia articles, call `inspect_article` first and pass a returned
outline `anchor` as `read_article.section`.
When it returns `next_image_offset`, call it again with the same article and
that `image_offset` to continue a long image list.

Diagnostics are off by default. Set `KIWIX_MCP_LOG_LEVEL=INFO` for stderr logs,
or set `KIWIX_MCP_LOG_FILE=/path/to/kiwix.log` for a small rotating log (up to
three 1 MiB files).

Idle stdio processes exit after 600 seconds by default. Override this with
`KIWIX_MCP_IDLE_TIMEOUT=<seconds>`, or set it to `0` to disable idle shutdown.
Any inbound MCP message, including initialization and ping, resets the timer.
Use idle shutdown only with an MCP client that respawns stdio servers on the
next call.

This repository currently has no project license and is not an open-source
release. Before redistribution, choose a project license and review the
GPL-3.0 license shipped with the `libzim` dependency.
