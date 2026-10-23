# agent-tracker

**A live terminal dashboard for all your coding agents.** Run 15 agents across 15 projects and see, in one screen, what each one is doing right now, which ones are **blocked waiting on you**, which ones are **editing the same file**, which ones are **failing in a loop**, and what each one costs, along with tokens, context fill, project, and what it plans to do next.

```
 ◆ AGENT TRACKER   7 agents · 3 working · 2 need you · 1 conflicts · 52.4M tokens · $41.87            15:49:35  ctrl+c to quit

   AGENT        CLI    PROJECT             MODEL         CONTEXT        TOTAL    COST ERR  AGO  DOING                             NEXT
 ⚠ 7be1c0d4     claude api-server          opus-5-5      ▰▱▱▱▱▱  212k    6.1M   $4.12   ·  35s  answer: Postgres or SQLite?        → Write migrations
 ⚠ c03a99f1     claude infra               sonnet-5-5    ▰▱▱▱▱▱   88k    2.4M   $0.71   ·   2m  approve? Bash: terraform apply
 ● 2f73ebd2 ⚡  claude api-server          opus-5-5      ▰▱▱▱▱▱   61k     549k   $0.38   2   4s  Bash: pnpm test auth              → Fix token refresh race
 ● 4cf2f29c ⚡  claude api-server          sonnet-5-5    ▰▱▱▱▱▱  137k   18.5M   $4.85   9  12s  Edit: src/routes/users.ts         → Add pagination tests
 ● ↳ a91c03e2   claude api-server          haiku-5-5     ▱▱▱▱▱▱   22k     110k   $0.01   ·  30s  Grep: TODO\(auth\)
 ✓ 019c9278     codex  web-frontend        gpt-5.3-codex ▰▰▰▰▰▱  174k    4.5M   $1.89   1   6m  apply_patch: Update File: App.tsx
 ○ 658f8a6e     codex  data-pipeline       gpt-5.3-codex ▰▰▰▰▰▰  246k   23.0M  $29.91   3   5h  shell: python etl.py --dry-run
   ⚡ src/routes/users.ts  edited by 2f73ebd2 (3m ago), 4cf2f29c (12s ago)
```

In a real terminal it's color-coded. The title bar turns **red** when an agent needs you or files conflict, and blocked agents jump to the top. ERR turns red when an agent is failing over and over. The context bar goes green, then yellow, then red as the window fills. Tool names are colored by kind (shell, edit, read, web), and each project gets its own color.

## What it shows

| Column | Meaning |
|---|---|
| status | **⚠ needs you**: blocked on a question or a permission prompt · **●** working · **✓** finished its turn, waiting for your next prompt · **○** idle |
| AGENT | session id. `↳` = subagent spawned by another agent. **⚡** = this agent edited a file another agent also edited in the last hour |
| CLI | which agent tool: `claude` (Claude Code), `codex` (OpenAI Codex CLI / IDE) |
| PROJECT | the directory the agent is working in |
| MODEL | model of the latest turn: Opus, Sonnet, Haiku, GPT-5.x, … |
| CONTEXT | how full the context window is right now (1M for current Claude models; Codex reports its own) |
| TOTAL | all tokens processed, including cached input |
| COST | estimated $ at API list prices, with cache reads and writes priced separately |
| ERR | failed tool calls (non-zero exit codes, failed edits, rejected calls). **Red** = 3+ of its last 5 calls failed, so it's probably looping |
| AGO | time since its last action |
| DOING | the task it marked in-progress, else its last tool call (`Bash: npm test`, `Edit: src/app.ts`). For a blocked agent: the question it asked or the call waiting for approval |
| NEXT | the next pending item on its todo list / plan |

Under the table, each **⚡ conflict** line lists the file and which agents touched it, and when.

Drill into one agent for a timestamped timeline of every command and file it touched (`✗` = failed, `…` = still pending), plus its full todo list:

```
$ agents 4cf2f29c
 ◆ 4cf2f29c-a5e8-4cd9-b336-4122b00f809b   claude  Add pagination to users API
   ~/code/api-server  claude-sonnet-5-5  context ▰▱▱▱▱▱ 137k  total 18.5M  $4.85  9 errors  ● working

   10-23 14:14:13 · Read: src/routes/users.ts
   10-23 14:14:23 · Edit: src/routes/users.ts
   10-23 14:14:27 ✗ Bash: pnpm test users
   10-23 14:14:41 … Bash: pnpm test users --reporter verbose

   TODOS
   ✔ Add limit/offset params
   ▶ Write pagination tests
   ○ Update OpenAPI spec
```

## How it works

Nothing to configure: no hooks, no daemon, no API keys. Agent CLIs already write a full transcript of every session to disk, including tool calls and their results, timestamps, token usage, and todo lists. `agent-tracker` just reads them:

| CLI | Transcripts |
|---|---|
| Claude Code (CLI, IDE, desktop, including subagents) | `~/.claude/projects/**/*.jsonl` |
| OpenAI Codex (CLI, IDE) | `~/.codex/sessions/**/rollout-*.jsonl` |

Because it reads the global transcript folders, **one install sees every agent in every project**. Files are only re-parsed when they change, so refreshing every 2s is cheap.

How the signals are derived:

- **Needs you:** the agent asked a question (`AskUserQuestion`, `ExitPlanMode`, Codex `request_user_input`), or a tool call has no result after 20s (90s for shell commands, subagents, and web calls, which can legitimately run long). An agent at a permission prompt looks exactly like this. A shell command that runs longer than 90s will also show up here, so the label reads `approve?`.
- **Conflicts:** two agents' file edits (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`, Codex `apply_patch`) hit the same path within an hour. Edits made through shell commands (`sed -i`, `>`) aren't detected.
- **Errors:** Claude Code's `is_error` tool results, and Codex non-zero exit codes.
- **Cost:** per-message token usage × per-model price, from the `PRICES` table in `tracker.py`. Claude prices are Anthropic's API list prices. GPT-5.x uses GPT-5 list pricing as an estimate. If you're on a subscription plan, read this as "what it would cost on the API".

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

Set `NO_COLOR=1` to disable colors. Thresholds (`STUCK_SECS`, `SLOW_STUCK_SECS`, `CONFLICT_SECS`, `ACTIVE_SECS`) and prices (`PRICES`) are constants at the top of `tracker.py`.

## Adding another agent CLI

Every source is one entry in `SOURCES` in `tracker.py`: a name, a glob for its transcript files, and a parser that fills in the agent's `cwd`, `model`, `tokens`, `ctx`, `cost`, `edits`, `todos`, and calls `add_event` / `finish_event` for each tool call. See `parse_codex` for an example. PRs for Gemini CLI, Aider, OpenCode, etc. welcome.

## Notes

- **NEXT** is only filled when the agent keeps a todo list (Claude Code's `TodoWrite`, Codex's `update_plan`). Agents that don't plan show only DOING.
- "Processes" here means the commands each agent runs, read from its transcript. There's no OS process inspection.

## Test

```bash
python test_tracker.py
```

## License

MIT
