/**
 * Session Manager pane: live session table with control toolbar.
 */

import { ensureMkio } from "/mkui/src/mkio-bridge.js";
import { openDialog } from "/mkui/src/widgets/mkui-dialog.js";

const { registerPaneType } = window.Mkui;

const STATUS_COLORS = {
  ACTIVE: "#4ec9b0",
  LISTENING: "#dcdcaa",
  INITIATING: "#dcdcaa",
  LOGON_SENT: "#dcdcaa",
  DOWN: "#858585",
  ERROR: "#f44747",
  LOGOUT_SENT: "#ce9178",
};

registerPaneType("session-manager", async (spec, app, host) => {
  const wsUrl = app.config?.mkio?.url;
  const client = await ensureMkio(wsUrl);

  host.innerHTML = "";
  host.style.display = "flex";
  host.style.flexDirection = "column";
  host.style.overflow = "hidden";

  // ── Toolbar ──
  const toolbar = document.createElement("div");
  toolbar.className = "mkfix-toolbar";
  toolbar.innerHTML = `
    <button class="mkui-btn" data-action="new">New</button>
    <button class="mkui-btn" data-action="edit">Edit</button>
    <button class="mkui-btn" data-action="start">Start</button>
    <button class="mkui-btn" data-action="stop">Stop</button>
    <button class="mkui-btn" data-action="delete">Delete</button>
    <button class="mkui-btn" data-action="reset">Reset Seq</button>
  `;
  host.appendChild(toolbar);

  // ── Table ──
  const tableWrap = document.createElement("div");
  tableWrap.style.flex = "1";
  tableWrap.style.overflow = "auto";
  host.appendChild(tableWrap);

  const table = document.createElement("table");
  table.className = "mkui-table mkfix-table";
  tableWrap.appendChild(table);

  const thead = document.createElement("thead");
  const columns = ["session_id", "fix_version", "sender_comp_id", "target_comp_id", "host", "port", "status", "tx_seq_num", "rx_seq_num"];
  const headerLabels = ["Session", "Version", "Sender", "Target", "Host", "Port", "Status", "TX Seq", "RX Seq"];
  thead.innerHTML = "<tr>" + headerLabels.map((h) => `<th>${h}</th>`).join("") + "</tr>";
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  table.appendChild(tbody);

  const rows = new Map();
  const stateBySession = new Map();
  let selectedSessionId = null;

  let configSubId = null;
  let stateSubId = null;

  function subscribeAll() {
    subscribeConfig();
    subscribeState();
  }

  function unsubscribeAll() {
    if (configSubId) client.unsubscribe(configSubId);
    if (stateSubId) client.unsubscribe(stateSubId);
    configSubId = null;
    stateSubId = null;
  }

  // ── Session config subscription ──
  function subscribeConfig() {
    configSubId = `session-mgr-config-${Date.now()}`;
    client.subscribe("sessions_query", "query", {
    subid: configSubId,
    onSnapshot: (snapRows) => {
      rows.clear();
      tbody.innerHTML = "";
      for (const row of snapRows) renderRow(row);
    },
    onUpdate: (op, row) => {
      if (op === "delete") {
        const el = rows.get(row._mkio_row);
        if (el) { el.remove(); rows.delete(row._mkio_row); }
      } else {
        renderRow(row);
      }
    },
    onDelta: (changes) => {
      for (const { op, row } of changes) {
        if (op === "delete") {
          const el = rows.get(row._mkio_row);
          if (el) { el.remove(); rows.delete(row._mkio_row); }
        } else {
          renderRow(row);
        }
      }
    },
    });
  }

  // ── Session state subscription ──
  function subscribeState() {
    stateSubId = `session-mgr-state-${Date.now()}`;
    client.subscribe("session_state_query", "query", {
    subid: stateSubId,
    onSnapshot: (snapRows) => {
      for (const row of snapRows) {
        stateBySession.set(row.session_id, row);
        updateRowState(row.session_id);
      }
    },
    onUpdate: (op, row) => {
      stateBySession.set(row.session_id, row);
      updateRowState(row.session_id);
    },
    onDelta: (changes) => {
      for (const { op, row } of changes) {
        stateBySession.set(row.session_id, row);
        updateRowState(row.session_id);
      }
    },
    });
  }

  subscribeAll();

  function renderRow(data) {
    const key = data._mkio_row || data.session_id;
    let tr = rows.get(key);
    if (!tr) {
      tr = document.createElement("tr");
      tr.addEventListener("click", () => selectRow(key, data.session_id));
      tbody.appendChild(tr);
      rows.set(key, tr);
    }
    tr._data = data;
    const state = stateBySession.get(data.session_id) || {};
    const merged = { ...data, ...state };
    tr.innerHTML = columns.map((col) => {
      const val = merged[col] ?? "";
      if (col === "status") {
        const color = STATUS_COLORS[val] || "#858585";
        return `<td style="color:${color};font-weight:bold">${val}</td>`;
      }
      return `<td>${val}</td>`;
    }).join("");
  }

  function updateRowState(sessionId) {
    for (const [key, tr] of rows) {
      if (tr._data?.session_id === sessionId) {
        renderRow(tr._data);
        break;
      }
    }
  }

  function selectRow(key, sessionId) {
    selectedSessionId = sessionId;
    for (const [k, tr] of rows) {
      tr.classList.toggle("selected", k === key);
    }
    app.state.set("selected_session", sessionId);
  }

  // ── Toolbar actions ──
  toolbar.addEventListener("click", async (e) => {
    const action = e.target.dataset?.action;
    if (!action) return;

    if (action === "new") {
      openSessionDialog(null);
    } else if (action === "edit") {
      if (!selectedSessionId) return;
      const data = findSessionData(selectedSessionId);
      if (data) openSessionDialog(data);
    } else if (action === "start" && selectedSessionId) {
      await client.send("fix_cmd", { command: "start_session", session_id: selectedSessionId }, { op: "start_session" });
    } else if (action === "stop" && selectedSessionId) {
      await client.send("fix_cmd", { command: "stop_session", session_id: selectedSessionId }, { op: "stop_session" });
    } else if (action === "delete" && selectedSessionId) {
      await client.send("session_mgmt", { session_id: selectedSessionId }, { op: "delete" });
      selectedSessionId = null;
    } else if (action === "reset" && selectedSessionId) {
      await client.send("fix_cmd", { command: "reset_sequence", session_id: selectedSessionId, tx_seq_num: 1, rx_seq_num: 1 }, { op: "reset_sequence" });
    }
  });

  function findSessionData(sessionId) {
    for (const tr of rows.values()) {
      if (tr._data?.session_id === sessionId) return tr._data;
    }
    return null;
  }

  // d == null → New; otherwise Edit of the given session row.
  function openSessionDialog(d) {
    const isEdit = d != null;
    const fields = isEdit
      ? [
          { label: "Session ID", type: "readonly", value: d.session_id },
          { name: "session_id", type: "hidden", value: d.session_id },
          { name: "enabled", type: "hidden", value: d.enabled ?? 1 },
        ]
      : [
          { name: "session_id", label: "Session ID", type: "text", placeholder: "e.g. BBERG-UAT", required: true },
        ];
    fields.push(
      { name: "fix_version", label: "FIX Version", type: "select", value: d?.fix_version ?? "FIX.4.2", options: [
        { value: "FIX.4.2", label: "FIX.4.2" },
        { value: "FIX.4.4", label: "FIX.4.4" },
      ] },
      { row: [
        { name: "sender_comp_id", label: "SenderCompID", type: "text", value: d?.sender_comp_id ?? "", placeholder: "OUR_COMP", width: 0.5, required: true },
        { name: "target_comp_id", label: "TargetCompID", type: "text", value: d?.target_comp_id ?? "", placeholder: "THEIR_COMP", width: 0.5, required: true },
      ] },
      { row: [
        { name: "host", label: "Host (empty=acceptor)", type: "text", value: d?.host ?? "", placeholder: "127.0.0.1", width: 0.5 },
        { name: "port", label: "Port", type: "number", value: d?.port ?? 9876, min: 1, width: 0.5 },
      ] },
      { row: [
        { name: "heartbeat_interval", label: "Heartbeat (s)", type: "number", value: d?.heartbeat_interval ?? 30, min: 1, width: 0.5 },
        { name: "reset_on_logon", label: "Reset on Logon", type: "select", value: String(d?.reset_on_logon ?? 0), width: 0.5, options: [
          { value: "0", label: "No" },
          { value: "1", label: "Yes" },
        ] },
      ] },
      { name: "description", label: "Description", type: "text", value: d?.description ?? "", placeholder: "Optional description" },
    );

    openDialog({
      title: isEdit ? `Edit Session ${d.session_id}` : "New Session",
      modal: true,
      width: 440,
      fields,
      submit: { label: "Save", service: "session_mgmt", op: isEdit ? "update" : "add" },
    }, { state: app.state.get() }, app, { client });
  }

  // Panes are pooled: closing a frame parks the pane and showPane() re-hosts
  // it without re-running this factory, so re-subscribe on reopen.
  const paneEl = host.closest("mkui-pane");
  paneEl?.addEventListener("mkui-pane-close", unsubscribeAll);
  paneEl?.addEventListener("mkui-pane-open", () => {
    if (!configSubId) subscribeAll();
  });
});
