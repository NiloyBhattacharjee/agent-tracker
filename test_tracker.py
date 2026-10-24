import json, os, pathlib, shutil, subprocess, sys, tempfile
import tracker


def run(lines, parser):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write("\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines))
    a = tracker.parse(f.name, "x", parser)
    os.unlink(f.name)
    return a


def call(i, name, inp, ts="2026-10-23T10:00:00Z"):
    return {"type": "assistant", "cwd": "/x/proj", "timestamp": ts, "message": {"id": f"m{i}", "model": "claude-opus-5-5",
            "stop_reason": "tool_use", "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}}


def result(i, err=False):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "is_error": err}]}}


# claude: same message split over two records counts tokens once
u = {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5}
a = run([
    {"type": "assistant", "cwd": "/x/proj", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": u,
     "content": [{"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {"todos": [
         {"content": "a", "status": "in_progress", "activeForm": "Doing a"}, {"content": "b", "status": "pending"}]}}]}},
    {"type": "assistant", "cwd": "/x/proj", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": u,
     "content": [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls"}}]}},
    result(1), result(2),
    "not json",
], tracker.parse_claude)
assert a["tokens"] == 105 and a["out"] == 5 and a["ctx"] == 105 and a["cap"] == 1_000_000, a
assert a["compactions"] == 0
assert run([{"type": "system", "subtype": "compact_boundary", "compactMetadata": {"preTokens": 167000}}] * 2,
           tracker.parse_claude)["compactions"] == 2
assert a["events"][-1][1] == "Bash: ls"
assert tracker.doing_next(a) == ("Doing a", "b")
assert tracker.status(a, a["mtime"] + 30) == ("working", "")

# needs-you: a pending question is immediate; a pending Edit only after STUCK_SECS; a pending Bash waits longer
a = run([call(1, "AskUserQuestion", {"questions": [{"question": "Which DB?"}]})], tracker.parse_claude)
assert tracker.status(a, a["mtime"]) == ("needs", "answer: Which DB?")
a = run([call(1, "Edit", {"file_path": "/x/proj/app.py"})], tracker.parse_claude)
assert tracker.status(a, a["mtime"] + 5)[0] == "working"
assert tracker.status(a, a["mtime"] + 30) == ("needs", "approve? Edit: /x/proj/app.py")
a = run([call(1, "Bash", {"command": "pytest"})], tracker.parse_claude)
assert tracker.status(a, a["mtime"] + 30)[0] == "working"
assert tracker.status(a, a["mtime"] + 120)[0] == "needs"

# errors: 3 of the last 5 calls failed → looping
a = run([x for i in range(5) for x in (call(i, "Bash", {"command": "npm test"}), result(i, err=i >= 2))], tracker.parse_claude)
assert tracker.errors(a) == (3, 3)
assert tracker.status(a, a["mtime"] + 200)[0] == "idle"

# conflicts: two agents edited the same file recently
now = tracker.epoch("2026-10-23T10:30:00Z")
b1 = run([call(1, "Edit", {"file_path": "/x/proj/app.py"})], tracker.parse_claude)
b2 = run([call(1, "Write", {"file_path": "/x/proj/app.py"}), call(2, "Edit", {"file_path": "/x/proj/other.py"})], tracker.parse_claude)
clash = tracker.conflicts([b1, b2], now)
assert list(clash) == [os.path.normcase(os.path.normpath("/x/proj/app.py"))] and len(clash[list(clash)[0]]) == 2
assert tracker.conflicts([b1, b2], now + 2 * tracker.CONFLICT_SECS) == {}

# codex: cumulative token_count, update_plan → todos, argv shell, exit codes, apply_patch edits
a = run([
    {"type": "session_meta", "payload": {"id": "s1", "cwd": "/proj"}},
    {"type": "turn_context", "payload": {"cwd": "/proj", "model": "gpt-5.3-codex"}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "update_plan", "call_id": "c1",
     "arguments": json.dumps({"plan": [{"step": "fix bug", "status": "completed"}, {"step": "write test", "status": "pending"}]})}},
    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "Plan updated"}},
    {"type": "response_item", "timestamp": "2026-10-23T10:00:00Z", "payload": {"type": "custom_tool_call", "name": "apply_patch",
     "call_id": "c2", "input": "*** Begin Patch\n*** Update File: src/app.py\n@@\n-a\n+b\n*** End Patch"}},
    {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c2", "output": '{"output":"ok","metadata":{"exit_code":0}}'}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "c3", "arguments": '{"command":["git","status"]}'}},
    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c3", "output": "Exit code: 128\nfatal"}},
    {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 450,
     "cached_input_tokens": 50, "output_tokens": 50, "total_tokens": 500}, "last_token_usage": {"total_tokens": 300},
     "model_context_window": 258400}}},
    {"type": "compacted", "payload": {"message": ""}},
    {"type": "event_msg", "timestamp": "2026-10-23T10:00:00Z", "payload": {"type": "token_count", "info": None, "rate_limits": {
     "primary": {"used_percent": 85.0, "window_minutes": 300, "resets_at": 1793000000},
     "secondary": {"used_percent": 4.0, "window_minutes": 10080, "resets_at": 1761000000}}}},
    {"type": "event_msg", "payload": {"type": "task_complete"}},
], tracker.parse_codex)
assert (a["model"], a["tokens"], a["out"], a["ctx"], a["cap"]) == ("gpt-5.3-codex", 500, 50, 300, 258400), a
assert a["compactions"] == 1
assert tracker.left(a) == 258100  # Codex counts to its full window
assert tracker.left(dict(src="claude", ctx=150_000, cap=200_000)) == 17_000  # Claude compacts 33k early
# plan limits: newest snapshot wins; the weekly window already reset by `now`, so it's dropped
assert tracker.codex_limits([a], 1770000000) == [("5h", 85.0, 1793000000)], tracker.codex_limits([a], 1770000000)
assert tracker.codex_limits([], 0) == []
assert tracker.doing_next(a) == ("shell: git status", "write test")
assert tracker.errors(a) == (1, 1)
assert list(a["edits"]) == [os.path.normcase(os.path.normpath("/proj/src/app.py"))]
assert tracker.status(a, a["mtime"] + 200)[0] == "done"
# alerts fire only on transitions: into needs, working → done, new conflicts
x = run([call(1, "Edit", {"file_path": "/x/proj/app.py"})], tracker.parse_claude)
y = run([call(1, "Write", {"file_path": "/x/proj/app.py"})], tracker.parse_claude)
st = {x["path"]: ("needs", "approve? Edit: app.py"), y["path"]: ("done", "")}
clash = {"app.py": [x, y]}
got = tracker.alerts(([x, y], st, clash, []), ([x, y], {x["path"]: ("working", ""), y["path"]: ("working", "")}, {}, []))
assert [t for t, _ in got] == ["⚠ proj: agent needs you", "✓ proj: agent finished", "⚡ File conflict"], got
assert tracker.alerts(([x, y], st, clash, []), ([x, y], st, clash, [])) == []  # nothing changed → no alerts
# context crossing 80% and a Codex window crossing 80% alert once
x_full = dict(x, ctx=170_000, cap=200_000)
limits = [("5h", 85.0, None)]
got = tracker.alerts(([x_full], st, {}, limits), ([x], st, {}, [("5h", 60.0, None)]))
assert [t for t, _ in got] == ["◔ proj: context 85% full", "⏳ Codex 5h limit 85% used"], got
assert tracker.alerts(([x_full], st, {}, limits), ([x_full], st, {}, limits)) == []

# live-mode input: arrows (VT and Windows console), SGR mouse press/release/wheel, Enter/Esc/letters
assert tracker.keys("\x1b[A\x1b[B\xe0H\xe0P\r\x1bK") == [("up", 0), ("down", 0), ("up", 0), ("down", 0), ("enter", 0), ("esc", 0), ("k", 0)]
assert tracker.keys("\x1b[<0;12;7M\x1b[<0;12;7m\x1b[<64;1;1M\x1b[<65;1;1M") == [("click", 7), ("up", 0), ("down", 0)]

# copilot: replay the .jsonl mutation log, folder from workspace.json, terminal exit codes, todo list
ws = tempfile.mkdtemp()
os.makedirs(os.path.join(ws, "h", "chatSessions"))
proj_dir = os.path.join(ws, "my proj")
with open(os.path.join(ws, "h", "workspace.json"), "w") as f:
    json.dump({"folder": pathlib.Path(proj_dir).as_uri()}, f)
session = os.path.join(ws, "h", "chatSessions", "aaaaaaaa-1111-2222-3333-444444444444.jsonl")
term = lambda cmd, code: {"kind": "toolInvocationSerialized", "toolId": "run_in_terminal", "toolCallId": cmd, "isComplete": True,
                          "toolSpecificData": {"kind": "terminal", "commandLine": {"original": cmd}, "terminalCommandState": {"exitCode": code}}}
todo = {"kind": "toolInvocationSerialized", "toolId": "manage_todo_list", "toolCallId": "td", "isComplete": True, "invocationMessage": "",
        "toolSpecificData": {"kind": "todoList", "todoList": [{"title": "fix", "status": "completed"}, {"title": "test", "status": "not-started"}]}}
with open(session, "w") as f:
    for line in [{"kind": 0, "v": {"customTitle": "T", "requests": []}},
                 {"kind": 2, "k": ["requests"], "v": [{"requestId": "r1", "timestamp": 1792000000000, "modelId": "copilot/claude-opus-4.5", "message": {"text": "fix it"}, "response": []}]},
                 {"kind": 2, "k": ["requests", 0, "response"], "v": [term("npm test", 1), {"kind": "thinking"}]},
                 {"kind": 2, "k": ["requests", 0, "response"], "i": 1, "v": [todo, term("npm test -- --fix", 0)]},  # i=1 truncates the thinking item
                 {"kind": 1, "k": ["requests", 0, "result"], "v": {"metadata": {"promptTokens": 100, "outputTokens": 10}}}]:
        f.write(json.dumps(line) + "\n")
a = tracker.parse(session, "copilot", tracker.parse_copilot)
assert (a["cwd"], a["title"], a["model"], a["tokens"], a["where"]) == (proj_dir, "T", "claude-opus-4.5", 110, "vscode"), a
assert [e[1] for e in a["events"]] == ["run_in_terminal: npm test", "manage_todo_list", "run_in_terminal: npm test -- --fix"]
assert tracker.errors(a) == (1, 1) and a["ended"]
assert tracker.doing_next(a) == ("run_in_terminal: npm test -- --fix", "test")
if sys.platform == "win32":
    assert tracker.uri_path("file:///c%3A/Users/x%20y") == "c:\\Users\\x y"  # VS Code's encoded drive colon
shutil.rmtree(ws)

# closing: a dummy process stands in for a Claude Code session.
# Windows checks identity via procStart; POSIX checks the binary name, so the dummy is `sleep` copied to "claude".
sid = "11111111-2222-3333-4444-555555555555"
tmp = tempfile.mkdtemp()
if sys.platform == "win32":
    dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    start = tracker.win_proc_start(dummy.pid)
    with open(os.path.join(tmp, f"{dummy.pid}.json"), "w") as f:
        json.dump({"pid": dummy.pid, "sessionId": sid, "procStart": "1"}, f)
else:
    fake = os.path.join(tmp, "claude")
    shutil.copy(shutil.which("sleep"), fake)
    dummy, start = subprocess.Popen([fake, "60"]), None
tracker.SESSIONS_DIR = tmp
if sys.platform == "win32":
    assert tracker.running() == {}, "wrong procStart must not match (PID reuse guard)"
with open(os.path.join(tmp, f"{dummy.pid}.json"), "w") as f:
    json.dump({"pid": dummy.pid, "sessionId": sid, "procStart": start}, f)
assert tracker.running() == {sid: dummy.pid}
with open(os.path.join(tmp, f"{sid}.jsonl"), "w") as f:
    f.write(json.dumps(call(1, "Bash", {"command": "sleep"})))
tracker.SOURCES = [("claude", os.path.join(tmp, "*.jsonl"), tracker.parse_claude)]
tracker.kill_agent(sid[:8], yes=True)
assert dummy.wait(timeout=10) is not None and tracker.running() == {}
shutil.rmtree(tmp)
print("ok")
