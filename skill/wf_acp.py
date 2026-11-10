#!/usr/bin/env python3
"""Local coding agents for `wf fix`, spoken to over the Agent Client Protocol (ACP) when they support it.

ACP (https://agentclientprotocol.com) is JSON-RPC 2.0 over the agent's stdio, one message per line.
We are the client: initialize -> session/new -> session/prompt, while the agent streams session/update
notifications and calls back into us for fs/read_text_file, fs/write_text_file and
session/request_permission. Every file the agent writes through us is tracked, so the change can be
learned as a replayable patch.

Agents without ACP run once in headless "exec" mode (e.g. `muse exec`, `claude -p`); their edits are
picked up from git afterwards.

Built-in agents can be overridden or extended in ~/workflows/agents.json:
  {"default": "codex",
   "agents": {"muse":   {"exec": ["muse", "exec", "--workspace", "{cwd}", "{prompt}"]},
              "mine":   {"acp": [["my-agent", "--acp"]]}}}
"""
import difflib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PROTOCOL_VERSION = 1

# Order matters: the first available one is the default. ACP adapters are tried by binary first, then
# through npx with a pinned version (downloaded on first use). `exec` is the fallback: {prompt} and {cwd}
# are substituted, and the agent edits files with its own tools.
BUILTIN = {
    "claude": {"acp": [["claude-agent-acp"], ["claude-code-acp"]],
               "npx": "@agentclientprotocol/claude-agent-acp@0.102.0",
               "exec": ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits"]},
    "codex":  {"acp": [["codex-acp"]],
               "npx": "@zed-industries/codex-acp@0.16.0",
               "exec": ["codex", "exec", "--sandbox", "workspace-write", "-C", "{cwd}", "{prompt}"]},
    # Antigravity CLI: https://antigravity.google/docs/cli/acp/ (reuses `agy` / `agy acp --login` credentials;
    # permissions stay with us, so no --dangerously-skip-permissions)
    "antigravity": {"acp": [["agy", "acp", "--disable-telemetry"]]},
    "gemini": {"acp": [["gemini", "--experimental-acp"]],
               "exec": ["gemini", "--approval-mode", "auto_edit", "{prompt}"]},
    "muse":   {"exec": ["muse", "exec", "--workspace", "{cwd}", "{prompt}"]},  # speaks MSP, not ACP
}


def config_path():
    return Path(os.environ.get("WF_HOME", Path.home() / "workflows")) / "agents.json"


def load_config():
    try:
        return json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}


def agents():
    cfg = load_config()
    out = {k: dict(v) for k, v in BUILTIN.items()}
    for name, d in (cfg.get("agents") or {}).items():
        out[name] = {**out.get(name, {}), **d}
    return out


def resolve(name, transport=None):
    """-> {"name", "transport": "acp"|"exec", "argv"} or None if the agent can't be started here."""
    d = agents().get(name)
    if not d:
        return None
    if transport in (None, "acp"):
        for argv in d.get("acp") or []:
            if shutil.which(argv[0]):
                return {"name": name, "transport": "acp", "argv": list(argv), "auth": d.get("auth")}
        if d.get("npx") and shutil.which("npx") and os.environ.get("WF_ACP_NPX", "1") != "0":
            return {"name": name, "transport": "acp", "argv": ["npx", "-y", d["npx"]], "via_npx": True,
                    "auth": d.get("auth")}
    if transport in (None, "exec") and d.get("exec") and shutil.which(d["exec"][0]):
        return {"name": name, "transport": "exec", "argv": list(d["exec"])}
    return None


def default_agent():
    cfg = load_config()
    if cfg.get("default") and resolve(cfg["default"]):
        return cfg["default"]
    found = [(n, resolve(n)) for n in agents()]
    found = [(n, r) for n, r in found if r]
    # an agent installed with native ACP beats one npx has to download, which beats one-shot exec
    rank = lambda r: 0 if r["transport"] == "acp" and not r.get("via_npx") else 1 if r["transport"] == "acp" else 2
    return min(found, key=lambda nr: rank(nr[1]))[0] if found else None


def describe():
    """Rows for `wf agents`."""
    rows = []
    for name in agents():
        r = resolve(name)
        if not r:
            rows.append((name, "-", "not installed"))
        else:
            how = " ".join(r["argv"][:4]) + (" (npx downloads it on first use)" if r.get("via_npx") else "")
            rows.append((name, r["transport"], how))
    return rows


class ACPError(Exception):
    pass


class Permissions:
    """How agent permission requests are answered: "ask" (on the terminal), "yes" (allow, except commands
    the recorder flags as dangerous), or "no" (reject everything)."""

    def __init__(self, mode="ask", is_dangerous=None, out=None, ask_fn=None):
        self.mode, self.is_dangerous, self.out = mode, is_dangerous or (lambda s: []), out or sys.stderr
        self.ask_fn = ask_fn or ask  # the app swaps in one that asks in the browser
        self.lock = threading.Lock()
        self.always = set()

    def decide(self, tool_call, options):
        title = tool_call.get("title") or tool_call.get("kind") or "tool call"
        raw = tool_call.get("rawInput")
        text = json.dumps(raw, ensure_ascii=False) if raw is not None else ""
        danger = self.is_dangerous(raw.get("command", "") if isinstance(raw, dict) else text)
        by_kind = {}
        for o in options:
            by_kind.setdefault(o.get("kind"), o["optionId"])
        allow = by_kind.get("allow_once") or by_kind.get("allow_always")
        reject = by_kind.get("reject_once") or by_kind.get("reject_always")
        key = tool_call.get("kind") or title
        with self.lock:
            if danger:
                print(f"  ! agent wants to run a dangerous command ({'; '.join(danger)}): {text[:300]}", file=self.out)
            if self.mode == "no" or (self.mode == "yes" and danger):
                return reject
            if self.mode == "yes" or (key in self.always and not danger):
                return allow
            answer = self.ask_fn(f"  ? allow {title} {text[:300]} [y]es / [a]lways for {key} / [N]o: ",
                                 {"kind": "permission", "title": title, "detail": text[:2000], "group": key, "danger": danger})
            if answer is None:
                print(f"  ! denied {title}: no terminal to ask on (pass --yes to approve non-dangerous calls)", file=self.out)
            if answer == "a" and not danger:
                self.always.add(key)
                return by_kind.get("allow_always") or allow
            return allow if answer in ("y", "a") else reject


def ask(prompt, meta=None):
    """One-letter answer typed on the controlling terminal, or None when there is none.
    /dev/tty is opened once per direction: "r+" needs a seekable file and a terminal isn't one."""
    try:
        with open("/dev/tty", "w") as w, open("/dev/tty") as r:
            w.write(prompt)
            w.flush()
            return r.readline().strip().lower()[:1]
    except OSError:
        return None


class ACPClient:
    def __init__(self, argv, cwd, root, permissions, out=None, log=None, env=None, auth=None, emit=None):
        self.auth = auth
        self.emit = emit or (lambda _type, **data: None)
        self.cwd, self.root = str(cwd), Path(root).resolve()
        self.perms, self.out, self.log = permissions, out or sys.stdout, log
        self.proc = subprocess.Popen(argv, cwd=self.cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=log if log else subprocess.DEVNULL, text=True, bufsize=1)
        self.wlock, self.next_id, self.pending = threading.Lock(), 0, {}
        self.writes = {}  # absolute path -> content before the agent's first write (None = new file)
        self.message = []
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    # ------------------------------------------------------------ transport
    def _send(self, msg):
        msg["jsonrpc"] = "2.0"
        if self.log:
            self.log.write(">> " + json.dumps(msg)[:2000] + "\n")
        self.emit("rpc", dir="out", msg=json.dumps(msg)[:4000])
        with self.wlock:
            try:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                self.alive = False

    def request(self, method, params, timeout=120):
        with self.wlock:
            self.next_id += 1
            rid = self.next_id
            slot = self.pending[rid] = {"ev": threading.Event()}
        self._send({"id": rid, "method": method, "params": params})
        if not slot["ev"].wait(timeout):
            self.pending.pop(rid, None)
            raise TimeoutError(f"{method} timed out after {timeout}s")
        if "error" in slot:
            e = slot["error"]
            raise ACPError(f"{method}: {e.get('message')} {json.dumps(e.get('data'))[:300] if e.get('data') else ''}".strip())
        return slot.get("result")

    def notify(self, method, params):
        self._send({"method": method, "params": params})

    def _reader(self):
        for line in self.proc.stdout:
            if not line.strip():
                continue
            if self.log:
                self.log.write("<< " + line[:2000] + ("" if line.endswith("\n") else "\n"))
            self.emit("rpc", dir="in", msg=line.strip()[:4000])
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # agents sometimes print banners on stdout
            if "method" in msg:
                # served off the reader thread: a permission prompt must not stop us reading
                threading.Thread(target=self._serve, args=(msg,), daemon=True).start()
            elif msg.get("id") in self.pending:
                slot = self.pending.pop(msg["id"])
                slot.update({k: msg[k] for k in ("result", "error") if k in msg})
                slot["ev"].set()
        self.alive = False
        for slot in list(self.pending.values()):
            slot["error"] = {"message": "agent exited"}
            slot["ev"].set()

    def _serve(self, msg):
        m, p, rid = msg["method"], msg.get("params") or {}, msg.get("id")
        handler = {"session/update": self.on_update, "session/request_permission": self.on_permission,
                   "fs/read_text_file": self.on_read, "fs/write_text_file": self.on_write}.get(m)
        try:
            if not handler:
                raise LookupError(m)
            res = handler(p)
            if rid is not None:
                self._send({"id": rid, "result": res})
        except LookupError:
            if rid is not None:
                self._send({"id": rid, "error": {"code": -32601, "message": f"method not found: {m}"}})
        except Exception as ex:
            if rid is not None:
                self._send({"id": rid, "error": {"code": -32603, "message": str(ex)}})

    # ------------------------------------------------------------ agent -> client
    def on_update(self, p):
        u = p.get("update") or {}
        kind = u.get("sessionUpdate")
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            c = u.get("content") or {}
            if c.get("type") == "text":
                if kind == "agent_message_chunk":
                    self.message.append(c.get("text", ""))
                    self.out.write(c.get("text", ""))
                    self.out.flush()
                self.emit("agent_text", text=c.get("text", ""), thought=kind == "agent_thought_chunk")
        elif kind in ("tool_call", "tool_call_update"):
            if kind == "tool_call":
                self.out.write(f"\n  -> {u.get('title') or u.get('kind') or 'tool'}\n")
            elif u.get("status") == "failed":
                self.out.write(f"  !! {u.get('title') or 'tool call'} failed\n")
            raw = u.get("rawInput")
            self.emit("tool", call=u.get("toolCallId"), title=u.get("title"), tool_kind=u.get("kind"),
                      status=u.get("status"), input=json.dumps(raw, ensure_ascii=False)[:1000] if raw is not None else None)
        elif kind == "plan":
            for e in u.get("entries") or []:
                self.out.write(f"  [plan] {e.get('content', '')}\n")
            self.emit("plan", entries=[{"content": e.get("content", ""), "status": e.get("status")} for e in u.get("entries") or []])
        return None

    def on_permission(self, p):
        option = self.perms.decide(p.get("toolCall") or {}, p.get("options") or [])
        if not option:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": option}}

    def _path(self, p, write):
        path = Path(p.get("path") or "")
        if not path.is_absolute():
            raise ValueError("path must be absolute")
        path = path.resolve()
        from wf import SENSITIVE_PATH  # noqa: shared rule
        if SENSITIVE_PATH.search(str(path)):
            raise PermissionError(f"wf: {path.name} looks sensitive; not exposed to the agent")
        if write and self.root not in (path, *path.parents):
            raise PermissionError(f"wf: writes are limited to {self.root}")
        return path

    def on_read(self, p):
        path = self._path(p, write=False)
        text = path.read_text(errors="replace")
        line, limit = p.get("line"), p.get("limit")
        if line or limit:
            lines = text.splitlines(keepends=True)
            start = max((line or 1) - 1, 0)
            text = "".join(lines[start:start + limit] if limit else lines[start:])
        return {"content": text}

    def on_write(self, p):
        path = self._path(p, write=True)
        if path not in self.writes:
            self.writes[path] = path.read_text(errors="replace") if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(p.get("content", ""))
        self.out.write(f"  ~ wrote {path.relative_to(self.root)}\n")
        self.emit("wrote", path=str(path.relative_to(self.root)))
        return None

    # ------------------------------------------------------------ client -> agent
    def run(self, prompt, timeout=900):
        init = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": False},
            "clientInfo": {"name": "workflow-recorder", "version": "0.2.0"}}, timeout=180)
        self.info = init or {}
        self.emit("connected", protocol=self.info.get("protocolVersion"), agent_info=self.info.get("agentInfo"))
        try:
            sess = self.request("session/new", {"cwd": self.cwd, "mcpServers": []})
        except ACPError as ex:
            # e.g. gemini: "Authentication required". Use the agent's configured method, else the first
            # it offers (normally the login its own CLI already has), then try once more.
            methods = [m.get("id") for m in (init or {}).get("authMethods") or [] if m.get("id")]
            method = self.auth or (methods[0] if methods else None)
            if not method or "auth" not in str(ex).lower():
                raise
            try:
                self.request("authenticate", {"methodId": method}, timeout=180)
                sess = self.request("session/new", {"cwd": self.cwd, "mcpServers": []})
            except ACPError as ex2:
                raise ACPError(f"{ex2} (tried auth method '{method}'; others: {', '.join(methods)}; "
                               f"set \"auth\" for this agent in {config_path()})")
        sid = sess["sessionId"]
        try:
            res = self.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]},
                               timeout=timeout)
        except TimeoutError:
            self.notify("session/cancel", {"sessionId": sid})
            time.sleep(2)
            return "timeout"
        return (res or {}).get("stopReason", "end_turn")

    def patch(self):
        """Unified diff (a/ b/ relative to root) of every file the agent wrote through us."""
        out = []
        for path, before in sorted(self.writes.items()):
            rel = str(path.relative_to(self.root))
            after = path.read_text(errors="replace") if path.exists() else ""
            a = (before or "").splitlines(keepends=True)
            b = after.splitlines(keepends=True)
            d = list(difflib.unified_diff(a, b, "/dev/null" if before is None else f"a/{rel}", f"b/{rel}"))
            out += [l if l.endswith("\n") else l + "\n\\ No newline at end of file\n" for l in d]
        return "".join(out)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def probe(agent, timeout=120):
    """ACP handshake only (initialize, no session, no prompt): is the agent there and what does it offer?"""
    started = time.time()
    c = ACPClient(agent["argv"], os.getcwd(), os.getcwd(), Permissions("no"), out=open(os.devnull, "w"))
    try:
        init = c.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": False},
            "clientInfo": {"name": "workflow-recorder", "version": "0.2.0"}}, timeout=timeout) or {}
        caps = init.get("agentCapabilities") or {}
        return {"ok": True, "ms": int((time.time() - started) * 1000), "protocol": init.get("protocolVersion"),
                "agent_info": init.get("agentInfo"),
                "capabilities": sorted(k for k, v in caps.items() if v and not k.startswith("_") and "_" not in k),
                "prompt": sorted(k for k, v in (caps.get("promptCapabilities") or {}).items() if v),
                "auth": [m.get("name") or m.get("id") for m in init.get("authMethods") or []]}
    except Exception as ex:
        return {"ok": False, "ms": int((time.time() - started) * 1000), "error": str(ex)}
    finally:
        c.close()


def run_agent(agent, prompt, cwd, root, permissions, out=None, log=None, timeout=900, emit=None):
    """Run one fix attempt. -> {"ok", "summary", "patch"} where patch is the change made through ACP
    (exec agents edit on their own; the caller diffs git for those)."""
    out = out or sys.stdout
    if agent["transport"] == "acp":
        c = ACPClient(agent["argv"], cwd, root, permissions, out=out, log=log, auth=agent.get("auth"), emit=emit)
        try:
            stop = c.run(prompt, timeout)
        except (ACPError, TimeoutError) as ex:
            return {"ok": False, "summary": f"{agent['name']}: {ex}", "patch": c.patch()}
        finally:
            c.close()
        out.write("\n")
        return {"ok": stop in ("end_turn", "max_tokens"), "summary": "".join(c.message).strip(),
                "stop": stop, "patch": c.patch()}

    argv = [a.replace("{prompt}", prompt).replace("{cwd}", str(cwd)) for a in agent["argv"]]
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    tail = []
    try:
        deadline = time.time() + timeout
        for line in proc.stdout:
            out.write(line)
            if emit:
                emit("agent_text", text=line, thought=False)
            tail = (tail + [line])[-40:]
            if log:
                log.write(line)
            if time.time() > deadline:
                proc.terminate()
                break
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        raise
    return {"ok": rc == 0, "summary": "".join(tail).strip()[-1500:], "patch": ""}
