# Kiwix MCP improvement and acceptance plan — 2026-10-22

## Goal and constraints

Improve reliable retrieval and maintenance for a small, local, read-only ZIM
server. Keep the six existing tools and the stdio launch command compatible.
The primary use case remains Chinese/English Wikipedia research; Wikibooks,
Stack Exchange, plain-text and interactive ZIM collections must degrade clearly.
Implementation is delegated to DS4.1 Flash; acceptance is performed independently
by the parent agent. All changes stay in this repository.

## Verified baseline

- Baseline commit: `435fa66`; clean working tree; public, non-fork repository.
- 79 tests pass; Ruff and lock-file checks pass. Baseline wheel/sdist build works.
- Nine local archives include full and selected Wikipedia collections, Wikibooks,
  Stack Exchange and a title-index-only interactive collection.
- `fainting goat` matches an exact English title; `fainting_goat` returns no
  title-mode result. Chinese simplified-to-traditional lookup already works.
- The 2,931-entry Chinese table lives inside `server.py`. Even an ASCII query
  currently constructs the reverse map on its first use, although it generates
  no extra search variant. The previous "first Chinese query only" description
  was inaccurate. Earlier per-dictionary memory sums also double-count shared
  string objects; they are not measurements of incremental process RSS.
- The MCP handler replaces false-like non-object arguments with `{}` before
  dispatch, bypassing the dispatcher's correct input-type rejection.
- A recent user session guessed six missing paths in a selected `wp1-0.8`
  archive. Media flavour and article coverage need clearer guidance.
- Embedded OpenCC-derived data lacks accompanying third-party license notices.

## Work to complete in this round

| Priority | Change | Acceptance evidence |
| --- | --- | --- |
| P2 | Extract the unchanged Chinese table and conversion helpers to `zh_variants.py`; lazy loading and `KIWIX_MCP_ZH_VARIANTS=auto\|off` | Fresh-process English/off queries do not import the table; simplified/traditional regression cases still pass; ambiguous reversals stay excluded |
| P2 | Normalize title-suggestion whitespace/underscores; compare bounded suggestions accurately, including legacy namespace paths; reuse title suggestions | Lowercase and underscore queries return the correct exact title; a non-exact suggestion is never promoted; full-text query text stays intact |
| P2 | Pass raw MCP arguments to shared validation | False-like non-object inputs return an actionable error; omitted/empty-object arguments still work for discovery |
| P2 | Explain no-hit, filter and partial-error outcomes; clarify full collections versus `maxi` media flavour | Failed searches do not imply absence; recovery guidance never silently changes archive or performs extra searches |
| P2 | Include OpenCC attribution/license and explicit module packaging | Wheel/sdist contain both modules and required notices; the built package runs independently of the checkout |
| P2 | Targeted refresh of the transitive PyJWT dependency | Baseline installed PyJWT 2.13.0 has 14 advisory records; refresh only that dependency to the upstream patched release, confirm compatibility and rescan |
| P3 | Correct size-limit recovery and synchronize English/Chinese usage documentation | Guidance matches actual limits; cross-archive strict-fulltext behavior is documented accurately |

`auto` preserves bounded character-level query expansion; it does not translate
English queries into Chinese or implement OpenCC phrase/region conversion.
The existing table is retained, rather than replacing it with a moving upstream
dictionary. The original imported revision is not recorded and must not be
invented in the notices.

The PyJWT scan finding is a dependency-maintenance issue: this server exposes
stdio and does not use JWT/HTTP authentication. Advisory counts must not be
presented as proof of fourteen exploitable Kiwix vulnerabilities. Upstream's
[release history](https://github.com/jpadilla/pyjwt/releases) confirms patched
2.14/2.15 releases, including 2.15.1; avoid an unrelated full dependency upgrade.

## Independent acceptance

1. Run regression tests, Ruff check/format, lock consistency, dependency
   compatibility, and whitespace checks.
2. Verify real simplified/traditional Wikipedia title lookup, English exact
   titles, underscores, cross-archive exact priority, and stable non-overlapping
   pages. Exercise fulltext/title/auto and language/flavour filters.
3. Run an official MCP client against a fresh subprocess: initialize, ping,
   tools/list, all six tools, section/text pagination, references, resources,
   image metadata/native-image extraction, recoverable errors and a valid call
   after an error. Keep image writes in a test-only temporary cache.
4. Exercise representative other ZIMs, including an archive without fulltext
   and an interactive article without readable text. Do not claim to render JS.
5. Build and inspect wheel/sdist, install into an isolated environment, run from
   outside the checkout and verify bundled license files. Record any environment
   limitation rather than treating unrun platform tests as passes.
6. Review changes and source/package contents for private paths, credentials,
   archives, caches and generated artifacts. Clean only this run's temporary
   artifacts and subprocesses. Commit only reviewed project files; preserve the
   existing no-Actions preference with `[skip ci]` if pushing.

## Deliberate exclusions

No new tools, services, vector index, reranker, GraphRAG, automatic multi-hop
crawler, speculative plugin system, full type-framework migration, or dictionary
expansion. Existing article links/sections already support agent-led multi-hop.
Do not change Pi/model services or user-level client configuration in this round.

## Accepted results

Implementation is recorded in `9e6f72c`; the parent independently reviewed and
accepted the code, and added a build check to the existing CI matrix. The worker
created that local implementation commit before parent acceptance; it remained
unpushed until review. The worker also switched the project virtual environment
while testing Python versions; the parent restored its original Python 3.12 and
used separate environments for the other versions.

Pi running DeepSeek V4.1 Flash independently re-ran the live calls. It found a
zero-estimate/title-hit contradiction introduced by the underscore-title fix;
`bccd103` reports that estimate as null and locks the null/zero distinction with
a regression test (90 passed). The Pi run also confirmed image recall and noted
that ZIM content MIME may differ from the source path extension.

| Check | Verified result |
| --- | --- |
| Python 3.12.11 | 89 passed; project environment restored to this version |
| Python 3.13.15 | 89 passed in a separate environment |
| Python 3.14.7 | 89 passed in a separate environment |
| Ruff, lock and installed-dependency compatibility | All pass |
| Official client against source subprocess | Ten real-ZIM acceptance groups pass; final run 5.168 seconds |
| Official client against independently installed wheel | Same ten groups pass from outside the checkout, on Python 3.13; 6.023 seconds |
| Wheel and sdist contents | Both runtime modules, project/third-party licenses present; sdist also includes both READMEs, tests and lock |
| Dependency advisory scan | 37 third-party dependencies scanned; zero known findings after PyJWT 2.15.1 update; the local unpublished project is separately reviewed, not covered by that advisory feed |
| Data preservation | 2,931 forward entries and 2,675 unambiguous reverse entries; forward-table SHA-256 unchanged |
| Loading behavior | Fresh English and `off` queries leave `zh_variants` unimported; Chinese `auto` loads it |
| File hygiene | No detected real local paths, token/key patterns, tracked symlinks or hardlinks in source; package manifests contain no archives or local environments |

The ten real-data groups cover initialization/ping and six tools; discovery of
nine archives; Chinese simplified/traditional exact titles; English case,
underscores, whitespace and cross-archive exact ranking; filtered non-overlapping
pages; inspect/read/text continuation/sections/references/link-following; native
images in a test-only cache; resource discovery/templates/reads; Wikibooks,
Stack Exchange, interactive empty text and missing fulltext; and errors followed
by successful calls. Existing real-subprocess idle-timeout tests also pass.

The forward dictionary checksum is
`e99a44ad81f5cec82c7aab0744098578284046ee62b1b0dff570e94a0cf7dca6`.
`server.py` is reduced from 5,349 to 2,521 lines by separating the dictionary;
the data itself has not been discarded. This is not a claim of faster full-ZIM
retrieval. No GPU model, private archive, or user client configuration was changed.

All runtime validation above was local macOS arm64. Linux remains covered by
the repository's existing CI matrix, but this round does not claim a new Linux
run: automatic workflows are intentionally skipped at the user's request.

## Primary references

- [MCP tool contracts, schemas and recoverable errors](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [libzim suggestion API and optional title index](https://libzim.readthedocs.io/en/latest/api/classzim_1_1SuggestionSearcher.html)
- [Python libzim, including thread-safety guidance](https://github.com/openzim/python-libzim)
- [Kiwix media flavours](https://get.kiwix.org/en/faq-items/what-do-mini-nopic-and-maxi-mean-in-the-wikipedia-zim-files/)
- [OpenCC source dictionary and licensing](https://github.com/BYVoid/OpenCC/blob/master/data/dictionary/TSCharacters.txt)
- [OpenCC license](https://github.com/BYVoid/OpenCC/blob/master/LICENSE)
- [Python packaging metadata and license files](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)
- [Explicit setuptools module configuration](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html)
