# claudex

Splunk Claude session transcripts. Pre-built flat-text index for fast
search; on-demand prettifier for reading; per-session summaries (L3 of the
retrieval pyramid) via `claude -p`.

Two sources, unified search:

- **code** — Claude Code session JSONLs at `~/.claude/projects/<encoded-cwd>/`
- **cloud** — claude.ai data exports (`conversations.json` from a downloaded export)

_Developed with AI assistance. See the git history for which agents contributed._

## How it works

### Code transcripts (always-on)

- **Source:** `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`, plus
  subagent transcripts (`.../<session-id>/subagents/agent-*.jsonl`) and
  workflow subagents (`.../subagents/workflows/<wf-id>/agent-*.jsonl`).
  Workflow `journal.jsonl` event logs are excluded.
- **Index:** `~/.claudex/index/<session-id>.txt` — one ruthless plain-text
  file per session. Tool calls, tool results, thinking blocks, attachments,
  and snapshots are all stripped. Just user and assistant prose.
- **Manifest:** `~/.claudex/manifest.tsv` — quick session list.
- **The index is an archive.** Claude Code prunes old session JSONLs
  (30-day retention by default); the index outlives them and is often the
  only surviving copy. `show` and `summary` fall back to it automatically
  when the source transcript is gone (tool one-liners aren't preserved in
  that path — the index never had them).

### Cloud transcripts (claude.ai export)

- **Source:** request a data export from Anthropic; unzip it; point
  `claudex cloud-ingest` at the directory (or let it auto-find the newest
  `data-*-batch-0000` under `~/Downloads`).
- **Raw cache:** `~/.claudex/cloud/raw/<uuid>.json` — one conversation per
  file, split out from the monolithic `conversations.json`.
- **Index:** `~/.claudex/cloud/index/<uuid>.txt` — same prose-only flat
  text as the code index, plus the conversation's claude.ai-assigned name
  and any attachment text.
- **Manifest:** `~/.claudex/cloud/manifest.tsv`.

### Summaries (L3)

- **Storage:** `~/.claudex/summaries/<sid>.md` (code) or
  `~/.claudex/cloud/summaries/<uuid>.md` (cloud).
- **Format:** minimal frontmatter (`topic`, `outcome`, `keywords`) plus a
  ~500-word TL;DR.
- **Generator:** Haiku 4.5 via `claude -p` (uses your Claude Code OAuth
  session — Max quota, not the API tier). Long sessions are chunked
  recursively: per-slice partial summaries → meta-summary into the final
  structured output.
- **Search integration:** when a summary exists, `claudex search` prints
  the topic line above the excerpts.

## Usage

```bash
# Build / refresh the code-transcript index. Idempotent — only re-writes stale files.
./claudex index

# Ingest a claude.ai export (auto-finds newest data-*-batch-0000 under ~/Downloads).
./claudex cloud-ingest
./claudex cloud-ingest --export /path/to/data-XXXX-batch-0000

# List most recent sessions across both sources.
./claudex list --limit 20
./claudex list --source cloud --limit 50

# Find a thread of conversation. Searches both sources by default;
# results print newest-first.
./claudex search "ninja turtle"
./claudex search "claude\.ai|export"     # regex, case-insensitive
./claudex search "neurospicy" --source cloud
./claudex search "headroom" --limit 5            # only the 5 most recent sessions
./claudex search "export" --since 2026-05        # date filters (ISO prefixes:
./claudex search "export" --until 2026-05-15     #  2026, 2026-05, 2026-05-15 all work)

# Read one session pretty. Prefix IDs are fine; an ambiguous prefix lists
# the candidates instead of guessing.
./claudex show a7efca23
./claudex show agent-a6eafe9024dfca371 --no-tools
./claudex show 02006e08                 # cloud uuid (prefix ok)

# Generate per-session summaries via Haiku (uses Max quota via `claude -p`).
./claudex summarize                      # all uncached sessions in both sources
./claudex summarize a7efca23             # one specific session
./claudex summarize --source cloud       # only cloud
./claudex summarize a7efca23 --force     # re-run even if cached

# Print a cached summary.
./claudex summary a7efca23

# Synthesize across all sessions matching a regex (L4 of the pyramid).
# Pulls every cached summary that hits, asks Haiku to produce a cross-cutting
# narrative digest. Saved to ~/.claudex/syntheses/<slug>-<timestamp>.md.
./claudex synthesize "retrieval pyramid|claudex"
./claudex synthesize "Dancers in the Dark|Morgan|Alex" --source cloud --max 30

# When you kick off a new claude.ai data export, flip the bit:
./claudex expecting-export
# (now SessionStart hook checks on each Code session start until bit clears)

# When you change your mind:
./claudex expecting-export --cancel

# To suppress the hook for an export you don't want to ingest:
./claudex dismiss data-XXXX-batch-0000
```

## Auto-detection of new exports

`claudex auto-detect` is a hook entry point that looks for unprocessed
exports — but only when you've flagged that you're expecting one.
Wire it into Claude Code's `SessionStart` hooks (sibling to the existing
`claudex index` entry):

```json
{
  "hooks": [
    {
      "type": "command",
      "command": "/path/to/claudex auto-detect"
    }
  ]
}
```

Flow:

1. You request a new export from claude.ai.
2. Run `claudex expecting-export`. State persists.
3. On every subsequent Code session, the hook runs `claudex auto-detect`:
   - Bit unset → exits in milliseconds, emits nothing.
   - Bit set → checks `~/Downloads/data-*-batch-0000/` for unprocessed
     exports. If found, emits a one-line notice.
   - If nothing on disk and Fastmail token is configured, searches your
     inbox for an Anthropic export email; emits link if found.
4. Claude (the agent) sees the notice in conversation start context and
   offers to ingest. You say go or not.
5. On successful `cloud-ingest`, the bit clears automatically.

### Fastmail token (optional)

The email-side check uses Fastmail's JMAP API. To enable:

1. Create a JMAP API token at <https://app.fastmail.com/settings/security/tokens>
   with the **read-only** mail scope.
2. Save it to `~/.claudex/secrets/fastmail.token` (one line, just the token),
   or export `FASTMAIL_TOKEN` in your shell.

Without a token, the email check no-ops cleanly — disk-scan still works.

`search` returns matching session IDs with metadata, the topic line (if
summarized), and a few highlighted lines per session. Take an ID, run
`show` or `summary`, read.

## Nightly refresh (code transcripts)

Add to `crontab -e`:

```
0 3 * * * /Users/hljod/Projects/claudex/claudex index >/dev/null 2>&1
```

The cloud index is one-shot per export; re-run `cloud-ingest` whenever you
download a fresh export.

## Tests

```bash
python3 -m unittest discover tests -v
```

Stdlib `unittest`, no deps. All filesystem tests run against a tempdir —
they never touch the real `~/.claudex`.

## Dependencies

- Python 3.10+, standard library only.
- For `summarize`: the `claude` CLI (Claude Code) on PATH, with an active
  login. Auth flows through OAuth so summaries are billed against your
  Claude Pro/Max quota rather than the per-token API tier. Set
  `--model` overrides via `SUMMARIZER_MODEL` in the source if you want
  Sonnet/Opus.

## Adding to PATH (optional)

```bash
ln -s "$PWD/claudex" /usr/local/bin/claudex
# or alias claudex="$PWD/claudex" in ~/.zshrc
```

## Codex skill

The repository includes a Codex skill at `skills/claudex`. Install it globally
from GitHub with:

```bash
python3 ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo tohuw/claudex --path skills/claudex --method git
```

The skill triggers when a user refers to a prior Claude conversation. It uses
the retrieval pyramid: refresh and search the prose-only archive first, inspect
one specific transcript only when necessary, and prefer cached summaries over
loading raw transcript history.
