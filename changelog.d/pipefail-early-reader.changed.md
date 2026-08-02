`check-pipefail-grep-pipe` now finds three shapes of the SIGPIPE misread that it used to miss, and it over-fires less on the shapes it already found.

- A bounded producer must carry only LITERAL arguments. `printf '%s' "$input" | grep -q …` writes as many bytes as `$input` holds, so it can outrun the pipe buffer. Only `echo hello`-style producers keep the exemption.
- A heredoc body whose first line is a shell shebang is parsed as shell. A script that writes a hook out of `<<'HOOK'` emits shell that later runs, and the check now reads it. Hits point at the enclosing file's line numbers. The descent goes one level.
- The reader set is no longer `grep -q` alone. It adds `grep -m N`, `grep -l`, the `egrep`/`fgrep`/`rg` spellings, `head` without a negative count, and `sed` whose script carries a `q`/`Q` quit command.
- The check now needs the pipeline's exit status to be READ: the condition of an `if`/`while`/`until`/`elif`, an operand of `&&`/`||`, or under `!`. A trailing `|| true` discards the status, so nothing can misread it. This gate is what keeps the wider reader set precise.
