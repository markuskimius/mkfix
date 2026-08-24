/**
 * Message Detail pane: shows field-by-field breakdown of a selected FIX message.
 *
 * - Translates through the owning session's dictionary (fix_version), falling
 *   back to the message's BeginString, then FIX.4.2.
 * - Repeating groups render as collapsible sub-blocks using the dictionary's
 *   group metadata (display-only; the engine has no group model).
 * - Header/Body/Trailer sections collapse; the choice persists per browser.
 * - Columns are drag-resizable; widths persist per browser.
 * - UTC timestamps render in a selectable timezone (default: browser's).
 */

import { loadDictionary, defaultDictionary, parseMessageTree } from "../fix-dictionary.js";
import { summarizeMessage, parseRawMessage } from "../fix-formatter.js";
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

const TS_RE = /^(\d{4})(\d{2})(\d{2})-(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?$/;

/** Format a FIX UTC timestamp (YYYYMMDD-HH:MM:SS[.mmm]) in the given zone. */
function formatTimestamp(value, tz) {
  const m = TS_RE.exec(value);
  if (!m) return null;
  const ms = m[7] ? m[7].padEnd(3, "0").slice(0, 3) : "000";
  const date = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6], +ms));
  if (Number.isNaN(date.getTime())) return null;
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: tz,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false, timeZoneName: "short",
    }).formatToParts(date);
    const p = {};
    for (const { type, value: v } of parts) p[type] = v;
    const frac = m[7] ? `.${ms}` : "";
    return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}${frac} ${p.timeZoneName}`;
  } catch {
    return null;
  }
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
    const raw = msg.raw_message || "";
    const m = /(?:^|[|\x01])8=([^|\x01]+)/.exec(raw);
    if (m && m[1] && m[1] !== "FIXT.1.1") return m[1];
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
        `<th>${h}${i < 3 ? `<span class="mkfix-col-resizer" data-col="${i}"></span>` : ""}</th>`
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

  host.addEventListener("mousedown", (e) => {
    const resizer = e.target.closest(".mkfix-col-resizer");
    if (!resizer) return;
    e.preventDefault();
    const col = parseInt(resizer.dataset.col, 10);
    const startX = e.clientX;
    const startW = colWidths[col];
    const table = host.querySelector(".mkfix-detail-table");
    const cols = table ? table.querySelectorAll("colgroup col") : [];

    function onMove(ev) {
      const w = Math.max(30, startW + (ev.clientX - startX));
      colWidths[col] = w;
      if (cols[col]) cols[col].style.width = `${w}px`;
    }
    function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      lsSet(LS.cols, colWidths);
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });

  // Panes are pooled: closing a frame parks the pane and showPane() re-hosts
  // it without re-running this factory, so re-subscribe on reopen.
  const paneEl = host.closest("mkui-pane");
  paneEl?.addEventListener("mkui-pane-close", unsubscribeSessions);
  paneEl?.addEventListener("mkui-pane-open", () => {
    if (!sessionsSubId) subscribeSessions();
  });
});
