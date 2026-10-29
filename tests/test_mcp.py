"""MCP tests: the workflows server directly, and through the router."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, cmd, env):
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, env=env)
        self.i = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        self.p.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.p.stdin.flush()

    def rpc(self, method, params):
        self.i += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.i, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        while True:
            m = json.loads(self.p.stdout.readline())
            if m.get("id") == self.i:
                return m["result"]

    def call(self, name, args=None):
        r = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        text = r["content"][0]["text"]
        return (json.loads(text) if not r.get("isError") else text), bool(r.get("isError"))

    def close(self):
        self.p.stdin.close()
        self.p.terminate()
        self.p.wait()
        self.p.stdout.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.env = {**os.environ, "WF_HOME": str(t / "store")}
        proj = t / "proj"
        proj.mkdir()
        for cmd, desc in [("./gradlew assembleDebug", "Build debug APK"), ("adb logcat -d", "Dump logcat for crash")]:
            ev = {"hook_event_name": "PostToolUse", "session_id": "s", "cwd": str(proj), "tool_name": "Bash",
                  "tool_input": {"command": cmd, "description": desc}, "tool_response": {}}
            subprocess.run([sys.executable, str(ROOT / "skill" / "wf.py"), "hook"], input=json.dumps(ev), text=True,
                           env=self.env, check=True)

    def tearDown(self):
        self.tmp.cleanup()


class TestWorkflowsServer(Base):
    def test_search_and_get(self):
        c = Client([sys.executable, str(ROOT / "skill" / "wf_mcp.py")], self.env)
        try:
            names = [t["name"] for t in c.rpc("tools/list", {})["tools"]]
            self.assertIn("search_steps", names)
            res, err = c.call("search_steps", {"query": "logcat crash"})
            self.assertFalse(err)
            self.assertEqual(res["matches"][0]["ref"], "proj#1:2")
            res, _ = c.call("get_steps", {"session": 1, "steps": "1"})
            self.assertEqual(res["items"][0]["command"], "./gradlew assembleDebug")
            _, err = c.call("get_workflow", {"name": "missing"})
            self.assertTrue(err)
        finally:
            c.close()


class TestRouter(Base):
    def test_router_only_exposes_search_and_proxies(self):
        cfg = Path(self.tmp.name) / "router" / "servers.json"
        cfg.parent.mkdir()
        cfg.write_text(json.dumps({"servers": {
            "workflows": {"description": "recorded commands", "command": sys.executable,
                          "args": [str(ROOT / "skill" / "wf_mcp.py")], "env": {"WF_HOME": self.env["WF_HOME"]}},
            "off": {"enabled": False, "command": "false"}}}))
        c = Client([sys.executable, str(ROOT / "router" / "router.py")], {**self.env, "MCP_ROUTER_CONFIG": str(cfg)})
        try:
            names = sorted(t["name"] for t in c.rpc("tools/list", {})["tools"])
            self.assertEqual(names, ["call_tool", "list_servers", "refresh_index", "search_tools"])
            res, _ = c.call("search_tools", {"query": "search recorded steps", "limit": 2})
            self.assertEqual(res["tools"][0]["tool"], "workflows__search_steps")
            self.assertIn("inputSchema", res["tools"][0])
            r = c.rpc("tools/call", {"name": "call_tool", "arguments": {"tool": "workflows__search_steps", "arguments": {"query": "gradlew"}}})
            self.assertIn("proj#1:1", r["content"][0]["text"])
            _, err = c.call("call_tool", {"tool": "off__x"})
            self.assertTrue(err)
            self.assertTrue((cfg.parent / "index.json").exists())
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
