import json, os, tempfile
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


# claude: same message split over two records counts tokens (and cost) once
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
assert a["tokens"] == 105 and a["out"] == 5 and a["ctx"] == 105, a
assert abs(a["cost"] - (10 * 4 + 90 * 0.20 + 5 * 20) / 1e6) < 1e-12, a["cost"]
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

# codex: cumulative token_count, update_plan → todos, argv shell, exit codes, apply_patch edits, cost
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
    {"type": "event_msg", "payload": {"type": "task_complete"}},
], tracker.parse_codex)
assert (a["model"], a["tokens"], a["out"], a["ctx"], a["cap"]) == ("gpt-5.3-codex", 500, 50, 300, 258400), a
assert tracker.doing_next(a) == ("shell: git status", "write test")
assert tracker.errors(a) == (1, 1)
assert list(a["edits"]) == [os.path.normcase(os.path.normpath("/proj/src/app.py"))]
assert abs(a["cost"] - (400 * 1.25 + 50 * 0.125 + 50 * 10) / 1e6) < 1e-12
assert tracker.status(a, a["mtime"] + 200)[0] == "done"
# cost: 1h cache writes bill at 2x input (Claude Code's default), 5m at 1.25x; fast mode doubles; web search $10/1k
u = {"input_tokens": 1000, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 0, "output_tokens": 0,
     "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 2000}}
assert abs(tracker.claude_cost(u, "claude-opus-5-5") - (1000 * 4 + 1000 * 5 + 2000 * 8) / 1e6) < 1e-12
assert abs(tracker.claude_cost(dict(u, speed="fast"), "claude-opus-5-5") - 2 * (1000 * 4 + 1000 * 5 + 2000 * 8) / 1e6) < 1e-12
assert abs(tracker.claude_cost({"server_tool_use": {"web_search_requests": 3}}, "claude-sonnet-5-5") - 0.03) < 1e-12
assert tracker.claude_cost(u, "some-unknown-model") is None
big = {"input_tokens": 150_000, "output_tokens": 1000}
assert abs(tracker.claude_cost(big, "claude-haiku-5-5") - (150_000 * 0.5 + 1000 * 2.5) / 1e6) < 1e-12

# alerts fire only on transitions: into needs, working → done, new conflicts
x = run([call(1, "Edit", {"file_path": "/x/proj/app.py"})], tracker.parse_claude)
y = run([call(1, "Write", {"file_path": "/x/proj/app.py"})], tracker.parse_claude)
st = {x["path"]: ("needs", "approve? Edit: app.py"), y["path"]: ("done", "")}
clash = {"app.py": [x, y]}
got = tracker.alerts([x, y], st, clash, {x["path"]: ("working", ""), y["path"]: ("working", "")}, {})
assert [t for t, _ in got] == ["⚠ proj: agent needs you", "✓ proj: agent finished", "⚡ File conflict"], got
assert tracker.alerts([x, y], st, clash, st, clash) == []  # nothing changed → no alerts
print("ok")
