/**
 * Raw Message Viewer pane: shows FIX messages in tag=value format.
 */

import { ensureMkio } from "/mkui/src/mkio-bridge.js";

const { registerPaneType } = window.Mkui;

registerPaneType("raw-messages", async (spec, app, host) => {
  const wsUrl = app.config?.mkio?.url;
  const client = await ensureMkio(wsUrl);

  host.innerHTML = "";
  host.style.display = "flex";
  host.style.flexDirection = "column";
  host.style.overflow = "hidden";

  // ── Filter bar ──
  const filterBar = document.createElement("div");
  filterBar.className = "mkfix-toolbar";
  filterBar.innerHTML = `
    <label class="mkfix-filter-label">Session:</label>
    <select class="mkfix-select mkfix-filter-select" data-filter="session_id">
      <option value="">All</option>
    </select>
    <label class="mkfix-filter-label">Dir:</label>
    <select class="mkfix-select mkfix-filter-select" data-filter="direction">
      <option value="">All</option>
      <option value="TX">TX</option>
      <option value="RX">RX</option>
    </select>
    <label class="mkfix-filter-label">Category:</label>
    <select class="mkfix-select mkfix-filter-select" data-filter="category">
      <option value="">All</option>
      <option value="ADMIN">Admin</option>
      <option value="APP">App</option>
    </select>
    <label class="mkfix-filter-label" style="margin-left:auto">
      <input type="checkbox" class="mkfix-autoscroll" checked> Auto-scroll
    </label>
  `;
  host.appendChild(filterBar);

  // ── Message list ──
  const listWrap = document.createElement("div");
  listWrap.className = "mkfix-message-list";
  listWrap.style.flex = "1";
  listWrap.style.overflow = "auto";
  listWrap.style.fontFamily = "monospace";
  listWrap.style.fontSize = "12px";
  host.appendChild(listWrap);

  const messageEls = [];
  const sessions = new Set();
  let autoScroll = true;
  let filterSession = "";
  let filterDirection = "";
  let filterCategory = "";

  const autoscrollCb = filterBar.querySelector(".mkfix-autoscroll");
  autoscrollCb.addEventListener("change", () => { autoScroll = autoscrollCb.checked; });

  // ── Filters ──
  filterBar.addEventListener("change", (e) => {
    const f = e.target.dataset?.filter;
    if (!f) return;
    if (f === "session_id") filterSession = e.target.value;
    if (f === "direction") filterDirection = e.target.value;
    if (f === "category") filterCategory = e.target.value;
    applyFilters();
  });

  function applyFilters() {
    for (const el of messageEls) {
      const d = el._data;
      const show =
        (!filterSession || d.session_id === filterSession) &&
        (!filterDirection || d.direction === filterDirection) &&
        (!filterCategory || d.category === filterCategory);
      el.style.display = show ? "" : "none";
    }
  }

  function updateSessionFilter() {
    const sel = filterBar.querySelector("[data-filter=session_id]");
    const current = sel.value;
    const opts = ['<option value="">All</option>'];
    for (const s of [...sessions].sort()) {
      opts.push(`<option value="${s}">${s}</option>`);
    }
    sel.innerHTML = opts.join("");
    sel.value = current;
  }

  // ── Subscribe to message stream ──
  const protocol = spec.protocol || "stream";
  const service = spec.service || "messages_stream";
  const subid = `raw-msg-${Date.now()}`;

  client.subscribe(service, protocol, {
    subid, ref: "0",
    onSnapshot: (rows) => {
      listWrap.innerHTML = "";
      messageEls.length = 0;
      for (const row of rows) addMessage(row);
      if (autoScroll) listWrap.scrollTop = listWrap.scrollHeight;
    },
    onUpdate: (op, row) => {
      if (op !== "delete") addMessage(row);
    },
    onDelta: (changes) => {
      for (const { op, row } of changes) {
        if (op !== "delete") addMessage(row);
      }
    },
  });

  function addMessage(row) {
    if (row.session_id) {
      if (!sessions.has(row.session_id)) {
        sessions.add(row.session_id);
        updateSessionFilter();
      }
    }

    const el = document.createElement("div");
    el.className = "mkfix-raw-msg";
    el._data = row;

    const dirColor = row.direction === "TX" ? "#4ec9b0" : "#569cd6";
    const dirArrow = row.direction === "TX" ? "↑" : "↓";

    el.innerHTML =
      `<span class="mkfix-msg-time">${row.timestamp || ""}</span>` +
      `<span class="mkfix-msg-dir" style="color:${dirColor}">${dirArrow}${row.direction}</span>` +
      `<span class="mkfix-msg-seq">${row.seq_num || ""}</span>` +
      `<span class="mkfix-msg-type">${row.msg_type_name || row.msg_type || ""}</span>` +
      `<span class="mkfix-msg-raw">${escapeHtml(row.raw_message || "")}</span>`;

    el.addEventListener("click", () => {
      for (const m of messageEls) m.classList.remove("selected");
      el.classList.add("selected");
      app.state.set("selected_message", row);
    });

    // Apply current filters
    const show =
      (!filterSession || row.session_id === filterSession) &&
      (!filterDirection || row.direction === filterDirection) &&
      (!filterCategory || row.category === filterCategory);
    el.style.display = show ? "" : "none";

    messageEls.push(el);
    listWrap.appendChild(el);

    if (autoScroll) listWrap.scrollTop = listWrap.scrollHeight;
  }
});

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
