"""Agent tracker: live view of every coding agent (Claude Code, Codex, ...) read from their local transcripts.

  agents                 # live table of agents active in last 24h, refreshes every 2s
  agents --once          # print once and exit
  agents -p erp          # only projects whose path contains "erp"
  agents --here          # only agents running in the current directory
  agents --hours 2       # change the time window
  agents 5aeb0dda        # timeline for one agent (session id prefix)
"""
import argparse, glob, json, os, sys, time
from datetime import datetime

ACTIVE_SECS = 90  # transcript written within this window = agent is working
_cache = {}  # path -> (mtime, size, parsed)

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
PALETTE = ["36", "35", "33", "32", "34", "96", "95", "93"]
TOOL_COLOR = {"Bash": "36", "PowerShell": "36", "shell": "36", "shell_command": "36", "Edit": "33", "Write": "33",
              "NotebookEdit": "33", "apply_patch": "33", "Read": "34", "Grep": "34", "Glob": "34", "Agent": "35",
              "Task": "35", "WebFetch": "32", "WebSearch": "32", "web_search": "32"}
SRC_COLOR = {"claude": "38;5;209", "codex": "38;5;75"}


def c(code, s):
    return f"\033[{code}m{s}\033[0m" if COLOR else s


def summarize(name, inp):
    for k in ("command", "file_path", "path", "pattern", "description", "url", "query", "prompt"):
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
        if t == "user" and isinstance(m.get("content"), str) and not d.get("isMeta"):
            a["prompt"] = m["content"]
        if t != "assistant":
            continue
        a["model"] = m.get("model") or a["model"]
        u = m.get("usage")
        # one API message is split across several records that repeat the same usage; count it once
        if u and m.get("id") not in seen:
            seen.add(m.get("id"))
            ctx = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
            a["ctx"] = ctx + u.get("output_tokens", 0)
            a["out"] += u.get("output_tokens", 0)
            a["tokens"] += ctx + u.get("output_tokens", 0)
        for b in m.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                inp = b.get("input") or {}
                if b["name"] == "TodoWrite" and isinstance(inp.get("todos"), list):
                    a["todos"] = inp["todos"]
                a["events"].append((d.get("timestamp", ""), summarize(b["name"], inp)))


def parse_codex(f, a):
    seen = set()
    for d in jsonl(f):
        p = d.get("payload")
        if not isinstance(p, dict):
            continue
        a["cwd"] = p.get("cwd") or a["cwd"]
        if d.get("type") == "turn_context":
            a["model"] = p.get("model") or a["model"]
        pt = p.get("type")
        if pt == "token_count" and isinstance(p.get("info"), dict):
            tot, last = p["info"].get("total_token_usage") or {}, p["info"].get("last_token_usage") or {}
            a["tokens"], a["out"] = tot.get("total_tokens", a["tokens"]), tot.get("output_tokens", a["out"])
            a["ctx"] = last.get("total_tokens", a["ctx"])
            a["cap"] = p["info"].get("model_context_window") or a["cap"]
        elif pt == "user_message" and p.get("message"):
            a["prompt"] = p["message"]
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
                inp = {"command": str(p.get("input", "")).replace("*** Begin Patch", "")}
            else:
                inp = {"query": (p.get("action") or {}).get("query", "")}
            inp = inp if isinstance(inp, dict) else {}
            if name == "update_plan" and isinstance(inp.get("plan"), list):
                a["todos"] = [{"content": s.get("step", ""), "status": s.get("status")} for s in inp["plan"] if isinstance(s, dict)]
            a["events"].append((d.get("timestamp", ""), summarize(name, inp)))


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
    # ponytail: context bar defaults to a 200k window; per-model sizes if 1M-context Claude models matter
    a = dict(path=path, src=src, id=os.path.basename(path)[:-6][-36:], cwd="?", title="", model="", tokens=0,
             ctx=0, cap=200_000, out=0, events=[], todos=[], prompt="", mtime=st.st_mtime, sub="subagents" in path)
    with open(path, encoding="utf-8", errors="ignore") as f:
        parser(f, a)
    _cache[path] = (st.st_mtime, st.st_size, a)
    return a


def files():
    for src, pattern, parser in SOURCES:
        for p in glob.glob(os.path.expanduser(pattern), recursive=True):
            yield p, src, parser


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


def ago(secs):
    secs = int(secs)
    return f"{secs}s" if secs < 60 else f"{secs//60}m" if secs < 3600 else f"{secs//3600}h" if secs < 172800 else f"{secs//86400}d"


def clip(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


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
    w = os.get_terminal_size().columns if sys.stdout.isatty() else 200
    flex = max(20, (w - 92) // 2)
    live = sum(now - a["mtime"] < ACTIVE_SECS for a in rows)
    title = f" ◆ AGENT TRACKER   {len(rows)} agents · {live} active · {fmt_n(sum(a['tokens'] for a in rows))} tokens"
    clock = f"{datetime.now():%H:%M:%S}  ctrl+c to quit "
    out = [c("1;97;44", title + " " * max(1, w - len(title) - len(clock)) + clock), ""]
    out.append(c("1;90", f"   {'AGENT':12}{'CLI':7}{'PROJECT':20}{'MODEL':14}{'CONTEXT':13}{'OUT':>6}{'TOTAL':>7}{'AGO':>5}  "
                         f"{'DOING':{flex}}  NEXT"))
    for a in rows:
        on = now - a["mtime"] < ACTIVE_SECS
        doing, nxt = doing_next(a)
        proj_name = os.path.basename(a["cwd"].rstrip("/\\")) or a["cwd"]
        name = ("↳ " if a["sub"] else "") + a["id"][:8]
        out.append(
            " " + (c("1;92", "●") if on else c("90", "○")) + " "
            + c("1;97" if on else "37", f"{name:12}")
            + c(SRC_COLOR.get(a["src"], "37"), f"{a['src']:7}")
            + c(PALETTE[sum(map(ord, proj_name)) % len(PALETTE)], f"{clip(proj_name, 19):20}")
            + c("90", f"{clip(a['model'].replace('claude-', ''), 13):14}")
            + bar(a["ctx"], a["cap"]) + f" {fmt_n(a['ctx']):>5} "
            + f"{fmt_n(a['out']):>6}" + c("1", f"{fmt_n(a['tokens']):>7}")
            + c("92" if on else "90", f"{ago(now - a['mtime']):>5}") + "  "
            + action(doing, flex, dim=not on) + "  " + c("95", "→ " + clip(nxt, flex - 2) if nxt else ""))
    if not rows:
        out.append(c("90", f"   no agents in the last {hours:g}h" + (f" matching {proj!r}" if proj else "")))
    print("\n".join(out))


def timeline(prefix, n=40):
    hit = next(((p, s, f) for p, s, f in files() if os.path.basename(p)[:-6][-36:].startswith(prefix)), None)
    if not hit:
        sys.exit(f"no agent matching {prefix!r}")
    a = parse(*hit)
    on = time.time() - a["mtime"] < ACTIVE_SECS
    print(c("1;97;44", f" ◆ {a['id']} ") + "  " + c(SRC_COLOR.get(a["src"], "37"), a["src"]) + "  " + c("1", a["title"] or "(untitled)"))
    print(f"   {c('36', a['cwd'])}  {c('90', a['model'])}  context {bar(a['ctx'], a['cap'])} {fmt_n(a['ctx'])}  "
          f"total {c('1', fmt_n(a['tokens']))}  {c('1;92', '● active') if on else c('90', '○ idle ' + ago(time.time() - a['mtime']))}\n")
    for ts, ev in a["events"][-n:]:
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M:%S")
        except ValueError:
            pass
        print(f"   {c('90', ts)}  {action(ev, 110)}")
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
    args = ap.parse_args()
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
        try:
            while True:
                print("\033[H\033[J", end="")
                table(args.hours, args.project)
                time.sleep(args.n)
        except KeyboardInterrupt:
            pass
        finally:
            print("\033[?25h", end="")


if __name__ == "__main__":
    main()
