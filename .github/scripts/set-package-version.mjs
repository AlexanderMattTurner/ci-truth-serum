// Set the "version" field of a package.json, keeping prettier's 2-space indent and
// trailing newline. Usage: NEW_VERSION=X.Y.Z node set-package-version.mjs <package.json>
import fs from "node:fs";

const file = process.argv[2];
const version = process.env.NEW_VERSION;
if (!file || !version) {
  throw new Error("usage: NEW_VERSION=X.Y.Z node set-package-version.mjs <package.json>");
}
const pkg = JSON.parse(fs.readFileSync(file, "utf8"));
pkg.version = version;
fs.writeFileSync(file, JSON.stringify(pkg, null, 2) + "\n");
