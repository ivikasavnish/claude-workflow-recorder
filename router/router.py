#!/usr/bin/env python3
"""mcp-router: one MCP server in front of many (stdio, stdlib only).

Clients see 4 tools instead of every server's full tool list:
  search_tools(query)        -> only the relevant tools, with their input schemas
  call_tool(tool, arguments) -> proxied to the owning server
  list_servers / refresh_index

Backends are stdio MCP servers listed in servers.json (next to this file). They
start lazily and stay up for the router's lifetime, so stateful servers
(e.g. a browser) keep their state between calls. Tool lists are cached in
index.json so searching doesn't have to boot every backend each session.
"""
import hashlib
import json
import math
import os
import re
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("MCP_ROUTER_CONFIG", HERE / "servers.json"))
INDEX = CONFIG.parent / "index.json"  # tool-list cache lives next to its config
SEP = "__"
LAUNCH_CWD = os.getcwd()


def log(*a):
    print("[mcp-router]", *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- backend client

class Backend:
    def __init__(self, name, spec):
        self.name, self.spec = name, spec
        self.proc, self.next_id, self._buf = None, 1, b""
        self.lock = threading.Lock()

    def _start(self):
        env = {**os.environ, **{k: os.path.expandvars(v) for k, v in (self.spec.get("env") or {}).items()}}
        cmd = [os.path.expanduser(self.spec["command"])] + [os.path.expanduser(a) for a in self.spec.get("args", [])]
        log("starting", self.name, " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
                                     env=env, cwd=os.path.expanduser(self.spec.get("cwd", "~")), bufsize=0)
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {"roots": {}},
                                 "clientInfo": {"name": "mcp-router", "version": "1.0"}}, timeout=90)
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _write(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def _rpc(self, method, params, timeout=120):
        rid = self.next_id
        self.next_id += 1
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while True:
            for raw in self._lines(deadline):
                if not raw.strip():
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("id") == rid and "method" not in msg:
                    if "error" in msg:
                        raise RuntimeError(f"{self.name}: {msg['error'].get('message')}")
                    return msg["result"]
                if "method" in msg and msg.get("id") is not None:
                    if msg["method"] == "roots/list":  # the project dir the client launched us in
                        self._write({"jsonrpc": "2.0", "id": msg["id"], "result": {"roots": [
                            {"uri": Path(LAUNCH_CWD).as_uri(), "name": Path(LAUNCH_CWD).name}]}})
                    else:  # sampling, elicitation...: not supported
                        self._write({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})

    def _lines(self, deadline):
        """Yield complete lines from stdout until the caller returns."""
        fd = self.proc.stdout.fileno()
        while True:
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                yield line
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError(f"{self.name}: no response within timeout")
            if select.select([fd], [], [], left)[0]:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    self.proc = None
                    raise ConnectionError(f"{self.name}: server exited")
                self._buf += chunk

    def request(self, method, params, timeout=120):
        with self.lock:
            if not self.proc or self.proc.poll() is not None:
                self._buf = b""
                self._start()
            return self._rpc(method, params, timeout)

    def list_tools(self):
        tools, cursor = [], None
        while True:
            res = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ---------------------------------------------------------------- index & search

def load_config():
    cfg = json.loads(CONFIG.read_text())
    return {k: v for k, v in cfg["servers"].items() if v.get("enabled", True)}


def spec_hash(spec):
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]


SERVERS = load_config()
BACKENDS = {name: Backend(name, spec) for name, spec in SERVERS.items()}


INDEX_LOCK = threading.Lock()


def load_index(refresh=False, only=None):
    with INDEX_LOCK:
        return _load_index(refresh, only)


def _load_index(refresh, only):
    idx = json.loads(INDEX.read_text()) if INDEX.exists() else {}
    changed, errors = False, {}
    for name, spec in SERVERS.items():
        if only and name != only:
            continue
        ent = idx.get(name)
        if ent and ent.get("hash") == spec_hash(spec) and not refresh:
            continue
        try:
            idx[name] = {"hash": spec_hash(spec), "indexed": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "tools": BACKENDS[name].list_tools()}
            changed = True
        except Exception as ex:
            errors[name] = str(ex)
            log("index failed", name, ex)
    for stale in set(idx) - set(SERVERS):
        del idx[stale]
        changed = True
    if changed:
        INDEX.write_text(json.dumps(idx, indent=1))
    return idx, errors


def tokens(s):
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s or "")
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) > 1]


def search(query, limit, server=None):
    idx, errors = load_index()
    docs = []
    for name, ent in idx.items():
        if server and name != server:
            continue
        hint = SERVERS.get(name, {}).get("description", "")
        for t in ent["tools"]:
            params = list((t.get("inputSchema") or {}).get("properties", {}))
            toks = tokens(t["name"]) * 3 + (tokens(name) + tokens(hint)) * 2 + tokens(t.get("description")) + tokens(" ".join(params))
            docs.append((name, t, toks))
    q = tokens(query)
    n = len(docs) or 1
    df = {w: sum(1 for d in docs if w in d[2] or any(x.startswith(w) for x in d[2])) for w in q}
    scored = []
    for name, t, toks in docs:
        s = 0.0
        for w in q:
            idf = math.log(1 + n / (1 + df[w]))
            exact = toks.count(w)
            s += idf * (min(exact, 4) if exact else 0.5 * any(x.startswith(w) and len(w) >= 3 for x in toks))
        if s:
            scored.append((s, name, t))
    scored.sort(key=lambda x: -x[0])
    return [{"tool": f"{name}{SEP}{t['name']}", "server": name, "description": (t.get("description") or "")[:400],
             "inputSchema": t.get("inputSchema", {})} for _, name, t in scored[:limit]], errors


# ---------------------------------------------------------------- router tools

def server_summary():
    return "; ".join(f"{k} ({v.get('description', '')})" for k, v in SERVERS.items())


def router_tools():
    return [
        {"name": "search_tools",
         "description": "Find the tools relevant to a task among the MCP servers behind this router, instead of loading them all. "
                        f"Servers: {server_summary()}. Returns tool names (server{SEP}tool) with input schemas; then use call_tool.",
         "inputSchema": {"type": "object", "required": ["query"], "properties": {
             "query": {"type": "string", "description": "What you want to do, in keywords, e.g. 'click element page', 'search past gradle test commands'"},
             "server": {"type": "string", "description": "Optional: restrict to one server"},
             "limit": {"type": "integer", "default": 5}}}},
        {"name": "call_tool",
         "description": "Call a tool found via search_tools. Pass its full name (server__tool) and arguments matching its inputSchema.",
         "inputSchema": {"type": "object", "required": ["tool"], "properties": {
             "tool": {"type": "string"}, "arguments": {"type": "object"}}}},
        {"name": "list_servers", "description": "List backend servers, their descriptions, tool counts and whether they are running.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "refresh_index", "description": "Re-read tool lists from backends (after a server update).",
         "inputSchema": {"type": "object", "properties": {"server": {"type": "string"}}}},
    ]


def text(obj, err=False):
    return {"content": [{"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, indent=1, ensure_ascii=False)}],
            "isError": err}


def call(name, a):
    if name == "search_tools":
        hits, errors = search(a.get("query", ""), int(a.get("limit", 5)), a.get("server"))
        out = {"tools": hits}
        if not hits:
            out["hint"] = f"No match. Servers: {server_summary()}. Try other keywords or list_servers."
        if errors:
            out["unavailable_servers"] = errors
        return text(out)
    if name == "call_tool":
        server, _, tool = (a.get("tool") or "").partition(SEP)
        if server not in BACKENDS or not tool:
            return text(f"unknown tool '{a.get('tool')}'. Use search_tools to get a server{SEP}tool name.", True)
        try:
            timeout = SERVERS[server].get("timeout", 300)
            return BACKENDS[server].request("tools/call", {"name": tool, "arguments": a.get("arguments") or {}}, timeout)
        except Exception as ex:
            return text(f"{server}: {ex}", True)
    if name == "list_servers":
        idx = json.loads(INDEX.read_text()) if INDEX.exists() else {}
        return text([{"server": k, "description": v.get("description", ""), "tools": len(idx.get(k, {}).get("tools", [])) or None,
                      "running": bool(BACKENDS[k].proc and BACKENDS[k].proc.poll() is None)} for k, v in SERVERS.items()])
    if name == "refresh_index":
        idx, errors = load_index(refresh=True, only=a.get("server"))
        return text({"indexed": {k: len(v["tools"]) for k, v in idx.items()}, "errors": errors})
    return text(f"unknown router tool {name}", True)


# ---------------------------------------------------------------- stdio server

def send(msg, lock=threading.Lock()):
    with lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def serve(req):
    m, rid = req.get("method"), req.get("id")
    try:
        if m == "initialize":
            res = {"protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "mcp-router", "version": "1.0"}}
        elif m == "ping":
            res = {}
        elif m == "tools/list":
            res = {"tools": router_tools()}
        elif m == "tools/call":
            p = req.get("params", {})
            res = call(p.get("name"), p.get("arguments") or {})
        elif rid is None:
            return
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {m}"}})
            return
        if rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "result": res})
    except Exception as ex:
        log("error", m, ex)
        if rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "result": text(f"router error: {ex}", True)})


def main():
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            if "method" in req:
                # one thread per request so a slow backend doesn't block searches; each backend serialises itself
                threading.Thread(target=serve, args=(req,), daemon=True).start()
    finally:
        for b in BACKENDS.values():
            b.stop()


if __name__ == "__main__":
    main()
