#!/usr/bin/env python3
"""workflow-recorder: log every Claude Code Bash command per session, then
regroup the logged commands into runnable, isolated workflows.

Storage (per project, outside the repo so secrets never get committed):
  ~/workflows/<project>/index.json        session uuid -> incremental number
  ~/workflows/<project>/sessions/0007.jsonl   source of truth (one event per line)
  ~/workflows/<project>/sessions/0007.sh      human-readable, commented log
  ~/workflows/<project>/workflows/<name>.sh   extracted runnable workflows
  ~/workflows/<project>/runs/<name>-<ts>.log  output of `wf run`

Hook mode (stdin = hook JSON):  wf.py hook
CLI mode:                       wf.py <command> ...   (see `wf.py -h`)
"""
import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
ROOT = Path(os.environ.get("WF_HOME", HOME / "workflows"))

# ---------------------------------------------------------------- helpers

def now(fmt="%Y-%m-%dT%H:%M:%S"):
    return dt.datetime.now().strftime(fmt)


def project_root(cwd):
    p = Path(cwd or os.getcwd()).resolve()
    for d in [p, *p.parents]:
        if (d / ".git").exists():
            return d
    return p


def project_dir(cwd=None, name=None):
    slug = name or project_root(cwd).name or "root"
    d = ROOT / slug
    (d / "sessions").mkdir(parents=True, exist_ok=True)
    (d / "workflows").mkdir(exist_ok=True)
    return d


def git_branch(cwd):
    try:
        return subprocess.run(["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
                              capture_output=True, text=True, timeout=2).stdout.strip() or "-"
    except Exception:
        return "-"


class Index:
    """index.json guarded by an flock so parallel hooks can't hand out the same number."""

    def __init__(self, pdir):
        self.path = pdir / "index.json"
        self.lock = open(pdir / ".index.lock", "w")

    def __enter__(self):
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"next": 1, "sessions": {}}
        return self.data

    def __exit__(self, *exc):
        if exc[0] is None:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            tmp.replace(self.path)
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()


def session_number(pdir, sid, cwd):
    """Return (number, is_new) for a Claude session uuid, allocating the next id if unseen."""
    with Index(pdir) as idx:
        s = idx["sessions"].get(sid)
        if s:
            s["last"] = now()
            idx["last_session"] = s["n"]
            return s["n"], False
        n = idx["next"]
        idx["next"] = n + 1
        idx["sessions"][sid] = {"n": n, "started": now(), "last": now(),
                                "cwd": str(cwd), "branch": git_branch(cwd)}
        idx["last_session"] = n
    sh = pdir / "sessions" / f"{n:04d}.sh"
    sh.write_text(
        "#!/usr/bin/env bash\n"
        f"# Session {n:04d} · {pdir.name} · branch {git_branch(cwd)}\n"
        f"# claude-session: {sid}\n"
        f"# started: {now()}   cwd: {cwd}\n"
        "# LOG ONLY - not meant to be run as-is. Build runnable scripts with:\n"
        f"#   wf extract <name> --from {n}:<steps>\n"
        "# " + "-" * 70 + "\n")
    return n, True


def append(pdir, n, event, text):
    base = pdir / "sessions" / f"{n:04d}"
    with open(base.with_suffix(".jsonl"), "a") as f:
        f.write(json.dumps(event) + "\n")
    with open(base.with_suffix(".sh"), "a") as f:
        f.write(text)


def load(pdir, n):
    p = pdir / "sessions" / f"{n:04d}.jsonl"
    if not p.exists():
        sys.exit(f"wf: session {n} not found in {pdir}")
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# ---------------------------------------------------------------- classification & redaction

CATEGORY_RULES = [
    ("test",  r"(gradlew\S*\s.*\btest|\bgo test\b|\bpytest\b|\bnpm (run )?test\b|\bcargo test\b|\bjest\b|\bmake test\b|-m unittest\b|\bvitest\b|\bmvn test\b|\bctest\b)"),
    ("build", r"(gradlew\S*\s.*\b(assemble|build|compile|bundle)|\bgo build\b|\bnpm run build\b|\bcargo build\b|\bmake( build| all)?\s*$|\bdocker build\b|\btsc\b|-m compileall\b|\bmvn (package|install)\b|\bcmake --build\b)"),
    ("debug", r"(\badb (logcat|shell (dumpsys|getprop|ps|top))|\blogcat\b|\blsof\b|\bps aux\b|\bcurl\b.*-v\b|\bstrace\b|\bdlv\b|\blldb\b|\bnetstat\b|\btail -f\b|\bjournalctl\b)"),
    ("run",   r"(\badb (install|shell am|reverse|forward|push)|gradlew\S*\s.*\binstall|\bnpm (run )?(dev|start)\b|\bgo run\b|\bcargo run\b|\bpython3? \S+\.py\b|\bdocker(-compose| compose) up\b|\bnode \S+|\bcurl\b|\bmake (run|dev)\b)"),
    ("git",   r"^\s*(git|gh)\s"),
    ("edit",  r"(\bsed -i\b|^\s*(mv|cp|mkdir|rm|touch|chmod|ln)\s|>\s*\S+\s*$|\btee\b|<<\s*'?EOF)"),
    ("find",  r"^\s*(grep|rg|find|fd|ls|cat|head|tail|sed -n|wc|tree|which|file|stat|jq|awk|less|du|echo)\b"),
]

def categorize(cmd):
    for cat, rx in CATEGORY_RULES:
        if re.search(rx, cmd, re.M):
            return cat
    return "other"


REDACTIONS = [
    (r"(?i)(authorization:\s*(?:bearer|basic)\s+)[^\s'\"]+", r"\1<REDACTED>"),
    (r"(?i)(x-api-key:\s*)[^\s'\"]+", r"\1<REDACTED>"),
    (r"\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS)[A-Z0-9_]*=)(?!\$)[^\s'\"]+", r"\1<REDACTED>"),
    (r"(?i)(--?(?:password|passwd|token|api-key|apikey|secret)[= ]\s*)(?!\$)[^\s'\"]+", r"\1<REDACTED>"),
    (r"\b(sk-|ghp_|gho_|xox[bp]-|AIza)[A-Za-z0-9_\-]{12,}", r"\1<REDACTED>"),
]

def redact(s):
    for rx, rep in REDACTIONS:
        s = re.sub(rx, rep, s)
    return s


# ---------------------------------------------------------------- risk & repeatability

# Heuristics, not a sandbox: they catch common shapes so a human looks before replaying.
DANGER_RULES = [
    ("recursive delete", r"\brm\s+(-\S+\s+)*(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b|\bfind\b.*\s-delete\b|\bxargs\s+(-\S+\s+)*rm\b"),
    ("discards git work", r"\bgit\s+reset\b.*--hard|\bgit\s+clean\b.*\s-[a-zA-Z]*f[a-zA-Z]*\b|\bgit\s+checkout\s+(--\s+)?\.(\s|$)|\bgit\s+branch\s+-D\b|\bgit\s+filter-branch\b"),
    ("rewrites or deletes remote git history", r"\bgit\s+push\b[^;&|\n]*(\s--force|\s-[a-zA-Z]*f[a-zA-Z]*\b|\s--delete\b|\s\+\S)"),
    ("runs as root", r"\bsudo\b"),
    ("pipes remote code into a shell", r"\b(curl|wget)\b[^\n]*\|\s*(sudo\s+)?(ba|z)?sh\b"),
    ("writes raw disk or formats", r"\bdd\b[^\n]*\bof=|\bmkfs\b|>\s*/dev/(sd|nvme|disk)"),
    ("world-writable or recursive ownership change", r"\bchmod\b[^\n]*\b777\b|\bchown\s+-R\b"),
    ("stops the machine", r"\b(shutdown|reboot|halt)\b"),
    ("changes infrastructure", r"\bterraform\s+(apply|destroy)\b|\bkubectl\s+(apply|delete|drain|replace)\b|\bhelm\s+(install|upgrade|uninstall|delete)\b|\baws\b[^\n]*\s(rm|delete\S*|terminate\S*)\b|\bgcloud\b[^\n]*\sdelete\b"),
    ("removes containers or volumes", r"\bdocker\s+(system|volume|image|container)\s+prune\b|\bdocker(-compose|\s+compose)\s+down\b[^\n]*\s-v\b|\bdocker\s+(rm|rmi)\b"),
    ("deploys or publishes", r"\b(npm|cargo)\s+publish\b|\btwine\s+upload\b|\bdocker\s+push\b|\bgh\s+(release\s+(create|delete)|repo\s+delete|pr\s+merge)\b|\b(firebase|fly|flyctl)\s+deploy\b|\bvercel\b[^\n]*--prod\b"),
    ("wipes device app data", r"\badb\b[^\n]*\s(uninstall|pm\s+clear|shell\s+rm)\b"),
    ("destroys a database", r"\bdropdb\b|\bredis-cli\b[^\n]*\bflush(all|db)\b"),
]
SQL_CLIENT = r"\b(psql|mysql|mariadb|sqlite3|sqlcmd|clickhouse-client)\b"
SQL_DANGER = r"(?i)\b(drop\s+(table|database|schema)|truncate\s+table|delete\s+from\s+\S+\s*(;|['\"]|$))"
ONCE_RULES = [
    ("commit, tag or branch already exists on rerun", r"\bgit\s+(commit|tag|init|clone|merge|rebase|cherry-pick|checkout\s+-b|switch\s+-c|branch\s+(?!-))"),
    ("pushes to a remote", r"\bgit\s+push\b"),
    ("creates a remote object", r"\bgh\s+(pr|issue|release|repo)\s+create\b"),
    ("fails if the directory exists", r"\bmkdir\s+(?![^\n;&|]*(-[a-zA-Z]*p[a-zA-Z]*\b|--parents))"),
    ("appends on every run", r">>"),
    ("moves its source away", r"(^|[;&|]\s*)mv\s"),
    ("fails if the link exists", r"\bln\s+-s\s"),
    ("sends a mutating HTTP request", r"\bcurl\b[^\n]*(\s-X\s*(POST|PUT|PATCH|DELETE)|\s--request\s+(POST|PUT|PATCH|DELETE)|\s(-d|--data\S*)\s)"),
    ("in-place edit may not reapply", r"\bsed\s+-i\b"),
    ("named container already exists", r"\bdocker\s+run\b[^\n]*--name\b"),
    ("bumps the version", r"\bnpm\s+version\b"),
]


def strip_heredocs(cmd):
    return re.sub(r"(<<-?\s*['\"]?(\w+)['\"]?[^\n]*)\n.*?\n\s*\2[ \t]*(?=\n|$)", r"\1", cmd, flags=re.S)


def shell_view(cmd):
    """The command as the shell acts on it: no heredoc bodies, quoted text or comments, so
    `git commit -m "rm -rf"` and file contents don't trigger rules. Text handed to a nested
    shell (`bash -c`, `eval`, `ssh`) stays visible, since it does run."""
    view = strip_heredocs(cmd)
    if not re.search(r"\b((ba|z)?sh\s+-\w*c|eval|ssh)\b", view):
        view = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", '""', view)
    return re.sub(r"(^|\s)#[^\n]*", "", view, flags=re.M)


def assess(cmd):
    """-> {"danger": [reasons], "once": [reasons]}: what could hurt, and what won't rerun cleanly."""
    view = shell_view(cmd)
    danger = [why for why, rx in DANGER_RULES if re.search(rx, view, re.M)]
    if re.search(SQL_CLIENT, view) and re.search(SQL_DANGER, cmd, re.M):
        danger.append("destroys database rows")
    once = [why for why, rx in ONCE_RULES if re.search(rx, view, re.M)]
    if "assert old in s, 'wf: edit target not found" in cmd:
        once.append("edit fails once its target text is replaced")
    return {"danger": danger, "once": once}


def risk_lines(a):
    return ([f"# ! DANGEROUS: {'; '.join(a['danger'])}"] if a["danger"] else []) + \
           ([f"# ! NOT REPEATABLE: {'; '.join(a['once'])}"] if a["once"] else [])


# ---------------------------------------------------------------- parameters

# {{NAME}} is filled in as text by `wf run`, so it also works inside quoted heredocs, single
# quotes and the step's directory, where shell expansion would not. That is why values are
# restricted to characters that can't change how a command parses.
PARAM_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
PARAM_VALUE = re.compile(r"^[\w./:@%+,=~-]+$")
PARAM_DECL = re.compile(r"^# param: ([A-Z][A-Z0-9_]*)=(.*)$", re.M)


def parse_kv(items):
    out = {}
    for kv in items or []:
        k, sep, v = kv.partition("=")
        if not sep or not PARAM_NAME.match(k):
            raise ValueError(f"bad parameter '{kv}' (use UPPER_NAME=value)")
        if not PARAM_VALUE.match(v):
            raise ValueError(f"parameter {k}: value may only use letters, digits and . / : @ % + , = ~ - _")
        out[k] = v
    return out


def parameterize(text, params):
    """Replace literal values with {{NAME}}, whole tokens only (80 doesn't match inside 8080)."""
    for k, v in sorted(params.items(), key=lambda kv: -len(kv[1])):
        text = re.sub(rf"(?<!\w){re.escape(v)}(?![\w-])", lambda _: "{{" + k + "}}", text)
    return text


def render(text, params):
    return re.sub(r"\{\{([A-Z][A-Z0-9_]*)\}\}", lambda m: params.get(m.group(1), m.group(0)), text)


def suggest_params(commands, taken):
    """Values worth parameterizing, as `NAME=value` hints for `wf extract --param`."""
    found = {}
    text = "\n".join(strip_heredocs(c) for c in commands)
    for name, rx in (("VERSION", r"(?<![\w.])v?\d+\.\d+\.\d+(?![\w.])"),
                     ("HOST_IP", r"(?<![\w.])(?!127\.|0\.0\.0\.0)\d{1,3}(?:\.\d{1,3}){3}(?![\w.])"),
                     ("HOST", r"(?<=://)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")):
        for i, v in enumerate(dict.fromkeys(re.findall(rx, text))):
            if v not in taken and i < 3:
                found[f"{name}{i + 1 if i else ''}"] = v
    return found


# ---------------------------------------------------------------- Claude tools -> bash

# Bookkeeping / UI tools that say nothing about how the work was done.
SKIP_TOOLS = {"ToolSearch", "TodoWrite", "TaskStop", "AskUserQuestion", "ScheduleWakeup", "ListAgents",
              "SendFeedback", "ReportFindings", "EnterPlanMode", "ExitPlanMode", "Monitor"}
SENSITIVE_PATH = re.compile(r"(^|/)(\.env[^/]*|.*secret[^/]*|.*credential[^/]*|.*\.pem|.*\.key|id_[a-z0-9]+)$", re.I)
INLINE_MAX_LINES = 80
q = shlex.quote


def comment(text):
    return "\n".join("# " + l for l in text.splitlines() or [""])


def snapshot(files_dir, step, name, content):
    files_dir.mkdir(exist_ok=True)
    p = files_dir / f"step-{step}-{Path(name).name}"
    p.write_text(content)
    return p


def write_cmd(path, content, files_dir, step):
    lines = content.splitlines()
    if (content.endswith("\n") and len(lines) <= INLINE_MAX_LINES
            and "WF_EOF" not in lines):
        return f"cat > {q(path)} <<'WF_EOF'\n{content}WF_EOF"
    snap = snapshot(files_dir, step, path, content)
    return f"# {len(lines)} lines, exact content snapshotted\ncp {q(str(snap))} {q(path)}"


def edit_cmd(path, edits):
    """Exact-string replacement, same semantics as Claude's Edit tool (fails if old text is missing)."""
    body = []
    for e in edits:
        old, new = json.dumps(e.get("old_string", "")), json.dumps(e.get("new_string", ""))
        count = "" if e.get("replace_all") else ", 1"
        body += [f"old, new = {old}, {new}",
                 "assert old in s, 'wf: edit target not found in ' + p.name",
                 f"s = s.replace(old, new{count})"]
    return ("python3 - " + q(path) + " <<'WF_PY'\nimport sys, pathlib\n"
            "p = pathlib.Path(sys.argv[1]); s = p.read_text()\n"
            + "\n".join(body) + "\np.write_text(s)\nWF_PY")


def translate(tool, ti, files_dir, step, cwd=None):
    """Map a tool call to (bash, description, category, raw) - raw=True skips secret redaction
    (file contents must stay byte-exact). Tools with no shell equivalent become bash comments."""
    if tool in SKIP_TOOLS:
        return None
    if tool == "Bash":
        cmd = (ti.get("command") or "").strip()
        if not cmd or re.match(r"^\s*(wf|\S*workflow-recorder/wf(\.py)?)(\s|$)", cmd):
            return None  # don't log the recorder's own bookkeeping
        return cmd, (ti.get("description") or "").strip().replace("\n", " "), categorize(cmd), False

    path = ti.get("file_path") or ti.get("notebook_path") or ""
    # descriptions show project-relative paths; commands keep absolute ones so replays don't depend on cwd
    shown = path[len(cwd) + 1:] if cwd and path.startswith(cwd.rstrip("/") + "/") else path
    if tool == "Read":
        off, lim = ti.get("offset"), ti.get("limit")
        if off and lim:
            cmd, rng = f"sed -n '{off},{off + lim - 1}p' {q(path)}", f" lines {off}-{off + lim - 1}"
        elif off:
            cmd, rng = f"sed -n '{off},$p' {q(path)}", f" from line {off}"
        elif lim:
            cmd, rng = f"head -n {lim} {q(path)}", f" first {lim} lines"
        else:
            cmd, rng = f"cat {q(path)}", ""
        if ti.get("pages"):
            cmd, rng = f"open {q(path)}  # PDF pages {ti['pages']}", f" pages {ti['pages']}"
        return f"{cmd}  # [Read]", f"Read {shown}{rng}", "find", False
    if tool == "Write":
        if SENSITIVE_PATH.search(path):
            return comment(f"[Write] {path} (sensitive file - content not logged)"), f"Write {shown}", "edit", True
        return write_cmd(path, ti.get("content", ""), files_dir, step), f"Write {shown}", "edit", True
    if tool in ("Edit", "MultiEdit"):
        edits = ti.get("edits") or [ti]
        if SENSITIVE_PATH.search(path):
            return comment(f"[Edit] {path} (sensitive file - change not logged)"), f"Edit {shown}", "edit", True
        first = (edits[0].get("old_string") or "").strip().splitlines()
        hint = f": {first[0][:60]}" if first else ""
        return edit_cmd(path, edits), f"Edit {shown}{hint}", "edit", True
    if tool == "Grep":
        # Claude's Grep is a bundled ripgrep; plain grep keeps replays portable.
        args = ["grep", "-rE", "--exclude-dir=.git"]
        mode = ti.get("output_mode", "files_with_matches")
        args += {"files_with_matches": ["-l"], "count": ["-c"]}.get(mode, ["-n"] if ti.get("-n", True) else [])
        for flag in ("-i", "-A", "-B", "-C"):
            v = ti.get(flag)
            if v is True:
                args.append(flag)
            elif v not in (None, False):
                args += [flag, str(v)]
        if ti.get("glob"):
            args.append("--include=" + ti["glob"].replace("**/", ""))
        if ti.get("type"):
            args.append(f"--include=*.{ti['type']}")
        args += ["-e", ti.get("pattern", ""), ti.get("path") or "."]
        cmd = " ".join(q(a) for a in args) + (f" | head -n {ti['head_limit']}" if ti.get("head_limit") else "")
        note = " (multiline in Claude)" if ti.get("multiline") else ""
        return f"{cmd}  # [Grep]{note}", f"Search for /{ti.get('pattern', '')}/", "find", False
    if tool == "Glob":
        base, pat = ti.get("path") or ".", ti.get("pattern", "").replace("**/", "")
        test = f"-name {q(pat)}" if "/" not in pat else f"-path {q(base.rstrip('/') + '/' + pat)}"
        cmd = f"find {q(base)} -type f {test} -not -path '*/.git/*'"
        return cmd + "  # [Glob]", f"List files matching {ti.get('pattern', '')}", "find", False
    if tool == "WebFetch":
        cmd = f"curl -sL {q(ti.get('url', ''))}  # [WebFetch] {(ti.get('prompt') or '')[:100]}".replace("\n", " ")
        return cmd, f"Fetch {ti.get('url', '')}", "find", False

    # No shell equivalent: keep a comment so the session reads end to end.
    if tool in ("Agent", "Task"):
        text = f"[Agent:{ti.get('subagent_type') or 'general'}] {ti.get('description', '')}\n{(ti.get('prompt') or '')[:400]}"
        desc = f"Subagent: {ti.get('description', '')}"
    elif tool == "WebSearch":
        text, desc = f"[WebSearch] {ti.get('query', '')}", f"Web search: {ti.get('query', '')}"
    elif tool.endswith("__call_tool") and ti.get("tool"):
        args = json.dumps(ti.get("arguments") or {}, ensure_ascii=False)[:400]
        text, desc = f"[{ti['tool']}] {args}", f"MCP {ti['tool']}"
    elif tool.endswith("__search_tools"):
        text, desc = f"[{tool}] {ti.get('query', '')}", f"MCP tool search: {ti.get('query', '')}"
    elif tool == "Skill":
        text, desc = f"[Skill] {ti.get('skill', '')} {ti.get('args') or ''}", f"Load skill {ti.get('skill', '')}"
    else:
        brief = json.dumps({k: v for k, v in ti.items() if not isinstance(v, (dict, list)) or len(json.dumps(v)) < 200},
                           ensure_ascii=False)[:400]
        text, desc = f"[{tool}] {brief}", f"Tool {tool}"
    return comment(redact(text)), desc, "note", True


# ---------------------------------------------------------------- hook

def hook():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return
    name, sid, cwd = ev.get("hook_event_name"), ev.get("session_id", "unknown"), ev.get("cwd") or os.getcwd()
    pdir = project_dir(cwd)
    n, is_new = session_number(pdir, sid, cwd)

    if name == "SessionStart":
        state = "started" if is_new else "resumed"
        print(f"[workflow-recorder] Session #{n:04d} {state} for project '{pdir.name}'. "
              f"Every Bash command is logged to {pdir}/sessions/{n:04d}.sh with its description as the comment. "
              f"Write precise Bash descriptions; run `wf mark --session {n} \"<goal>\"` when starting a new sub-task.")
        return

    events = load(pdir, n) if (pdir / "sessions" / f"{n:04d}.jsonl").exists() else []
    step = 1 + sum(1 for e in events if e["type"] == "cmd")
    ti = ev.get("tool_input") or {}
    tr = translate(ev.get("tool_name") or "", ti, pdir / "sessions" / f"{n:04d}.files", step, cwd)
    if not tr:
        return
    cmd, desc, cat, raw = tr

    resp = ev.get("tool_response") or {}
    if name == "PostToolUseFailure":
        status, err = "FAILED", str(ev.get("error") or resp.get("stderr") or "")
    elif isinstance(resp, dict) and resp.get("interrupted"):
        status, err = "INTERRUPTED", ""
    else:
        status, err = "ok", ""

    prev_cwd = next((e["cwd"] for e in reversed(events) if e["type"] == "cmd"), None)
    task = next((e["task"] for e in reversed(events) if e["type"] == "mark"), 0)
    cmd_r = cmd if raw else redact(cmd)
    err_tail = redact(err.strip().splitlines()[-1][:200]) if err.strip() else ""

    risk = assess(cmd_r)
    event = {"type": "cmd", "step": step, "ts": now(), "task": task, "category": cat, "status": status,
             "cwd": cwd, "description": desc, "command": cmd_r, "tool": ev.get("tool_name"),
             "background": bool(ti.get("run_in_background")), "error": err_tail, **risk}
    text = f"\n# ── step {step} · {now('%H:%M:%S')} · [{cat}] · {status}" + (f" · task {task}" if task else "") + "\n"
    if desc:
        text += f"# {desc}\n"
    if err_tail:
        text += f"# ! {err_tail}\n"
    text += "".join(l + "\n" for l in risk_lines(risk))
    if cwd != prev_cwd:
        text += f"cd {shlex.quote(cwd)}\n"
    text += cmd_r + "\n"
    append(pdir, n, event, text)


# ---------------------------------------------------------------- CLI

def resolve_session(pdir, s):
    with Index(pdir) as idx:
        if s in (None, "last", "current"):
            n = idx.get("last_session")
            if not n:
                sys.exit("wf: no sessions recorded yet")
            return n
        return int(s)


def cmd_status(a):
    pdir = project_dir(name=a.project)
    with Index(pdir) as idx:
        total, last = len(idx["sessions"]), idx.get("last_session")
    wfs = sorted(p.stem for p in (pdir / "workflows").glob("*.sh"))
    print(f"project:   {pdir.name}\nstore:     {pdir}\nsessions:  {total} (last #{last or '-'})\nworkflows: {', '.join(wfs) or '-'}")


def cmd_list(a):
    pdir = project_dir(name=a.project)
    with Index(pdir) as idx:
        rows = sorted(idx["sessions"].values(), key=lambda s: s["n"])
    for s in rows[-a.limit:]:
        p = pdir / "sessions" / f"{s['n']:04d}.jsonl"
        ev = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
        cmds = [e for e in ev if e["type"] == "cmd"]
        cats = {}
        for e in cmds:
            cats[e["category"]] = cats.get(e["category"], 0) + 1
        marks = [e["title"] for e in ev if e["type"] == "mark"]
        print(f"#{s['n']:04d}  {s['started']}  {s.get('branch','-'):<22} {len(cmds):>3} cmds  "
              + " ".join(f"{k}:{v}" for k, v in sorted(cats.items()))
              + (f"\n        tasks: {' | '.join(marks)}" if marks else ""))


def cmd_show(a):
    pdir = project_dir(name=a.project)
    n = resolve_session(pdir, a.session)
    cats = set(a.cat.split(",")) if a.cat else None
    for e in load(pdir, n):
        if e["type"] == "mark":
            print(f"\n=== task {e['task']}: {e['title']}")
        elif e["type"] == "note":
            print(f"    # note: {e['text']}")
        elif (not cats or e["category"] in cats) and (a.task is None or e["task"] == a.task):
            risk = assess(e["command"])  # recomputed so older logs get current rules
            flag = "" if e["status"] == "ok" else f" [{e['status']}]"
            flag += " [DANGEROUS]" if risk["danger"] else ""
            flag += " [ONCE]" if risk["once"] else ""
            print(f"{e['step']:>4} [{e['category']:<5}]{flag} {e['description'] or '-'}")
            print("       " + e["command"].replace("\n", "\n       "))


def cmd_mark(a):
    pdir = project_dir(name=a.project)
    n = resolve_session(pdir, a.session)
    ev = load(pdir, n) if (pdir / "sessions" / f"{n:04d}.jsonl").exists() else []
    task = 1 + max([e["task"] for e in ev if e["type"] == "mark"], default=0)
    append(pdir, n, {"type": "mark", "task": task, "title": a.title, "ts": now()},
           f"\n# {'=' * 70}\n# TASK {task}: {a.title}\n# {'=' * 70}\n")
    print(f"session #{n:04d}: task {task} -> {a.title}")


def cmd_note(a):
    pdir = project_dir(name=a.project)
    n = resolve_session(pdir, a.session)
    append(pdir, n, {"type": "note", "text": a.text, "ts": now()}, f"# NOTE: {a.text}\n")
    print(f"session #{n:04d}: note added")


def cmd_search(a):
    pdir = project_dir(name=a.project)
    rx = re.compile(a.pattern, re.I)
    for p in sorted((pdir / "sessions").glob("*.jsonl")):
        for e in (json.loads(l) for l in p.read_text().splitlines()):
            if e["type"] == "cmd" and (rx.search(e["command"]) or rx.search(e["description"])):
                print(f"{int(p.stem)}:{e['step']:<4} [{e['category']}] {e['description']}\n      {e['command'].splitlines()[0]}")


def parse_selector(pdir, sel):
    """'7' | '7:3-9,12' | '7@2' (task 2) | 'last' | 'all'  ->  [(session, step-filter)]"""
    if sel == "all":
        return [(int(p.stem), None) for p in sorted((pdir / "sessions").glob("*.jsonl"))]
    m = re.fullmatch(r"(\w+)(?::([\d,\-]+)|@(\d+))?", sel)
    if not m:
        sys.exit(f"wf: bad selector '{sel}' (use N, N:3-9,12, N@task, last, all)")
    n = resolve_session(pdir, m.group(1))
    if m.group(2):
        steps = set()
        for part in m.group(2).split(","):
            lo, _, hi = part.partition("-")
            steps.update(range(int(lo), int(hi or lo) + 1))
        return [(n, lambda e, s=steps: e["step"] in s)]
    if m.group(3):
        return [(n, lambda e, t=int(m.group(3)): e["task"] == t)]
    return [(n, None)]


def cmd_extract(a):
    pdir = project_dir(name=a.project)
    cats = set(a.cat.split(",")) if a.cat else None
    excl = set(a.exclude_cat.split(",")) if a.exclude_cat else set()
    picked, seen = [], set()
    for sel in a.sources:
        for n, flt in parse_selector(pdir, sel):
            for e in load(pdir, n):
                if e["type"] != "cmd" or (flt and not flt(e)):
                    continue
                if (cats and e["category"] not in cats) or e["category"] in excl:
                    continue
                if e["status"] != "ok" and not a.include_failed:
                    continue
                if a.grep and not re.search(a.grep, e["command"] + " " + e["description"], re.I):
                    continue
                if a.safe_only and assess(e["command"])["danger"]:
                    continue
                if not a.keep_dupes and e["command"] in seen:
                    continue
                if e["background"]:
                    e = {**e, "command": e["command"] + " &"}
                seen.add(e["command"])
                picked.append((n, e))
    if not picked:
        sys.exit("wf: nothing matched")

    try:
        params = parse_kv(a.param)
    except ValueError as ex:
        sys.exit(f"wf: {ex}")
    for k, v in params.items():
        if redact(f"{k}={v}") != f"{k}={v}":
            sys.exit(f"wf: {k} looks like a secret and defaults are stored in plain text - pass it at run time with -e")
        if not any(parameterize(e["command"] + "\n" + e["cwd"], {k: v}) != e["command"] + "\n" + e["cwd"] for _, e in picked):
            sys.exit(f"wf: --param {k}: value '{v}' not found in the selected steps")
    if a.auto_params:
        cwds = [e["cwd"] for _, e in picked]
        root = str(project_root(max(set(cwds), key=cwds.count)))
        if PARAM_VALUE.match(root) and "PROJECT_DIR" not in params:
            params["PROJECT_DIR"] = root

    out = pdir / "workflows" / f"{a.name}.sh"
    lines = ["#!/usr/bin/env bash",
             f"# Workflow: {a.name}",
             f"# {a.description or 'Extracted with workflow-recorder'}",
             f"# project: {pdir.name}   created: {now()}",
             f"# sources: {' '.join(a.sources)}" + (f"   categories: {a.cat}" if a.cat else ""),
             "# Run isolated:  wf run " + a.name + "   (or: wf run " + a.name + " --dry-run)",
             *[f"# param: {k}={v}" for k, v in sorted(params.items())],
             "set -euo pipefail"]
    if params:  # {{NAME}} means nothing to bash, so stop before anything runs
        lines.append(f"echo 'wf: this workflow has parameters; run it with: wf run {a.name} -P NAME=value' >&2; exit 2")
    lines.append("")
    prev_cwd, redacted, flagged = None, False, []
    for i, (n, e) in enumerate(picked, 1):
        lines.append(f"# ── {i}. [{e['category']}] {e['description'] or '(no description)'}   (from #{n:04d} step {e['step']})")
        if e["cwd"] != prev_cwd:
            lines.append(f"cd {parameterize(shlex.quote(e['cwd']), params)}")
            prev_cwd = e["cwd"]
        risk = assess(e["command"])
        lines += risk_lines(risk)
        if risk["danger"] or risk["once"]:
            flagged.append((i, risk))
        lines.append(parameterize(e["command"], params))
        lines.append("")
        redacted |= "<REDACTED>" in e["command"]
    if redacted:
        lines.insert(6, "# WARNING: some values were redacted when logged - replace <REDACTED> (e.g. with \"$ENV_VAR\") before running.")
    out.write_text("\n".join(lines) + "\n")
    out.chmod(0o755)
    print(f"wrote {out} ({len(picked)} steps)")
    for k, v in sorted(params.items()):
        print(f"  param {k} (default {v})")
    for i, risk in flagged:
        for l in risk_lines(risk):
            print(f"  step {i}: {l[4:]}")
    hints = suggest_params([e["command"] for _, e in picked], set(params.values()))
    if hints:
        print("  could parameterize: " + " ".join(f"--param {k}={v}" for k, v in hints.items()))


def cmd_inspect(a):
    pdir = project_dir(name=a.project)
    wf = pdir / "workflows" / f"{a.name}.sh"
    if not wf.exists():
        sys.exit(f"wf: no workflow '{a.name}'")
    text = wf.read_text()
    for k, v in PARAM_DECL.findall(text):
        print(f"param {k} = {v}")
    for s in parse_workflow(text):
        risk = assess(s["body"])
        label = "DANGEROUS" if risk["danger"] else "ONCE" if risk["once"] else "ok"
        print(f"{s['n']:>4} {label:<9} {s['title']}")
        for why in risk["danger"] + risk["once"]:
            print(f"       - {why}")


def cmd_workflows(a):
    pdir = project_dir(name=a.project)
    for p in sorted((pdir / "workflows").glob("*.sh")):
        head = p.read_text().splitlines()
        desc = head[2].lstrip("# ") if len(head) > 2 else ""
        steps = sum(1 for l in head if l.startswith("# ── "))
        print(f"{p.stem:<28} {steps:>3} steps  {desc}")


STEP_MARK = "::wf-step"
ISOLATED_VARS = ("HOME", "PATH", "USER", "TERM", "LANG", "JAVA_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT")


def isolated_env(extra=None, inherit=False):
    """Minimal environment for a fresh non-login, non-rc bash (or the full one with inherit)."""
    env = dict(os.environ) if inherit else {k: os.environ[k] for k in ISOLATED_VARS if k in os.environ}
    env.update(extra or {})
    return env


def parse_workflow(text):
    """Split a workflow script into steps: [{n, title, cwd, body}]. `cwd` is the directory in
    effect for that step (tracked across steps), so any subset of steps can run on its own."""
    steps, cwd, cur = [], None, None
    for line in text.splitlines():
        m = re.match(r"^# ── (\d+)\. (.*)$", line)
        if m:
            cur = {"n": int(m.group(1)), "title": m.group(2), "cwd": cwd, "body": []}
            steps.append(cur)
            continue
        if cur is None:
            continue
        cd = re.match(r"^cd (.+)$", line)
        if cd and not cur["body"]:
            try:
                cwd = cur["cwd"] = shlex.split(cd.group(1))[0]
                continue
            except ValueError:
                pass
        cur["body"].append(line)
    for s in steps:
        s["body"] = "\n".join(s["body"]).strip("\n")
    return steps


def parse_steps(spec):
    """'3' | '2-5,8' -> set of ints; empty -> None (all)."""
    if not spec:
        return None
    out = set()
    for part in str(spec).split(","):
        if part.strip():
            lo, _, hi = part.partition("-")
            out.update(range(int(lo), int(hi or lo) + 1))
    return out


def build_script(steps, only=None):
    """Runnable script for all steps (only=None) or a subset, printing a STEP_MARK line before each."""
    lines = ["set -euo pipefail"]
    for s in steps:
        if only is not None and s["n"] not in only:
            continue
        lines.append(f"printf '\\n{STEP_MARK} %s\\n' {s['n']}")
        if s["cwd"]:
            lines.append(f"cd {shlex.quote(s['cwd'])}")
        lines.append(s["body"])
    return "\n".join(lines) + "\n"


def prepare_run(pdir, name, only=None, params=None, allow_dangerous=False):
    """-> (script text, selected steps, log path). Raises ValueError on a bad request.
    Risk is judged on the rendered steps at run time, so editing the script or passing a
    parameter can't slip a dangerous command past the check."""
    wf = pdir / "workflows" / f"{name}.sh"
    if not wf.exists():
        raise ValueError(f"no workflow '{name}'")
    text = wf.read_text()
    declared = dict(PARAM_DECL.findall(text))
    unknown = set(params or {}) - set(declared)
    if unknown:
        raise ValueError(f"unknown parameter {', '.join(sorted(unknown))} (declared: {', '.join(sorted(declared)) or 'none'})")
    values = parse_kv(f"{k}={v}" for k, v in {**declared, **(params or {})}.items())
    steps = parse_workflow(text)
    for s in steps:
        s["body"] = render(s["body"], values)
        s["cwd"] = render(s["cwd"], values) if s["cwd"] else s["cwd"]
    chosen = [s for s in steps if only is None or s["n"] in only]
    if not chosen:
        raise ValueError("no matching steps")
    if any("<REDACTED>" in s["body"] for s in chosen):
        raise ValueError("selected steps still contain <REDACTED> placeholders - edit the workflow first")
    risky = [(s["n"], assess(s["body"])["danger"]) for s in chosen]
    risky = [f"step {n}: {'; '.join(why)}" for n, why in risky if why]
    if risky and not allow_dangerous:
        raise ValueError("refusing dangerous steps without explicit approval (--allow-dangerous):\n  " + "\n  ".join(risky))
    (pdir / "runs").mkdir(exist_ok=True)
    tag = "all" if only is None else "steps-" + "_".join(str(s["n"]) for s in chosen)[:40]
    log = pdir / "runs" / f"{name}-{now('%Y%m%d-%H%M%S')}-{tag}.log"
    return build_script(steps, only), chosen, log


def cmd_run(a):
    pdir = project_dir(name=a.project)
    try:
        script, chosen, log = prepare_run(pdir, a.name, parse_steps(a.steps), parse_kv(a.param),
                                          a.allow_dangerous or a.dry_run)
    except ValueError as ex:
        sys.exit(f"wf: {ex}")
    if a.dry_run:
        print(script)
        return
    env = isolated_env(dict(kv.partition("=")[::2] for kv in a.env or []), a.inherit_env)
    for s in chosen:
        for why in assess(s["body"])["once"]:
            print(f"wf: warning: step {s['n']} may not rerun cleanly: {why}", file=sys.stderr)
    print(f"running {a.name} steps {','.join(str(s['n']) for s in chosen)} (isolated env, log -> {log})")
    with open(log, "w") as f:
        proc = subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-c", script], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            sys.stdout.write(line)
            f.write(line)
        rc = proc.wait()
        f.write(f"\n::wf-exit {rc}\n")
    print(f"exit {rc}")
    sys.exit(rc)


def cmd_ui(a):
    app = HOME / "Applications" / "Workflows.app"
    subprocess.run(["open", str(app)] if app.exists() else
                   [sys.executable, str(Path(__file__).resolve().parent / "app" / "server.py"), "--open"])


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        try:
            hook()
        except Exception as ex:  # a recorder bug must never break the session
            print(f"[workflow-recorder] hook error: {ex}", file=sys.stderr)
        return

    ap = argparse.ArgumentParser(prog="wf", description="Claude Code workflow recorder")
    ap.add_argument("-p", "--project", help="project name (default: git root of cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="store location, session count, workflows").set_defaults(fn=cmd_status)
    p = sub.add_parser("list", help="list sessions"); p.add_argument("-n", "--limit", type=int, default=20); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show", help="show a session's commands")
    p.add_argument("session", nargs="?", default="last"); p.add_argument("--cat"); p.add_argument("--task", type=int)
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("mark", help="start a named task section in the session")
    p.add_argument("title"); p.add_argument("-s", "--session"); p.set_defaults(fn=cmd_mark)
    p = sub.add_parser("note", help="add a free-form comment to the session")
    p.add_argument("text"); p.add_argument("-s", "--session"); p.set_defaults(fn=cmd_note)
    p = sub.add_parser("search", help="regex search commands+descriptions across sessions")
    p.add_argument("pattern"); p.set_defaults(fn=cmd_search)
    p = sub.add_parser("extract", help="group logged commands into a runnable workflow")
    p.add_argument("name")
    p.add_argument("--from", dest="sources", nargs="+", required=True, metavar="SEL",
                   help="N | N:3-9,12 | N@task | last | all  (repeatable)")
    p.add_argument("--cat", help="only these categories (comma list: find,debug,test,build,run,git,edit,other)")
    p.add_argument("--exclude-cat", help="drop these categories (e.g. find)")
    p.add_argument("--grep", help="only commands/descriptions matching regex")
    p.add_argument("-d", "--description")
    p.add_argument("--include-failed", action="store_true")
    p.add_argument("--keep-dupes", action="store_true")
    p.add_argument("-P", "--param", action="append", metavar="NAME=VALUE",
                   help="replace VALUE with {{NAME}}; VALUE becomes the default (repeatable)")
    p.add_argument("--auto-params", action="store_true", help="also turn the project path into {{PROJECT_DIR}}")
    p.add_argument("--safe-only", action="store_true", help="drop steps flagged dangerous")
    p.set_defaults(fn=cmd_extract)
    sub.add_parser("workflows", help="list extracted workflows").set_defaults(fn=cmd_workflows)
    p = sub.add_parser("inspect", help="show a workflow's parameters and risky steps")
    p.add_argument("name"); p.set_defaults(fn=cmd_inspect)
    p = sub.add_parser("run", help="run a workflow in an isolated bash")
    p.add_argument("name"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("-s", "--steps", help="run only these steps, e.g. 3 or 2-5,8 (default: all)")
    p.add_argument("-e", "--env", action="append", metavar="K=V", help="extra env var for the run")
    p.add_argument("--inherit-env", action="store_true", help="pass the full current environment")
    p.add_argument("-P", "--param", action="append", metavar="NAME=VALUE", help="override a workflow parameter")
    p.add_argument("--allow-dangerous", action="store_true", help="run steps flagged dangerous")
    p.set_defaults(fn=cmd_run)
    sub.add_parser("ui", help="open the Workflows app (viewer + runner)").set_defaults(fn=cmd_ui)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
