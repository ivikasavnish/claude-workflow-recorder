#!/usr/bin/env python3
"""Workflows app: local viewer + runner for the workflow-recorder store (~/workflows).

  /usr/bin/python3 server.py [--port 47821] [--open]

Binds 127.0.0.1 only. Every request must carry the right Host header (blocks DNS
rebinding) and every POST the per-launch token embedded in the page (blocks other
sites from triggering runs).
"""
import argparse
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import wf  # noqa: E402  (shared store/run helpers)

TOKEN = secrets.token_urlsafe(24)
RUNS = {}  # run id -> Run
NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def projects():
    if not wf.ROOT.exists():
        return []
    ps = [p for p in wf.ROOT.iterdir() if (p / "index.json").exists()]
    return sorted(ps, key=lambda p: (p / "index.json").stat().st_mtime, reverse=True)


def pdir_of(name):
    if not name or not NAME_RX.match(name) or not (wf.ROOT / name / "index.json").exists():
        raise ValueError(f"unknown project '{name}'")
    return wf.ROOT / name


def checked_name(name):
    if not name or not NAME_RX.match(name):
        raise ValueError("name must be letters, digits, . _ - (no slashes)")
    return name


# ---------------------------------------------------------------- runs

class Run:
    def __init__(self, project, name, script, chosen, log, env):
        self.id = secrets.token_hex(6)
        self.project, self.name, self.log = project, name, log
        self.steps = [{"n": s["n"], "title": s["title"]} for s in chosen]
        self.lines, self.rc, self.started, self.ended = [], None, time.time(), None
        self.proc = subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-c", script], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     start_new_session=True)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        with open(self.log, "w") as f:
            for line in self.proc.stdout:
                self.lines.append(line.rstrip("\n"))
                f.write(line)
                f.flush()
            self.rc = self.proc.wait()
            self.ended = time.time()
            f.write(f"\n::wf-exit {self.rc}\n")

    def stop(self):
        if self.rc is None:
            os.killpg(self.proc.pid, signal.SIGTERM)

    def summary(self):
        return {"id": self.id, "project": self.project, "name": self.name, "steps": self.steps,
                "rc": self.rc, "running": self.rc is None, "log": self.log.name,
                "started": self.started, "ended": self.ended}


# ---------------------------------------------------------------- API

def api_get(path, q):
    g = lambda k, d=None: q.get(k, [d])[0]
    if path == "/api/projects":
        out = []
        for p in projects():
            idx = json.loads((p / "index.json").read_text())
            out.append({"name": p.name, "sessions": len(idx["sessions"]),
                        "workflows": len(list((p / "workflows").glob("*.sh")))})
        return out
    if path == "/api/sessions":
        p = pdir_of(g("project"))
        idx = json.loads((p / "index.json").read_text())
        out = []
        for s in sorted(idx["sessions"].values(), key=lambda s: -s["n"]):
            f = p / "sessions" / f"{s['n']:04d}.jsonl"
            ev = [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.exists() else []
            out.append({"n": s["n"], "started": s["started"], "branch": s.get("branch"),
                        "steps": sum(1 for e in ev if e["type"] == "cmd"),
                        "failed": sum(1 for e in ev if e["type"] == "cmd" and e["status"] != "ok"),
                        "tasks": [e["title"] for e in ev if e["type"] == "mark"]})
        return out
    if path == "/api/session":
        p = pdir_of(g("project"))
        return wf.load(p, int(g("n")))
    if path == "/api/search":
        rx = re.compile(re.escape(g("q", "")), re.I)
        p = pdir_of(g("project"))
        out = []
        for f in sorted((p / "sessions").glob("*.jsonl"), reverse=True):
            for e in (json.loads(l) for l in f.read_text().splitlines() if l.strip()):
                if e["type"] == "cmd" and (rx.search(e["command"]) or rx.search(e["description"])):
                    out.append({**e, "session": int(f.stem)})
                    if len(out) >= 300:
                        return out
        return out
    if path == "/api/workflows":
        p = pdir_of(g("project"))
        out = []
        for f in sorted((p / "workflows").glob("*.sh"), key=lambda f: -f.stat().st_mtime):
            text = f.read_text()
            lines = text.splitlines()
            out.append({"name": f.stem, "description": lines[2].lstrip("# ") if len(lines) > 2 else "",
                        "steps": len(wf.parse_workflow(text)), "modified": f.stat().st_mtime})
        return out
    if path == "/api/workflow":
        p = pdir_of(g("project"))
        f = p / "workflows" / f"{checked_name(g('name'))}.sh"
        if not f.exists():
            raise ValueError("no such workflow")
        text = f.read_text()
        return {"name": f.stem, "script": text, "steps": wf.parse_workflow(text), "path": str(f)}
    if path == "/api/runs":
        p = pdir_of(g("project"))
        live = {r.log.name: r.id for r in RUNS.values() if r.project == p.name}
        logs = sorted((p / "runs").glob("*.log"), key=lambda f: -f.stat().st_mtime)[:100] if (p / "runs").exists() else []
        out = []
        for f in logs:
            tail = f.read_text(errors="replace")[-200:]
            m = re.search(r"::wf-exit (-?\d+)", tail)
            out.append({"file": f.name, "modified": f.stat().st_mtime, "live": live.get(f.name),
                        "rc": int(m.group(1)) if m else None})
        return out
    if path == "/api/runlog":
        p = pdir_of(g("project"))
        f = p / "runs" / checked_name(g("file"))
        return {"file": f.name, "text": f.read_text(errors="replace")[-400000:]}
    if path == "/api/run":
        r = RUNS.get(g("id"))
        if not r:
            raise ValueError("unknown run")
        since = int(g("since", "0"))
        return {**r.summary(), "lines": r.lines[since:], "next": len(r.lines)}
    raise LookupError(path)


def api_post(path, b):
    if path == "/api/extract":
        p = pdir_of(b.get("project"))
        args = [sys.executable, str(HERE.parent / "wf.py"), "-p", p.name, "extract", checked_name(b.get("name")),
                "--from", *b["sources"]]
        if b.get("description"):
            args += ["-d", b["description"]]
        if b.get("include_failed"):
            args.append("--include-failed")
        if b.get("keep_dupes"):
            args.append("--keep-dupes")
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise ValueError((r.stderr or r.stdout).strip())
        return {"ok": True, "name": b["name"]}
    if path == "/api/workflow/save":
        p = pdir_of(b.get("project"))
        f = p / "workflows" / f"{checked_name(b.get('name'))}.sh"
        f.write_text(b["script"])
        f.chmod(0o755)
        return {"ok": True}
    if path == "/api/run":
        p = pdir_of(b.get("project"))
        only = set(int(x) for x in b["steps"]) if b.get("steps") else None
        script, chosen, log = wf.prepare_run(p, checked_name(b.get("name")), only)
        extra = {}
        for line in (b.get("env") or "").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                extra[k] = v
        run = Run(p.name, b["name"], script, chosen, log, wf.isolated_env(extra, bool(b.get("inherit_env"))))
        RUNS[run.id] = run
        return run.summary()
    if path == "/api/run/stop":
        r = RUNS.get(b.get("id"))
        if r:
            r.stop()
        return {"ok": True}
    if path == "/api/quit":
        threading.Timer(0.3, lambda: os._exit(0)).start()
        return {"ok": True}
    raise LookupError(path)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "Workflows/1.0"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        return self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, fn, *args):
        try:
            self._send(200, fn(*args))
        except LookupError:
            self._send(404, {"error": "not found"})
        except (ValueError, KeyError, json.JSONDecodeError) as ex:
            self._send(400, {"error": str(ex)})
        except Exception as ex:  # keep the server up, show the error in the UI
            self._send(500, {"error": f"{type(ex).__name__}: {ex}"})

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urlparse(self.path)
        if u.path == "/health":
            return self._send(200, {"ok": True})
        if u.path in ("/", "/index.html"):
            html = (HERE / "index.html").read_text().replace("__WF_TOKEN__", TOKEN).replace("__WF_ROOT__", str(wf.ROOT)).replace("__WF_HOMEDIR__", str(Path.home()))
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        self._dispatch(api_get, u.path, parse_qs(u.query))

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-WF-Token") != TOKEN:
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        self._dispatch(api_post, urlparse(self.path).path, body)


def main():
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=47821)
    ap.add_argument("--open", action="store_true", help="open the UI in a browser")
    a = ap.parse_args()
    PORT = a.port
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Workflows app on {url} (store {wf.ROOT})", flush=True)
    if a.open:
        threading.Timer(0.5, lambda: subprocess.run(["open", url])).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
