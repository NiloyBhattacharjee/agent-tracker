import json, os, tempfile
import tracker


def run(lines, parser):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write("\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines))
    a = tracker.parse(f.name, "x", parser)
    os.unlink(f.name)
    return a


# claude: same message split over two records must count tokens once
u = {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5}
a = run([
    {"type": "assistant", "cwd": "/x/proj", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": u,
     "content": [{"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
         {"content": "a", "status": "in_progress", "activeForm": "Doing a"}, {"content": "b", "status": "pending"}]}}]}},
    {"type": "assistant", "cwd": "/x/proj", "message": {"id": "m1", "usage": u,
     "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
    "not json",
], tracker.parse_claude)
assert a["tokens"] == 105 and a["out"] == 5 and a["ctx"] == 105, a
assert a["events"][-1][1] == "Bash: ls"
assert tracker.doing_next(a) == ("Doing a", "b")

# codex: cumulative token_count, update_plan → todos, argv-style shell command
a = run([
    {"type": "session_meta", "payload": {"id": "s1", "cwd": "C:\\proj"}},
    {"type": "turn_context", "payload": {"cwd": "C:\\proj", "model": "gpt-5.3-codex"}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "update_plan",
     "arguments": json.dumps({"plan": [{"step": "fix bug", "status": "completed"}, {"step": "write test", "status": "pending"}]})}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": '{"command":["git","status"]}'}},
    {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 500, "output_tokens": 50},
     "last_token_usage": {"total_tokens": 300}, "model_context_window": 258400}}},
], tracker.parse_codex)
assert (a["model"], a["tokens"], a["out"], a["ctx"], a["cap"]) == ("gpt-5.3-codex", 500, 50, 300, 258400), a
assert tracker.doing_next(a) == ("shell: git status", "write test")
print("ok")
