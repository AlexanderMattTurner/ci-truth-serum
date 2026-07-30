- The four comment-reading lints now locate comments in JavaScript/TypeScript
  with a real grammar (tree-sitter-javascript / tree-sitter-typescript) instead
  of scanning for a `//` delimiter, closing the last language for which "is this
  a comment?" was answered by the text. Measured over 241 real `.mjs`/`.js`/`.ts`
  files: 169 lines the delimiter scan called comments are not (a `#!` hashbang, a
  `**bold**` line inside a template literal read as a `/* … */` continuation),
  and 250 real comment lines it missed (an inline `/** @type {…} */` cast, a
  block comment's opening and closing lines). A `.ts` file is parsed by the
  TypeScript grammar and a `.tsx` by its own, since under the wrong one a type
  annotation is a syntax error that drops every comment after it.
  The four: `check-drift-guards`, `check-historical-comments`,
  `check-workflow-refs`, `check-graceful-handwave`. <!-- allow-graceful: names the lint whose banned word this is; it makes no claim about behaviour -->
- The same lints now number physical lines by `\n`, matching the line numbering
  every grammar reports. `str.splitlines()` also breaks on `\v`, `\f`, `\x85` and
  U+2028/U+2029, so a file containing one of those had every later reported line
  number off by the difference.
- Three of those hooks declared **no** `additional_dependencies` while already
  importing PyYAML through `_linecheck` — latent until a downstream repo enabled
  one of the ids directly rather than through the `check-extras` aggregate. All
  now declare PyYAML plus the grammar bindings.
- New shared module `ci_truth_serum/_comments.py` is the one place a lint asks
  which lines carry narration: `tokenize` for Python, tree-sitter-bash for shell,
  tree-sitter-{javascript,typescript} for JS/TS, and a documented delimiter scan
  only for a language with no grammar here (YAML). The four lints previously
  routed this three different ways.
