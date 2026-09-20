// Scenarios pane: the list of saved scripts and an Ace editor over the one
// selected. It comes in two — Client Scenarios and Market Scenarios, the same
// pane type told its `side` in app.json — because a script is for one side:
// a client scenario sends orders and acts on them (Run…, as many runs at once
// as asked for), a market scenario acts on orders received (Arm…). Each lists,
// checks, saves and follows only its own side. Everything it knows about the language comes from the server —
// the vocabulary (`scenario_vocab`) for colouring, completion and help, and
// `check_scenario` for the problems it underlines — so the Python parser is
// the only thing that decides what a script means.
//
// Ace is vendored prebuilt (static/vendor/ace, ace-builds 1.44.0): classic
// scripts loaded on first use, no build step.

import { ensureMkio } from "/mkui/src/mkio-bridge.js";
import { aceRules, completionsAt, helpAt } from "/static/scenario-lang.js";

const { registerPaneType } = window.Mkui;

const ACE = "/static/vendor/ace";
const VIM_PREF = "mkfix.scenario.vim";
const TEMPLATE_SCOPES = {
  new: "order", replace: "order", cancel: "cancel", accept: "accept", reject: "reject", fill: "fill",
  "unsol cxl": "unsolicited", restate: "restate", dk: "dk", correct: "correct", bust: "bust", renotify: "renotify",
};
const STARTER = {
  market: (name) => `scenario ${name}\n\non order\n    after 250ms\n    accept\n`,
  client: (name) => `scenario ${name}\n\nrun\n    new symbol: 'IBM', side: buy, qty: 100, type: limit, price: 100.00\n`
    + `    expect ack within 2s\n    pass\n`,
};
const LIVE = ["armed", "paused"];

let aceReady = null;
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = src;
    el.onload = resolve;
    el.onerror = () => reject(new Error(`could not load ${src}`));
    document.head.appendChild(el);
  });
}

// Ace, its completion extension, and our mode, theme and folding — once per page.
function loadAce(vocab) {
  if (aceReady) return aceReady;
  aceReady = (async () => {
    await loadScript(`${ACE}/ace.js`);
    const ace = window.ace;
    ace.config.set("basePath", ACE);
    await loadScript(`${ACE}/ext-language_tools.js`);
    await loadScript(`${ACE}/ext-searchbox.js`);

    ace.define("ace/mode/scenario", ["require", "exports", "ace/lib/oop", "ace/mode/text",
      "ace/mode/text_highlight_rules", "ace/mode/folding/fold_mode", "ace/range"], (require, exports) => {
      const oop = require("ace/lib/oop");
      const TextMode = require("ace/mode/text").Mode;
      const { TextHighlightRules } = require("ace/mode/text_highlight_rules");
      const { FoldMode: BaseFold } = require("ace/mode/folding/fold_mode");
      const { Range } = require("ace/range");

      const Rules = function () { this.$rules = aceRules(vocab); this.normalizeRules(); };
      oop.inherits(Rules, TextHighlightRules);

      // Blocks are indentation: a line folds when the next line that says anything is deeper.
      const indentOf = (line) => (/\S/.test(line) ? line.length - line.trimStart().length : -1);
      const Fold = function () {};
      oop.inherits(Fold, BaseFold);
      Fold.prototype.getFoldWidget = function (session, style, row) {
        const here = indentOf(session.getLine(row));
        if (here < 0) return "";
        for (let r = row + 1; r < session.getLength(); r++) {
          const next = indentOf(session.getLine(r));
          if (next >= 0) return next > here ? "start" : "";
        }
        return "";
      };
      Fold.prototype.getFoldWidgetRange = function (session, style, row) {
        const here = indentOf(session.getLine(row));
        let last = row;
        for (let r = row + 1; r < session.getLength(); r++) {
          const next = indentOf(session.getLine(r));
          if (next >= 0 && next <= here) break;
          if (next >= 0) last = r;
        }
        return last > row ? new Range(row, session.getLine(row).length, last, session.getLine(last).length) : null;
      };

      const Mode = function () { this.HighlightRules = Rules; this.foldingRules = new Fold(); };
      oop.inherits(Mode, TextMode);
      Mode.prototype.lineCommentStart = "#";
      Mode.prototype.$id = "ace/mode/scenario";
      // A line that opens a block indents the next one.
      Mode.prototype.getNextLineIndent = function (state, line, tab) {
        const indent = this.$getIndent(line);
        const opens = /^\s*(on\s+(sent\s+)?order\b|run\b|when\b|if\b|else\b|while\b|repeat\b)/i.test(line.replace(/#.*$/, ""));
        return opens ? indent + tab : indent;
      };
      exports.Mode = Mode;
    });

    ace.define("ace/theme/mkfix", ["require", "exports", "ace/lib/dom"], (require, exports) => {
      exports.isDark = true;
      exports.cssClass = "ace-mkfix";
      exports.cssText = "";            // the rules live in mkfix.css, on mkui's variables
    });
    return ace;
  })();
  return aceReady;
}

registerPaneType("scenarios", async (spec, app, host) => {
  const client = await ensureMkio(app.config?.mkio?.url);
  const cmd = (command, data = {}) => client.send("fix_cmd", { command, ...data }, { op: command });
  const side = spec.side === "client" ? "client" : "market";
  const Side = side === "client" ? "Client" : "Market";
  const startWord = side === "client" ? "Run" : "Arm";

  host.innerHTML = `
    <div class="scn-pane">
      <div class="mkfix-toolbar scn-toolbar">
        <button class="mkui-btn" data-act="new">New</button>
        <button class="mkui-btn" data-act="example">From example…</button>
        <button class="mkui-btn" data-act="save" disabled>Save</button>
        <button class="mkui-btn" data-act="arm" disabled>${startWord}…</button>
        <button class="mkui-btn" data-act="stop" disabled>Stop all</button>
        <span class="scn-gap"></span>
        <button class="mkui-btn" data-act="import">Import</button>
        <button class="mkui-btn" data-act="export" disabled>Export</button>
        <button class="mkui-btn" data-act="delete" disabled>Delete</button>
        <label class="scn-vim"><input type="checkbox" data-act="vim"> vim</label>
        <button class="mkui-btn" data-act="help" title="The language reference (F1 on a word)">?</button>
      </div>
      <div class="scn-body">
        <div class="scn-list" tabindex="0"></div>
        <div class="scn-main">
          <div class="scn-editor"></div>
          <div class="scn-status"></div>
        </div>
      </div>
      <input type="file" accept=".scenario,.txt,text/plain" hidden>
    </div>`;
  const $ = (sel) => host.querySelector(sel);
  const listEl = $(".scn-list"), statusEl = $(".scn-status"), fileEl = $("input[type=file]");
  const button = (act) => $(`[data-act="${act}"]`);

  const { vocabulary: vocab } = await cmd("scenario_vocab");
  const ace = await loadAce(vocab);
  const { Range } = ace.require("ace/range");
  const langTools = ace.require("ace/ext/language_tools");

  const editor = ace.edit($(".scn-editor"), {
    mode: "ace/mode/scenario", theme: "ace/theme/mkfix", useWorker: false, tabSize: 4, useSoftTabs: true,
    showPrintMargin: false, fontSize: 12, enableBasicAutocompletion: true, enableLiveAutocompletion: true,
    displayIndentGuides: true, highlightActiveLine: true, fixedWidthGutter: true,
  });
  editor.setReadOnly(true);

  // -- state -----------------------------------------------------------------------
  const scenarios = new Map();          // name -> row
  const runs = new Map();               // run id -> row
  const instances = new Map();          // instance id -> row
  let current = null;                   // the name open in the editor
  let saved = "";                       // its text as last saved
  let problems = 0;
  let checked = { needs_session: false, session: "" };
  let markers = [];
  let liveMarkers = [];
  let extras = { sessions: [], templates: {}, templateScopes: TEMPLATE_SCOPES, side };
  const sessions = new Map();
  const templates = new Map();

  const dirty = () => current !== null && editor.getValue() !== saved;
  const liveRuns = (name) => [...runs.values()].filter((r) => r.scenario === name && LIVE.includes(r.status));

  function renderList() {
    const names = [...scenarios.keys()].sort((a, b) => a.localeCompare(b));
    listEl.innerHTML = names.map((name) => {
      const row = scenarios.get(name);
      const live = liveRuns(name);
      const scripts = live.reduce((n, r) => n + (r.live || 0), 0);
      const state = live.length && live.every((r) => r.status === "paused") ? "paused" : "armed";
      const what = live.length > 1 ? `${live.length} runs` : side === "client" && state === "armed" ? "running" : state;
      const badge = live.length ? `<span class="scn-badge scn-${state}">${what}${scripts ? ` · ${scripts}` : ""}</span>`
        : row.problems ? `<span class="scn-badge scn-problems">${row.problems} problem${row.problems === 1 ? "" : "s"}</span>` : "";
      return `<div class="scn-item${name === current ? " scn-current" : ""}" data-name="${name.replace(/"/g, "&quot;")}">
        <span class="scn-name"></span>${badge}</div>`;
    }).join("") || `<div class="scn-empty">No ${side} scenarios yet — scripts that ${side === "client"
      ? "send orders and act on them" : "act on the orders you receive"}. <b>New</b> starts one; <b>From example…</b> copies a bundled one.</div>`;
    listEl.querySelectorAll(".scn-item").forEach((el) => { el.querySelector(".scn-name").textContent = el.dataset.name; });
  }

  function renderButtons() {
    const live = current ? liveRuns(current) : [];
    button("save").disabled = !dirty();
    // A live run is no bar to another: a client script runs as often as asked,
    // a market script is armed once per session (the server refuses a repeat).
    button("arm").disabled = !current || dirty() || problems > 0;
    button("stop").disabled = !live.length;
    button("stop").textContent = live.length > 1 ? `Stop all (${live.length})` : "Stop run";
    button("export").disabled = !current;
    button("delete").disabled = !current || live.length > 0;
    button("delete").title = live.length ? "Stop its runs first" : "";
    button("arm").title = !current ? "" : dirty() ? "Save first" : problems ? "Fix the problems first"
      : side === "client" ? `Send this script's orders${live.length ? " — another run beside the " + live.length + " live" : ""}`
        : "Let this script take matching orders";
  }

  function status(text, kind = "") {
    statusEl.textContent = text;
    statusEl.className = `scn-status ${kind ? "scn-" + kind : ""}`;
  }

  // -- diagnostics -------------------------------------------------------------------
  let checkTimer = null;
  let checkSeq = 0;
  function scheduleCheck() {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(runCheck, 300);
  }
  async function runCheck() {
    if (current === null) return;
    const seq = ++checkSeq;
    let result;
    try {
      result = await cmd("check_scenario", { source: editor.getValue(), side });
    } catch (err) {
      status(`check failed: ${err.message ?? err}`, "error");
      return;
    }
    if (seq !== checkSeq) return;                       // an older answer to text that has moved on
    const session = editor.getSession();
    markers.forEach((id) => session.removeMarker(id));
    markers = result.diagnostics.map((d) =>
      session.addMarker(new Range(d.line - 1, d.col, d.line - 1, Math.max(d.end, d.col + 1)), `scn-mark-${d.severity}`, "text"));
    session.setAnnotations(result.diagnostics.map((d) => ({ row: d.line - 1, column: d.col, text: d.message, type: d.severity })));
    problems = result.errors;
    checked = { needs_session: !!result.needs_session, session: result.session ?? "" };
    const first = result.diagnostics.find((d) => d.severity === "error");
    status(problems ? `${problems} problem${problems === 1 ? "" : "s"} — line ${first.line}: ${first.message}`
      : result.diagnostics.length ? `${result.diagnostics.length} warning(s)` : "No problems", problems ? "error" : "ok");
    renderButtons();
  }

  // -- where the scripts are, live ----------------------------------------------------
  function renderLive() {
    const session = editor.getSession();
    liveMarkers.forEach(({ id, row }) => { session.removeMarker(id); session.removeGutterDecoration(row, "scn-live-gutter"); });
    liveMarkers = [];
    if (!current || dirty()) return;
    // Every live run of this text: a run of an earlier version is on other lines.
    const version = scenarios.get(current)?._mkio_version;
    const mine = new Set(liveRuns(current).filter((r) => version == null || !r.version || r.version === version).map((r) => r.id));
    if (!mine.size) return;
    const byLine = new Map();
    for (const inst of instances.values()) {
      if (!mine.has(inst.run_id) || !["running", "listening"].includes(inst.status) || !inst.line) continue;
      byLine.set(inst.line, (byLine.get(inst.line) ?? 0) + 1);
    }
    for (const [line, count] of byLine) {
      const row = line - 1;
      session.addGutterDecoration(row, "scn-live-gutter");
      liveMarkers.push({ id: session.addMarker(new Range(row, 0, row, 1), "scn-live-line", "fullLine"), row, count });
    }
    if (byLine.size && !problems) {
      status([...byLine].sort((a, b) => a[0] - b[0]).map(([l, c]) => `line ${l} ×${c}`).join("   ")
        + `   — scripts waiting here${mine.size > 1 ? `, of ${mine.size} runs` : ""}`, "live");
    }
  }

  // A run of the open script that has just ended says how: a script may fail in
  // its first instant, and an empty blotter is not an explanation.
  const announced = new Set();
  let seenRuns = false;
  function announceRun() {
    const ended = [...runs.values()].filter((r) => !LIVE.includes(r.status));
    if (!seenRuns) { ended.forEach((r) => announced.add(r.id)); seenRuns = true; return; }   // history, not news
    for (const r of ended) {
      if (announced.has(r.id)) continue;
      announced.add(r.id);
      if (r.scenario !== current) continue;
      const tally = `${r.orders} order${r.orders === 1 ? "" : "s"}, ${r.passed} passed, ${r.failed} failed`;
      if (r.verdict === "failed") status(`Run ${r.id} ${r.status}: FAILED (${tally}) — ${Side} Log and ${Side} Scripts say why`, "error");
      else status(`Run ${r.id} ${r.status}${r.verdict ? ": " + r.verdict : ""} (${tally})`, r.verdict ? "ok" : "");
    }
  }

  // -- opening, saving ------------------------------------------------------------------
  async function leaveCurrent() {
    if (!dirty()) return true;
    return app.confirm(`Discard the unsaved changes to ${current}?`, { title: "Unsaved changes", kind: "warning", ok: "Discard" });
  }

  function open(name, source) {
    current = name;
    saved = source;
    editor.setReadOnly(false);
    editor.setValue(source, -1);
    editor.getSession().getUndoManager().reset();
    problems = 0;
    renderList();
    renderButtons();
    runCheck().then(renderLive);
    app.state.set("selected_scenario", name);
    editor.focus();
  }

  async function select(name) {
    if (name === current || !(await leaveCurrent())) return;
    open(name, scenarios.get(name)?.source ?? "");
  }

  async function save() {
    if (current === null) return;
    const source = editor.getValue();
    try {
      await cmd("save_scenario", { name: current, source, side });
      saved = source;
      status(problems ? `Saved as a draft: ${problems} problem(s) keep it from being ${side === "client" ? "run" : "armed"}` : "Saved", problems ? "error" : "ok");
    } catch (err) {
      status(String(err.message ?? err), "error");
    }
    renderButtons();
    renderLive();
  }

  async function createNamed(suggested, source) {
    const name = await askName(suggested);
    if (!name) return;
    if (scenarios.has(name)) { status(`${name} already exists`, "error"); return; }
    if (!(await leaveCurrent())) return;
    const text = source ? source.replace(/^(\s*scenario\s+).*$/m, `$1${name}`) : STARTER[side](name);
    try {
      await cmd("save_scenario", { name, source: text, side });
      open(name, text);
    } catch (err) {
      status(String(err.message ?? err), "error");
    }
  }

  function askName(suggested) {
    return new Promise((resolve) => {
      const bar = document.createElement("div");
      bar.className = "scn-ask";
      bar.innerHTML = `<span>Name</span><input type="text" spellcheck="false"><button class="mkui-btn">OK</button><button class="mkui-btn">Cancel</button>`;
      const input = bar.querySelector("input");
      const [ok, cancel] = bar.querySelectorAll("button");
      input.value = suggested;
      const done = (value) => { bar.remove(); resolve(value); };
      ok.onclick = () => done(input.value.trim().replace(/\s+/g, " ") || null);
      cancel.onclick = () => done(null);
      input.onkeydown = (e) => { if (e.key === "Enter") ok.onclick(); if (e.key === "Escape") done(null); };
      $(".scn-main").prepend(bar);
      input.focus();
      input.select();
    });
  }

  async function fromExample() {
    const { examples } = await cmd("list_examples", { side });
    const menu = document.createElement("div");
    menu.className = "scn-examples";
    menu.innerHTML = `<div class="scn-examples-head">Bundled ${side} examples — opened as a copy of your own <button class="mkui-btn">Close</button></div>`
      + examples.map((e) => `<div class="scn-example" data-name="${e.name}"><b></b><span></span></div>`).join("");
    menu.querySelectorAll(".scn-example").forEach((el, i) => {
      el.querySelector("b").textContent = examples[i].title;
      el.querySelector("span").textContent = examples[i].shows ?? "";
      el.onclick = async () => {
        menu.remove();
        const { source } = await cmd("get_example", { name: el.dataset.name });
        let name = el.dataset.name;
        for (let n = 2; scenarios.has(name); n++) name = `${el.dataset.name}-${n}`;
        createNamed(name, source);
      };
    });
    menu.querySelector("button").onclick = () => menu.remove();
    $(".scn-main").prepend(menu);
  }

  // -- toolbar ------------------------------------------------------------------------------
  host.addEventListener("click", async (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    const item = e.target.closest(".scn-item");
    if (item) return select(item.dataset.name);
    if (!act || e.target.disabled) return;
    if (act === "new") return createNamed("my-scenario", null);
    if (act === "example") return fromExample();
    if (act === "save") return save();
    if (act === "help") return app.fireAction("pane.show", "help-viewer");
    if (act === "import") return fileEl.click();
    if (act === "export") {
      const blob = new Blob([editor.getValue()], { type: "text/plain" });
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: `${current}.scenario` });
      a.click();
      URL.revokeObjectURL(a.href);
      return;
    }
    if (act === "arm") {
      const context = { row: { name: current, ...checked } };
      return side === "client" ? app.dialog("run_scenario", context) : app.dialog("arm_scenario", context);
    }
    if (act === "stop") {
      // One run of several is stopped from the Runs pane; this stops the script.
      cmd("stop_scenario", { name: current }).catch((err) => status(String(err.message ?? err), "error"));
      return;
    }
    if (act === "delete") {
      if (!(await app.confirm(`Delete ${current}? Its saved versions go with it.`, { title: "Delete scenario", kind: "danger", ok: "Delete" }))) return;
      try {
        await cmd("delete_scenario", { name: current });
        current = null; saved = "";
        editor.setValue("", -1);
        editor.setReadOnly(true);
        status("");
      } catch (err) { status(String(err.message ?? err), "error"); }
      renderList(); renderButtons();
    }
  });

  fileEl.addEventListener("change", async () => {
    const file = fileEl.files[0];
    fileEl.value = "";
    if (!file) return;
    const text = await file.text();
    const named = /^\s*scenario\s+(.+?)\s*(#.*)?$/m.exec(text)?.[1] ?? file.name.replace(/\.[^.]+$/, "");
    createNamed(named, text);
  });

  const vim = button("vim");
  function setVim(on) {
    editor.setKeyboardHandler(on ? "ace/keyboard/vim" : null, () => {
      if (!on) return;
      const { Vim } = ace.require("ace/keyboard/vim").CodeMirror;
      Vim.defineEx("write", "w", () => save());
    });
    try { localStorage.setItem(VIM_PREF, on ? "1" : ""); } catch { /* private mode */ }
  }
  try { vim.checked = localStorage.getItem(VIM_PREF) === "1"; } catch { /* private mode */ }
  if (vim.checked) setVim(true);
  vim.addEventListener("change", () => { setVim(vim.checked); editor.focus(); });

  editor.commands.addCommand({ name: "save", bindKey: { win: "Ctrl-S", mac: "Command-S" }, exec: () => save() });
  editor.commands.addCommand({
    name: "help", bindKey: { win: "F1", mac: "F1" },
    exec: () => {
      const pos = editor.getCursorPosition();
      const help = helpAt(vocab, editor.getSession().getDocument().getAllLines(), pos.row, pos.column);
      if (!help) return status("No help for this word — ? opens the reference");
      status(`${help.form || help.name} — ${help.doc}`, "help");
      app.state.set("help_target", { page: "scenario-language", anchor: help.name.toLowerCase().replace(/\s+/g, "-") });
    },
  });

  langTools.setCompleters([{
    identifierRegexps: [/[A-Za-z_][A-Za-z0-9_]*/],
    getCompletions(ed, session, pos, prefix, callback) {
      const found = completionsAt(vocab, session.getDocument().getAllLines(), pos.row, pos.column, extras);
      callback(null, found.map((c, i) => ({ caption: c.caption, value: c.value, meta: c.meta, docText: c.doc, score: 1000 - i })));
    },
  }]);

  editor.on("change", () => { if (current !== null) { scheduleCheck(); renderButtons(); } });

  // -- subscriptions ----------------------------------------------------------------------------
  const subs = [];
  function follow(service, map, key, after, filter) {
    const subid = `scn-${side}-${service}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    const apply = (op, row) => { if (op === "delete") map.delete(row[key]); else map.set(row[key], row); };
    client.subscribe(service, "query", {
      subid, ...(filter ? { filter } : {}),
      onSnapshot: (rows) => { map.clear(); rows.forEach((r) => apply("insert", r)); after(); },
      onUpdate: (op, row) => { apply(op, row); after(); },
      onDelta: (changes) => { changes.forEach(({ op, row }) => apply(op, row)); after(); },
    });
    subs.push(subid);
  }
  function followAll() {
  const mine = `side == '${side}'`;
  follow("scenarios_query", scenarios, "name", () => {
    if (current !== null && !scenarios.has(current)) { current = null; editor.setReadOnly(true); }
    renderList(); renderButtons();
  }, mine);
  follow("scenario_runs_query", runs, "id", () => { renderList(); renderButtons(); renderLive(); announceRun(); }, mine);
  follow("scenario_instances_query", instances, "id", renderLive, mine);
  follow("sessions_query", sessions, "session_id", () => { extras.sessions = [...sessions.keys()]; });
  follow("templates_query", templates, "id", () => {
    extras.templates = {};
    for (const t of templates.values()) (extras.templates[t.scope] ??= []).push(t.name);
  });
  }
  followAll();

  // The Runs pane selects a script's row: show where it is.
  const unwatch = app.state.subscribe("selected_scenario_instance", (inst) => {
    if (!inst || inst.scenario !== current || !inst.line) return;
    editor.gotoLine(inst.line, 0, true);
  });
  // The Help viewer's "Open in editor".
  const unexample = app.state.subscribe("open_example", async (wanted) => {
    if (!wanted || wanted.side !== side) return;          // the other editor's
    const { name } = wanted;
    app.state.set("open_example", null);
    const { source } = await cmd("get_example", { name });
    let unique = name;
    for (let n = 2; scenarios.has(unique); n++) unique = `${name}-${n}`;
    createNamed(unique, source);
  });

  const resize = new ResizeObserver(() => editor.resize());
  resize.observe($(".scn-editor"));
  renderList();
  renderButtons();

  // mkui keeps a closed pane's element; its subscriptions should not outlive the view.
  const paneEl = host.closest("mkui-pane") ?? host;
  paneEl.addEventListener("mkui-pane-close", () => {
    subs.splice(0).forEach((id) => client.unsubscribe(id));
    clearTimeout(checkTimer);
  });
  paneEl.addEventListener("mkui-pane-open", () => { if (!subs.length) followAll(); editor.resize(); });
  void unwatch; void unexample;
});
