---
name: claudex
description: Search and retrieve prior Claude Code sessions, imported claude.ai conversations, claude.ai projects (docs, prompts, project memory), and claude.ai memory using the claudex archive. Use whenever the user refers to an earlier Claude conversation (for example, "we talked about X," "find the thread where," "what did we decide," or "continue that discussion"), asks about a claude.ai project or what Claude's memory says, asks to search/list/read/summarize Claude transcript history, or wants to ingest a claude.ai data export.
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

   Add `--source code|cloud|project|memory`, plus `--since` or `--until`, when context supports narrowing. `project` covers claude.ai project docs, prompt templates, and per-project memory; `memory` covers the global claude.ai memory and its memory-directory files. Content deleted upstream on claude.ai is excluded by default — add `--deleted` when the user asks about something that may have been deleted (hits are tagged `[... deleted]`).

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
- List claude.ai projects: `list --source project`
- Show a full claude.ai project (docs + memory): `show <project-uuid-prefix>`
- Synthesize matching cached summaries: `synthesize '<regex>' --max 30`
- Import the newest claude.ai export (conversations + projects + memories): `cloud-ingest`
- Import an export known to contain ALL conversations (not a ~30-day window): `cloud-ingest --full` — this enables deletion detection for conversations; never pass `--full` unless the user confirms the export is complete
- Import a specific export directory: `cloud-ingest --export <path>`
- Track export arrival: run `expecting-export`, then use `auto-detect`
- Suppress an unwanted export notice: `dismiss <export-folder>`

Treat `~/.claudex/index` as an archive. Claude Code may prune source JSONLs while their indexed prose remains available.

## Failure handling

- If a session prefix is ambiguous, use a printed candidate ID; never guess.
- If search returns nothing, try a small number of spelling variants or widen the date bound.
- Fall back to raw JSONL inspection only when diagnosing claudex itself.
- If the launcher cannot find the CLI, report that `~/Projects/claudex/claudex` or a `claudex` executable on `PATH` is required.
