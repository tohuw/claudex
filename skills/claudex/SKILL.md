---
name: claudex
description: Search and retrieve prior Claude Code sessions and imported claude.ai conversations using the claudex archive. Use whenever the user refers to an earlier Claude conversation (for example, "we talked about X," "find the thread where," "what did we decide," or "continue that discussion"), asks to search/list/read/summarize Claude transcript history, or wants to ingest a claude.ai data export.
---

# Claudex

Run commands through the bundled launcher:

```bash
<skill-dir>/scripts/claudex <command> [arguments]
```

Resolve `<skill-dir>` to this skill directory. Do not crawl Claude transcript JSONL files manually when this skill applies.

## Retrieve prior conversation

1. Refresh the durable Claude Code index:

   ```bash
   <skill-dir>/scripts/claudex index
   ```

2. Search with the narrowest useful case-insensitive regex:

   ```bash
   <skill-dir>/scripts/claudex search 'term|alternate term' --limit 5
   ```

   Add `--source code` or `--source cloud`, plus `--since` or `--until`, when context supports narrowing.

3. Use excerpts directly when sufficient. Distill results; never paste raw `claudex` output into the active conversation.

4. Inspect one clearly relevant session only when excerpts are insufficient:

   ```bash
   <skill-dir>/scripts/claudex show <session-prefix> --no-tools
   ```

   Do not load an entire transcript without a specific retrieval purpose. If more than five substantial sessions match, narrow the query first. When broad investigation remains necessary and delegation is available, have a subagent inspect the corpus and return a digest of at most 500 words.

5. Prefer a cached L3 summary for deeper context:

   ```bash
   <skill-dir>/scripts/claudex summary <session-prefix>
   ```

   If none exists and generating one is justified, run `summarize <session-prefix>`. This invokes Claude Code non-interactively using the existing login.

## Other operations

- List recent sessions: `list --limit 20`
- Synthesize matching cached summaries: `synthesize '<regex>' --max 30`
- Import the newest claude.ai export: `cloud-ingest`
- Import a specific export directory: `cloud-ingest --export <path>`
- Track export arrival: run `expecting-export`, then use `auto-detect`
- Suppress an unwanted export notice: `dismiss <export-folder>`

Treat `~/.claudex/index` as an archive. Claude Code may prune source JSONLs while their indexed prose remains available.

## Failure handling

- If a session prefix is ambiguous, use a printed candidate ID; never guess.
- If search returns nothing, try a small number of spelling variants or widen the date bound.
- Fall back to raw JSONL inspection only when diagnosing claudex itself.
- If the launcher cannot find the CLI, report that `~/Projects/claudex/claudex` or a `claudex` executable on `PATH` is required.
