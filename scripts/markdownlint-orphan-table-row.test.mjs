import assert from "node:assert/strict";
import test from "node:test";

import { applyFixes } from "markdownlint";
import { lint } from "markdownlint/promise";

import rule from "./markdownlint-orphan-table-row.mjs";

const RULE = "orphan-table-row";

const DETAIL =
  "GFM tables end at the first blank line; this row renders as literal text";

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
const ROW = "| `b` | second |";

// Rows the rule can prove were severed from the table directly above: deleting
// the blank line puts each one back where it was written to be.
const REJOINED = [
  {
    name: "a row severed by a blank line",
    markdown: `${TABLE}\n${ROW}\n`,
    flagged: [5],
    fixed: `${TABLE}${ROW}\n`,
  },
  {
    name: "a severed row with no trailing pipe (GFM lets a row drop it)",
    markdown: `${TABLE}\n| \`b\` | second\n`,
    flagged: [5],
    fixed: `${TABLE}| \`b\` | second\n`,
  },
  {
    name: "a severing line of whitespace rather than nothing",
    markdown: `${TABLE}   \n${ROW}\n`,
    flagged: [5],
    fixed: `${TABLE}${ROW}\n`,
  },
  {
    name: "a run of severed rows, rejoined in one pass",
    markdown: `${TABLE}\n${ROW}\n\n| \`c\` | third |\n`,
    flagged: [5, 7],
    fixed: `${TABLE}${ROW}\n| \`c\` | third |\n`,
  },
  {
    // Two rows in one paragraph, then another paragraph: a walk that stepped
    // line-pair-wise instead of paragraph-wise would settle only the first run
    // per pass, so `--fix` would rewrite the file on every invocation.
    name: "a run of multi-row paragraphs, all rejoined in one pass",
    markdown: `${TABLE}\n| 3 | 4 |\n| 5 | 6 |\n\n| 7 | 8 |\n`,
    flagged: [5, 6, 8],
    fixed: `${TABLE}| 3 | 4 |\n| 5 | 6 |\n| 7 | 8 |\n`,
  },
  {
    name: "a severed row nested in a list item (the table is not top level)",
    markdown:
      "- item\n\n  | A | B |\n  | - | - |\n  | 1 | 2 |\n\n  | 3 | 4 |\n",
    flagged: [7],
    fixed: "- item\n\n  | A | B |\n  | - | - |\n  | 1 | 2 |\n  | 3 | 4 |\n",
  },
];

for (const { name, markdown, flagged, fixed } of REJOINED) {
  test(`flagged and rejoined: ${name}`, async () => {
    const errors = await run(markdown);
    assert.deepEqual(
      errors.map((error) => error.lineNumber),
      flagged,
    );
    assert.equal(applyFixes(markdown, errors), fixed);
    // A fix that leaves work behind would need a second `--fix` pass to settle.
    assert.deepEqual(await linesFlagged(fixed), []);
  });
}

// Rows the rule cannot prove anything about. Each is still reported — the line
// does not render as a table row either way — but guessing at a fix here would
// rewrite text the author never asked it to touch.
const REPORTED_ONLY = [
  {
    name: "a row at the very top of a file, with nothing above to rejoin it to",
    markdown: `${ROW}\n`,
    flagged: [1],
  },
  {
    name: "a row whose only neighbour above is prose",
    markdown: `Some prose.\n\n${ROW}\n`,
    flagged: [3],
  },
  {
    name: "a row two blank lines below a table",
    markdown: `${TABLE}\n\n${ROW}\n`,
    flagged: [6],
  },
  {
    name: "a row mid-paragraph, a lazy continuation of the prose above it",
    markdown: `${TABLE}\nProse lead-in.\n${ROW}\n`,
    flagged: [6],
  },
  {
    // The blank line is the only thing keeping the fix from being destructive:
    // without that check the fix would delete the heading.
    name: "a row whose neighbour above is a heading, not a blank line",
    markdown: `${TABLE}# Heading\n${ROW}\n`,
    flagged: [5],
  },
  {
    // A column-0 row is not a continuation of a table indented into a list, so
    // deleting the blank between them would rewrite the file and rejoin
    // nothing — leaving `--fix` to churn on an error it can never clear.
    name: "a row outdented from the list-indented table above it",
    markdown: "- item\n\n  | A | B |\n  | - | - |\n  | 1 | 2 |\n\n| 3 | 4 |\n",
    flagged: [7],
  },
  {
    name: "a run that traces back to prose rather than to a table",
    markdown: "prose\n| 1 | 2 |\n\n| 3 | 4 |\n",
    flagged: [2, 4],
  },
  {
    // Rejoining would hand the table the whole paragraph — a table runs to the
    // next blank line — so the prose tail would be eaten as table rows.
    name: "a severed row whose paragraph continues into prose",
    markdown: `${TABLE}\n${ROW}\nplain prose sentence\n`,
    flagged: [5],
  },
];

for (const { name, markdown, flagged } of REPORTED_ONLY) {
  test(`flagged but not fixed: ${name}`, async () => {
    const errors = await run(markdown);
    assert.deepEqual(
      errors.map((error) => error.lineNumber),
      flagged,
    );
    assert.deepEqual(
      errors.map((error) => error.fixInfo),
      flagged.map(() => null),
    );
    assert.equal(applyFixes(markdown, errors), markdown);
  });
}

const CLEAN = [
  { name: "a well-formed table", markdown: `${TABLE}${ROW}\n` },
  {
    name: "a fenced code block quoting a row",
    markdown: "```\n| `b` | second |\n```\n",
  },
  {
    name: "an indented code block quoting a row",
    markdown: "Prose.\n\n    | `b` | second |\n",
  },
  {
    // The shape test is anchored: only a *leading* pipe starts a row.
    name: "prose with interior pipes",
    markdown: `${TABLE}\nUse \`a | b\` to pipe | like this.\n`,
  },
  {
    // Out of scope by design: deleting a bare blank line would not rejoin a
    // quoted table, whose blank line needs its own `>`.
    name: "a severed row inside a blockquote",
    markdown: "> | A | B |\n> | - | - |\n> | 1 | 2 |\n>\n> | 3 | 4 |\n",
  },
];

for (const { name, markdown } of CLEAN) {
  test(`not flagged: ${name}`, async () => {
    assert.deepEqual(await linesFlagged(markdown), []);
  });
}

test("a long row is reported truncated, a short one in full", async () => {
  const long = `| \`b\` | ${"x".repeat(200)} |`;
  const [longError] = await run(`${TABLE}\n${long}\n`);
  assert.deepEqual(longError.ruleNames, [RULE]);
  assert.equal(longError.errorDetail, DETAIL);
  assert.equal(longError.errorContext, `${long.slice(0, 57)}...`);

  const [shortError] = await run(`${TABLE}\n${ROW}\n`);
  assert.equal(shortError.errorContext, ROW);

  // Exactly at the threshold is short; one over is truncated.
  const exactly60 = `| a |${" ".repeat(54)}|`;
  assert.equal(exactly60.length, 60);
  const [atLimit] = await run(`${TABLE}\n${exactly60}\n`);
  assert.equal(atLimit.errorContext, exactly60);
  const [overLimit] = await run(`${TABLE}\n${exactly60} |\n`);
  assert.equal(overLimit.errorContext, `${exactly60} |`.slice(0, 57) + "...");
});

test("the repo's own markdownlint config catches both defects it exists for", async () => {
  // Drives the real config through the real CLI rather than grepping the file
  // for a path: a config that stopped loading the rule, or that switched MD056
  // off, would keep a grep-style guard passing.
  const { execFileSync } = await import("node:child_process");
  const { mkdtempSync, rmSync, writeFileSync } = await import("node:fs");
  const { join } = await import("node:path");
  const { tmpdir } = await import("node:os");

  const repoRoot = new URL("..", import.meta.url).pathname;
  const lintThroughRepoConfig = (markdown) => {
    const dir = mkdtempSync(join(tmpdir(), "mdl-"));
    try {
      const file = join(dir, "fixture.md");
      writeFileSync(file, markdown);
      // The CLI exits non-zero when it finds anything, which is the point.
      try {
        execFileSync(
          join(repoRoot, "node_modules/.bin/markdownlint-cli2"),
          ["--config", join(repoRoot, ".markdownlint-cli2.jsonc"), file],
          { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
        );
        return "";
      } catch (exit) {
        // Only a real lint failure carries an exit status; anything else (a
        // missing binary, say) must not be mistaken for "found no issues".
        if (exit.status === undefined) throw exit;
        return `${exit.stdout}${exit.stderr}`;
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  };

  assert.match(
    lintThroughRepoConfig(`${TABLE}\n${ROW}\n`),
    new RegExp(`error ${RULE}`),
    "the custom rule must load and fire through the repo config",
  );
  assert.match(
    lintThroughRepoConfig("| A | B |\n| - | - |\n| a `|` b | c |\n"),
    /error MD056/,
    "an unescaped pipe splitting a cell must still be caught",
  );
});
