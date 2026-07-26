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

// Autofix only the shape this rule exists to catch: a row sitting one blank
// line below a table that ends directly above, which deleting that blank line
// rejoins. Requiring a blank line immediately above also excludes a row buried
// mid-paragraph — its predecessor is prose by definition — where deleting a
// line would splice unrelated text into a table.
const severingBlankLine = (lines, lineNumber, inTable) => {
  const index = lineNumber - 1;
  // Step up over blank/row pairs — each severed row sits one blank line below
  // the previous — and stop as soon as the pair above is a real table, whose
  // severing blank line is the one directly above this row.
  let i = index;
  while (i >= 2 && isBlank(lines[i - 1]) && isTableRowShaped(lines[i - 2])) {
    if (inTable.has(i - 1)) return index;
    i -= 2;
  }
  return null;
};

// Deleting the blank line hands the table every line of this paragraph, since
// a table runs to the next blank line. A prose tail would be silently eaten as
// table rows, so a paragraph that is not rows all the way down gets no fix.
const allRows = (lines, from, to) => {
  for (let n = from; n <= to; n++) {
    if (!isTableRowShaped(lines[n - 1])) return false;
  }
  return true;
};

export default {
  names: ["orphan-table-row"],
  description:
    "Line is written as a table row but renders as paragraph text (blank line severs a GFM table)",
  tags: ["tables"],
  parser: "micromark",
  function: ({ lines, parsers }, onError) => {
    const { inTable, paragraphs } = scan(parsers.micromark.tokens);
    for (const paragraph of paragraphs) {
      const fixable = allRows(lines, paragraph.startLine, paragraph.endLine);
      for (let n = paragraph.startLine; n <= paragraph.endLine; n++) {
        const line = lines[n - 1];
        if (!isTableRowShaped(line)) continue;
        const fixLine = fixable ? severingBlankLine(lines, n, inTable) : null;
        onError({
          lineNumber: n,
          detail:
            "GFM tables end at the first blank line; this row renders as literal text",
          context: line.length > 60 ? `${line.slice(0, 57)}...` : line,
          ...(fixLine === null
            ? {}
            : { fixInfo: { lineNumber: fixLine, deleteCount: -1 } }),
        });
      }
    }
  },
};
