/**
 * Session Manager pane: live session table with control toolbar.
 */

import { ensureMkio } from "/mkui/src/mkio-bridge.js";

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

  // ── Form (hidden by default) ──
  const form = document.createElement("div");
  form.className = "mkfix-session-form";
  form.style.display = "none";
  form.innerHTML = `
    <div class="mkfix-form-row">
      <label>Session ID</label><input name="session_id" placeholder="e.g. BBERG-UAT">
      <label>FIX Version</label>
      <select name="fix_version">
        <option value="FIX.4.2">FIX.4.2</option>
        <option value="FIX.4.4">FIX.4.4</option>
      </select>
    </div>
    <div class="mkfix-form-row">
      <label>SenderCompID</label><input name="sender_comp_id" placeholder="OUR_COMP">
      <label>TargetCompID</label><input name="target_comp_id" placeholder="THEIR_COMP">
    </div>
    <div class="mkfix-form-row">
      <label>Host (empty=acceptor)</label><input name="host" placeholder="127.0.0.1">
      <label>Port</label><input name="port" type="number" value="9876">
    </div>
    <div class="mkfix-form-row">
      <label>Heartbeat (s)</label><input name="heartbeat_interval" type="number" value="30">
      <label>Reset on Logon</label>
      <select name="reset_on_logon">
        <option value="0">No</option>
        <option value="1">Yes</option>
      </select>
    </div>
    <div class="mkfix-form-row">
      <label>Description</label><input name="description" style="flex:2" placeholder="Optional description">
    </div>
    <div class="mkfix-form-row">
      <button class="mkui-btn" data-action="save">Save</button>
      <button class="mkui-btn" data-action="cancel-form">Cancel</button>
    </div>
  `;
  host.appendChild(form);

  // ── Table ──
  const tableWrap = document.createElement("div");
  tableWrap.style.flex = "1";
  tableWrap.style.overflow = "auto";
  host.appendChild(tableWrap);

  const table = document.createElement("table");
  table.className = "mkui-table";
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
  let formMode = null; // "new" or "edit"

  // ── Session config subscription ──
  const configSubId = `session-mgr-config-${Date.now()}`;
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

  // ── Session state subscription ──
  const stateSubId = `session-mgr-state-${Date.now()}`;
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
      formMode = "new";
      form.style.display = "";
      form.querySelector("[name=session_id]").disabled = false;
      clearForm();
    } else if (action === "edit") {
      if (!selectedSessionId) return;
      formMode = "edit";
      form.style.display = "";
      form.querySelector("[name=session_id]").disabled = true;
      populateForm(selectedSessionId);
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

  // ── Form actions ──
  form.addEventListener("click", async (e) => {
    const action = e.target.dataset?.action;
    if (!action) return;

    if (action === "cancel-form") {
      form.style.display = "none";
    } else if (action === "save") {
      const data = getFormData();
      if (formMode === "new") {
        await client.send("session_mgmt", data, { op: "add" });
      } else {
        await client.send("session_mgmt", data, { op: "update" });
      }
      form.style.display = "none";
    }
  });

  function clearForm() {
    for (const input of form.querySelectorAll("input, select")) {
      if (input.type === "number") input.value = input.defaultValue || "";
      else if (input.tagName === "SELECT") input.selectedIndex = 0;
      else input.value = "";
    }
  }

  function populateForm(sessionId) {
    for (const [key, tr] of rows) {
      if (tr._data?.session_id === sessionId) {
        const d = tr._data;
        form.querySelector("[name=session_id]").value = d.session_id || "";
        form.querySelector("[name=fix_version]").value = d.fix_version || "FIX.4.2";
        form.querySelector("[name=sender_comp_id]").value = d.sender_comp_id || "";
        form.querySelector("[name=target_comp_id]").value = d.target_comp_id || "";
        form.querySelector("[name=host]").value = d.host || "";
        form.querySelector("[name=port]").value = d.port || 9876;
        form.querySelector("[name=heartbeat_interval]").value = d.heartbeat_interval || 30;
        form.querySelector("[name=reset_on_logon]").value = d.reset_on_logon ? "1" : "0";
        form.querySelector("[name=description]").value = d.description || "";
        return;
      }
    }
  }

  function getFormData() {
    return {
      session_id: form.querySelector("[name=session_id]").value,
      fix_version: form.querySelector("[name=fix_version]").value,
      sender_comp_id: form.querySelector("[name=sender_comp_id]").value,
      target_comp_id: form.querySelector("[name=target_comp_id]").value,
      host: form.querySelector("[name=host]").value,
      port: parseInt(form.querySelector("[name=port]").value) || 9876,
      heartbeat_interval: parseInt(form.querySelector("[name=heartbeat_interval]").value) || 30,
      reset_on_logon: parseInt(form.querySelector("[name=reset_on_logon]").value) || 0,
      description: form.querySelector("[name=description]").value,
    };
  }
});
