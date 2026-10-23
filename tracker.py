"""Agent tracker: live view of every coding agent (Claude Code, Codex, ...) read from their local transcripts.

  agents                 # live table of agents active in last 24h, refreshes every 2s
  agents --once          # print once and exit
  agents -p erp          # only projects whose path contains "erp"
  agents --here          # only agents running in the current directory
  agents --hours 2       # change the time window
  agents 5aeb0dda        # timeline for one agent (session id prefix)
"""
import argparse, glob, json, os, re, subprocess, sys, time
from datetime import datetime

ACTIVE_SECS = 90  # transcript written within this window = agent is working
STUCK_SECS = 20  # a quick tool (Edit, Read...) unanswered this long = waiting on a permission prompt
SLOW_STUCK_SECS = 90  # same for tools that can legitimately run long (shell, subagents, web)
SLOW_TOOLS = {"Bash", "PowerShell", "Agent", "Task", "WebFetch", "WebSearch", "Monitor", "shell", "shell_command"}
ASK_TOOLS = {"AskUserQuestion", "ExitPlanMode", "request_user_input"}
CONFLICT_SECS = 3600  # two agents editing the same file within this window = conflict
_cache = {}  # path -> (mtime, size, parsed)
# models with a 1M context window; everything else (and Codex, which reports its own) uses 200k
CTX_1M = ("claude-fable", "claude-mythos", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
          "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-5")

# $ per 1M tokens: input, output, cache read (cache writes bill at 1.25x input for 5m, 2x for 1h).
# Longest matching prefix wins. Override or extend in ~/.agent-tracker/prices.json: {"<model prefix>": [input, output, cache_read]}
PRICES = {
    "claude-fable-5-1": (10, 50, 0.25), "claude-mythos-5-1": (10, 50, 0.25),
    "claude-fable-5": (10, 50, 1.0), "claude-mythos-5": (10, 50, 1.0),
    "claude-opus-5-5": (4, 20, 0.20), "claude-opus-5": (5, 25, 0.50), "claude-opus-4-8": (5, 25, 0.50),
    "claude-opus-4-7": (5, 25, 0.50), "claude-opus-4-6": (5, 25, 0.50),
    "claude-sonnet-5": (2, 10, 0.20), "claude-sonnet-4-6": (3, 15, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4-5": (1, 5, 0.10),
    "gpt-5": (1.25, 10, 0.125),  # GPT-5 list price, used for every gpt-5.x without its own row
}
PRICES_FILE = os.path.expanduser("~/.agent-tracker/prices.json")
try:
    with open(PRICES_FILE, encoding="utf-8") as f:
        PRICES.update({k: tuple(v) for k, v in json.load(f).items() if isinstance(v, list) and len(v) == 3})
except (OSError, ValueError, AttributeError):
    pass  # no file / bad JSON → built-in prices

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
PALETTE = ["36", "35", "33", "32", "34", "96", "95", "93"]
TOOL_COLOR = {"Bash": "36", "PowerShell": "36", "shell": "36", "shell_command": "36", "Edit": "33", "Write": "33",
              "NotebookEdit": "33", "apply_patch": "33", "Read": "34", "Grep": "34", "Glob": "34", "Agent": "35",
              "Task": "35", "WebFetch": "32", "WebSearch": "32", "web_search": "32"}
SRC_COLOR = {"claude": "38;5;209", "codex": "38;5;75"}


def c(code, s):
    return f"\033[{code}m{s}\033[0m" if COLOR else s


def price(model):
    hits = [k for k in PRICES if model.startswith(k)]
    return PRICES[max(hits, key=len)] if hits else None


def claude_cost(u, model):
    """$ for one API response, priced like Claude Code's own /cost: 5m vs 1h cache writes, fast mode, web search."""
    p = price(model)
    if not p:
        return None
    inp, cw, cr, out = (u.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
    w1h = (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0)
    rate = (2 if u.get("speed") == "fast" else 1) * (5 if model.startswith("claude-haiku-5-5") and inp + cw + cr > 100_000 else 1)
    searches = (u.get("server_tool_use") or {}).get("web_search_requests", 0)
    return rate * (inp * p[0] + (cw - w1h) * p[0] * 1.25 + w1h * p[0] * 2 + cr * p[2] + out * p[1]) / 1e6 + searches * 0.01


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
        if isinstance(v, str):
            return f"{name}: {v}"
    return name


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
            cost = claude_cost(u, m.get("model") or "")
            if cost is not None:
                a["cost"] += cost
            elif inp + cw + cr + out:
                a["unpriced"] = True
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
    seen, tot = set(), {}
    for d in jsonl(f):
        p = d.get("payload")
        if not isinstance(p, dict):
            continue
        a["cwd"] = p.get("cwd") or a["cwd"]
        if d.get("type") == "turn_context":
            a["model"] = p.get("model") or a["model"]
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
    p = price(a["model"])
    if p and tot:
        cached = tot.get("cached_input_tokens", 0)
        a["cost"] = ((tot.get("input_tokens", 0) - cached) * p[0] + cached * p[2] + tot.get("output_tokens", 0) * p[1]) / 1e6
    elif a["tokens"]:
        a["unpriced"] = True


# add another agent CLI: a name, a glob for its transcripts, and a parser that fills the agent dict
SOURCES = [
    ("claude", "~/.claude/projects/**/*.jsonl", parse_claude),
    ("codex", "~/.codex/sessions/**/rollout-*.jsonl", parse_codex),
]


def parse(path, src, parser):
    st = os.stat(path)
    hit = _cache.get(path)
    if hit and hit[:2] == (st.st_mtime, st.st_size):
        return hit[2]
    # ponytail: re-reads the whole file when it changes; tail-only incremental read if transcripts get huge
    a = dict(path=path, src=src, id=os.path.basename(path)[:-6][-36:], cwd="?", title="", model="", tokens=0,
             ctx=0, cap=200_000, out=0, cost=0.0, unpriced=False, events=[], pending={}, edits={}, todos=[],
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
        if proj.lower() in a["cwd"].lower():
            out.append(a)
    return sorted(out, key=lambda a: -a["mtime"])


def doing_next(a):
    todo = lambda s: next((t for t in a["todos"] if t.get("status") == s), None)
    cur, nxt = todo("in_progress"), todo("pending")
    doing = (cur.get("activeForm") or cur.get("content")) if cur else (a["events"][-1][1] if a["events"] else a["prompt"])
    return doing or "", (nxt.get("content", "") if nxt else "")


def fmt_n(n):
    return f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.0f}k" if n >= 1e3 else str(n)


def fmt_cost(a):
    if a["unpriced"] and not a["cost"]:
        return "–"
    return f"${a['cost']:.2f}" if a["cost"] < 100 else f"${a['cost']:.0f}"


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


def table(hours, proj):
    rows, now = agents(hours, proj), time.time()
    st = {a["path"]: status(a, now) for a in rows}
    rows.sort(key=lambda a: st[a["path"]][0] != "needs")  # stable: blocked agents first, then most recent
    clash = conflicts(rows, now)
    clashing = {a["path"] for ags in clash.values() for a in ags}
    w = os.get_terminal_size().columns if sys.stdout.isatty() else 200
    flex = max(20, (w - 100) // 2)
    live = sum(s[0] == "working" for s in st.values())
    needs = sum(s[0] == "needs" for s in st.values())
    title = (f" ◆ AGENT TRACKER   {len(rows)} agents · {live} working · {needs} need you · {len(clash)} conflicts · "
             f"{fmt_n(sum(a['tokens'] for a in rows))} tokens · ${sum(a['cost'] for a in rows):.2f}")
    clock = f"{datetime.now():%H:%M:%S}  ctrl+c to quit "
    out = [c("1;97;41" if needs or clash else "1;97;44", title + " " * max(1, w - len(title) - len(clock)) + clock), ""]
    out.append(c("1;90", f"   {'AGENT':13}{'CLI':7}{'PROJECT':20}{'MODEL':14}{'CONTEXT':13}{'TOTAL':>7}{'COST':>8}{'ERR':>4}"
                         f"{'AGO':>5}  {'DOING':{flex}}  NEXT"))
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
            + c(SRC_COLOR.get(a["src"], "37"), f"{a['src']:7}")
            + c(PALETTE[sum(map(ord, proj_name)) % len(PALETTE)], f"{clip(proj_name, 19):20}")
            + c("90", f"{clip(a['model'].replace('claude-', ''), 13):14}")
            + bar(a["ctx"], a["cap"]) + f" {fmt_n(a['ctx']):>5} "
            + c("1", f"{fmt_n(a['tokens']):>7}") + f"{fmt_cost(a):>8}" + err
            + c("92" if on else "90", f"{ago(now - a['mtime']):>5}") + "  "
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
    hit = next(((p, s, f) for p, s, f in files() if os.path.basename(p)[:-6][-36:].startswith(prefix)), None)
    if not hit:
        sys.exit(f"no agent matching {prefix!r}")
    a, now = parse(*hit), time.time()
    kind, detail = status(a, now)
    n_err, _ = errors(a)
    print(c("1;97;44", f" ◆ {a['id']} ") + "  " + c(SRC_COLOR.get(a["src"], "37"), a["src"]) + "  " + c("1", a["title"] or "(untitled)"))
    state = {"needs": c("1;91", "⚠ needs you: " + detail), "working": c("1;92", "● working"),
             "done": c("36", "✓ done " + ago(now - a["mtime"])), "idle": c("90", "○ idle " + ago(now - a["mtime"]))}[kind]
    print(f"   {c('36', a['cwd'])}  {c('90', a['model'])}  context {bar(a['ctx'], a['cap'])} {fmt_n(a['ctx'])}  "
          f"total {c('1', fmt_n(a['tokens']))}  {fmt_cost(a)}  {c('91', str(n_err) + ' errors') if n_err else ''}  {state}\n")
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
    args = ap.parse_args()
    if args.test_notify:
        return notify("⚠ agent-tracker: test", "Notifications work. You'll get one like this when an agent needs you.")
    if args.here:
        args.project = os.getcwd()
    sys.stdout.reconfigure(encoding="utf-8")
    os.system("")  # enables ANSI escapes on Windows consoles
    if args.agent:
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
