import assert from "node:assert/strict";
import test from "node:test";

import { pinRevs } from "./pin-readme-rev.mjs";

const README = `# pack

\`\`\`yaml
repos:
  - repo: https://github.com/AlexanderMattTurner/ci-truth-serum
    rev: v1.0.0 # the release tag; matches the package version (vX.Y.Z)
    hooks:
      - id: check-tier1
\`\`\`

\`\`\`yaml
- repo: https://github.com/AlexanderMattTurner/ci-truth-serum
  rev: v1.0.0
  hooks:
    - id: check-select
\`\`\`
`;

test("moves every rev pin to the new version", () => {
  const { text, count } = pinRevs(README, "1.1.0");
  assert.equal(count, 2);
  assert.equal(text.match(/rev: v1\.1\.0/g).length, 2);
  assert.equal(text.includes("v1.0.0"), false);
});

test("keeps the trailing comment on the pin line", () => {
  const { text } = pinRevs(README, "1.1.0");
  assert.match(text, /rev: v1\.1\.0 # the release tag/);
});

test("counts nothing when the README shows no pin", () => {
  const { text, count } = pinRevs("# pack\n\nno config here\n", "1.1.0");
  assert.equal(count, 0);
  assert.equal(text, "# pack\n\nno config here\n");
});

test("a version that is not X.Y.Z is refused", () => {
  assert.throws(() => pinRevs(README, "v1.1.0"), /strict X\.Y\.Z/);
  assert.throws(() => pinRevs(README, "1.1"), /strict X\.Y\.Z/);
});

test("a replacement-looking version is inserted verbatim", () => {
  // `String.replace` expands `$&` and `$1` inside a STRING replacement; this
  // module passes a function, so a pin can never grow the matched text.
  assert.throws(() => pinRevs(README, "$&.0.0"), /strict X\.Y\.Z/);
  const { text } = pinRevs("rev: old\n", "9.9.9");
  assert.equal(text, "rev: v9.9.9\n");
});
