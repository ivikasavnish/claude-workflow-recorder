#!/usr/bin/env python3
"""Fix memory: a local, growing library of "this failed -> this fixed it", used by `wf fix`, `wf run --fix`,
the recorder hook and the MCP server.

Where fixes come from
  * sessions: a step fails, a few steps later the same command succeeds -> the steps in between are the fix
    (or, if nothing ran in between, the changed command is). Learned live by the hook and by `wf learn`.
  * agents: `wf fix` hands a failure it can't fix from memory to a local coding agent over ACP (Claude,
    Codex, Gemini, ...) or a headless exec (muse). Only a change that makes the command pass is learned,
    as a patch.

How it gets better
  Every fix keeps wins/losses. A fix is tried again only when its error matches, ranked by
  match x confidence ((wins+1)/(wins+losses+2)); fixes that keep failing stop being offered. Agents get the
  best related past fixes from every project in their prompt, so they start from what worked before.

Store: ~/workflows/<project>/fixes.jsonl (one fix per line), fix transcripts in runs/fix-*.log.
"""
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import wf

WINDOW = 15          # a success more than this many steps after the failure isn't treated as its fix
MIN_SCORE = 0.3      # weaker matches are not offered
HINT_SCORE = 0.45    # ...and not pushed into a live Claude session
SKIP_CATS = {"find", "note", "edit"}  # failed reads/edits are not "a command that needs fixing"


# ---------------------------------------------------------------- matching

def words(s):
    return {w for w in re.findall(r"[a-z_][a-z0-9_]{2,}", s.lower())}


def norm_error(s):
    s = re.sub(r"(?:[\w.@~+-]*/)+[\w.@~+-]+", " ", s)       # paths differ between machines and checkouts
    s = re.sub(r"0x[0-9a-fA-F]+|\b[0-9a-f]{7,40}\b", " ", s)  # addresses, hashes
    return re.sub(r"\d+", " ", s)


def err_words(s):
    return words(norm_error(s))


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def first_line(cmd):
    view = wf.strip_heredocs(cmd).strip()
    return view.splitlines()[0] if view else ""


SETUP = ("cd", "pushd", "export", "source", ".", "set", "unset", "echo", "true")


def main_part(cmd):
    """The part of the first line that does the work: `cd x && export A=1 && npm test` -> `npm test`."""
    segs = [x.strip() for x in re.split(r"&&|\|\||;", first_line(cmd)) if x.strip()]
    for seg in segs:
        if seg.split()[0] not in SETUP:
            return seg
    return segs[-1] if segs else ""


def program(cmd):
    try:
        parts = shlex.split(main_part(cmd), comments=True)
    except ValueError:
        parts = main_part(cmd).split()
    while parts and (re.match(r"^\w+=", parts[0]) or parts[0] in ("sudo", "env", "time", "command", "exec", "timeout")):
        parts.pop(0)
        if parts and re.match(r"^\d+[smh]?$", parts[0]):  # timeout 60 ...
            parts.pop(0)
    return Path(parts[0]).name if parts else ""


def cmd_sim(a, b):
    if not a or not b or program(a) != program(b):
        return 0.0
    return max(jaccard(words(main_part(a)), words(main_part(b))), 0.2)


def error_match(known, output):
    """How well a known error line shows up in a command's output (0..1)."""
    k = err_words(known)
    if not k or not output:
        return 0.0
    lines = [l for l in output.splitlines() if l.strip()][-60:]
    best = max((jaccard(k, err_words(l)) for l in lines), default=0.0)
    tail = err_words("\n".join(lines))
    return max(best, 0.8 * len(k & tail) / len(k))


# ---------------------------------------------------------------- store

def store(pdir):
    return Path(pdir) / "fixes.jsonl"


def load(pdir):
    p = store(pdir)
    if not p.exists():
        return []
    out = []
    for l in p.read_text().splitlines():
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def update(pdir, fn):
    """Read-modify-write fixes.jsonl under an flock (parallel hooks)."""
    pdir = Path(pdir)
    with open(pdir / ".fixes.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        items = load(pdir)
        res = fn(items)
        tmp = store(pdir).with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items))
        tmp.replace(store(pdir))
        return res


def fix_id(*parts):
    return hashlib.sha1("\x00".join(map(str, parts)).encode()).hexdigest()[:10]


def upsert(pdir, entry):
    """Add a fix, or count another win for one already known. -> (id, is_new)"""
    def go(items):
        for i in items:
            if i["id"] == entry["id"]:
                return i["id"], False
        items.append(entry)
        return entry["id"], True
    return update(pdir, go)


def outcome(pdir, fid, won):
    def go(items):
        for i in items:
            if i["id"] == fid:
                i["wins" if won else "losses"] = i.get("wins" if won else "losses", 0) + 1
                i["last_used"] = wf.now()
                return i
    return update(pdir, go)


def forget(pdir, fid):
    def go(items):
        n = len(items)
        items[:] = [i for i in items if i["id"] != fid]
        return n != len(items)
    return update(pdir, go)


def projects():
    return sorted(p for p in wf.ROOT.iterdir() if (p / "fixes.jsonl").exists()) if wf.ROOT.exists() else []


def entry(pdir, origin, source, command, cwd, error, kind, **kw):
    e = {"id": fix_id(pdir.name, origin, source), "origin": origin, "source": source, "ts": wf.now(),
         "command": command[:4000], "cwd": cwd, "error": wf.redact(error)[:500], "kind": kind,
         "steps": [], "retry": "", "patch": "", "summary": "", "wins": 1, "losses": 0}
    e.update(kw)
    return e


# ---------------------------------------------------------------- learning from sessions

# Steps that only look (or re-test) don't fix anything; edits, builds, installs, config changes might.
NOT_A_FIX = {"find", "note", "test", "debug"}


def useful_step(e, failed):
    if e["type"] != "cmd" or e["status"] != "ok" or e["category"] in NOT_A_FIX or e.get("background"):
        return False
    risk = wf.assess(e["command"])
    if risk["danger"] or any(w in why for why in risk["once"] for w in ("commit", "push", "remote")):
        return False
    return cmd_sim(e["command"], failed["command"]) < 0.6  # retries of the failing command aren't the fix


def learn_events(pdir, n, events):
    """Learn failure -> fix pairs from one session's events. Idempotent. -> number of new fixes."""
    cmds = [e for e in events if e["type"] == "cmd"]
    new = 0
    for i, f in enumerate(cmds):
        if f["status"] != "FAILED" or f["category"] in SKIP_CATS:
            continue
        ok = None
        for s in cmds[i + 1:i + 1 + WINDOW]:
            if cmd_sim(s["command"], f["command"]) >= 0.6 and s["cwd"] == f["cwd"]:
                ok = s if s["status"] == "ok" else None
                break  # a later failure of the same command learns from its own success
        if not ok:
            continue
        between = [e for e in cmds[i + 1:cmds.index(ok)] if useful_step(e, f)]
        if not between and ok["command"] == f["command"]:
            continue  # it just passed on retry: nothing to learn
        if not between and f["category"] in ("git", "other"):
            continue  # e.g. `git diff` exiting 1: a reworded command there is rarely a fix worth replaying
        kind = "steps" if between else "command"
        e = entry(Path(pdir), "session", f"#{n:04d}:{f['step']}->{ok['step']}", f["command"], f["cwd"],
                  f.get("error") or "", kind, description=f.get("description", ""), retry=ok["command"],
                  steps=[{"cwd": s["cwd"], "command": s["command"], "description": s["description"]} for s in between])
        new += upsert(pdir, e)[1]
    return new


def learn_session(pdir, n):
    p = Path(pdir) / "sessions" / f"{int(n):04d}.jsonl"
    if not p.exists():
        return 0
    return learn_events(pdir, int(n), [json.loads(l) for l in p.read_text().splitlines() if l.strip()])


# ---------------------------------------------------------------- lookup

def lookup(command, output, pdir=None, everywhere=True, limit=5):
    """-> [(score, project_dir, fix)] best first. Fixes from other projects are hints only."""
    homes = projects() if everywhere else []
    if pdir and Path(pdir) not in homes:
        homes.append(Path(pdir))
    found = []
    for home in homes:
        local = pdir is not None and home.name == Path(pdir).name
        for fx in load(home):
            wins, losses = fx.get("wins", 0), fx.get("losses", 0)
            if losses >= 3 and wins == 0:
                continue  # retired
            es = error_match(fx.get("error", ""), output)
            cs = cmd_sim(command, fx.get("command", ""))
            if es < 0.35:
                continue
            conf = (wins + 1) / (wins + losses + 2)
            score = (0.65 * es + 0.35 * cs) * conf * (1.0 if local else 0.8)
            if score >= MIN_SCORE:
                found.append((round(score, 3), home, fx))
    found.sort(key=lambda t: -t[0])
    return found[:limit]


def describe(fx, width=160):
    stats = f"worked {fx.get('wins', 0)}/{fx.get('wins', 0) + fx.get('losses', 0)}"
    if fx["kind"] == "steps":
        body = " ; ".join(first_line(s["command"])[:80] for s in fx["steps"])
    elif fx["kind"] == "command":
        body = "run instead: " + first_line(fx["retry"])
    elif fx["kind"] == "patch":
        files = re.findall(r"^\+\+\+ b/(\S+)", fx.get("patch", ""), re.M)
        body = f"patch {', '.join(files) or '?'}" + (f" — {fx['summary'][:120]}" if fx.get("summary") else "")
    else:
        body = fx.get("summary", "")[:200]
    return f"[{fx['id']}] {stats} ({fx['origin']} {fx['source']}): {body}"[:width * 2]


def hook_hint(pdir, command, error):
    """Text for Claude when a step fails and memory knows a fix (None otherwise)."""
    hits = [h for h in lookup(command, error, pdir) if h[0] >= HINT_SCORE][:2]
    if not hits:
        return None
    lines = ["[workflow-recorder] This error matches fixes that worked before:"]
    for score, home, fx in hits:
        lines.append(f"- {describe(fx)}" + ("" if home.name == Path(pdir).name else f" (project {home.name})"))
        if fx["kind"] == "steps":
            lines += [f"    {s['command'][:300]}" for s in fx["steps"][:4]]
    lines.append("Check it applies before using it. `wf fixes show <id>` has the full fix.")
    return "\n".join(lines)


def hook_learn(pdir, n, events):
    recent = [e for e in events if e["type"] == "cmd"][-(WINDOW + 1):]
    if any(e["status"] == "FAILED" for e in recent[:-1]):
        learn_events(pdir, n, recent)


# ---------------------------------------------------------------- applying

def sh(cmd, cwd, env, out, log=None):
    """Run a step the way `wf run` does. -> (rc, last ~200 lines of output)"""
    proc = subprocess.Popen(["/bin/bash", "--noprofile", "--norc", "-c", "set -euo pipefail\n" + cmd],
                            cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail = []
    for line in proc.stdout:
        out.write(line)
        if log:
            log.write(line)
        tail.append(line)
        del tail[:-200]
    return proc.wait(), "".join(tail)


def is_git(root):
    return subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                          capture_output=True, text=True).returncode == 0


def apply_patch(root, patch, reverse=False):
    if is_git(root):
        base = ["git", "-C", str(root), "apply", "--whitespace=nowarn"] + (["-R"] if reverse else [])
        check = base + ["--check"]
    else:
        base = ["patch", "-p1", "-s", "-d", str(root)] + (["-R"] if reverse else ["--forward"])
        check = base + ["--dry-run"]
    if subprocess.run(check, input=patch, text=True, capture_output=True).returncode:
        return False
    return subprocess.run(base, input=patch, text=True, capture_output=True).returncode == 0


def git_state(root):
    if not is_git(root):
        return None
    g = lambda *a: subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True)
    base = g("stash", "create").stdout.strip() or g("rev-parse", "HEAD").stdout.strip()
    if not base:
        return None
    return {"base": base, "untracked": set(g("ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")) - {""}}


def git_changes(root, state):
    """The change made since git_state(): tracked edits + new untracked files, as one applicable patch."""
    g = lambda *a: subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True).stdout
    top = Path(g("rev-parse", "--show-toplevel").strip() or root)
    text = g("diff", "--binary", state["base"])
    now_untracked = set(g("ls-files", "--others", "--exclude-standard", "-z").split("\0")) - {""}
    for rel in sorted(now_untracked - state["untracked"]):
        if not wf.SENSITIVE_PATH.search(rel):
            text += subprocess.run(["git", "diff", "--no-index", "--binary", "--", "/dev/null", rel],
                                   cwd=top, capture_output=True, text=True).stdout
    return text


def blocked(fx, local, allow_dangerous):
    """Why a remembered fix can't be applied automatically here (None if it can)."""
    if fx["kind"] == "hint":
        return "only a hint"
    if not local:
        return "learned in another project"
    text = "\n".join([fx.get("retry", "")] + [s["command"] for s in fx.get("steps", [])])
    if "<REDACTED>" in text:
        return "contains <REDACTED> values"
    if not allow_dangerous and wf.assess(text)["danger"]:
        return "has dangerous steps (--allow-dangerous)"
    if any(s["cwd"] and not Path(s["cwd"]).is_dir() for s in fx.get("steps", [])):
        return "its directory no longer exists"
    return None


def apply(fx, root, env, out, log):
    """-> (applied, command to verify with or None for the original, undo)"""
    undo = lambda: None
    if fx["kind"] == "steps":
        for s in fx["steps"]:
            out.write(f"  $ {first_line(s['command'])}\n")
            rc, _ = sh(s["command"], s["cwd"] or str(root), env, out, log)
            if rc:
                return False, None, undo
        return True, None, undo
    if fx["kind"] == "command":
        return True, fx["retry"], undo
    if fx["kind"] == "patch":
        if not apply_patch(root, fx["patch"]):
            out.write("  patch no longer applies\n")
            return False, None, undo
        return True, None, lambda: apply_patch(root, fx["patch"], reverse=True)
    return False, None, undo


# ---------------------------------------------------------------- the fix loop

PROMPT = """A command in this project fails. Make the smallest change that makes it succeed.

Project root: {root}
Command (run from {cwd}):
{command}

It exited with code {rc}. End of its output:
```
{output}
```
{hints}
Rules: change files inside the project only; don't commit, push, delete data or run anything destructive;
don't just skip or disable the failing check unless that is clearly the right fix. You may run the command
to check your change. When you are done, reply with two or three sentences: the root cause and what you changed.
"""


def build_prompt(command, cwd, root, rc, output, hints, attempt):
    lines = [l for l in output.splitlines()][-80:]
    h = ""
    if hints:
        h = "\nFixes that worked for similar errors before (from the local fix memory; they may not apply):\n"
        for _, home, fx in hints:
            h += f"- {describe(fx)}\n"
            if fx["kind"] == "patch":
                h += "  " + fx["patch"][:1200].replace("\n", "\n  ") + "\n"
            elif fx["kind"] == "steps":
                h += "".join(f"  $ {s['command'][:300]}\n" for s in fx["steps"][:5])
    if attempt:
        h += "\nYour previous change did not make the command pass; the output above is from after it.\n"
    return PROMPT.format(root=root, cwd=cwd, command=command, rc=rc, output=wf.redact("\n".join(lines)), hints=h)


class Options:
    def __init__(self, agent=None, transport=None, yes=False, memory=True, attempts=2, allow_dangerous=False,
                 timeout=900, env=None, ask=None, emit=None):
        self.agent, self.transport, self.yes, self.memory = agent, transport, yes, memory
        self.attempts, self.allow_dangerous, self.timeout = attempts, allow_dangerous, timeout
        self.env = env if env is not None else dict(os.environ)
        self.ask = ask              # (prompt, meta) -> one letter, or None if nobody can answer
        self.emit = emit or (lambda _type, **data: None)


class EventStream:
    """`--ui-events`: the Workflows app runs `wf run --fix` as a child. Output stays plain text; structured
    events go on their own `::wf-event {json}` lines; questions are events too, answered by one JSON line
    {"ask": id, "answer": "y"} on stdin."""
    PREFIX = "::wf-event "

    def __init__(self, out=sys.stdout, inp=sys.stdin):
        import threading
        self.out, self.inp, self.lock, self.mid_line, self.n = out, inp, threading.RLock(), False, 0

    def write(self, text):
        with self.lock:
            self.out.write(text)
            if text:
                self.mid_line = not text.endswith("\n")
            self.out.flush()

    def flush(self):
        self.out.flush()

    def emit(self, _type, /, **data):  # positional-only: event data may have its own "kind"
        with self.lock:
            if self.mid_line:
                self.out.write("\n")
                self.mid_line = False
            self.out.write(self.PREFIX + json.dumps({**data, "type": _type}, ensure_ascii=False) + "\n")
            self.out.flush()

    def ask(self, prompt, meta=None):
        with self.lock:
            self.n += 1
            aid = self.n
        self.emit("ask", id=aid, prompt=prompt.strip(), **(meta or {}))
        while True:
            line = self.inp.readline()
            if not line:
                return None  # the app went away
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("ask") == aid:
                answer = (msg.get("answer") or "")[:1].lower()
                self.emit("answered", id=aid, answer=answer)
                return answer


def confirm(opts, question, meta=None):
    if opts.yes:
        return True
    import wf_acp
    answer = (opts.ask or wf_acp.ask)(question + " [y/N] ", {"kind": "confirm", "title": question.strip(), **(meta or {})})
    if answer is None:
        print("  skipped: no terminal to ask on (pass --yes to apply remembered fixes)")
    return answer == "y"


def fix_command(pdir, command, cwd, opts, rc=None, output=None, out=None):
    """Try to make `command` pass: remembered fixes first, then a local agent. -> True if it passes."""
    import wf_acp
    out = out or sys.stdout
    pdir, root = Path(pdir), wf.project_root(cwd)
    (pdir / "runs").mkdir(exist_ok=True)
    log_path = pdir / "runs" / f"fix-{wf.now('%Y%m%d-%H%M%S')}.log"
    with open(log_path, "w") as log:
        log.write(f"# wf fix\n# cwd: {cwd}\n# command:\n{command}\n")
        if output is None:
            out.write(f"wf fix: running the command to see how it fails\n")
            rc, output = sh(command, cwd, opts.env, out, log)
            if rc == 0:
                out.write("wf fix: the command passes; nothing to fix\n")
                return True

        tried = set()
        if opts.memory:
            for score, home, fx in lookup(command, output, pdir):
                local = home.name == pdir.name
                why = blocked(fx, local, opts.allow_dangerous)
                out.write(f"\nwf fix: known fix (match {score:.2f}) {describe(fx)}\n")
                opts.emit("memory", id=fx["id"], score=score, describe=describe(fx), blocked=why, project=home.name)
                if why:
                    out.write(f"  not applied: {why}\n")
                    continue
                if not confirm(opts, "  apply it?", {"detail": describe(fx), "fix": fx["id"]}):
                    continue
                tried.add(fx["id"])
                ok, alt, undo = apply(fx, root, opts.env, out, log)
                if ok:
                    rc2, out2 = sh(alt or command, cwd, opts.env, out, log)
                    opts.emit("verify", rc=rc2, by=f"memory {fx['id']}")
                    if rc2 == 0:
                        outcome(home, fx["id"], True)
                        opts.emit("fixed", by="memory", id=fx["id"], replacement=alt)
                        out.write(f"wf fix: fixed by remembered fix {fx['id']}\n")
                        if alt:
                            out.write(f"  note: the working command is different; update the step to:\n  {alt}\n")
                        log.write(f"\n::wf-fixed memory {fx['id']}\n")
                        return True
                    rc, output = rc2, out2
                outcome(home, fx["id"], False)
                opts.emit("memory_failed", id=fx["id"])
                undo()

        if opts.agent == "none":
            out.write("wf fix: no remembered fix worked and agents are off (--agent none)\n")
            return False
        name = opts.agent or wf_acp.default_agent()
        agent = wf_acp.resolve(name, opts.transport) if name else None
        if not agent:
            out.write(f"wf fix: no agent available{f' ({name})' if name else ''}; see `wf agents`\n")
            return False
        perms = wf_acp.Permissions("yes" if opts.yes else "ask", lambda c: wf.assess(c or "")["danger"], out, opts.ask)
        hints = lookup(command, output, pdir, limit=3)
        for attempt in range(max(1, opts.attempts)):
            out.write(f"\nwf fix: asking {agent['name']} over {agent['transport'].upper()}"
                      f" (attempt {attempt + 1}/{opts.attempts}, transcript -> {log_path})\n")
            state = git_state(root)
            log.write(f"\n::wf-agent {agent['name']} {agent['transport']} {' '.join(agent['argv'])}\n")
            log.flush()
            prompt = build_prompt(command, cwd, root, rc, output, hints, attempt)
            opts.emit("agent_start", agent=agent["name"], transport=agent["transport"], argv=agent["argv"],
                      attempt=attempt + 1, attempts=opts.attempts, prompt=prompt, log=log_path.name)
            res = wf_acp.run_agent(agent, prompt, cwd, root, perms, out=out, log=log, timeout=opts.timeout, emit=opts.emit)
            opts.emit("agent_end", agent=agent["name"], ok=res["ok"], stop=res.get("stop"), summary=res["summary"][-1500:])
            patch = git_changes(root, state) if state else res["patch"]
            if not res["ok"]:
                out.write(f"wf fix: agent stopped: {res['summary'][:300] or res.get('stop')}\n")
            out.write("\nwf fix: checking the command again\n")
            rc2, out2 = sh(command, cwd, opts.env, out, log)
            opts.emit("verify", rc=rc2, by=agent["name"])
            if rc2 == 0:
                e = entry(pdir, f"agent:{agent['name']}", wf.now(), command, cwd, last_error(output),
                          "patch" if patch.strip() else "hint", patch=patch, summary=wf.redact(res["summary"])[-1500:],
                          description="")
                fid, _ = upsert(pdir, e)
                opts.emit("fixed", by=agent["name"], id=fid, kind=e["kind"], patch=patch[:20000])
                log.write(f"\n::wf-fixed agent {fid}\n")
                out.write(f"wf fix: fixed by {agent['name']}; remembered as fix {fid}"
                          f"{' (patch)' if patch.strip() else ' (no file changes: kept as a hint)'}\n")
                return True
            rc, output = rc2, out2
        out.write(f"wf fix: still failing; any changes the agent made are left in place for you to review\n")
        opts.emit("not_fixed")
        return False


def last_error(output):
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    for l in reversed(lines[-40:]):
        if re.search(r"(?i)error|fail|exception|not found|cannot|denied|undefined|traceback|panic", l):
            return l[:300]
    return lines[-1][:300] if lines else ""


def run_with_fix(pdir, name, chosen, env, log_path, opts, out=None):
    """`wf run --fix`: run steps one at a time; on a failure, fix it and retry that step. -> exit code"""
    out = out or sys.stdout
    with open(log_path, "w") as log:
        for s in chosen:
            cwd = s["cwd"] or os.getcwd()
            line = f"\n{wf.STEP_MARK} {s['n']}\n"
            out.write(line)
            log.write(line)
            rc, output = sh(s["body"], cwd, env, out, log)
            if rc == 0:
                continue
            log.flush()
            # fix_command re-runs the step to verify, so a fixed step has already passed
            opts.emit("step_failed", step=s["n"], rc=rc, error=last_error(output))
            if not fix_command(pdir, s["body"], cwd, opts, rc, output, out):
                log.write(f"\n::wf-exit {rc}\n")
                out.write(f"exit {rc} (step {s['n']})\n")
                return rc
            log.write(f"\n::wf-fixed step {s['n']}\n")
            opts.emit("step_fixed", step=s["n"])
            out.write(f"wf run: step {s['n']} fixed, continuing\n")
        log.write("\n::wf-exit 0\n")
    out.write("exit 0\n")
    return 0
