// Help viewer: the pages under static/help, rendered by the small Markdown
// renderer in markdown.js, with a contents list. The same files read well on
// GitHub, so the documentation is written once.
//
// The Scenario Examples page is not a file: it is built from the headers of
// the examples the server ships (`fix_cmd list_examples`), each with a way
// to open a copy in the Scenarios pane.
//
// `app.state.help_target = { page, anchor }` opens a page at a heading — the
// editor's F1 sets it.

import { ensureMkio } from "/mkui/src/mkio-bridge.js";
import { escapeHtml, headings, renderMarkdown } from "/static/markdown.js";
import { highlight } from "/static/scenario-lang.js";

const { registerPaneType } = window.Mkui;

registerPaneType("help-viewer", async (spec, app, host) => {
  const client = await ensureMkio(app.config?.mkio?.url);
  const cmd = (command, data = {}) => client.send("fix_cmd", { command, ...data }, { op: command });

  host.innerHTML = `<div class="help-pane"><nav class="help-nav"></nav><article class="help-page" tabindex="0"></article></div>`;
  const nav = host.querySelector(".help-nav");
  const page = host.querySelector(".help-page");

  const pages = await (await fetch("/static/help/index.json", { cache: "no-cache" })).json();
  let vocab = null;
  let currentId = null;

  async function examplesPage() {
    const { examples } = await cmd("list_examples");
    const cards = examples.map((e) => `
      <section class="help-example">
        <h3 id="${escapeHtml(e.name)}">${escapeHtml(e.title ?? e.name)}</h3>
        <dl>${["shows", "needs", "watch", "outcome"].filter((k) => e[k]).map((k) =>
          `<dt>${k[0].toUpperCase() + k.slice(1)}</dt><dd>${escapeHtml(e[k])}</dd>`).join("")}</dl>
        <button class="mkui-btn" data-example="${escapeHtml(e.name)}">Open in editor</button>
      </section>`).join("");
    return { html: `<h1 id="scenario-examples">Scenario Examples</h1>
      <p>Bundled with mkfix and read-only: <b>Open in editor</b> makes a copy of your own in the Scenarios pane.</p>
      <p>The examples that send orders run on the session <code>LOOP-CLI</code>, facing <code>LOOP-MKT</code> — this
      server talking to itself, so both sides of every order are on your screen. Arm a market-side example
      (<i>slow-fill</i>, <i>cancel-replace-desk</i>, <i>dispute-desk</i>), then run a sending one; <i>loopback-tour</i>
      plays both sides by itself.</p>
      <p><button class="mkui-btn" data-loopback>Set up loopback sessions</button> <span class="help-loopback"></span></p>${cards}`,
      toc: examples.map((e) => ({ level: 2, text: e.title ?? e.name, id: e.name })) };
  }

  async function show(id, anchor) {
    const entry = pages.find((p) => p.id === id) ?? pages[0];
    currentId = entry.id;
    let content;
    if (entry.builtin === "examples") {
      content = await examplesPage();
    } else {
      const text = await (await fetch(`/static/help/${entry.file}`, { cache: "no-cache" })).text();
      vocab ??= (await cmd("scenario_vocab")).vocabulary;
      content = { html: renderMarkdown(text, { highlight: (code, lang) => (lang === "scenario" ? highlight(code, vocab) : null) }),
        toc: headings(text).filter((h) => h.level === 2 || h.level === 3) };
    }
    page.innerHTML = content.html;
    nav.innerHTML = pages.map((p) => `<a class="help-nav-page${p.id === currentId ? " help-current" : ""}" data-page="${p.id}">${escapeHtml(p.title)}</a>`
      + (p.id === currentId ? content.toc.map((h) => `<a class="help-nav-h help-nav-h${h.level}" data-anchor="${h.id}">${escapeHtml(h.text)}</a>`).join("") : "")).join("");
    const target = anchor && page.querySelector(`#${CSS.escape(anchor)}`);
    if (target) target.scrollIntoView({ block: "start" }); else page.scrollTop = 0;
  }

  host.addEventListener("click", (e) => {
    const link = e.target.closest("a[href]");
    if (link) {
      const href = link.getAttribute("href");
      if (href.startsWith("#")) { e.preventDefault(); page.querySelector(`#${CSS.escape(href.slice(1))}`)?.scrollIntoView(); return; }
      const local = /^([\w-]+)\.md(?:#([\w-]*))?$/.exec(href);
      if (local) { e.preventDefault(); show(local[1], local[2]); }
      return;
    }
    const navPage = e.target.closest("[data-page]");
    if (navPage) return show(navPage.dataset.page);
    const navAnchor = e.target.closest("[data-anchor]");
    if (navAnchor) return page.querySelector(`#${CSS.escape(navAnchor.dataset.anchor)}`)?.scrollIntoView({ block: "start" });
    const loopback = e.target.closest("[data-loopback]");
    if (loopback) {
      const note = page.querySelector(".help-loopback");
      note.textContent = "…";
      cmd("setup_loopback").then((r) => {
        note.textContent = (r.created.length ? `Created ${r.created.join(" and ")}. ` : "Both sessions already exist. ")
          + (r.started.length ? `Started ${r.started.join(" and ")} on port ${r.port}.` : `Port ${r.port}.`);
      }).catch((err) => { note.textContent = String(err.message ?? err); });
      return;
    }
    const example = e.target.closest("[data-example]");
    if (example) {
      app.fireAction("pane.show", "scenarios");
      app.state.set("open_example", example.dataset.example);
    }
  });

  // F1 in the editor names a heading; an open viewer follows it without
  // taking the focus, and one opened later starts there.
  let first = true;
  app.state.subscribe("help_target", (target) => {
    if (first) return;
    if (target) show(target.page, target.anchor);
  });
  first = false;
  const start = app.state.get("help_target");
  await show(start?.page ?? spec.page ?? pages[0].id, start?.anchor);
});
