// Smoke test without VS Code: a stub `vscode` module, the real tracker.py stream, real transcripts on this machine.
// Run: node test.js   (needs ../tracker.py copied here, which `vscode:prepublish` does; this script does it too)
const assert = require('assert');
const fs = require('fs');
const Module = require('module');
const path = require('path');

fs.copyFileSync(path.join(__dirname, '..', 'tracker.py'), path.join(__dirname, 'tracker.py'));
const shown = [];
const settings = { pythonPath: process.env.PYTHON || '', refreshSeconds: 1, hours: 24 * 365 * 5, onlyThisWorkspace: false, notifications: true };
const vscode = {
  EventEmitter: class { constructor() { this.fns = []; this.event = f => this.fns.push(f); } fire(x) { this.fns.forEach(f => f(x)); } },
  TreeItem: class { constructor(label, collapsibleState) { Object.assign(this, { label, collapsibleState }); } },
  TreeItemCollapsibleState: { None: 0, Collapsed: 1 },
  ThemeIcon: class { constructor(id, color) { Object.assign(this, { id, color }); } },
  ThemeColor: class { constructor(id) { this.id = id; } },
  MarkdownString: class {
    constructor(v = '') { this.value = v; }
    appendMarkdown(s) { this.value += s; return this; }
    appendText(s) { this.value += s.replace(/[\\`*_{}[\]()#+\-.!$<>]/g, '\\$&'); return this; }
  },
  StatusBarAlignment: { Left: 1 },
  ConfigurationTarget: { Global: 1 },
  Uri: { file: fsPath => ({ fsPath }) },
  window: {
    createTreeView: (id, o) => { vscode.tree = o.treeDataProvider; return { dispose() {} }; },
    createStatusBarItem: () => (vscode.sb = { show() {}, dispose() {} }),
    showWarningMessage: (...a) => (shown.push(['warn', a[0]]), Promise.resolve()),
    showInformationMessage: (...a) => (shown.push(['info', a[0]]), Promise.resolve()),
    showErrorMessage: (...a) => shown.push(['error', a[0]]),
    showTextDocument: async doc => shown.push(['doc', doc.content]),
  },
  workspace: {
    getConfiguration: () => ({ get: k => settings[k], update: async () => {} }),
    onDidChangeConfiguration: () => ({ dispose() {} }),
    openTextDocument: async o => o,
  },
  commands: { registerCommand: (id, f) => ((vscode.cmds[id] = f), { dispose() {} }), executeCommand: async () => {} },
  cmds: {},
};
const load = Module._load;
Module._load = function (req, ...rest) { return req === 'vscode' ? vscode : load.call(this, req, ...rest); };
const ext = require('./extension');

(async () => {
  const ctx = { extensionPath: __dirname, subscriptions: [] };
  ext.activate(ctx);
  for (let t = 0; t < 120 && !(vscode.tree.data || vscode.tree.error); t++) await new Promise(r => setTimeout(r, 250));
  assert(!vscode.tree.error, vscode.tree.error);
  const top = vscode.tree.getChildren();
  console.log(`${vscode.tree.data.agents.length} agents · status bar: ${vscode.sb.text}`);
  for (const it of top.slice(0, 6)) console.log(`  [${it.iconPath.id}] ${it.label} - ${it.description}  (${(it.children || []).length} children)`);
  if (vscode.tree.data.agents.length) {
    const first = top.find(it => it.agent);
    assert(first.tooltip.value.includes('Context'), 'tooltip shows the context window');
    await vscode.cmds['agentTracker.timeline'](first);
    const doc = shown.find(s => s[0] === 'doc');
    assert(doc && doc[1].includes(first.agent.id), 'timeline document opened for the agent');
  }

  // notifications fire only on transitions
  const agent = (id, status, ctx = 1000) => ({ id, status, project: 'p', detail: 'approve? Bash: rm', title: 'T', doing: 'x', ctx, cap: 200000 });
  shown.length = 0;
  ext.alerts({ agents: [agent('a', 'working'), agent('b', 'working')], conflicts: [] },
    { agents: [agent('a', 'needs'), agent('b', 'done')], conflicts: [{ path: '/x/app.py', agents: ['a', 'b'] }] });
  assert.deepStrictEqual(shown.map(s => s[0]), ['warn', 'info', 'warn'], JSON.stringify(shown));
  shown.length = 0;
  const same = { agents: [agent('a', 'needs')], conflicts: [] };
  ext.alerts(same, same);
  ext.alerts(null, same); // first snapshot is a baseline
  assert.strictEqual(shown.length, 0);
  // context crossing 80% and a Codex window crossing 80% notify once
  const hot = { agents: [agent('a', 'working', 170000)], conflicts: [], limits: [{ window: '5h', used: 85, resets_at: null }] };
  ext.alerts({ agents: [agent('a', 'working')], conflicts: [], limits: [{ window: '5h', used: 60, resets_at: null }] }, hot);
  assert.deepStrictEqual(shown.map(s => s[1].split(' ').slice(0, 3).join(' ')), ['p: context 85%', 'Codex 5h limit'], JSON.stringify(shown));
  shown.length = 0;
  ext.alerts(hot, hot);
  assert.strictEqual(shown.length, 0);

  ctx.subscriptions.forEach(s => s.dispose && s.dispose());
  console.log('ok');
  process.exit(0);
})().catch(e => { console.error(e); process.exit(1); });
