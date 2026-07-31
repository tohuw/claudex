#!/usr/bin/env python3
"""claudex — splunk Claude Code session transcripts.

Layout under ~/.claude/projects/<encoded-cwd>/<session>.jsonl
We build a flat searchable text mirror at ~/.claudex/index/<session>.txt
and prettify on demand."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

PROJECTS_DIR = Path.home() / ".claude" / "projects"
INDEX_DIR = Path.home() / ".claudex" / "index"
SUMMARIES_DIR = Path.home() / ".claudex" / "summaries"
MANIFEST = Path.home() / ".claudex" / "manifest.tsv"

CLOUD_RAW_DIR = Path.home() / ".claudex" / "cloud" / "raw"
CLOUD_INDEX_DIR = Path.home() / ".claudex" / "cloud" / "index"
CLOUD_SUMMARIES_DIR = Path.home() / ".claudex" / "cloud" / "summaries"
CLOUD_MANIFEST = Path.home() / ".claudex" / "cloud" / "manifest.tsv"

CLOUD_PROJ_RAW_DIR = Path.home() / ".claudex" / "cloud" / "projects" / "raw"
CLOUD_PROJ_INDEX_DIR = Path.home() / ".claudex" / "cloud" / "projects" / "index"
CLOUD_PROJ_MEMORY_DIR = Path.home() / ".claudex" / "cloud" / "projects" / "memory"
CLOUD_PROJ_MANIFEST = Path.home() / ".claudex" / "cloud" / "projects" / "manifest.tsv"
CLOUD_MEMORY_RAW = Path.home() / ".claudex" / "cloud" / "memory" / "memories.json"
CLOUD_MEMORY_INDEX_DIR = Path.home() / ".claudex" / "cloud" / "memory" / "index"

# Deleted tier: content that vanished from the newest claude.ai export lives
# here — excluded from normal search/list, reachable via --deleted and `show`.
CLOUD_INDEX_DELETED_DIR = Path.home() / ".claudex" / "cloud" / "index-deleted"
CLOUD_MANIFEST_DELETED = Path.home() / ".claudex" / "cloud" / "manifest-deleted.tsv"
CLOUD_PROJ_INDEX_DELETED_DIR = Path.home() / ".claudex" / "cloud" / "projects" / "index-deleted"
CLOUD_PROJ_MANIFEST_DELETED = Path.home() / ".claudex" / "cloud" / "projects" / "manifest-deleted.tsv"
CLOUD_MEMORY_INDEX_DELETED_DIR = Path.home() / ".claudex" / "cloud" / "memory" / "index-deleted"

SYNTHESES_DIR = Path.home() / ".claudex" / "syntheses"
STATE_FILE = Path.home() / ".claudex" / "state.json"
FASTMAIL_TOKEN_FILE = Path.home() / ".claudex" / "secrets" / "fastmail.token"
EXPECT_STALE_DAYS = 14  # auto-warn if expecting-export bit is older than this

# Cap per-attachment chars when rendering. Some claude.ai conversations include
# 200K+ char single-message attachments which can't be split at turn boundaries
# and bust the per-message context limit during summarization.
ATTACHMENT_MAX_CHARS = 30_000

SUMMARIZER_MODEL = "claude-haiku-4-5"
# Char-based proxy for token count (~3 chars/token for mixed content).
# Haiku 4.5 has a 200K context window; reserve headroom for prompt overhead + output.
DIRECT_MAX_CHARS = 500_000   # ~167K tokens — single-shot if transcript fits
CHUNK_TARGET_CHARS = 400_000  # ~133K tokens per slice

PARTIAL_SUMMARIZER_SYSTEM = """You summarize one slice of a longer Claude conversation transcript.

Output a dense ~300-word factual digest of just this slice. Capture topics covered, decisions or pivots, specific tools/files/commands mentioned, and any unresolved threads. Plain paragraphs only — no frontmatter, no headers, no preamble. A downstream meta-summarizer combines your output with sibling slices into the final structured summary.
"""

META_SUMMARIZER_INTRO = "These are sequential partial summaries of slices of one long Claude conversation. Synthesize them into the standard final summary in the format described below.\n\n"

SYNTHESIZER_SYSTEM = """You synthesize across multiple Claude conversations on a related topic.

Given a list of per-session summaries, produce a single narrative digest that captures:
- The arc / evolution of thinking across sessions
- Recurring themes, decisions, or pivots
- Open threads that span multiple sessions
- Notable contradictions or refinements over time

Output exactly this format, no preamble:

---
query: <verbatim search query>
n_sessions: <count>
generated: <ISO 8601 timestamp>
---

# <one-line headline capturing the synthesis>

<400-800 word narrative digest. Cite session IDs in parens when referencing specifics, e.g., "(a7efca23)". Lead with the strongest thread; don't try to mention every session. Direct prose, no hedging, no framing language.>
"""

SUMMARIZER_SYSTEM = """You distill Claude session transcripts into compact summaries.

The transcript shows a user/assistant conversation. Tool calls are collapsed to one-line markers (→ Bash: ..., → Edit: file.py, ...). Use those markers as evidence of what actually happened.

Output exactly this format, no preamble or trailing commentary:

---
topic: <one line, ~10-15 words, what this session was about>
outcome: <one of: resolved | abandoned | ongoing | reference>
keywords: [k1, k2, k3]
---

<200-500 word TL;DR>

TL;DR rules:
- State what the user wanted and what they got.
- Name specific files, commands, tools, or decisions when load-bearing.
- Note pivots, failed approaches, and unresolved threads.
- Dense and direct. No hedging, no framing, no "the user and assistant discussed".

outcome tags:
- resolved: a working thing shipped, or a question got a definitive answer
- abandoned: gave up, pivoted away, or scope-deferred
- ongoing: paused mid-work, expected to resume
- reference: exploratory Q&A or design discussion with no concrete work product

keywords: 3-7 lowercase tags, hyphenated multi-words ("claude-api", "cron-setup"). Skip generic ones ("code", "help", "session").
"""


def iter_events(jsonl: Path) -> Iterator[dict]:
    with jsonl.open(errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def extract_text_blocks(content) -> list[str]:
    """Pull human-readable text from a message.content (str or list)."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            txt = block.get("text", "")
            if txt:
                out.append(txt)
        elif t == "image":
            out.append("[image]")
    return out


def summarize_tool_use(block: dict) -> str:
    name = block.get("name", "tool")
    inp = block.get("input", {}) or {}
    if name == "Bash":
        cmd = inp.get("command", "")
        return f"→ Bash: {cmd[:200]}"
    if name in ("Read", "Edit", "Write"):
        return f"→ {name}: {inp.get('file_path', '')}"
    if name in ("Grep", "Glob"):
        return f"→ {name}: {inp.get('pattern', '')}"
    if name == "Agent":
        return f"→ Agent({inp.get('subagent_type', 'general')}): {inp.get('description', '')}"
    if name == "WebFetch":
        return f"→ WebFetch: {inp.get('url', '')}"
    if name == "WebSearch":
        return f"→ WebSearch: {inp.get('query', '')}"
    if name == "TodoWrite" or name.startswith("Task"):
        return f"→ {name}"
    keys = ", ".join(list(inp.keys())[:3])
    return f"→ {name}({keys})"


def summarize_tool_result(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        n = len(content)
    elif isinstance(content, list):
        n = sum(len(b.get("text", "")) for b in content if isinstance(b, dict))
    else:
        n = 0
    err = " err" if block.get("is_error") else ""
    return f"← result ({n}B){err}"


@dataclass
class Turn:
    role: str  # "user" | "assistant" | "tool"
    text: str
    timestamp: str
    tool_lines: list[str]  # only for assistant turns when prettifying


def all_session_jsonls() -> list[Path]:
    """Every conversation transcript under PROJECTS_DIR: main sessions,
    subagents, and workflow subagents (nested one level deeper under
    subagents/workflows/<wf_id>/). journal.jsonl files are workflow event
    logs, not conversations — excluded."""
    if not PROJECTS_DIR.exists():
        return []
    return sorted(
        p for p in PROJECTS_DIR.glob("**/*.jsonl")
        if p.name != "journal.jsonl"
    )


def _parent_session_of(jsonl: Path) -> str:
    """For a subagent transcript, the session id is the path component just
    before 'subagents' (works for both direct and workflow-nested agents)."""
    parts = jsonl.parts
    try:
        return parts[parts.index("subagents") - 1]
    except ValueError:
        return ""


def parse_session(jsonl: Path) -> tuple[dict, list[Turn]]:
    """Return (meta, turns). meta has cwd, start_ts, end_ts, branch, session_id, n_user."""
    is_subagent = "subagents" in jsonl.parts
    meta = {
        "session_id": jsonl.stem,
        "cwd": "",
        "start_ts": "",
        "end_ts": "",
        "branch": "",
        "version": "",
        "n_user": 0,
        "n_assistant": 0,
        "kind": "subagent" if is_subagent else "session",
        "parent_session": _parent_session_of(jsonl) if is_subagent else "",
    }
    turns: list[Turn] = []
    for ev in iter_events(jsonl):
        et = ev.get("type")
        ts = ev.get("timestamp", "")
        if not meta["start_ts"] and ts:
            meta["start_ts"] = ts
        if ts:
            meta["end_ts"] = ts
        if not meta["cwd"]:
            meta["cwd"] = ev.get("cwd", "") or meta["cwd"]
        if not meta["branch"]:
            meta["branch"] = ev.get("gitBranch", "") or meta["branch"]
        if not meta["version"]:
            meta["version"] = ev.get("version", "") or meta["version"]

        if ev.get("isSidechain") and not is_subagent:
            # subagent traffic from a main session — skip; the subagent has its own file
            continue

        if et == "user":
            msg = ev.get("message", {}) or {}
            content = msg.get("content")
            # detect "user" turns that are actually tool_result containers
            if isinstance(content, list) and content and isinstance(content[0], dict) and content[0].get("type") == "tool_result":
                # tool result — skip in index
                continue
            texts = extract_text_blocks(content)
            text = "\n".join(t for t in texts if t).strip()
            if not text:
                continue
            meta["n_user"] += 1
            turns.append(Turn("user", text, ts, []))
        elif et == "assistant":
            msg = ev.get("message", {}) or {}
            content = msg.get("content") or []
            text_parts: list[str] = []
            tool_lines: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    txt = block.get("text", "")
                    if txt:
                        text_parts.append(txt)
                elif bt == "tool_use":
                    tool_lines.append(summarize_tool_use(block))
                # thinking blocks: dropped
            text = "\n".join(text_parts).strip()
            if not text and not tool_lines:
                continue
            meta["n_assistant"] += 1
            turns.append(Turn("assistant", text, ts, tool_lines))
        # everything else (attachment, file-history-snapshot, permission-mode): ignore
    return meta, turns


def write_index_file(jsonl: Path, force: bool = False) -> bool:
    """Build flat-text index for a session. Returns True if written."""
    out = INDEX_DIR / f"{jsonl.stem}.txt"
    if not force and out.exists() and out.stat().st_mtime >= jsonl.stat().st_mtime:
        return False
    meta, turns = parse_session(jsonl)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        f.write(f"# session: {meta['session_id']}\n")
        f.write(f"# kind: {meta['kind']}\n")
        if meta["parent_session"]:
            f.write(f"# parent: {meta['parent_session']}\n")
        f.write(f"# cwd: {meta['cwd']}\n")
        f.write(f"# start: {meta['start_ts']}\n")
        f.write(f"# end: {meta['end_ts']}\n")
        f.write(f"# branch: {meta['branch']}\n")
        f.write(f"# turns: user={meta['n_user']} assistant={meta['n_assistant']}\n")
        f.write("\n")
        for turn in turns:
            if turn.role == "user":
                f.write(f"[USER {turn.timestamp}]\n{turn.text}\n\n")
            else:
                # index excludes tool one-liners (keep prose-only for grep)
                if turn.text:
                    f.write(f"[ASSISTANT {turn.timestamp}]\n{turn.text}\n\n")
    return True


def render_show(meta: dict, turns: list, no_tools: bool = False) -> str:
    """Render parsed session as markdown — same as cmd_show writes to stdout."""
    parts = [f"# {meta['session_id']}\n\n"]
    if meta.get("name"):
        parts.append(f"- **name:** {meta['name']}\n")
    if meta.get("cwd"):
        parts.append(f"- **cwd:** `{meta['cwd']}`\n")
    parts.append(f"- **start:** {meta['start_ts']}\n")
    parts.append(f"- **end:** {meta['end_ts']}\n")
    if meta.get("branch"):
        parts.append(f"- **branch:** {meta['branch']}\n")
    parts.append(f"- **turns:** user={meta['n_user']} assistant={meta['n_assistant']}\n\n")
    parts.append("---\n\n")
    for turn in turns:
        if turn.role == "user":
            parts.append(f"## 👤 user · {turn.timestamp}\n\n{turn.text}\n\n")
        else:
            parts.append(f"## 🤖 assistant · {turn.timestamp}\n\n")
            if turn.text:
                parts.append(turn.text + "\n\n")
            if turn.tool_lines and not no_tools:
                for tl in turn.tool_lines:
                    parts.append(f"`{tl}`\n")
                parts.append("\n")
    return "".join(parts)


def parse_index_file(txt: Path) -> tuple[dict, list[Turn]]:
    """Rehydrate (meta, turns) from a flat index file.

    Fallback for sessions whose source JSONL was pruned by Claude Code's
    transcript retention — the index is the only surviving copy. Tool
    one-liners are not in the index, so rehydrated turns carry none."""
    raw = _read_meta(txt)
    tm = re.match(r"user=(\d+) assistant=(\d+)", raw.get("turns", ""))
    meta = {
        "session_id": raw.get("session", txt.stem),
        "name": raw.get("name", ""),
        "cwd": raw.get("cwd", ""),
        "start_ts": raw.get("start", ""),
        "end_ts": raw.get("end", ""),
        "branch": raw.get("branch", ""),
        "n_user": int(tm.group(1)) if tm else 0,
        "n_assistant": int(tm.group(2)) if tm else 0,
        "kind": raw.get("kind", "session"),
        "parent_session": raw.get("parent", ""),
    }
    turns: list[Turn] = []
    cur_role, cur_ts = None, ""
    buf: list[str] = []

    def flush():
        if cur_role in ("user", "assistant"):
            text = "\n".join(buf).strip()
            if text:
                turns.append(Turn(cur_role, text, cur_ts, []))

    for line in txt.read_text(errors="replace").splitlines():
        m = re.match(r"\[(USER|ASSISTANT|NAME)(?: ([^\]]*))?\]$", line)
        if m:
            flush()
            cur_role = m.group(1).lower()
            cur_ts = m.group(2) or ""
            buf = []
        elif cur_role is not None:
            buf.append(line)
    flush()
    return meta, turns


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


DISALLOWED_TOOLS = "Bash Read Write Edit Glob Grep Agent WebFetch WebSearch TodoWrite Skill ToolSearch NotebookEdit"


def call_summarizer(_unused: str, system: str, user: str, max_tokens: int = 1500) -> str:
    """Shell out to `claude -p` (uses OAuth/Max, not API key). Returns assistant text.

    The signature keeps a leading positional arg for compatibility with prior callers
    that passed the API key — it is ignored. Auth comes from the user's logged-in
    Claude Code session via OAuth.
    """
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # force OAuth/Max instead of API-key path
    cmd = [
        "claude", "-p",
        "--model", SUMMARIZER_MODEL,
        "--system-prompt", system,
        "--disallowedTools", *DISALLOWED_TOOLS.split(),
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=user,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"claude -p timed out after 600s") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()[:500]
        raise RuntimeError(f"claude -p exit {proc.returncode}: {err}")
    return proc.stdout.strip()


# legacy alias — older code paths still reference this name
call_anthropic = call_summarizer


def split_transcript_chunks(transcript: str, target_chars: int = CHUNK_TARGET_CHARS) -> list[str]:
    """Split rendered show output into chunks at turn boundaries (## lines).
    Each chunk keeps the metadata header from the top of the transcript."""
    lines = transcript.split("\n")
    header_end = 0
    for i, line in enumerate(lines):
        if line.startswith("## "):
            header_end = i
            break
    header = "\n".join(lines[:header_end]) + "\n" if header_end else ""
    body_lines = lines[header_end:]
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in body_lines:
        if line.startswith("## ") and current_size >= target_chars and current:
            chunks.append(header + "\n".join(current))
            current = []
            current_size = 0
        current.append(line)
        current_size += len(line) + 1
    if current:
        chunks.append(header + "\n".join(current))
    return chunks


def generate_summary(transcript: str, header_msg: str, api_key: str) -> tuple[str, int]:
    """Return (final_summary_text, n_chunks). n_chunks=1 means single-shot."""
    if len(transcript) <= DIRECT_MAX_CHARS:
        user_msg = f"{header_msg}Session transcript:\n\n{transcript}"
        return call_anthropic(api_key, SUMMARIZER_SYSTEM, user_msg), 1
    chunks = split_transcript_chunks(transcript)
    partials: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        slice_msg = f"{header_msg}Slice {i} of {len(chunks)}:\n\n{chunk}"
        partial = call_anthropic(api_key, PARTIAL_SUMMARIZER_SYSTEM, slice_msg, max_tokens=800)
        partials.append(f"[slice {i}/{len(chunks)}]\n{partial}")
    combined = "\n\n".join(partials)
    final_msg = f"{header_msg}{META_SUMMARIZER_INTRO}{combined}"
    return call_anthropic(api_key, SUMMARIZER_SYSTEM, final_msg), len(chunks)


def summary_path(session_id: str, source: str = "code") -> Path:
    base = SUMMARIES_DIR if source == "code" else CLOUD_SUMMARIES_DIR
    return base / f"{session_id}.md"


def read_summary_frontmatter(session_id: str, source: str = "code") -> dict:
    """Parse the YAML-ish frontmatter at the top of a summary file.
    Returns a dict (possibly empty). Keys: topic, outcome, keywords (list)."""
    path = summary_path(session_id, source)
    if not path.exists():
        return {}
    fm: dict = {}
    in_fm = False
    for line in path.read_text(errors="replace").splitlines():
        if line.strip() == "---":
            if in_fm:
                break
            in_fm = True
            continue
        if not in_fm:
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key == "keywords":
            # accept either "[a, b, c]" or "a, b, c"
            val = val.strip("[]")
            fm[key] = [normalize_keyword(k) for k in val.split(",") if k.strip()]
        else:
            fm[key] = val
    return fm


def read_summary_topic(session_id: str, source: str = "code") -> str | None:
    return read_summary_frontmatter(session_id, source).get("topic") or None


def normalize_keyword(k: str) -> str:
    k = k.strip().lower().strip('"\'')
    k = re.sub(r"[\s_]+", "-", k)
    k = re.sub(r"-+", "-", k)
    return k.strip("-")


# ----- claude.ai export ingest -----

def _cloud_tool_line(block: dict) -> str:
    name = block.get("name", "tool")
    inp = block.get("input") or {}
    if isinstance(inp, dict):
        for k in ("command", "url", "path", "query", "file_path"):
            if k in inp and inp[k]:
                return f"→ {name}: {str(inp[k])[:200]}"
        keys = ", ".join(list(inp.keys())[:3])
        return f"→ {name}({keys})"
    return f"→ {name}"


def parse_cloud_conversation(conv: dict) -> tuple[dict, list[Turn]]:
    meta = {
        "session_id": conv.get("uuid", ""),
        "cwd": "",  # n/a for cloud
        "name": conv.get("name", ""),
        "start_ts": conv.get("created_at", ""),
        "end_ts": conv.get("updated_at", ""),
        "branch": "",
        "version": "",
        "n_user": 0,
        "n_assistant": 0,
        "kind": "cloud",
        "parent_session": "",
    }
    turns: list[Turn] = []
    for msg in conv.get("chat_messages") or []:
        sender = msg.get("sender")
        ts = msg.get("created_at", "")
        text = (msg.get("text") or "").strip()
        content = msg.get("content") or []
        if not text and isinstance(content, list):
            text = "\n".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
            ).strip()
        attachments = msg.get("attachments") or []
        for att in attachments:
            ec = (att.get("extracted_content") or "").strip()
            if ec:
                fn = att.get("file_name", "attachment")
                if len(ec) > ATTACHMENT_MAX_CHARS:
                    ec = ec[:ATTACHMENT_MAX_CHARS] + f"\n[...truncated; full attachment {len(ec)} chars]"
                text += f"\n\n[attachment: {fn}]\n{ec}"
        tool_lines: list[str] = []
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tool_lines.append(_cloud_tool_line(b))
        if not text and not tool_lines:
            continue
        if sender == "human":
            meta["n_user"] += 1
            turns.append(Turn("user", text, ts, []))
        elif sender == "assistant":
            meta["n_assistant"] += 1
            turns.append(Turn("assistant", text, ts, tool_lines))
    return meta, turns


def write_cloud_index(meta: dict, turns: list[Turn], out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        f.write(f"# session: {meta['session_id']}\n")
        f.write(f"# kind: cloud\n")
        if meta.get("name"):
            f.write(f"# name: {meta['name']}\n")
        f.write(f"# start: {meta['start_ts']}\n")
        f.write(f"# end: {meta['end_ts']}\n")
        f.write(f"# turns: user={meta['n_user']} assistant={meta['n_assistant']}\n")
        f.write("\n")
        if meta.get("name"):
            f.write(f"[NAME]\n{meta['name']}\n\n")
        for turn in turns:
            if turn.role == "user":
                f.write(f"[USER {turn.timestamp}]\n{turn.text}\n\n")
            else:
                if turn.text:
                    f.write(f"[ASSISTANT {turn.timestamp}]\n{turn.text}\n\n")


def update_cloud_manifest():
    for index_dir, manifest in ((CLOUD_INDEX_DIR, CLOUD_MANIFEST),
                                (CLOUD_INDEX_DELETED_DIR, CLOUD_MANIFEST_DELETED)):
        rows = []
        for ix in sorted(index_dir.glob("*.txt")) if index_dir.exists() else []:
            meta = _read_meta(ix)
            rows.append((
                meta.get("session", ix.stem),
                meta.get("start", ""),
                meta.get("end", ""),
                meta.get("name", ""),
                meta.get("turns", ""),
            ))
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("w") as f:
            f.write("session_id\tstart\tend\tname\tturns\n")
            for r in rows:
                f.write("\t".join(r) + "\n")


def cloud_raw_path(uuid: str) -> Path:
    return CLOUD_RAW_DIR / f"{uuid}.json"


# ----- claude.ai projects + memories ingest -----

def _memhash(text: str) -> str:
    """Short content hash for project memory text; '-' when empty."""
    if not text:
        return "-"
    import hashlib
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def render_project_index(proj: dict, memory_text: str) -> str:
    docs = proj.get("docs") or []
    lines = [
        f"# project: {proj.get('uuid', '')}",
        "# kind: project",
        f"# name: {proj.get('name', '')}",
        f"# start: {proj.get('created_at', '')}",
        f"# end: {proj.get('updated_at', '')}",
        f"# docs: {len(docs)}",
        f"# memhash: {_memhash(memory_text)}",
        "",
    ]
    if proj.get("name"):
        lines += ["[NAME]", proj["name"], ""]
    if proj.get("description"):
        lines += ["[DESCRIPTION]", proj["description"], ""]
    if proj.get("prompt_template"):
        lines += ["[PROMPT]", proj["prompt_template"], ""]
    if memory_text:
        lines += ["[MEMORY]", memory_text, ""]
    for d in docs:
        content = (d.get("content") or "").strip()
        lines += [f"[DOC {d.get('filename', 'untitled')}]", content, ""]
    return "\n".join(lines) + "\n"


def load_export_memories(export: Path) -> dict | None:
    """First entry of the export's memories.json, or None if absent/unreadable."""
    mf = export / "memories.json"
    if not mf.exists():
        return None
    try:
        entries = json.loads(mf.read_text(errors="replace"))
    except Exception as e:
        print(f"memories.json unreadable: {e}", file=sys.stderr)
        return None
    return entries[0] if isinstance(entries, list) and entries else None


def _memory_slug(path: str) -> str:
    slug = path.strip("/").replace("/", "-")
    return re.sub(r"[^\w.-]+", "-", slug) or "unnamed"


def write_memory_indexes(mem: dict) -> set[str]:
    """Flatten conversations_memory + memory_files into the memory index.
    Returns the slugs written — the upstream-active memory set. Entries no
    longer present upstream are moved to the deleted tier by the caller's
    sync pass, not erased."""
    CLOUD_MEMORY_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    slugs: set[str] = set()
    conv = (mem.get("conversations_memory") or "").strip()
    if conv:
        atomic_write(
            CLOUD_MEMORY_INDEX_DIR / "conversations-memory.txt",
            "# kind: memory\n# name: conversations memory\n# scope: global\n\n" + conv + "\n")
        slugs.add("conversations-memory")
    for mfile in mem.get("memory_files") or []:
        path = mfile.get("path") or "unnamed"
        content = (mfile.get("content") or "").strip()
        if not content:
            continue
        slug = _memory_slug(path)
        atomic_write(
            CLOUD_MEMORY_INDEX_DIR / f"{slug}.txt",
            f"# kind: memory\n# name: {path}\n# scope: file\n\n" + content + "\n")
        slugs.add(slug)
    return slugs


def update_proj_manifest():
    for index_dir, manifest in ((CLOUD_PROJ_INDEX_DIR, CLOUD_PROJ_MANIFEST),
                                (CLOUD_PROJ_INDEX_DELETED_DIR, CLOUD_PROJ_MANIFEST_DELETED)):
        rows = []
        for ix in sorted(index_dir.glob("*.txt")) if index_dir.exists() else []:
            meta = _read_meta(ix)
            rows.append((
                meta.get("project", ix.stem),
                meta.get("start", ""),
                meta.get("end", ""),
                meta.get("name", ""),
                meta.get("docs", ""),
                "yes" if meta.get("memhash", "-") != "-" else "no",
            ))
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("w") as f:
            f.write("project_id\tstart\tend\tname\tdocs\tmemory\n")
            for r in rows:
                f.write("\t".join(r) + "\n")


def render_project_memory_tombstone(proj: dict, memory_text: str) -> str:
    return "\n".join([
        f"# project: {proj.get('uuid', '')}",
        "# kind: project-memory",
        f"# name: {proj.get('name', '')} (project memory — cleared upstream)",
        f"# end: {proj.get('updated_at', '')}",
        "",
        "[MEMORY]",
        memory_text,
        "",
    ]) + "\n"


def _sync_deleted_tier(active_ids: set[str] | None, index_dir: Path, deleted_dir: Path) -> tuple[int, int]:
    """Move indexes whose id is gone upstream into deleted_dir; restore ones
    that reappeared. active_ids=None means this tier's upstream state is
    unknown — no-op. Returns (n_moved, n_restored)."""
    if active_ids is None:
        return 0, 0
    n_moved = n_restored = 0
    if index_dir.exists():
        for p in list(index_dir.glob("*.txt")):
            if p.stem not in active_ids:
                deleted_dir.mkdir(parents=True, exist_ok=True)
                p.rename(deleted_dir / p.name)
                n_moved += 1
    if deleted_dir.exists():
        for p in list(deleted_dir.glob("*.txt")):
            if p.stem in active_ids:
                tgt = index_dir / p.name
                if tgt.exists():
                    p.unlink()  # a fresh copy was just ingested
                else:
                    p.rename(tgt)
                n_restored += 1
    return n_moved, n_restored


def ingest_projects_and_memories(export: Path, force: bool = False) -> dict:
    """Ingest memories.json and projects/*.json from a claude.ai export.
    Project memories are embedded into their project's index file; the global
    conversations memory and memory files get their own index. Returns counts."""
    counts = {"proj_written": 0, "proj_cached": 0, "proj_total": 0,
              "proj_with_memory": 0, "memory_files": 0,
              "proj_ids": None, "mem_slugs": None, "max_updated": ""}
    mem = load_export_memories(export)
    if mem:
        CLOUD_MEMORY_RAW.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(CLOUD_MEMORY_RAW, json.dumps(mem))
        counts["mem_slugs"] = write_memory_indexes(mem)
        counts["memory_files"] = len(counts["mem_slugs"])
    proj_memories = (mem or {}).get("project_memories") or {}
    proj_dir = export / "projects"
    proj_files = sorted(proj_dir.glob("*.json")) if proj_dir.is_dir() else []
    counts["proj_total"] = len(proj_files)
    if not proj_files:
        if proj_memories:
            print(f"  note: {len(proj_memories)} project memories but no projects/ dir — not indexed", file=sys.stderr)
        return counts
    counts["proj_ids"] = set()
    CLOUD_PROJ_RAW_DIR.mkdir(parents=True, exist_ok=True)
    CLOUD_PROJ_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    for pf in proj_files:
        try:
            proj = json.loads(pf.read_text(errors="replace"))
        except Exception as e:
            print(f"  skipping {pf.name}: {e}", file=sys.stderr)
            continue
        uuid = proj.get("uuid")
        if not uuid:
            continue
        counts["proj_ids"].add(uuid)
        counts["max_updated"] = max(counts["max_updated"], proj.get("updated_at", ""))
        memory_text = (proj_memories.get(uuid) or "").strip()
        # sidecar keeps the last-known memory forever; a memory cleared
        # upstream moves to a deleted-tier tombstone instead of the index
        side = CLOUD_PROJ_MEMORY_DIR / f"{uuid}.md"
        tomb = CLOUD_PROJ_INDEX_DELETED_DIR / f"{uuid}.memory.txt"
        if memory_text:
            CLOUD_PROJ_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write(side, memory_text + "\n")
            tomb.unlink(missing_ok=True)
            counts["proj_with_memory"] += 1
        elif mem is not None and side.exists():
            CLOUD_PROJ_INDEX_DELETED_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write(tomb, render_project_memory_tombstone(
                proj, side.read_text(errors="replace").strip()))
        index_out = CLOUD_PROJ_INDEX_DIR / f"{uuid}.txt"
        deleted_out = CLOUD_PROJ_INDEX_DELETED_DIR / f"{uuid}.txt"
        # idempotency: same updated_at and (when memories present) same memory
        # hash — also honored for a copy currently in the deleted tier
        if not force:
            existing_at = index_out if index_out.exists() else (deleted_out if deleted_out.exists() else None)
            if existing_at:
                existing = _read_meta(existing_at)
                same_end = existing.get("end") == proj.get("updated_at", "")
                same_mem = mem is None or existing.get("memhash", "-") == _memhash(memory_text)
                if same_end and same_mem:
                    counts["proj_cached"] += 1
                    continue
        atomic_write(CLOUD_PROJ_RAW_DIR / f"{uuid}.json", json.dumps(proj))
        atomic_write(index_out, render_project_index(proj, memory_text))
        counts["proj_written"] += 1
    orphans = [u for u in proj_memories if u not in counts["proj_ids"]]
    if orphans:
        print(f"  note: {len(orphans)} project memories reference unknown projects — not indexed", file=sys.stderr)
    return counts


# ----- state.json: tracks expecting-export bit, ingested/dismissed exports -----

def read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def write_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(STATE_FILE, json.dumps(state, indent=2, sort_keys=True))


def find_unprocessed_exports() -> list[Path]:
    """Return export folders under ~/Downloads that have conversations.json
    and aren't recorded as ingested or dismissed."""
    state = read_state()
    ingested = set(state.get("ingested", {}).keys())
    dismissed = set(state.get("dismissed", {}).keys())
    out: list[Path] = []
    for cand in sorted(Path.home().glob("Downloads/data-*-batch-0000")):
        if not (cand / "conversations.json").exists():
            continue
        if cand.name in ingested or cand.name in dismissed:
            continue
        out.append(cand)
    return out


# ----- Fastmail JMAP — search inbox for Anthropic export emails -----

def _read_fastmail_token() -> str | None:
    tok = os.environ.get("FASTMAIL_TOKEN")
    if tok:
        return tok.strip()
    if FASTMAIL_TOKEN_FILE.exists():
        return FASTMAIL_TOKEN_FILE.read_text().strip()
    return None


def fastmail_search_export_email() -> dict | None:
    """Look for a recent Anthropic data-export email via JMAP.
    Returns a dict {subject, from, received_at, unread, body, urls} or None.
    Cleanly no-ops (returns None) when no token is configured."""
    token = _read_fastmail_token()
    if not token:
        return None
    import urllib.request
    import urllib.error

    def jmap_post(url: str, body: dict) -> dict:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())

    try:
        # 1. Discover session — gives us API URL and account id
        sess_req = urllib.request.Request(
            "https://api.fastmail.com/jmap/session",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(sess_req, timeout=20) as resp:
            session = json.loads(resp.read())
        api_url = session["apiUrl"]
        account_id = next(iter(session["primaryAccounts"].values()))

        # 2. Search for recent Anthropic emails
        search = jmap_post(api_url, {
            "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
            "methodCalls": [
                ["Email/query", {
                    "accountId": account_id,
                    "filter": {
                        "from": "anthropic.com",
                        "text": "export",
                    },
                    "sort": [{"property": "receivedAt", "isAscending": False}],
                    "limit": 5,
                }, "0"],
                ["Email/get", {
                    "accountId": account_id,
                    "#ids": {"resultOf": "0", "name": "Email/query", "path": "/ids"},
                    "properties": ["subject", "from", "receivedAt", "keywords", "preview", "bodyValues", "textBody", "htmlBody"],
                    "fetchTextBodyValues": True,
                    "fetchHTMLBodyValues": True,
                }, "1"],
            ],
        })
    except Exception as e:
        print(f"[claudex] fastmail lookup failed: {e}", file=sys.stderr)
        return None

    emails = []
    for r in search.get("methodResponses", []):
        if r[0] == "Email/get":
            emails = r[1].get("list", [])
            break
    if not emails:
        return None
    # Pick the newest "your data export is ready"-ish email
    for e in emails:
        subj = (e.get("subject") or "").lower()
        if "export" in subj or "data" in subj:
            best = e
            break
    else:
        best = emails[0]
    body_text = ""
    for part in best.get("textBody", []) or best.get("htmlBody", []):
        bv = (best.get("bodyValues") or {}).get(part.get("partId"))
        if bv and bv.get("value"):
            body_text += bv["value"]
    urls = re.findall(r"https?://[^\s\"'<>)]+", body_text)
    return {
        "subject": best.get("subject"),
        "from": ", ".join((a.get("email") or "") for a in (best.get("from") or [])),
        "received_at": best.get("receivedAt"),
        "unread": "$seen" not in (best.get("keywords") or {}),
        "preview": (best.get("preview") or "")[:200],
        "urls": urls[:10],
    }


def cmd_expecting_export(args):
    state = read_state()
    if args.cancel:
        if "expecting_export" in state:
            del state["expecting_export"]
            write_state(state)
            print("expecting-export bit cleared")
        else:
            print("no expecting-export bit was set", file=sys.stderr)
        return 0
    from datetime import datetime, timezone
    state["expecting_export"] = {"requested_at": datetime.now(timezone.utc).isoformat()}
    write_state(state)
    print("expecting-export bit set; SessionStart hook will now check on each Code session")
    return 0


def cmd_dismiss(args):
    state = read_state()
    state.setdefault("dismissed", {})
    from datetime import datetime, timezone
    state["dismissed"][args.export_name] = datetime.now(timezone.utc).isoformat()
    write_state(state)
    print(f"dismissed: {args.export_name}")
    return 0


def cmd_auto_detect(args):
    """Hook entry point. Fast no-op when expecting-export bit is unset.
    When set: scan Downloads + (optionally) Fastmail inbox, emit one-line notices."""
    state = read_state()
    expecting = state.get("expecting_export")
    if not expecting:
        return 0
    from datetime import datetime, timezone, timedelta
    notices: list[str] = []
    # Disk check (always when bit set; fast)
    fresh = find_unprocessed_exports()
    if fresh:
        for p in fresh:
            size_mb = sum(c.stat().st_size for c in p.glob("*") if c.is_file()) / 1e6
            notices.append(f"[claudex] unprocessed claude.ai export at {p} (~{size_mb:.0f}MB) — offer ingest?")
    # Email check (only when nothing on disk yet, and only if token configured)
    if not fresh and not args.no_email:
        info = fastmail_search_export_email()
        if info:
            tag = "UNREAD" if info["unread"] else "read"
            url_hint = f" link: {info['urls'][0]}" if info["urls"] else ""
            notices.append(f"[claudex] Anthropic email {tag}: \"{info['subject']}\" ({info['received_at']}).{url_hint}")
    # Stale-bit warning
    try:
        requested_at = datetime.fromisoformat(expecting["requested_at"].replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - requested_at
        if age > timedelta(days=EXPECT_STALE_DAYS) and not fresh:
            notices.append(f"[claudex] expecting-export bit set {age.days} days ago — still waiting? `claudex expecting-export --cancel` to clear.")
    except Exception:
        pass
    for n in notices:
        print(n)
    return 0


def cmd_cloud_ingest(args):
    export = Path(args.export).expanduser() if args.export else None
    if not export:
        cands = sorted(Path.home().glob("Downloads/data-*-batch-0000"))
        cands = [c for c in cands if (c / "conversations.json").exists()]
        if not cands:
            print("no claude.ai export found under ~/Downloads/data-*-batch-0000", file=sys.stderr)
            print("pass --export <path> to point at one explicitly", file=sys.stderr)
            return 1
        export = cands[-1]
        print(f"using export: {export}")
    convs_file = export / "conversations.json"
    if not convs_file.exists():
        print(f"no conversations.json under {export}", file=sys.stderr)
        return 1
    print(f"loading {convs_file.stat().st_size / 1e6:.0f}MB ...")
    with convs_file.open() as f:
        convs = json.load(f)
    print(f"  {len(convs)} conversations")
    CLOUD_RAW_DIR.mkdir(parents=True, exist_ok=True)
    CLOUD_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    n_written = n_cached = n_empty = 0
    for conv in convs:
        uuid = conv.get("uuid")
        if not uuid:
            continue
        raw_out = cloud_raw_path(uuid)
        index_out = CLOUD_INDEX_DIR / f"{uuid}.txt"
        deleted_out = CLOUD_INDEX_DELETED_DIR / f"{uuid}.txt"
        updated_at = conv.get("updated_at", "")
        # idempotency: skip if an index (active or deleted tier) has same end
        if not args.force:
            existing_at = index_out if index_out.exists() else (deleted_out if deleted_out.exists() else None)
            if existing_at and _read_meta(existing_at).get("end") == updated_at:
                n_cached += 1
                continue
        meta, turns = parse_cloud_conversation(conv)
        if not turns:
            n_empty += 1
            continue
        atomic_write(raw_out, json.dumps(conv))
        write_cloud_index(meta, turns, index_out)
        n_written += 1
    pm = ingest_projects_and_memories(export, force=args.force)

    # ----- deleted-tier sync -----
    # Projects and memories are complete snapshots in every export, so the
    # newest export yet seen defines their upstream-active sets. But
    # conversations.json may cover only a ~30-day window: only an export the
    # user vouches for via --full may define the active conversation set;
    # windowed exports newer than the last full one can only ADD to it.
    conv_ids = {c.get("uuid") for c in convs if c.get("uuid")}
    conv_ts = max((c.get("updated_at") or "" for c in convs), default="")
    this_ts = max(conv_ts, pm["max_updated"])
    state = read_state()
    active = state.setdefault("active_upstream", {})
    if this_ts and this_ts >= state.get("export_watermark", ""):
        state["export_watermark"] = this_ts
        if pm["proj_ids"] is not None:
            active["proj"] = sorted(pm["proj_ids"])
        if pm["mem_slugs"] is not None:
            active["memfile"] = sorted(pm["mem_slugs"])
    full_wm = state.get("full_export_watermark", "")
    if getattr(args, "full", False):
        if conv_ts and conv_ts >= full_wm:
            state["full_export_watermark"] = conv_ts
            active["conv"] = sorted(conv_ids)
    elif active.get("conv") is not None and conv_ts >= full_wm:
        active["conv"] = sorted(set(active["conv"]) | conv_ids)

    def _active(key):
        v = active.get(key)
        return set(v) if v is not None else None

    n_moved = n_restored = 0
    for ids, idx_dir, del_dir in (
        (_active("conv"), CLOUD_INDEX_DIR, CLOUD_INDEX_DELETED_DIR),
        (_active("proj"), CLOUD_PROJ_INDEX_DIR, CLOUD_PROJ_INDEX_DELETED_DIR),
        (_active("memfile"), CLOUD_MEMORY_INDEX_DIR, CLOUD_MEMORY_INDEX_DELETED_DIR),
    ):
        d, r = _sync_deleted_tier(ids, idx_dir, del_dir)
        n_moved += d
        n_restored += r
    update_cloud_manifest()
    update_proj_manifest()

    # Mark this export as ingested + clear the expecting-export bit.
    state.setdefault("ingested", {})
    from datetime import datetime, timezone
    state["ingested"][export.name] = {
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "n_written": n_written,
        "n_cached": n_cached,
        "n_empty": n_empty,
        "n_total": len(convs),
        "n_projects": pm["proj_written"],
        "n_memory_files": pm["memory_files"],
    }
    state.pop("expecting_export", None)
    write_state(state)
    print(f"cloud indexed: {n_written} written, {n_cached} cached, {n_empty} empty (total {len(convs)})")
    if pm["proj_total"]:
        print(f"projects indexed: {pm['proj_written']} written, {pm['proj_cached']} cached "
              f"(total {pm['proj_total']}; {pm['proj_with_memory']} with project memory)")
    if pm["memory_files"]:
        print(f"memory indexed: {pm['memory_files']} files (global + memory directory)")
    if n_moved or n_restored:
        print(f"deleted tier: {n_moved} moved out of normal search, {n_restored} restored")
    if active.get("conv") is None:
        print("note: conversation deletion state unknown — ingest a FULL export with "
              "`cloud-ingest --full` to separate deleted conversations")
    print(f"index dir: {CLOUD_INDEX_DIR}")
    return 0


def summarize_one_cloud(raw_path: Path, api_key: str, force: bool = False) -> tuple[str, str]:
    uuid = raw_path.stem
    out = summary_path(uuid, source="cloud")
    if not force and out.exists() and out.stat().st_mtime >= raw_path.stat().st_mtime:
        return "cached", ""
    try:
        with raw_path.open() as f:
            conv = json.load(f)
    except Exception as e:
        return "error", f"load: {e}"
    meta, turns = parse_cloud_conversation(conv)
    if not turns:
        return "empty", ""
    transcript = render_show(meta, turns, no_tools=False)
    name = meta.get("name") or ""
    header_msg = f"Conversation name: {name}\n\n" if name else ""
    try:
        text, n_chunks = generate_summary(transcript, header_msg, api_key)
    except Exception as e:
        return "error", str(e)
    if not text.startswith("---"):
        text = "---\ntopic: (model output malformed — inspect)\noutcome: reference\nkeywords: [malformed]\n---\n\n" + text
    atomic_write(out, text + "\n")
    return "written", f"chunked({n_chunks})" if n_chunks > 1 else ""


def update_manifest():
    """Single TSV mapping session_id → cwd, start, end, branch."""
    rows = []
    for ix in sorted(INDEX_DIR.glob("*.txt")):
        meta = {}
        with ix.open() as f:
            for line in f:
                if not line.startswith("#"):
                    break
                m = re.match(r"# (\w+):\s*(.*)", line)
                if m:
                    meta[m.group(1)] = m.group(2).strip()
        rows.append((
            meta.get("session", ix.stem),
            meta.get("start", ""),
            meta.get("end", ""),
            meta.get("cwd", ""),
            meta.get("branch", ""),
            meta.get("turns", ""),
        ))
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("w") as f:
        f.write("session_id\tstart\tend\tcwd\tbranch\tturns\n")
        for r in rows:
            f.write("\t".join(r) + "\n")


def cmd_index(args):
    jsonls = all_session_jsonls()
    if not jsonls:
        print(f"no JSONL sessions found under {PROJECTS_DIR}", file=sys.stderr)
        return 1
    n_written = 0
    n_skipped = 0
    for jl in jsonls:
        try:
            wrote = write_index_file(jl, force=args.force)
        except Exception as e:
            print(f"err {jl.name}: {e}", file=sys.stderr)
            continue
        if wrote:
            n_written += 1
        else:
            n_skipped += 1
    update_manifest()
    print(f"indexed: {n_written} written, {n_skipped} up-to-date, total {len(jsonls)}")
    print(f"index dir: {INDEX_DIR}")
    return 0


def _meta_from_lines(lines: Iterable[str]) -> dict:
    meta = {}
    for line in lines:
        if not line.startswith("#"):
            break
        m = re.match(r"# (\w+):\s*(.*)", line)
        if m:
            meta[m.group(1)] = m.group(2).strip()
    return meta


def _read_meta(txt: Path) -> dict:
    with txt.open(errors="replace") as f:
        return _meta_from_lines(f)


def compile_query(query: str) -> "re.Pattern | None":
    """Compile a user-supplied regex; explain on stderr instead of crashing."""
    try:
        return re.compile(query, re.IGNORECASE)
    except re.error as e:
        print(f"invalid regex {query!r}: {e}", file=sys.stderr)
        print("hint: escape literal metacharacters, e.g. \\( \\+ \\[", file=sys.stderr)
        return None


def _in_date_range(meta: dict, since: str | None, until: str | None) -> bool:
    """ISO-8601 timestamps compare lexicographically, so prefix compare works
    for date-only bounds like 2026-05 or 2026-05-05."""
    if since:
        end = meta.get("end") or meta.get("start") or ""
        if end[: len(since)] < since:
            return False
    if until:
        start = meta.get("start") or meta.get("end") or ""
        if start[: len(until)] > until:
            return False
    return True


def _all_index_files(include_deleted: bool = False) -> list[tuple[Path, str, bool]]:
    """Return [(index_txt, source, is_deleted)] across code, cloud, project,
    and memory. The deleted tier (content gone from claude.ai) is included
    only on request."""
    out: list[tuple[Path, str, bool]] = []

    def _add(d: Path, source: str, deleted: bool = False):
        if d.exists():
            out.extend((p, source, deleted) for p in sorted(d.glob("*.txt")))

    _add(INDEX_DIR, "code")
    _add(CLOUD_INDEX_DIR, "cloud")
    _add(CLOUD_PROJ_INDEX_DIR, "project")
    _add(CLOUD_MEMORY_INDEX_DIR, "memory")
    if include_deleted:
        _add(CLOUD_INDEX_DELETED_DIR, "cloud", True)
        _add(CLOUD_PROJ_INDEX_DELETED_DIR, "project", True)
        _add(CLOUD_MEMORY_INDEX_DELETED_DIR, "memory", True)
    return out


def cmd_search(args):
    files = _all_index_files(include_deleted=getattr(args, "deleted", False))
    if not files:
        print("no index — run: claudex index   (and optionally: claudex cloud-ingest)", file=sys.stderr)
        return 1
    pat = compile_query(args.query)
    if pat is None:
        return 1
    if args.source:
        files = [(p, s, d) for (p, s, d) in files if s == args.source]
    since, until = getattr(args, "since", None), getattr(args, "until", None)
    hits: list[tuple[str, Path, str, bool, dict, list[str], list[tuple[int, str]]]] = []
    for txt, source, is_deleted in files:
        body_lines = txt.read_text(errors="replace").splitlines()
        meta = _meta_from_lines(body_lines)
        if not _in_date_range(meta, since, until):
            continue
        matched = [(i, line) for i, line in enumerate(body_lines) if pat.search(line)]
        if matched:
            end_ts = meta.get("end") or meta.get("start") or ""
            hits.append((end_ts, txt, source, is_deleted, meta, body_lines, matched))
    if not hits:
        print("no matches")
        return 0
    hits.sort(key=lambda h: h[0], reverse=True)
    total = len(hits)
    if args.limit and total > args.limit:
        hits = hits[: args.limit]
        print(f"{total} match(es), showing {len(hits)} newest:\n")
    else:
        print(f"{total} match(es), newest first:\n")
    for _end_ts, txt, source, is_deleted, meta, body_lines, matched in hits:
        kind = meta.get("kind", "session")
        labels = [] if kind == "session" else [kind]
        if is_deleted:
            labels.append("deleted")
        tag = f"[{' '.join(labels)}]" if labels else ""
        print(f"  {meta.get('session', txt.stem)} {tag}")
        topic = read_summary_topic(txt.stem, source=source)
        if topic:
            print(f"    topic:  {topic}")
        if meta.get("name"):
            print(f"    name:   {meta['name']}")
        if meta.get("parent"):
            print(f"    parent: {meta['parent']}")
        if meta.get("cwd"):
            print(f"    cwd:    {meta['cwd']}")
        if meta.get("start") or meta.get("end"):
            print(f"    when:   {meta.get('start','')}  →  {meta.get('end','')}")
        if meta.get("docs"):
            print(f"    docs:   {meta['docs']}")
        if meta.get("turns"):
            print(f"    turns:  {meta['turns']}")
        for i, line in matched[: args.max_per_file]:
            ctx_start = max(0, i - 1)
            ctx_end = min(len(body_lines), i + 2)
            for j in range(ctx_start, ctx_end):
                marker = "►" if j == i else " "
                print(f"    {marker} {body_lines[j][:180]}")
            print()
    return 0


def resolve_sessions(prefix: str) -> list[dict]:
    """All sessions matching an id prefix, across live code JSONLs, the flat
    index archive (survives Claude Code's transcript pruning), cloud raw
    JSON, and the cloud index.

    Returns [{source, sid, live, index, deleted}] — live/index are Paths or None."""
    out: dict[tuple[str, str], dict] = {}

    def add(source: str, sid: str, live: Path | None = None, index: Path | None = None,
            deleted: bool = False):
        e = out.setdefault((source, sid), {"source": source, "sid": sid,
                                           "live": None, "index": None, "deleted": False})
        if live:
            e["live"] = live
        if index:
            e["index"] = index
        if deleted:
            e["deleted"] = True

    for p in all_session_jsonls():
        if p.stem.startswith(prefix) or p.stem.startswith(f"agent-{prefix}"):
            add("code", p.stem, live=p)
    if INDEX_DIR.exists():
        for pat in (f"{prefix}*.txt", f"agent-{prefix}*.txt"):
            for p in INDEX_DIR.glob(pat):
                add("code", p.stem, index=p)
    if CLOUD_RAW_DIR.exists():
        for p in CLOUD_RAW_DIR.glob(f"{prefix}*.json"):
            add("cloud", p.stem, live=p)
    if CLOUD_INDEX_DIR.exists():
        for p in CLOUD_INDEX_DIR.glob(f"{prefix}*.txt"):
            add("cloud", p.stem, index=p)
    if CLOUD_PROJ_RAW_DIR.exists():
        for p in CLOUD_PROJ_RAW_DIR.glob(f"{prefix}*.json"):
            add("project", p.stem, live=p)
    if CLOUD_PROJ_INDEX_DIR.exists():
        for p in CLOUD_PROJ_INDEX_DIR.glob(f"{prefix}*.txt"):
            add("project", p.stem, index=p)
    # deleted tier: still resolvable on demand
    if CLOUD_INDEX_DELETED_DIR.exists():
        for p in CLOUD_INDEX_DELETED_DIR.glob(f"{prefix}*.txt"):
            add("cloud", p.stem, index=p, deleted=True)
    if CLOUD_PROJ_INDEX_DELETED_DIR.exists():
        for p in CLOUD_PROJ_INDEX_DELETED_DIR.glob(f"{prefix}*.txt"):
            if p.stem.endswith(".memory"):  # tombstones would shadow their project id
                continue
            add("project", p.stem, index=p, deleted=True)
    return sorted(out.values(), key=lambda e: (e["source"], e["sid"]))


def pick_session(prefix: str) -> dict | None:
    """Resolve a prefix to exactly one session, or explain why not on stderr."""
    cands = resolve_sessions(prefix)
    if not cands:
        print(f"session not found: {prefix}", file=sys.stderr)
        return None
    if len(cands) == 1:
        return cands[0]
    exact = [e for e in cands if e["sid"] in (prefix, f"agent-{prefix}")]
    if len(exact) == 1:
        return exact[0]
    print(f"ambiguous prefix {prefix!r} — {len(cands)} matches:", file=sys.stderr)
    for e in cands[:20]:
        print(f"  {e['source']:5} {e['sid']}", file=sys.stderr)
    if len(cands) > 20:
        print(f"  ... and {len(cands) - 20} more", file=sys.stderr)
    return None


def cmd_show(args):
    e = pick_session(args.session)
    if e is None:
        return 1
    if e.get("deleted"):
        print("(deleted upstream on claude.ai — retrieved from the deleted tier)", file=sys.stderr)
    if e["source"] == "project":
        # the project index IS the rendered form (docs + embedded memory)
        if e["index"] and e["index"].exists():
            sys.stdout.write(e["index"].read_text(errors="replace"))
        else:
            with e["live"].open() as f:
                proj = json.load(f)
            sys.stdout.write(render_project_index(proj, ""))
        return 0
    if e["live"]:
        if e["source"] == "code":
            meta, turns = parse_session(e["live"])
        else:
            with e["live"].open() as f:
                conv = json.load(f)
            meta, turns = parse_cloud_conversation(conv)
    else:
        # source transcript pruned — the flat index is the surviving copy
        meta, turns = parse_index_file(e["index"])
        print("(source transcript pruned — rendered from index archive; "
              "tool one-liners not preserved)", file=sys.stderr)
    sys.stdout.write(render_show(meta, turns, no_tools=args.no_tools))
    return 0


def summarize_one(jsonl: Path, api_key: str, force: bool = False) -> tuple[str, str]:
    """Summarize one session. Returns (status, detail). status: written|cached|empty|error."""
    out = summary_path(jsonl.stem, source="code")
    if not force and out.exists() and out.stat().st_mtime >= jsonl.stat().st_mtime:
        return "cached", ""
    meta, turns = parse_session(jsonl)
    if not turns:
        return "empty", ""
    transcript = render_show(meta, turns, no_tools=False)
    try:
        text, n_chunks = generate_summary(transcript, "", api_key)
    except Exception as e:
        return "error", str(e)
    if not text.startswith("---"):
        text = "---\ntopic: (model output malformed — inspect)\noutcome: reference\nkeywords: [malformed]\n---\n\n" + text
    atomic_write(out, text + "\n")
    return "written", f"chunked({n_chunks})" if n_chunks > 1 else ""


def cmd_summarize(args):
    api_key = ""  # unused now; auth flows through `claude -p` (OAuth/Max)
    if not shutil.which("claude"):
        print("`claude` CLI not on PATH — install Claude Code", file=sys.stderr)
        return 1
    targets: list[tuple[str, Path]] = []  # (source, path)
    if args.source in (None, "code"):
        for jl in all_session_jsonls():
            targets.append(("code", jl))
    if args.source in (None, "cloud"):
        if CLOUD_RAW_DIR.exists():
            for raw in sorted(CLOUD_RAW_DIR.glob("*.json")):
                targets.append(("cloud", raw))
    if args.session:
        targets = [(s, p) for (s, p) in targets if p.stem.startswith(args.session)]
    if not targets:
        print("no sessions to summarize", file=sys.stderr)
        return 1
    n = len(targets)
    counts = {"written": 0, "cached": 0, "empty": 0, "error": 0}
    for i, (source, path) in enumerate(targets, 1):
        if source == "code":
            status, detail = summarize_one(path, api_key, force=args.force)
        else:
            status, detail = summarize_one_cloud(path, api_key, force=args.force)
        counts[status] += 1
        sid = path.stem[:8]
        if status == "written" and detail:
            suffix = f"  {detail}"
        elif detail:
            suffix = f" — {detail[:120]}"
        else:
            suffix = ""
        print(f"[{i}/{n}] {source} {sid}  {status}{suffix}", flush=True)
    print(f"\ndone: {counts['written']} written, {counts['cached']} cached, {counts['empty']} empty, {counts['error']} error")
    return 0 if counts["error"] == 0 else 2


def cmd_synthesize(args):
    if not shutil.which("claude"):
        print("`claude` CLI not on PATH — install Claude Code", file=sys.stderr)
        return 1
    files = _all_index_files(include_deleted=getattr(args, "deleted", False))
    if args.source:
        files = [(p, s, d) for (p, s, d) in files if s == args.source]
    if not files:
        print("no index — run: claudex index / cloud-ingest first", file=sys.stderr)
        return 1
    pat = compile_query(args.query)
    if pat is None:
        return 1
    hits: list[tuple[str, str]] = []  # (session_id, source)
    for txt, source, _is_deleted in files:
        body = txt.read_text(errors="replace")
        if pat.search(body):
            hits.append((txt.stem, source))
    if not hits:
        print("no matches", file=sys.stderr)
        return 1
    # filter to sessions with cached summaries before applying --max
    summarized: list[tuple[str, str]] = []
    skipped = 0
    for sid, source in hits:
        if summary_path(sid, source).exists():
            summarized.append((sid, source))
        else:
            skipped += 1
    if not summarized:
        print(f"none of the {len(hits)} matched sessions have summaries — run: claudex summarize", file=sys.stderr)
        return 1
    if args.max and len(summarized) > args.max:
        print(f"limiting to {args.max} of {len(summarized)} summarized hits ({skipped} unsummarized matches skipped, use --max to change)", file=sys.stderr)
        summarized = summarized[: args.max]
    elif skipped:
        print(f"({skipped} matches skipped — no summary cached)", file=sys.stderr)
    print(f"synthesizing across {len(summarized)} sessions...", file=sys.stderr)
    parts: list[str] = []
    for sid, source in summarized:
        body = summary_path(sid, source).read_text(errors="replace")
        parts.append(f"=== session {sid} [{source}] ===\n{body}\n")
    blob = "\n".join(parts)
    from datetime import datetime, timezone
    header_msg = f"Search query: {args.query}\nMatched sessions: {len(parts)}\nGenerated: {datetime.now(timezone.utc).isoformat()}\n\n"
    try:
        text, n_chunks = generate_synthesis(blob, header_msg, args.query, len(parts))
    except Exception as e:
        print(f"synthesis failed: {e}", file=sys.stderr)
        return 2
    SYNTHESES_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", args.query.lower()).strip("-")[:40] or "synthesis"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = SYNTHESES_DIR / f"{slug}-{ts}.md"
    atomic_write(out, text + "\n")
    sys.stdout.write(text)
    sys.stdout.write(f"\n\n--- saved to {out} ---\n")
    if n_chunks > 1:
        sys.stderr.write(f"(used chunked synthesis, {n_chunks} slices)\n")
    return 0


def generate_synthesis(blob: str, header_msg: str, query: str, n_sessions: int) -> tuple[str, int]:
    """Synthesize across many summaries. Single-shot if it fits, otherwise chunk
    summaries → partial syntheses → meta-synthesis."""
    if len(blob) <= DIRECT_MAX_CHARS:
        user_msg = f"{header_msg}{blob}"
        return call_summarizer("", SYNTHESIZER_SYSTEM, user_msg), 1
    # chunk on session boundaries (=== marker), each ~CHUNK_TARGET_CHARS
    sessions = re.split(r"(?=^=== session )", blob, flags=re.MULTILINE)
    chunks: list[str] = []
    cur: list[str] = []
    cur_size = 0
    for s in sessions:
        if cur and cur_size + len(s) > CHUNK_TARGET_CHARS:
            chunks.append("".join(cur))
            cur = []
            cur_size = 0
        cur.append(s)
        cur_size += len(s)
    if cur:
        chunks.append("".join(cur))
    partials: list[str] = []
    for i, ch in enumerate(chunks, 1):
        slice_msg = f"{header_msg}Slice {i} of {len(chunks)}:\n\n{ch}"
        partial = call_summarizer("", PARTIAL_SUMMARIZER_SYSTEM, slice_msg, max_tokens=800)
        partials.append(f"[slice {i}/{len(chunks)}]\n{partial}")
    combined = "\n\n".join(partials)
    final_msg = f"{header_msg}{META_SUMMARIZER_INTRO}{combined}"
    return call_summarizer("", SYNTHESIZER_SYSTEM, final_msg), len(chunks)


def cmd_summary(args):
    e = pick_session(args.session)
    if e is None:
        return 1
    path = summary_path(e["sid"], source=e["source"])
    if path.exists():
        sys.stdout.write(path.read_text())
        return 0
    hint = f"claudex summarize {e['sid']}" if e["live"] else "(source transcript pruned; cannot regenerate)"
    print(f"no summary for {e['sid']} — {hint}", file=sys.stderr)
    return 1


def cmd_list(args):
    rows: list[tuple[str, str, str, str, str]] = []  # (source, sid, start, label, turns)
    if args.source in (None, "code") and MANIFEST.exists():
        with MANIFEST.open() as f:
            f.readline()
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 6:
                    continue
                sid, start, _end, cwd, _branch, turns = parts[:6]
                label = cwd.replace(str(Path.home()), "~")
                rows.append(("code", sid, start, label, turns))
    include_deleted = getattr(args, "deleted", False)
    cloud_manifests = [(CLOUD_MANIFEST, "")]
    proj_manifests = [(CLOUD_PROJ_MANIFEST, "")]
    if include_deleted:
        cloud_manifests.append((CLOUD_MANIFEST_DELETED, " [deleted]"))
        proj_manifests.append((CLOUD_PROJ_MANIFEST_DELETED, " [deleted]"))
    if args.source in (None, "cloud"):
        for manifest, mark in cloud_manifests:
            if not manifest.exists():
                continue
            with manifest.open() as f:
                f.readline()
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 5:
                        continue
                    sid, start, _end, name, turns = parts[:5]
                    rows.append(("cloud", sid, start, name + mark, turns))
    if args.source in (None, "project"):
        for manifest, mark in proj_manifests:
            if not manifest.exists():
                continue
            with manifest.open() as f:
                f.readline()
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 6:
                        continue
                    pid, start, _end, name, docs, memory = parts[:6]
                    extra = f"docs={docs}" + ("+mem" if memory == "yes" else "")
                    rows.append(("proj", pid, start, name + mark, extra))
    if not rows:
        print("no manifest — run: claudex index   (and optionally: claudex cloud-ingest)", file=sys.stderr)
        return 1
    rows.sort(key=lambda r: r[2], reverse=True)
    if args.limit:
        rows = rows[: args.limit]
    for source, sid, start, label, turns in rows:
        topic = read_summary_topic(sid, source) if not args.no_topic else None
        line = f"{source:5}  {sid[:8]}  {start[:19]}  {turns:<25}  {label[:60]}"
        if topic:
            line += f"  · {topic[:80]}"
        print(line)
    return 0


def main():
    p = argparse.ArgumentParser(prog="claudex", description="splunk Claude Code transcripts")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="build/refresh flat-text index")
    pi.add_argument("--force", action="store_true", help="reindex even if up-to-date")
    pi.set_defaults(fn=cmd_index)

    ps = sub.add_parser("search", help="search the index (regex, case-insensitive)")
    ps.add_argument("query")
    ps.add_argument("--max-per-file", type=int, default=3, help="max matches shown per session")
    ps.add_argument("--source", choices=("code", "cloud", "project", "memory"), default=None, help="restrict to one source")
    ps.add_argument("--limit", type=int, default=0, help="show only the N most recent matching sessions (0 = all)")
    ps.add_argument("--since", default=None, metavar="DATE", help="only sessions ending on/after ISO date, e.g. 2026-05 or 2026-05-01")
    ps.add_argument("--until", default=None, metavar="DATE", help="only sessions starting on/before ISO date")
    ps.add_argument("--deleted", action="store_true", help="also search content deleted upstream on claude.ai")
    ps.set_defaults(fn=cmd_search)

    psh = sub.add_parser("show", help="prettify a session to stdout")
    psh.add_argument("session", help="session id (prefix ok)")
    psh.add_argument("--no-tools", action="store_true", help="hide tool-call one-liners")
    psh.set_defaults(fn=cmd_show)

    pl = sub.add_parser("list", help="list indexed sessions, newest first")
    pl.add_argument("--limit", type=int, default=20)
    pl.add_argument("--source", choices=("code", "cloud", "project"), default=None)
    pl.add_argument("--no-topic", action="store_true", help="hide topic lines from cached summaries")
    pl.add_argument("--deleted", action="store_true", help="include content deleted upstream on claude.ai")
    pl.set_defaults(fn=cmd_list)

    psm = sub.add_parser("summarize", help="generate per-session summaries via Claude API (Haiku)")
    psm.add_argument("session", nargs="?", help="optional session id prefix; default: all sessions")
    psm.add_argument("--force", action="store_true", help="resummarize even if cached")
    psm.add_argument("--source", choices=("code", "cloud"), default=None)
    psm.set_defaults(fn=cmd_summarize)

    psy = sub.add_parser("summary", help="print a session's cached summary")
    psy.add_argument("session", help="session id (prefix ok)")
    psy.set_defaults(fn=cmd_summary)

    pci = sub.add_parser("cloud-ingest", help="ingest a claude.ai data export (conversations, projects, memories)")
    pci.add_argument("--full", action="store_true",
                     help="export contains ALL conversations (not a ~30-day window) — "
                          "enables deletion detection for conversations")
    pci.add_argument("--export", help="path to export dir (default: newest data-*-batch-0000 under ~/Downloads)")
    pci.add_argument("--force", action="store_true", help="rewrite even if cached")
    pci.set_defaults(fn=cmd_cloud_ingest)

    psyn = sub.add_parser("synthesize", help="cross-cutting digest across all sessions matching a regex")
    psyn.add_argument("query", help="regex (case-insensitive)")
    psyn.add_argument("--source", choices=("code", "cloud"), default=None)
    psyn.add_argument("--max", type=int, default=50, help="cap on matched sessions to include (default 50)")
    psyn.add_argument("--deleted", action="store_true", help="also match content deleted upstream on claude.ai")
    psyn.set_defaults(fn=cmd_synthesize)

    pee = sub.add_parser("expecting-export", help="flag that you've requested a claude.ai data export — SessionStart hook will then check for arrival")
    pee.add_argument("--cancel", action="store_true", help="clear the bit")
    pee.set_defaults(fn=cmd_expecting_export)

    pad = sub.add_parser("auto-detect", help="hook entry point: emits notices when an expected export arrives. Fast no-op when bit is unset.")
    pad.add_argument("--no-email", action="store_true", help="skip Fastmail check (disk-only)")
    pad.set_defaults(fn=cmd_auto_detect)

    pdis = sub.add_parser("dismiss", help="suppress hook notices for an export folder you don't intend to ingest")
    pdis.add_argument("export_name", help="folder name, e.g. data-XXXX-batch-0000")
    pdis.set_defaults(fn=cmd_dismiss)

    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
