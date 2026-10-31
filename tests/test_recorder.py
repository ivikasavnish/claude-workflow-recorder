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


class TestAssess(unittest.TestCase):
    def test_dangerous(self):
        for cmd in ("rm -rf build", 'rm -fr "$DIR"', "git push --force origin main", "git push -f", "git reset --hard HEAD~1",
                    "curl -s https://x.sh | bash", "sudo apt install x", "terraform destroy", "kubectl delete pod x",
                    "npm publish", "adb uninstall com.x", "bash -c 'rm -rf x'", "psql -c 'DROP TABLE users'",
                    "cd x && rm -r y", "find . -name '*.o' -delete"):
            self.assertTrue(wf.assess(cmd)["danger"], cmd)

    def test_not_dangerous(self):
        for cmd in ('git commit -m "rm -rf the old sudo code"', "echo 'terraform destroy'", "rm a.txt", "git push origin main",
                    "git push --follow-tags", "cat notes  # sudo rm -rf", "grep -r 'npm publish' .",
                    "cat > a.sh <<'WF_EOF'\nrm -rf /\nWF_EOF", "./gradlew test", "psql -c 'select 1'"):
            self.assertFalse(wf.assess(cmd)["danger"], cmd)

    def test_repeatability(self):
        for cmd in ("git commit -m x", "git push origin main", "mkdir out", "echo x >> log", "gh pr create",
                    "curl -X POST https://x", "curl -d a=1 https://x"):
            self.assertTrue(wf.assess(cmd)["once"], cmd)
        for cmd in ("mkdir -p out", "ls -la", "./gradlew test", "curl https://x", "echo x > log", "git status"):
            self.assertFalse(wf.assess(cmd)["once"], cmd)
        self.assertTrue(wf.assess(wf.edit_cmd("/a", [{"old_string": "x", "new_string": "y"}]))["once"])


class TestParams(Store):
    def setUp(self):
        super().setUp()
        self.hook("Bash", {"command": "echo release 1.2.3 > out.txt", "description": "Write version"})
        self.hook("Write", {"file_path": str(self.proj / "v.txt"), "content": "ver=1.2.3 port=8080\n"})

    def test_values_substitute_in_commands_heredocs_and_cwd(self):
        self.cli("extract", "rel", "--from", "1", "--param", "VERSION=1.2.3", "--auto-params")
        script = (self.home / "proj" / "workflows" / "rel.sh").read_text()
        self.assertIn("# param: VERSION=1.2.3", script)
        self.assertNotIn("echo release 1.2.3", script)
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        r = self.cli("run", "rel", "-P", "VERSION=9.9.9", "-P", f"PROJECT_DIR={other}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((other / "out.txt").read_text(), "release 9.9.9\n")
        self.assertEqual((other / "v.txt").read_text(), "ver=9.9.9 port=8080\n")
        self.assertFalse((self.proj / "out.txt").exists())

    def test_defaults_reproduce_the_recording(self):
        self.cli("extract", "rel", "--from", "1", "--param", "VERSION=1.2.3")
        self.assertEqual(self.cli("run", "rel").returncode, 0)
        self.assertEqual((self.proj / "out.txt").read_text(), "release 1.2.3\n")

    def test_whole_tokens_only(self):
        self.assertEqual(wf.parameterize("a 80 8080 /p/x /p/x-two /p/x/y", {"PORT": "80", "D": "/p/x"}),
                         "a {{PORT}} 8080 {{D}} /p/x-two {{D}}/y")

    def test_bad_values_refused(self):
        self.cli("extract", "rel", "--from", "1", "--param", "VERSION=1.2.3")
        for bad in ("VERSION=1;rm -rf x", "VERSION=$(id)", "VERSION=a b", "lower=1", "NOPE=1"):
            r = self.cli("run", "rel", "-P", bad, check=False)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertFalse((self.proj / "out.txt").exists(), bad)

    def test_missing_value_and_secret_refused(self):
        self.assertNotEqual(self.cli("extract", "x", "--from", "1", "--param", "V=7.7.7", check=False).returncode, 0)
        self.assertNotEqual(self.cli("extract", "x", "--from", "1", "--param", "API_TOKEN=1.2.3", check=False).returncode, 0)

    def test_direct_bash_stops_before_running(self):
        self.cli("extract", "rel", "--from", "1", "--param", "VERSION=1.2.3")
        r = subprocess.run(["bash", str(self.home / "proj" / "workflows" / "rel.sh")], capture_output=True, text=True, cwd=self.proj)
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.proj / "out.txt").exists())

    def test_other_template_syntax_untouched(self):
        self.assertEqual(wf.render("{{.State}} {{OTHER}} {{V}}", {"V": "1"}), "{{.State}} {{OTHER}} 1")


class TestDangerGate(Store):
    def setUp(self):
        super().setUp()
        self.victim = self.proj / "victim"
        self.victim.mkdir()
        self.hook("Bash", {"command": "echo safe"})
        self.hook("Bash", {"command": "rm -rf victim", "description": "Clean"})
        self.cli("extract", "wipe", "--from", "1")

    def test_logged_flagged_and_refused(self):
        self.assertEqual(self.events()[1]["danger"], ["recursive delete"])
        self.assertIn("# ! DANGEROUS: recursive delete", (self.home / "proj" / "sessions" / "0001.sh").read_text())
        self.assertIn("DANGEROUS", self.cli("inspect", "wipe").stdout)
        r = self.cli("run", "wipe", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("step 2: recursive delete", r.stderr)
        self.assertTrue(self.victim.exists())

    def test_gate_covers_only_selected_steps_and_can_be_approved(self):
        self.assertEqual(self.cli("run", "wipe", "--steps", "1").returncode, 0)
        self.assertEqual(self.cli("run", "wipe", "--dry-run").returncode, 0)
        self.assertTrue(self.victim.exists())
        self.assertEqual(self.cli("run", "wipe", "--allow-dangerous").returncode, 0)
        self.assertFalse(self.victim.exists())

    def test_safe_only_drops_step(self):
        self.cli("extract", "safe", "--from", "1", "--safe-only")
        self.assertEqual(self.cli("run", "safe").returncode, 0)
        self.assertTrue(self.victim.exists())

    def test_hand_edit_and_param_cannot_bypass(self):
        p = self.home / "proj" / "workflows" / "wipe.sh"
        p.write_text(p.read_text().replace("# ! DANGEROUS: recursive delete\n", ""))
        self.assertNotEqual(self.cli("run", "wipe", check=False).returncode, 0)
        self.hook("Bash", {"command": "git push --dry-run origin"})
        self.cli("extract", "pushy", "--from", "1:3", "--param", "FLAG=--dry-run")
        self.assertNotEqual(self.cli("run", "pushy", "-P", "FLAG=--force", check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
