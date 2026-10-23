# agent-tracker

**A live terminal dashboard for all your coding agents.** Run 15 agents across 15 projects and see, in one screen, what each one is doing right now, how many tokens it has burned, how full its context is, which project it's in, and what it plans to do next.

```
 ◆ AGENT TRACKER   6 agents · 3 active · 48.2M tokens                                   15:49:35  ctrl+c to quit

   AGENT       CLI    PROJECT             MODEL         CONTEXT         OUT  TOTAL  AGO  DOING                              NEXT
 ● 2f73ebd2    claude api-server          opus-5-5      ▰▰▰▱▱▱   61k     8k   549k   4s  Bash: pnpm test auth               → Fix token refresh race
 ● 4cf2f29c    claude api-server          sonnet-5-5    ▰▰▰▰▱▱  137k    97k  18.5M  12s  Edit: src/routes/users.ts          → Add pagination tests
 ● ↳ a91c03e2  claude api-server          haiku-5-5     ▰▱▱▱▱▱   22k     2k   110k  30s  Grep: TODO\(auth\)
 ● 019c9278    codex  web-frontend        gpt-5.3-codex ▰▰▰▰▰▱  174k    60k   4.5M  41s  apply_patch: Update File: App.tsx  → Wire up dark mode toggle
 ○ dbe45ecb    claude infra               opus-5-5      ▰▰▱▱▱▱   94k    26k   1.9M   2h  Bash: terraform plan
 ○ 658f8a6e    codex  data-pipeline       gpt-5.3-codex ▰▰▰▰▰▰  246k   189k  23.0M   5h  shell: python etl.py --dry-run
```

In a real terminal it's color-coded: green ● for agents working right now, a context bar that turns yellow then red as the window fills, tool names colored by kind (shell, edit, read, web), and each project in its own color.

## What it shows

| Column | Meaning |
|---|---|
| ● / ○ | **active** (wrote to its transcript in the last 90s) / idle |
| AGENT | session id (`↳` = subagent spawned by another agent) |
| CLI | which agent tool: `claude` (Claude Code), `codex` (OpenAI Codex CLI / IDE) |
| PROJECT | the directory the agent is working in |
| MODEL | model of the latest turn — Opus, Sonnet, Haiku, GPT-5.x, … |
| CONTEXT | how full the context window is right now |
| OUT / TOTAL | output tokens / all tokens processed (incl. cached input) |
| AGO | time since its last action |
| DOING | the task it marked in-progress, else its last tool call (`Bash: npm test`, `Edit: src/app.ts`) |
| NEXT | the next pending item on its todo list / plan |

Drill into one agent for a timestamped timeline of every command and file it touched, plus its full todo list:

```
$ agents 4cf2f29c
 ◆ 4cf2f29c-a5e8-4cd9-b336-4122b00f809b   claude  Add pagination to users API
   ~/code/api-server  claude-sonnet-5-5  context ▰▰▰▰▱▱ 137k  total 18.5M  ● active

   10-23 14:14:13  Read: src/routes/users.ts
   10-23 14:14:23  Edit: src/routes/users.ts
   10-23 14:14:27  Bash: pnpm test users

   TODOS
   ✔ Add limit/offset params
   ▶ Write pagination tests
   ○ Update OpenAPI spec
```

## How it works

Nothing to configure, no hooks, no daemon, no API keys. Agent CLIs already write a full transcript of every session to disk — tool calls, timestamps, token usage, todo lists. `agent-tracker` just reads them:

| CLI | Transcripts |
|---|---|
| Claude Code (CLI, IDE, desktop — including subagents) | `~/.claude/projects/**/*.jsonl` |
| OpenAI Codex (CLI, IDE) | `~/.codex/sessions/**/rollout-*.jsonl` |

Because it reads the global transcript folders, **one install sees every agent in every project**. Files are only re-parsed when they change, so refreshing every 2s is cheap.

## Install

Requires Python 3.9+. Zero dependencies.

```bash
pip install git+https://github.com/NiloyBhattacharjee/agent-tracker
```

or run it straight from a clone:

```bash
git clone https://github.com/NiloyBhattacharjee/agent-tracker
python agent-tracker/tracker.py
```

## Usage

```bash
agents                 # live dashboard, all agents active in the last 24h
agents --here          # only agents working in the current directory
agents -p api-server   # only projects whose path contains "api-server"
agents --hours 2       # change the time window
agents --once          # print once and exit (pipe-friendly, no colors when piped)
agents -n 5            # refresh every 5s
agents 4cf2f29c        # timeline + todos for one agent (any id prefix)
```

Set `NO_COLOR=1` to disable colors.

## Adding another agent CLI

Every source is one entry in `SOURCES` in `tracker.py`: a name, a glob for its transcript files, and a parser that fills in the agent's `cwd`, `model`, `tokens`, `ctx`, `events` and `todos`. See `parse_codex` for a ~30-line example. PRs for Gemini CLI, Aider, OpenCode, etc. welcome.

## Notes

- **NEXT** is only filled when the agent keeps a todo list (Claude Code's `TodoWrite`, Codex's `update_plan`). Agents that don't plan show only DOING.
- The context bar assumes a 200k window for Claude; Codex reports its own window size.
- "Processes" here means the commands each agent runs, read from its transcript — not OS process inspection.

## Test

```bash
python test_tracker.py
```

## License

MIT
