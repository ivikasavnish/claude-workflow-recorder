#!/usr/bin/env python3
"""MCP server (stdio, stdlib only) over the workflow-recorder store (~/workflows).

Lets any MCP client (Claude, Codex, Cursor, the mcp-router...) query recorded
session steps and extracted workflows, returning only the matching pieces.
Register:  /usr/bin/python3 ~/.claude/skills/workflow-recorder/wf_mcp.py
"""
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("WF_HOME", Path.home() / "workflows"))
WF = Path(__file__).with_name("wf.py")


# ---------------------------------------------------------------- store access

def projects():
    if not ROOT.exists():
        return []
    ps = [p for p in ROOT.iterdir() if (p / "index.json").exists()]
    return sorted(ps, key=lambda p: (p / "index.json").stat().st_mtime, reverse=True)


def project(name=None):
    if name:
        p = ROOT / name
        if not (p / "index.json").exists():
            raise ValueError(f"unknown project '{name}' (have: {', '.join(x.name for x in projects())})")
        return p
    ps = projects()
    if not ps:
        raise ValueError(f"no recorded projects in {ROOT}")
    return ps[0]  # most recently active


def events(pdir, n):
    p = pdir / "sessions" / f"{int(n):04d}.jsonl"
    if not p.exists():
        raise ValueError(f"session {n} not found in {pdir.name}")
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def step_view(pname, n, e, max_cmd=600):
    cmd = e["command"]
    return {"ref": f"{pname}#{int(n)}:{e['step']}", "category": e["category"], "status": e["status"],
            "tool": e.get("tool", "Bash"), "task": e.get("task", 0), "description": e["description"],
            "cwd": e["cwd"], "command": cmd if len(cmd) <= max_cmd else cmd[:max_cmd] + f"... [+{len(cmd) - max_cmd} chars]"}


def tokens(s):
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) > 1]


# ---------------------------------------------------------------- tools

def t_list_projects(_):
    out = []
    for p in projects():
        idx = json.loads((p / "index.json").read_text())
        out.append({"project": p.name, "sessions": len(idx["sessions"]), "last_session": idx.get("last_session"),
                    "workflows": sorted(w.stem for w in (p / "workflows").glob("*.sh"))})
    return out


def t_list_sessions(a):
    p = project(a.get("project"))
    idx = json.loads((p / "index.json").read_text())
    rows = sorted(idx["sessions"].values(), key=lambda s: s["n"])[-int(a.get("limit", 20)):]
    out = []
    for s in rows:
        try:
            ev = events(p, s["n"])
        except ValueError:
            ev = []
        cats = {}
        for e in ev:
            if e["type"] == "cmd":
                cats[e["category"]] = cats.get(e["category"], 0) + 1
        out.append({"session": s["n"], "started": s["started"], "branch": s.get("branch"), "steps_by_category": cats,
                    "tasks": [e["title"] for e in ev if e["type"] == "mark"]})
    return {"project": p.name, "sessions": out}


def t_search_steps(a):
    q = tokens(a["query"])
    if not q:
        raise ValueError("query has no searchable words")
    cats = set(a["category"].split(",")) if a.get("category") else None
    ok_only = a.get("ok_only", True)
    docs = []
    for p in ([project(a["project"])] if a.get("project") else projects()):
        for f in sorted((p / "sessions").glob("*.jsonl")):
            for e in (json.loads(l) for l in f.read_text().splitlines() if l.strip()):
                if e["type"] != "cmd" or (cats and e["category"] not in cats) or (ok_only and e["status"] != "ok"):
                    continue
                docs.append((p.name, int(f.stem), e, tokens(e["description"]) * 2 + tokens(e["command"])))
    df = {t: sum(1 for d in docs if t in d[3]) for t in q}
    scored = []
    for pname, n, e, toks in docs:
        score = sum(math.log(1 + len(docs) / (1 + df[t])) * min(toks.count(t), 3) for t in q if t in toks)
        if score:
            scored.append((score, n, pname, e))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return {"matches": [step_view(pname, n, e) for _, n, pname, e in scored[: int(a.get("limit", 15))]],
            "total_matches": len(scored)}


def t_get_steps(a):
    p = project(a.get("project"))
    n = int(a["session"])
    wanted = set()
    for part in str(a.get("steps") or "").split(","):
        if part.strip():
            lo, _, hi = part.partition("-")
            wanted.update(range(int(lo), int(hi or lo) + 1))
    out = []
    for e in events(p, n):
        if e["type"] == "mark" and not wanted and a.get("task") in (None, e["task"]):
            out.append({"task": e["task"], "title": e["title"]})
        elif e["type"] == "note" and not wanted:
            out.append({"note": e["text"]})
        elif e["type"] == "cmd" and (not wanted or e["step"] in wanted) and a.get("task") in (None, e.get("task")):
            out.append(step_view(p.name, n, e, max_cmd=4000))
    return {"project": p.name, "session": n, "items": out}


def t_list_workflows(a):
    p = project(a.get("project"))
    out = []
    for w in sorted((p / "workflows").glob("*.sh")):
        lines = w.read_text().splitlines()
        out.append({"name": w.stem, "description": lines[2].lstrip("# ") if len(lines) > 2 else "",
                    "steps": sum(1 for l in lines if l.startswith("# ── ")), "path": str(w)})
    return {"project": p.name, "workflows": out}


def t_get_workflow(a):
    p = project(a.get("project"))
    w = p / "workflows" / f"{a['name']}.sh"
    if not w.exists():
        raise ValueError(f"no workflow '{a['name']}' in {p.name}")
    return {"project": p.name, "path": str(w), "script": w.read_text()}


def t_extract_workflow(a):
    p = project(a.get("project"))
    args = [sys.executable, str(WF), "-p", p.name, "extract", a["name"], "--from", *a["sources"]]
    for k, flag in (("category", "--cat"), ("exclude_category", "--exclude-cat"), ("grep", "--grep"), ("description", "-d")):
        if a.get(k):
            args += [flag, a[k]]
    if a.get("include_failed"):
        args.append("--include-failed")
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise ValueError((r.stderr or r.stdout).strip())
    return t_get_workflow({"project": p.name, "name": a["name"]})


P = {"type": "string", "description": "Project name (folder in ~/workflows). Default: most recently active."}
TOOLS = [
    ("list_projects", t_list_projects, "List recorded projects with session counts and saved workflow names.", {}, []),
    ("list_sessions", t_list_sessions, "List recorded Claude sessions for a project: number, branch, step counts per category, task titles.",
     {"project": P, "limit": {"type": "integer", "default": 20}}, []),
    ("search_steps", t_search_steps,
     "Keyword search over every recorded step (bash commands and Claude tool calls in their bash form) across sessions. "
     "Returns only the best matches, each with a ref 'project#session:step'. Use to find how something was built, run, tested or debugged before.",
     {"query": {"type": "string"}, "project": P,
      "category": {"type": "string", "description": "Comma list of: find,debug,test,build,run,git,edit,note,other"},
      "ok_only": {"type": "boolean", "default": True, "description": "Skip failed/interrupted steps"},
      "limit": {"type": "integer", "default": 15}}, ["query"]),
    ("get_steps", t_get_steps, "Get full steps of one session, optionally only some steps ('3-9,12') or one task number.",
     {"project": P, "session": {"type": "integer"}, "steps": {"type": "string"}, "task": {"type": "integer"}}, ["session"]),
    ("list_workflows", t_list_workflows, "List saved runnable workflows for a project.", {"project": P}, []),
    ("get_workflow", t_get_workflow, "Get a saved workflow script.", {"project": P, "name": {"type": "string"}}, ["name"]),
    ("extract_workflow", t_extract_workflow,
     "Group recorded steps into a new runnable workflow script (deduped, failed steps skipped). "
     "sources: selectors like '7', '7:3-9,12', '7@2' (task 2), 'last', 'all'.",
     {"project": P, "name": {"type": "string", "description": "kebab-case name"},
      "sources": {"type": "array", "items": {"type": "string"}},
      "category": {"type": "string"}, "exclude_category": {"type": "string"}, "grep": {"type": "string"},
      "description": {"type": "string"}, "include_failed": {"type": "boolean"}}, ["name", "sources"]),
]
BY_NAME = {t[0]: t for t in TOOLS}


# ---------------------------------------------------------------- stdio JSON-RPC

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(req):
    m, rid = req.get("method"), req.get("id")
    if m == "initialize":
        return {"protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "workflows", "version": "1.0"}}
    if m == "ping":
        return {}
    if m == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": props, "required": req_}}
                          for n, _, d, props, req_ in TOOLS]}
    if m == "tools/call":
        prm = req.get("params", {})
        tool = BY_NAME.get(prm.get("name"))
        if not tool:
            return {"content": [{"type": "text", "text": f"unknown tool {prm.get('name')}"}], "isError": True}
        try:
            out = tool[1](prm.get("arguments") or {})
            return {"content": [{"type": "text", "text": json.dumps(out, indent=1, ensure_ascii=False)}]}
        except Exception as ex:
            return {"content": [{"type": "text", "text": f"error: {ex}"}], "isError": True}
    if rid is not None:
        raise LookupError(m)
    return None


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if "method" not in req:
            continue  # response to something we never send
        try:
            res = handle(req)
            if req.get("id") is not None:
                send({"jsonrpc": "2.0", "id": req["id"], "result": res})
        except LookupError:
            send({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": f"method not found: {req['method']}"}})


if __name__ == "__main__":
    main()
