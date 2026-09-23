// Macros pane: the list of saved macros and an Ace editor over the one
// selected. It comes in two — Client Macros and Market Macros, the same
// pane type told its `side` in app.json — because a macro is for one side:
// a client macro sends orders and acts on them (Run…, as many runs at once
// as asked for), a market macro acts on orders received (Arm…). Each lists,
// checks, saves and follows only its own side. Everything it knows about the language comes from the server —
// the vocabulary (`macro_vocab`) for colouring, completion and help, and
// `check_macro` for the problems it underlines — so the Python parser is
// the only thing that decides what a macro means.
//
// Ace is vendored prebuilt (static/vendor/ace, ace-builds 1.44.0): classic
// scripts loaded on first use, no build step.

import { ensureMkio } from "/mkui/src/mkio-bridge.js";
import { aceRules, completionsAt, exportFileName, helpAt, hoverAt, recordingName } from "/static/macro-lang.js";
import { diffMarks, splitLines } from "/static/line-diff.js";

const { registerPaneType } = window.Mkui;

const ACE = "/static/vendor/ace";
const VIM_PREF = "mkfix.macro.vim";
const TEMPLATE_SCOPES = {
  new: "order", replace: "order", cancel: "cancel", accept: "accept", reject: "reject", fill: "fill",
  "unsol cxl": "unsolicited", restate: "restate", dk: "dk", correct: "correct", bust: "bust", renotify: "renotify",
};
const STARTER = {
  market: "on order\n    after 250ms\n    accept\n",
  client: "run\n    new symbol: 'IBM', side: buy, qty: 100, type: limit, price: 100.00\n"
    + "    expect ack within 2s\n    pass\n",
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

    ace.define("ace/mode/macro", ["require", "exports", "ace/lib/oop", "ace/mode/text",
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
      Mode.prototype.$id = "ace/mode/macro";
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

registerPaneType("macros", async (spec, app, host) => {
  const client = await ensureMkio(app.config?.mkio?.url);
  const cmd = (command, data = {}) => client.send("fix_cmd", { command, ...data }, { op: command });
  const side = spec.side === "client" ? "client" : "market";
  const Side = side === "client" ? "Client" : "Market";
  const startWord = side === "client" ? "Run" : "Arm";

  host.innerHTML = `
    <div class="macro-pane">
      <div class="mkfix-toolbar macro-toolbar">
        <button class="mkui-btn" data-act="new">New</button>
        <button class="mkui-btn" data-act="example">From example…</button>
        <button class="mkui-btn" data-act="save" disabled>Save</button>
        <button class="mkui-btn" data-act="arm" disabled>${startWord}…</button>
        <button class="mkui-btn" data-act="stop" disabled>Stop all</button>
        <button class="mkui-btn" data-act="record" title="Work orders by hand and get the macro that would have done it">Record…</button>
        <button class="mkui-btn" data-act="history" disabled title="Every Save of this macro: look at one, compare it, bring it back">History</button>
        <span class="macro-gap"></span>
        <button class="mkui-btn" data-act="import">Import</button>
        <button class="mkui-btn" data-act="export" disabled>Export</button>
        <button class="mkui-btn" data-act="delete" disabled>Delete</button>
        <label class="macro-vim"><input type="checkbox" data-act="vim"> vim</label>
        <button class="mkui-btn" data-act="help" title="The language reference (F1 on a word)">?</button>
      </div>
      <div class="macro-body">
        <div class="macro-list" tabindex="0"></div>
        <div class="macro-main">
          <div class="macro-editor"></div>
          <div class="macro-status"></div>
        </div>
        <div class="macro-history" hidden></div>
      </div>
      <input type="file" accept=".macro,.txt,text/plain" hidden>
    </div>`;
  const $ = (sel) => host.querySelector(sel);
  const listEl = $(".macro-list"), statusEl = $(".macro-status"), fileEl = $("input[type=file]");
  const historyEl = $(".macro-history");
  const button = (act) => $(`[data-act="${act}"]`);

  const { vocabulary: vocab } = await cmd("macro_vocab");
  const ace = await loadAce(vocab);
  const { Range } = ace.require("ace/range");
  const langTools = ace.require("ace/ext/language_tools");

  const editor = ace.edit($(".macro-editor"), {
    mode: "ace/mode/macro", theme: "ace/theme/mkfix", useWorker: false, tabSize: 4, useSoftTabs: true,
    showPrintMargin: false, fontSize: 12, enableBasicAutocompletion: true, enableLiveAutocompletion: true,
    displayIndentGuides: true, highlightActiveLine: true, fixedWidthGutter: true,
  });
  editor.setReadOnly(true);

  // -- state -----------------------------------------------------------------------
  const macros = new Map();          // name -> row
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

  // History: `versions` while the panel is open, `viewing` while the editor
  // shows a saved version instead of the macro — whose text, unsaved edits
  // and all, waits in `draft`.
  let versions = null;
  let viewing = null;
  let draft = "";
  let diffMarkers = [];

  const text = () => (viewing ? draft : editor.getValue());
  const dirty = () => current !== null && text() !== saved;
  const liveRuns = (name) => [...runs.values()].filter((r) => r.macro === name && LIVE.includes(r.status));

  function renderList() {
    const names = [...macros.keys()].sort((a, b) => a.localeCompare(b));
    listEl.innerHTML = names.map((name) => {
      const row = macros.get(name);
      const live = liveRuns(name);
      const working = live.reduce((n, r) => n + (r.live || 0), 0);      // orders with a live macro
      const state = live.length && live.every((r) => r.status === "paused") ? "paused" : "armed";
      const what = live.length > 1 ? `${live.length} runs` : side === "client" && state === "armed" ? "running" : state;
      const badge = live.length ? `<span class="macro-badge macro-${state}">${what}${working ? ` · ${working}` : ""}</span>`
        : row.problems ? `<span class="macro-badge macro-problems">${row.problems} problem${row.problems === 1 ? "" : "s"}</span>` : "";
      return `<div class="macro-item${name === current ? " macro-current" : ""}" data-name="${name.replace(/"/g, "&quot;")}">
        <span class="macro-name"></span>${badge}</div>`;
    }).join("") || `<div class="macro-empty">No ${side} macros yet — a ${side} macro ${side === "client"
      ? "sends orders and acts on them" : "acts on the orders you receive"}. <b>New</b> starts one; <b>From example…</b> copies a bundled one.</div>`;
    listEl.querySelectorAll(".macro-item").forEach((el) => { el.querySelector(".macro-name").textContent = el.dataset.name; });
  }

  function renderButtons() {
    const live = current ? liveRuns(current) : [];
    button("save").disabled = !dirty() || !!viewing;
    button("history").disabled = !current;
    // A live run is no bar to another: a client macro runs as often as asked,
    // a market macro is armed once per session (the server refuses a repeat).
    button("arm").disabled = !current || dirty() || problems > 0 || !!viewing;
    button("stop").disabled = !live.length;
    button("stop").textContent = live.length > 1 ? `Stop all (${live.length})` : "Stop run";
    button("export").disabled = !current;
    button("delete").disabled = !current || live.length > 0;
    button("delete").title = live.length ? "Stop its runs first" : "";
    button("arm").title = !current ? "" : viewing ? "Back to the macro first" : dirty() ? "Save first" : problems ? "Fix the problems first"
      : side === "client" ? `Send this macro's orders${live.length ? " — another run beside the " + live.length + " live" : ""}`
        : "Let this macro take matching orders";
  }

  function status(text, kind = "") {
    statusEl.textContent = text;
    statusEl.className = `macro-status ${kind ? "macro-" + kind : ""}`;
  }

  // -- diagnostics -------------------------------------------------------------------
  let checkTimer = null;
  let checkSeq = 0;
  function scheduleCheck() {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(runCheck, 300);
  }
  async function runCheck() {
    if (current === null || viewing) return;
    const seq = ++checkSeq;
    let result;
    try {
      result = await cmd("check_macro", { source: editor.getValue(), side });
    } catch (err) {
      status(`check failed: ${err.message ?? err}`, "error");
      return;
    }
    if (seq !== checkSeq) return;                       // an older answer to text that has moved on
    const session = editor.getSession();
    markers.forEach((id) => session.removeMarker(id));
    markers = result.diagnostics.map((d) =>
      session.addMarker(new Range(d.line - 1, d.col, d.line - 1, Math.max(d.end, d.col + 1)), `macro-mark-${d.severity}`, "text"));
    session.setAnnotations(result.diagnostics.map((d) => ({ row: d.line - 1, column: d.col, text: d.message, type: d.severity })));
    problems = result.errors;
    checked = { needs_session: !!result.needs_session, session: result.session ?? "" };
    const first = result.diagnostics.find((d) => d.severity === "error");
    status(problems ? `${problems} problem${problems === 1 ? "" : "s"} — line ${first.line}: ${first.message}`
      : result.diagnostics.length ? `${result.diagnostics.length} warning(s)` : "No problems", problems ? "error" : "ok");
    renderButtons();
  }

  // -- where the macros are, live ----------------------------------------------------
  function renderLive() {
    const session = editor.getSession();
    liveMarkers.forEach(({ id, row }) => { session.removeMarker(id); session.removeGutterDecoration(row, "macro-live-gutter"); });
    liveMarkers = [];
    if (!current || dirty() || viewing) return;
    // Every live run of this text: a run of an earlier version is on other lines.
    const version = macros.get(current)?._mkio_version;
    const mine = new Set(liveRuns(current).filter((r) => version == null || !r.version || r.version === version).map((r) => r.id));
    if (!mine.size) return;
    const byLine = new Map();
    for (const inst of instances.values()) {
      if (!mine.has(inst.run_id) || !["running", "listening"].includes(inst.status) || !inst.line) continue;
      byLine.set(inst.line, (byLine.get(inst.line) ?? 0) + 1);
    }
    for (const [line, count] of byLine) {
      const row = line - 1;
      session.addGutterDecoration(row, "macro-live-gutter");
      liveMarkers.push({ id: session.addMarker(new Range(row, 0, row, 1), "macro-live-line", "fullLine"), row, count });
    }
    if (byLine.size && !problems) {
      status([...byLine].sort((a, b) => a[0] - b[0]).map(([l, c]) => `line ${l} ×${c}`).join("   ")
        + `   — macros waiting here${mine.size > 1 ? `, of ${mine.size} runs` : ""}`, "live");
    }
  }

  // A run of the open macro that has just ended says how: a macro may fail in
  // its first instant, and an empty blotter is not an explanation.
  const announced = new Set();
  let seenRuns = false;
  function announceRun() {
    const ended = [...runs.values()].filter((r) => !LIVE.includes(r.status));
    if (!seenRuns) { ended.forEach((r) => announced.add(r.id)); seenRuns = true; return; }   // history, not news
    for (const r of ended) {
      if (announced.has(r.id)) continue;
      announced.add(r.id);
      if (r.macro !== current) continue;
      const tally = `${r.orders} order${r.orders === 1 ? "" : "s"}, ${r.passed} passed, ${r.failed} failed`;
      if (r.verdict === "failed") status(`Run ${r.id} ${r.status}: FAILED (${tally}) — ${Side} Macro Log and ${Side} Macro Orders say why`, "error");
      else status(`Run ${r.id} ${r.status}${r.verdict ? ": " + r.verdict : ""} (${tally})`, r.verdict ? "ok" : "");
    }
  }

  // -- opening, saving ------------------------------------------------------------------
  async function leaveCurrent() {
    if (!dirty()) return true;
    return app.confirm(`Discard the unsaved changes to ${current}?`, { title: "Unsaved changes", kind: "warning", ok: "Discard" });
  }

  // -- history ---------------------------------------------------------------------------
  // Every Save is a version of the row (the table is versioned). A version is
  // looked at in the editor itself, read-only, against the macro as it is
  // saved now; Restore puts its text back as an unsaved edit, so bringing an
  // old version back is one more Save and nothing is ever lost to it.
  const stamp = (fix) => {
    const m = /^(\d{4})(\d\d)(\d\d)-(\d\d):(\d\d):(\d\d)/.exec(fix ?? "");
    if (!m) return fix ?? "";
    const d = new Date(Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]));
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };

  function clearDiff() {
    const session = editor.getSession();
    diffMarkers.forEach(({ id, row, cls }) => { if (id != null) session.removeMarker(id); session.removeGutterDecoration(row, cls); });
    diffMarkers = [];
  }

  function renderHistory() {
    historyEl.hidden = versions === null;
    if (versions === null) return;
    const latest = versions[0]?._mkio_version;
    const usedBy = (v) => [...runs.values()].filter((r) => r.macro === current && r.version === v).map((r) => `#${r.id}`);
    historyEl.innerHTML = `<div class="macro-history-head"><b>History</b><span></span><button class="mkui-btn" data-act="history">Close</button></div>`
      + (versions.length ? versions.map((v) => {
        const by = usedBy(v._mkio_version);
        return `<div class="macro-version${viewing?.version === v._mkio_version ? " macro-current" : ""}" data-version="${v._mkio_version}">
          <div><b>v${v._mkio_version}</b>${v._mkio_version === latest ? ` <span class="macro-badge">saved now</span>` : ""}
            ${v.problems ? `<span class="macro-badge macro-problems">${v.problems} problem${v.problems === 1 ? "" : "s"}</span>` : ""}</div>
          <div class="macro-version-meta">${stamp(v.updated_at)}${by.length ? ` · run${by.length > 1 ? "s" : ""} ${by.join(" ")}` : ""}</div>
        </div>`;
      }).join("") : `<div class="macro-empty">No saved versions.</div>`)
      + (viewing ? `<div class="macro-history-foot"><button class="mkui-btn" data-act="restore">Restore v${viewing.version}</button>
          <button class="mkui-btn" data-act="back">Back to the macro</button></div>` : "");
    historyEl.querySelector(".macro-history-head span").textContent = current ?? "";
  }

  async function loadVersions() {
    const id = macros.get(current)?.id;
    if (id == null) { versions = []; return; }
    const reply = await client.request("macro_versions", { id });
    if (reply.type === "error") throw new Error(reply.message ?? "could not read the history");
    versions = reply.rows ?? [];
  }

  async function toggleHistory() {
    if (versions !== null) { backToScript(); versions = null; renderHistory(); editor.resize(); return; }
    try { await loadVersions(); } catch (err) { versions = null; return status(String(err.message ?? err), "error"); }
    renderHistory();
    editor.resize();
  }

  function view(version) {
    const row = versions?.find((v) => v._mkio_version === version);
    if (!row) return;
    if (!viewing) draft = editor.getValue();
    viewing = { version, source: row.source };
    editor.setReadOnly(true);
    editor.setValue(row.source, -1);
    const session = editor.getSession();
    markers.forEach((id) => session.removeMarker(id));
    markers = [];
    session.clearAnnotations();
    clearDiff();
    liveMarkers.forEach(({ id, row: r }) => { session.removeMarker(id); session.removeGutterDecoration(r, "macro-live-gutter"); });
    liveMarkers = [];
    // Against the macro as saved now — what Restore would change.
    const marks = diffMarks(splitLines(row.source), splitLines(saved));
    for (const r of marks.only) {
      session.addGutterDecoration(r, "macro-diff-only-gutter");
      diffMarkers.push({ id: session.addMarker(new Range(r, 0, r, 1), "macro-diff-only", "fullLine"), row: r, cls: "macro-diff-only-gutter" });
    }
    const last = session.getLength() - 1;
    for (const gap of marks.gaps) {
      const r = Math.min(gap.row, last);
      const cls = gap.row > last ? "macro-diff-gap-after" : "macro-diff-gap";
      session.addGutterDecoration(r, cls);
      diffMarkers.push({ id: null, row: r, cls });
    }
    const same = !marks.removed && !marks.added;
    status(`v${version}, saved ${stamp(row.updated_at)} — ` + (same ? "the same as the macro saved now"
      : `${marks.removed} line${marks.removed === 1 ? "" : "s"} only in this version (marked), ${marks.added} only in the macro saved now (▸ in the margin)`)
      + (draft !== saved ? " — your unsaved edits are kept" : ""), "help");
    renderHistory();
    renderButtons();
  }

  function backToScript(replacement) {
    if (!viewing) return;
    clearDiff();
    viewing = null;
    editor.setReadOnly(false);
    editor.setValue(replacement ?? draft, -1);
    draft = "";
    renderHistory();
    renderButtons();
    return runCheck().then(renderLive);
  }

  async function restore() {
    if (!viewing) return;
    const { version, source } = viewing;
    if (draft !== saved && draft !== source
      && !(await app.confirm(`Replace your unsaved edits to ${current} with v${version}?`, { title: "Restore", kind: "warning", ok: "Replace" }))) return;
    await backToScript(source);           // its check writes the status line: ours goes after
    clearTimeout(checkTimer);
    status(source === saved ? `v${version} is the macro as saved now: nothing to restore`
      : `v${version} is in the editor, unsaved — Save keeps it as a new version`, source === saved ? "" : "ok");
    editor.focus();
  }

  function open(name, source) {
    clearDiff();
    viewing = null;
    draft = "";
    const hadHistory = versions !== null;
    versions = null;
    current = name;
    saved = source;
    editor.setReadOnly(false);
    editor.setValue(source, -1);
    editor.getSession().getUndoManager().reset();
    problems = 0;
    renderList();
    renderButtons();
    const settled = runCheck().then(renderLive);
    app.state.set("selected_macro", name);
    editor.focus();
    if (hadHistory) loadVersions().then(renderHistory, () => renderHistory()); else renderHistory();
    return settled;                     // its check writes the status line: a caller with more to say waits for it
  }

  async function select(name) {
    if (name === current || !(await leaveCurrent())) return;
    return open(name, macros.get(name)?.source ?? "");
  }

  async function save() {
    if (current === null || viewing) return;
    const source = editor.getValue();
    try {
      await cmd("save_macro", { name: current, source, side });
      saved = source;
      if (versions !== null) loadVersions().then(renderHistory, () => {});
      status(problems ? `Saved as a draft: ${problems} problem(s) keep it from being ${side === "client" ? "run" : "armed"}` : "Saved", problems ? "error" : "ok");
    } catch (err) {
      status(String(err.message ?? err), "error");
    }
    renderButtons();
    renderLive();
  }

  // -- recording --------------------------------------------------------------------------
  // The server listens while you work orders by hand on this side's blotters;
  // Stop writes what you did as a macro and opens it here. The recording
  // lives in the server, so it survives this pane closing: the button asks.
  let recording = null;                 // the server's status while one is under way
  let recordTimer = null;

  function renderRecord() {
    const b = button("record");
    b.classList.toggle("macro-recording", !!recording);
    b.textContent = recording ? `Stop recording · ${recording.actions} action${recording.actions === 1 ? "" : "s"}` : "Record…";
    clearInterval(recordTimer);
    if (recording) recordTimer = setInterval(pollRecord, 1500);
  }

  async function pollRecord() {
    try {
      const s = await cmd("record_status", { side });
      recording = s.recording ? s : null;
    } catch { recording = null; }
    renderRecord();
  }

  function askSession() {
    return new Promise((resolve) => {
      const bar = document.createElement("div");
      bar.className = "macro-ask";
      bar.innerHTML = `<span>Record what you do by hand on</span><select></select>
        <button class="mkui-btn">Start</button><button class="mkui-btn">Cancel</button>`;
      const select = bar.querySelector("select");
      select.appendChild(new Option("every session", ""));
      [...sessions.keys()].sort().forEach((s) => select.appendChild(new Option(s, s)));
      const [start, cancel] = bar.querySelectorAll("button");
      const done = (value) => { bar.remove(); resolve(value); };
      start.onclick = () => done(select.value);
      cancel.onclick = () => done(null);
      $(".macro-main").prepend(bar);
      select.focus();
    });
  }

  async function toggleRecord() {
    try {
      if (!recording) {
        const session = await askSession();
        if (session === null) return;
        recording = await cmd("record_start", { side, session });
        renderRecord();
        status(side === "client"
          ? "Recording: send orders from Sent Orders and work them — Replace, Cancel, DK. Stop recording writes the macro."
          : "Recording: work the orders that arrive in Received Orders and Sent Trades. Stop recording writes the macro.", "live");
        return;
      }
      const stamped = recordingName(side);
      let suggested = stamped;
      for (let n = 2; macros.has(suggested); n++) suggested = `${stamped}-${n}`;
      const asked = await askName(suggested, { label: "Keep my delays",
        title: "Off: each action runs the moment what it answered comes. On: it also waits the time you took to answer." });
      if (!asked) return;                                  // still recording: nothing is lost by changing your mind
      const { name, ticked: delays } = asked;
      if (macros.has(name)) return status(`${name} already exists`, "error");
      if (!(await leaveCurrent())) return;
      const result = await cmd("record_stop", { side, name, delays: delays ? "1" : "" });
      recording = null;
      renderRecord();
      if (!result.orders) {
        return status(side === "client" ? "Nothing recorded: no order was sent by hand while recording"
          : "Nothing recorded: no order arrived and was worked by hand while recording", "error");
      }
      await cmd("save_macro", { name, source: result.source, side });
      await open(name, result.source);
      clearTimeout(checkTimer);
      status(`Recorded ${result.orders} order${result.orders === 1 ? "" : "s"}, ${result.actions} action${result.actions === 1 ? "" : "s"} — a first draft: read it, and loosen what is too exact`, "ok");
    } catch (err) {
      status(String(err.message ?? err), "error");
      pollRecord();
    }
  }

  async function createNamed(suggested, source) {
    const name = await askName(suggested);
    if (!name) return;
    if (macros.has(name)) { status(`${name} already exists`, "error"); return; }
    if (!(await leaveCurrent())) return;
    const text = source ?? STARTER[side];
    try {
      await cmd("save_macro", { name, source: text, side });
      open(name, text);
    } catch (err) {
      status(String(err.message ?? err), "error");
    }
  }

  // `option` = a tick box beside the name ({ label, title }): the answer is then { name, ticked }.
  function askName(suggested, option = null) {
    return new Promise((resolve) => {
      const bar = document.createElement("div");
      bar.className = "macro-ask";
      bar.innerHTML = `<span>Name</span><input type="text" spellcheck="false">`
        + (option ? `<label class="macro-vim" title="${option.title}"><input type="checkbox"> ${option.label}</label>` : "")
        + `<button class="mkui-btn">OK</button><button class="mkui-btn">Cancel</button>`;
      const input = bar.querySelector("input[type=text]");
      const tick = bar.querySelector("input[type=checkbox]");
      const [ok, cancel] = bar.querySelectorAll("button");
      input.value = suggested;
      const done = (value) => { bar.remove(); resolve(value); };
      ok.onclick = () => {
        const name = input.value.trim().replace(/\s+/g, " ") || null;
        done(option && name ? { name, ticked: !!tick?.checked } : name);
      };
      cancel.onclick = () => done(null);
      input.onkeydown = (e) => { if (e.key === "Enter") ok.onclick(); if (e.key === "Escape") done(null); };
      $(".macro-main").prepend(bar);
      input.focus();
      input.select();
    });
  }

  async function fromExample() {
    const { examples } = await cmd("list_examples", { side });
    const menu = document.createElement("div");
    menu.className = "macro-examples";
    menu.innerHTML = `<div class="macro-examples-head">Bundled ${side} examples — opened as a copy of your own <button class="mkui-btn">Close</button></div>`
      + examples.map((e) => `<div class="macro-example" data-name="${e.name}"><b></b><span></span></div>`).join("");
    menu.querySelectorAll(".macro-example").forEach((el, i) => {
      el.querySelector("b").textContent = examples[i].title;
      el.querySelector("span").textContent = examples[i].shows ?? "";
      el.onclick = async () => {
        menu.remove();
        const { source } = await cmd("get_example", { name: el.dataset.name });
        let name = el.dataset.name;
        for (let n = 2; macros.has(name); n++) name = `${el.dataset.name}-${n}`;
        createNamed(name, source);
      };
    });
    menu.querySelector("button").onclick = () => menu.remove();
    $(".macro-main").prepend(menu);
  }

  // -- toolbar ------------------------------------------------------------------------------
  host.addEventListener("click", async (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    const item = e.target.closest(".macro-item");
    if (item) return select(item.dataset.name);
    const version = e.target.closest(".macro-version");
    if (version) return view(Number(version.dataset.version));
    if (!act || e.target.disabled) return;
    if (act === "history") return toggleHistory();
    if (act === "record") return toggleRecord();
    if (act === "restore") return restore();
    if (act === "back") return backToScript();
    if (act === "new") return createNamed("my-macro", null);
    if (act === "example") return fromExample();
    if (act === "save") return save();
    if (act === "help") return app.fireAction("pane.show", "help-viewer");
    if (act === "import") return fileEl.click();
    if (act === "export") {
      const blob = new Blob([editor.getValue()], { type: "text/plain" });
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: exportFileName(current) });
      a.click();
      URL.revokeObjectURL(a.href);
      return;
    }
    if (act === "arm") {
      const context = { row: { name: current, ...checked } };
      return side === "client" ? app.dialog("run_macro", context) : app.dialog("arm_macro", context);
    }
    if (act === "stop") {
      // One run of several is stopped from the Macro Runs pane; this stops the macro.
      cmd("stop_macro", { name: current }).catch((err) => status(String(err.message ?? err), "error"));
      return;
    }
    if (act === "delete") {
      if (!(await app.confirm(`Delete ${current}? Its saved versions go with it.`, { title: "Delete macro", kind: "danger", ok: "Delete" }))) return;
      try {
        await cmd("delete_macro", { name: current });
        clearDiff();
        current = null; saved = ""; viewing = null; versions = null; draft = "";
        renderHistory();
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
    createNamed(file.name.replace(/\.[^.]+$/, ""), text);
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
      app.state.set("help_target", { page: "macro-language", anchor: help.name.toLowerCase().replace(/\s+/g, "-") });
    },
  });

  langTools.setCompleters([{
    identifierRegexps: [/[A-Za-z_][A-Za-z0-9_]*/],
    getCompletions(ed, session, pos, prefix, callback) {
      const found = completionsAt(vocab, session.getDocument().getAllLines(), pos.row, pos.column, extras);
      callback(null, found.map((c, i) => ({ caption: c.caption, value: c.value, meta: c.meta, docText: c.doc, score: 1000 - i })));
    },
  }]);

  // Hover: what F1 says, where the mouse is — and a problem's message first,
  // since that is the more urgent thing to read about a word.
  const { HoverTooltip } = ace.require("ace/tooltip");
  const hover = new HoverTooltip();
  hover.setDataProvider((e, ed) => {
    const pos = e.getDocumentPosition();
    const session = ed.getSession();
    const tip = hoverAt(vocab, session.getDocument().getAllLines(), pos.row, pos.column);
    const problem = (session.getAnnotations() ?? []).find((a) => a.row === pos.row && !viewing);
    if (!tip && !problem) return;
    const node = document.createElement("div");
    node.className = "macro-hover";
    if (problem) node.appendChild(Object.assign(document.createElement("div"), { className: `macro-hover-${problem.type}`, textContent: problem.text }));
    if (tip) {
      node.appendChild(Object.assign(document.createElement("div"), { className: "macro-hover-title", textContent: tip.title }));
      tip.lines.forEach((line) => node.appendChild(Object.assign(document.createElement("div"), { textContent: line.replace(/`/g, "") })));
    }
    const line = session.getLine(pos.row);
    const range = tip ? new Range(pos.row, tip.start, pos.row, tip.end) : new Range(pos.row, line.search(/\S|$/), pos.row, line.length);
    if (!tip && (pos.column < range.start.column || pos.column > range.end.column)) return;
    hover.showForRange(ed, range, node, e);
  });
  hover.addToEditor(editor);

  editor.on("change", () => { if (current !== null) { scheduleCheck(); renderButtons(); } });

  // -- subscriptions ----------------------------------------------------------------------------
  const subs = [];
  function follow(service, map, key, after, filter) {
    const subid = `macro-${side}-${service}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
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
  follow("macros_query", macros, "name", () => {
    if (current !== null && !macros.has(current)) {
      clearDiff();
      current = null; viewing = null; versions = null; draft = "";
      editor.setReadOnly(true);
      renderHistory();
    }
    renderList(); renderButtons();
  }, mine);
  follow("macro_runs_query", runs, "id", () => { renderList(); renderButtons(); renderLive(); announceRun(); renderHistory(); }, mine);
  follow("macro_orders_query", instances, "id", renderLive, mine);
  follow("sessions_query", sessions, "session_id", () => { extras.sessions = [...sessions.keys()]; });
  follow("templates_query", templates, "id", () => {
    extras.templates = {};
    for (const t of templates.values()) (extras.templates[t.scope] ??= []).push(t.name);
  });
  }
  followAll();

  // The Macro Orders pane selects a macro's row: show where it is.
  const unwatch = app.state.subscribe("selected_macro_order", (inst) => {
    if (!inst || inst.macro !== current || !inst.line) return;
    editor.gotoLine(inst.line, 0, true);
  });
  // A recording stopped and saved from an order blotter: open it here. The
  // row may still be on its way when the ask arrives, and may never come — a
  // recording of nothing saves nothing.
  const unopen = app.state.subscribe("open_macro", async (wanted) => {
    if (!wanted || wanted.side !== side) return;
    app.state.set("open_macro", null);
    for (let n = 0; n < 30 && !macros.has(wanted.name); n++) await new Promise((r) => setTimeout(r, 100));
    if (!macros.has(wanted.name)) return status("Nothing was recorded, so nothing was saved", "error");
    await select(wanted.name);
    clearTimeout(checkTimer);
    status(`Recorded as ${wanted.name} — a first draft: read it, and loosen what is too exact`, "ok");
  });
  // The recording is the server's and the blotters can start and stop it too.
  const unrecording = app.state.subscribe(`macros.${side}`, (s) => {
    if (!s || !!recording === !!s.recording) return;
    pollRecord();
  });
  // The Help viewer's "Open in editor".
  const unexample = app.state.subscribe("open_example", async (wanted) => {
    if (!wanted || wanted.side !== side) return;          // the other editor's
    const { name } = wanted;
    app.state.set("open_example", null);
    const { source } = await cmd("get_example", { name });
    let unique = name;
    for (let n = 2; macros.has(unique); n++) unique = `${name}-${n}`;
    createNamed(unique, source);
  });

  const resize = new ResizeObserver(() => editor.resize());
  resize.observe($(".macro-editor"));
  renderList();
  renderButtons();
  pollRecord();

  // mkui keeps a closed pane's element; its subscriptions should not outlive the view.
  const paneEl = host.closest("mkui-pane") ?? host;
  paneEl.addEventListener("mkui-pane-close", () => {
    subs.splice(0).forEach((id) => client.unsubscribe(id));
    clearTimeout(checkTimer);
    clearInterval(recordTimer);
  });
  paneEl.addEventListener("mkui-pane-open", () => { if (!subs.length) followAll(); editor.resize(); pollRecord(); });
  void unwatch; void unexample; void unopen; void unrecording;
});
