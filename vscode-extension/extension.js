// Agent Tracker for VS Code: a sidebar + status bar over tracker.py's `--json --stream` output. No dependencies.
const vscode = require('vscode');
const cp = require('child_process');
const path = require('path');

const STATUS = {
  needs: { icon: 'warning', color: 'list.errorForeground', label: 'needs you' },
  working: { icon: 'sync~spin', color: 'charts.green', label: 'working' },
  done: { icon: 'check', color: 'charts.blue', label: 'done, waiting for your next prompt' },
  idle: { icon: 'circle-outline', color: 'disabledForeground', label: 'idle' },
};
const TODO_ICON = { completed: 'pass', in_progress: 'arrow-right' };

const cfg = () => vscode.workspace.getConfiguration('agentTracker');
const fmtN = n => (n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n));
const ago = s => (s < 60 ? s + 's' : s < 3600 ? Math.floor(s / 60) + 'm' : s < 172800 ? Math.floor(s / 3600) + 'h' : Math.floor(s / 86400) + 'd');
const pct = a => Math.min(100, Math.round((100 * a.ctx) / a.cap));
const full = a => a.ctx >= 0.8 * a.cap; // tracker.py CTX_WARN: Claude Code auto-compacts a 200k window at ~83%
const LIMIT_WARN = 80;
const compacted = a => (a.compactions ? ` ⟳${a.compactions}` : '');
const bar = a => '▰'.repeat(Math.round(pct(a) / 10)) + '▱'.repeat(10 - Math.round(pct(a) / 10));
const where = a => (a.where === 'cli' ? a.src : `${a.src} ${a.where}`);

function run(file, args) {
  return new Promise((resolve, reject) =>
    cp.execFile(file, args, { windowsHide: true, maxBuffer: 64 << 20, env: { ...process.env, NO_COLOR: '1' } },
      (err, stdout, stderr) => (err ? reject(new Error((stderr || err.message).trim())) : resolve(stdout))));
}

async function findPython() {
  const custom = cfg().get('pythonPath');
  for (const p of custom ? [custom] : ['python3', 'python', 'py']) {
    try {
      await run(p, ['-c', 'import sys; sys.exit(sys.version_info < (3, 9))']);
      return p;
    } catch {}
  }
  return null;
}

function item(label, icon, color, collapsible = vscode.TreeItemCollapsibleState.None) {
  const it = new vscode.TreeItem(label, collapsible);
  it.iconPath = new vscode.ThemeIcon(icon, color && new vscode.ThemeColor(color));
  return it;
}

function agentItem(a) {
  const s = STATUS[a.status] || STATUS.idle;
  const it = item(a.project, s.icon, s.color, vscode.TreeItemCollapsibleState.Collapsed);
  it.id = a.id; // keeps expansion state across refreshes
  it.description = `${full(a) ? '◔ ' : ''}${pct(a)}% · ${fmtN(a.left)} left${compacted(a)} · ${a.status === 'needs' ? a.detail : a.doing}`;
  it.contextValue = a.pid ? 'agent-running' : 'agent';
  // transcript text is untrusted: appendText escapes it, and the tooltip isn't trusted to run command links
  const md = new vscode.MarkdownString('', true);
  const line = (...parts) => { parts.forEach((p, i) => (i % 2 ? md.appendText(p) : md.appendMarkdown(p))); md.appendMarkdown('  \n'); };
  line('**', a.title || a.project, '**');
  line(`$(${s.icon}) ${s.label}`, a.detail ? `: ${a.detail}` : '');
  line('', `${where(a)} · ${a.model || '?'} · ${a.id.slice(0, 8)}${a.pid ? ` · pid ${a.pid}` : ''} · last active ${ago(a.idle_secs)} ago`);
  line('', `Context ${bar(a)} ${fmtN(a.ctx)} / ${fmtN(a.cap)} (${pct(a)}%, ${fmtN(a.left)} left before compacting) · ${fmtN(a.tokens)} tokens total`);
  if (a.compactions) line('', `Compacted ${a.compactions}× (older context was summarized, details may be lost)`);
  if (a.errors) line('', `${a.errors} failed tool calls${a.recent_errors >= 3 ? ', failing repeatedly' : ''}`);
  if (a.conflict) line('$(zap) ', 'edited a file another agent also edited');
  if (a.next) line('Next: ', a.next);
  line('', a.cwd);
  it.tooltip = md;
  it.agent = a;
  it.children = [
    ...a.todos.map((t, i) => Object.assign(item(t.content, TODO_ICON[t.status] || 'circle-large-outline'), { id: `${a.id}:t${i}` })),
    ...a.events.slice().reverse().map((e, i) => {
      const ev = item(e.action, e.ok === false ? 'error' : e.ok === null ? 'watch' : 'debug-stackframe-dot', e.ok === false && 'list.errorForeground');
      ev.id = `${a.id}:e${i}`;
      ev.description = e.ts ? new Date(e.ts).toLocaleTimeString() : '';
      ev.tooltip = e.action;
      return ev;
    }),
  ];
  return it;
}

function conflictItem(c) {
  const it = item(`Conflict: ${path.basename(c.path)}`, 'zap', 'list.warningForeground');
  it.description = c.agents.map(id => id.slice(0, 8)).join(', ');
  it.tooltip = `${c.path}\nedited by ${c.agents.length} agents within the last hour`;
  it.command = { command: 'vscode.open', title: 'Open file', arguments: [vscode.Uri.file(c.path)] };
  return it;
}

class AgentTree {
  constructor() {
    this.changed = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.changed.event;
  }
  set(data, error) {
    Object.assign(this, { data, error });
    this.changed.fire();
  }
  getTreeItem(it) {
    return it;
  }
  getChildren(it) {
    if (it) return it.children || [];
    if (this.error) return [item(this.error, 'error', 'list.errorForeground')];
    if (!this.data) return [item('Loading agents…', 'loading~spin')];
    if (!this.data.agents.length) return [item(`No agents active in the last ${cfg().get('hours')}h`, 'info')];
    return [...this.data.conflicts.map(conflictItem), ...this.data.agents.map(agentItem)];
  }
}

function updateStatusBar(sb, data) {
  const count = k => data.agents.filter(a => a.status === k).length;
  const [needs, working, conflicts] = [count('needs'), count('working'), data.conflicts.length];
  const limits = data.limits || [];
  const hot = limits.some(l => l.used >= LIMIT_WARN);
  sb.text = `$(pulse) ${working}` + (needs ? ` $(warning) ${needs}` : '') + (conflicts ? ` $(zap) ${conflicts}` : '')
    + (limits.length ? ` $(dashboard) ${Math.round(Math.max(...limits.map(l => l.used)))}%` : '');
  sb.tooltip = `Agent Tracker: ${data.agents.length} agents · ${working} working · ${needs} need you · ${conflicts} file conflicts`
    + limits.map(l => `\nCodex ${l.window} limit: ${Math.round(l.used)}% used${resets(l)}`).join('');
  sb.backgroundColor = needs || conflicts || hot ? new vscode.ThemeColor('statusBarItem.warningBackground') : undefined;
  sb.show();
}

const resets = l => (l.resets_at ? `, resets ${new Date(l.resets_at * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })}` : '');

// notify on state changes only, mirroring tracker.py's alerts(): needs you, finished a turn, context nearly full,
// new conflict, Codex plan limit nearly used up
function alerts(prev, data) {
  if (!prev || !cfg().get('notifications')) return;
  const was = new Map(prev.agents.map(a => [a.id, a.status]));
  const wasFull = new Set(prev.agents.filter(full).map(a => a.id));
  const warn = msg => vscode.window.showWarningMessage(msg, 'Show Agents')
    .then(pick => pick && vscode.commands.executeCommand('agentTracker.agents.focus'));
  for (const a of data.agents) {
    if (a.status === 'needs' && was.get(a.id) !== 'needs') warn(`${a.project}: agent needs you (${a.detail})`);
    else if (a.status === 'done' && was.get(a.id) === 'working') vscode.window.showInformationMessage(`${a.project}: agent finished (${a.title || a.doing})`);
    if (full(a) && !wasFull.has(a.id)) warn(`${a.project}: context ${pct(a)}% full (${fmtN(a.ctx)} of ${fmtN(a.cap)}), it will compact soon`);
  }
  const known = new Set(prev.conflicts.map(c => c.path));
  for (const c of data.conflicts) {
    if (!known.has(c.path)) warn(`File conflict: ${path.basename(c.path)} edited by ${c.agents.map(id => id.slice(0, 8)).join(', ')}`);
  }
  const wasHot = new Set((prev.limits || []).filter(l => l.used >= LIMIT_WARN).map(l => l.window));
  for (const l of data.limits || []) {
    if (l.used >= LIMIT_WARN && !wasHot.has(l.window)) warn(`Codex ${l.window} limit ${Math.round(l.used)}% used${resets(l)}`);
  }
}

function activate(ctx) {
  const script = path.join(ctx.extensionPath, 'tracker.py');
  const tree = new AgentTree();
  const sb = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  sb.command = 'agentTracker.agents.focus';
  let python, proc, prev, retry, stopped = false;

  async function start() {
    python = python || (await findPython());
    if (!python) return tree.set(null, 'Python 3.9+ not found. Set "agentTracker.pythonPath".');
    const args = [script, '--json', '--stream', '-n', String(cfg().get('refreshSeconds')), '--hours', String(cfg().get('hours'))];
    const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (cfg().get('onlyThisWorkspace') && folder) args.push('-p', folder);
    let buf = '', errText = '';
    const p = (proc = cp.spawn(python, args, { windowsHide: true, env: { ...process.env, NO_COLOR: '1' } }));
    p.stdout.setEncoding('utf8');
    p.stdout.on('data', chunk => {
      buf += chunk;
      for (let i; (i = buf.indexOf('\n')) >= 0; buf = buf.slice(i + 1)) {
        let data;
        try { data = JSON.parse(buf.slice(0, i)); } catch { continue; }
        alerts(prev, data);
        prev = data;
        tree.set(data, null);
        updateStatusBar(sb, data);
      }
    });
    p.stderr.on('data', d => (errText = (errText + d).slice(-2000)));
    p.on('error', e => (errText += e.message));
    p.on('exit', code => {
      if (p !== proc || stopped) return; // replaced by restart() or extension shutting down
      tree.set(null, `Tracker stopped (exit ${code}): ${errText.trim().split('\n').pop() || 'no output'}. Retrying in 5s.`);
      retry = setTimeout(start, 5000);
    });
  }

  function restart() {
    const old = proc;
    [proc, python, prev] = [null, null, null]; // re-resolve python; next snapshot is a new baseline, not alerts
    old?.kill();
    clearTimeout(retry);
    start();
  }

  const needPython = () => python || (vscode.window.showErrorMessage('Python 3.9+ not found. Set "agentTracker.pythonPath".'), null);

  ctx.subscriptions.push(
    vscode.window.createTreeView('agentTracker.agents', { treeDataProvider: tree, showCollapseAll: true }),
    sb,
    vscode.commands.registerCommand('agentTracker.refresh', restart),
    vscode.commands.registerCommand('agentTracker.toggleWorkspace', () =>
      cfg().update('onlyThisWorkspace', !cfg().get('onlyThisWorkspace'), vscode.ConfigurationTarget.Global)),
    vscode.commands.registerCommand('agentTracker.dashboard', () => {
      if (needPython()) vscode.window.createTerminal({ name: 'Agent Tracker', shellPath: python, shellArgs: [script] }).show();
    }),
    vscode.commands.registerCommand('agentTracker.timeline', async it => {
      if (!needPython() || !it?.agent) return;
      try {
        const content = await run(python, [script, it.agent.id]);
        await vscode.window.showTextDocument(await vscode.workspace.openTextDocument({ content, language: 'log' }), { preview: true });
      } catch (e) {
        vscode.window.showErrorMessage(`Couldn't load timeline: ${e.message}`);
      }
    }),
    vscode.commands.registerCommand('agentTracker.kill', async it => {
      const a = it?.agent;
      if (!needPython() || !a) return;
      const choice = await vscode.window.showWarningMessage(`Close agent ${a.id.slice(0, 8)} in ${a.project}?`, {
        modal: true,
        detail: `pid ${a.pid} · ${a.status}: ${a.detail || a.doing}\n\nWork in progress stops mid-step. The transcript is kept, so you can resume with claude --resume.`,
      }, 'Close Agent');
      if (choice !== 'Close Agent') return;
      try {
        const out = await run(python, [script, '--kill', a.id, '-y']);
        vscode.window.showInformationMessage(out.trim().split('\n').slice(-3).join(' '));
      } catch (e) {
        vscode.window.showErrorMessage(`Couldn't close agent: ${e.message}`);
      }
    }),
    vscode.workspace.onDidChangeConfiguration(e => e.affectsConfiguration('agentTracker') && restart()),
    { dispose: () => { stopped = true; clearTimeout(retry); proc?.kill(); } },
  );
  start();
}

module.exports = { activate, deactivate() {}, alerts }; // alerts exported for test.js
