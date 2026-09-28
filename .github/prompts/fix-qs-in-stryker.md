# Fix: qs vulnerability in @stryker-mutator/core

## Background

`pnpm audit` reports three MODERATE vulnerabilities in `qs` via the chain:

```text
@stryker-mutator/core@10.0.0 > typed-rest-client@2.3.1 > qs@6.15.1
```

- GHSA-q8mj-m7cp-5q26: qs.stringify crashes on null/undefined in comma-format arrays
- GHSA-x5fp-wj9c-mxmx: qs array-limit bypass via bracket-key comma parsing
- GHSA-4mjr-xmp4-gh2g: qs Denial of Service via attacker-controlled isBuffer

All three are fixed in `qs@>=6.16.0`.

## Why it was deferred

`typed-rest-client@2.3.1` pins `qs` to exactly `6.15.1`. The only patch
is to upgrade `typed-rest-client` to v3.x (which requires `qs@^6.16.0`),
but `@stryker-mutator/core@10.0.0` depends on `typed-rest-client@~2.3.0`.
`pnpm.json` overrides did not take effect in the pnpm v11.8.0 environment.

## Remediation options

1. Wait for `@stryker-mutator/core@10.x` to bump `typed-rest-client` to v3.
2. If pnpm.json overrides start working (pnpm v11 config format is in flux),
   add `{ "overrides": { "qs": "^6.16.0" } }` to `pnpm.json`.
3. Use `.pnpmfile.cjs` to rewrite the `qs` version constraint at install time.

## Risk

These vulnerabilities are in a dev-only mutation-testing tool. They are not
reachable in production or in normal CI. The risk to end users is nil.
