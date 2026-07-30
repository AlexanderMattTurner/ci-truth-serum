# Shell lints parse the grammar, not the text

**When a lint's question is about shell _structure_, answer it with `_bash_ast`
(tree-sitter-bash). Never with a regex, a quote-state scanner, or `shlex`.**

Structural questions — the ones a text scan cannot answer, only approximate:

| Question                                     | The node that answers it                                 |
| -------------------------------------------- | -------------------------------------------------------- |
| Is this a command, or text a command prints? | `command` vs a `string` argument                         |
| Is this token an argument or a redirection?  | `word` vs `file_redirect` (a **sibling** of the command) |
| Is this one command or two?                  | `list` / `pipeline` children                             |
| Does this `;` separate commands?             | it does not, inside a `string`                           |
| Is this body executed?                       | `heredoc_body` is data; a shell's `-c` argument is not   |
| Is this value fixed or computed?             | `expansion` / `command_substitution` child               |
| Where does this command start and end?       | `start_point` / `end_point`, continuations included      |

## The tell

You are on the wrong side of this rule the moment you write any of these:

- a character walk tracking whether you are inside `'` or `"`;
- a "find the previous `&&`/`;`/`|`" segment splitter;
- `shlex.split` to recover a command's arguments;
- a regex asserting a command boundary (`(?:^|[\s;&|(])cmd`);
- a filter dropping `>&2`-shaped tokens from an argument list.

Each is a re-implementation of the grammar, and each one gets a different subset
of real shell wrong. `_bash_ast`'s own docstring records the first round of this:
two lints' hand-rolled quote/heredoc state machines "mis-parsed real shell", which
is why the module exists. `check-versionless-install` grew every one of the five
above and shipped four false positives in a day — help text read as a command, a
`;` inside quotes starting a new command, `>&2` read as a package name, an
interpreter name in a message counting as an executor — before being rewritten on
the grammar, after which the class disappeared rather than shrinking by one.

## Text scanning is still right for text

Use the line-oriented helpers (`_linecheck`) when the question is genuinely about
text rather than structure: reading an `# annotation:` out of a comment, judging
prose in a doc, matching a version string's shape, or scanning a file format that
is not shell at all. `strip_comments` is not a substitute for parsing — it uses
the grammar to blank comments and then hands you text.

## Cost, and the one carve-out

`parse()` is not free: it refuses pathological input (`PathologicalInputError`,
which a lint must surface **loudly** — see `check_untrusted_exec.main`) and costs
a parse per file. That is the price of being right about shell, and every lint in
this pack already pays it through the aggregate. A text scan is only justified
when the input is not shell; say so in the module docstring when you take that
route, so the next reader knows it was a decision.

## Audit — the two probes every shell lint should survive

Measured by feeding each one its own banned idiom twice: once inside a logger's
message string (`gb_warn "…"`), once inside a heredoc body written to a file.
Neither is executed code, so a finding is a false positive:

| Lint                              | Fires on a message string | Fires on a heredoc body |
| --------------------------------- | ------------------------- | ----------------------- |
| `check_exit_suppression`          | no                        | no                      |
| `check_echo_fallback`             | no                        | no                      |
| `check_stderr_merge_parse`        | no                        | no                      |
| `check_pinned_downloads`          | no                        | no                      |
| `check_gh_slurp_jq`               | no                        | no                      |
| `check_stderr_suppression`        | no                        | no                      |
| `check_substitution_exit_swallow` | no                        | no                      |
| `check_secret_file_perms`         | no                        | no                      |
| `check_drift_guards`              | no                        | no                      |

The top five fired on one or both probes and were rewritten on the grammar, which
removed the class rather than the instance. **The bottom three still scan text** —
they pass these two probes, which is not the same as being structurally sound, so
they are the remaining candidates: `check_stderr_suppression` and
`check_substitution_exit_swallow` both ask redirect/substitution questions the
grammar answers directly, and `check_secret_file_perms` reasons about command
ordering.

`check_drift_guards` is the ninth row and a later, separate instance of the same
story: its laundered-copy trigger shipped reading comments out of the text, fired
on the heredoc probe the first time the probe was run against it, and was moved
onto `shell_comments` (`iter_nodes(parse(script), "comment")`) before landing.
Note which half moved — only "where does a comment start", the structural
question. Judging the PROSE inside that comment stays a regex, because English
has no grammar here to parse; that is the carve-out above, not a second thing to
fix. Its Python half has the same split and answers the same question with
`tokenize`. Its JS/TS half still scans text, and that is the honest remaining
gap: no JS grammar is a dependency here yet.

Reproduce with `violations()` on `gb_warn "<idiom>"` and on a
`cat <<'EOF' > doc.txt` block containing the idiom — that pair is the cheapest
audit of a shell lint you will ever run, and both cases are text no shell
executes.

A rewrite has to preserve every verdict its existing suite pins: that is the
evidence the rule was kept and only the decision procedure changed. State in the
commit message how many assertions moved and why — for these five it was one line
number in `check_gh_slurp_jq`, one changed meta-test shape in
`check_exit_suppression`, and zero in the other three.
