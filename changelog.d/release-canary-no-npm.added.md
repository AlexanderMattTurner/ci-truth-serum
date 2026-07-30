- `release-canary --no-npm`: skip the npm marker (and its network call) for a
  repo whose releases are git tags only, comparing the remaining markers. It is
  an explicit opt-out, never inferred from an absent package.
