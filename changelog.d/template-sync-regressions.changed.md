- `check-unscoped-tool-grant` reads its opt-out annotation from the same window
  every other check uses: the grant line plus the unbroken comment block above
  it. One grant can trip both the read and the write class, and only one
  annotation fits the single line the check accepted before, so such a grant had
  no way to opt out of both at once.
