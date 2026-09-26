// What the macros are doing, worked out from the runs table and the server's
// recording status: the shape the order blotters' macro controls are enabled
// by and the status bar reads. Pure functions — node tests them.

const LIVE = ["armed", "paused"];
const RECENT_MS = 60_000;            // an ended run is news for a minute
const FAILED_MS = 600_000;           // a failed one until something else happens on its side, ten minutes at most

export const SIDES = ["client", "market"];

// A FIX stamp (YYYYMMDD-HH:MM:SS[.mmm], UTC) as epoch milliseconds, or NaN.
export function stampMs(fix) {
  const m = /^(\d{4})(\d\d)(\d\d)-(\d\d):(\d\d):(\d\d)(?:\.(\d{1,3}))?/.exec(fix ?? "");
  return m ? Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6], +(m[7] ?? "0").padEnd(3, "0")) : NaN;
}

// One side's state: { recording, actions, session, playing, paused, live,
// orders, runs: [live rows], last: the ended run still worth showing | null }.
export function sideState(side, runs, recording, now) {
  const mine = runs.filter((r) => r.side === side);
  const live = mine.filter((r) => LIVE.includes(r.status));
  const playing = live.filter((r) => r.status === "armed");
  const ended = mine.filter((r) => !LIVE.includes(r.status) && Number.isFinite(stampMs(r.ended_at)))
    .sort((a, b) => stampMs(b.ended_at) - stampMs(a.ended_at) || b.id - a.id);
  let last = ended[0] ?? null;
  if (last) {
    const age = now - stampMs(last.ended_at);
    const superseded = mine.some((r) => r.id !== last.id && stampMs(r.started_at) > stampMs(last.ended_at));
    // A pass keeps its minute whatever follows. A failure, a stop or an
    // interruption gives way to a newer run on its side — `▶ slow-fill` beside
    // `■ slow-fill interrupted` says two things about one macro.
    const keep = last.verdict === "passed" ? age < RECENT_MS
      : !superseded && age < (last.verdict === "failed" ? FAILED_MS : RECENT_MS);
    if (!keep) last = null;
  }
  return {
    recording: !!recording?.recording, actions: recording?.actions ?? 0, session: recording?.session ?? "",
    playing: playing.length, paused: live.length - playing.length, live: live.length,
    orders: live.reduce((n, r) => n + (r.orders || 0), 0), runs: live, last,
  };
}

export function macroState(runs, recordings, now) {
  return Object.fromEntries(SIDES.map((side) => [side, sideState(side, runs, recordings?.[side], now)]));
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// The status bar's items, in the order they are shown:
// [{ kind: recording | playing | paused | passed | failed | ended, side, text, pane | frame }]:
// `pane` opens the side's editor, `frame` its Macro Runs window (the runs
// tree over the log).
export function statusItems(state) {
  const items = [];
  for (const side of SIDES) {
    const s = state[side];
    if (!s) continue;
    if (s.recording) {
      items.push({ kind: "recording", side, pane: `${side}-macros`,
        text: `● REC ${side} · ${plural(s.actions, "action")}${s.session ? " on " + s.session : ""}` });
    }
    const playing = s.runs.filter((r) => r.status === "armed");
    const paused = s.runs.filter((r) => r.status === "paused");
    if (playing.length) {
      const orders = playing.reduce((n, r) => n + (r.orders || 0), 0);
      items.push({ kind: "playing", side, frame: `${side}-runs`,
        text: `▶ ${side} ${playing.length === 1 ? playing[0].macro : plural(playing.length, "run")} · ${plural(orders, "order")}` });
    }
    if (paused.length) {
      items.push({ kind: "paused", side, frame: `${side}-runs`,
        text: `⏸ ${side} ${paused.length === 1 ? paused[0].macro : plural(paused.length, "run")} paused` });
    }
    if (s.last) {
      const r = s.last;
      const tally = r.verdict === "failed" ? ` · ${r.failed} of ${r.orders} failed` : r.verdict === "passed" ? ` · ${r.passed} of ${r.orders}` : "";
      items.push({ kind: r.verdict === "failed" ? "failed" : r.verdict === "passed" ? "passed" : "ended", side,
        frame: `${side}-runs`, text: `■ ${r.macro} ${r.verdict || r.status}${tally}` });
    }
  }
  return items;
}
