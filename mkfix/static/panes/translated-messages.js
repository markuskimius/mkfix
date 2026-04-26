/**
 * Translated Message Viewer pane: shows FIX messages with human-readable field names.
 */

import { ensureMkio } from "/mkui/src/mkio-bridge.js";
import { translateMessage, msgTypeName } from "../fix-dictionary.js";
import { summarizeMessage, parseRawMessage } from "../fix-formatter.js";

const { registerPaneType } = window.Mkui;

registerPaneType("translated-messages", async (spec, app, host) => {
  const wsUrl = app.config?.mkio?.url;
  const client = await ensureMkio(wsUrl);

  host.innerHTML = "";
  host.style.display = "flex";
  host.style.flexDirection = "column";
  host.style.overflow = "hidden";

  const listWrap = document.createElement("div");
  listWrap.className = "mkfix-message-list";
  listWrap.style.flex = "1";
  listWrap.style.overflow = "auto";
  listWrap.style.fontSize = "12px";
  host.appendChild(listWrap);

  const messageEls = [];
  let autoScroll = true;

  const protocol = spec.protocol || "stream";
  const service = spec.service || "messages_stream";
  const subid = `trans-msg-${Date.now()}`;

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
    const el = document.createElement("div");
    el.className = "mkfix-trans-msg";
    el._data = row;

    const dirColor = row.direction === "TX" ? "#4ec9b0" : "#569cd6";
    const dirArrow = row.direction === "TX" ? "↑" : "↓";

    const { fields } = parseRawMessage(row.raw_message || "");
    const summary = summarizeMessage(fields);

    el.innerHTML =
      `<div class="mkfix-trans-header">` +
        `<span class="mkfix-msg-time">${row.timestamp || ""}</span>` +
        `<span class="mkfix-msg-dir" style="color:${dirColor}">${dirArrow}${row.direction}</span>` +
        `<span class="mkfix-msg-seq">${row.seq_num || ""}</span>` +
        `<span class="mkfix-trans-summary">${escapeHtml(summary)}</span>` +
        `<span class="mkfix-expand-btn">▶</span>` +
      `</div>` +
      `<div class="mkfix-trans-detail" style="display:none"></div>`;

    const headerDiv = el.querySelector(".mkfix-trans-header");
    const detailDiv = el.querySelector(".mkfix-trans-detail");
    const expandBtn = el.querySelector(".mkfix-expand-btn");

    headerDiv.addEventListener("click", (e) => {
      const showing = detailDiv.style.display !== "none";
      detailDiv.style.display = showing ? "none" : "";
      expandBtn.textContent = showing ? "▶" : "▼";

      if (!showing && !detailDiv._rendered) {
        detailDiv._rendered = true;
        renderDetail(detailDiv, row.raw_message || "");
      }

      app.state.set("selected_message", row);
    });

    messageEls.push(el);
    listWrap.appendChild(el);

    if (autoScroll) listWrap.scrollTop = listWrap.scrollHeight;
  }

  function renderDetail(container, rawMessage) {
    const translated = translateMessage(rawMessage);
    let currentSection = "";

    let html = '<table class="mkfix-detail-table">';
    for (const field of translated) {
      if (field.section !== currentSection) {
        currentSection = field.section;
        const sectionColor =
          currentSection === "header" ? "#569cd6" :
          currentSection === "trailer" ? "#ce9178" : "#d4d4d4";
        html += `<tr class="mkfix-section-row"><td colspan="4" style="color:${sectionColor};font-weight:bold;padding:4px 0 2px">${currentSection.toUpperCase()}</td></tr>`;
      }
      html += `<tr>` +
        `<td class="mkfix-detail-tag">${field.tag}</td>` +
        `<td class="mkfix-detail-name">${escapeHtml(field.name)}</td>` +
        `<td class="mkfix-detail-raw">${escapeHtml(field.value)}</td>` +
        `<td class="mkfix-detail-translated">${escapeHtml(field.translated)}</td>` +
        `</tr>`;
    }
    html += "</table>";
    container.innerHTML = html;
  }
});

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
