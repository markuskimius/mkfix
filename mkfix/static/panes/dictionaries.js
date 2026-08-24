/**
 * Dictionaries pane — manage FIX dictionaries.
 *
 * Standard dictionaries (shipped, read-only) and custom dictionaries: either
 * a delta over a standard base ("derived" — stays linked, edits are stored as
 * the difference) or a standalone full document. Supports create, clone,
 * flatten (derived → standalone), edit, delete, and JSON import/export.
 * Edits go through fix_cmd save_dictionary, so the engine registry and every
 * other browser resolve the new content immediately; running sessions pick it
 * up on restart.
 */

import {
  STANDARD_VERSIONS, loadDictionary, invalidateDictionary,
  mergeDictionary, dictionaryToDoc,
} from "../fix-dictionary.js";
import { ensureMkio } from "/mkui/src/mkio-bridge.js";

const { registerPaneType } = window.Mkui;

const TABS = ["fields", "enums", "messages", "groups", "wire"];
const ROW_CAP = 300;

registerPaneType("dictionaries", async (spec, app, host) => {
  const client = await ensureMkio(app.config?.mkio?.url);

  host.innerHTML = `
    <div class="mkfix-dict-pane">
      <div class="mkfix-toolbar">
        <select data-role="dict-select" style="min-width:160px"></select>
        <span data-role="kind-badge" class="mkfix-dict-badge"></span>
        <button class="mkui-btn" data-action="new">New</button>
        <button class="mkui-btn" data-action="clone">Clone</button>
        <button class="mkui-btn" data-action="flatten">Flatten</button>
        <button class="mkui-btn" data-action="delete">Delete</button>
        <button class="mkui-btn" data-action="import">Import</button>
        <button class="mkui-btn" data-action="export">Export</button>
        <button class="mkui-btn" data-action="export-delta">Export Delta</button>
        <button class="mkui-btn mkfix-submit" data-action="save" disabled>Save</button>
        <input type="file" accept=".json,application/json" style="display:none" data-role="file-input">
      </div>
      <div class="mkfix-dict-newform" data-role="new-form" style="display:none">
        <label>Name <input type="text" data-role="new-name" placeholder="MY_DICT"></label>
        <label data-role="new-base-wrap">Base
          <select data-role="new-base">
            ${STANDARD_VERSIONS.map((v) => `<option value="${v}"${v === "FIX.4.2" ? " selected" : ""}>${v}</option>`).join("")}
          </select>
        </label>
        <label data-role="new-mode-wrap"><input type="radio" name="dict-mode" value="linked" checked> Keep linked (delta)</label>
        <label><input type="radio" name="dict-mode" value="full"> Full copy (standalone)</label>
        <button class="mkui-btn mkfix-submit" data-action="create">Create</button>
        <button class="mkui-btn" data-action="cancel-create">Cancel</button>
      </div>
      <div class="mkfix-dict-tabs" data-role="tabs">
        ${TABS.map((t, i) => `<button data-tab="${t}"${i === 0 ? ' class="active"' : ""}>${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}
      </div>
      <div class="mkfix-dict-filterbar">
        <input type="text" data-role="filter" placeholder="Filter...">
        <span data-role="count" style="color:var(--mkui-fg-mute,#858585)"></span>
      </div>
      <div class="mkfix-dict-form" data-role="editor-form"></div>
      <div class="mkfix-dict-removed" data-role="removed"></div>
      <div class="mkfix-dict-body" data-role="body"></div>
      <div class="mkfix-dict-note">Sessions bind a dictionary by name and pick up changes on their next start.</div>
    </div>
  `;
  host.style.overflow = "hidden";
  host.style.display = "flex";
  host.style.flexDirection = "column";

  const $ = (role) => host.querySelector(`[data-role="${role}"]`);
  const dictSelect = $("dict-select");
  const fileInput = $("file-input");

  let list = [];
  let current = null;      // selected name
  let meta = null;         // {kind, base_version, doc} from get_dictionary
  let working = null;      // mutable doc for custom dicts (delta or full)
  let baseDoc = {};        // resolved base document for derived dicts
  let resolved = {};       // what the tabs render
  let dirty = false;
  let tab = "fields";
  let deleteArmed = null;

  const editable = () => meta?.kind === "custom";
  const derived = () => editable() && !!meta.base_version;

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = String(s);
    return d.innerHTML;
  }

  async function cmd(command, data) {
    return client.send("fix_cmd", { command, ...data }, { op: command });
  }

  function recompute() {
    if (working) resolved = mergeDictionary(baseDoc, working);
  }

  function markDirty() {
    dirty = true;
    recompute();
    renderToolbar();
    renderBody();
  }

  // ── data loading ───────────────────────────────────────────────────
  async function refreshList(selectName) {
    const res = await cmd("list_dictionaries", {});
    list = res.dictionaries || [];
    if (selectName) current = selectName;
    if (!list.some((d) => d.name === current)) current = list[0]?.name ?? null;
    dictSelect.innerHTML = list.map((d) =>
      `<option value="${esc(d.name)}"${d.name === current ? " selected" : ""}>` +
      `${esc(d.name)}${d.kind === "custom" ? " *" : ""}</option>`
    ).join("");
    await loadCurrent();
  }

  async function loadCurrent() {
    dirty = false;
    working = null;
    baseDoc = {};
    meta = null;
    resolved = {};
    if (current) {
      const res = await cmd("get_dictionary", { name: current });
      meta = { kind: res.kind, base_version: res.base_version, doc: res.doc };
      if (res.kind === "custom") {
        working = structuredClone(res.doc || {});
        baseDoc = meta.base_version
          ? dictionaryToDoc(await loadDictionary(meta.base_version))
          : {};
        recompute();
      } else {
        resolved = res.dictionary;
      }
    }
    renderToolbar();
    renderBody();
  }

  // ── delta editing helpers (no-ops for standard dictionaries) ───────
  function setEntry(kind, tag, entry) {
    working[kind] = working[kind] || {};
    working[kind][tag] = entry;
    markDirty();
  }

  function removeEntry(kind, tag) {
    working[kind] = working[kind] || {};
    if (derived() && (baseDoc[kind] || {})[tag] !== undefined) {
      working[kind][tag] = null;          // mask the base entry
    } else {
      delete working[kind][tag];          // drop our own addition
    }
    markDirty();
  }

  function revertEntry(kind, tag) {
    if (working[kind]) delete working[kind][tag];
    markDirty();
  }

  function setEnum(tag, code, name) {
    working.enums = working.enums || {};
    working.enums[tag] = working.enums[tag] || {};
    working.enums[tag][code] = name;
    markDirty();
  }

  function removeEnum(tag, code) {
    working.enums = working.enums || {};
    const baseHas = ((baseDoc.enums || {})[tag] || {})[code] !== undefined;
    if (derived() && baseHas) {
      working.enums[tag] = working.enums[tag] || {};
      working.enums[tag][code] = null;
    } else if (working.enums[tag]) {
      delete working.enums[tag][code];
      if (!Object.keys(working.enums[tag]).length) delete working.enums[tag];
    }
    markDirty();
  }

  function isDelta(kind, tag, code) {
    if (!working) return false;
    const entry = (working[kind] || {})[tag];
    if (entry === undefined) return false;
    if (kind === "enums" && code !== undefined) {
      return entry === null || entry[code] !== undefined;
    }
    return true;
  }

  // ── rendering ──────────────────────────────────────────────────────
  function renderToolbar() {
    $("kind-badge").textContent = !meta ? "" :
      meta.kind === "standard" ? "standard (read-only)" :
      derived() ? `custom, delta over ${meta.base_version}` : "custom, standalone";
    const btn = (a) => host.querySelector(`[data-action="${a}"]`);
    btn("save").disabled = !dirty || !editable();
    btn("delete").disabled = !editable();
    btn("delete").textContent = deleteArmed === current ? "Confirm?" : "Delete";
    btn("flatten").disabled = !derived();
    btn("export-delta").disabled = !derived();
    btn("clone").disabled = !current;
    btn("export").disabled = !current;
  }

  function filterText() {
    return $("filter").value.trim().toLowerCase();
  }

  function matches(...parts) {
    const f = filterText();
    if (!f) return true;
    return parts.some((p) => String(p).toLowerCase().includes(f));
  }

  function actionCells(kind, tag) {
    if (!editable()) return "";
    const revert = derived() && isDelta(kind, tag) && (baseDoc[kind] || {})[tag] !== undefined
      ? `<button class="mkui-btn" data-act="revert" data-kind="${kind}" data-tag="${esc(tag)}">Revert</button>` : "";
    return `<td class="mkfix-dict-actions">` +
      `<button class="mkui-btn" data-act="edit" data-kind="${kind}" data-tag="${esc(tag)}">Edit</button>` +
      `<button class="mkui-btn" data-act="remove" data-kind="${kind}" data-tag="${esc(tag)}">Remove</button>` +
      revert + `</td>`;
  }

  function deltaBadge(on) {
    return on ? '<span class="mkfix-delta">Δ</span>' : "";
  }

  function numSort(a, b) {
    const na = parseInt(a, 10), nb = parseInt(b, 10);
    if (Number.isNaN(na) || Number.isNaN(nb)) return String(a).localeCompare(String(b));
    return na - nb;
  }

  function renderRemoved() {
    const el = $("removed");
    el.innerHTML = "";
    if (!working || tab === "wire") return;
    const kinds = tab === "enums" ? ["enums"] : [tab];
    const gone = [];
    for (const kind of kinds) {
      for (const [tag, entry] of Object.entries(working[kind] || {})) {
        if (entry === null) gone.push({ kind, tag });
      }
    }
    if (!gone.length) return;
    el.innerHTML = `Removed: ` + gone.map(({ kind, tag }) =>
      `<span>${esc(tag)} <button class="mkui-btn" data-act="revert" data-kind="${kind}" data-tag="${esc(tag)}">Restore</button></span>`
    ).join(" ");
  }

  function renderForm() {
    const form = $("editor-form");
    if (!editable() || tab === "wire") {
      form.innerHTML = "";
      return;
    }
    const inputs = {
      fields: `<input data-f="tag" placeholder="Tag" size="6"><input data-f="name" placeholder="Name" size="24"><input data-f="type" placeholder="Type" size="14" value="STRING">`,
      enums: `<input data-f="tag" placeholder="Tag" size="6"><input data-f="code" placeholder="Code" size="6"><input data-f="name" placeholder="Name" size="24">`,
      messages: `<input data-f="tag" placeholder="MsgType" size="8"><input data-f="name" placeholder="Name" size="24"><select data-f="category"><option value="app">app</option><option value="admin">admin</option></select>`,
      groups: `<input data-f="tag" placeholder="Counter tag" size="10"><input data-f="delim" placeholder="Delim tag" size="8"><input data-f="members" placeholder="Member tags (space separated)" size="36">`,
    }[tab];
    form.innerHTML = `${inputs}<button class="mkui-btn mkfix-submit" data-act="set">Set</button>`;
  }

  function renderWire() {
    const ro = editable() ? "" : " readonly";
    return `<div class="mkfix-dict-wire">
      <label>BeginString (tag 8) <input data-w="begin_string" value="${esc(resolved.begin_string || "")}"${ro}></label>
      <label>Header tags (ordered) <textarea data-w="header" rows="3"${ro}>${esc((resolved.header || []).join(" "))}</textarea></label>
      <label>Trailer tags (ordered) <textarea data-w="trailer" rows="2"${ro}>${esc((resolved.trailer || []).join(" "))}</textarea></label>
      ${editable() ? '<button class="mkui-btn mkfix-submit" data-act="apply-wire">Apply</button>' : ""}
    </div>`;
  }

  function renderBody() {
    renderForm();
    renderRemoved();
    const body = $("body");
    const count = $("count");

    if (!current) {
      body.innerHTML = "";
      count.textContent = "";
      return;
    }
    if (tab === "wire") {
      count.textContent = "";
      body.innerHTML = renderWire();
      return;
    }

    let rows = [];
    if (tab === "fields") {
      rows = Object.entries(resolved.fields || {})
        .filter(([tag, f]) => matches(tag, f.name, f.type))
        .sort(([a], [b]) => numSort(a, b))
        .map(([tag, f]) =>
          `<tr><td>${esc(tag)}</td><td>${deltaBadge(isDelta("fields", tag))}${esc(f.name)}</td>` +
          `<td>${esc(f.type || "")}</td>${actionCells("fields", tag)}</tr>`);
    } else if (tab === "enums") {
      for (const [tag, values] of Object.entries(resolved.enums || {}).sort(([a], [b]) => numSort(a, b))) {
        const tagName = (resolved.fields || {})[tag]?.name || "";
        for (const [code, name] of Object.entries(values)) {
          if (!matches(tag, tagName, code, name)) continue;
          rows.push(
            `<tr><td>${esc(tag)}</td><td>${esc(tagName)}</td><td>${esc(code)}</td>` +
            `<td>${deltaBadge(isDelta("enums", tag, code))}${esc(name)}</td>` +
            (editable()
              ? `<td class="mkfix-dict-actions">` +
                `<button class="mkui-btn" data-act="edit-enum" data-tag="${esc(tag)}" data-code="${esc(code)}">Edit</button>` +
                `<button class="mkui-btn" data-act="remove-enum" data-tag="${esc(tag)}" data-code="${esc(code)}">Remove</button></td>`
              : ""));
        }
      }
    } else if (tab === "messages") {
      rows = Object.entries(resolved.messages || {})
        .filter(([mt, m]) => matches(mt, m.name, m.category))
        .sort(([a], [b]) => String(a).localeCompare(String(b)))
        .map(([mt, m]) =>
          `<tr><td>${esc(mt)}</td><td>${deltaBadge(isDelta("messages", mt))}${esc(m.name)}</td>` +
          `<td>${esc(m.category || "")}</td>${actionCells("messages", mt)}</tr>`);
    } else if (tab === "groups") {
      rows = Object.entries(resolved.groups || {})
        .filter(([tag, g]) => matches(tag, (resolved.fields || {})[tag]?.name, g.delim))
        .sort(([a], [b]) => numSort(a, b))
        .map(([tag, g]) =>
          `<tr><td>${esc(tag)}</td><td>${deltaBadge(isDelta("groups", tag))}${esc((resolved.fields || {})[tag]?.name || "")}</td>` +
          `<td>${esc(g.delim)}</td><td class="mkfix-dict-members">${esc((g.members || []).join(" "))}</td>` +
          `${actionCells("groups", tag)}</tr>`);
    }

    const total = rows.length;
    const shown = rows.slice(0, ROW_CAP);
    count.textContent = total > shown.length ? `showing ${shown.length} of ${total}` : `${total} rows`;

    const heads = {
      fields: ["Tag", "Name", "Type"],
      enums: ["Tag", "Field", "Code", "Name"],
      messages: ["MsgType", "Name", "Category"],
      groups: ["Counter", "Name", "Delim", "Members"],
    }[tab].concat(editable() ? ["Actions"] : []);

    body.innerHTML = `<table class="mkfix-detail-table"><thead><tr>` +
      heads.map((h) => `<th>${h}</th>`).join("") +
      `</tr></thead><tbody>${shown.join("")}</tbody></table>`;
  }

  // ── toolbar actions ────────────────────────────────────────────────
  let formMode = "new"; // "new" | "clone" | "import"
  let importPayload = null;

  function showNewForm(mode, defaults = {}) {
    formMode = mode;
    const form = $("new-form");
    form.style.display = "";
    $("new-name").value = defaults.name || "";
    $("new-base-wrap").style.display = mode === "new" || defaults.showBase ? "" : "none";
    $("new-mode-wrap").parentElement.querySelectorAll('[name="dict-mode"]').forEach((r) => {
      r.parentElement.style.display = mode === "import" ? "none" : "";
    });
    $("new-name").focus();
  }

  function hideNewForm() {
    $("new-form").style.display = "none";
    importPayload = null;
  }

  async function createFromForm() {
    const name = $("new-name").value.trim();
    if (!name) return;
    const mode = host.querySelector('[name="dict-mode"]:checked')?.value || "linked";
    const base = $("new-base").value;
    try {
      if (formMode === "new") {
        if (mode === "linked") {
          await cmd("save_dictionary", { name, base_version: base, doc: "{}" });
        } else {
          const full = dictionaryToDoc(await loadDictionary(base));
          await cmd("save_dictionary", { name, base_version: "", doc: JSON.stringify(full) });
        }
      } else if (formMode === "clone") {
        if (meta.kind === "custom") {
          await cmd("save_dictionary", {
            name, base_version: meta.base_version, doc: JSON.stringify(working),
          });
        } else if (mode === "linked") {
          await cmd("save_dictionary", { name, base_version: current, doc: "{}" });
        } else {
          await cmd("save_dictionary", { name, base_version: "", doc: JSON.stringify(resolved) });
        }
      } else if (formMode === "import" && importPayload) {
        await cmd("save_dictionary", {
          name,
          base_version: importPayload.base_version || "",
          doc: JSON.stringify(importPayload.doc),
        });
      }
      invalidateDictionary(name);
      hideNewForm();
      await refreshList(name);
    } catch (err) {
      console.error("dictionary save failed:", err);
    }
  }

  function download(filename, obj) {
    const blob = new Blob([JSON.stringify(obj, null, 1)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  host.addEventListener("click", async (e) => {
    const action = e.target.closest("[data-action]")?.dataset.action;
    if (action) {
      if (action !== "delete") deleteArmed = null;
      switch (action) {
        case "new":
          showNewForm("new");
          break;
        case "clone":
          showNewForm("clone", { name: `${current}_COPY`, showBase: false });
          break;
        case "create":
          await createFromForm();
          break;
        case "cancel-create":
          hideNewForm();
          break;
        case "flatten":
          if (derived()) {
            await cmd("save_dictionary", {
              name: current, base_version: "", doc: JSON.stringify(resolved),
            });
            invalidateDictionary(current);
            await refreshList(current);
          }
          break;
        case "delete":
          if (!editable()) break;
          if (deleteArmed !== current) {
            deleteArmed = current;
            renderToolbar();
            break;
          }
          deleteArmed = null;
          try {
            await cmd("delete_dictionary", { name: current });
            invalidateDictionary(current);
            current = null;
            await refreshList();
          } catch (err) {
            console.error("delete failed:", err);
            renderToolbar();
          }
          break;
        case "import":
          fileInput.click();
          break;
        case "export":
          download(`${current}.json`, resolved);
          break;
        case "export-delta":
          download(`${current}.delta.json`, {
            mkfix_delta: true, name: current,
            base_version: meta.base_version, delta: working,
          });
          break;
        case "save":
          if (editable() && dirty) {
            await cmd("save_dictionary", {
              name: current, base_version: meta.base_version, doc: JSON.stringify(working),
            });
            invalidateDictionary(current);
            dirty = false;
            meta.doc = structuredClone(working);
            renderToolbar();
          }
          break;
      }
      return;
    }

    const tabBtn = e.target.closest("[data-tab]");
    if (tabBtn) {
      tab = tabBtn.dataset.tab;
      host.querySelectorAll("[data-tab]").forEach((b) =>
        b.classList.toggle("active", b.dataset.tab === tab));
      $("filter").value = "";
      renderBody();
      return;
    }

    const act = e.target.closest("[data-act]");
    if (!act || !editable()) return;
    const { act: kindAct, kind, tag, code } = act.dataset;
    if (kindAct === "set") {
      const form = $("editor-form");
      const v = (f) => form.querySelector(`[data-f="${f}"]`)?.value.trim() ?? "";
      if (tab === "fields" && v("tag") && v("name")) {
        setEntry("fields", v("tag"), { name: v("name"), type: v("type") || "STRING" });
      } else if (tab === "enums" && v("tag") && v("code")) {
        setEnum(v("tag"), v("code"), v("name"));
      } else if (tab === "messages" && v("tag") && v("name")) {
        setEntry("messages", v("tag"), { name: v("name"), category: v("category") || "app" });
      } else if (tab === "groups" && v("tag") && v("delim")) {
        const members = v("members").split(/[\s,|]+/).filter(Boolean);
        setEntry("groups", v("tag"), { delim: v("delim"), members });
      }
    } else if (kindAct === "edit") {
      const form = $("editor-form");
      const set = (f, val) => { const el = form.querySelector(`[data-f="${f}"]`); if (el) el.value = val; };
      const entry = (resolved[kind] || {})[tag];
      set("tag", tag);
      if (kind === "fields") { set("name", entry?.name || ""); set("type", entry?.type || "STRING"); }
      if (kind === "messages") { set("name", entry?.name || ""); set("category", entry?.category || "app"); }
      if (kind === "groups") { set("delim", entry?.delim || ""); set("members", (entry?.members || []).join(" ")); }
    } else if (kindAct === "edit-enum") {
      const form = $("editor-form");
      const set = (f, val) => { const el = form.querySelector(`[data-f="${f}"]`); if (el) el.value = val; };
      set("tag", tag); set("code", code);
      set("name", ((resolved.enums || {})[tag] || {})[code] || "");
    } else if (kindAct === "remove") {
      removeEntry(kind, tag);
    } else if (kindAct === "remove-enum") {
      removeEnum(tag, code);
    } else if (kindAct === "revert") {
      if (kind === "enums") { if (working.enums) { delete working.enums[tag]; markDirty(); } }
      else revertEntry(kind, tag);
    } else if (kindAct === "apply-wire") {
      const w = (f) => host.querySelector(`[data-w="${f}"]`);
      working.begin_string = w("begin_string").value.trim();
      working.header = w("header").value.split(/[\s,|]+/).filter(Boolean);
      working.trailer = w("trailer").value.split(/[\s,|]+/).filter(Boolean);
      markDirty();
    }
  });

  host.addEventListener("change", async (e) => {
    if (e.target === dictSelect) {
      if (dirty && !window.confirm("Discard unsaved dictionary changes?")) {
        dictSelect.value = current;
        return;
      }
      current = dictSelect.value;
      deleteArmed = null;
      await loadCurrent();
    } else if (e.target === fileInput) {
      const file = fileInput.files?.[0];
      fileInput.value = "";
      if (!file) return;
      try {
        const doc = JSON.parse(await file.text());
        const stem = file.name.replace(/\.delta\.json$|\.json$/i, "");
        if (doc.mkfix_delta) {
          importPayload = { base_version: doc.base_version || "", doc: doc.delta || {} };
          showNewForm("import", { name: doc.name || stem });
        } else {
          importPayload = { base_version: "", doc };
          showNewForm("import", { name: stem });
        }
      } catch (err) {
        console.error("import failed:", err);
      }
    }
  });

  host.addEventListener("input", (e) => {
    if (e.target === $("filter")) renderBody();
  });

  await refreshList();
});
