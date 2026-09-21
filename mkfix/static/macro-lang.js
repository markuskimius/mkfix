// The macro language for the editor: colouring rules and completion, both
// built from the server's vocabulary (`fix_cmd macro_vocab`), so neither
// lists a word of its own. Pure functions — the Macros pane hands them to
// Ace, the Help viewer colours code blocks with them, and node tests them.
//
// Colouring is only ever colouring: whether a macro is valid is the Python
// parser's call, shown through `check_macro` diagnostics.

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const alt = (words) => [...words].sort((a, b) => b.length - a.length).map((w) => esc(w).replace(/ /g, "\\s+")).join("|");

const HEADERS = ["macro", "seed", "on error", "on sent order", "on order", "run"];
// A macro is for one side, so an editor offers only its side's blocks.
const SIDE_BLOCKS = { market: ["on order"], client: ["run", "on sent order"] };
const BLOCKS = Object.values(SIDE_BLOCKS).flat();
const CLAUSES = ["where", "within", "or timeout", "else fail", "using", "every", "with", "and", "or", "not", "in",
  "last trade", "first trade", "trade where", "continue"];

// One ordered rule list: the first rule matching at a position wins, which is
// how Ace applies them and how `tokenizeLine` does.
export function buildRules(vocab) {
  const statements = Object.keys(vocab.statements).filter((s) => !HEADERS.includes(s)).concat(["else if"]);
  const verbs = Object.keys(vocab.verbs);
  const events = Object.keys(vocab.events);
  const terms = [...new Set(Object.values(vocab.verbs).flatMap((v) => v.terms))];
  const roots = Object.keys(vocab.context);
  const words = [...new Set(Object.values(vocab.enums).flatMap((e) => Object.keys(e)))];
  return [
    { token: "comment", regex: "#.*$" },
    { token: "string", regex: "'(?:[^'\\\\]|\\\\.)*'?" },
    { token: "string", regex: '"(?:[^"\\\\]|\\\\.)*"?' },
    { token: "keyword.control", regex: `^\\s*(?:${alt(HEADERS)})\\b`, caseInsensitive: true },
    { token: "keyword", regex: `^\\s*(?:${alt(statements)})\\b`, caseInsensitive: true },
    { token: "entity.name.function", regex: `^\\s*(?:${alt(verbs)})\\b`, caseInsensitive: true },
    { token: "constant.numeric", regex: "\\b\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?(?:ms|s|m|h|d)?\\b" },
    { token: "keyword.operator", regex: "±|\\+/-|->|\\|>|&&|\\|\\||\\?\\?|[=!<>]=|[<>+\\-*/%!]" },
    { token: "variable.parameter", regex: `\\b(?:${alt(terms)})(?=\\s*:)`, caseInsensitive: true },
    { token: "constant.language", regex: `\\b(?:${alt(events)})\\b`, caseInsensitive: true },
    { token: "keyword.operator", regex: `\\b(?:${alt(CLAUSES)})\\b`, caseInsensitive: true },
    { token: "constant.language", regex: "\\b(?:TRUE|FALSE|NULL)\\b", caseInsensitive: true },
    { token: "support.function", regex: "\\b[A-Za-z_][A-Za-z0-9_]*(?=\\()" },
    { token: "variable.language", regex: `\\b(?:${alt(roots)})\\b(?!\\s*:)` },
    { token: "support.constant", regex: `(?<=:\\s*)(?:${alt(words)})\\b(?=\\s*(?:,|$))`, caseInsensitive: true },
    { token: "text", regex: "[A-Za-z_][A-Za-z0-9_]*" },
  ];
}

// Ace wants { start: [ … ] }; lookbehind is not something its tokenizer can
// be trusted with on every browser, so the enum-word rule is left to the
// generic tokenizer below.
export function aceRules(vocab) {
  return { start: buildRules(vocab).filter((r) => !r.regex.startsWith("(?<=")).map((r) => ({ ...r })) };
}

// [[token, text], …] for one line, by the same first-match rule.
export function tokenizeLine(rules, line) {
  const compiled = rules.map((r) => ({ token: r.token, anchored: r.regex.startsWith("^"),
    re: new RegExp(r.regex.replace(/^\^/, ""), "y" + (r.caseInsensitive ? "i" : "")) }));
  const out = [];
  let pos = 0;
  let plain = "";
  while (pos < line.length) {
    let hit = null;
    for (const r of compiled) {
      if (r.anchored && pos !== 0) continue;
      r.re.lastIndex = pos;
      const m = r.re.exec(line);
      if (m && m[0].length) { hit = [r.token, m[0]]; break; }
    }
    if (!hit) { plain += line[pos++]; continue; }
    if (plain) { out.push(["text", plain]); plain = ""; }
    out.push(hit);
    pos += hit[1].length;
  }
  if (plain) out.push(["text", plain]);
  return out;
}

const html = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// A whole macro as HTML spans, for the Help viewer's code blocks.
export function highlight(code, vocab) {
  const rules = buildRules(vocab);
  return code.split("\n").map((line) => tokenizeLine(rules, line)
    .map(([token, text]) => (token === "text" ? html(text) : `<span class="macro-${token.replace(/\./g, "-")}">${html(text)}</span>`))
    .join("")).join("\n");
}

// -- completion -----------------------------------------------------------------------

// The kind of the block the row sits in: market | client | attached | null (the top of the file).
export function blockKindAt(lines, row) {
  if (!/^\s/.test(lines[row] ?? "")) return null;          // not indented: the top of the file
  for (let r = row - 1; r >= 0; r--) {
    const line = lines[r] ?? "";
    if (!line.trim() || /^\s/.test(line) || /^#/.test(line)) continue;
    if (/^on\s+sent\s+order\b/i.test(line)) return "attached";
    if (/^on\s+order\b/i.test(line)) return "market";
    if (/^run\b/i.test(line)) return "client";
    return null;
  }
  return null;
}

const item = (value, meta, doc, extra = {}) => ({ caption: value, value, meta, doc: doc || "", ...extra });

// What could come next at (row, col). `extras` = { templates: {scope: [names]}, sessions: [names],
// side: "client" | "market" — the editor's side, which narrows the blocks offered }.
export function completionsAt(vocab, lines, row, col, extras = {}) {
  const line = (lines[row] ?? "").slice(0, col);
  const kind = blockKindAt(lines, row);
  const before = line.replace(/[A-Za-z_][A-Za-z0-9_]*$/, "");      // the line up to the word being typed
  const trimmed = before.trim().toLowerCase().replace(/\s+/g, " ");
  const sideOk = (sides) => !kind || sides.includes(kind);

  // fields of order. trade. event. event.prev.
  const dotted = /([A-Za-z_][\w.]*)\.$/.exec(before);
  if (dotted) {
    const path = dotted[1];
    const fields = path === "event.prev" ? vocab.fields.order
      : path === "order" ? vocab.fields.order : path === "trade" ? vocab.fields.trade
        : path === "event" ? vocab.fields.event : [];
    return fields.map((f) => item(f, path));
  }

  if (!/^\s/.test(lines[row] ?? "") && trimmed === "") {
    const mine = SIDE_BLOCKS[extras.side];
    return HEADERS.filter((h) => !mine || !BLOCKS.includes(h) || mine.includes(h))
      .map((h) => item(h, "block", vocab.statements[h]?.[1] ?? vocab.statements[h]?.doc));
  }
  if (/^run$/.test(trimmed)) return [item("on ", "session", "Name the session here, or leave it to be chosen at Run…", { caption: "on" })];
  if (/^run on$/.test(trimmed)) return (extras.sessions ?? []).map((s) => item(s, "session"));

  if (trimmed === "") {
    const verbs = Object.entries(vocab.verbs).filter(([, v]) => sideOk(v.sides)).map(([n, v]) => item(n, "action", v.doc));
    const statements = Object.entries(vocab.statements).filter(([s]) => !HEADERS.includes(s))
      .map(([s, d]) => item(s, "statement", d.doc ?? d[1]));
    return [...verbs, ...statements];
  }

  if (/^(when|wait|expect)( .* or)?$/.test(trimmed)) {
    return Object.entries(vocab.events).filter(([, e]) => sideOk(e.sides)).map(([n, e]) => item(n, "event", e.doc));
  }

  const verb = Object.keys(vocab.verbs).sort((a, b) => b.length - a.length)
    .find((v) => trimmed === v || trimmed.startsWith(v + " "));
  if (verb) {
    const spec = vocab.verbs[verb];
    const rest = trimmed.slice(verb.length).trim();
    const using = /using '([^']*)$/.exec(before);
    if (using) {
      const scope = extras.templateScopes?.[verb];
      return ((extras.templates ?? {})[scope] ?? []).map((t) => item(t, "template"));
    }
    const term = /([a-z_]+)\s*:\s*$/.exec(trimmed);
    if (term) {
      const enumKey = term[1] === "reason" ? { dk: "dk reason", restate: "restate reason" }[verb] : term[1];
      const words = vocab.enums[enumKey];
      if (words) return Object.entries(words).map(([w, code]) => item(w, `= ${code}`));
    }
    if (rest === "" || /,$/.test(rest) || /^(last|first) trade$/.test(rest)) {
      const used = new Set([...trimmed.matchAll(/([a-z_]+)\s*:/g)].map((m) => m[1]));
      const out = spec.terms.filter((t) => !used.has(t)).map((t) => item(t + ": ", "term", "", { caption: t }));
      if (rest === "") {
        if (spec.trade) out.unshift(...Object.entries(vocab.trade_targets).map(([t, d]) => item(t, "trade", d)));
        out.push(item("using '", "template", "Take the terms from a saved template; inline terms override it.", { caption: "using" }));
      }
      return out;
    }
  }

  const roots = Object.entries(vocab.context).map(([n, d]) => item(n, "macro", d));
  const functions = Object.entries(vocab.functions).map(([n, f]) => item(n + "(", "function", f.doc, { caption: n }));
  return [...roots, ...functions];
}

// One line of help for the word at (row, col), or null.
export function helpAt(vocab, lines, row, col) {
  const line = lines[row] ?? "";
  const lower = line.toLowerCase();
  const tables = [["events", vocab.events], ["verbs", vocab.verbs], ["statements", vocab.statements]];
  let best = null;
  for (const [group, table] of tables) {
    for (const [name, entry] of Object.entries(table)) {
      const re = new RegExp(`\\b${alt([name])}\\b`, "gi");
      for (let m; (m = re.exec(lower));) {
        if (m.index <= col && col <= m.index + m[0].length && (!best || name.length > best.name.length)) {
          const verbFirst = group === "verbs" && lower.trim().startsWith(name);
          const eventAfter = group === "events" && /^\s*(when|wait|expect)\b/.test(lower);
          if (group === "statements" || verbFirst || eventAfter) {
            best = { name, group, doc: entry.doc ?? entry[1], form: entry.form ?? entry[0] ?? "", start: m.index, end: m.index + m[0].length };
          }
        }
      }
    }
  }
  if (best) return best;
  const before = /[A-Za-z_]\w*$/.exec(line.slice(0, col))?.[0] ?? "";
  const word = before + (/^\w*/.exec(line.slice(col))?.[0] ?? "");
  const span = { start: col - before.length, end: col - before.length + word.length };
  if (word && vocab.context[word]) return { name: word, group: "context", doc: vocab.context[word], form: "", ...span };
  const fn = word && vocab.functions[word.toUpperCase()];
  if (fn) return { name: word.toUpperCase(), group: "functions", doc: fn.doc, form: "", ...span };
  return null;
}

// What a hover over (row, col) shows: { title, lines: [text…], start, end } or
// null. More than `helpAt`'s one line — an action lists the terms it takes,
// and a word given to a term (`side: buy`) says the FIX code it stands for.
export function hoverAt(vocab, lines, row, col) {
  const line = lines[row] ?? "";
  if (col >= line.length) return null;
  let quote = null;                                            // nothing to say inside a string or a comment
  for (let i = 0; i <= col; i++) {
    const ch = line[i];
    if (quote) { if (ch === "\\") i++; else if (ch === quote) quote = i === col ? quote : null; }
    else if (ch === "'" || ch === '"') quote = ch;
    else if (ch === "#") return null;
  }
  if (quote) return null;
  const help = helpAt(vocab, lines, row, col);
  if (help) {
    const out = { title: help.form || help.name, lines: [help.doc], start: help.start, end: help.end };
    const verb = help.group === "verbs" ? vocab.verbs[help.name] : null;
    if (verb) {
      out.lines.push(`Terms: ${verb.terms.join(", ")}${verb.trade ? " — and which trade: " + Object.keys(vocab.trade_targets).join(", ") : ""}`);
      out.lines.push("`using 'TEMPLATE'` takes the terms from a saved template.");
    }
    return out;
  }
  // an enum word after its term: `side: buy`, `reason: no_match`
  const verbName = Object.keys(vocab.verbs).sort((a, b) => b.length - a.length)
    .find((v) => new RegExp(`^\\s*${esc(v).replace(/ /g, "\\s+")}\\b`, "i").test(line));
  for (const m of line.matchAll(/\b([a-z_]+)\s*:\s*([A-Za-z_][\w]*)/gi)) {
    const at = m.index + m[0].length - m[2].length;
    if (col < at || col > at + m[2].length) continue;
    const term = m[1].toLowerCase();
    const key = term === "reason" ? { dk: "dk reason", restate: "restate reason" }[verbName] : term;
    const code = vocab.enums[key]?.[m[2].toLowerCase()];
    if (code !== undefined) return { title: `${m[2]} = ${code}`, lines: [`${term}: the FIX code ${code} goes on the wire. A quoted value is sent as written.`], start: at, end: at + m[2].length };
  }
  return null;
}
