# Changelog

## Unreleased

- Access-log retention no longer keeps every ordinary line when unlock lines already fill the cap. The event just written is always kept. Unlock lines are still kept, so the file can grow past `max_events`.
- `session_retention.max_turns_per_conversation` caps turn hashes inside one conversation. Omitted means unlimited. The minimal template sets it to 50. Older configs that only set `max_conversations` are unchanged.
- Default recall, after the size/mtime manifest matches, queries the SQLite FTS and hit-word tables and reads only the selected L1 files. It does not parse `search.json`, `hitwords.json`, or `catalog.json`. `reindex` builds those tables. A same-size edit with the original mtime restored is still invisible until `--full-verify` or `reindex --verify`.
- `cmd_init()` now defaults to the minimal profile, matching `hfa init`. Pass `profile="example"` for the teaching tree.
- A fenced `hfa-memory` block is no longer discarded because the surrounding reply contains words such as 尚未 or 示例. Those words still reject an unfenced reply, and they still reject the fenced conclusion itself.
