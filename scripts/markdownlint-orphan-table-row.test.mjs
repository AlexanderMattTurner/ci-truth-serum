import assert from "node:assert/strict";
import test from "node:test";

import { applyFixes } from "markdownlint";
import { lint } from "markdownlint/promise";

import rule from "./markdownlint-orphan-table-row.mjs";

const RULE = "orphan-table-row";

/** Lint `markdown` with only this rule enabled. */
const run = async (markdown) => {
  const results = await lint({
    strings: { s: markdown },
    config: { default: false, [RULE]: true },
    customRules: [rule],
  });
  return results.s;
};

const linesFlagged = async (markdown) =>
  (await run(markdown)).map((error) => error.lineNumber);

const TABLE = "| Hook | Failure |\n| ---- | ------- |\n| `a`  | first   |\n";

test("a well-formed table is clean", async () => {
  assert.deepEqual(await linesFlagged(`${TABLE}| \`b\` | second |\n`), []);
});

test("a row severed by a blank line is flagged and rejoined", async () => {
  const markdown = `${TABLE}\n| \`b\` | second |\n`;
  assert.deepEqual(await linesFlagged(markdown), [5]);
  assert.equal(
    applyFixes(markdown, await run(markdown)),
    `${TABLE}| \`b\` | second |\n`,
  );
});

test("a severed row with no trailing pipe is still caught", async () => {
  // GFM lets a row drop its trailing pipe, so the shape test must too.
  const markdown = `${TABLE}\n| \`b\` | second\n`;
  assert.deepEqual(await linesFlagged(markdown), [5]);
  assert.equal(
    applyFixes(markdown, await run(markdown)),
    `${TABLE}| \`b\` | second\n`,
  );
});

test("a run of severed rows is flagged and rejoined in one pass", async () => {
  const markdown = `${TABLE}\n| \`b\` | second |\n\n| \`c\` | third |\n`;
  assert.deepEqual(await linesFlagged(markdown), [5, 7]);
  assert.equal(
    applyFixes(markdown, await run(markdown)),
    `${TABLE}| \`b\` | second |\n| \`c\` | third |\n`,
  );
});

test("a pipe line that traces back to prose is flagged but not fixed", async () => {
  const markdown = "Some prose.\n\n| `b` | second |\n";
  const errors = await run(markdown);
  assert.deepEqual(
    errors.map((error) => error.lineNumber),
    [3],
  );
  assert.equal(errors[0].fixInfo, null);
  assert.equal(applyFixes(markdown, errors), markdown);
});

test("a pipe line mid-paragraph is flagged but not fixed", async () => {
  // Deleting a line here would splice unrelated prose into the table above.
  const markdown = `${TABLE}\nProse lead-in.\n| \`b\` | second |\n`;
  const errors = await run(markdown);
  assert.deepEqual(
    errors.map((error) => error.lineNumber),
    [6],
  );
  assert.equal(errors[0].fixInfo, null);
  assert.equal(applyFixes(markdown, errors), markdown);
});

test("pipe lines the reader never sees as a table are not flagged", async () => {
  // Fenced code and indented code render as code, so no row was lost.
  assert.deepEqual(
    await linesFlagged("```\n| `b` | second |\n```\n"),
    [],
    "fenced code block",
  );
  assert.deepEqual(
    await linesFlagged("Prose.\n\n    | `b` | second |\n"),
    [],
    "indented code block",
  );
});

test("prose with interior pipes is not a table row", async () => {
  // The shape test is anchored: only a *leading* pipe starts a row.
  assert.deepEqual(
    await linesFlagged(`${TABLE}\nUse \`a | b\` to pipe | like this.\n`),
    [],
  );
});

test("a whitespace-only severing line is treated as blank and deleted", async () => {
  const markdown = `${TABLE}   \n| \`b\` | second |\n`;
  assert.deepEqual(await linesFlagged(markdown), [5]);
  assert.equal(
    applyFixes(markdown, await run(markdown)),
    `${TABLE}| \`b\` | second |\n`,
  );
});

test("a run that breaks before reaching a table is flagged but not fixed", async () => {
  // Line 2 is table-shaped but is a lazy continuation of prose, so the walk up
  // from line 4 hits prose instead of a table: there is nothing to rejoin.
  const markdown = "prose\n| 1 | 2 |\n\n| 3 | 4 |\n";
  const errors = await run(markdown);
  assert.deepEqual(
    errors.map((error) => error.lineNumber),
    [2, 4],
  );
  assert.deepEqual(
    errors.map((error) => error.fixInfo),
    [null, null],
  );
  assert.equal(applyFixes(markdown, errors), markdown);
});

test("a row at the very top of a file is flagged but not fixed", async () => {
  // There is no line above to rejoin it to, and no table it could have left.
  const markdown = "| `b` | second |\n";
  const errors = await run(markdown);
  assert.deepEqual(
    errors.map((error) => error.lineNumber),
    [1],
  );
  assert.equal(errors[0].fixInfo, null);
});

test("a row two blank lines below a table is flagged but not fixed", async () => {
  // Deleting one of the two blanks would not rejoin anything, and guessing
  // which paragraph break the author meant is not the linter's call.
  const markdown = `${TABLE}\n\n| \`b\` | second |\n`;
  const errors = await run(markdown);
  assert.deepEqual(
    errors.map((error) => error.lineNumber),
    [6],
  );
  assert.equal(errors[0].fixInfo, null);
});

test("a severed row nested in a list item is found and rejoined", async () => {
  // Exercises the walk into child tokens: the table is not at top level.
  const markdown =
    "- item\n\n  | A | B |\n  | - | - |\n  | 1 | 2 |\n\n  | 3 | 4 |\n";
  assert.deepEqual(await linesFlagged(markdown), [7]);
  assert.equal(
    applyFixes(markdown, await run(markdown)),
    "- item\n\n  | A | B |\n  | - | - |\n  | 1 | 2 |\n  | 3 | 4 |\n",
  );
});

test("the reported error names the rule and truncates a long line", async () => {
  const long = `| \`b\` | ${"x".repeat(200)} |`;
  const [error] = await run(`${TABLE}\n${long}\n`);
  assert.deepEqual(error.ruleNames, ["orphan-table-row"]);
  assert.match(error.errorDetail, /blank line/);
  assert.equal(error.errorContext, `${long.slice(0, 57)}...`);
  assert.equal(error.errorContext.length, 60);
});

test("a short row is reported with its full text as context", async () => {
  const short = "| `b` | second |";
  const [error] = await run(`${TABLE}\n${short}\n`);
  assert.equal(error.errorContext, short);
});

test("the rule's fix converges — a fixed document lints clean", async () => {
  const markdown = `${TABLE}\n| \`b\` | second |\n\n| \`c\` | third |\n`;
  const fixed = applyFixes(markdown, await run(markdown));
  assert.deepEqual(await linesFlagged(fixed), []);
});

test("the repo's own markdown config wires this rule in", async () => {
  const config = await import("node:fs/promises").then((fs) =>
    fs.readFile(
      new URL("../.markdownlint-cli2.jsonc", import.meta.url),
      "utf8",
    ),
  );
  assert.match(config, /scripts\/markdownlint-orphan-table-row\.mjs/);
});
