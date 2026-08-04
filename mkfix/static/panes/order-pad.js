/**
 * Order Pad pane: form for sending NewOrderSingle, CancelRequest, CancelReplaceRequest.
 */

import { ensureMkio } from "/mkui/src/mkio-bridge.js";

const { registerPaneType } = window.Mkui;

registerPaneType("order-pad", async (spec, app, host) => {
  const wsUrl = app.config?.mkio?.url;
  const client = await ensureMkio(wsUrl);

  host.innerHTML = "";
  host.style.overflow = "auto";
  host.style.padding = "8px";

  // ── Tabs ──
  const tabs = document.createElement("div");
  tabs.className = "mkfix-pad-tabs";
  tabs.innerHTML = `
    <button class="mkui-btn active" data-tab="new">New Order</button>
    <button class="mkui-btn" data-tab="cancel">Cancel</button>
    <button class="mkui-btn" data-tab="replace">Cancel/Replace</button>
  `;
  host.appendChild(tabs);

  // ── New Order Form ──
  const newForm = buildForm("new", [
    { name: "session_id", label: "Session", type: "select", bindOptions: true },
    { name: "symbol", label: "Symbol", placeholder: "AAPL" },
    { name: "side", label: "Side", type: "select", options: [
      { value: "1", label: "Buy" }, { value: "2", label: "Sell" },
      { value: "5", label: "Sell Short" },
    ]},
    { name: "qty", label: "Quantity", inputType: "number", placeholder: "100" },
    { name: "ord_type", label: "Order Type", type: "select", options: [
      { value: "1", label: "Market" }, { value: "2", label: "Limit" },
      { value: "3", label: "Stop" }, { value: "4", label: "Stop Limit" },
    ]},
    { name: "price", label: "Price", inputType: "number", placeholder: "0.00", step: "0.01" },
    { name: "tif", label: "Time in Force", type: "select", options: [
      { value: "0", label: "Day" }, { value: "1", label: "GTC" },
      { value: "3", label: "IOC" }, { value: "4", label: "FOK" },
      { value: "6", label: "GTD" },
    ]},
    { name: "account", label: "Account", placeholder: "Optional" },
  ]);
  host.appendChild(newForm);

  // ── Cancel Form ──
  const cancelForm = buildForm("cancel", [
    { name: "session_id", label: "Session", type: "select", bindOptions: true },
    { name: "orig_cl_ord_id", label: "Orig ClOrdID", placeholder: "MKFIX-00000001" },
    { name: "symbol", label: "Symbol", placeholder: "AAPL" },
    { name: "side", label: "Side", type: "select", options: [
      { value: "1", label: "Buy" }, { value: "2", label: "Sell" },
    ]},
  ]);
  cancelForm.style.display = "none";
  host.appendChild(cancelForm);

  // ── Cancel/Replace Form ──
  const replaceForm = buildForm("replace", [
    { name: "session_id", label: "Session", type: "select", bindOptions: true },
    { name: "orig_cl_ord_id", label: "Orig ClOrdID", placeholder: "MKFIX-00000001" },
    { name: "symbol", label: "Symbol", placeholder: "AAPL" },
    { name: "side", label: "Side", type: "select", options: [
      { value: "1", label: "Buy" }, { value: "2", label: "Sell" },
    ]},
    { name: "qty", label: "Quantity", inputType: "number", placeholder: "100" },
    { name: "ord_type", label: "Order Type", type: "select", options: [
      { value: "1", label: "Market" }, { value: "2", label: "Limit" },
    ]},
    { name: "price", label: "Price", inputType: "number", placeholder: "0.00", step: "0.01" },
  ]);
  replaceForm.style.display = "none";
  host.appendChild(replaceForm);

  // ── Status area ──
  const status = document.createElement("div");
  status.className = "mkfix-pad-status";
  host.appendChild(status);

  const forms = { new: newForm, cancel: cancelForm, replace: replaceForm };
  let activeTab = "new";

  // ── Tab switching ──
  tabs.addEventListener("click", (e) => {
    const tab = e.target.dataset?.tab;
    if (!tab) return;
    activeTab = tab;
    for (const [k, f] of Object.entries(forms)) f.style.display = k === tab ? "" : "none";
    for (const btn of tabs.querySelectorAll("button")) btn.classList.toggle("active", btn.dataset.tab === tab);
  });

  // ── Populate session dropdowns from live sessions ──
  let sessionSubId = null;
  const sessionOptions = [];

  function subscribe() {
    sessionSubId = `pad-sessions-${Date.now()}`;
    client.subscribe("sessions_query", "query", {
    subid: sessionSubId,
    onSnapshot: (rows) => {
      sessionOptions.length = 0;
      for (const row of rows) sessionOptions.push({ value: row.session_id, label: row.session_id });
      updateSessionSelects();
    },
    onUpdate: (op, row) => {
      if (op === "delete") {
        const idx = sessionOptions.findIndex((o) => o.value === row.session_id);
        if (idx >= 0) sessionOptions.splice(idx, 1);
      } else {
        if (!sessionOptions.find((o) => o.value === row.session_id)) {
          sessionOptions.push({ value: row.session_id, label: row.session_id });
        }
      }
      updateSessionSelects();
    },
    onDelta: (changes) => {
      for (const { op, row } of changes) {
        if (op === "delete") {
          const idx = sessionOptions.findIndex((o) => o.value === row.session_id);
          if (idx >= 0) sessionOptions.splice(idx, 1);
        } else {
          if (!sessionOptions.find((o) => o.value === row.session_id)) {
            sessionOptions.push({ value: row.session_id, label: row.session_id });
          }
        }
      }
      updateSessionSelects();
    },
    });
  }

  subscribe();

  // Panes are pooled: closing a frame parks the pane and showPane() re-hosts
  // it without re-running this factory, so re-subscribe on reopen.
  const paneEl = host.closest("mkui-pane");
  paneEl?.addEventListener("mkui-pane-close", () => {
    if (sessionSubId) client.unsubscribe(sessionSubId);
    sessionSubId = null;
  });
  paneEl?.addEventListener("mkui-pane-open", () => {
    if (!sessionSubId) subscribe();
  });

  function updateSessionSelects() {
    for (const f of Object.values(forms)) {
      const sel = f.querySelector("[name=session_id]");
      if (!sel) continue;
      const cur = sel.value;
      sel.innerHTML = '<option value="">Select session...</option>' +
        sessionOptions.map((o) => `<option value="${o.value}">${o.label}</option>`).join("");
      sel.value = cur;
    }
  }

  // ── Auto-populate from selected order ──
  app.state.subscribe("selected_order", (order) => {
    if (!order) return;
    const sel = cancelForm.querySelector("[name=orig_cl_ord_id]");
    if (sel) sel.value = order.cl_ord_id || "";
    const sym = cancelForm.querySelector("[name=symbol]");
    if (sym) sym.value = order.symbol || "";
  });

  // ── Submit handlers ──
  host.addEventListener("click", async (e) => {
    if (!e.target.classList.contains("mkfix-submit")) return;

    const formEl = forms[activeTab];
    const data = {};
    for (const input of formEl.querySelectorAll("input, select")) {
      if (input.name) data[input.name] = input.value;
    }

    let command;
    if (activeTab === "new") command = "send_new_order";
    else if (activeTab === "cancel") command = "send_cancel";
    else command = "send_cancel_replace";

    try {
      status.textContent = "Sending...";
      status.style.color = "#dcdcaa";
      const result = await client.send("fix_cmd", { command, ...data }, { op: command });
      status.textContent = result.cl_ord_id ? `Sent: ${result.cl_ord_id}` : "Sent";
      status.style.color = "#4ec9b0";
    } catch (err) {
      status.textContent = `Error: ${err.message || err}`;
      status.style.color = "#f44747";
    }
  });

  function buildForm(name, fields) {
    const div = document.createElement("div");
    div.className = "mkfix-order-form";

    for (const f of fields) {
      const row = document.createElement("div");
      row.className = "mkfix-form-row";

      const label = document.createElement("label");
      label.textContent = f.label;
      row.appendChild(label);

      if (f.type === "select") {
        const sel = document.createElement("select");
        sel.name = f.name;
        sel.className = "mkfix-select";
        if (f.options) {
          for (const opt of f.options) {
            const o = document.createElement("option");
            o.value = opt.value;
            o.textContent = opt.label;
            sel.appendChild(o);
          }
        }
        row.appendChild(sel);
      } else {
        const input = document.createElement("input");
        input.name = f.name;
        input.className = "mkfix-input";
        input.type = f.inputType || "text";
        if (f.placeholder) input.placeholder = f.placeholder;
        if (f.step) input.step = f.step;
        row.appendChild(input);
      }

      div.appendChild(row);
    }

    const submitRow = document.createElement("div");
    submitRow.className = "mkfix-form-row";
    const submit = document.createElement("button");
    submit.className = "mkui-btn mkfix-submit";
    submit.textContent = name === "new" ? "Send Order" : name === "cancel" ? "Cancel Order" : "Replace Order";
    submitRow.appendChild(submit);
    div.appendChild(submitRow);

    return div;
  }
});
