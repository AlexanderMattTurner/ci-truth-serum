import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { ERROR_WORDS, distinct, hasErrorText, headAndTail, report, signalName } from "./report.mjs";

function ledgerDir(files) {
  const dir = mkdtempSync(join(tmpdir(), "mute-report-"));
  for (const [name, text] of Object.entries(files)) writeFileSync(join(dir, name), text);
  return dir;
}

const LEDGER = "build\t41\tbin/lib/msg-emit.bash:19\t1\t7\t_gb_scrub \"$@\"\n";

test("a failed step that printed no error text gets a mute-failure error", () => {
  const dir = ledgerDir({
    steps: "checkout\t0\nbuild\t1\n",
    "step-build.out": "✗ \n",
    ledger: LEDGER,
  });
  const lines = report(dir);
  assert.ok(lines.includes("Step build exited 1."));
  assert.ok(lines.includes(`${LEDGER.trimEnd()}\t(1x)`));
  assert.deepEqual(
    lines.filter((line) => line.startsWith("::error")),
    [
      "::error title=mute failure::mute failure: step build exited 1 and printed no line with error text. " +
        "Its last failed commands are in the mute-failure-guard report above.",
    ],
  );
});

test("a failed step that named its error gets the report and no mute-failure error", () => {
  const dir = ledgerDir({
    steps: "build\t1\n",
    "step-build.out": "building\nError: the archive is missing\n",
    ledger: LEDGER,
  });
  const lines = report(dir);
  assert.ok(lines.includes(`${LEDGER.trimEnd()}\t(1x)`));
  assert.deepEqual(lines.filter((line) => line.startsWith("::error")), []);
});

test("a consumer's extra error words count as error text", () => {
  const dir = ledgerDir({ steps: "build\t2\n", "step-build.out": "✗ boom\n" });
  assert.equal(report(dir).filter((l) => l.startsWith("::error")).length, 1);
  assert.equal(report(dir, [...ERROR_WORDS, "boom"]).filter((l) => l.startsWith("::error")).length, 0);
});

test("a job with no recorded failed step says so instead of reporting nothing", () => {
  const lines = report(ledgerDir({ steps: "build\t0\n" }));
  assert.ok(lines.some((line) => line.startsWith("No guarded bash step recorded a non-zero exit")));
  assert.deepEqual(lines.filter((line) => line.startsWith("::error")), []);
});

test("a failed step's first failure is printed when the step has more than 50 rows", () => {
  // A live check that fails first in a long pytest step leaves the oldest row of the step.
  const rows = Array.from({ length: 60 }, (_, i) => `build\t1\tx.sh:${i + 1}\t1\t5\tfalse`);
  const lines = report(ledgerDir({ steps: "build\t1\n", "step-build.out": "fail\n", ledger: rows.join("\n") }));
  const shown = (row) => lines.filter((line) => line === `${row}\t(1x)`).length;
  assert.deepEqual([shown(rows[0]), shown(rows[30]), shown(rows[59])], [1, 1, 1]);
});

test("a failed step past its row limit keeps its first and last rows and counts the rest", () => {
  const rows = ["a", "b", "c", "d", "e", "f", "g"];
  assert.deepEqual(headAndTail(rows, 4), ["a", "b", "... 3 rows not shown ...", "f", "g"]);
  assert.deepEqual(headAndTail(rows, 7), rows);
});

test("rows from the steps that passed are cut to the last 50", () => {
  const rows = Array.from({ length: 60 }, (_, i) => `setup\t1\tx.sh:${i + 1}\t1\t5\tfalse`);
  const lines = report(ledgerDir({ steps: "setup\t0\nbuild\t1\n", "step-build.out": "fail\n", ledger: rows.join("\n") }));
  assert.ok(lines.includes("Ledger rows from the other steps: the last 50 of 60 distinct rows (60 rows in the whole ledger)"));
  const shown = (row) => lines.filter((line) => line === `${row}\t(1x)`).length;
  assert.deepEqual([shown(rows[59]), shown(rows[10]), shown(rows[9])], [1, 1, 0]);
});

test("a probe that fails on every call is one row with its count, kept where it last failed", () => {
  const probe = (pid) => `build\t${pid}\tlib/python.bash:165\t1\t4\tsubshell exit, first command: command -v python3.14`;
  const cause = "build\t9\tlib/msg-emit.bash:19\t1\t4\tsubshell exit, first command: _gb_scrub";
  const rows = [probe(1), probe(2), cause, probe(3)];
  assert.deepEqual(distinct(rows), [`${cause}\t(1x)`, `${probe(3)}\t(3x)`]);
});

test("the failed step's own rows come before the other steps' rows", () => {
  const other = "lint\t5\ta.sh:1\t1\t4\tfalse";
  const mine = "build\t6\tb.sh:2\t2\t4\tgrep x";
  const lines = report(ledgerDir({ steps: "build\t2\n", "step-build.out": "error\n", ledger: `${mine}\n${other}\n` }));
  const head = lines.indexOf("Ledger rows from step build, in the order each row last failed: 1 distinct");
  assert.equal(lines[head + 1], `${mine}\t(1x)`);
  assert.ok(lines.indexOf(`${other}\t(1x)`) > head + 1);
});

test("an exit status above 128 names its signal", () => {
  assert.equal(signalName(137), "SIGKILL");
  assert.equal(signalName(143), "SIGTERM");
  assert.equal(signalName(1), "");
});

test("error text is matched per line and ignores case", () => {
  assert.equal(hasErrorText("ok\nFATAL: no disk\n", ERROR_WORDS), true);
  assert.equal(hasErrorText("✗ \n\n", ERROR_WORDS), false);
});

test("a process count that ps could not read says so instead of claiming one process", () => {
  const path = process.env.PATH;
  process.env.PATH = "/nonexistent";
  try {
    const lines = report(ledgerDir({ steps: "build\t1\n", "step-build.out": "error\n" }));
    const count = lines.find((line) => line.startsWith("processes on the host: "));
    assert.match(count, /^processes on the host: unreadable \(/);
  } finally {
    process.env.PATH = path;
  }
});
