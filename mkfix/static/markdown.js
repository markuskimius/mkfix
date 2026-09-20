// A small Markdown renderer for the Help viewer: headings, paragraphs, lists
// (nested by indent), fenced code, tables, rules, and inline code, bold,
// italic and links. No dependency, no build. The help pages are written to
// this subset and a test holds them to it, so what reads well on GitHub
// reads the same here.
//
// `renderMarkdown(text, { highlight })` returns HTML; `highlight(code, lang)`
// may colour a fenced block (it must return escaped HTML).

export const escapeHtml = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export const slug = (text) => text.toLowerCase().replace(/`/g, "").replace(/[^\w\s-]/g, "").trim().replace(/\s+/g, "-");

export function inline(text) {
  const held = [];
  const hold = (htmlText) => { held.push(htmlText); return `\u0000${held.length - 1}\u0000`; };
  let s = text.replace(/`([^`]+)`/g, (_, code) => hold(`<code>${escapeHtml(code)}</code>`));
  s = escapeHtml(s);
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, href) => {
    const safe = /^(https?:|#|[\w./-]+(#[\w-]*)?$)/.test(href) ? href : "#";
    const external = /^https?:/.test(safe);
    return hold(`<a href="${safe}"${external ? ' target="_blank" rel="noopener"' : ""}>${label}</a>`);
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => held[Number(i)]);
}

const cells = (row) => row.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));

function list(lines, start, baseIndent) {
  const ordered = /^\s*\d+\.\s/.test(lines[start]);
  const out = [ordered ? "<ol>" : "<ul>"];
  let i = start;
  while (i < lines.length) {
    const m = /^(\s*)(?:[-*]|\d+\.)\s+(.*)$/.exec(lines[i]);
    if (!m || m[1].length < baseIndent) break;
    if (m[1].length > baseIndent) {                       // a list inside the last item
      const [inner, next] = list(lines, i, m[1].length);
      out[out.length - 1] = out[out.length - 1].replace(/<\/li>$/, inner + "</li>");
      i = next;
      continue;
    }
    let text = m[2];
    i++;
    while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*(?:[-*]|\d+\.)\s/.test(lines[i])) text += " " + lines[i++].trim();
    out.push(`<li>${inline(text)}</li>`);
  }
  out.push(ordered ? "</ol>" : "</ul>");
  return [out.join(""), i];
}

export function renderMarkdown(text, { highlight } = {}) {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    const fence = /^```\s*([\w-]*)\s*$/.exec(line);
    if (fence) {
      const body = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) body.push(lines[i++]);
      i++;
      const code = body.join("\n");
      const painted = highlight ? highlight(code, fence[1]) : null;
      out.push(`<pre class="md-code" data-lang="${escapeHtml(fence[1])}"><code>${painted ?? escapeHtml(code)}</code></pre>`);
      continue;
    }

    const heading = /^(#{1,4})\s+(.*?)\s*#*$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level} id="${slug(heading[2])}">${inline(heading[2])}</h${level}>`);
      i++;
      continue;
    }

    if (/^(?:---+|\*\*\*+)\s*$/.test(line)) { out.push("<hr>"); i++; continue; }

    if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push("<table><thead><tr>" + head.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>"
        + rows.map((r) => "<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") + "</tbody></table>");
      continue;
    }

    if (/^\s*(?:[-*]|\d+\.)\s+/.test(line)) {
      const [htmlText, next] = list(lines, i, /^(\s*)/.exec(line)[1].length);
      out.push(htmlText);
      i = next;
      continue;
    }

    const para = [line.trim()];
    i++;
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\s*\||\s*(?:[-*]|\d+\.)\s|---+\s*$)/.test(lines[i])) para.push(lines[i++].trim());
    out.push(`<p>${inline(para.join(" "))}</p>`);
  }
  return out.join("\n");
}

// The headings of a page, for the contents list: [{ level, text, id }].
export function headings(text) {
  const out = [];
  let fenced = false;
  for (const line of text.split("\n")) {
    if (/^```/.test(line)) fenced = !fenced;
    const m = !fenced && /^(#{1,4})\s+(.*?)\s*#*$/.exec(line);
    if (m) out.push({ level: m[1].length, text: m[2].replace(/`/g, ""), id: slug(m[2]) });
  }
  return out;
}
