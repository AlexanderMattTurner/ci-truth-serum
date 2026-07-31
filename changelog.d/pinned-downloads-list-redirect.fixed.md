- `check-pinned-downloads` no longer reports a command because a LATER `&&`
  branch redirects to a file. tree-sitter wraps `a && b > f` in one
  `redirected_statement` around the whole list, but bash gives `> f` to `b`
  alone; crediting every branch made an `apt-get install … curl …` look like a
  fetch that saved a file, and the bogus finding then squeezed the real download
  beside it out of its own verification window. A `{ …; } > f` / `( …; ) > f`
  group still covers every command inside it.
