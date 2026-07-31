"""Tests for claudex. Stdlib only: python3 -m unittest discover tests -v

All filesystem-touching tests patch the module's directory constants to a
tempdir — never the real ~/.claudex or ~/.claude/projects.
"""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claudex  # noqa: E402


class TestCodexSkill(unittest.TestCase):
    def test_bundled_launcher_reaches_project_cli(self):
        root = Path(__file__).resolve().parent.parent
        launcher = root / "skills" / "claudex" / "scripts" / "claudex"
        result = subprocess.run(
            [str(launcher), "--help"], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("splunk Claude Code transcripts", result.stdout)


class TmpDirsMixin(unittest.TestCase):
    """Redirect every claudex directory constant into a fresh tempdir."""

    DIR_ATTRS = (
        "PROJECTS_DIR", "INDEX_DIR", "SUMMARIES_DIR", "MANIFEST",
        "CLOUD_RAW_DIR", "CLOUD_INDEX_DIR", "CLOUD_SUMMARIES_DIR",
        "CLOUD_MANIFEST", "SYNTHESES_DIR", "STATE_FILE",
        "CLOUD_PROJ_RAW_DIR", "CLOUD_PROJ_INDEX_DIR", "CLOUD_PROJ_MANIFEST",
        "CLOUD_PROJ_MEMORY_DIR", "CLOUD_MEMORY_RAW", "CLOUD_MEMORY_INDEX_DIR",
        "CLOUD_INDEX_DELETED_DIR", "CLOUD_MANIFEST_DELETED",
        "CLOUD_PROJ_INDEX_DELETED_DIR", "CLOUD_PROJ_MANIFEST_DELETED",
        "CLOUD_MEMORY_INDEX_DELETED_DIR",
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved = {a: getattr(claudex, a) for a in self.DIR_ATTRS}
        claudex.PROJECTS_DIR = self.root / "projects"
        claudex.INDEX_DIR = self.root / "index"
        claudex.SUMMARIES_DIR = self.root / "summaries"
        claudex.MANIFEST = self.root / "manifest.tsv"
        claudex.CLOUD_RAW_DIR = self.root / "cloud" / "raw"
        claudex.CLOUD_INDEX_DIR = self.root / "cloud" / "index"
        claudex.CLOUD_SUMMARIES_DIR = self.root / "cloud" / "summaries"
        claudex.CLOUD_MANIFEST = self.root / "cloud" / "manifest.tsv"
        claudex.SYNTHESES_DIR = self.root / "syntheses"
        claudex.STATE_FILE = self.root / "state.json"
        claudex.CLOUD_PROJ_RAW_DIR = self.root / "cloud" / "cproj" / "raw"
        claudex.CLOUD_PROJ_MEMORY_DIR = self.root / "cloud" / "cproj" / "memory"
        claudex.CLOUD_PROJ_INDEX_DIR = self.root / "cloud" / "cproj" / "index"
        claudex.CLOUD_PROJ_MANIFEST = self.root / "cloud" / "cproj" / "manifest.tsv"
        claudex.CLOUD_MEMORY_RAW = self.root / "cloud" / "memory" / "memories.json"
        claudex.CLOUD_MEMORY_INDEX_DIR = self.root / "cloud" / "memory" / "index"
        claudex.CLOUD_INDEX_DELETED_DIR = self.root / "cloud" / "index-deleted"
        claudex.CLOUD_MANIFEST_DELETED = self.root / "cloud" / "manifest-deleted.tsv"
        claudex.CLOUD_PROJ_INDEX_DELETED_DIR = self.root / "cloud" / "cproj" / "index-deleted"
        claudex.CLOUD_PROJ_MANIFEST_DELETED = self.root / "cloud" / "cproj" / "manifest-deleted.tsv"
        claudex.CLOUD_MEMORY_INDEX_DELETED_DIR = self.root / "cloud" / "memory" / "index-deleted"

    def tearDown(self):
        for a, v in self._saved.items():
            setattr(claudex, a, v)
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def write_jsonl(self, name: str, events: list[dict], project: str = "proj") -> Path:
        d = claudex.PROJECTS_DIR / project
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{name}.jsonl"
        p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        return p

    def write_index(self, sid: str, *, start="", end="", cwd="/tmp/x",
                    body="", kind="session", cloud=False) -> Path:
        d = claudex.CLOUD_INDEX_DIR if cloud else claudex.INDEX_DIR
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sid}.txt"
        hdr = [f"# session: {sid}", f"# kind: {kind}"]
        if not cloud:
            hdr.append(f"# cwd: {cwd}")
        hdr += [f"# start: {start}", f"# end: {end}",
                "# turns: user=1 assistant=1", ""]
        p.write_text("\n".join(hdr) + "\n" + body)
        return p


SESSION_EVENTS = [
    {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "cwd": "/tmp/x",
     "gitBranch": "main", "version": "1.0",
     "message": {"content": "hello world"}},
    # tool_result container masquerading as a user turn — must be skipped
    {"type": "user", "timestamp": "2026-01-01T00:00:30Z",
     "message": {"content": [{"type": "tool_result", "content": "out"}]}},
    {"type": "assistant", "timestamp": "2026-01-01T00:01:00Z",
     "message": {"content": [
         {"type": "text", "text": "hi there"},
         {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
     ]}},
    # sidechain traffic — must be skipped for a main-session file
    {"type": "user", "isSidechain": True, "timestamp": "2026-01-01T00:02:00Z",
     "message": {"content": "subagent chatter"}},
    "not-json-at-all",  # malformed line must not crash the parser
]


class TestParseSession(TmpDirsMixin):
    def _write(self):
        d = claudex.PROJECTS_DIR / "proj"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "abc123.jsonl"
        lines = [e if isinstance(e, str) else json.dumps(e) for e in SESSION_EVENTS]
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_turn_extraction_and_skips(self):
        meta, turns = claudex.parse_session(self._write())
        self.assertEqual(meta["session_id"], "abc123")
        self.assertEqual(meta["n_user"], 1)
        self.assertEqual(meta["n_assistant"], 1)
        self.assertEqual(meta["cwd"], "/tmp/x")
        self.assertEqual(meta["branch"], "main")
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0].role, "user")
        self.assertEqual(turns[0].text, "hello world")
        self.assertEqual(turns[1].role, "assistant")
        self.assertIn("→ Bash: ls -la", turns[1].tool_lines)

    def test_timestamps_span_session(self):
        meta, _ = claudex.parse_session(self._write())
        self.assertEqual(meta["start_ts"], "2026-01-01T00:00:00Z")
        self.assertEqual(meta["end_ts"], "2026-01-01T00:02:00Z")


class TestIndexRoundTrip(TmpDirsMixin):
    def test_write_then_rehydrate(self):
        jl = self.write_jsonl("abc123", [e for e in SESSION_EVENTS if isinstance(e, dict)])
        self.assertTrue(claudex.write_index_file(jl))
        meta, turns = claudex.parse_index_file(claudex.INDEX_DIR / "abc123.txt")
        self.assertEqual(meta["session_id"], "abc123")
        self.assertEqual(meta["n_user"], 1)
        self.assertEqual(meta["n_assistant"], 1)
        self.assertEqual([t.role for t in turns], ["user", "assistant"])
        self.assertEqual(turns[0].text, "hello world")
        self.assertEqual(turns[1].text, "hi there")

    def test_multiline_turn_text_preserved(self):
        body = ("[USER 2026-01-01T00:00:00Z]\nline one\n\nline three\n\n"
                "[ASSISTANT 2026-01-01T00:01:00Z]\nanswer\n\n")
        p = self.write_index("s1", start="2026-01-01", end="2026-01-01", body=body)
        _, turns = claudex.parse_index_file(p)
        self.assertEqual(turns[0].text, "line one\n\nline three")

    def test_cloud_name_block_not_a_turn(self):
        body = "[NAME]\nMy Conversation\n\n[USER 2026-01-01]\nhi\n\n"
        p = self.write_index("c1", body=body, cloud=True)
        _, turns = claudex.parse_index_file(p)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].role, "user")

    def test_skip_when_fresh_rewrite_when_forced(self):
        jl = self.write_jsonl("abc123", [e for e in SESSION_EVENTS if isinstance(e, dict)])
        self.assertTrue(claudex.write_index_file(jl))
        self.assertFalse(claudex.write_index_file(jl))
        self.assertTrue(claudex.write_index_file(jl, force=True))


class TestResolveSessions(TmpDirsMixin):
    def test_index_only_session_resolves(self):
        self.write_index("aaaa1111-dead-beef")
        e = claudex.pick_session("aaaa")
        self.assertIsNotNone(e)
        self.assertEqual(e["source"], "code")
        self.assertIsNone(e["live"])
        self.assertIsNotNone(e["index"])

    def test_live_and_index_merge_into_one_candidate(self):
        jl = self.write_jsonl("aaaa1111", [{"type": "user", "timestamp": "t",
                                           "message": {"content": "x"}}])
        claudex.write_index_file(jl)
        cands = claudex.resolve_sessions("aaaa")
        self.assertEqual(len(cands), 1)
        self.assertIsNotNone(cands[0]["live"])
        self.assertIsNotNone(cands[0]["index"])

    def test_ambiguous_prefix_rejected(self):
        self.write_index("aaaa1111")
        self.write_index("aaaa2222")
        with redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(claudex.pick_session("aaaa"))
        self.assertIn("ambiguous", err.getvalue())

    def test_exact_id_wins_over_longer_siblings(self):
        self.write_index("aaaa")
        self.write_index("aaaa1111")
        e = claudex.pick_session("aaaa")
        self.assertIsNotNone(e)
        self.assertEqual(e["sid"], "aaaa")

    def test_agent_prefix_matches_subagent_index(self):
        self.write_index("agent-bbbb1111", kind="subagent")
        e = claudex.pick_session("bbbb")
        self.assertIsNotNone(e)
        self.assertEqual(e["sid"], "agent-bbbb1111")

    def test_missing_session(self):
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(claudex.pick_session("zzzz"))


class TestCmdShowFallback(TmpDirsMixin):
    def test_show_renders_from_index_when_jsonl_pruned(self):
        body = "[USER 2026-01-01T00:00:00Z]\nthe pruned question\n\n"
        self.write_index("cccc1111", start="2026-01-01", end="2026-01-02", body=body)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = claudex.cmd_show(Namespace(session="cccc", no_tools=False))
        self.assertEqual(rc, 0)
        self.assertIn("the pruned question", out.getvalue())
        self.assertIn("pruned", err.getvalue())


class TestSearch(TmpDirsMixin):
    def _args(self, **kw):
        base = dict(query="needle", source=None, max_per_file=3,
                    limit=0, since=None, until=None)
        base.update(kw)
        return Namespace(**base)

    def _seed(self):
        self.write_index("old1", start="2026-01-01T00:00:00Z",
                         end="2026-01-01T01:00:00Z",
                         body="[USER t]\nneedle in january\n\n")
        self.write_index("new1", start="2026-06-01T00:00:00Z",
                         end="2026-06-01T01:00:00Z",
                         body="[USER t]\nneedle in june\n\n")

    def test_newest_first(self):
        self._seed()
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(claudex.cmd_search(self._args()), 0)
        text = out.getvalue()
        self.assertLess(text.index("new1"), text.index("old1"))

    def test_limit(self):
        self._seed()
        with redirect_stdout(io.StringIO()) as out:
            claudex.cmd_search(self._args(limit=1))
        text = out.getvalue()
        self.assertIn("new1", text)
        self.assertNotIn("old1", text)
        self.assertIn("showing 1 newest", text)

    def test_since_until(self):
        self._seed()
        with redirect_stdout(io.StringIO()) as out:
            claudex.cmd_search(self._args(since="2026-05"))
        self.assertNotIn("old1", out.getvalue())
        with redirect_stdout(io.StringIO()) as out:
            claudex.cmd_search(self._args(until="2026-02"))
        self.assertNotIn("new1", out.getvalue())

    def test_invalid_regex_is_friendly(self):
        self._seed()
        with redirect_stderr(io.StringIO()) as err:
            rc = claudex.cmd_search(self._args(query="oops("))
        self.assertEqual(rc, 1)
        self.assertIn("invalid regex", err.getvalue())


PROJ_UUID = "0199aaaa-1111-2222-3333-444455556666"

EXPORT_PROJECT = {
    "uuid": PROJ_UUID,
    "name": "Cyberwise",
    "description": "Cyberpunk 2077 Assistant",
    "prompt_template": "Reference Instructions.md for behavior.",
    "created_at": "2025-09-19T01:35:39+00:00",
    "updated_at": "2026-02-22T20:32:08+00:00",
    "docs": [
        {"uuid": "d1", "filename": "Remorse Settings.md",
         "content": "How V feels about the flatline option."},
    ],
}

EXPORT_MEMORIES = {
    "account_uuid": "acct-1",
    "conversations_memory": "Ron shoots a Nikon Z6 III.",
    "project_memories": {PROJ_UUID: "User plays a nomad V with high remorse."},
    "memory_files": [
        {"path": "/areas/10tdb.md", "content": "Transmedia live music publication."},
        {"path": "/topics/pets.md", "content": ""},  # empty → skipped
    ],
}


def make_conv(uuid: str, text: str, updated: str) -> dict:
    return {"uuid": uuid, "name": f"conv {uuid[:4]}", "created_at": updated,
            "updated_at": updated,
            "chat_messages": [{"sender": "human", "created_at": updated,
                               "text": text, "content": [], "attachments": []}]}


class ExportMixin(TmpDirsMixin):
    def make_export(self, memories=EXPORT_MEMORIES, projects=(EXPORT_PROJECT,),
                    convs=(), name="export") -> Path:
        exp = self.root / name
        (exp / "projects").mkdir(parents=True, exist_ok=True)
        (exp / "conversations.json").write_text(json.dumps(list(convs)))
        if memories is not None:
            (exp / "memories.json").write_text(json.dumps([memories]))
        for proj in projects:
            (exp / "projects" / f"{proj['uuid']}.json").write_text(json.dumps(proj))
        return exp

    def ingest(self, exp: Path, force=False, full=False) -> str:
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            rc = claudex.cmd_cloud_ingest(Namespace(export=str(exp), force=force, full=full))
        self.assertEqual(rc, 0)
        return out.getvalue()

    def search(self, query, **kw):
        base = dict(query=query, source=None, max_per_file=3,
                    limit=0, since=None, until=None, deleted=False)
        base.update(kw)
        with redirect_stdout(io.StringIO()) as out:
            claudex.cmd_search(Namespace(**base))
        return out.getvalue()


class TestProjectMemoryIngest(ExportMixin):

    def test_project_index_embeds_docs_and_memory(self):
        out = self.ingest(self.make_export())
        self.assertIn("projects indexed: 1 written", out)
        self.assertIn("1 with project memory", out)
        text = (claudex.CLOUD_PROJ_INDEX_DIR / f"{PROJ_UUID}.txt").read_text()
        self.assertIn("# kind: project", text)
        self.assertIn("# docs: 1", text)
        self.assertIn("[PROMPT]\nReference Instructions.md", text)
        self.assertIn("[MEMORY]\nUser plays a nomad V", text)
        self.assertIn("[DOC Remorse Settings.md]\nHow V feels", text)
        self.assertTrue((claudex.CLOUD_PROJ_RAW_DIR / f"{PROJ_UUID}.json").exists())
        self.assertIn("Cyberwise", claudex.CLOUD_PROJ_MANIFEST.read_text())

    def test_memory_index_written_and_empty_files_skipped(self):
        self.ingest(self.make_export())
        idx = claudex.CLOUD_MEMORY_INDEX_DIR
        self.assertIn("Nikon Z6 III", (idx / "conversations-memory.txt").read_text())
        self.assertIn("# name: /areas/10tdb.md", (idx / "areas-10tdb.md.txt").read_text())
        self.assertFalse((idx / "topics-pets.md.txt").exists())
        self.assertTrue(claudex.CLOUD_MEMORY_RAW.exists())

    def test_reingest_cached_until_memory_changes(self):
        exp = self.make_export()
        self.ingest(exp)
        self.assertIn("0 written, 1 cached", self.ingest(exp))
        changed = dict(EXPORT_MEMORIES,
                       project_memories={PROJ_UUID: "V went corpo after all."})
        (exp / "memories.json").write_text(json.dumps([changed]))
        self.assertIn("1 written, 0 cached", self.ingest(exp))
        text = (claudex.CLOUD_PROJ_INDEX_DIR / f"{PROJ_UUID}.txt").read_text()
        self.assertIn("V went corpo", text)
        self.assertNotIn("nomad V", text)

    def test_cleared_project_memory_moves_to_deleted_tier(self):
        exp = self.make_export()
        self.ingest(exp)
        # newer export: memory cleared upstream, project since updated
        cleared = dict(EXPORT_MEMORIES, project_memories={})
        (exp / "memories.json").write_text(json.dumps([cleared]))
        bumped = dict(EXPORT_PROJECT, updated_at="2026-03-01T00:00:00+00:00")
        (exp / "projects" / f"{PROJ_UUID}.json").write_text(json.dumps(bumped))
        self.ingest(exp)
        text = (claudex.CLOUD_PROJ_INDEX_DIR / f"{PROJ_UUID}.txt").read_text()
        self.assertIn("# end: 2026-03-01", text)
        self.assertNotIn("[MEMORY]", text)  # no longer embedded in active index
        tomb = claudex.CLOUD_PROJ_INDEX_DELETED_DIR / f"{PROJ_UUID}.memory.txt"
        self.assertIn("User plays a nomad V", tomb.read_text())
        self.assertNotIn("nomad V", self.search("nomad V"))
        hit = self.search("nomad V", deleted=True)
        self.assertIn("[project-memory deleted]", hit)
        # memory restored upstream → tombstone goes away, embedding returns
        (exp / "memories.json").write_text(json.dumps([EXPORT_MEMORIES]))
        rebumped = dict(EXPORT_PROJECT, updated_at="2026-04-01T00:00:00+00:00")
        (exp / "projects" / f"{PROJ_UUID}.json").write_text(json.dumps(rebumped))
        self.ingest(exp)
        self.assertFalse(tomb.exists())
        self.assertIn("nomad V", self.search("nomad V"))

    def test_search_finds_project_and_memory_with_source_filter(self):
        self.ingest(self.make_export())
        def search(**kw):
            base = dict(query="x", source=None, max_per_file=3,
                        limit=0, since=None, until=None)
            base.update(kw)
            with redirect_stdout(io.StringIO()) as out:
                claudex.cmd_search(Namespace(**base))
            return out.getvalue()
        hit = search(query="flatline")
        self.assertIn("[project]", hit)
        self.assertIn(PROJ_UUID, hit)
        hit = search(query="Nikon")
        self.assertIn("[memory]", hit)
        self.assertNotIn("Nikon", search(query="Nikon", source="project"))
        self.assertNotIn("flatline", search(query="flatline", source="memory"))

    def test_show_project_by_uuid_prefix(self):
        self.ingest(self.make_export())
        with redirect_stdout(io.StringIO()) as out:
            rc = claudex.cmd_show(Namespace(session=PROJ_UUID[:8], no_tools=False))
        self.assertEqual(rc, 0)
        self.assertIn("[MEMORY]", out.getvalue())
        self.assertIn("How V feels", out.getvalue())

    def test_export_without_memories_or_projects_still_ingests(self):
        exp = self.make_export(memories=None, projects=())
        out = self.ingest(exp)
        self.assertIn("cloud indexed: 0 written", out)
        self.assertNotIn("projects indexed", out)


class TestDeletedTier(ExportMixin):
    P2 = dict(EXPORT_PROJECT, uuid="0199bbbb-1111-2222-3333-444455556666",
              name="Ephemeral", description="short-lived project",
              docs=[{"uuid": "d9", "filename": "notes.md", "content": "chrome flamingo"}])

    def test_project_deleted_upstream_leaves_normal_search(self):
        both = self.make_export(projects=(EXPORT_PROJECT, self.P2), name="e1")
        self.ingest(both)
        self.assertIn("chrome flamingo", self.search("chrome flamingo"))
        # newer export (bumped updated_at) no longer contains P2
        newer_proj = dict(EXPORT_PROJECT, updated_at="2026-05-01T00:00:00+00:00")
        only_one = self.make_export(projects=(newer_proj,), name="e2")
        out = self.ingest(only_one)
        self.assertIn("moved out of normal search", out)
        self.assertNotIn("chrome flamingo", self.search("chrome flamingo"))
        hit = self.search("chrome flamingo", deleted=True)
        self.assertIn("[project deleted]", hit)
        self.assertIn(self.P2["uuid"], hit)
        # show still resolves it on demand, with a stderr note
        out_s, err_s = io.StringIO(), io.StringIO()
        with redirect_stdout(out_s), redirect_stderr(err_s):
            rc = claudex.cmd_show(Namespace(session=self.P2["uuid"][:8], no_tools=False))
        self.assertEqual(rc, 0)
        self.assertIn("chrome flamingo", out_s.getvalue())
        self.assertIn("deleted upstream", err_s.getvalue())
        # project comes back → restored to normal search
        back = self.make_export(
            projects=(newer_proj, dict(self.P2, updated_at="2026-06-01T00:00:00+00:00")),
            name="e3")
        out = self.ingest(back)
        self.assertIn("restored", out)
        self.assertIn("chrome flamingo", self.search("chrome flamingo"))

    def test_conversation_deletion_needs_full_export(self):
        c1 = make_conv("aaaa1111-0000-0000-0000-000000000001", "the samurai jacket", "2026-01-10T00:00:00+00:00")
        c2 = make_conv("bbbb2222-0000-0000-0000-000000000002", "the arasaka tower", "2026-01-20T00:00:00+00:00")
        full = self.make_export(convs=(c1, c2), name="e1")
        out = self.ingest(full, full=True)
        self.assertNotIn("deletion state unknown", out)
        # windowed export without c1 must NOT mark it deleted — only add c3
        c3 = make_conv("cccc3333-0000-0000-0000-000000000003", "the delamain cab", "2026-02-05T00:00:00+00:00")
        window = self.make_export(convs=(c3,), name="e2")
        self.ingest(window)
        self.assertIn("samurai jacket", self.search("samurai jacket"))
        self.assertIn("delamain cab", self.search("delamain cab"))
        # newer FULL export without c1 → c1 moves to the deleted tier
        full2 = self.make_export(
            convs=(dict(c2, updated_at="2026-03-01T00:00:00+00:00"), c3), name="e3")
        out = self.ingest(full2, full=True)
        self.assertIn("moved out of normal search", out)
        self.assertNotIn("samurai jacket", self.search("samurai jacket"))
        self.assertIn("[cloud deleted]", self.search("samurai jacket", deleted=True))
        self.assertIn("delamain cab", self.search("delamain cab"))

    def test_old_export_cannot_resurrect_deleted_conversation(self):
        c1 = make_conv("aaaa1111-0000-0000-0000-000000000001", "the samurai jacket", "2026-01-10T00:00:00+00:00")
        e1 = self.make_export(convs=(c1,), name="e1")
        self.ingest(e1, full=True)
        c9 = make_conv("dddd9999-0000-0000-0000-000000000009", "the afterlife bar", "2026-05-01T00:00:00+00:00")
        e2 = self.make_export(convs=(c9,), name="e2")
        self.ingest(e2, full=True)
        self.assertNotIn("samurai jacket", self.search("samurai jacket"))
        # re-ingesting the old export merges content but keeps it in the deleted tier
        self.ingest(e1)
        self.assertNotIn("samurai jacket", self.search("samurai jacket"))
        self.assertIn("samurai jacket", self.search("samurai jacket", deleted=True))

    def test_list_deleted_flag(self):
        c1 = make_conv("aaaa1111-0000-0000-0000-000000000001", "hello", "2026-01-10T00:00:00+00:00")
        self.ingest(self.make_export(convs=(c1,), name="e1"), full=True)
        c9 = make_conv("dddd9999-0000-0000-0000-000000000009", "later", "2026-05-01T00:00:00+00:00")
        self.ingest(self.make_export(convs=(c9,), name="e2"), full=True)
        def listing(deleted):
            with redirect_stdout(io.StringIO()) as out:
                claudex.cmd_list(Namespace(limit=0, source=None, no_topic=True, deleted=deleted))
            return out.getvalue()
        self.assertNotIn("aaaa1111", listing(False))
        self.assertIn("[deleted]", listing(True))
        self.assertIn("aaaa1111", listing(True))


class TestPureHelpers(unittest.TestCase):
    def test_extract_text_blocks(self):
        self.assertEqual(claudex.extract_text_blocks("plain"), ["plain"])
        blocks = [{"type": "text", "text": "a"}, {"type": "image"},
                  {"type": "tool_use", "name": "Bash"}, "junk"]
        self.assertEqual(claudex.extract_text_blocks(blocks), ["a", "[image]"])
        self.assertEqual(claudex.extract_text_blocks(None), [])

    def test_summarize_tool_use(self):
        self.assertEqual(
            claudex.summarize_tool_use({"name": "Bash", "input": {"command": "ls"}}),
            "→ Bash: ls")
        self.assertEqual(
            claudex.summarize_tool_use({"name": "Read", "input": {"file_path": "/a"}}),
            "→ Read: /a")

    def test_normalize_keyword(self):
        self.assertEqual(claudex.normalize_keyword('  "Foo  Bar"  '), "foo-bar")
        self.assertEqual(claudex.normalize_keyword("a__b--c"), "a-b-c")

    def test_in_date_range(self):
        meta = {"start": "2026-05-01T00:00:00Z", "end": "2026-05-03T00:00:00Z"}
        self.assertTrue(claudex._in_date_range(meta, None, None))
        self.assertTrue(claudex._in_date_range(meta, "2026-05", None))
        self.assertTrue(claudex._in_date_range(meta, "2026-05-03", None))
        self.assertFalse(claudex._in_date_range(meta, "2026-05-04", None))
        self.assertTrue(claudex._in_date_range(meta, None, "2026-05-01"))
        self.assertFalse(claudex._in_date_range(meta, None, "2026-04"))

    def test_compile_query(self):
        self.assertIsNotNone(claudex.compile_query("a+b"))
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(claudex.compile_query("a("))

    def test_split_transcript_chunks(self):
        header = "# meta\nstuff\n"
        turns = "".join(f"## turn {i}\n{'x' * 50}\n" for i in range(10))
        chunks = claudex.split_transcript_chunks(header + turns, target_chars=120)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertTrue(c.startswith("# meta"))


class TestFrontmatter(TmpDirsMixin):
    def test_bracketed_and_bare_keywords(self):
        claudex.SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
        (claudex.SUMMARIES_DIR / "s1.md").write_text(
            "---\ntopic: A thing\noutcome: resolved\n"
            "keywords: [Foo Bar, baz]\n---\n\nbody\n")
        fm = claudex.read_summary_frontmatter("s1", source="code")
        self.assertEqual(fm["topic"], "A thing")
        self.assertEqual(fm["keywords"], ["foo-bar", "baz"])

    def test_missing_summary(self):
        self.assertEqual(claudex.read_summary_frontmatter("nope", "code"), {})


if __name__ == "__main__":
    unittest.main()
