"""Fix memory, `wf fix`, `wf run --fix` and the ACP client. Run: python3 -m unittest discover -s tests"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / "skill" / "wf.py"
sys.path.insert(0, str(ROOT / "skill"))
import wf  # noqa: E402
import wf_fix  # noqa: E402

# A minimal ACP agent: reads calc.py through the client, asks permission, writes the fix through the client.
FAKE_AGENT = textwrap.dedent(r'''
    import json, re, sys
    def send(m):
        m["jsonrpc"] = "2.0"; sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
    def call(rid, method, params):
        send({"id": rid, "method": method, "params": params})
        for line in sys.stdin:
            m = json.loads(line)
            if m.get("id") == rid and "method" not in m:
                return m
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("method") == "initialize":
            assert m["params"]["clientCapabilities"]["fs"]["writeTextFile"]
            send({"id": m["id"], "result": {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []}})
        elif m.get("method") == "session/new":
            send({"id": m["id"], "result": {"sessionId": "s1"}})
        elif m.get("method") == "session/prompt":
            text = m["params"]["prompt"][0]["text"]
            root = re.search(r"Project root: (.+)", text).group(1).strip()
            path = root + "/calc.py"
            src = call(100, "fs/read_text_file", {"sessionId": "s1", "path": path})["result"]["content"]
            perm = call(101, "session/request_permission", {"sessionId": "s1",
                "toolCall": {"toolCallId": "t1", "title": "Edit calc.py", "kind": "edit", "rawInput": {"path": path}},
                "options": [{"optionId": "ok", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "no", "name": "Reject", "kind": "reject_once"}]})
            if perm["result"]["outcome"].get("optionId") == "ok":
                call(102, "fs/write_text_file", {"sessionId": "s1", "path": path, "content": src.replace("a - b", "a + b")})
            send({"method": "session/update", "params": {"sessionId": "s1", "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "add() subtracted; now adds."}}}})
            send({"id": m["id"], "result": {"stopReason": "end_turn"}})
''')

BUGGY = "def add(a, b):\n    return a - b\n"
CHECK = "python3 -B -c 'import calc; assert calc.add(2, 2) == 4, \"add is broken\"'"


class FixBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.home, self.proj = base / "store", base / "proj"
        self.proj.mkdir()
        (self.proj / "calc.py").write_text(BUGGY)
        g = lambda *a: subprocess.run(["git", "-C", str(self.proj), *a], check=True, capture_output=True)
        g("init", "-q"); g("add", "."); g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self.env = {**os.environ, "WF_HOME": str(self.home), "WF_ACP_NPX": "0"}
        self.home.mkdir()
        agent = base / "fake_agent.py"
        agent.write_text(FAKE_AGENT)
        (self.home / "agents.json").write_text(json.dumps({"agents": {"fake": {"acp": [[sys.executable, str(agent)]]}}}))
        wf.ROOT = self.home

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, tool, ti, event="PostToolUse", sid="s1", **extra):
        ev = {"hook_event_name": event, "session_id": sid, "cwd": str(self.proj), "tool_name": tool,
              "tool_input": ti, "tool_response": {}, **extra}
        return subprocess.run([sys.executable, str(WF), "hook"], input=json.dumps(ev), text=True,
                              capture_output=True, env=self.env, check=True).stdout

    def cli(self, *args, check=False):
        return subprocess.run([sys.executable, str(WF), "-p", "proj", *args], text=True, cwd=self.proj,
                              capture_output=True, env=self.env, check=check, stdin=subprocess.DEVNULL)

    def fixes(self):
        return wf_fix.load(self.home / "proj")

    def record_fix_session(self, sid="s1"):
        """Claude: check fails -> edits calc.py -> check passes."""
        self.hook("Bash", {"command": CHECK, "description": "Check add()"}, event="PostToolUseFailure", sid=sid,
                  error="Traceback...\nAssertionError: add is broken")
        self.hook("Bash", {"command": "sed -i.bak 's/a - b/a + b/' calc.py", "description": "Fix add()"}, sid=sid)
        self.hook("Bash", {"command": CHECK, "description": "Check add() again"}, sid=sid)


class TestLearning(FixBase):
    def test_hook_learns_the_steps_between_failure_and_success(self):
        self.record_fix_session()
        (fx,) = self.fixes()
        self.assertEqual(fx["kind"], "steps")
        self.assertEqual([s["command"] for s in fx["steps"]], ["sed -i.bak 's/a - b/a + b/' calc.py"])
        self.assertEqual(fx["error"], "AssertionError: add is broken")
        self.assertEqual(fx["source"], "#0001:1->3")

    def test_known_error_is_hinted_to_claude(self):
        self.record_fix_session()
        out = self.hook("Bash", {"command": CHECK, "description": "Check"}, event="PostToolUseFailure", sid="s2",
                        error="AssertionError: add is broken")
        ctx = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "PostToolUseFailure")
        self.assertIn("sed -i.bak 's/a - b/a + b/' calc.py", ctx["additionalContext"])

    def test_unrelated_error_gets_no_hint(self):
        self.record_fix_session()
        out = self.hook("Bash", {"command": "make deploy"}, event="PostToolUseFailure", sid="s2",
                        error="make: *** No rule to make target 'deploy'")
        self.assertEqual(out.strip(), "")

    def test_retry_that_just_passes_teaches_nothing_and_learn_is_idempotent(self):
        self.hook("Bash", {"command": "npm test"}, event="PostToolUseFailure", error="flaky timeout")
        self.hook("Bash", {"command": "npm test"})
        self.assertEqual(self.fixes(), [])
        self.record_fix_session()
        self.assertIn("learned 0 new", self.cli("learn").stdout)
        self.assertEqual(len(self.fixes()), 1)

    def test_changed_command_is_learned_as_replacement(self):
        self.hook("Bash", {"command": "python3 -m pytest tests"}, event="PostToolUseFailure",
                  error="No module named pytest")
        self.hook("Bash", {"command": "python3 -m pytest tests -p no:cacheprovider"})
        (fx,) = self.fixes()
        self.assertEqual((fx["kind"], fx["retry"]), ("command", "python3 -m pytest tests -p no:cacheprovider"))


class TestRanking(FixBase):
    def test_confidence_and_retirement(self):
        pdir = self.home / "proj"
        (pdir / "sessions").mkdir(parents=True)
        mk = lambda src, w, l: {**wf_fix.entry(pdir, "session", src, CHECK, str(self.proj), "AssertionError: add is broken",
                                               "command", retry="true"), "wins": w, "losses": l}
        for e in (mk("good", 5, 0), mk("meh", 1, 2), mk("dead", 0, 3)):
            wf_fix.upsert(pdir, e)
        hits = wf_fix.lookup(CHECK, "x\nAssertionError: add is broken\n", pdir)
        self.assertEqual([h[2]["source"] for h in hits], ["good", "meh"])

    def test_error_matching_ignores_paths_and_numbers(self):
        a = wf_fix.error_match("FileNotFoundError: /home/a/x/config.yml line 12", "FileNotFoundError: /Users/b/config.yml line 40")
        self.assertGreater(a, 0.9)
        self.assertLess(wf_fix.error_match("FileNotFoundError: config.yml", "permission denied"), 0.35)


class TestPrompt(unittest.TestCase):
    def test_permission_prompt_reads_the_terminal(self):
        """Regression: /dev/tty opened "r+" raises (a terminal isn't seekable), which silently denied every request."""
        import pty
        import time
        pid, fd = pty.fork()
        if pid == 0:
            import wf_acp
            print("answer:", repr(wf_acp.ask("ok? ")), flush=True)
            os._exit(0)
        buf, deadline = b"", time.time() + 10
        while b"ok? " not in buf and time.time() < deadline:
            buf += os.read(fd, 100)
        os.write(fd, b"y\n")
        try:
            while b"answer:" not in buf or not buf.rstrip().endswith(b"'"):
                buf += os.read(fd, 100)
        except OSError:
            pass
        os.waitpid(pid, 0)
        self.assertIn(b"answer: 'y'", buf)


class TestEventStream(unittest.TestCase):
    def test_questions_and_events_over_stdio(self):
        """The app's protocol. Regression: question metadata carries its own "kind" (permission/confirm)."""
        import io
        out = io.StringIO()
        es = wf_fix.EventStream(out, io.StringIO('junk\n{"ask": 1, "answer": "y"}\n'))
        es.write("partial agent text")
        self.assertEqual(es.ask("allow?", {"kind": "permission", "title": "Edit calc.py"}), "y")
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "partial agent text")  # events always start on their own line
        ask = json.loads(lines[1][len(es.PREFIX):])
        self.assertEqual((ask["type"], ask["kind"], ask["id"]), ("ask", "permission", 1))
        self.assertEqual(json.loads(lines[2][len(es.PREFIX):]), {"id": 1, "answer": "y", "type": "answered"})

    def test_no_app_means_no(self):
        import io
        self.assertIsNone(wf_fix.EventStream(io.StringIO(), io.StringIO("")).ask("allow?"))


class TestMCP(FixBase):
    def test_any_agent_can_find_record_and_rate_fixes(self):
        import wf_mcp
        wf_mcp.ROOT = self.home
        self.record_fix_session()
        (hit,) = wf_mcp.t_find_fixes({"error": "AssertionError: add is broken", "command": CHECK})
        self.assertEqual((hit["kind"], hit["project"]), ("steps", "proj"))
        r = wf_mcp.t_record_fix({"project": "proj", "command": "npm run build", "error": "Cannot find module 'left-pad'",
                                 "steps": ["npm install left-pad"], "agent": "codex", "summary": "missing dependency"})
        self.assertTrue(r["new"])
        (hit,) = wf_mcp.t_find_fixes({"error": "Error: Cannot find module 'left-pad'", "command": "npm run build"})
        self.assertEqual((hit["id"], hit["origin"]), (r["id"], "mcp:codex"))
        self.assertEqual(wf_mcp.t_fix_outcome({"project": "proj", "id": r["id"], "worked": False})["losses"], 1)


class TestFix(FixBase):
    def test_memory_fix_is_applied_verified_and_counted(self):
        self.record_fix_session()
        (self.proj / "calc.py").write_text(BUGGY)  # the bug is back
        r = self.cli("fix", "--cmd", CHECK, "--agent", "none", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("fixed by remembered fix", r.stdout)
        self.assertIn("a + b", (self.proj / "calc.py").read_text())
        self.assertEqual(self.fixes()[0]["wins"], 2)

    def test_memory_fix_is_not_applied_without_approval(self):
        self.record_fix_session()
        (self.proj / "calc.py").write_text(BUGGY)
        r = self.cli("fix", "--cmd", CHECK, "--agent", "none")  # no tty, no --yes: declined
        self.assertEqual(r.returncode, 1)
        self.assertIn("a - b", (self.proj / "calc.py").read_text())

    def test_agent_fixes_over_acp_and_the_patch_is_replayed_later(self):
        r = self.cli("fix", "--cmd", CHECK, "--agent", "fake", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("asking fake over ACP", r.stdout)
        self.assertIn("add() subtracted; now adds.", r.stdout)
        (fx,) = self.fixes()
        self.assertEqual((fx["kind"], fx["origin"]), ("patch", "agent:fake"))
        self.assertIn("+    return a + b", fx["patch"])
        log = next((self.home / "proj" / "runs").glob("fix-*.log")).read_text()
        self.assertIn('"method": "session/prompt"', log)
        # the bug comes back: memory fixes it with no agent at all
        subprocess.run(["git", "-C", str(self.proj), "checkout", "-q", "calc.py"], check=True)
        r = self.cli("fix", "--cmd", CHECK, "--agent", "none", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"fixed by remembered fix {fx['id']}", r.stdout)

    def test_agent_write_rejected_without_approval(self):
        r = self.cli("fix", "--cmd", CHECK, "--agent", "fake", "--attempts", "1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("a - b", (self.proj / "calc.py").read_text())
        self.assertEqual(self.fixes(), [])

    def test_agent_cannot_write_outside_the_project(self):
        c = __import__("wf_acp").ACPClient([sys.executable, "-c", "import sys; sys.stdin.read()"], self.proj,
                                           self.proj, None)
        try:
            with self.assertRaises(PermissionError):
                c.on_write({"path": str(self.home / "x.txt"), "content": "x"})
            with self.assertRaises(PermissionError):
                c.on_read({"path": str(self.proj / ".env")})
        finally:
            c.close()

    def test_last_failed_step(self):
        self.hook("Bash", {"command": CHECK, "description": "Check add()"}, event="PostToolUseFailure",
                  error="AssertionError: add is broken")
        r = self.cli("fix", "--last", "--agent", "fake", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("step 1: Check add()", r.stdout)

    def test_run_fix_repairs_a_step_and_continues(self):
        for i, cmd in enumerate([CHECK, "echo after > done.txt"], 1):
            self.hook("Bash", {"command": cmd, "description": f"step {i}"})
        self.cli("extract", "check", "--from", "1", check=True)
        r = self.cli("run", "check", "--fix", "--agent", "fake", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("step 1 fixed, continuing", r.stdout)
        self.assertTrue((self.proj / "done.txt").exists())

    def test_agents_lists_configured_agent(self):
        out = self.cli("agents").stdout
        self.assertRegex(out, r"fake\s+acp")
        self.assertRegex(out, r"muse\s+(exec|-)")


if __name__ == "__main__":
    unittest.main()
