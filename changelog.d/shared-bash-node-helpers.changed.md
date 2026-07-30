- Shell lints now read a braced `${VAR}` argument uniformly. The set of
  `command` children counted as arguments lived in seven separate copies, six of
  which omitted the `expansion` node type — so `pip install ${PKG}`,
  `curl -o ${out} url` and friends were read with that token missing from the
  argument list. All lints now share one set, and it includes `expansion`.
