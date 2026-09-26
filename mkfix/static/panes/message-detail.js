/**
 * Message Detail pane: shows field-by-field breakdown of a selected FIX message.
 *
 * - Translates through the owning session's dictionary (fix_version), falling
 *   back to the message's BeginString, then FIX.4.2.
 * - Repeating groups render as collapsible sub-blocks using the dictionary's
 *   group metadata (display-only; the engine has no group model).
 * - Header/Body/Trailer sections collapse; the choice persists per browser.
 * - Columns are drag-resizable (double-click a divider fits the column
 *   to its content); widths persist per browser.
 * - UTC timestamps render in a selectable timezone (default: browser's).
 */

import { loadDictionary, defaultDictionary, parseMessageTree, splitFix } from "../fix-dictionary.js";
import { summarizeMessage, parseRawMessage, formatTimestamp } from "../fix-formatter.js";
import { ensureMkio } from "/mkui/src/mkio-bridge.js";

const { registerPaneType } = window.Mkui;

const LS = {
  tz: "mkfix.detail.tz",
  sections: "mkfix.detail.sections",
  cols: "mkfix.detail.cols",
};

function lsGet(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : JSON.parse(v);
  } catch {
    return fallback;
  }
}

function lsSet(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private mode etc. — prefs just don't persist */
  }
}

function browserTz() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function timezoneList() {
  let zones = [];
  try {
    zones = Intl.supportedValuesOf("timeZone");
  } catch {
    zones = [];
  }
  const set = new Set(["UTC", browserTz(), ...zones]);
  return [...set];
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

registerPaneType("message-detail", async (spec, app, host) => {
  const client = await ensureMkio(app.config?.mkio?.url);

  host.innerHTML = "";
  host.style.overflow = "auto";
  host.style.padding = "8px";
  host.style.fontSize = "12px";

  const placeholder = document.createElement("div");
  placeholder.style.color = "#858585";
  placeholder.textContent = "Click a message to view details";
  host.appendChild(placeholder);

  // ── prefs ──────────────────────────────────────────────────────────
  let tz = lsGet(LS.tz, browserTz());
  const collapsedSections = lsGet(LS.sections, {});
  let colWidths = lsGet(LS.cols, [50, 170, 160]);
  const MIN_COL_W = 30;
  const collapsedGroups = new Set();

  // ── session_id -> fix_version (live) ───────────────────────────────
  const sessionVersions = new Map();
  let sessionsSubId = null;

  function trackSession(op, row) {
    if (op === "delete") sessionVersions.delete(row.session_id);
    else if (row.session_id) {
      sessionVersions.set(row.session_id, row.dictionary || row.fix_version);
    }
  }

  function subscribeSessions() {
    sessionsSubId = `msg-detail-sessions-${Date.now()}`;
    client.subscribe("sessions_query", "query", {
      subid: sessionsSubId,
      onSnapshot: (rows) => {
        sessionVersions.clear();
        for (const row of rows) trackSession("insert", row);
      },
      onUpdate: (op, row) => trackSession(op, row),
      onDelta: (changes) => {
        for (const { op, row } of changes) trackSession(op, row);
      },
    });
  }

  function unsubscribeSessions() {
    if (sessionsSubId) client.unsubscribe(sessionsSubId);
    sessionsSubId = null;
  }

  subscribeSessions();

  // ── rendering ──────────────────────────────────────────────────────
  let currentMsg = null;
  let currentDict = defaultDictionary;
  let renderToken = 0;

  function versionFor(msg) {
    const v = sessionVersions.get(msg.session_id);
    if (v) return v;
    const begin = splitFix(msg.raw_message).find((p) => p.tag === "8");
    if (begin && begin.value && begin.value !== "FIXT.1.1") return begin.value;
    return "FIX.4.2";
  }

  function translated(tag, value, dict) {
    const type = dict.fieldType(tag);
    if (type === "UTCTIMESTAMP") {
      const t = formatTimestamp(value, tz);
      if (t) return t;
    }
    return dict.enumName(tag, value);
  }

  function fieldRow(n, dict, depth) {
    const pad = depth ? ` style="padding-left:${8 + depth * 16}px"` : "";
    return `<tr>` +
      `<td class="mkfix-detail-tag"${pad}>${escapeHtml(n.tag)}</td>` +
      `<td class="mkfix-detail-name">${escapeHtml(dict.tagName(n.tag))}</td>` +
      `<td class="mkfix-detail-raw">${escapeHtml(n.value)}</td>` +
      `<td class="mkfix-detail-translated">${escapeHtml(translated(n.tag, n.value, dict))}</td>` +
      `</tr>`;
  }

  function groupRows(n, dict, depth, path) {
    const open = !collapsedGroups.has(path);
    const chev = open ? "▾" : "▸";
    const pad = ` style="padding-left:${8 + depth * 16}px"`;
    let html = `<tr class="mkfix-group-row" data-path="${escapeHtml(path)}">` +
      `<td class="mkfix-detail-tag"${pad}>${escapeHtml(n.tag)}</td>` +
      `<td class="mkfix-detail-name"><span class="mkfix-chevron">${chev}</span>${escapeHtml(dict.tagName(n.tag))}</td>` +
      `<td class="mkfix-detail-raw">${escapeHtml(n.value)}</td>` +
      `<td class="mkfix-detail-translated">${n.entries.length} ${n.entries.length === 1 ? "entry" : "entries"}</td>` +
      `</tr>`;
    if (open) {
      n.entries.forEach((nodes, i) => {
        if (n.entries.length > 1) {
          html += `<tr class="mkfix-group-instance"><td colspan="4" style="padding-left:${8 + (depth + 1) * 16}px">[${i + 1}]</td></tr>`;
        }
        for (const child of nodes) html += renderNode(child, dict, depth + 1, `${path}.${i}`);
      });
    }
    return html;
  }

  function renderNode(n, dict, depth, path) {
    if (n.kind === "group") return groupRows(n, dict, depth, `${path}.${n.tag}`);
    return fieldRow(n, dict, depth);
  }

  const SECTION_COLORS = { header: "#569cd6", body: "#d4d4d4", trailer: "#ce9178" };

  function render() {
    const msg = currentMsg;
    if (!msg || !msg.raw_message) {
      host.innerHTML = "";
      host.appendChild(placeholder);
      return;
    }
    const dict = currentDict;

    const { fields } = parseRawMessage(msg.raw_message);
    const summary = summarizeMessage(fields, dict);
    const tree = parseMessageTree(msg.raw_message, dict);

    const sections = { header: [], body: [], trailer: [] };
    for (const n of tree) {
      const sec = dict.isHeader(n.tag) ? "header" : dict.isTrailer(n.tag) ? "trailer" : "body";
      sections[sec].push(n);
    }

    const dirColor = msg.direction === "TX" ? "#4ec9b0" : "#569cd6";
    const dirArrow = msg.direction === "TX" ? "↑ SENT" : "↓ RECEIVED";
    const ts = formatTimestamp(msg.timestamp || "", tz) || msg.timestamp || "";

    let html = `<div class="mkfix-detail-header">` +
      `<div><span style="color:${dirColor};font-weight:bold">${dirArrow}</span> ` +
      `<span style="color:#dcdcaa">${escapeHtml(msg.msg_type_name || dict.msgTypeName(msg.msg_type) || msg.msg_type)}</span>` +
      `<span style="color:#858585;margin-left:8px">${escapeHtml(dict.version || "")}</span></div>` +
      `<div style="color:#d4d4d4;margin:4px 0">${escapeHtml(summary)}</div>` +
      `<div style="color:#858585">Session: ${escapeHtml(msg.session_id || "")} | Seq: ${escapeHtml(String(msg.seq_num ?? ""))} | ${escapeHtml(ts)}</div>` +
      `</div>`;

    html += `<div class="mkfix-detail-toolbar">` +
      `<label>Timezone</label>` +
      `<select class="mkfix-tz-select">` +
      timezoneList().map((z) =>
        `<option value="${escapeHtml(z)}"${z === tz ? " selected" : ""}>${escapeHtml(z)}</option>`
      ).join("") +
      `</select></div>`;

    html += `<table class="mkfix-detail-table" style="table-layout:fixed;margin-top:4px">`;
    html += `<colgroup>` +
      colWidths.map((w) => `<col style="width:${w}px">`).join("") +
      `<col></colgroup>`;
    html += `<thead><tr>` +
      ["Tag", "Name", "Raw", "Translated"].map((h, i) =>
        `<th>${h}${i < 3 ? `<span class="mkfix-col-resizer" data-col="${i}" title="Drag to resize, double-click to fit"></span>` : ""}</th>`
      ).join("") +
      `</tr></thead><tbody>`;

    for (const sec of ["header", "body", "trailer"]) {
      if (!sections[sec].length) continue;
      const open = !collapsedSections[sec];
      const chev = open ? "▾" : "▸";
      html += `<tr class="mkfix-section-row" data-sec="${sec}">` +
        `<td colspan="4" style="color:${SECTION_COLORS[sec]}">` +
        `<span class="mkfix-chevron">${chev}</span>${sec.toUpperCase()}</td></tr>`;
      if (open) {
        sections[sec].forEach((n, i) => {
          html += renderNode(n, currentDict, 0, `${sec}.${i}`);
        });
      }
    }
    html += "</tbody></table>";

    host.innerHTML = html;
  }

  async function show(msg) {
    currentMsg = msg;
    collapsedGroups.clear();
    if (!msg || !msg.raw_message) {
      render();
      return;
    }
    const token = ++renderToken;
    currentDict = defaultDictionary;
    render();
    const dict = await loadDictionary(versionFor(msg));
    if (token !== renderToken) return;
    currentDict = dict;
    render();
  }

  app.state.subscribe("selected_message", (msg) => {
    show(msg);
  });

  // ── interactions (host survives re-renders; delegate everything) ───
  host.addEventListener("click", (e) => {
    const secRow = e.target.closest(".mkfix-section-row");
    if (secRow) {
      const sec = secRow.dataset.sec;
      collapsedSections[sec] = !collapsedSections[sec];
      lsSet(LS.sections, collapsedSections);
      render();
      return;
    }
    const groupRow = e.target.closest(".mkfix-group-row");
    if (groupRow) {
      const path = groupRow.dataset.path;
      if (collapsedGroups.has(path)) collapsedGroups.delete(path);
      else collapsedGroups.add(path);
      render();
    }
  });

  host.addEventListener("change", (e) => {
    if (e.target.classList.contains("mkfix-tz-select")) {
      tz = e.target.value;
      lsSet(LS.tz, tz);
      render();
    }
  });

  const COL_CLASSES = ["mkfix-detail-tag", "mkfix-detail-name", "mkfix-detail-raw"];

  function colElements() {
    const table = host.querySelector(".mkfix-detail-table");
    return table ? table.querySelectorAll("colgroup col") : [];
  }

  function setColWidth(col, w, cols = colElements()) {
    colWidths[col] = w;
    if (cols[col]) cols[col].style.width = `${w}px`;
  }

  // Pointer events like mkio-table's grips; stopPropagation keeps the frame
  // from treating the press as a tab-group click and a drag start.
  host.addEventListener("pointerdown", (e) => {
    const resizer = e.target.closest(".mkfix-col-resizer");
    if (!resizer || e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const col = parseInt(resizer.dataset.col, 10);
    const pid = e.pointerId;
    const startX = e.clientX;
    const startW = colWidths[col];
    const cols = colElements();

    function onMove(ev) {
      if (ev.pointerId !== pid) return;
      setColWidth(col, Math.max(MIN_COL_W, startW + (ev.clientX - startX)), cols);
    }
    function onUp(ev) {
      if (ev.pointerId !== pid) return;
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      document.removeEventListener("pointercancel", onUp);
      lsSet(LS.cols, colWidths);
    }
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
    document.addEventListener("pointercancel", onUp);
  });

  // Double-click a divider: fit the column to its widest cell. Cells clip
  // with overflow hidden, so scrollWidth is the content plus padding.
  host.addEventListener("dblclick", (e) => {
    const resizer = e.target.closest(".mkfix-col-resizer");
    if (!resizer) return;
    e.preventDefault();
    e.stopPropagation();
    const col = parseInt(resizer.dataset.col, 10);
    let w = MIN_COL_W;
    for (const cell of host.querySelectorAll(`.${COL_CLASSES[col]}`))
      w = Math.max(w, cell.scrollWidth + 1);
    setColWidth(col, Math.min(w, Math.ceil(host.clientWidth * 0.8)));
    lsSet(LS.cols, colWidths);
  });

  // Panes are pooled: closing a frame parks the pane and showPane() re-hosts
  // it without re-running this factory, so re-subscribe on reopen.
  const paneEl = host.closest("mkui-pane");
  paneEl?.addEventListener("mkui-pane-close", unsubscribeSessions);
  paneEl?.addEventListener("mkui-pane-open", () => {
    if (!sessionsSubId) subscribeSessions();
  });
});
