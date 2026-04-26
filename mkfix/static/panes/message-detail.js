/**
 * Message Detail pane: shows field-by-field breakdown of a selected FIX message.
 */

import { translateMessage, msgTypeName } from "../fix-dictionary.js";
import { summarizeMessage, parseRawMessage } from "../fix-formatter.js";

const { registerPaneType } = window.Mkui;

registerPaneType("message-detail", async (spec, app, host) => {
  host.innerHTML = "";
  host.style.overflow = "auto";
  host.style.padding = "8px";
  host.style.fontSize = "12px";

  const placeholder = document.createElement("div");
  placeholder.style.color = "#858585";
  placeholder.textContent = "Click a message to view details";
  host.appendChild(placeholder);

  app.state.subscribe("selected_message", (msg) => {
    if (!msg || !msg.raw_message) {
      host.innerHTML = "";
      host.appendChild(placeholder);
      return;
    }

    const { fields } = parseRawMessage(msg.raw_message);
    const summary = summarizeMessage(fields);
    const translated = translateMessage(msg.raw_message);

    const dirColor = msg.direction === "TX" ? "#4ec9b0" : "#569cd6";
    const dirArrow = msg.direction === "TX" ? "↑ SENT" : "↓ RECEIVED";

    let html = `<div class="mkfix-detail-header">` +
      `<div><span style="color:${dirColor};font-weight:bold">${dirArrow}</span> ` +
      `<span style="color:#dcdcaa">${msg.msg_type_name || msgTypeName(msg.msg_type) || msg.msg_type}</span></div>` +
      `<div style="color:#d4d4d4;margin:4px 0">${escapeHtml(summary)}</div>` +
      `<div style="color:#858585">Session: ${msg.session_id || ""} | Seq: ${msg.seq_num || ""} | ${msg.timestamp || ""}</div>` +
      `</div>`;

    html += '<table class="mkfix-detail-table" style="margin-top:8px">';
    html += `<tr><th>Tag</th><th>Name</th><th>Raw</th><th>Translated</th></tr>`;

    let currentSection = "";
    for (const field of translated) {
      if (field.section !== currentSection) {
        currentSection = field.section;
        const sectionColor =
          currentSection === "header" ? "#569cd6" :
          currentSection === "trailer" ? "#ce9178" : "#d4d4d4";
        html += `<tr class="mkfix-section-row"><td colspan="4" style="color:${sectionColor};font-weight:bold;padding:6px 0 2px;border-bottom:1px solid #3c3c3c">${currentSection.toUpperCase()}</td></tr>`;
      }
      html += `<tr>` +
        `<td class="mkfix-detail-tag">${field.tag}</td>` +
        `<td class="mkfix-detail-name">${escapeHtml(field.name)}</td>` +
        `<td class="mkfix-detail-raw">${escapeHtml(field.value)}</td>` +
        `<td class="mkfix-detail-translated">${escapeHtml(field.translated)}</td>` +
        `</tr>`;
    }
    html += "</table>";

    host.innerHTML = html;
  });
});

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
