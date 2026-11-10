# Changelog

## Unreleased

- Access-log retention no longer keeps every ordinary line when unlock lines already fill the cap. The event just written is always kept. Unlock lines are still kept, so the file can grow past `max_events`.
- `session_retention.max_turns_per_conversation` caps turn hashes inside one conversation. Omitted means unlimited. The minimal template sets it to 50. Older configs that only set `max_conversations` are unchanged.
- Default recall, after the size/mtime manifest matches, queries the SQLite FTS and hit-word tables and reads only the selected L1 files. It does not parse `search.json`, `hitwords.json`, or `catalog.json`. `reindex` builds those tables. A same-size edit with the original mtime restored is still invisible until `--full-verify` or `reindex --verify`.
- `cmd_init()` now defaults to the minimal profile, matching `hfa init`. Pass `profile="example"` for the teaching tree.
- A fenced `hfa-memory` block is no longer discarded because the surrounding reply contains words such as 尚未 or 示例. Those words still reject an unfenced reply, and they still reject the fenced conclusion itself.
- Read-only SQLite opens with `Path.resolve().as_uri()` plus `?mode=ro` and `PRAGMA query_only=ON`. `?`, `#`, `%`, and spaces in the path no longer select a different file, and a failed open is not retried read-write.
- `hfa init` builds the derived index before it releases the memory lock, so a concurrent recall cannot observe a directory that has no index yet.
- `--phase auto` treats a payload as post only for an explicit save object or a turn-end field (`lastAssistantResponse`, `stopResponse`, `assistantMessage`). A leftover `response`, `output`, or `modelOutput` stays pre.
- A ` ```hfa-memory ` fence may carry an extra info token such as `json`, and the closing fence may sit on the same line as `}`.
- Transactions copy an existing `fts.sqlite` aside under `.hfa-prior/` instead of base64-encoding the whole file into `.hfa-transaction.json`. Index rows are inserted with `executemany`. Crash recovery still rolls the copy back. Every save still rebuilds derived indexes, so write time grows with the number of L1 records.
- Hook subprocesses wait 120 seconds by default. `HFA_HOOK_TIMEOUT` changes that, and `--print-hooks` publishes the same number. A host can still enforce a shorter cap.
- `status` and `doctor` trust the size/mtime manifest plus `PRAGMA quick_check` and `meta.source_digest`. They rebuild the derived image only when that check fails or `--full-verify` is set.
- Source distributions include `LICENSE`. Templates are taken from the package directory; `site-packages/templates` cannot shadow them. `scripts/__init__.py` is the version string the build backend reads. The import package stays `scripts` so existing `hfa` / `hfa-hook` / `hfa-mcp` entry points keep working.
- Recall compares `meta.source_digest` in `fts.sqlite` with the manifest. If FTS5 does not answer, recall uses `term_df` and `doc_term` instead of reading every chunk. If those tables are missing too, recall fails with an error instead of scanning.
