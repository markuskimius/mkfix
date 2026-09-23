// Macro status and controls, always loaded: it is the `macro-status` status
// bar widget, and from there it
//   - keeps `state.macros.client` / `.market` current (macro-status-lib.js):
//     the runs table by subscription, the recordings by a light poll — a
//     recording lives in the server, and may have been started elsewhere;
//   - shows what the macros are doing in the status bar, each item a link to
//     the pane it is about;
//   - puts ▶ ⏸ ■ ● — play, pause, stop, record; symbols only, the words in
//     their tooltips — in the toolbars of the two order blotters (mkio-table's
//     `_toolbar` slot). mkui's declared buttons cannot
//     follow app state, and these must: enabled by what is live, the record
//     button red and counting while it records.
// The editors read the same state, so a recording started on a blotter can be
// stopped in an editor and the other way round.

import { ensureMkio } from "/mkui/src/mkio-bridge.js";
import { SIDES, macroState, statusItems } from "/static/macro-status-lib.js";
import { recordingName } from "/static/macro-lang.js";

const { registerWidget } = window.Mkui;

const BLOTTERS = { "order-blotter": "client", "market-order-blotter": "market" };
const IDLE_MS = 3000;
const RECORDING_MS = 1000;

registerWidget("macro-status", (spec, app, host) => {
  const bar = document.createElement("span");
  bar.className = "macro-statusbar";
  host.appendChild(bar);

  const runs = new Map();
  let recordings = {};
  let client = null;
  let timer = null;

  const cmd = (command, data = {}) => client.send("fix_cmd", { command, ...data }, { op: command });
  const Side = (side) => side[0].toUpperCase() + side.slice(1);

  let state = macroState([], {}, Date.now());
  // While the server is away nothing is known — and when it comes back
  // nothing is playing, since a restart stops every macro. So the last
  // picture is dropped with the connection: ▶ does not stay lit over an
  // outage, and the fresh snapshot a reconnect brings says what is true.
  let online = true;

  function publish() {
    state = macroState(online ? [...runs.values()] : [], online ? recordings : {}, Date.now());
    for (const side of SIDES) state[side].offline = !online;
    app.state.set("macros", state);
    bar.replaceChildren(...statusItems(state).map((item) => {
      const el = document.createElement("a");
      el.className = `macro-status-item macro-status-${item.kind}`;
      el.textContent = item.text;
      el.title = `Open ${Side(item.side)} ${item.pane.endsWith("-macros") ? "Macros" : "Macro Runs"}`;
      el.onclick = () => app.fireAction("pane.show", item.pane);
      return el;
    }));
    renderControls(state);
  }

  async function poll() {
    clearTimeout(timer);
    try {
      const status = await cmd("macro_status");
      recordings = Object.fromEntries(SIDES.map((side) => [side, status[side]]));
    } catch { /* the server is away: the last word stands */ }
    publish();
    timer = setTimeout(poll, SIDES.some((s) => recordings[s]?.recording) ? RECORDING_MS : IDLE_MS);
  }

  // -- the order blotters' controls -------------------------------------------------------
  const controls = new Map();           // pane id -> { side, buttons }

  function button(label, title, onclick) {
    const b = document.createElement("button");
    b.className = "mkui-btn macro-control";
    b.textContent = label;
    b.title = title;
    b.setAttribute("aria-label", title);
    b.onclick = onclick;
    return b;
  }

  function mountControls() {
    for (const [paneId, side] of Object.entries(BLOTTERS)) {
      const slot = document.querySelector(`mkui-pane[data-id="${paneId}"]`)?._toolbar;
      if (!slot) continue;
      const extras = slot.extras();
      if (controls.get(paneId)?.group.isConnected && extras.contains(controls.get(paneId).group)) continue;
      const context = { row: { side, Side: Side(side) } };
      const buttons = {
        play: button("▶", `Play a ${side} macro, or resume a paused run…`, () => app.dialog("play_macro", context)),
        pause: button("⏸", `Pause ${side} macro runs…`, () => app.dialog("pause_runs", context)),
        stop: button("■", `Stop ${side} macro runs…`, () => app.dialog("stop_runs", context)),
        // Stop suggests the name at the click, not at mount: the side, then the time Stop was pressed
        record: button("●", "", () => (app.state.get("macros")?.[side]?.recording
          ? app.dialog("stop_recording", { row: { ...context.row, name: recordingName(side) } })
          : app.dialog("record_macro", context))),
      };
      const group = document.createElement("span");
      group.className = "macro-controls";
      group.append(...Object.values(buttons));
      extras.appendChild(group);
      slot.sync();
      controls.set(paneId, { side, buttons, group });
    }
  }

  // The controls go in when their blotter is ready, not at the next poll: a
  // pane is built by an async factory (mkio-table awaits its client), so its
  // `_toolbar` slot exists only once the pane's `_ready` has resolved. Waiting
  // for a poll to notice showed the toolbar without them for a few seconds.
  const awaited = new WeakSet();
  function watchBlotters() {
    for (const paneId of Object.keys(BLOTTERS)) {
      const el = document.querySelector(`mkui-pane[data-id="${paneId}"]`);
      if (!el || awaited.has(el)) continue;
      awaited.add(el);
      Promise.resolve(el._ready).then(() => renderControls(state), () => {});
    }
  }
  new MutationObserver((changes) => {
    // Table rows come and go all day; only a pane arriving is of interest.
    const pane = changes.some((c) => [...c.addedNodes].some((n) => n.nodeType === 1
      && (n.localName === "mkui-pane" || n.localName === "mkui-frame" || n.querySelector?.("mkui-pane"))));
    if (pane) watchBlotters();
  }).observe(document.body, { childList: true, subtree: true });
  watchBlotters();

  function renderControls(state) {
    mountControls();
    for (const { side, buttons } of controls.values()) {
      const s = state[side];
      // Like a tape deck: the button for what is happening is lit — ▶ while a
      // run of this side plays, ⏸ while one is paused (both, when both are so).
      const count = (n) => `${n} run${n === 1 ? "" : "s"}`;      // not `runs`: that is the map of them
      for (const b of Object.values(buttons)) b.disabled = !!s.offline;
      if (s.offline) {
        for (const [b, cls] of [[buttons.play, "macro-playing"], [buttons.pause, "macro-paused"], [buttons.record, "macro-recording"]]) b.classList.remove(cls);
        for (const b of Object.values(buttons)) { b.title = "The server is away"; b.setAttribute("aria-label", b.title); }
        continue;
      }
      buttons.play.classList.toggle("macro-playing", s.playing > 0);
      buttons.play.title = (s.playing ? `Playing: ${count(s.playing)}, ${s.orders} order${s.orders === 1 ? "" : "s"}. ` : "")
        + (s.paused ? `Resume a paused run, or play another ${side} macro…` : `Play a ${side} macro…`);
      buttons.pause.classList.toggle("macro-paused", s.paused > 0);
      buttons.pause.title = (s.paused ? `Paused: ${count(s.paused)} — ▶ resumes. ` : "")
        + (s.playing ? `Pause ${side} macro runs…` : `Nothing of the ${side} side is playing`);
      buttons.stop.title = s.live ? `Stop ${side} macro runs… (${count(s.live)} live)` : `No ${side} macro run is live`;
      for (const b of [buttons.play, buttons.pause, buttons.stop]) b.setAttribute("aria-label", b.title);
      buttons.play.setAttribute("aria-pressed", String(s.playing > 0));
      buttons.pause.setAttribute("aria-pressed", String(s.paused > 0));
      buttons.pause.disabled = !s.playing;
      buttons.stop.disabled = !s.live;
      buttons.record.classList.toggle("macro-recording", s.recording);
      // The dot alone, red while it records; the count is in the tooltip and the status bar.
      buttons.record.title = s.recording
        ? `Recording — ${s.actions} action${s.actions === 1 ? "" : "s"} so far. Stop recording and save the macro…`
        : side === "client" ? "Record what you do by hand to the orders you send — Replace, Cancel, and DK on Received Trades — as a macro"
          : "Record what you do by hand to the orders that arrive — Accept, Reject, Fill, and Correct, Bust, Re-notify on Sent Trades — as a macro";
      buttons.record.setAttribute("aria-label", buttons.record.title);
    }
  }

  // What the dialogs fire once the server has said yes.
  app.registerAction("macro.refresh", () => poll());
  app.registerAction("macro.recorded", (_app, args) => {
    poll();
    if (!args?.name || !SIDES.includes(args.side)) return;
    app.fireAction("pane.show", `${args.side}-macros`);
    app.state.set("open_macro", { name: args.name, side: args.side });
  });

  app.state.subscribe("mkio.connected", (connected) => {
    const now = connected !== false;
    if (now === online) return;
    online = now;
    if (!online) { runs.clear(); recordings = {}; }
    publish();
    if (online && client) poll();
  });

  (async () => {
    client = await ensureMkio(app.config?.mkio?.url);
    const apply = (op, row) => { if (op === "delete") runs.delete(row.id); else runs.set(row.id, row); };
    client.subscribe("macro_runs_query", "query", {
      subid: `macro-status-${Date.now()}`,
      onSnapshot: (rows) => { runs.clear(); rows.forEach((r) => apply("insert", r)); publish(); },
      onUpdate: (op, row) => { apply(op, row); publish(); },
      onDelta: (changes) => { changes.forEach(({ op, row }) => apply(op, row)); publish(); },
    });
    poll();
    // A blotter opened later, and an ended run whose minute is up.
    setInterval(publish, 5000);
  })();
});
