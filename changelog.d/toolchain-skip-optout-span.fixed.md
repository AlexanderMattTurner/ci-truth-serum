- `check-toolchain-skips` now accepts its `# toolchain-skip-ok: <reason>`
  annotation on any physical line of the flagged call, not only the line it is
  reported on or the one above. A multi-line `@pytest.mark.skipif(` is reported
  at the decorator, while the reason naturally sits beside the condition it
  explains several lines below — so an annotated skip was flagged anyway, and
  the only way to silence it was to move the comment above the decorator, where
  it describes a condition the reader has not reached.
