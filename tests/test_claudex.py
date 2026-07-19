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
