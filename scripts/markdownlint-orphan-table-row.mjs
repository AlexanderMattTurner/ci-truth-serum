// markdownlint custom rule: a line written as a table row that the parser does
// not put in a table.
//
// GFM tables end at the first blank line. A row separated from its table by a
// blank line is not a syntax error — it silently degrades to a paragraph, so
// the source reads as a table row while the rendered page shows a stray line of
// `| pipe | text |`. No stock markdownlint rule fires: MD055/MD056 only inspect
// rows the parser already accepted as part of a table.
//
// Scope: rows at the document's own indentation. A row inside a blockquote
// (`> | a | b |`) is not matched, because the fix — deleting a blank line —
// would not rejoin it anyway; the quote's blank line needs its own `>`.
//
// Loaded by markdownlint-cli2 (see .markdownlint-cli2.jsonc) in an environment
// that installs no other packages, so this module must stay import-free.

// A leading pipe plus at least one more: GFM lets a row omit its trailing
// pipe, so requiring one would miss `| a | b`. Up to three leading spaces still
// starts a table row; a fourth makes it an indented code block, which renders
// as code and is therefore not this bug.
const TABLE_ROW = /^ {0,3}\|.*\|/;

const isTableRowShaped = (line) => TABLE_ROW.test(line);

const isBlank = (line) => line.trim() === "";

/**
 * One pass over the micromark token tree: the 1-based line numbers the parser
 * accepted as part of a real table, and every paragraph token.
 */
const scan = (tokens) => {
  const inTable = new Set();
  const paragraphs = [];
  const walk = (token) => {
    if (token.type === "table") {
      for (let n = token.startLine; n <= token.endLine; n++) inTable.add(n);
      return;
    }
    if (token.type === "paragraph") paragraphs.push(token);
    for (const child of token.children ?? []) walk(child);
  };
  for (const token of tokens) walk(token);
  return { inTable, paragraphs };
};

const indentOf = (line) => line.length - line.trimStart().length;

// Deleting the blank line hands the table every line of the paragraph below,
// since a table runs to the next blank line. A prose tail would be silently
// eaten as table rows, so only an all-rows paragraph can ever be rejoined.
const allRows = (lines, from, to) => {
  for (let n = from; n <= to; n++) {
    if (!isTableRowShaped(lines[n - 1])) return false;
  }
  return true;
};

// Autofix only the shape this rule exists to catch: an all-rows paragraph
// sitting one blank line below a table it was severed from, which deleting
// that blank line rejoins. Returns the blank line's number, or null.
//
// The walk steps paragraph-wise so a run of severed paragraphs is rejoined in
// a single `--fix` pass — every blank line in the run is reported at once,
// rather than the run collapsing one paragraph per pass.
//
// Indentation must match the table's: a col-0 row is not a continuation of a
// table indented inside a list item, so deleting the blank between them would
// rewrite the file without rejoining anything, leaving the error live forever.
const severingBlankLine = (lines, paragraph, byEndLine, inTable) => {
  const indent = indentOf(lines[paragraph.startLine - 1]);
  let start = paragraph.startLine;
  while (start >= 3 && isBlank(lines[start - 2])) {
    const above = start - 2;
    if (inTable.has(above)) {
      // The line to delete is this paragraph's own severing blank, not the
      // one that severed whichever paragraph the walk finished on.
      return indentOf(lines[above - 1]) === indent
        ? paragraph.startLine - 1
        : null;
    }
    // Not a table yet: keep walking only through earlier severed paragraphs of
    // the same run. Anything else above means there is nothing to rejoin to.
    const previous = byEndLine.get(above);
    if (!previous || !allRows(lines, previous.startLine, previous.endLine)) {
      return null;
    }
    start = previous.startLine;
  }
  return null;
};

export default {
  names: ["orphan-table-row"],
  description:
    "Line is written as a table row but renders as paragraph text (blank line severs a GFM table)",
  tags: ["tables"],
  parser: "micromark",
  function: ({ lines, parsers }, onError) => {
    const { inTable, paragraphs } = scan(parsers.micromark.tokens);
    const byEndLine = new Map(paragraphs.map((p) => [p.endLine, p]));
    for (const paragraph of paragraphs) {
      const fixLine = allRows(lines, paragraph.startLine, paragraph.endLine)
        ? severingBlankLine(lines, paragraph, byEndLine, inTable)
        : null;
      for (let n = paragraph.startLine; n <= paragraph.endLine; n++) {
        const line = lines[n - 1];
        if (!isTableRowShaped(line)) continue;
        onError({
          lineNumber: n,
          detail:
            "GFM tables end at the first blank line; this row renders as literal text",
          context: line.length > 60 ? `${line.slice(0, 57)}...` : line,
          // One deletion per severed paragraph: the fix belongs to the run, not
          // to each row in it.
          ...(fixLine === null || n !== paragraph.startLine
            ? {}
            : { fixInfo: { lineNumber: fixLine, deleteCount: -1 } }),
        });
      }
    }
  },
};
