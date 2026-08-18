// Rewrite the README's `rev:` pins to the version a release is cutting.
//
// The README shows consumers a `.pre-commit-config.yaml` they copy-paste, and
// each example pins `rev: vX.Y.Z`. That pin resolves only when it names a tag
// this repo has cut, and the tag comes from package.json (`tag-release.sh`
// reads that file and nothing else). So the pins must move in the same commit
// as the version bump — `tests/cts/test_readme_rev.py` fails the release
// otherwise, which is exactly what stalled v1.1.0.
//
// Usage: node scripts/pin-readme-rev.mjs <version> [readme-path]

import { existsSync, readFileSync, writeFileSync } from "node:fs";

const REV = /^(\s*rev:\s*)(\S+)/gm;
const SEMVER = /^\d+\.\d+\.\d+$/;

/**
 * Point every `rev:` line at `v<version>`.
 *
 * The replacement is a function, never an assembled string: `String.replace`
 * reads `$1`, `$&` and friends inside a string replacement as patterns.
 *
 * @returns {{text: string, count: number}} the rewritten text and how many pins moved.
 */
export const pinRevs = (text, version) => {
  if (!SEMVER.test(version)) {
    throw new Error(`version must be strict X.Y.Z, got: ${version}`);
  }
  let count = 0;
  const out = text.replace(REV, (_match, prefix) => {
    count += 1;
    return `${prefix}v${version}`;
  });
  return { text: out, count };
};

const main = (argv) => {
  const [version, readme = "README.md"] = argv;
  if (!version) {
    throw new Error("usage: node scripts/pin-readme-rev.mjs <version> [readme-path]");
  }
  if (!existsSync(readme)) {
    // Same reason as the no-pin case below: a repo without this file still
    // releases, and this repo's own README is guarded by its rev test.
    console.log(`${readme} does not exist; nothing to move.`);
    return;
  }
  const { text, count } = pinRevs(readFileSync(readme, "utf8"), version);
  if (count === 0) {
    // A repo whose README shows no consumer config has nothing to pin. Say so
    // and leave the file alone: this script also runs in downstream repos that
    // ship no `rev:` example, and a hard error there would block their release.
    console.log(`${readme} documents no \`rev:\` pin; nothing to move.`);
    return;
  }
  writeFileSync(readme, text);
  console.log(`Pinned ${count} rev(s) in ${readme} to v${version}`);
};

if (import.meta.url === `file://${process.argv[1]}`) {
  main(process.argv.slice(2));
}
