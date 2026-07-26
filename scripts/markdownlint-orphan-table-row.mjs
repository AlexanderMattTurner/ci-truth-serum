// markdownlint custom rule: a line written as a table row that the parser does
// not put in a table.
//
// GFM tables end at the first blank line. A row separated from its table by a
// blank line is not a syntax error — it silently degrades to a paragraph, so
// the source reads as a table row while the rendered page shows a stray line of
// `| pipe | text |`. No stock markdownlint rule fires: MD055/MD056 only inspect
// rows the parser already accepted as part of a table.
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

/** Line numbers (1-based) the parser accepted as part of a real table. */
const tableLineNumbers = (tokens) => {
  const lines = new Set();
  const walk = (token) => {
    if (token.type === "table") {
      for (let n = token.startLine; n <= token.endLine; n++) lines.add(n);
      return;
    }
    for (const child of token.children ?? []) walk(child);
  };
  for (const token of tokens) walk(token);
  return lines;
};

/** Paragraph tokens, flattened out of the micromark token tree. */
const paragraphs = (tokens) => {
  const found = [];
  const walk = (token) => {
    if (token.type === "paragraph") found.push(token);
    for (const child of token.children ?? []) walk(child);
  };
  for (const token of tokens) walk(token);
  return found;
};

// Autofix only the shape this rule exists to catch: a row sitting one blank
// line below a table that ends directly above, which deleting that blank line
// rejoins. Requiring a blank line immediately above also excludes a row buried
// mid-paragraph — its predecessor is prose by definition — where deleting a
// line would splice unrelated text into a table.
const severingBlankLine = (lines, lineNumber, inTable) => {
  const index = lineNumber - 1;
  if (index === 0 || !isBlank(lines[index - 1])) return null;
  // Walk up over earlier orphan rows of the same severed run — each one a
  // table-shaped line sitting under exactly one blank line — and require the
  // run to terminate in a real table. A run that traces back to prose (or to
  // the top of the file) is not a severed table, so it gets no fix.
  let i = index - 2;
  while (i >= 0 && isTableRowShaped(lines[i]) && !inTable.has(i + 1)) {
    if (i === 0 || !isBlank(lines[i - 1])) return null;
    i -= 2;
  }
  return i >= 0 && inTable.has(i + 1) ? index : null;
};

export default {
  names: ["orphan-table-row"],
  description:
    "Line is written as a table row but renders as paragraph text (blank line severs a GFM table)",
  tags: ["tables"],
  parser: "micromark",
  function: ({ lines, parsers }, onError) => {
    const inTable = tableLineNumbers(parsers.micromark.tokens);
    for (const paragraph of paragraphs(parsers.micromark.tokens)) {
      for (let n = paragraph.startLine; n <= paragraph.endLine; n++) {
        const line = lines[n - 1];
        if (!isTableRowShaped(line)) continue;
        const fixLine = severingBlankLine(lines, n, inTable);
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
