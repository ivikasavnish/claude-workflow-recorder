"""Recorder, translation, extraction and runner tests. Run: python3 -m unittest discover -s tests"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / "skill" / "wf.py"
sys.path.insert(0, str(ROOT / "skill"))
import wf  # noqa: E402


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "store"
        self.proj = Path(self.tmp.name) / "proj"
        self.proj.mkdir()
        self.env = {**os.environ, "WF_HOME": str(self.home)}

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, tool, ti, event="PostToolUse", sid="s1", **extra):
        ev = {"hook_event_name": event, "session_id": sid, "cwd": str(self.proj), "tool_name": tool,
              "tool_input": ti, "tool_response": {}, **extra}
        return subprocess.run([sys.executable, str(WF), "hook"], input=json.dumps(ev), text=True,
                              capture_output=True, env=self.env, check=True).stdout

    def cli(self, *args, check=True):
        return subprocess.run([sys.executable, str(WF), "-p", "proj", *args], text=True,
                              capture_output=True, env=self.env, check=check)

    def events(self, n=1):
        p = self.home / "proj" / "sessions" / f"{n:04d}.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()]


class TestHook(Store):
    def test_incremental_session_ids_and_resume(self):
        out = self.hook(None, {}, event="SessionStart", sid="a")
        self.assertIn("Session #0001 started", out)
        self.assertIn("Session #0002 started", self.hook(None, {}, event="SessionStart", sid="b"))
        self.assertIn("Session #0001 resumed", self.hook(None, {}, event="SessionStart", sid="a"))

    def test_bash_logged_with_description_category_and_failure(self):
        self.hook("Bash", {"command": "./gradlew :app:testDebugUnitTest", "description": "Run unit tests"})
        self.hook("Bash", {"command": "adb install -r a.apk", "description": "Install"},
                  event="PostToolUseFailure", error="x\nadb: no devices")
        e1, e2 = self.events()
        self.assertEqual((e1["category"], e1["status"], e1["description"]), ("test", "ok", "Run unit tests"))
        self.assertEqual((e2["category"], e2["status"], e2["error"]), ("run", "FAILED", "adb: no devices"))
        log = (self.home / "proj" / "sessions" / "0001.sh").read_text()
        self.assertIn("# Run unit tests\n", log)
        self.assertIn("# ! adb: no devices", log)

    def test_secrets_redacted_and_wf_calls_skipped(self):
        self.hook("Bash", {"command": "curl -H 'Authorization: Bearer abc123' x && API_KEY=sk-abcdefghijklmnopqrst y"})
        self.hook("Bash", {"command": "wf list"})
        (e,) = self.events()
        self.assertNotIn("abc123", e["command"])
        self.assertNotIn("sk-abcdefghijklmnopqrst", e["command"])
        self.assertEqual(e["command"].count("<REDACTED>"), 2)

    def test_skipped_tools_not_logged(self):
        self.hook("Bash", {"command": "echo hi"})
        self.hook("TodoWrite", {"todos": []})
        self.assertEqual(len(self.events()), 1)

    def test_mark_assigns_task(self):
        self.hook("Bash", {"command": "echo a"})
        self.cli("mark", "Build")
        self.hook("Bash", {"command": "echo b"})
        cmds = [e for e in self.events() if e["type"] == "cmd"]
        self.assertEqual([c["task"] for c in cmds], [0, 1])


class TestTranslate(unittest.TestCase):
    def tr(self, tool, ti, files=Path("/nonexistent")):
        return wf.translate(tool, ti, files, 1)

    def test_read(self):
        self.assertEqual(self.tr("Read", {"file_path": "/a b"})[0], "cat '/a b'  # [Read]")
        self.assertEqual(self.tr("Read", {"file_path": "/a", "offset": 10, "limit": 5})[0], "sed -n '10,14p' /a  # [Read]")

    def test_grep_and_glob(self):
        cmd = self.tr("Grep", {"pattern": "foo|bar", "path": "src", "output_mode": "content", "-i": True, "glob": "**/*.kt"})[0]
        self.assertTrue(cmd.startswith("grep -rE --exclude-dir=.git -n -i '--include=*.kt' -e 'foo|bar' src"), cmd)
        self.assertIn("find src -type f -name '*.kt'", self.tr("Glob", {"pattern": "**/*.kt", "path": "src"})[0])

    def test_sensitive_paths_not_logged(self):
        cmd, _, cat, _ = self.tr("Write", {"file_path": "/p/.env", "content": "SECRET=1\n"})
        self.assertTrue(cmd.startswith("#"))
        self.assertNotIn("SECRET=1", cmd)

    def test_unknown_tool_is_comment(self):
        cmd, desc, cat, _ = self.tr("mcp__x__y", {"q": "z"})
        self.assertEqual(cat, "note")
        self.assertTrue(all(l.startswith("#") for l in cmd.splitlines()))


class TestReplayFidelity(Store):
    def test_write_and_edit_replay_exactly(self):
        target, big = self.proj / "a.txt", self.proj / "big.txt"
        tricky = "x \"q\" $HOME 'y' `z`\n"
        self.hook("Write", {"file_path": str(target), "content": "hello\nworld\n"})
        self.hook("Edit", {"file_path": str(target), "old_string": "hello", "new_string": tricky})
        self.hook("Write", {"file_path": str(big), "content": "no newline at end"})
        self.cli("extract", "replay", "--from", "1")
        r = self.cli("run", "replay")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(target.read_text(), tricky + "\nworld\n")
        self.assertEqual(big.read_text(), "no newline at end")


class TestRunner(Store):
    SCRIPT = """#!/usr/bin/env bash
# Workflow: demo
# Demo
set -euo pipefail

# ── 1. [run] one   (from #0001 step 1)
cd /tmp
echo "one $(pwd)"

# ── 2. [run] two   (from #0001 step 2)
echo two

# ── 3. [run] three   (from #0001 step 3)
cd /
echo "three $(pwd)"
false

# ── 4. [run] four   (from #0001 step 4)
echo "four $(pwd)"
"""

    def setUp(self):
        super().setUp()
        d = self.home / "proj" / "workflows"
        d.mkdir(parents=True)
        (self.home / "proj" / "index.json").write_text('{"next": 1, "sessions": {}}')
        (d / "demo.sh").write_text(self.SCRIPT)

    def test_parse_tracks_cwd(self):
        steps = wf.parse_workflow(self.SCRIPT)
        self.assertEqual([(s["n"], s["cwd"]) for s in steps], [(1, "/tmp"), (2, "/tmp"), (3, "/"), (4, "/")])

    def test_single_step_runs_in_recorded_dir(self):
        r = self.cli("run", "demo", "--steps", "4")
        self.assertEqual(r.returncode, 0)
        self.assertIn("four /\n", r.stdout)
        self.assertNotIn("one", r.stdout)

    def test_run_all_stops_at_failure(self):
        r = self.cli("run", "demo", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("three /", r.stdout)
        self.assertNotIn("four", r.stdout)
        log = next((self.home / "proj" / "runs").glob("demo-*-all.log")).read_text()
        self.assertIn("::wf-exit 1", log)

    def test_redacted_steps_refused(self):
        p = self.home / "proj" / "workflows" / "demo.sh"
        p.write_text(self.SCRIPT.replace("echo two", "echo <REDACTED>"))
        self.assertNotEqual(self.cli("run", "demo", "--steps", "2", check=False).returncode, 0)
        self.assertEqual(self.cli("run", "demo", "--steps", "4").returncode, 0)


if __name__ == "__main__":
    unittest.main()
