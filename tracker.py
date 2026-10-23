"""Agent tracker: live view of every coding agent (Claude Code, Codex, Copilot) in CLI, VS Code or desktop, from local transcripts.

  agents                 # live table of agents active in last 24h, refreshes every 2s
  agents --once          # print once and exit
  agents -p erp          # only projects whose path contains "erp"
  agents --here          # only agents running in the current directory
  agents --hours 2       # change the time window
  agents 5aeb0dda        # timeline for one agent (session id prefix)
  agents --kill 5aeb0dda # close a running Claude Code agent (asks first)
  agents --json          # one JSON snapshot; add --stream for one line per refresh (used by the VS Code extension)
"""
import argparse, glob, json, os, re, signal, subprocess, sys, time
import urllib.parse, urllib.request
from datetime import datetime, timezone

ACTIVE_SECS = 90  # transcript written within this window = agent is working
STUCK_SECS = 20  # a quick tool (Edit, Read...) unanswered this long = waiting on a permission prompt
SLOW_STUCK_SECS = 90  # same for tools that can legitimately run long (shell, subagents, web)
SLOW_TOOLS = {"Bash", "PowerShell", "Agent", "Task", "WebFetch", "WebSearch", "Monitor", "shell", "shell_command",
              "run_in_terminal"}
ASK_TOOLS = {"AskUserQuestion", "ExitPlanMode", "request_user_input"}
CONFLICT_SECS = 3600  # two agents editing the same file within this window = conflict
_cache = {}  # path -> (mtime, size, parsed)
# models with a 1M context window; everything else (and Codex, which reports its own) uses 200k
CTX_1M = ("claude-fable", "claude-mythos", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
          "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-5")

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
PALETTE = ["36", "35", "33", "32", "34", "96", "95", "93"]
TOOL_COLOR = {"Bash": "36", "PowerShell": "36", "shell": "36", "shell_command": "36", "Edit": "33", "Write": "33",
              "NotebookEdit": "33", "apply_patch": "33", "Read": "34", "Grep": "34", "Glob": "34", "Agent": "35",
              "Task": "35", "WebFetch": "32", "WebSearch": "32", "web_search": "32", "run_in_terminal": "36",
              "copilot_readFile": "34", "copilot_findFiles": "34", "copilot_findTextInFiles": "34", "copilot_listDirectory": "34",
              "copilot_replaceString": "33", "copilot_multiReplaceString": "33", "copilot_createFile": "33",
              "copilot_applyPatch": "33", "copilot_insertEdit": "33"}
SRC_COLOR = {"claude": "38;5;209", "codex": "38;5;75", "copilot": "38;5;141"}


def c(code, s):
    return f"\033[{code}m{s}\033[0m" if COLOR else s


def epoch(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0


def summarize(name, inp):
    if name in ("AskUserQuestion", "request_user_input") and isinstance(inp.get("questions"), list) and inp["questions"]:
        q = inp["questions"][0]
        return f"{name}: {q.get('question', '') if isinstance(q, dict) else q}"
    for k in ("command", "file_path", "notebook_path", "path", "pattern", "description", "url", "query", "prompt"):
        v = inp.get(k)
        if isinstance(v, list):  # codex `shell` passes argv
            v = " ".join(map(str, v))
        if isinstance(v, str) and v.strip():
            return f"{name}: {v}"
    return name


def agent_id(path):
    return os.path.splitext(os.path.basename(path))[0][-36:]


def jsonl(f):
    for line in f:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            yield d


def add_event(a, ts, name, inp, call_id):
    """Record a tool call; it stays in a["pending"] until its result arrives."""
    a["events"].append([ts, summarize(name, inp), None, name])  # [timestamp, summary, ok, tool]
    if call_id:
        a["pending"][call_id] = a["events"][-1]


def finish_event(a, call_id, ok):
    ev = a["pending"].pop(call_id, None)
    if ev:
        ev[2] = ok


def parse_claude(f, a):
    seen = set()
    for d in jsonl(f):
        a["cwd"] = d.get("cwd") or a["cwd"]
        if isinstance(d.get("entrypoint"), str):  # cli, claude-vscode, claude-desktop, sdk-ts, ...
            a["where"] = d["entrypoint"].replace("claude-", "")
        t = d.get("type")
        if t == "ai-title":
            a["title"] = d.get("aiTitle", "")
        elif t == "last-prompt" and d.get("lastPrompt"):
            a["prompt"] = d["lastPrompt"]
        m = d.get("message")
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if t == "user":
            if isinstance(content, str) and not d.get("isMeta"):
                a["prompt"], a["ended"] = content, False
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    finish_event(a, b.get("tool_use_id"), not b.get("is_error"))
            continue
        if t != "assistant":
            continue
        a["model"] = m.get("model") or a["model"]
        u = m.get("usage")
        # one API message is split across several records that repeat the same usage; count it once
        if u and m.get("id") not in seen:
            seen.add(m.get("id"))
            inp, cw, cr, out = (u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
            a["ctx"] = inp + cw + cr + out
            a["out"] += out
            a["tokens"] += inp + cw + cr + out
        a["ended"] = m.get("stop_reason") == "end_turn"
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                inp = b.get("input") or {}
                if b["name"] == "TodoWrite" and isinstance(inp.get("todos"), list):
                    a["todos"] = inp["todos"]
                add_event(a, d.get("timestamp", ""), b["name"], inp, b.get("id"))
                if b["name"] in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                    path = inp.get("file_path") or inp.get("notebook_path")
                    if isinstance(path, str):
                        a["edits"][os.path.normcase(os.path.normpath(path))] = epoch(d.get("timestamp", ""))


def codex_ok(output):
    """Codex tool outputs are either 'Exit code: N\\n...' text or JSON with metadata.exit_code."""
    if not isinstance(output, str):
        return True
    try:
        return (json.loads(output).get("metadata") or {}).get("exit_code", 0) == 0
    except (ValueError, AttributeError):
        m = re.match(r"Exit code: (\d+)", output)
        return not m or m.group(1) == "0"


def parse_codex(f, a):
    seen = set()
    for d in jsonl(f):
        p = d.get("payload")
        if not isinstance(p, dict):
            continue
        a["cwd"] = p.get("cwd") or a["cwd"]
        if d.get("type") == "turn_context":
            a["model"] = p.get("model") or a["model"]
        elif d.get("type") == "session_meta":  # source: vscode / cli / exec
            a["where"] = p["source"] if isinstance(p.get("source"), str) else "vscode" if "vscode" in str(p.get("originator")) else "cli"
        pt, ts = p.get("type"), d.get("timestamp", "")
        if pt == "token_count" and isinstance(p.get("info"), dict):
            tot, last = p["info"].get("total_token_usage") or {}, p["info"].get("last_token_usage") or {}
            a["tokens"], a["out"] = tot.get("total_tokens", a["tokens"]), tot.get("output_tokens", a["out"])
            a["ctx"] = last.get("total_tokens", a["ctx"])
            a["cap"] = p["info"].get("model_context_window") or a["cap"]
        elif pt == "user_message" and p.get("message"):
            a["prompt"] = p["message"]
        elif pt == "task_started":
            a["ended"] = False
        elif pt == "task_complete":
            a["ended"] = True
        elif pt in ("function_call_output", "custom_tool_call_output"):
            finish_event(a, p.get("call_id"), codex_ok(p.get("output")))
        elif pt in ("function_call", "custom_tool_call", "web_search_call"):
            if p.get("call_id") in seen:  # rollouts can log the same call twice
                continue
            seen.add(p.get("call_id") or object())
            name, inp = p.get("name") or "web_search", {}
            if pt == "function_call":
                try:
                    inp = json.loads(p.get("arguments") or "{}")
                except ValueError:
                    pass
            elif pt == "custom_tool_call":  # apply_patch: raw patch text
                patch = str(p.get("input", ""))
                inp = {"command": patch.replace("*** Begin Patch", "")}
                for path in re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", patch, re.M):
                    a["edits"][os.path.normcase(os.path.normpath(os.path.join(a["cwd"], path.strip())))] = epoch(ts)
            else:
                inp = {"query": (p.get("action") or {}).get("query", "")}
            inp = inp if isinstance(inp, dict) else {}
            if name == "update_plan" and isinstance(inp.get("plan"), list):
                a["todos"] = [{"content": s.get("step", ""), "status": s.get("status")} for s in inp["plan"] if isinstance(s, dict)]
            add_event(a, ts, name, inp, p.get("call_id") if pt != "web_search_call" else None)


def vscode_chat(f):
    """A VS Code chat session: either one JSON doc, or a .jsonl mutation log
    (kind 0 = snapshot, 1 = set value at path k, 2 = append to array at k after truncating to i, 3 = delete)."""
    text = f.read()
    try:
        d = json.loads(text)
        return d["v"] if isinstance(d, dict) and d.get("kind") == 0 else d
    except ValueError:
        pass
    s = {}
    for d in jsonl(text.splitlines()):
        kind = d.get("kind")
        if kind == 0:
            s = d.get("v") or {}
            continue
        try:
            *path, last = d["k"]
            target = s
            for key in path:
                target = target[key]
            if kind == 1:
                target[last] = d.get("v")
            elif kind == 2:
                arr = target[last]
                if "i" in d:
                    del arr[d["i"]:]
                arr.extend(d.get("v") or [])
            elif kind == 3:
                del target[last]
        except (KeyError, IndexError, TypeError, ValueError):
            continue  # patch for a part of the session we never saw; skip it
    return s if isinstance(s, dict) else {}


def uri_path(uri):
    u = urllib.parse.urlparse(uri)
    # unquote first: url2pathname only recognises "/c:/x" as a drive path, not VS Code's "/c%3A/x"
    return urllib.request.url2pathname(urllib.parse.unquote(u.path)) if u.scheme == "file" else urllib.parse.unquote(u.path)


def workspace_folder(session_path):
    """workspaceStorage/<hash>/workspace.json names the folder that VS Code window has open."""
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(session_path)), "workspace.json"), encoding="utf-8") as f:
            w = json.load(f)
    except (OSError, ValueError):
        return None
    if w.get("folder"):
        return uri_path(w["folder"])
    return os.path.dirname(uri_path(w["workspace"])) if w.get("workspace") else None


COPILOT_EDIT_TOOLS = ("replaceString", "multiReplaceString", "createFile", "applyPatch", "insertEdit", "editFile")
COPILOT_TODO = {"in-progress": "in_progress", "not-started": "pending", "completed": "completed"}


def parse_copilot(f, a):
    s = vscode_chat(f)
    a["cwd"], a["where"] = workspace_folder(a["path"]) or "(no folder)", "vscode"
    a["title"] = s.get("customTitle") or ""
    for r in s.get("requests") or []:
        if not isinstance(r, dict):
            continue
        ms = r.get("timestamp")
        ts = datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if isinstance(ms, (int, float)) else ""
        a["model"] = str(r.get("modelId") or a["model"]).replace("copilot/", "")
        if isinstance(r.get("message"), dict):
            a["prompt"] = r["message"].get("text", a["prompt"])
        res = r.get("result") or {}
        md = res.get("metadata") or {}
        prompt_t, out_t = md.get("promptTokens") or 0, md.get("outputTokens") or 0
        a["tokens"] += prompt_t + out_t
        a["out"] += out_t
        a["ctx"] = prompt_t + out_t or a["ctx"]
        for x in r.get("response") or []:
            if not (isinstance(x, dict) and x.get("kind") == "toolInvocationSerialized"):
                continue
            tool, msg = x.get("toolId") or "tool", x.get("invocationMessage")
            uris = (msg.get("uris") or {}) if isinstance(msg, dict) else {}
            text = msg.get("value", "") if isinstance(msg, dict) else str(msg or "")
            # "[](file:///c%3A/x/app.py#1-1)" → "app.py"
            text = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", lambda m: m.group(1) or os.path.basename(uri_path(m.group(2))), text)
            call_id = x.get("toolCallId") or f"{r.get('requestId')}:{len(a['events'])}"
            tsd = x.get("toolSpecificData") or {}
            term = tsd.get("terminalCommandState") or {} if tsd.get("kind") == "terminal" else {}
            if tsd.get("kind") == "terminal":
                text = (tsd.get("commandLine") or {}).get("original") or text
            when = term.get("timestamp")
            add_event(a, datetime.fromtimestamp(when / 1000, timezone.utc).isoformat() if isinstance(when, (int, float)) else ts,
                      tool, {"command": text}, call_id)
            if x.get("isComplete"):
                finish_event(a, call_id, term.get("exitCode") in (None, 0))
            if tool.endswith(COPILOT_EDIT_TOOLS):
                for u in uris.values():
                    if isinstance(u, dict) and u.get("scheme") == "file" and u.get("path"):
                        a["edits"][os.path.normcase(os.path.normpath(urllib.request.url2pathname(u["path"])))] = epoch(ts)
            todos = tsd.get("todoList")
            if isinstance(todos, list):
                a["todos"] = [{"content": t.get("title", ""), "status": COPILOT_TODO.get(t.get("status"))}
                              for t in todos if isinstance(t, dict)]
        if isinstance(res.get("errorDetails"), dict):
            add_event(a, ts, "error", {"description": res["errorDetails"].get("message", "")}, None)
            a["events"][-1][2] = False
        a["ended"] = bool(res) or (r.get("modelState") or {}).get("value") not in (None, 0)
        if a["ended"]:
            a["pending"].clear()  # a finished request can't be waiting on anything (cancelled tools stay incomplete)


def vscode_user_dirs():
    """VS Code's per-user data folder on each OS, for stable, Insiders and VSCodium."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", "")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return [os.path.join(glob.escape(base), app, "User") for app in ("Code", "Code - Insiders", "VSCodium")]


# add another agent: a name, a glob for its transcripts, and a parser that fills the agent dict.
# Claude Code and Codex write the same transcripts whether they run in a terminal, VS Code or the desktop app.
SOURCES = [
    ("claude", "~/.claude/projects/**/*.jsonl", parse_claude),
    ("codex", "~/.codex/sessions/**/rollout-*.jsonl", parse_codex),
] + [("copilot", os.path.join(d, *sub), parse_copilot) for d in vscode_user_dirs()
     for sub in (("workspaceStorage", "*", "chatSessions", "*.json*"), ("globalStorage", "emptyWindowChatSessions", "*.json*"))]


def parse(path, src, parser):
    st = os.stat(path)
    hit = _cache.get(path)
    if hit and hit[:2] == (st.st_mtime, st.st_size):
        return hit[2]
    # ponytail: re-reads the whole file when it changes; tail-only incremental read if transcripts get huge
    a = dict(path=path, src=src, id=agent_id(path), where="cli", cwd="?", title="", model="", tokens=0,
             ctx=0, cap=200_000, out=0, events=[], pending={}, edits={}, todos=[],
             prompt="", ended=False, mtime=st.st_mtime, sub="subagents" in path)
    with open(path, encoding="utf-8", errors="ignore") as f:
        parser(f, a)
    if a["model"].startswith(CTX_1M):
        a["cap"] = 1_000_000
    _cache[path] = (st.st_mtime, st.st_size, a)
    return a


def files():
    for src, pattern, parser in SOURCES:
        for p in glob.glob(os.path.expanduser(pattern), recursive=True):
            yield p, src, parser


SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")  # Claude Code writes <pid>.json per running process


def win_proc_start(pid):
    """Creation time of a running Windows process as FILETIME (what Claude Code stores as procStart), else None."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code, times = wintypes.DWORD(), [wintypes.FILETIME() for _ in range(4)]
        if not k.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return None
        if not k.GetProcessTimes(h, *(ctypes.byref(t) for t in times)):
            return None
        return str(times[0].dwHighDateTime << 32 | times[0].dwLowDateTime)
    finally:
        k.CloseHandle(h)


def alive(pid, proc_start):
    """True if pid is still the process that wrote the session file (guards against PID reuse)."""
    if sys.platform == "win32":  # never os.kill() on Windows: any signal there terminates the process
        return proc_start is not None and win_proc_start(pid) == str(proc_start)
    try:
        os.kill(pid, 0)  # signal 0 = existence check only
    except OSError:
        return False
    # ponytail: POSIX checks the binary name instead of matching procStart; compare start times if PID reuse bites
    comm = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True).stdout
    return "claude" in comm or "node" in comm


def running():
    """→ {session_id: pid} for Claude Code sessions whose process is alive right now."""
    out = {}
    for f in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                s = json.load(fh)
            pid = int(s["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if alive(pid, s.get("procStart")):
            out[s.get("sessionId")] = pid
    return out


def close(pid):
    """Terminate an agent process and its child shells. → (ok, message)."""
    if sys.platform == "win32":
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    try:
        os.kill(pid, signal.SIGTERM)
        return True, ""
    except OSError as e:
        return False, str(e)


def kill_agent(prefix, yes):
    hits = [h for h in files() if agent_id(h[0]).startswith(prefix)]
    if len(hits) != 1:
        sys.exit(f"{len(hits)} agents match {prefix!r}; give a longer id prefix" if hits else f"no agent matching {prefix!r}")
    a = parse(*hits[0])
    if a["src"] != "claude":
        sys.exit(f"{a['id'][:8]} is a {a['src']} agent; only Claude Code sessions record their process id, so close it in its terminal")
    if a["sub"]:
        sys.exit(f"{a['id'][:8]} is a subagent; it runs inside its parent session, so close the parent instead")
    pid = running().get(a["id"])
    if not pid:
        sys.exit(f"{a['id'][:8]} isn't running (no live Claude Code process for this session)")
    kind, detail = status(a, time.time())
    print(f"{c('1', a['id'][:8])}  {project(a)}  pid {pid}  {a['title']}\n"
          f"  {kind}: {clip(detail or doing_next(a)[0], 100)}\n  {fmt_n(a['tokens'])} tokens")
    if not yes and input("Close this agent? Unsaved work in progress stops mid-step. [y/N] ").strip().lower() != "y":
        return print("cancelled")
    if running().get(a["id"]) != pid:  # re-check right before killing
        sys.exit("process changed or exited; nothing closed")
    ok, msg = close(pid)
    if not ok:
        sys.exit(f"failed to close pid {pid}: {msg}")
    print(c("32", f"closed {a['id'][:8]} (pid {pid}).") + f" Its transcript is kept; resume it with:\n"
          f"  cd {a['cwd']} && claude --resume {a['id']}")


def status(a, now):
    """→ (kind, detail): needs (blocked on you), working, done (turn finished), idle."""
    age = now - a["mtime"]
    # ponytail: an unanswered call in a crashed session looks the same as a prompt; ignored after 1h
    if a["pending"] and age < 3600:
        for ev in a["pending"].values():
            if ev[3] in ASK_TOOLS:
                return "needs", "answer: " + ev[1].partition(": ")[2]
            if age > (SLOW_STUCK_SECS if ev[3] in SLOW_TOOLS else STUCK_SECS):
                return "needs", "approve? " + ev[1]
    if age < ACTIVE_SECS:
        return "working", ""
    return ("done" if a["ended"] else "idle"), ""


def errors(a):
    """→ (total failed tool calls, failures among the last 5 finished calls)."""
    done = [e[2] for e in a["events"] if e[2] is not None]
    return done.count(False), done[-5:].count(False)


def conflicts(rows, now):
    """→ {path: [agent, ...]} for files edited by 2+ agents within CONFLICT_SECS."""
    by_path = {}
    for a in rows:
        for path, ts in a["edits"].items():
            if now - ts < CONFLICT_SECS:
                by_path.setdefault(path, []).append(a)
    return {p: ags for p, ags in by_path.items() if len(ags) > 1}


def agents(hours, proj):
    cutoff = time.time() - hours * 3600
    out = []
    for p, src, parser in files():
        try:
            if os.path.getmtime(p) < cutoff:
                continue
            a = parse(p, src, parser)
        except OSError:
            continue  # file rotated/deleted mid-scan
        if proj.lower() in a["cwd"].lower() and (a["events"] or a["tokens"] or a["prompt"]):  # skip never-used chats
            out.append(a)
    return sorted(out, key=lambda a: -a["mtime"])


def doing_next(a):
    todo = lambda s: next((t for t in a["todos"] if t.get("status") == s), None)
    cur, nxt = todo("in_progress"), todo("pending")
    doing = (cur.get("activeForm") or cur.get("content")) if cur else (a["events"][-1][1] if a["events"] else a["prompt"])
    return doing or "", (nxt.get("content", "") if nxt else "")


def fmt_n(n):
    return f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.0f}k" if n >= 1e3 else str(n)


def ago(secs):
    secs = int(secs)
    return f"{secs}s" if secs < 60 else f"{secs//60}m" if secs < 3600 else f"{secs//3600}h" if secs < 172800 else f"{secs//86400}d"


def clip(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def short_path(p, parts=3):
    return os.sep.join(p.split(os.sep)[-parts:])


def bar(n, cap, w=6):
    frac = min(1, n / cap)
    fill = round(frac * w)
    return c("32" if frac < .5 else "33" if frac < .8 else "31", "▰" * fill) + c("90", "▱" * (w - fill))


def action(s, n, dim=False):
    """Clip to n chars, pad, and color the leading tool name (`Bash: ls` → cyan Bash)."""
    s = clip(s, n).ljust(n)
    head, sep, rest = s.partition(": ")
    if sep and " " not in head:
        return c(TOOL_COLOR.get(head, "37"), head) + sep + (c("90", rest) if dim else rest)
    return c("90", s) if dim else s


def snapshot(hours, proj):
    rows, now = agents(hours, proj), time.time()
    st = {a["path"]: status(a, now) for a in rows}
    rows.sort(key=lambda a: st[a["path"]][0] != "needs")  # stable: blocked agents first, then most recent
    return rows, st, conflicts(rows, now), running(), now


def as_json(hours, proj):
    rows, st, clash, pids, now = snapshot(hours, proj)
    clashing = {a["path"] for ags in clash.values() for a in ags}
    out = []
    for a in rows:
        doing, nxt = doing_next(a)
        n_err, recent = errors(a)
        out.append(dict(
            id=a["id"], src=a["src"], where=a["where"], sub=a["sub"], project=project(a), cwd=a["cwd"], title=a["title"],
            model=a["model"], status=st[a["path"]][0], detail=clip(st[a["path"]][1], 300), doing=clip(doing, 300),
            next=clip(nxt, 300), prompt=clip(a["prompt"], 500),
            ctx=a["ctx"], cap=a["cap"], tokens=a["tokens"], out=a["out"],
            errors=n_err, recent_errors=recent,
            idle_secs=int(now - a["mtime"]), pid=pids.get(a["id"]), conflict=a["path"] in clashing,
            events=[dict(ts=ts, action=clip(ev, 300), ok=ok) for ts, ev, ok, _ in a["events"][-30:]],
            todos=[dict(content=clip(t.get("content", ""), 300), status=t.get("status")) for t in a["todos"]]))
    print(json.dumps({"agents": out, "conflicts": [{"path": p, "agents": [a["id"] for a in ags]} for p, ags in clash.items()]}),
          flush=True)


def table(hours, proj):
    rows, st, clash, pids, now = snapshot(hours, proj)
    clashing = {a["path"] for ags in clash.values() for a in ags}
    w = os.get_terminal_size().columns if sys.stdout.isatty() else 200
    flex = max(20, (w - 107) // 2)
    live = sum(s[0] == "working" for s in st.values())
    needs = sum(s[0] == "needs" for s in st.values())
    title = (f" ◆ AGENT TRACKER   {len(rows)} agents · {live} working · {needs} need you · {len(clash)} conflicts · "
             f"{fmt_n(sum(a['tokens'] for a in rows))} tokens")
    clock = f"{datetime.now():%H:%M:%S}  ctrl+c to quit "
    out = [c("1;97;41" if needs or clash else "1;97;44", title + " " * max(1, w - len(title) - len(clock)) + clock), ""]
    out.append(c("1;90", f"   {'AGENT':13}{'WHERE':15}{'PROJECT':20}{'MODEL':14}{'CONTEXT':13}{'TOTAL':>7}{'ERR':>4}"
                         f"{'AGO':>5}{'PID':>7}  {'DOING':{flex}}  NEXT"))
    icons = {"needs": c("1;91", "⚠"), "working": c("1;92", "●"), "done": c("36", "✓"), "idle": c("90", "○")}
    for a in rows:
        kind, detail = st[a["path"]]
        on = kind in ("needs", "working")
        doing, nxt = doing_next(a)
        proj_name = project(a)
        name =("↳ " if a["sub"] else "") + a["id"][:8] + (" ⚡" if a["path"] in clashing else "")
        n_err, recent = errors(a)
        err = c("1;91", f"{n_err:>4}") if recent >= 3 else c("33", f"{n_err:>4}") if n_err else c("90", f"{'·':>4}")
        out.append(
            " " + icons[kind] + " "
            + c("1;97" if on else "37", f"{name:13}")
            + c(SRC_COLOR.get(a["src"], "37"), f"{a['src']:8}") + c("90", f"{clip(a['where'], 6):7}")
            + c(PALETTE[sum(map(ord, proj_name)) % len(PALETTE)], f"{clip(proj_name, 19):20}")
            + c("90", f"{clip(a['model'].replace('claude-', ''), 13):14}")
            + bar(a["ctx"], a["cap"]) + f" {fmt_n(a['ctx']):>5} "
            + c("1", f"{fmt_n(a['tokens']):>7}") + err
            + c("92" if on else "90", f"{ago(now - a['mtime']):>5}")
            + (c("37", f"{pids[a['id']]:>7}") if a["id"] in pids else c("90", f"{'·':>7}")) + "  "
            + (c("1;91", clip(detail, flex).ljust(flex)) if kind == "needs" else action(doing, flex, dim=not on))
            + "  " + c("95", "→ " + clip(nxt, flex - 2) if nxt else ""))
    if not rows:
        out.append(c("90", f"   no agents in the last {hours:g}h" + (f" matching {proj!r}" if proj else "")))
    for path, ags in sorted(clash.items())[:8]:
        who = ", ".join(f"{a['id'][:8]} ({ago(now - a['edits'][path])} ago)" for a in ags)
        out.append(c("1;93", f"   ⚡ {short_path(path)}") + c("90", "  edited by ") + who)
    print("\n".join(out))
    return rows, st, clash


def project(a):
    return os.path.basename(a["cwd"].rstrip("/\\")) or a["cwd"]


def alerts(rows, st, clash, prev_st, prev_clash):
    """→ [(title, body)] for state changes since the last frame: blocked on you, finished a turn, new file conflict."""
    out = []
    for a in rows:
        kind, detail = st[a["path"]]
        was = prev_st.get(a["path"], ("",))[0]
        if kind == "needs" and was != "needs":
            out.append((f"⚠ {project(a)}: agent needs you", f"{a['id'][:8]}  {detail}"))
        elif kind == "done" and was == "working":
            out.append((f"✓ {project(a)}: agent finished", f"{a['id'][:8]}  {a['title'] or doing_next(a)[0]}"))
    for path in clash.keys() - prev_clash.keys():
        out.append(("⚡ File conflict", f"{short_path(path)} edited by " + ", ".join(a["id"][:8] for a in clash[path])))
    return out


# title/body arrive via env vars, never spliced into the script, so transcript text can't inject PowerShell
WIN_TOAST = """
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName('text')
$null = $n.Item(0).AppendChild($t.CreateTextNode($env:AT_TITLE))
$null = $n.Item(1).AppendChild($t.CreateTextNode($env:AT_BODY))
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def notify(title, body):
    """Fire-and-forget desktop notification: Windows toast, macOS Notification Center, Linux notify-send."""
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if sys.platform == "win32":
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", WIN_TOAST],
                             env=dict(os.environ, AT_TITLE=title, AT_BODY=body), creationflags=0x08000000, **quiet)  # no console window
        elif sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", "on run a", "-e", "display notification (item 2 of a) with title (item 1 of a)",
                              "-e", "end run", title, body], **quiet)
        else:
            subprocess.Popen(["notify-send", "-a", "agent-tracker", title, body], **quiet)
    except OSError:
        pass  # no notifier installed; the dashboard still shows everything


def timeline(prefix, n=40):
    hit = next(((p, s, f) for p, s, f in files() if agent_id(p).startswith(prefix)), None)
    if not hit:
        sys.exit(f"no agent matching {prefix!r}")
    a, now = parse(*hit), time.time()
    kind, detail = status(a, now)
    n_err, _ = errors(a)
    pid = running().get(a["id"])
    print(c("1;97;44", f" ◆ {a['id']} ") + "  " + c(SRC_COLOR.get(a["src"], "37"), a["src"]) + "  " + c("1", a["title"] or "(untitled)"))
    state = {"needs": c("1;91", "⚠ needs you: " + detail), "working": c("1;92", "● working"),
             "done": c("36", "✓ done " + ago(now - a["mtime"])), "idle": c("90", "○ idle " + ago(now - a["mtime"]))}[kind]
    print(f"   {c('36', a['cwd'])}  {c('90', a['model'])}  context {bar(a['ctx'], a['cap'])} {fmt_n(a['ctx'])}  "
          f"total {c('1', fmt_n(a['tokens']))}  {c('91', str(n_err) + ' errors') if n_err else ''}  {state}"
          f"{'  pid ' + str(pid) if pid else ''}\n")
    for ts, ev, ok, _ in a["events"][-n:]:
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M:%S")
        except ValueError:
            pass
        mark = c("91", "✗") if ok is False else c("33", "…") if ok is None else c("90", "·")
        print(f"   {c('90', ts)} {mark} {action(ev, 110)}")
    if a["todos"]:
        print(c("1", "\n   TODOS"))
        for t in a["todos"]:
            s = t.get("status")
            mark = {"completed": c("32", "✔"), "in_progress": c("1;93", "▶")}.get(s, c("90", "○"))
            print(f"   {mark} {c('90', t.get('content', '')) if s == 'completed' else t.get('content', '')}")


def main():
    ap = argparse.ArgumentParser(prog="agents", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("agent", nargs="?", help="session id prefix → show its timeline")
    ap.add_argument("-p", "--project", default="", help="filter by project path substring")
    ap.add_argument("--here", action="store_true", help="only agents in the current directory")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("-n", type=float, default=2, help="refresh seconds")
    ap.add_argument("--no-notify", action="store_true", help="no desktop notifications in live mode")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    ap.add_argument("--kill", metavar="ID", help="close a running Claude Code agent (asks first)")
    ap.add_argument("-y", "--yes", action="store_true", help="with --kill: don't ask for confirmation")
    ap.add_argument("--json", action="store_true", help="print one JSON snapshot and exit")
    ap.add_argument("--stream", action="store_true", help="with --json: print a snapshot line every -n seconds (parse cache stays warm)")
    args = ap.parse_args()
    if args.test_notify:
        return notify("⚠ agent-tracker: test", "Notifications work. You'll get one like this when an agent needs you.")
    if args.here:
        args.project = os.getcwd()
    sys.stdout.reconfigure(encoding="utf-8")
    os.system("")  # enables ANSI escapes on Windows consoles
    if args.json:
        while True:
            as_json(args.hours, args.project)
            if not args.stream:
                break
            time.sleep(args.n)
    elif args.kill:
        kill_agent(args.kill, args.yes)
    elif args.agent:
        timeline(args.agent)
    elif args.once:
        table(args.hours, args.project)
    else:
        print("\033[?25l", end="")  # hide cursor
        prev = None  # first frame only sets the baseline, so starting the tracker doesn't replay old states
        try:
            while True:
                print("\033[H\033[J", end="")
                rows, st, clash = table(args.hours, args.project)
                if prev and not args.no_notify:
                    for title, body in alerts(rows, st, clash, *prev):
                        notify(title, body)
                prev = st, clash
                time.sleep(args.n)
        except KeyboardInterrupt:
            pass
        finally:
            print("\033[?25h", end="")


if __name__ == "__main__":
    main()
