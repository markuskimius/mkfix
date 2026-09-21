// Line differences between two texts, for the macro editors' History: a
// saved version is shown as it was, with the lines that are no longer in the
// current macro marked, and the places where the current macro has lines
// this version lacks. Pure functions — node tests them.

// Above this many cell pairs the table is not worth its memory: the lines
// between the common head and tail are then all called changed, which is
// still true, only coarser.
const MAX_CELLS = 4_000_000;

// [{ op: "same" | "del" | "add", a, b }] — `a` indexes `from`, `b` indexes
// `to`; `del` is a line only in `from`, `add` a line only in `to`.
export function lineDiff(from, to) {
  const ops = [];
  let head = 0;
  while (head < from.length && head < to.length && from[head] === to[head]) head++;
  let tail = 0;
  while (tail < from.length - head && tail < to.length - head
    && from[from.length - 1 - tail] === to[to.length - 1 - tail]) tail++;
  for (let i = 0; i < head; i++) ops.push({ op: "same", a: i, b: i });

  const n = from.length - head - tail;
  const m = to.length - head - tail;
  if (n * m > MAX_CELLS) {
    for (let i = 0; i < n; i++) ops.push({ op: "del", a: head + i, b: null });
    for (let j = 0; j < m; j++) ops.push({ op: "add", a: null, b: head + j });
  } else {
    // Longest common subsequence, filled from the end so the walk runs forward.
    const width = m + 1;
    const table = new Uint32Array((n + 1) * width);
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        table[i * width + j] = from[head + i] === to[head + j]
          ? table[(i + 1) * width + j + 1] + 1
          : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (from[head + i] === to[head + j]) { ops.push({ op: "same", a: head + i, b: head + j }); i++; j++; }
      else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) { ops.push({ op: "del", a: head + i, b: null }); i++; }
      else { ops.push({ op: "add", a: null, b: head + j }); j++; }
    }
    for (; i < n; i++) ops.push({ op: "del", a: head + i, b: null });
    for (; j < m; j++) ops.push({ op: "add", a: null, b: head + j });
  }

  for (let k = 0; k < tail; k++) ops.push({ op: "same", a: from.length - tail + k, b: to.length - tail + k });
  return ops;
}

// What to draw on `from`'s own lines: `only` — its rows that `to` lacks;
// `gaps` — { row, count }: `count` lines of `to` belong before `from`'s row
// `row` (row === from.length: after its last line).
export function diffMarks(from, to) {
  const only = [];
  const gaps = [];
  let next = 0;                 // the row of `from` the next line would be
  let pending = 0;
  for (const step of lineDiff(from, to)) {
    if (step.op === "add") { pending++; continue; }
    if (pending) { gaps.push({ row: next, count: pending }); pending = 0; }
    if (step.op === "del") only.push(step.a);
    next = step.a + 1;
  }
  if (pending) gaps.push({ row: next, count: pending });
  return { only, gaps, removed: only.length, added: gaps.reduce((n, g) => n + g.count, 0) };
}

export const splitLines = (text) => text.replace(/\r\n?/g, "\n").replace(/\n$/, "").split("\n");
